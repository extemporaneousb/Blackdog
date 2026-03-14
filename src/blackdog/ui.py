from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Condition
from typing import Any, Callable
from urllib.parse import quote, unquote, urlsplit
import http.client
import json
import mimetypes
import os
import webbrowser

from .backlog import (
    BacklogError,
    build_plan_view,
    build_view_model,
    classify_task_status,
    load_backlog,
    sync_state_for_backlog,
)
from .config import ConfigError, Profile, ProjectPaths
from .store import StoreError, load_events, load_inbox, load_state, load_task_results, now_iso


UI_SNAPSHOT_SCHEMA_VERSION = 1
UI_SERVER_STATE_NAME = "ui-server.json"


class UIError(RuntimeError):
    pass


def ui_server_state_file(paths: ProjectPaths) -> Path:
    return paths.supervisor_runs_dir / UI_SERVER_STATE_NAME


def read_ui_server_state(paths: ProjectPaths) -> dict[str, Any] | None:
    state_file = ui_server_state_file(paths)
    if not state_file.exists():
        return None
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def notify_ui_server(paths: ProjectPaths) -> bool:
    payload = read_ui_server_state(paths)
    if payload is None:
        return False
    host = str(payload.get("host") or "127.0.0.1")
    port = payload.get("port")
    if not isinstance(port, int) or port < 1:
        return False
    connection = http.client.HTTPConnection(host, port, timeout=0.25)
    try:
        connection.request("POST", "/api/notify", body=b"")
        response = connection.getresponse()
        response.read()
        return 200 <= response.status < 300
    except OSError:
        return False
    finally:
        connection.close()


def _artifact_href(paths: ProjectPaths, path: str | Path | None) -> str | None:
    if not path:
        return None
    candidate = Path(path).resolve()
    try:
        relative = candidate.relative_to(paths.backlog_dir.resolve())
    except ValueError:
        return None
    return "/artifacts/" + quote(relative.as_posix(), safe="/")


def _find_run_dir(paths: ProjectPaths, run_id: str) -> Path | None:
    matches = sorted(paths.supervisor_runs_dir.glob(f"*-{run_id}"))
    return matches[0].resolve() if matches else None


def _child_artifacts(paths: ProjectPaths, run_dir: Path | None, task_id: str) -> dict[str, Any]:
    if run_dir is None:
        return {
            "run_dir": None,
            "run_href": None,
            "prompt_href": None,
            "stdout_href": None,
            "stderr_href": None,
        }
    child_dir = run_dir / task_id
    return {
        "run_dir": str(child_dir),
        "run_href": _artifact_href(paths, child_dir),
        "prompt_href": _artifact_href(paths, child_dir / "prompt.txt"),
        "stdout_href": _artifact_href(paths, child_dir / "stdout.log"),
        "stderr_href": _artifact_href(paths, child_dir / "stderr.log"),
    }


