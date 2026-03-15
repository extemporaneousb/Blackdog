from __future__ import annotations

from datetime import datetime
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
from .worktree import worktree_contract


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


def _parse_iso(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _duration_seconds(start: datetime | None, end: datetime | None = None) -> int | None:
    if start is None:
        return None
    stop = end or datetime.now().astimezone()
    return max(0, int((stop - start).total_seconds()))


def _format_duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


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


def _empty_task_activity() -> dict[str, Any]:
    return {
        "claimed_by": None,
        "claimed_at": None,
        "completed_at": None,
        "released_at": None,
        "active_compute_seconds": None,
        "active_compute_label": None,
        "total_compute_seconds": 0,
        "total_compute_label": "0s",
        "latest_result_status": None,
        "latest_result_at": None,
        "latest_result_href": None,
    }


def _build_task_activity(
    paths: ProjectPaths,
    state: dict[str, Any],
    events: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    activities: dict[str, dict[str, Any]] = {}
    open_claims: dict[str, dict[str, Any]] = {}
    ordered_events = sorted(events, key=lambda row: str(row.get("at") or ""))
    for event in ordered_events:
        task_id = str(event.get("task_id") or "")
        if not task_id:
            continue
        event_type = str(event.get("type") or "")
        event_at_text = str(event.get("at") or "")
        event_at = _parse_iso(event_at_text)
        activity = activities.setdefault(task_id, _empty_task_activity())
        if event_type == "claim" and event_at is not None:
            open_claims[task_id] = {
                "started_at": event_at,
                "started_at_text": event_at_text,
                "claimed_by": str(event.get("actor") or ""),
            }
            activity["claimed_at"] = event_at_text
            activity["claimed_by"] = str(event.get("actor") or "")
        elif event_type in {"release", "complete"}:
            current = open_claims.pop(task_id, None)
            if current and event_at is not None:
                activity["total_compute_seconds"] = int(activity.get("total_compute_seconds") or 0) + (
                    _duration_seconds(current["started_at"], event_at) or 0
                )
            if event_type == "release":
                activity["released_at"] = event_at_text
            else:
                activity["completed_at"] = event_at_text

    latest_results: dict[str, dict[str, Any]] = {}
    for row in results:
        task_id = str(row.get("task_id") or "")
        if task_id and task_id not in latest_results:
            latest_results[task_id] = row

    for task_id, entry in state.get("task_claims", {}).items():
        if not isinstance(entry, dict):
            continue
        task_id_text = str(task_id)
        activity = activities.setdefault(task_id_text, _empty_task_activity())
        claimed_at_text = str(entry.get("claimed_at") or "") or None
        claimed_at = _parse_iso(claimed_at_text)
        completed_at_text = str(entry.get("completed_at") or "") or None
        completed_at = _parse_iso(completed_at_text)
        released_at_text = str(entry.get("released_at") or "") or None
        status = str(entry.get("status") or "")
        if claimed_at_text and not activity.get("claimed_at"):
            activity["claimed_at"] = claimed_at_text
        if entry.get("claimed_by"):
            activity["claimed_by"] = str(entry.get("claimed_by") or "")
        if completed_at_text:
            activity["completed_at"] = completed_at_text
        if released_at_text:
            activity["released_at"] = released_at_text

        total_seconds = int(activity.get("total_compute_seconds") or 0)
        if status == "claimed" and claimed_at is not None:
            active_seconds = _duration_seconds(claimed_at)
            activity["active_compute_seconds"] = active_seconds
            activity["active_compute_label"] = _format_duration(active_seconds)
            total_seconds += active_seconds or 0
        elif status == "done" and total_seconds == 0 and claimed_at is not None and completed_at is not None:
            total_seconds = _duration_seconds(claimed_at, completed_at) or 0
        activity["total_compute_seconds"] = total_seconds
        activity["total_compute_label"] = _format_duration(total_seconds)

    for task_id, row in latest_results.items():
        activity = activities.setdefault(task_id, _empty_task_activity())
        activity["latest_result_status"] = row.get("status")
        activity["latest_result_at"] = row.get("recorded_at")
        activity["latest_result_href"] = _artifact_href(paths, row.get("result_file"))
    return activities


def _split_open_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    control_messages: list[dict[str, Any]] = []
    dispatch_messages: list[dict[str, Any]] = []
    for row in messages:
        recipient = str(row.get("recipient") or "")
        tags = {str(tag).strip().lower() for tag in row.get("tags") or []}
        if recipient.startswith("supervisor/child-") or "supervisor-run" in tags:
            dispatch_messages.append(row)
        else:
            control_messages.append(row)
    return control_messages, dispatch_messages


def _build_supervisor_runs(paths: ProjectPaths, events: list[dict[str, Any]], *, limit: int = 6) -> dict[str, Any]:
    runs: dict[str, dict[str, Any]] = {}
    stale_after_seconds = 30
    relevant_events = {
        "supervisor_run_started",
        "supervisor_run_finished",
        "worktree_start",
        "child_launch",
        "child_launch_failed",
        "child_finish",
    }
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type not in relevant_events:
            continue
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
        elif event_type in {"worktree_start", "child_launch", "child_launch_failed", "child_finish"}:
            task_id = str(event.get("task_id") or "")
            child_agent = str(payload.get("child_agent") or task_id or "child")
            child = run["children"].setdefault(
                child_agent,
                {
                    "child_agent": child_agent,
                    "task_id": task_id,
                    "status": "pending",
                    "workspace": None,
                    "workspace_mode": run["workspace_mode"],
                    "task_branch": None,
                    "target_branch": None,
                    "primary_worktree": None,
                    "pid": None,
                    "exit_code": None,
                    "timed_out": False,
                    "final_task_status": None,
                    "started_at": None,
                    "finished_at": None,
                },
            )
            if task_id:
                child["task_id"] = task_id
            if event_type == "worktree_start":
                child["workspace"] = payload.get("worktree_path") or child.get("workspace")
                child["task_branch"] = payload.get("branch")
                child["target_branch"] = payload.get("target_branch")
                child["primary_worktree"] = payload.get("primary_worktree")
            elif event_type == "child_launch":
                child["status"] = "running"
                child["workspace"] = payload.get("workspace")
                child["workspace_mode"] = payload.get("workspace_mode") or child.get("workspace_mode") or run["workspace_mode"]
                child["pid"] = payload.get("pid")
                child["started_at"] = str(event.get("at") or "")
            elif event_type == "child_launch_failed":
                child["status"] = "launch-failed"
                child["error"] = payload.get("error")
                child["finished_at"] = str(event.get("at") or "")
            elif event_type == "child_finish":
                child["status"] = "finished"
                child["exit_code"] = payload.get("exit_code")
                child["timed_out"] = bool(payload.get("timed_out"))
                child["final_task_status"] = payload.get("final_task_status")
                child["land_error"] = payload.get("land_error")
                if payload.get("land_error") and not child.get("error"):
                    child["error"] = payload.get("land_error")
                child["finished_at"] = str(event.get("at") or "")

    ordered_runs: list[dict[str, Any]] = []
    for run in runs.values():
        run_dir = _find_run_dir(paths, str(run["run_id"]))
        children: list[dict[str, Any]] = []
        has_running_child = False
        has_interrupted_child = False
        latest_finished_at: str | None = run.get("finished_at")
        for child in sorted(run["children"].values(), key=lambda row: (str(row.get("task_id") or ""), str(row.get("child_agent") or ""))):
            if child.get("status") == "running" and not _pid_alive(child.get("pid")):
                child["status"] = "interrupted"
                child["error"] = "child process is no longer running"
                child["finished_at"] = child.get("finished_at") or run.get("last_event_at")
            if child.get("status") == "running":
                has_running_child = True
            if child.get("status") == "interrupted":
                has_interrupted_child = True
            child_elapsed = _duration_seconds(_parse_iso(child.get("started_at")), _parse_iso(child.get("finished_at")))
            child["elapsed_seconds"] = child_elapsed
            child["elapsed_label"] = _format_duration(child_elapsed)
            latest_finished_at = str(child.get("finished_at") or latest_finished_at or "")
            artifacts = _child_artifacts(paths, run_dir, str(child.get("task_id") or ""))
            children.append({**child, **artifacts})
        target_branches = sorted(
            {
                str(child.get("target_branch") or "").strip()
                for child in children
                if str(child.get("target_branch") or "").strip()
            }
        )
        if run["status"] != "finished":
            if has_running_child:
                run["status"] = "running"
            elif children and all(child.get("status") in {"finished", "launch-failed"} for child in children):
                run["status"] = "finished"
                run["finished_at"] = run.get("finished_at") or latest_finished_at
            elif children and has_interrupted_child:
                run["status"] = "interrupted"
                run["finished_at"] = run.get("finished_at") or latest_finished_at
            elif children:
                run["status"] = "interrupted"
                run["finished_at"] = run.get("finished_at") or latest_finished_at
            else:
                run_age_seconds = _duration_seconds(_parse_iso(run.get("started_at")), _parse_iso(run.get("finished_at")))
                if run_age_seconds is not None and run_age_seconds > stale_after_seconds:
                    run["status"] = "interrupted"
                    run["finished_at"] = run.get("finished_at") or run.get("last_event_at")
        run_elapsed = _duration_seconds(_parse_iso(run.get("started_at")), _parse_iso(run.get("finished_at")))
        ordered_runs.append(
            {
                "run_id": run["run_id"],
                "actor": run["actor"],
                "status": run["status"],
                "started_at": run["started_at"],
                "finished_at": run["finished_at"],
                "elapsed_seconds": run_elapsed,
                "elapsed_label": _format_duration(run_elapsed),
                "workspace_mode": run["workspace_mode"],
                "target_branches": target_branches,
                "task_ids": run["task_ids"],
                "run_dir": str(run_dir) if run_dir is not None else None,
                "run_href": _artifact_href(paths, run_dir) if run_dir is not None else None,
                "children": children,
                "last_event_at": run["last_event_at"],
            }
        )
    ordered_runs.sort(key=lambda row: str(row.get("last_event_at") or ""), reverse=True)
    return {
        "active_runs": [row for row in ordered_runs if row.get("status") == "running"],
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
        poll_interval_seconds = float(payload.get("poll_interval_seconds") or 0)
        stale_after_seconds = max(15.0, (poll_interval_seconds * 3.0) + 5.0)
        age_seconds = max(0, int(datetime.now().astimezone().timestamp() - status_file.stat().st_mtime))
        status = payload.get("final_status") or last_cycle_status or "running"
        if not payload.get("completed_at") and age_seconds > stale_after_seconds:
            status = "interrupted"
        loops.append(
            {
                "loop_id": payload.get("loop_id"),
                "actor": payload.get("actor"),
                "status": status,
                "workspace_mode": payload.get("workspace_mode"),
                "cycle_count": cycle_count,
                "last_cycle_status": last_cycle_status,
                "completed_at": payload.get("completed_at"),
                "age_seconds": age_seconds,
                "age_label": _format_duration(age_seconds),
                "status_file": str(status_file),
                "status_href": _artifact_href(paths, status_file),
            }
        )
    return loops


def _build_active_tasks(graph_tasks: list[dict[str, Any]], active_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tasks_by_id = {str(task.get("id") or ""): task for task in graph_tasks}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for run in active_runs:
        for child in run.get("children") or []:
            task_id = str(child.get("task_id") or "")
            if not task_id or task_id in seen:
                continue
            task = tasks_by_id.get(task_id)
            if task is None:
                continue
            rows.append(
                {
                    "task_id": task_id,
                    "title": task.get("title"),
                    "status": child.get("status") or task.get("status"),
                    "lane_title": task.get("lane_title"),
                    "epic_title": task.get("epic_title"),
                    "detail": task.get("detail"),
                    "claimed_by": task.get("claimed_by"),
                    "claimed_at": task.get("claimed_at"),
                    "active_compute_seconds": task.get("active_compute_seconds"),
                    "active_compute_label": task.get("active_compute_label"),
                    "total_compute_seconds": task.get("total_compute_seconds"),
                    "total_compute_label": task.get("total_compute_label"),
                    "latest_result_status": task.get("latest_result_status"),
                    "latest_result_at": task.get("latest_result_at"),
                    "latest_result_href": task.get("latest_result_href"),
                    "child_agent": child.get("child_agent"),
                    "workspace_mode": child.get("workspace_mode"),
                    "task_branch": child.get("task_branch"),
                    "target_branch": child.get("target_branch"),
                    "primary_worktree": child.get("primary_worktree"),
                    "run_id": run.get("run_id"),
                    "run_href": run.get("run_href"),
                    "prompt_href": child.get("prompt_href"),
                    "stdout_href": child.get("stdout_href"),
                    "stderr_href": child.get("stderr_href"),
                    "elapsed_seconds": child.get("elapsed_seconds"),
                    "elapsed_label": child.get("elapsed_label"),
                }
            )
            seen.add(task_id)
    for task in graph_tasks:
        if task.get("status") != "claimed" or task.get("id") in seen:
            continue
        rows.append(
            {
                "task_id": task.get("id"),
                "title": task.get("title"),
                "status": task.get("status"),
                "lane_title": task.get("lane_title"),
                "epic_title": task.get("epic_title"),
                "detail": task.get("detail"),
                "claimed_by": task.get("claimed_by"),
                "claimed_at": task.get("claimed_at"),
                "active_compute_seconds": task.get("active_compute_seconds"),
                "active_compute_label": task.get("active_compute_label"),
                "total_compute_seconds": task.get("total_compute_seconds"),
                "total_compute_label": task.get("total_compute_label"),
                "latest_result_status": task.get("latest_result_status"),
                "latest_result_at": task.get("latest_result_at"),
                "latest_result_href": task.get("latest_result_href"),
                "child_agent": None,
                "run_id": None,
                "run_href": None,
                "prompt_href": None,
                "stdout_href": None,
                "stderr_href": None,
                "elapsed_seconds": task.get("active_compute_seconds"),
                "elapsed_label": task.get("active_compute_label"),
            }
        )
    rows.sort(key=lambda row: str(row.get("task_id") or ""))
    return rows


def build_ui_snapshot(profile: Profile) -> dict[str, Any]:
    snapshot = load_backlog(profile.paths, profile)
    state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
    events = load_events(profile.paths)
    messages = load_inbox(profile.paths)
    results = load_task_results(profile.paths)
    task_activity = _build_task_activity(profile.paths, state, events, results)
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
        activity = task_activity.get(task.id, _empty_task_activity())
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
                "claimed_by": activity.get("claimed_by"),
                "claimed_at": activity.get("claimed_at"),
                "completed_at": activity.get("completed_at"),
                "released_at": activity.get("released_at"),
                "active_compute_seconds": activity.get("active_compute_seconds"),
                "active_compute_label": activity.get("active_compute_label"),
                "total_compute_seconds": activity.get("total_compute_seconds"),
                "total_compute_label": activity.get("total_compute_label"),
                "latest_result_status": activity.get("latest_result_status"),
                "latest_result_at": activity.get("latest_result_at"),
                "latest_result_href": activity.get("latest_result_href"),
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
    control_messages, dispatch_messages = _split_open_messages(summary["open_messages"])
    supervisor = {
        **_build_supervisor_runs(profile.paths, events),
        "loops": _load_supervisor_loops(profile.paths),
    }
    workspace = worktree_contract(profile)

    return {
        "schema_version": UI_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "control_dir": str(profile.paths.control_dir),
        "profile_file": str(profile.paths.profile_file),
        "workspace_contract": workspace,
        "counts": summary["counts"],
        "total": summary["total"],
        "push_objective": summary["push_objective"],
        "objectives": summary["objectives"],
        "next_rows": summary["next_rows"],
        "open_messages": summary["open_messages"][:10],
        "control_messages": control_messages[:10],
        "dispatch_messages": dispatch_messages[:10],
        "recent_results": recent_results,
        "recent_events": summary["recent_events"],
        "plan": plan,
        "graph": {
            "tasks": graph_tasks,
            "edges": graph_edges,
        },
        "active_tasks": _build_active_tasks(graph_tasks, supervisor["active_runs"]),
        "supervisor": supervisor,
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
      --bg: #f5f0e8;
      --panel: rgba(255, 252, 248, 0.96);
      --panel-strong: rgba(255, 255, 255, 0.92);
      --panel-muted: rgba(248, 243, 236, 0.92);
      --ink: #1f1712;
      --muted: #6c6158;
      --line: rgba(56, 42, 28, 0.14);
      --ready: #a35a12;
      --claimed: #165fae;
      --done: #0f6a46;
      --waiting: #6f7782;
      --approval: #744ab0;
      --risk: #9d241f;
      --interrupted: #7e4d28;
      --running: #b86716;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, rgba(208, 168, 97, 0.2), transparent 28%),
        radial-gradient(circle at bottom left, rgba(30, 110, 93, 0.08), transparent 24%),
        linear-gradient(180deg, #fbf7f1 0%, var(--bg) 100%);
    }
    a { color: inherit; }
    .page { width: min(1680px, 100%); margin: 0 auto; padding: 24px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 20px;
      margin-bottom: 18px;
      min-width: 0;
      box-shadow: 0 16px 40px rgba(73, 47, 22, 0.05);
    }
    .hero { display: grid; grid-template-columns: 1.3fr 1fr; gap: 18px; }
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
    .hero-copy {
      margin: 6px 0 0;
      font-size: 1.02rem;
      line-height: 1.55;
      color: var(--ink);
      max-width: 64ch;
    }
    .stats { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
    .stat, .strip-card, .run-card, .loop-card, .message-card, .result-card, .task-node, .objective, .lane, .active-task {
      border: 1px solid var(--line);
      border-radius: 18px;
      background: rgba(255, 255, 255, 0.78);
      min-width: 0;
      overflow-wrap: anywhere;
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
    .section-head {
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 16px;
      margin-bottom: 14px;
    }
    .section-head h2 {
      margin: 0;
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
    .hero-links span {
      display: inline-flex;
      align-items: center;
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
    }
    .objectives, .task-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }
    .supervisor-strip {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 12px;
    }
    .message-grid, .result-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
    }
    .objective, .strip-card, .run-card, .loop-card, .message-card, .result-card, .active-task {
      padding: 14px;
      display: grid;
      gap: 8px;
    }
    .task-grid { grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
    .layout { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(320px, 0.9fr); gap: 18px; align-items: start; }
    .side-stack { display: grid; gap: 18px; min-width: 0; }
    .filter-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
    }
    .filter-chip, .filter-toggle {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
      color: var(--ink);
      font: inherit;
      cursor: pointer;
      text-decoration: none;
    }
    .filter-chip.active {
      border-color: rgba(22, 95, 174, 0.36);
      background: rgba(232, 241, 255, 0.92);
      color: var(--claimed);
    }
    .filter-toggle input {
      margin: 0;
    }
    .search-input {
      min-width: 220px;
      flex: 1 1 240px;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
      color: var(--ink);
      font: inherit;
    }
    .dag-shell {
      position: relative;
      overflow: auto;
      padding-bottom: 8px;
      max-width: 100%;
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
      min-width: max-content;
      align-items: start;
      width: max-content;
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
      background: rgba(250, 246, 240, 0.88);
    }
    .task-node {
      padding: 12px;
      display: grid;
      gap: 8px;
      background: rgba(255, 255, 255, 0.9);
    }
    .task-top {
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 8px;
    }
    .task-title {
      font-size: 1rem;
      line-height: 1.35;
    }
    .subtle {
      color: var(--muted);
      font-size: 0.9rem;
      line-height: 1.45;
    }
    .task-ready { border-color: rgba(156, 90, 19, 0.35); }
    .task-claimed { border-color: rgba(21, 95, 193, 0.35); }
    .task-done { border-color: rgba(18, 114, 74, 0.35); }
    .task-waiting, .task-high-risk { border-color: rgba(123, 128, 136, 0.35); }
    .task-approval { border-color: rgba(123, 75, 183, 0.35); }
    .task-interrupted { border-color: rgba(126, 77, 40, 0.35); }
    .meta { color: var(--muted); font-size: 0.92rem; }
    .chips { display: flex; flex-wrap: wrap; gap: 8px; }
    .chip {
      display: inline-flex;
      align-items: center;
      padding: 4px 8px;
      border: 0;
      border-radius: 999px;
      font-size: 0.8rem;
      color: #fff;
      background: var(--muted);
    }
    .chip-running, .chip-claimed { background: var(--claimed); }
    .chip-ready { background: var(--ready); }
    .chip-done, .chip-success { background: var(--done); }
    .chip-waiting, .chip-approval, .chip-high-risk { background: var(--waiting); }
    .chip-blocked, .chip-failed, .chip-released { background: var(--risk); }
    .chip-interrupted { background: var(--interrupted); }
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
    .hint {
      color: var(--muted);
      font-size: 0.9rem;
    }
    .run-children { display: grid; gap: 10px; }
    .child-row {
      padding-top: 10px;
      border-top: 1px solid var(--line);
      display: grid;
      gap: 8px;
    }
    .run-header {
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 10px;
    }
    .section-actions {
      display: inline-flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .ghost-button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 8px 12px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
      color: var(--ink);
      cursor: pointer;
      font: inherit;
    }
    .ghost-button.active {
      background: var(--ink);
      border-color: var(--ink);
      color: #fff;
    }
    .message-summary, .result-summary, .run-summary {
      color: var(--muted);
      font-size: 0.92rem;
      line-height: 1.45;
    }
    .stack-gap {
      height: 12px;
    }
    .reader-dialog {
      width: min(760px, calc(100vw - 32px));
      border: 0;
      border-radius: 20px;
      padding: 0;
      background: transparent;
    }
    .reader-dialog::backdrop {
      background: rgba(31, 23, 18, 0.48);
      backdrop-filter: blur(4px);
    }
    .reader-shell {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 20px;
      display: grid;
      gap: 14px;
      box-shadow: 0 24px 64px rgba(31, 23, 18, 0.2);
    }
    .reader-body {
      margin: 0;
      padding: 14px;
      border-radius: 16px;
      border: 1px solid var(--line);
      background: var(--panel-muted);
      color: var(--ink);
      font: 0.95rem/1.55 "SFMono-Regular", "Menlo", monospace;
      overflow: auto;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      max-height: 60vh;
    }
    .reader-actions {
      display: flex;
      justify-content: flex-end;
    }
    .history-note {
      color: var(--muted);
      font-size: 0.9rem;
    }
    @media (max-width: 1100px) {
      .hero, .layout { grid-template-columns: 1fr; }
      .stats { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    }
    @media (max-width: 700px) {
      .page { padding: 16px; }
      .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .section-head { align-items: start; flex-direction: column; }
      .filter-bar { width: 100%; }
      .search-input { width: 100%; }
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
        <p id="push-objective" class="hero-copy">Loading snapshot…</p>
        <div id="stats" class="stats"></div>
        <div id="hero-links" class="hero-links"></div>
      </div>
      <div>
        <h2>Supervisor Monitor</h2>
        <div id="supervisor-strip" class="supervisor-strip"></div>
        <p id="workspace-contract-note" class="history-note"></p>
      </div>
    </section>
    <section class="panel">
      <div class="section-head">
        <div>
          <span class="eyebrow">Now</span>
          <h2>Current Activity</h2>
        </div>
        <div id="activity-summary" class="hero-links"></div>
      </div>
      <div id="active-tasks" class="task-grid"></div>
    </section>
    <section class="panel">
      <h2>Objectives</h2>
      <div id="objectives" class="objectives"></div>
    </section>
    <div class="layout">
      <section class="panel">
        <div class="section-head">
          <div>
            <span class="eyebrow">Backlog Browser</span>
            <h2>Task Graph</h2>
          </div>
          <div id="filters" class="filter-bar"></div>
        </div>
        <div id="dag" class="dag-shell"></div>
      </section>
      <div class="side-stack">
        <section class="panel">
          <div class="section-head">
            <div>
              <span class="eyebrow">Operator Controls</span>
              <h2>Control Inbox</h2>
            </div>
            <div id="message-actions" class="section-actions"></div>
          </div>
          <div id="messages" class="message-grid"></div>
        </section>
        <section class="panel">
          <div class="section-head">
            <div>
              <span class="eyebrow">Outcomes</span>
              <h2>Latest Results</h2>
            </div>
            <div id="result-actions" class="section-actions"></div>
          </div>
          <div id="results" class="result-grid"></div>
        </section>
      </div>
    </div>
    <section class="panel">
      <div class="section-head">
        <div>
          <h2>Supervisor Runs</h2>
        </div>
        <div id="run-actions" class="section-actions"></div>
      </div>
      <div id="runs"></div>
    </section>
  </div>
  <dialog id="reader-dialog" class="reader-dialog">
    <div class="reader-shell">
      <div class="section-head">
        <div>
          <span class="eyebrow">Detail</span>
          <h2 id="reader-title">Reader</h2>
        </div>
      </div>
      <pre id="reader-body" class="reader-body"></pre>
      <div class="reader-actions">
        <button id="reader-close" type="button" class="ghost-button">Close</button>
      </div>
    </div>
  </dialog>
  <script>
    let currentSnapshot = null;
    let eventSource = null;
    const currentFilters = {
      query: "",
      showDone: false,
      showWaiting: true,
      scope: "active-wave",
    };
    const currentPanels = {
      showAllMessages: false,
      showAllResults: false,
      showRunHistory: false,
      showInterrupted: false,
    };

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function uiStatus(label) {
      const value = String(label || "").toLowerCase();
      return value === "stale" ? "interrupted" : value;
    }

    function statusLabel(label) {
      const value = uiStatus(label);
      if (!value) return "";
      if (value === "high-risk") return "high risk";
      if (value === "launch-failed") return "launch failed";
      return value;
    }

    function statusTone(label) {
      const value = uiStatus(label);
      if (["running", "claimed"].includes(value)) return "chip-running";
      if (["ready"].includes(value)) return "chip-ready";
      if (["done", "success", "finished"].includes(value)) return "chip-success";
      if (["blocked", "failed", "launch-failed", "released"].includes(value)) return "chip-blocked";
      if (["interrupted"].includes(value)) return "chip-interrupted";
      if (["waiting", "approval", "high-risk", "paused"].includes(value)) return "chip-waiting";
      return "";
    }

    function statusChip(label) {
      const display = statusLabel(label);
      if (!display) return "";
      return `<span class="chip ${statusTone(label)}">${escapeHtml(display)}</span>`;
    }

    function link(label, href) {
      if (!href) return "";
      return `<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`;
    }

    function asArray(value) {
      return Array.isArray(value) ? value : [];
    }

    function excerpt(value, limit = 120) {
      const text = String(value || "").trim().replace(/\\s+/g, " ");
      if (text.length <= limit) return text;
      return `${text.slice(0, limit - 1)}…`;
    }

    function readerButton(label, title, body) {
      if (!body) return "";
      return `<button type="button" class="ghost-button js-reader" data-reader-title="${escapeHtml(title)}" data-reader-body="${escapeHtml(body)}">${escapeHtml(label)}</button>`;
    }

    function bindReaderButtons(root) {
      root.querySelectorAll(".js-reader").forEach((button) => {
        button.addEventListener("click", () => {
          openReader(button.getAttribute("data-reader-title") || "Detail", button.getAttribute("data-reader-body") || "");
        });
      });
    }

    function openReader(title, body) {
      const dialog = document.getElementById("reader-dialog");
      document.getElementById("reader-title").textContent = title;
      document.getElementById("reader-body").textContent = body;
      if (!dialog.open) {
        dialog.showModal();
      }
    }

    function setPanelActions(containerId, buttons) {
      document.getElementById(containerId).innerHTML = buttons.filter(Boolean).join("");
    }

    function activeWave(tasks) {
      const waves = tasks
        .filter((task) => task.status !== "done" && typeof task.wave === "number")
        .map((task) => task.wave)
        .sort((a, b) => a - b);
      return waves.length ? waves[0] : null;
    }

    function dependencySummary(task) {
      const predecessors = asArray(task.predecessor_ids);
      if (!predecessors.length) return "";
      return `Depends on ${predecessors.join(", ")}`;
    }

    function filterGraphTasks(tasks) {
      const query = currentFilters.query.trim().toLowerCase();
      const wave = activeWave(tasks);
      return tasks.filter((task) => {
        if (!currentFilters.showDone && task.status === "done") return false;
        if (!currentFilters.showWaiting && ["waiting", "approval", "high-risk"].includes(task.status)) return false;
        if (currentFilters.scope === "active-wave" && wave != null && task.status !== "claimed" && task.wave !== wave) return false;
        if (!query) return true;
        const haystack = [
          task.id,
          task.title,
          task.lane_title,
          task.epic_title,
          task.status,
          task.latest_result_status,
          asArray(task.predecessor_ids).join(" "),
        ].join(" ").toLowerCase();
        return haystack.includes(query);
      });
    }

    function renderFilterBar(snapshot) {
      const tasks = asArray(snapshot.graph?.tasks);
      const visibleCount = filterGraphTasks(tasks).length;
      document.getElementById("filters").innerHTML = `
        <input id="filter-query" class="search-input" type="search" placeholder="Filter by task, lane, epic, or status" value="${escapeHtml(currentFilters.query)}">
        <button type="button" class="filter-chip ${currentFilters.scope === "active-wave" ? "active" : ""}" data-scope="active-wave">Active wave</button>
        <button type="button" class="filter-chip ${currentFilters.scope === "all" ? "active" : ""}" data-scope="all">All waves</button>
        <label class="filter-toggle"><input id="toggle-done" type="checkbox" ${currentFilters.showDone ? "checked" : ""}> Show done</label>
        <label class="filter-toggle"><input id="toggle-waiting" type="checkbox" ${currentFilters.showWaiting ? "checked" : ""}> Show waiting</label>
        <span class="hint">${escapeHtml(visibleCount)} visible</span>
      `;
      document.getElementById("filter-query").addEventListener("input", (event) => {
        currentFilters.query = event.target.value;
        refreshTaskBrowser();
      });
      document.querySelectorAll("[data-scope]").forEach((node) => {
        node.addEventListener("click", () => {
          currentFilters.scope = node.getAttribute("data-scope");
          refreshTaskBrowser();
        });
      });
      document.getElementById("toggle-done").addEventListener("change", (event) => {
        currentFilters.showDone = event.target.checked;
        refreshTaskBrowser();
      });
      document.getElementById("toggle-waiting").addEventListener("change", (event) => {
        currentFilters.showWaiting = event.target.checked;
        refreshTaskBrowser();
      });
    }

    function renderStats(snapshot) {
      const counts = snapshot.counts || {};
      const activeTasks = asArray(snapshot.active_tasks);
      const blockedRecent = asArray(snapshot.recent_results).filter((row) => row.status === "blocked").length;
      const rows = [
        ["Total", snapshot.total || 0],
        ["Active", activeTasks.length],
        ["Ready", counts.ready || 0],
        ["Waiting", (counts.waiting || 0) + (counts.approval || 0) + (counts["high-risk"] || 0)],
        ["Done", counts.done || 0],
        ["Blocked", blockedRecent],
        ["Control inbox", asArray(snapshot.control_messages).length],
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
      const activeTasks = asArray(snapshot.active_tasks);
      const dispatchCount = asArray(snapshot.dispatch_messages).length;
      const leadLink = nextRows.length
        ? `Next ready: ${nextRows[0].id}`
        : activeTasks.length
          ? `${activeTasks.length} task(s) running`
          : "No unclaimed runnable tasks";
      document.getElementById("hero-links").innerHTML = [
        link("Backlog", links.backlog),
        link("Static HTML", links.static_html),
        `<a href="#active-tasks">${escapeHtml(leadLink)}</a>`,
        dispatchCount ? `<span>${escapeHtml(dispatchCount)} dispatch message(s) hidden from inbox</span>` : "",
        snapshot.generated_at ? `<span>Updated ${escapeHtml(snapshot.generated_at)}</span>` : "",
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

    function renderActiveTasks(snapshot) {
      const rows = asArray(snapshot.active_tasks);
      const activeRuns = asArray(snapshot.supervisor?.active_runs);
      document.getElementById("activity-summary").innerHTML = [
        `<span>${escapeHtml(activeRuns.length)} active run(s)</span>`,
        asArray(snapshot.control_messages).length ? `<span>${escapeHtml(asArray(snapshot.control_messages).length)} control message(s)</span>` : "",
      ].filter(Boolean).join("");
      document.getElementById("active-tasks").innerHTML = rows.length
        ? rows.map((row) => `
            <article class="active-task task-${escapeHtml(row.status || "claimed")}">
              <div class="task-top">
                <div>
                  <span class="eyebrow">${escapeHtml(row.task_id || "?")} · ${escapeHtml(row.lane_title || "lane")}</span>
                  <strong class="task-title">${escapeHtml(row.title || "")}</strong>
                </div>
                <div class="chips">${statusChip(row.status || "claimed")}${row.latest_result_status ? statusChip(`latest ${row.latest_result_status}`) : ""}</div>
              </div>
              <span class="subtle">${escapeHtml(row.epic_title || "")}</span>
              <span class="meta">${escapeHtml(row.claimed_by || row.child_agent || "")}${row.workspace_mode ? ` · ${escapeHtml(row.workspace_mode)}` : ""}${row.target_branch ? ` · ${escapeHtml(row.task_branch || "task")} -> ${escapeHtml(row.target_branch)}` : ""}${row.elapsed_label ? ` · running ${row.elapsed_label}` : ""}${row.total_compute_label ? ` · total ${escapeHtml(row.total_compute_label)}` : ""}</span>
              <span class="subtle">${escapeHtml(row.detail || "")}</span>
              <div class="inline-links">
                ${link("Run", row.run_href)}
                ${link("Prompt", row.prompt_href)}
                ${link("Stdout", row.stdout_href)}
                ${link("Stderr", row.stderr_href)}
                ${link("Latest result", row.latest_result_href)}
              </div>
            </article>
          `).join("")
        : `<div class="empty">No active tasks right now.</div>`;
    }

    function renderSupervisorStrip(snapshot) {
      const supervisor = snapshot.supervisor || {};
      const contract = snapshot.workspace_contract || {};
      const interruptedRuns = asArray(supervisor.recent_runs).filter((row) => uiStatus(row.status) === "interrupted").length;
      const rows = [
        ["Active runs", (supervisor.active_runs || []).length],
        ["Interrupted", interruptedRuns],
        ["Loops", (supervisor.loops || []).length],
        ["Dispatches", (snapshot.dispatch_messages || []).length],
        ["Mode", contract.workspace_mode || "unknown"],
        ["Target", contract.target_branch || "?"],
        ["Primary", contract.primary_dirty ? "dirty" : "clean"],
        ["Local .VE", contract.workspace_has_local_blackdog ? "ready" : "missing"],
      ];
      document.getElementById("supervisor-strip").innerHTML = rows
        .map(([label, value]) => `<article class="strip-card"><span class="eyebrow">${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`)
        .join("");
      document.getElementById("workspace-contract-note").textContent = contract.ve_expectation
        ? `${contract.ve_expectation} CLI for this checkout: ${contract.workspace_has_local_blackdog ? contract.workspace_blackdog_path : "blackdog"}.`
        : "";
    }

    function renderMessages(snapshot) {
      const rows = snapshot.control_messages || [];
      const visibleRows = currentPanels.showAllMessages ? rows : rows.slice(0, 3);
      setPanelActions("message-actions", rows.length > 3 ? [
        `<button type="button" class="ghost-button ${currentPanels.showAllMessages ? "active" : ""}" data-panel-action="toggle-messages">${currentPanels.showAllMessages ? "Latest only" : "Show all"}</button>`,
      ] : []);
      document.querySelectorAll('[data-panel-action="toggle-messages"]').forEach((button) => {
        button.addEventListener("click", () => {
          currentPanels.showAllMessages = !currentPanels.showAllMessages;
          renderMessages(currentSnapshot || snapshot);
        });
      });
      const container = document.getElementById("messages");
      container.innerHTML = rows.length
        ? visibleRows.map((row) => `
            <article class="message-card">
              <span class="eyebrow">${escapeHtml(row.sender)} -> ${escapeHtml(row.recipient)}</span>
              <strong>${escapeHtml(row.kind || "message")}</strong>
              <span class="message-summary">${escapeHtml(excerpt(row.body || "", 140))}</span>
              <div class="chips">${(row.tags || []).map(statusChip).join("")}</div>
              <div class="inline-links">${readerButton("View message", `${row.kind || "Message"} · ${row.sender} -> ${row.recipient}`, row.body || "")}</div>
            </article>
          `).join("")
        : `<div class="empty">No open control messages. Dispatch instructions to child agents are hidden here.</div>`;
      bindReaderButtons(container);
    }

    function renderResults(snapshot) {
      const rows = snapshot.recent_results || [];
      const visibleRows = currentPanels.showAllResults ? rows : rows.slice(0, 3);
      setPanelActions("result-actions", rows.length > 3 ? [
        `<button type="button" class="ghost-button ${currentPanels.showAllResults ? "active" : ""}" data-panel-action="toggle-results">${currentPanels.showAllResults ? "Latest only" : "Show all"}</button>`,
      ] : []);
      document.querySelectorAll('[data-panel-action="toggle-results"]').forEach((button) => {
        button.addEventListener("click", () => {
          currentPanels.showAllResults = !currentPanels.showAllResults;
          renderResults(currentSnapshot || snapshot);
        });
      });
      const container = document.getElementById("results");
      container.innerHTML = rows.length
        ? visibleRows.map((row) => `
            <article class="result-card">
              <span class="eyebrow">${escapeHtml(row.recorded_at || "")}</span>
              <strong>${escapeHtml(row.task_id || "?")}</strong>
              <div class="chips">${statusChip(row.status || "?")}</div>
              <span class="result-summary">${escapeHtml(row.actor || "")}${row.elapsed_label ? ` · ${escapeHtml(row.elapsed_label)}` : ""}${row.total_compute_label ? ` · total ${escapeHtml(row.total_compute_label)}` : ""}</span>
              <div class="inline-links">
                ${link("Result JSON", row.result_href)}
              </div>
            </article>
          `).join("")
        : `<div class="empty">No task results yet.</div>`;
      bindReaderButtons(container);
    }

    function renderRuns(snapshot) {
      const supervisor = snapshot.supervisor || {};
      const loops = supervisor.loops || [];
      const runs = supervisor.recent_runs || [];
      const runningLoops = loops.filter((loop) => !loop.completed_at);
      let visibleLoops = currentPanels.showRunHistory ? [...loops] : runningLoops;
      let visibleRuns = currentPanels.showRunHistory
        ? [...runs]
        : runs.filter((run) => uiStatus(run.status) === "running");
      if (!currentPanels.showInterrupted) {
        visibleLoops = visibleLoops.filter((loop) => uiStatus(loop.status) !== "interrupted");
      }
      if (!currentPanels.showInterrupted) {
        visibleRuns = visibleRuns.filter((run) => uiStatus(run.status) !== "interrupted");
      }
      const hiddenLoopCount = Math.max(0, loops.length - visibleLoops.length);
      const hiddenRunCount = Math.max(0, runs.length - visibleRuns.length);
      setPanelActions("run-actions", [
        (hiddenLoopCount || hiddenRunCount || currentPanels.showRunHistory)
          ? `<button type="button" class="ghost-button ${currentPanels.showRunHistory ? "active" : ""}" data-panel-action="toggle-run-history">${currentPanels.showRunHistory ? "Hide history" : "Show history"}</button>`
          : "",
        [...asArray(loops), ...asArray(runs)].some((row) => uiStatus(row.status) === "interrupted")
          ? `<button type="button" class="ghost-button ${currentPanels.showInterrupted ? "active" : ""}" data-panel-action="toggle-interrupted">${currentPanels.showInterrupted ? "Hide interrupted" : "Show interrupted"}</button>`
          : "",
      ]);
      document.querySelectorAll('[data-panel-action="toggle-run-history"]').forEach((button) => {
        button.addEventListener("click", () => {
          currentPanels.showRunHistory = !currentPanels.showRunHistory;
          renderRuns(currentSnapshot || snapshot);
        });
      });
      document.querySelectorAll('[data-panel-action="toggle-interrupted"]').forEach((button) => {
        button.addEventListener("click", () => {
          currentPanels.showInterrupted = !currentPanels.showInterrupted;
          renderRuns(currentSnapshot || snapshot);
        });
      });
      const loopHtml = visibleLoops.length
        ? visibleLoops.map((loop) => `
            <article class="loop-card">
              <div class="run-header">
                <span class="eyebrow">${escapeHtml(loop.actor || "loop")} · ${escapeHtml(loop.loop_id || "")}</span>
                <div class="chips">${statusChip(loop.status || "running")}</div>
              </div>
              <span class="run-summary">${escapeHtml(loop.cycle_count || 0)} cycle(s)${loop.age_label ? ` · age ${escapeHtml(loop.age_label)}` : ""}</span>
              <div class="inline-links">
                ${link("Status JSON", loop.status_href)}
              </div>
            </article>
          `).join("")
        : `<div class="empty">No active supervisor loops.</div>`;
      const runHtml = visibleRuns.length
        ? visibleRuns.map((run) => `
            <article class="run-card">
              <div class="run-header">
                <span class="eyebrow">${escapeHtml(run.actor || "supervisor")} · ${escapeHtml(run.run_id || "")}</span>
                <div class="chips">${statusChip(run.status || "running")}</div>
              </div>
              <strong>${escapeHtml(run.workspace_mode || "supervisor run")}${run.target_branches?.length ? ` -> ${escapeHtml(run.target_branches.join(", "))}` : ""}${run.elapsed_label ? ` · ${escapeHtml(run.elapsed_label)}` : ""}</strong>
              <span class="run-summary">${escapeHtml((run.children || []).length)} task(s)${run.completed_at ? " · finished" : ""}</span>
              <div class="inline-links">
                ${link("Run Artifacts", run.run_href)}
              </div>
              <div class="run-children">
                ${(run.children || []).map((child) => `
                  <div class="child-row">
                    <strong>${escapeHtml(child.task_id || "?")}</strong>
                    <div class="chips">${statusChip(child.status || "pending")}${child.final_task_status ? statusChip(child.final_task_status) : ""}${child.elapsed_label ? statusChip(child.elapsed_label) : ""}</div>
                    <span class="meta">${escapeHtml(child.child_agent || "")}${child.workspace_mode ? ` · ${escapeHtml(child.workspace_mode)}` : ""}${child.target_branch ? ` · ${escapeHtml(child.task_branch || "task")} -> ${escapeHtml(child.target_branch)}` : ""}</span>
                    ${child.error ? `<span class="run-summary">${escapeHtml(excerpt(child.error, 112))}</span>` : ""}
                    <div class="inline-links">
                      ${link("Prompt", child.prompt_href)}
                      ${link("Stdout", child.stdout_href)}
                      ${link("Stderr", child.stderr_href)}
                      ${readerButton("View detail", `${child.task_id || "Task"} · ${child.child_agent || "child"}`, child.error || "")}
                    </div>
                  </div>
                `).join("")}
              </div>
            </article>
          `).join("")
        : `<div class="empty">No current supervisor runs.${hiddenRunCount ? ` ${escapeHtml(hiddenRunCount)} historical run(s) hidden by default.` : ""}</div>`;
      const container = document.getElementById("runs");
      container.innerHTML = `
        <div class="supervisor-strip">${loopHtml}</div>
        <div class="stack-gap"></div>
        ${(hiddenLoopCount || hiddenRunCount) && !currentPanels.showRunHistory
          ? `<div class="history-note">${escapeHtml(hiddenLoopCount + hiddenRunCount)} historical supervisor artifact(s) hidden by default.</div><div class="stack-gap"></div>`
          : ""}
        <div class="supervisor-strip">${runHtml}</div>
      `;
      bindReaderButtons(container);
    }

    function renderDag(snapshot) {
      const graph = snapshot.graph || {};
      const tasks = filterGraphTasks(asArray(graph.tasks));
      const visibleIds = new Set(tasks.map((task) => task.id));
      const edges = asArray(graph.edges).filter((edge) => visibleIds.has(edge.from) && visibleIds.has(edge.to));
      const container = document.getElementById("dag");
      if (!tasks.length) {
        container.innerHTML = `<div class="empty">No tasks match the current filter set.</div>`;
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
                      <div class="task-top">
                        <code>${escapeHtml(task.id)}</code>
                        <div class="chips">
                          ${statusChip(task.status || "")}
                          ${task.latest_result_status ? statusChip(`latest ${task.latest_result_status}`) : ""}
                          ${task.total_compute_label ? statusChip(task.total_compute_label) : ""}
                        </div>
                      </div>
                      <strong class="task-title">${escapeHtml(task.title)}</strong>
                      <span class="meta">${escapeHtml(task.epic_title || "")}</span>
                      <span class="subtle">${escapeHtml(task.detail || "")}</span>
                      ${dependencySummary(task) ? `<span class="subtle">${escapeHtml(dependencySummary(task))}</span>` : ""}
                      <div class="chips">
                        ${statusChip(task.priority || "")}
                        ${statusChip(task.risk || "")}
                        ${(task.domains || []).slice(0, 3).map(statusChip).join("")}
                      </div>
                      <div class="inline-links">
                        ${link("Latest result", task.latest_result_href)}
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

    function refreshTaskBrowser() {
      if (!currentSnapshot || currentSnapshot.error) return;
      renderFilterBar(currentSnapshot);
      renderDag(currentSnapshot);
    }

    function renderError(snapshot) {
      document.getElementById("project-name").textContent = snapshot.project_name || "Blackdog";
      document.getElementById("repo-root").textContent = snapshot.project_root ? `Repo: ${snapshot.project_root}` : "";
      document.getElementById("push-objective").textContent = snapshot.error?.message || "UI snapshot failed.";
      document.getElementById("stats").innerHTML = `<div class="stat error"><span class="eyebrow">Snapshot Error</span><strong>${escapeHtml(snapshot.error?.type || "Error")}</strong></div>`;
      document.getElementById("objectives").innerHTML = "";
      document.getElementById("activity-summary").innerHTML = "";
      document.getElementById("active-tasks").innerHTML = "";
      document.getElementById("supervisor-strip").innerHTML = "";
      document.getElementById("workspace-contract-note").textContent = "";
      document.getElementById("filters").innerHTML = "";
      document.getElementById("message-actions").innerHTML = "";
      document.getElementById("result-actions").innerHTML = "";
      document.getElementById("run-actions").innerHTML = "";
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
      renderActiveTasks(snapshot);
      renderObjectives(snapshot);
      renderSupervisorStrip(snapshot);
      renderMessages(snapshot);
      renderResults(snapshot);
      renderRuns(snapshot);
      refreshTaskBrowser();
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
      const dialog = document.getElementById("reader-dialog");
      document.getElementById("reader-close").addEventListener("click", () => {
        dialog.close();
      });
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) {
          dialog.close();
        }
      });
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
