from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
import json
import os

from .backlog import (
    build_plan_view,
    build_view_model,
    classify_task_status,
    load_backlog,
    sync_state_for_backlog,
)
from .config import Profile, ProjectPaths
from .store import load_events, load_inbox, load_state, load_task_results, now_iso
from .worktree import worktree_contract


UI_SNAPSHOT_SCHEMA_VERSION = 2


class UIError(RuntimeError):
    pass


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


def _artifact_href(paths: ProjectPaths, path: str | Path | None, *, must_exist: bool = False) -> str | None:
    if not path:
        return None
    candidate = Path(path).resolve()
    if must_exist and not candidate.exists():
        return None
    try:
        relative = candidate.relative_to(paths.backlog_dir.resolve())
    except ValueError:
        return None
    return quote(relative.as_posix(), safe="/")


def _find_run_dir(paths: ProjectPaths, run_id: str) -> Path | None:
    matches = sorted(paths.supervisor_runs_dir.glob(f"*-{run_id}"))
    return matches[0].resolve() if matches else None


def _child_artifacts(paths: ProjectPaths, run_dir: Path | None, task_id: str) -> dict[str, Any]:
    if run_dir is None:
        return {
            "run_dir_href": None,
            "prompt_href": None,
            "stdout_href": None,
            "stderr_href": None,
            "metadata_href": None,
            "diff_href": None,
            "diffstat_href": None,
        }
    child_dir = run_dir / task_id
    return {
        "run_dir_href": _artifact_href(paths, child_dir, must_exist=True),
        "prompt_href": _artifact_href(paths, child_dir / "prompt.txt", must_exist=True),
        "stdout_href": _artifact_href(paths, child_dir / "stdout.log", must_exist=True),
        "stderr_href": _artifact_href(paths, child_dir / "stderr.log", must_exist=True),
        "metadata_href": _artifact_href(paths, child_dir / "metadata.json", must_exist=True),
        "diff_href": _artifact_href(paths, child_dir / "changes.diff", must_exist=True),
        "diffstat_href": _artifact_href(paths, child_dir / "changes.stat.txt", must_exist=True),
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
        activity["latest_result_href"] = _artifact_href(paths, row.get("result_file"), must_exist=True)
    return activities


def _result_preview(row: dict[str, Any] | None) -> str | None:
    if not isinstance(row, dict):
        return None
    for key in ("what_changed", "residual", "validation"):
        values = row.get(key)
        if isinstance(values, list):
            for item in values:
                text = str(item).strip()
                if text:
                    return text
    return None


def _build_result_index(paths: ProjectPaths, results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for row in results:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        counts[task_id] = counts.get(task_id, 0) + 1
        if task_id in index:
            continue
        result_dir = paths.results_dir / task_id
        index[task_id] = {
            "result_count": counts[task_id],
            "latest_result_file": row.get("result_file"),
            "latest_result_href": _artifact_href(paths, row.get("result_file"), must_exist=True),
            "latest_result_dir_href": _artifact_href(paths, result_dir, must_exist=result_dir.exists()),
            "latest_result_status": row.get("status"),
            "latest_result_at": row.get("recorded_at"),
            "latest_result_preview": _result_preview(row),
            "latest_result_what_changed": list(row.get("what_changed") or []),
            "latest_result_validation": list(row.get("validation") or []),
            "latest_result_residual": list(row.get("residual") or []),
            "latest_result_needs_user_input": bool(row.get("needs_user_input")),
        }
    for task_id, count in counts.items():
        index.setdefault(
            task_id,
            {
                "result_count": count,
                "latest_result_file": None,
                "latest_result_href": None,
                "latest_result_dir_href": _artifact_href(paths, paths.results_dir / task_id, must_exist=True),
                "latest_result_status": None,
                "latest_result_at": None,
                "latest_result_preview": None,
                "latest_result_what_changed": [],
                "latest_result_validation": [],
                "latest_result_residual": [],
                "latest_result_needs_user_input": False,
            },
        )
        index[task_id]["result_count"] = count
    return index


def _build_task_run_artifacts(paths: ProjectPaths, events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    relevant_events = {"worktree_start", "child_launch", "child_launch_failed", "child_finish"}
    for event in sorted(events, key=lambda row: str(row.get("at") or "")):
        event_type = str(event.get("type") or "")
        if event_type not in relevant_events:
            continue
        task_id = str(event.get("task_id") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        run_id = str(payload.get("run_id") or "")
        if not task_id or not run_id:
            continue
        entry = rows.setdefault(
            task_id,
            {
                "last_event_at": "",
                "run_id": None,
                "run_status": None,
                "child_agent": None,
                "workspace_mode": None,
                "task_branch": None,
                "target_branch": None,
                "primary_worktree": None,
                "pid": None,
                "started_at": None,
                "finished_at": None,
                "run_dir_href": None,
                "prompt_href": None,
                "stdout_href": None,
                "stderr_href": None,
                "metadata_href": None,
                "diff_href": None,
                "diffstat_href": None,
            },
        )
        entry["last_event_at"] = str(event.get("at") or entry["last_event_at"])
        entry["run_id"] = run_id
        if payload.get("child_agent"):
            entry["child_agent"] = payload.get("child_agent")
        if payload.get("workspace_mode"):
            entry["workspace_mode"] = payload.get("workspace_mode")
        if payload.get("branch"):
            entry["task_branch"] = payload.get("branch")
        if payload.get("target_branch"):
            entry["target_branch"] = payload.get("target_branch")
        if payload.get("primary_worktree"):
            entry["primary_worktree"] = payload.get("primary_worktree")
        if event_type == "worktree_start":
            entry["run_status"] = entry.get("run_status") or "prepared"
        elif event_type == "child_launch":
            entry["run_status"] = "running"
            entry["pid"] = payload.get("pid")
            entry["started_at"] = str(event.get("at") or "")
        elif event_type == "child_launch_failed":
            entry["run_status"] = "launch-failed"
            entry["finished_at"] = str(event.get("at") or "")
        elif event_type == "child_finish":
            if payload.get("timed_out"):
                entry["run_status"] = "timed-out"
            elif payload.get("land_error"):
                entry["run_status"] = "blocked"
            elif payload.get("exit_code") not in {0, None}:
                entry["run_status"] = "failed"
            else:
                entry["run_status"] = str(payload.get("final_task_status") or "finished")
            entry["finished_at"] = str(event.get("at") or "")

        run_dir = _find_run_dir(paths, run_id)
        entry.update(_child_artifacts(paths, run_dir, task_id))

    for entry in rows.values():
        if entry.get("run_status") == "running" and not _pid_alive(entry.get("pid")):
            entry["run_status"] = "interrupted"
            entry["finished_at"] = entry.get("finished_at") or entry.get("last_event_at")
        entry["elapsed_seconds"] = _duration_seconds(_parse_iso(entry.get("started_at")), _parse_iso(entry.get("finished_at")))
        entry["elapsed_label"] = _format_duration(entry.get("elapsed_seconds"))
    return rows


def _task_links(task_row: dict[str, Any]) -> list[dict[str, str]]:
    ordered = [
        ("Prompt", task_row.get("prompt_href")),
        ("Stdout", task_row.get("stdout_href")),
        ("Stderr", task_row.get("stderr_href")),
        ("Diff", task_row.get("diff_href")),
        ("Diff Stat", task_row.get("diffstat_href")),
        ("Result JSON", task_row.get("latest_result_href")),
        ("Result Dir", task_row.get("latest_result_dir_href")),
        ("Run Dir", task_row.get("run_dir_href")),
    ]
    links: list[dict[str, str]] = []
    for label, href in ordered:
        if href:
            links.append({"label": label, "href": str(href)})
    return links


def build_ui_snapshot(profile: Profile) -> dict[str, Any]:
    snapshot = load_backlog(profile.paths, profile)
    state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
    events = load_events(profile.paths)
    messages = load_inbox(profile.paths)
    results = load_task_results(profile.paths)
    task_activity = _build_task_activity(profile.paths, state, events, results)
    task_results = _build_result_index(profile.paths, results)
    task_runs = _build_task_run_artifacts(profile.paths, events)
    summary = build_view_model(
        profile,
        snapshot,
        state,
        events=events[-20:],
        messages=messages,
        results=results,
    )
    plan = build_plan_view(profile, snapshot, state)
    tasks: list[dict[str, Any]] = []
    graph_edges: list[dict[str, str]] = []
    ordered_tasks = sorted(snapshot.tasks.values(), key=lambda task: ((task.wave or 9999), (task.lane_order or 9999), task.id))
    for task in ordered_tasks:
        status, detail = classify_task_status(task, snapshot, state, allow_high_risk=False)
        activity = task_activity.get(task.id, _empty_task_activity())
        result_info = task_results.get(task.id, {})
        run_info = task_runs.get(task.id, {})
        task_row = {
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
            "why": task.payload.get("why") or "",
            "evidence": task.payload.get("evidence") or "",
            "paths": list(task.payload.get("paths") or []),
            "checks": list(task.payload.get("checks") or []),
            "docs": list(task.payload.get("docs") or []),
            "predecessor_ids": list(task.predecessor_ids),
            "claimed_by": activity.get("claimed_by"),
            "claimed_at": activity.get("claimed_at"),
            "completed_at": activity.get("completed_at"),
            "released_at": activity.get("released_at"),
            "active_compute_seconds": activity.get("active_compute_seconds"),
            "active_compute_label": activity.get("active_compute_label"),
            "total_compute_seconds": activity.get("total_compute_seconds"),
            "total_compute_label": activity.get("total_compute_label"),
            "latest_result_status": result_info.get("latest_result_status") or activity.get("latest_result_status"),
            "latest_result_at": result_info.get("latest_result_at") or activity.get("latest_result_at"),
            "latest_result_href": result_info.get("latest_result_href") or activity.get("latest_result_href"),
            "latest_result_dir_href": result_info.get("latest_result_dir_href"),
            "latest_result_preview": result_info.get("latest_result_preview"),
            "latest_result_what_changed": result_info.get("latest_result_what_changed") or [],
            "latest_result_validation": result_info.get("latest_result_validation") or [],
            "latest_result_residual": result_info.get("latest_result_residual") or [],
            "latest_result_needs_user_input": bool(result_info.get("latest_result_needs_user_input")),
            "result_count": int(result_info.get("result_count") or 0),
            "latest_run_status": run_info.get("run_status"),
            "latest_run_at": run_info.get("last_event_at"),
            "run_dir_href": run_info.get("run_dir_href"),
            "prompt_href": run_info.get("prompt_href"),
            "stdout_href": run_info.get("stdout_href"),
            "stderr_href": run_info.get("stderr_href"),
            "metadata_href": run_info.get("metadata_href"),
            "diff_href": run_info.get("diff_href"),
            "diffstat_href": run_info.get("diffstat_href"),
            "workspace_mode": run_info.get("workspace_mode"),
            "task_branch": run_info.get("task_branch"),
            "target_branch": run_info.get("target_branch"),
            "child_agent": run_info.get("child_agent"),
            "run_elapsed_seconds": run_info.get("elapsed_seconds"),
            "run_elapsed_label": run_info.get("elapsed_label"),
        }
        task_row["links"] = _task_links(task_row)
        tasks.append(task_row)
        for predecessor_id in task.predecessor_ids:
            graph_edges.append({"from": predecessor_id, "to": task.id})

    recent_results = []
    for row in results[:10]:
        recent_results.append(
            {
                "task_id": row.get("task_id"),
                "status": row.get("status"),
                "actor": row.get("actor"),
                "recorded_at": row.get("recorded_at"),
                "result_file": row.get("result_file"),
                "result_href": _artifact_href(profile.paths, row.get("result_file"), must_exist=True),
                "preview": _result_preview(row),
            }
        )
    open_messages = [row for row in summary["open_messages"][:10]]
    active_tasks = [
        row
        for row in tasks
        if row["status"] == "claimed" or row.get("latest_run_status") in {"prepared", "running", "interrupted"}
    ]
    active_tasks.sort(key=lambda row: (str(row.get("wave") or 9999), str(row["id"])))

    return {
        "schema_version": UI_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "control_dir": str(profile.paths.control_dir),
        "profile_file": str(profile.paths.profile_file),
        "workspace_contract": worktree_contract(profile),
        "counts": summary["counts"],
        "total": summary["total"],
        "push_objective": summary["push_objective"],
        "objectives": summary["objectives"],
        "next_rows": summary["next_rows"],
        "open_messages": open_messages,
        "recent_results": recent_results,
        "recent_events": summary["recent_events"],
        "plan": plan,
        "tasks": tasks,
        "graph": {
            "tasks": tasks,
            "edges": graph_edges,
        },
        "active_tasks": active_tasks,
        "links": {
            "backlog": _artifact_href(profile.paths, profile.paths.backlog_file, must_exist=True),
            "html": _artifact_href(profile.paths, profile.paths.html_file),
            "events": _artifact_href(profile.paths, profile.paths.events_file, must_exist=True),
            "inbox": _artifact_href(profile.paths, profile.paths.inbox_file, must_exist=True),
            "results": _artifact_href(profile.paths, profile.paths.results_dir, must_exist=True),
        },
        "grouping_guide": [
            {"name": "task", "meaning": "The executable unit. Claims, results, and completion happen here."},
            {"name": "epic", "meaning": "The thematic why for related tasks."},
            {"name": "lane", "meaning": "The ordered stream inside an epic or work area."},
            {"name": "wave", "meaning": "The current phase gate across lanes."},
        ],
    }


def _snapshot_json(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True).replace("</", "<\\/")


def render_static_html(snapshot: dict[str, Any], output_path: Path) -> None:
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{snapshot["project_name"]} Backlog</title>
  <style>
    :root {{
      --page: #f6f1e8;
      --panel: #fffaf2;
      --panel-strong: #fffdf9;
      --ink: #1e160f;
      --muted: #65584b;
      --line: #d8ccbd;
      --ready: #9c5a13;
      --claimed: #155fc1;
      --done: #12724a;
      --waiting: #6f7781;
      --approval: #7b4bb7;
      --high-risk: #9d2020;
      --running: #155fc1;
      --blocked: #9d2020;
      --partial: #9c5a13;
      --shadow: 0 18px 40px rgba(58, 44, 29, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      font: 15px/1.45 "Avenir Next", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(212, 170, 97, 0.18), transparent 28%),
        linear-gradient(180deg, #fbf7f1 0%, var(--page) 100%);
    }}
    a {{ color: inherit; }}
    button, input {{ font: inherit; }}
    .page {{ max-width: 1560px; margin: 0 auto; padding: 24px; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
      margin-bottom: 18px;
    }}
    .hero {{
      display: grid;
      grid-template-columns: 1.4fr 1fr;
      gap: 18px;
      padding: 24px;
    }}
    .eyebrow {{
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 0.75rem;
    }}
    h1, h2, h3, p {{ margin: 0; }}
    .hero-copy {{
      max-width: 60ch;
      color: var(--muted);
      margin-top: 10px;
    }}
    .hero-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 16px;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 11px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
      color: var(--muted);
      font-size: 0.88rem;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
      margin-top: 18px;
    }}
    .stat {{
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--panel-strong);
      padding: 14px;
    }}
    .stat strong {{
      display: block;
      font-size: 1.7rem;
      line-height: 1.1;
      margin-top: 6px;
    }}
    .hero-side {{
      display: grid;
      gap: 14px;
      align-content: start;
    }}
    .link-row, .filter-row, .task-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .link-pill, .filter-chip, .reader-button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      min-height: 38px;
      border-radius: 999px;
      border: 1px solid var(--line);
      padding: 0 14px;
      background: var(--panel-strong);
      text-decoration: none;
      cursor: pointer;
    }}
    .filter-chip[aria-pressed="true"] {{
      background: var(--ink);
      color: white;
      border-color: var(--ink);
    }}
    .controls {{
      padding: 18px 24px;
      display: grid;
      gap: 14px;
    }}
    .search {{
      width: min(420px, 100%);
      min-height: 44px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
      padding: 0 14px;
    }}
    .section-head {{
      padding: 18px 24px 0;
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
    }}
    .section-body {{ padding: 18px 24px 24px; }}
    .focus-grid, .results-grid, .task-grid {{
      display: grid;
      gap: 12px;
    }}
    .focus-grid {{ grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }}
    .results-grid {{ grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }}
    .group-stack {{ display: grid; gap: 18px; }}
    .group-block {{
      display: grid;
      gap: 12px;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 20px;
      background: rgba(255, 255, 255, 0.58);
    }}
    .group-title {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }}
    .task-grid {{ grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }}
    .task-card, .result-card {{
      display: grid;
      gap: 10px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 20px;
      background: var(--panel-strong);
    }}
    .task-top, .result-top {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 10px;
    }}
    .task-code {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-family: "SF Mono", "Menlo", monospace;
      font-size: 0.9rem;
    }}
    .chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      justify-content: flex-end;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      color: white;
      font-size: 0.8rem;
      font-weight: 600;
      border: 0;
    }}
    .chip-ready {{ background: var(--ready); }}
    .chip-claimed, .chip-running {{ background: var(--claimed); }}
    .chip-done, .chip-success, .chip-finished {{ background: var(--done); }}
    .chip-waiting, .chip-interrupted {{ background: var(--waiting); }}
    .chip-approval {{ background: var(--approval); }}
    .chip-high-risk, .chip-blocked, .chip-failed, .chip-launch-failed, .chip-timed-out {{ background: var(--high-risk); }}
    .chip-partial {{ background: var(--partial); }}
    .task-meta, .task-note, .empty, .guide-grid, .detail-block {{
      color: var(--muted);
    }}
    .task-meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
      font-size: 0.9rem;
    }}
    .task-note {{
      min-height: 2.8em;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }}
    .task-links {{
      align-items: center;
    }}
    .task-links a, .task-links button {{
      min-height: 34px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: white;
      padding: 0 12px;
      text-decoration: none;
      color: var(--ink);
      cursor: pointer;
    }}
    .guide-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }}
    .guide-card {{
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.58);
    }}
    dialog {{
      width: min(860px, calc(100vw - 32px));
      border: 0;
      border-radius: 24px;
      padding: 0;
      box-shadow: var(--shadow);
      background: var(--panel);
    }}
    dialog::backdrop {{
      background: rgba(19, 13, 9, 0.42);
    }}
    .reader {{
      padding: 24px;
      display: grid;
      gap: 18px;
    }}
    .reader-head {{
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 12px;
    }}
    .close-button {{
      min-height: 38px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
      padding: 0 14px;
      cursor: pointer;
    }}
    .detail-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }}
    .detail-block {{
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--panel-strong);
    }}
    .detail-block strong {{
      display: block;
      color: var(--ink);
      margin-bottom: 8px;
    }}
    .detail-block ul {{
      margin: 0;
      padding-left: 18px;
    }}
    .mono {{
      font-family: "SF Mono", "Menlo", monospace;
      font-size: 0.88rem;
    }}
    .empty {{
      padding: 16px;
      border: 1px dashed var(--line);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.46);
    }}
    @media (max-width: 980px) {{
      .hero {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
  </style>
</head>
<body>
  <script id="blackdog-snapshot" type="application/json">{_snapshot_json(snapshot)}</script>
  <div class="page">
    <section class="panel hero">
      <div>
        <span class="eyebrow">Blackdog static index</span>
        <h1 id="project-name">Backlog</h1>
        <p id="hero-copy" class="hero-copy"></p>
        <div id="hero-meta" class="hero-meta"></div>
        <div id="stats" class="stats"></div>
      </div>
      <div class="hero-side">
        <div>
          <span class="eyebrow">Global Links</span>
          <div id="global-links" class="link-row"></div>
        </div>
        <div>
          <span class="eyebrow">Next Runnable</span>
          <div id="next-runnable" class="focus-grid"></div>
        </div>
      </div>
    </section>

    <section class="panel controls">
      <div class="filter-row">
        <input id="task-search" class="search" type="search" placeholder="Search task id, title, lane, epic, or result note">
      </div>
      <div id="status-filters" class="filter-row"></div>
    </section>

    <section class="panel">
      <div class="section-head">
        <div>
          <span class="eyebrow">Working Now</span>
          <h2>Active Tasks</h2>
        </div>
        <span id="active-summary" class="eyebrow"></span>
      </div>
      <div id="active-tasks" class="section-body focus-grid"></div>
    </section>

    <section class="panel">
      <div class="section-head">
        <div>
          <span class="eyebrow">Backlog</span>
          <h2>Task Index</h2>
        </div>
        <span id="task-summary" class="eyebrow"></span>
      </div>
      <div id="task-groups" class="section-body group-stack"></div>
    </section>

    <section class="panel">
      <div class="section-head">
        <div>
          <span class="eyebrow">Recent Output</span>
          <h2>Task Results</h2>
        </div>
      </div>
      <div id="recent-results" class="section-body results-grid"></div>
    </section>

    <section class="panel">
      <div class="section-head">
        <div>
          <span class="eyebrow">Plan</span>
          <h2>Grouping Guide</h2>
        </div>
      </div>
      <div id="grouping-guide" class="section-body guide-grid"></div>
    </section>
  </div>

  <dialog id="reader-dialog">
    <article class="reader">
      <div class="reader-head">
        <div>
          <span id="reader-eyebrow" class="eyebrow"></span>
          <h2 id="reader-title"></h2>
        </div>
        <form method="dialog">
          <button class="close-button" type="submit">Close</button>
        </form>
      </div>
      <div id="reader-links" class="task-links"></div>
      <div id="reader-grid" class="detail-grid"></div>
    </article>
  </dialog>

  <script>
    const snapshot = JSON.parse(document.getElementById("blackdog-snapshot").textContent);
    const allTasks = Array.isArray(snapshot.tasks) ? snapshot.tasks.slice() : [];
    const activeTasks = Array.isArray(snapshot.active_tasks) ? snapshot.active_tasks.slice() : [];
    const recentResults = Array.isArray(snapshot.recent_results) ? snapshot.recent_results.slice() : [];
    const guideRows = Array.isArray(snapshot.grouping_guide) ? snapshot.grouping_guide.slice() : [];
    const filterState = {{
      search: "",
      showDone: false,
      statuses: new Set(["ready", "claimed", "waiting", "high-risk", "approval"]),
    }};

    function escapeHtml(value) {{
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }}

    function chipClass(status) {{
      const normalized = String(status || "unknown").toLowerCase().replaceAll(" ", "-");
      return `chip chip-${{normalized}}`;
    }}

    function chip(status) {{
      if (!status) {{
        return "";
      }}
      return `<span class="${{chipClass(status)}}">${{escapeHtml(status)}}</span>`;
    }}

    function linkButton(label, href) {{
      if (!href) {{
        return "";
      }}
      return `<a href="${{escapeHtml(href)}}" target="_blank" rel="noreferrer">${{escapeHtml(label)}}</a>`;
    }}

    function resultPreview(task) {{
      return task.latest_result_preview || task.detail || "";
    }}

    function taskMatches(task) {{
      if (!filterState.showDone && task.status === "done") {{
        return false;
      }}
      if (task.status !== "done" && filterState.statuses.size && !filterState.statuses.has(task.status)) {{
        return false;
      }}
      if (!filterState.search) {{
        return true;
      }}
      const haystack = [
        task.id,
        task.title,
        task.lane_title,
        task.epic_title,
        task.detail,
        task.latest_result_preview,
      ].join(" ").toLowerCase();
      return haystack.includes(filterState.search);
    }}

    function renderHero() {{
      document.getElementById("project-name").textContent = snapshot.project_name || "Blackdog";
      const pushObjective = Array.isArray(snapshot.push_objective) ? snapshot.push_objective.join(" ") : "";
      document.getElementById("hero-copy").textContent =
        pushObjective || "Static backlog report generated from the current backlog, state, results, and supervisor artifacts.";
      const contract = snapshot.workspace_contract || {{}};
      document.getElementById("hero-meta").innerHTML = [
        contract.target_branch ? `<span class="pill">Target branch: ${{escapeHtml(contract.target_branch)}}</span>` : "",
        contract.primary_dirty === false ? `<span class="pill">Primary worktree clean</span>` : "",
        contract.workspace_has_local_blackdog ? `<span class="pill">Local .VE ready</span>` : `<span class="pill">Bootstrap .VE in this worktree</span>`,
        snapshot.generated_at ? `<span class="pill">Rendered ${{escapeHtml(snapshot.generated_at)}}</span>` : "",
      ].filter(Boolean).join("");
      const counts = snapshot.counts || {{}};
      const stats = [
        ["Total", snapshot.total || 0],
        ["Ready", counts.ready || 0],
        ["Claimed", counts.claimed || 0],
        ["Done", counts.done || 0],
        ["High risk", counts["high-risk"] || 0],
      ];
      document.getElementById("stats").innerHTML = stats.map(([label, value]) => `
        <article class="stat">
          <span class="eyebrow">${{escapeHtml(label)}}</span>
          <strong>${{escapeHtml(value)}}</strong>
        </article>
      `).join("");
      const links = snapshot.links || {{}};
      document.getElementById("global-links").innerHTML = [
        ["Backlog", links.backlog],
        ["Events", links.events],
        ["Inbox", links.inbox],
        ["Results", links.results],
      ].map(([label, href]) => linkButton(label, href)).join("");
      const nextRows = Array.isArray(snapshot.next_rows) ? snapshot.next_rows : [];
      document.getElementById("next-runnable").innerHTML = nextRows.length
        ? nextRows.slice(0, 4).map((row) => `
            <article class="task-card">
              <div class="task-top">
                <span class="task-code">${{escapeHtml(row.id)}}</span>
                <div class="chips">${{chip(row.risk)}}</div>
              </div>
              <h3>${{escapeHtml(row.title)}}</h3>
              <div class="task-meta">
                <span>Wave ${{escapeHtml(row.wave ?? "unplanned")}}</span>
                <span>${{escapeHtml(row.lane || "")}}</span>
              </div>
            </article>
          `).join("")
        : `<div class="empty">No runnable tasks.</div>`;
    }}

    function renderFilters() {{
      const rows = [
        ["ready", "Ready"],
        ["claimed", "Claimed"],
        ["waiting", "Waiting"],
        ["high-risk", "High risk"],
        ["approval", "Approval"],
        ["done", filterState.showDone ? "Hide done" : "Show done"],
      ];
      document.getElementById("status-filters").innerHTML = rows.map(([key, label]) => {{
        const active = key === "done" ? filterState.showDone : filterState.statuses.has(key);
        return `<button class="filter-chip" type="button" data-filter="${{escapeHtml(key)}}" aria-pressed="${{active ? "true" : "false"}}">${{escapeHtml(label)}}</button>`;
      }}).join("");
      for (const button of document.querySelectorAll("[data-filter]")) {{
        button.addEventListener("click", () => {{
          const key = button.getAttribute("data-filter");
          if (key === "done") {{
            filterState.showDone = !filterState.showDone;
          }} else if (filterState.statuses.has(key)) {{
            filterState.statuses.delete(key);
          }} else {{
            filterState.statuses.add(key);
          }}
          renderTasks();
          renderFilters();
        }});
      }}
      const search = document.getElementById("task-search");
      search.value = filterState.search;
      search.oninput = (event) => {{
        filterState.search = String(event.target.value || "").trim().toLowerCase();
        renderTasks();
      }};
    }}

    function renderTaskLinks(task) {{
      const links = Array.isArray(task.links) ? task.links : [];
      return links.map((row) => linkButton(row.label, row.href)).join("");
    }}

    function taskCard(task) {{
      return `
        <article class="task-card" id="${{escapeHtml(task.id)}}">
          <div class="task-top">
            <span class="task-code">${{escapeHtml(task.id)}}</span>
            <div class="chips">
              ${{chip(task.status)}}
              ${{task.latest_result_status ? chip(task.latest_result_status) : ""}}
              ${{task.latest_run_status ? chip(task.latest_run_status) : ""}}
            </div>
          </div>
          <h3>${{escapeHtml(task.title)}}</h3>
          <div class="task-meta">
            <span>Wave ${{escapeHtml(task.wave ?? "unplanned")}}</span>
            <span>${{escapeHtml(task.epic_title || "No epic")}}</span>
            <span>${{escapeHtml(task.lane_title || "No lane")}}</span>
            <span>${{escapeHtml(task.priority || "")}}</span>
            <span>${{escapeHtml(task.risk || "")}}</span>
          </div>
          <div class="task-meta">
            ${{task.claimed_by ? `<span>claimed by ${{escapeHtml(task.claimed_by)}}</span>` : ""}}
            ${{task.total_compute_label ? `<span>total ${{escapeHtml(task.total_compute_label)}}</span>` : ""}}
            ${{task.run_elapsed_label ? `<span>run ${{escapeHtml(task.run_elapsed_label)}}</span>` : ""}}
            ${{task.target_branch ? `<span>${{escapeHtml(task.task_branch || "task")}} -> ${{escapeHtml(task.target_branch)}}</span>` : ""}}
          </div>
          <p class="task-note">${{escapeHtml(resultPreview(task))}}</p>
          <div class="task-links">
            ${{renderTaskLinks(task)}}
            <button class="reader-button" type="button" data-reader="${{escapeHtml(task.id)}}">Read</button>
          </div>
        </article>
      `;
    }}

    function renderActiveTasks() {{
      const container = document.getElementById("active-tasks");
      document.getElementById("active-summary").textContent = `${{activeTasks.length}} active task(s)`;
      container.innerHTML = activeTasks.length
        ? activeTasks.map(taskCard).join("")
        : `<div class="empty">No claimed or active task work right now.</div>`;
    }}

    function renderTasks() {{
      const visibleTasks = allTasks.filter(taskMatches);
      document.getElementById("task-summary").textContent = `${{visibleTasks.length}} visible of ${{allTasks.length}} total`;
      if (!visibleTasks.length) {{
        document.getElementById("task-groups").innerHTML = `<div class="empty">No tasks match the current filters.</div>`;
        wireReaderButtons();
        return;
      }}
      const groups = new Map();
      for (const task of visibleTasks) {{
        const key = `${{task.wave ?? "unplanned"}}::${{task.lane_title || "Unplanned"}}`;
        if (!groups.has(key)) {{
          groups.set(key, {{
            wave: task.wave,
            laneTitle: task.lane_title || "Unplanned",
            epicTitle: task.epic_title || "No epic",
            tasks: [],
          }});
        }}
        groups.get(key).tasks.push(task);
      }}
      const ordered = Array.from(groups.values()).sort((left, right) => {{
        const leftWave = left.wave == null ? 9999 : Number(left.wave);
        const rightWave = right.wave == null ? 9999 : Number(right.wave);
        if (leftWave !== rightWave) {{
          return leftWave - rightWave;
        }}
        return String(left.laneTitle).localeCompare(String(right.laneTitle));
      }});
      document.getElementById("task-groups").innerHTML = ordered.map((group) => `
        <section class="group-block">
          <div class="group-title">
            <div>
              <span class="eyebrow">Wave ${{escapeHtml(group.wave ?? "unplanned")}}</span>
              <h3>${{escapeHtml(group.laneTitle)}}</h3>
            </div>
            <span class="eyebrow">${{escapeHtml(group.epicTitle)}} · ${{group.tasks.length}} task(s)</span>
          </div>
          <div class="task-grid">${{group.tasks.map(taskCard).join("")}}</div>
        </section>
      `).join("");
      wireReaderButtons();
    }}

    function renderRecentResults() {{
      const container = document.getElementById("recent-results");
      container.innerHTML = recentResults.length
        ? recentResults.map((row) => `
            <article class="result-card">
              <div class="result-top">
                <strong>${{escapeHtml(row.task_id || "?")}}</strong>
                <div class="chips">${{chip(row.status)}}</div>
              </div>
              <div class="task-meta">
                <span>${{escapeHtml(row.actor || "")}}</span>
                <span>${{escapeHtml(row.recorded_at || "")}}</span>
              </div>
              <p class="task-note">${{escapeHtml(row.preview || "")}}</p>
              <div class="task-links">
                ${{linkButton("Result JSON", row.result_href)}}
                ${{row.task_id ? `<a href="#${{escapeHtml(row.task_id)}}">Task</a>` : ""}}
              </div>
            </article>
          `).join("")
        : `<div class="empty">No task results recorded yet.</div>`;
    }}

    function renderGuide() {{
      const container = document.getElementById("grouping-guide");
      container.innerHTML = guideRows.map((row) => `
        <article class="guide-card">
          <span class="eyebrow">${{escapeHtml(row.name || "")}}</span>
          <strong>${{escapeHtml(row.name || "")}}</strong>
          <p>${{escapeHtml(row.meaning || "")}}</p>
        </article>
      `).join("");
    }}

    function detailBlock(label, content) {{
      if (!content) {{
        return "";
      }}
      return `
        <section class="detail-block">
          <strong>${{escapeHtml(label)}}</strong>
          ${{content}}
        </section>
      `;
    }}

    function listBlock(items) {{
      if (!Array.isArray(items) || !items.length) {{
        return "";
      }}
      return `<ul>${{items.map((item) => `<li>${{escapeHtml(item)}}</li>`).join("")}}</ul>`;
    }}

    function openReader(taskId) {{
      const task = allTasks.find((row) => row.id === taskId);
      if (!task) {{
        return;
      }}
      document.getElementById("reader-eyebrow").textContent =
        `Wave ${{task.wave ?? "unplanned"}} · ${{task.epic_title || "No epic"}} · ${{task.lane_title || "No lane"}}`;
      document.getElementById("reader-title").textContent = `${{task.id}} ${{task.title}}`;
      document.getElementById("reader-links").innerHTML = renderTaskLinks(task);
      document.getElementById("reader-grid").innerHTML = [
        detailBlock("Why", task.why ? `<p>${{escapeHtml(task.why)}}</p>` : ""),
        detailBlock("Evidence", task.evidence ? `<p>${{escapeHtml(task.evidence)}}</p>` : ""),
        detailBlock("Safe first slice", task.safe_first_slice ? `<p>${{escapeHtml(task.safe_first_slice)}}</p>` : ""),
        detailBlock("Paths", listBlock(task.paths)),
        detailBlock("Checks", listBlock(task.checks)),
        detailBlock("Docs", listBlock(task.docs)),
        detailBlock("Latest result changes", listBlock(task.latest_result_what_changed)),
        detailBlock("Latest result validation", listBlock(task.latest_result_validation)),
        detailBlock("Latest result residual", listBlock(task.latest_result_residual)),
      ].filter(Boolean).join("");
      document.getElementById("reader-dialog").showModal();
    }}

    function wireReaderButtons() {{
      for (const button of document.querySelectorAll("[data-reader]")) {{
        button.onclick = () => openReader(button.getAttribute("data-reader"));
      }}
    }}

    renderHero();
    renderFilters();
    renderActiveTasks();
    renderTasks();
    renderRecentResults();
    renderGuide();
  </script>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