def _build_supervisor_runs(paths: ProjectPaths, events: list[dict[str, Any]], *, limit: int = 6) -> dict[str, Any]:
    runs: dict[str, dict[str, Any]] = {}
    for event in events:
        event_type = str(event.get("type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            continue
        run = runs.setdefault(
            run_id,
            {
                "run_id": run_id,
                "actor": str(event.get("actor") or ""),
                "status": "running",
                "started_at": None,
                "finished_at": None,
                "workspace_mode": "",
                "task_ids": [],
                "children": {},
                "last_event_at": "",
            },
        )
        run["last_event_at"] = str(event.get("at") or run["last_event_at"])
        if event_type == "supervisor_run_started":
            run["actor"] = str(event.get("actor") or run["actor"])
            run["started_at"] = str(event.get("at") or "")
            run["workspace_mode"] = str(payload.get("workspace_mode") or "")
            run["task_ids"] = [str(item) for item in payload.get("task_ids") or []]
        elif event_type == "supervisor_run_finished":
            run["status"] = "finished"
            run["finished_at"] = str(event.get("at") or "")
        elif event_type in {"child_launch", "child_launch_failed", "child_finish"}:
            task_id = str(event.get("task_id") or "")
            child_agent = str(payload.get("child_agent") or task_id or "child")
            child = run["children"].setdefault(
                child_agent,
                {
                    "child_agent": child_agent,
                    "task_id": task_id,
                    "status": "pending",
                    "workspace": None,
                    "pid": None,
                    "exit_code": None,
                    "timed_out": False,
                    "final_task_status": None,
                },
            )
            if task_id:
                child["task_id"] = task_id
            if event_type == "child_launch":
                child["status"] = "running"
                child["workspace"] = payload.get("workspace")
                child["pid"] = payload.get("pid")
            elif event_type == "child_launch_failed":
                child["status"] = "launch-failed"
                child["error"] = payload.get("error")
            elif event_type == "child_finish":
                child["status"] = "finished"
                child["exit_code"] = payload.get("exit_code")
                child["timed_out"] = bool(payload.get("timed_out"))
                child["final_task_status"] = payload.get("final_task_status")

    ordered_runs: list[dict[str, Any]] = []
    for run in runs.values():
        run_dir = _find_run_dir(paths, str(run["run_id"]))
        children: list[dict[str, Any]] = []
        for child in sorted(run["children"].values(), key=lambda row: (str(row.get("task_id") or ""), str(row.get("child_agent") or ""))):
            artifacts = _child_artifacts(paths, run_dir, str(child.get("task_id") or ""))
            children.append({**child, **artifacts})
        ordered_runs.append(
            {
                "run_id": run["run_id"],
                "actor": run["actor"],
                "status": run["status"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "workspace_mode": run["workspace_mode"],
                "task_ids": run["task_ids"],
                "run_dir": str(run_dir) if run_dir is not None else None,
                "run_href": _artifact_href(paths, run_dir) if run_dir is not None else None,
                "children": children,
                "last_event_at": run["last_event_at"],
            }
        )
    ordered_runs.sort(key=lambda row: str(row.get("last_event_at") or ""), reverse=True)
    return {
        "active_runs": [row for row in ordered_runs if row.get("status") != "finished"],
        "recent_runs": ordered_runs[:limit],
    }


def _load_supervisor_loops(paths: ProjectPaths, *, limit: int = 6) -> list[dict[str, Any]]:
    status_files = sorted(
        paths.supervisor_runs_dir.glob("*-loop-*/status.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    loops: list[dict[str, Any]] = []
    for status_file in status_files[:limit]:
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        cycles = payload.get("cycles")
        cycle_count = len(cycles) if isinstance(cycles, list) else 0
        last_cycle_status = None
        if isinstance(cycles, list) and cycles:
            last_cycle_status = cycles[-1].get("status")
        loops.append(
            {
                "loop_id": payload.get("loop_id"),
                "actor": payload.get("actor"),
                "status": payload.get("final_status") or last_cycle_status or "running",
                "workspace_mode": payload.get("workspace_mode"),
                "cycle_count": cycle_count,
                "last_cycle_status": last_cycle_status,
                "completed_at": payload.get("completed_at"),
                "status_file": str(status_file),
                "status_href": _artifact_href(paths, status_file),
            }
        )
    return loops


def build_ui_snapshot(profile: Profile) -> dict[str, Any]:
    snapshot = load_backlog(profile.paths, profile)
    state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
    events = load_events(profile.paths)
    messages = load_inbox(profile.paths)
    results = load_task_results(profile.paths)
    summary = build_view_model(
        profile,
        snapshot,
        state,
        events=events[-20:],
        messages=messages,
        results=results,
    )
    plan = build_plan_view(profile, snapshot, state)
    graph_tasks: list[dict[str, Any]] = []
    graph_edges: list[dict[str, str]] = []
    ordered_tasks = sorted(snapshot.tasks.values(), key=lambda task: ((task.wave or 9999), (task.lane_order or 9999), task.id))
    for task in ordered_tasks:
        status, detail = classify_task_status(task, snapshot, state, allow_high_risk=False)
        graph_tasks.append(
            {
                "id": task.id,
                "title": task.title,
                "status": status,
                "detail": detail,
                "wave": task.wave,
                "lane_id": task.lane_id,
                "lane_title": task.lane_title,
                "epic_title": task.epic_title,
                "priority": task.payload["priority"],
                "risk": task.payload["risk"],
                "objective": task.payload.get("objective") or "",
                "domains": list(task.payload.get("domains", [])),
                "safe_first_slice": task.payload["safe_first_slice"],
                "predecessor_ids": list(task.predecessor_ids),
            }
        )
        for predecessor_id in task.predecessor_ids:
            graph_edges.append({"from": predecessor_id, "to": task.id})

    recent_results = []
    for row in summary["recent_results"]:
        recent_results.append(
            {
                "task_id": row.get("task_id"),
                "status": row.get("status"),
                "actor": row.get("actor"),
                "recorded_at": row.get("recorded_at"),
                "result_file": row.get("result_file"),
                "result_href": _artifact_href(profile.paths, row.get("result_file")),
            }
        )

    return {
        "schema_version": UI_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "control_dir": str(profile.paths.control_dir),
        "profile_file": str(profile.paths.profile_file),
        "counts": summary["counts"],
        "total": summary["total"],
        "push_objective": summary["push_objective"],
        "objectives": summary["objectives"],
        "next_rows": summary["next_rows"],
        "open_messages": summary["open_messages"][:10],
        "recent_results": recent_results,
        "recent_events": summary["recent_events"],
        "plan": plan,
        "graph": {
            "tasks": graph_tasks,
            "edges": graph_edges,
        },
        "supervisor": {
            **_build_supervisor_runs(profile.paths, events),
            "loops": _load_supervisor_loops(profile.paths),
        },
        "links": {
            "backlog": "/artifacts/backlog.md",
            "static_html": "/artifacts/backlog-index.html",
            "events": "/artifacts/events.jsonl",
            "inbox": "/artifacts/inbox.jsonl",
        },
    }


def _snapshot_error_payload(profile: Profile, exc: Exception) -> dict[str, Any]:
    return {
        "schema_version": UI_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "control_dir": str(profile.paths.control_dir),
        "profile_file": str(profile.paths.profile_file),
        "error": {
            "type": exc.__class__.__name__,
            "message": str(exc),
        },
    }


class _UIServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, profile: Profile, host: str, port: int) -> None:
        super().__init__((host, port), _UIRequestHandler)
        self.profile = profile
        self._condition = Condition()
        self._revision = 0
        self._snapshot: dict[str, Any] = {}
        self._snapshot_bytes = b""
        self.refresh_snapshot()

    def current_snapshot(self) -> tuple[int, dict[str, Any], bytes]:
        with self._condition:
            return self._revision, self._snapshot, self._snapshot_bytes

    def refresh_snapshot(self) -> tuple[int, dict[str, Any], bytes]:
        try:
            snapshot = build_ui_snapshot(self.profile)
        except (BacklogError, ConfigError, StoreError, OSError) as exc:
            snapshot = _snapshot_error_payload(self.profile, exc)
        snapshot_bytes = json.dumps(snapshot, indent=2, sort_keys=True).encode("utf-8")
        with self._condition:
            self._revision += 1
            self._snapshot = snapshot
            self._snapshot_bytes = snapshot_bytes
            self._condition.notify_all()
            return self._revision, self._snapshot, self._snapshot_bytes


class _UIRequestHandler(BaseHTTPRequestHandler):
    server: _UIServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/":
            self._send_text(HTTPStatus.OK, _render_ui_shell(), "text/html; charset=utf-8")
            return
        if parts.path == "/api/snapshot":
            _, _, snapshot_bytes = self.server.current_snapshot()
            self._send_bytes(HTTPStatus.OK, snapshot_bytes, "application/json; charset=utf-8")
            return
        if parts.path == "/api/stream":
            self._serve_stream()
            return
        if parts.path.startswith("/artifacts/"):
            self._serve_artifact(parts.path.removeprefix("/artifacts/"))
            return
        if parts.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self._send_text(HTTPStatus.NOT_FOUND, "Not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        if parts.path != "/api/notify":
            self._send_text(HTTPStatus.NOT_FOUND, "Not found\n", "text/plain; charset=utf-8")
            return
        self.server.refresh_snapshot()
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def _send_bytes(self, status: HTTPStatus, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_text(self, status: HTTPStatus, payload: str, content_type: str) -> None:
        self._send_bytes(status, payload.encode("utf-8"), content_type)

    def _serve_stream(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        revision, _, snapshot_bytes = self.server.current_snapshot()
        self._write_sse_event(revision, snapshot_bytes)
        try:
            while True:
                with self.server._condition:
                    while revision == self.server._revision:
                        self.server._condition.wait()
                    revision = self.server._revision
                    snapshot_bytes = self.server._snapshot_bytes
                self._write_sse_event(revision, snapshot_bytes)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _write_sse_event(self, revision: int, snapshot_bytes: bytes) -> None:
        self.wfile.write(f"id: {revision}\n".encode("utf-8"))
        self.wfile.write(b"event: snapshot\n")
        for line in snapshot_bytes.decode("utf-8").splitlines():
            self.wfile.write(f"data: {line}\n".encode("utf-8"))
        self.wfile.write(b"\n")
        self.wfile.flush()

    def _serve_artifact(self, relative_path: str) -> None:
        backlog_dir = self.server.profile.paths.backlog_dir.resolve()
        candidate = (backlog_dir / Path(unquote(relative_path))).resolve()
        if candidate != backlog_dir and backlog_dir not in candidate.parents:
            self._send_text(HTTPStatus.NOT_FOUND, "Not found\n", "text/plain; charset=utf-8")
            return
        if not candidate.exists() or candidate.is_dir():
            self._send_text(HTTPStatus.NOT_FOUND, "Not found\n", "text/plain; charset=utf-8")
            return
        content_type, _ = mimetypes.guess_type(candidate.name)
        self._send_bytes(
            HTTPStatus.OK,
            candidate.read_bytes(),
            content_type or "application/octet-stream",
        )


def _render_ui_shell() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Blackdog Live UI</title>
  <style>
    :root {
      --bg: #f7f1ea;
      --panel: rgba(255, 252, 248, 0.95);
      --panel-strong: rgba(255, 255, 255, 0.9);
      --ink: #201913;
      --muted: #6d635b;
      --line: rgba(57, 43, 28, 0.14);
      --ready: #9c5a13;
      --claimed: #155fc1;
      --done: #12724a;
      --waiting: #7b8088;
      --approval: #7b4bb7;
      --risk: #a12626;
      --running: #b86716;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, rgba(208, 168, 97, 0.2), transparent 28%),
        linear-gradient(180deg, #fbf7f1 0%, var(--bg) 100%);
    }
    a { color: inherit; }
    .page { max-width: 1680px; margin: 0 auto; padding: 28px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 20px;
      margin-bottom: 18px;
    }
    .hero { display: grid; grid-template-columns: 1.4fr 1fr; gap: 18px; }
    .hero-top {
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 16px;
    }
    .brand {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--muted);
    }
    .brand-mark {
      width: 36px;
      height: 36px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: linear-gradient(135deg, #201913, #7a4a20);
      color: white;
      display: inline-grid;
      place-items: center;
      font-size: 0.82rem;
    }
    .stats { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
    .stat, .strip-card, .run-card, .loop-card, .message-card, .result-card, .task-node, .objective, .lane {
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.78);
    }
    .stat { padding: 12px; }
    .stat strong { display: block; font-size: 1.6rem; }
    .eyebrow { color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.75rem; }
    .sync-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      padding: 8px 12px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
      white-space: nowrap;
    }
    .sync-dot {
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--waiting);
    }
    .sync-pill.live .sync-dot { background: var(--done); }
    .sync-pill.error .sync-dot { background: var(--risk); }
    .hero-meta {
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 0.92rem;
      overflow-wrap: anywhere;
    }
    .hero-links, .inline-links {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 12px;
    }
    .hero-links a, .inline-links a {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 8px 12px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
      text-decoration: none;
    }
    .objectives, .supervisor-strip, .message-grid, .result-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }
    .objective, .strip-card, .run-card, .loop-card, .message-card, .result-card {
      padding: 14px;
      display: grid;
      gap: 8px;
    }
    .layout { display: grid; grid-template-columns: 1.55fr 0.95fr; gap: 18px; }
    .dag-shell {
      position: relative;
      overflow: auto;
      padding-bottom: 8px;
    }
    .dag-links {
      position: absolute;
      inset: 0;
      pointer-events: none;
      overflow: visible;
    }
    .dag-links path {
      fill: none;
      stroke: rgba(122, 74, 32, 0.28);
      stroke-width: 2;
    }
    .dag-columns {
      position: relative;
      display: flex;
      gap: 18px;
      min-width: fit-content;
      align-items: start;
    }
    .wave-column {
      min-width: 280px;
      display: grid;
      gap: 12px;
    }
    .lane {
      padding: 14px;
      display: grid;
      gap: 12px;
    }
    .task-node {
      padding: 12px;
      display: grid;
      gap: 8px;
      background: rgba(255, 255, 255, 0.9);
    }
    .task-ready { border-color: rgba(156, 90, 19, 0.35); }
    .task-claimed { border-color: rgba(21, 95, 193, 0.35); }
    .task-done { border-color: rgba(18, 114, 74, 0.35); }
    .task-waiting, .task-high-risk { border-color: rgba(123, 128, 136, 0.35); }
    .task-approval { border-color: rgba(123, 75, 183, 0.35); }
    .meta { color: var(--muted); font-size: 0.92rem; }
    .chips { display: flex; flex-wrap: wrap; gap: 8px; }
    .chip {
      display: inline-flex;
      padding: 4px 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      font-size: 0.8rem;
      background: rgba(255, 255, 255, 0.65);
    }
    .empty {
      padding: 14px;
      border-radius: 18px;
      border: 1px dashed var(--line);
      color: var(--muted);
      background: rgba(255,255,255,0.45);
    }
    .error {
      color: var(--risk);
      border-color: rgba(161, 38, 38, 0.3);
      background: rgba(255, 243, 243, 0.9);
    }
    .run-children { display: grid; gap: 10px; }
    .child-row {
      padding-top: 10px;
      border-top: 1px solid var(--line);
      display: grid;
      gap: 8px;
    }
    @media (max-width: 1100px) {
      .hero, .layout { grid-template-columns: 1fr; }
      .stats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    @media (max-width: 700px) {
      .page { padding: 16px; }
      .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
  </style>
</head>
<body>
  <div class="page">
    <section class="panel hero">
      <div>
        <div class="hero-top">
          <div>
            <div class="brand"><span class="brand-mark">BD</span><span>Blackdog Live</span></div>
            <h1 id="project-name">Blackdog</h1>
            <p id="repo-root" class="hero-meta"></p>
          </div>
          <div id="sync-status" class="sync-pill"><span class="sync-dot"></span><span>Connecting…</span></div>
        </div>
        <p id="push-objective">Loading snapshot…</p>
        <div id="stats" class="stats"></div>
        <div id="hero-links" class="hero-links"></div>
      </div>
      <div>
        <h2>Supervisor Monitor</h2>
        <div id="supervisor-strip" class="supervisor-strip"></div>
      </div>
    </section>
    <section class="panel">
      <h2>Objectives</h2>
      <div id="objectives" class="objectives"></div>
    </section>
    <div class="layout">
      <section class="panel">
        <h2>Task Graph</h2>
        <div id="dag" class="dag-shell"></div>
      </section>
      <section class="panel">
        <h2>Inbox</h2>
        <div id="messages" class="message-grid"></div>
        <h2>Recent Results</h2>
        <div id="results" class="result-grid"></div>
      </section>
    </div>
    <section class="panel">
      <h2>Supervisor Runs</h2>
      <div id="runs"></div>
    </section>
  </div>
  <script>
    let currentSnapshot = null;
    let eventSource = null;

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function statusChip(label) {
      return `<span class="chip">${escapeHtml(label)}</span>`;
    }

    function renderStats(snapshot) {
      const counts = snapshot.counts || {};
      const rows = [
        ["Total", snapshot.total || 0],
        ["Ready", counts.ready || 0],
        ["Claimed", counts.claimed || 0],
        ["Done", counts.done || 0],
        ["Approval", counts.approval || 0],
        ["Open inbox", (snapshot.open_messages || []).length],
      ];
      document.getElementById("stats").innerHTML = rows
        .map(([label, value]) => `<div class="stat"><span class="eyebrow">${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`)
        .join("");
    }

    function renderHero(snapshot) {
      document.getElementById("project-name").textContent = snapshot.project_name || "Blackdog";
      document.getElementById("repo-root").textContent = snapshot.project_root ? `Repo: ${snapshot.project_root}` : "";
      const pushObjective = (snapshot.push_objective || []).slice(0, 2).join(" ");
      document.getElementById("push-objective").textContent = pushObjective || "Live backlog monitoring for Blackdog.";
      const links = snapshot.links || {};
      const nextRows = snapshot.next_rows || [];
      document.getElementById("hero-links").innerHTML = [
        links.backlog ? `<a href="${escapeHtml(links.backlog)}" target="_blank" rel="noreferrer">Backlog</a>` : "",
        links.static_html ? `<a href="${escapeHtml(links.static_html)}" target="_blank" rel="noreferrer">Static HTML</a>` : "",
        nextRows.length ? `<a href="#dag">Next: ${escapeHtml(nextRows[0].id)}</a>` : `<a href="#dag">No runnable tasks</a>`,
        snapshot.generated_at ? `<span class="chip">Updated ${escapeHtml(snapshot.generated_at)}</span>` : "",
      ].filter(Boolean).join("");
      renderStats(snapshot);
    }

    function renderObjectives(snapshot) {
      const rows = snapshot.objectives || [];
      document.getElementById("objectives").innerHTML = rows.length
        ? rows.map((row) => `
            <article class="objective">
              <span class="eyebrow">${escapeHtml(row.id)}</span>
              <strong>${escapeHtml(row.title)}</strong>
              <span>${escapeHtml(row.done || 0)}/${escapeHtml(row.total || 0)}</span>
            </article>
          `).join("")
        : `<div class="empty">No objectives tagged yet.</div>`;
    }

    function renderSupervisorStrip(snapshot) {
      const supervisor = snapshot.supervisor || {};
      const rows = [
        ["Active runs", (supervisor.active_runs || []).length],
        ["Recent runs", (supervisor.recent_runs || []).length],
        ["Loops", (supervisor.loops || []).length],
        ["Next runnable", (snapshot.next_rows || []).length],
      ];
      document.getElementById("supervisor-strip").innerHTML = rows
        .map(([label, value]) => `<article class="strip-card"><span class="eyebrow">${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`)
        .join("");
    }

    function renderMessages(snapshot) {
      const rows = snapshot.open_messages || [];
      document.getElementById("messages").innerHTML = rows.length
        ? rows.map((row) => `
            <article class="message-card">
              <span class="eyebrow">${escapeHtml(row.sender)} -> ${escapeHtml(row.recipient)}</span>
              <strong>${escapeHtml(row.kind || "message")}</strong>
              <span>${escapeHtml(row.body || "")}</span>
              <div class="chips">${(row.tags || []).map(statusChip).join("")}</div>
            </article>
          `).join("")
        : `<div class="empty">No open inbox messages.</div>`;
    }

    function renderResults(snapshot) {
      const rows = snapshot.recent_results || [];
      document.getElementById("results").innerHTML = rows.length
        ? rows.map((row) => `
            <article class="result-card">
              <span class="eyebrow">${escapeHtml(row.recorded_at || "")}</span>
              <strong>${escapeHtml(row.task_id || "?")} · ${escapeHtml(row.status || "?")}</strong>
              <span>${escapeHtml(row.actor || "")}</span>
              <div class="inline-links">
                ${row.result_href ? `<a href="${escapeHtml(row.result_href)}" target="_blank" rel="noreferrer">Result JSON</a>` : ""}
              </div>
            </article>
          `).join("")
        : `<div class="empty">No task results yet.</div>`;
    }

    function renderRuns(snapshot) {
      const supervisor = snapshot.supervisor || {};
      const loops = supervisor.loops || [];
      const runs = supervisor.recent_runs || [];
      const loopHtml = loops.length
        ? loops.map((loop) => `
            <article class="loop-card">
              <span class="eyebrow">${escapeHtml(loop.actor || "loop")} · ${escapeHtml(loop.loop_id || "")}</span>
              <strong>${escapeHtml(loop.status || "running")}</strong>
              <span>${escapeHtml(loop.cycle_count || 0)} cycle(s)</span>
              <div class="inline-links">
                ${loop.status_href ? `<a href="${escapeHtml(loop.status_href)}" target="_blank" rel="noreferrer">Status JSON</a>` : ""}
              </div>
            </article>
          `).join("")
        : `<div class="empty">No supervisor loop snapshots yet.</div>`;
      const runHtml = runs.length
        ? runs.map((run) => `
            <article class="run-card">
              <span class="eyebrow">${escapeHtml(run.actor || "supervisor")} · ${escapeHtml(run.run_id || "")}</span>
              <strong>${escapeHtml(run.status || "running")}</strong>
              <span>${escapeHtml(run.workspace_mode || "")}</span>
              <div class="inline-links">
                ${run.run_href ? `<a href="${escapeHtml(run.run_href)}" target="_blank" rel="noreferrer">Run Artifacts</a>` : ""}
              </div>
              <div class="run-children">
                ${(run.children || []).map((child) => `
                  <div class="child-row">
                    <strong>${escapeHtml(child.task_id || "?")} · ${escapeHtml(child.status || "pending")}</strong>
                    <span class="meta">${escapeHtml(child.child_agent || "")}</span>
                    <div class="inline-links">
                      ${child.prompt_href ? `<a href="${escapeHtml(child.prompt_href)}" target="_blank" rel="noreferrer">Prompt</a>` : ""}
                      ${child.stdout_href ? `<a href="${escapeHtml(child.stdout_href)}" target="_blank" rel="noreferrer">Stdout</a>` : ""}
                      ${child.stderr_href ? `<a href="${escapeHtml(child.stderr_href)}" target="_blank" rel="noreferrer">Stderr</a>` : ""}
                    </div>
                  </div>
                `).join("")}
              </div>
            </article>
          `).join("")
        : `<div class="empty">No supervisor runs yet.</div>`;
      document.getElementById("runs").innerHTML = `
        <div class="supervisor-strip">${loopHtml}</div>
        <div style="height: 12px"></div>
        <div class="supervisor-strip">${runHtml}</div>
      `;
    }

    function renderDag(snapshot) {
      const graph = snapshot.graph || {};
      const tasks = graph.tasks || [];
      const edges = graph.edges || [];
      const container = document.getElementById("dag");
      if (!tasks.length) {
        container.innerHTML = `<div class="empty">No tasks in the graph yet.</div>`;
        return;
      }
      const grouped = new Map();
      for (const task of tasks) {
        const waveKey = task.wave == null ? "unplanned" : String(task.wave);
        const laneKey = `${waveKey}::${task.lane_id || "unplanned"}`;
        if (!grouped.has(waveKey)) grouped.set(waveKey, new Map());
        if (!grouped.get(waveKey).has(laneKey)) {
          grouped.get(waveKey).set(laneKey, {
            title: task.lane_title || "Unplanned",
            laneId: task.lane_id || "unplanned",
            tasks: [],
          });
        }
        grouped.get(waveKey).get(laneKey).tasks.push(task);
      }
      const orderedWaves = [...grouped.keys()].sort((a, b) => {
        if (a === "unplanned") return 1;
        if (b === "unplanned") return -1;
        return Number(a) - Number(b);
      });
      container.innerHTML = `
        <svg class="dag-links"></svg>
        <div class="dag-columns">
          ${orderedWaves.map((waveKey) => `
            <div class="wave-column">
              <div class="eyebrow">Wave ${escapeHtml(waveKey)}</div>
              ${[...grouped.get(waveKey).values()].map((lane) => `
                <section class="lane">
                  <div>
                    <span class="eyebrow">${escapeHtml(lane.laneId)}</span>
                    <h3>${escapeHtml(lane.title)}</h3>
                  </div>
                  ${lane.tasks.map((task) => `
                    <article class="task-node task-${escapeHtml(task.status)}" data-node-id="${escapeHtml(task.id)}">
                      <code>${escapeHtml(task.id)}</code>
                      <strong>${escapeHtml(task.title)}</strong>
                      <span class="meta">${escapeHtml(task.status)} · ${escapeHtml(task.detail)}</span>
                      <div class="chips">
                        ${statusChip(task.priority || "")}
                        ${statusChip(task.risk || "")}
                        ${(task.domains || []).slice(0, 3).map(statusChip).join("")}
                      </div>
                    </article>
                  `).join("")}
                </section>
              `).join("")}
            </div>
          `).join("")}
        </div>
      `;
      requestAnimationFrame(() => drawDagEdges(edges));
    }

    function drawDagEdges(edges) {
      const container = document.getElementById("dag");
      const svg = container.querySelector(".dag-links");
      if (!svg) return;
      const rect = container.getBoundingClientRect();
      const paths = [];
      for (const edge of edges) {
        const from = container.querySelector(`[data-node-id="${CSS.escape(edge.from)}"]`);
        const to = container.querySelector(`[data-node-id="${CSS.escape(edge.to)}"]`);
        if (!from || !to) continue;
        const fromRect = from.getBoundingClientRect();
        const toRect = to.getBoundingClientRect();
        const x1 = fromRect.right - rect.left + container.scrollLeft;
        const y1 = fromRect.top - rect.top + container.scrollTop + (fromRect.height / 2);
        const x2 = toRect.left - rect.left + container.scrollLeft;
        const y2 = toRect.top - rect.top + container.scrollTop + (toRect.height / 2);
        const delta = Math.max(48, (x2 - x1) / 2);
        paths.push(`<path d="M ${x1} ${y1} C ${x1 + delta} ${y1}, ${x2 - delta} ${y2}, ${x2} ${y2}"></path>`);
      }
      const width = Math.max(container.scrollWidth, rect.width);
      const height = Math.max(container.scrollHeight, rect.height);
      svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
      svg.setAttribute("width", width);
      svg.setAttribute("height", height);
      svg.innerHTML = paths.join("");
    }

    function renderError(snapshot) {
      document.getElementById("project-name").textContent = snapshot.project_name || "Blackdog";
      document.getElementById("repo-root").textContent = snapshot.project_root ? `Repo: ${snapshot.project_root}` : "";
      document.getElementById("push-objective").textContent = snapshot.error?.message || "UI snapshot failed.";
      document.getElementById("stats").innerHTML = `<div class="stat error"><span class="eyebrow">Snapshot Error</span><strong>${escapeHtml(snapshot.error?.type || "Error")}</strong></div>`;
      document.getElementById("objectives").innerHTML = "";
      document.getElementById("supervisor-strip").innerHTML = "";
      document.getElementById("messages").innerHTML = `<div class="empty error">${escapeHtml(snapshot.error?.message || "")}</div>`;
      document.getElementById("results").innerHTML = "";
      document.getElementById("runs").innerHTML = "";
      document.getElementById("dag").innerHTML = `<div class="empty error">${escapeHtml(snapshot.error?.message || "")}</div>`;
    }

    function renderSnapshot(snapshot) {
      currentSnapshot = snapshot;
      if (snapshot.error) {
        renderError(snapshot);
        return;
      }
      renderHero(snapshot);
      renderObjectives(snapshot);
      renderSupervisorStrip(snapshot);
      renderMessages(snapshot);
      renderResults(snapshot);
      renderRuns(snapshot);
      renderDag(snapshot);
    }

    function setSyncState(label, stateClass) {
      const node = document.getElementById("sync-status");
      node.className = `sync-pill ${stateClass || ""}`.trim();
      node.innerHTML = `<span class="sync-dot"></span><span>${escapeHtml(label)}</span>`;
    }

    async function loadSnapshot() {
      const response = await fetch("/api/snapshot", { cache: "no-store" });
      const snapshot = await response.json();
      renderSnapshot(snapshot);
    }

    function connectStream() {
      if (eventSource) {
        eventSource.close();
      }
      eventSource = new EventSource("/api/stream");
      eventSource.addEventListener("snapshot", (event) => {
        setSyncState("Live updates", "live");
        renderSnapshot(JSON.parse(event.data));
      });
      eventSource.onerror = () => {
        setSyncState("Reconnecting…", "");
      };
    }

    window.addEventListener("resize", () => {
      if (currentSnapshot && currentSnapshot.graph) {
        requestAnimationFrame(() => drawDagEdges(currentSnapshot.graph.edges || []));
      }
    });

    window.addEventListener("load", async () => {
      setSyncState("Connecting…", "");
      await loadSnapshot();
      connectStream();
    });
  </script>
</body>
</html>
"""


def serve_ui(
    profile: Profile,
    *,
    host: str,
    port: int,
    open_browser: bool = False,
    announce: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    try:
        server = _UIServer(profile, host, port)
    except OSError as exc:
        raise UIError(str(exc)) from exc
    actual_host, actual_port = server.server_address[:2]
    startup_payload = {
        "url": f"http://{actual_host}:{actual_port}/",
        "host": actual_host,
        "port": actual_port,
        "snapshot_url": f"http://{actual_host}:{actual_port}/api/snapshot",
        "stream_url": f"http://{actual_host}:{actual_port}/api/stream",
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "control_dir": str(profile.paths.control_dir),
        "state_file": str(ui_server_state_file(profile.paths)),
    }
    state_file = ui_server_state_file(profile.paths)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(
            {
                **startup_payload,
                "started_at": now_iso(),
                "pid": os.getpid(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if announce is not None:
        announce(startup_payload)
    if open_browser:
        webbrowser.open(startup_payload["url"])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        current = read_ui_server_state(profile.paths)
        if current and current.get("port") == actual_port and current.get("pid") == os.getpid():
            state_file.unlink(missing_ok=True)
