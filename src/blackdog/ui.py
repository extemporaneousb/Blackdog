from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
import html as html_lib
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


UI_SNAPSHOT_SCHEMA_VERSION = 3


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


def _title_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("-", " ").replace("_", " ").title()


def _actor_role(actor: Any) -> str:
    normalized = str(actor or "").strip().lower()
    if not normalized or normalized == "blackdog":
        return "system"
    if "supervisor" in normalized:
        return "supervisor"
    return "agent"


def _latest_activity(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not events:
        return None
    latest = max(events, key=lambda row: str(row.get("at") or ""))
    actor = str(latest.get("actor") or "")
    payload = latest.get("payload") if isinstance(latest.get("payload"), dict) else {}
    summary = (
        str(payload.get("note") or "").strip()
        or str(payload.get("title") or "").strip()
        or str(payload.get("status") or "").strip()
        or _title_label(latest.get("type"))
    )
    return {
        "at": latest.get("at"),
        "actor": actor,
        "actor_role": _actor_role(actor),
        "type": latest.get("type"),
        "type_label": _title_label(latest.get("type")),
        "task_id": latest.get("task_id"),
        "summary": summary,
    }


def _operator_status(task_row: dict[str, Any]) -> dict[str, str]:
    task_status = str(task_row.get("status") or "")
    run_status = str(task_row.get("latest_run_status") or "")
    result_status = str(task_row.get("latest_result_status") or "")
    detail = str(task_row.get("detail") or "").strip()

    if task_status == "done":
        return {
            "operator_status": "Complete",
            "operator_status_key": "complete",
            "operator_status_detail": str(task_row.get("completed_at") or detail or "Task completed"),
        }

    if run_status == "running":
        actor = str(task_row.get("child_agent") or task_row.get("claimed_by") or "agent").strip()
        run_detail = f"{actor} running" if actor else "Active child run"
        return {
            "operator_status": "Running",
            "operator_status_key": "running",
            "operator_status_detail": run_detail,
        }

    if run_status in {"failed", "launch-failed", "timed-out", "interrupted"}:
        run_detail = {
            "failed": "Child run failed",
            "launch-failed": "Child launch failed",
            "timed-out": "Child run timed out",
            "interrupted": "Child run interrupted",
        }[run_status]
        return {
            "operator_status": "Failed",
            "operator_status_key": "failed",
            "operator_status_detail": run_detail,
        }

    if run_status == "blocked":
        return {
            "operator_status": "Blocked",
            "operator_status_key": "blocked",
            "operator_status_detail": "Landing blocked by the target branch state",
        }

    if result_status == "blocked":
        return {
            "operator_status": "Blocked",
            "operator_status_key": "blocked",
            "operator_status_detail": str(task_row.get("latest_result_preview") or "Latest result is blocked"),
        }

    if task_status == "approval":
        return {
            "operator_status": "Blocked",
            "operator_status_key": "blocked",
            "operator_status_detail": detail or "Approval required",
        }

    if task_status == "high-risk":
        return {
            "operator_status": "Blocked",
            "operator_status_key": "blocked",
            "operator_status_detail": detail or "High-risk task",
        }

    if task_status == "claimed" or run_status == "prepared":
        owner = str(task_row.get("claimed_by") or task_row.get("child_agent") or "").strip()
        claimed_detail = f"Claimed by {owner}" if owner else "Task claimed"
        return {
            "operator_status": "Claimed",
            "operator_status_key": "claimed",
            "operator_status_detail": claimed_detail,
        }

    if task_status == "waiting":
        return {
            "operator_status": "Waiting",
            "operator_status_key": "waiting",
            "operator_status_detail": detail or "Waiting on dependency or wave gate",
        }

    return {
        "operator_status": "Ready",
        "operator_status_key": "ready",
        "operator_status_detail": detail or "Claimable now",
    }


def _activity_message(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if event_type == "claim":
        return "claimed"
    if event_type == "release":
        note = str(payload.get("note") or "").strip()
        return f"released · {note}" if note else "released"
    if event_type == "complete":
        note = str(payload.get("note") or "").strip()
        return f"completed · {note}" if note else "completed"
    if event_type == "child_launch":
        return "run started"
    if event_type == "child_finish":
        if payload.get("timed_out"):
            return "run timed out"
        if payload.get("land_error"):
            return "run blocked"
        if payload.get("exit_code") not in {0, None}:
            return "run failed"
        final_status = _title_label(payload.get("final_task_status") or "finished").lower()
        return f"run {final_status}"
    if event_type == "task_result":
        status = _title_label(payload.get("status") or "recorded").lower()
        return f"result {status}"
    return _title_label(event_type).lower()


def _build_task_timeline(events: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    timelines: dict[str, list[dict[str, str]]] = {}
    relevant = {"claim", "release", "complete", "child_launch", "child_finish", "task_result"}
    for event in sorted(events, key=lambda row: str(row.get("at") or "")):
        event_type = str(event.get("type") or "")
        task_id = str(event.get("task_id") or "")
        if event_type not in relevant or not task_id:
            continue
        timelines.setdefault(task_id, []).append(
            {
                "at": str(event.get("at") or ""),
                "actor": str(event.get("actor") or ""),
                "message": _activity_message(event),
            }
        )
    return timelines


def _task_links(task_row: dict[str, Any]) -> list[dict[str, str]]:
    ordered = [
        ("Prompt", task_row.get("prompt_href")),
        ("Stdout", task_row.get("stdout_href")),
        ("Stderr", task_row.get("stderr_href")),
        ("Metadata", task_row.get("metadata_href")),
        ("Diff", task_row.get("diff_href")),
        ("Diffstat", task_row.get("diffstat_href")),
        ("Result", task_row.get("latest_result_href")),
        ("Result Dir", task_row.get("latest_result_dir_href")),
        ("Run", task_row.get("run_dir_href")),
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
    task_timeline = _build_task_timeline(events)
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
            "activity": list(task_timeline.get(task.id, [])),
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
        task_row.update(_operator_status(task_row))
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
        "last_activity": _latest_activity(events),
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
    title = html_lib.escape(str(snapshot["project_name"]))
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__BLACKDOG_TITLE__ Backlog</title>
  <style>
    :root {
      --page: #f3efe7;
      --panel: rgba(255, 252, 247, 0.92);
      --panel-strong: #fffdf8;
      --ink: #1f160f;
      --muted: #695d50;
      --line: #d7cabb;
      --accent: #b66a23;
      --shadow: 0 18px 40px rgba(58, 44, 29, 0.08);
      --ready-bg: #f7dfbb;
      --ready-fg: #7a4d16;
      --claimed-bg: #e0e7ff;
      --claimed-fg: #3f3cb9;
      --running-bg: #dbeafe;
      --running-fg: #0f4fb5;
      --waiting-bg: #ece6dc;
      --waiting-fg: #615548;
      --blocked-bg: #fde0ba;
      --blocked-fg: #8b4c04;
      --failed-bg: #f8d4d4;
      --failed-fg: #8d2020;
      --complete-bg: #d9efdf;
      --complete-fg: #155f38;
      --partial-bg: #f7e0bb;
      --partial-fg: #81561d;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      font: 15px/1.45 "Avenir Next", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(214, 150, 63, 0.18), transparent 24%),
        linear-gradient(180deg, #fcf8f2 0%, var(--page) 100%);
    }
    a { color: inherit; }
    button, input { font: inherit; }
    h1, h2, h3, p { margin: 0; }
    .page { width: 100%; padding: 20px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 24px;
      box-shadow: var(--shadow);
    }
    .eyebrow {
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      font-size: 0.74rem;
    }
    .topbar {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 18px;
      margin-bottom: 18px;
      align-items: start;
    }
    .hero {
      padding: 24px;
      display: grid;
      gap: 16px;
    }
    .hero-copy {
      color: var(--muted);
      max-width: 72ch;
      font-size: 1rem;
    }
    .last-updated {
      color: var(--muted);
      font-size: 1rem;
    }
    .tag-row, .link-row, .artifact-row, .lane-summary, .reader-links {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .pill, .link-pill, .artifact-link, .reader-action, .search-hint {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      min-height: 34px;
      padding: 0 12px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
      text-decoration: none;
      color: var(--muted);
    }
    .top-stats {
      margin-bottom: 18px;
      padding: 16px 18px;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));
      gap: 12px;
    }
    .stat-card {
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--panel-strong);
      padding: 14px;
      text-align: left;
      cursor: pointer;
      transition: transform 120ms ease, border-color 120ms ease, box-shadow 120ms ease;
    }
    .stat-card:hover {
      transform: translateY(-1px);
      border-color: rgba(182, 106, 35, 0.45);
    }
    .stat-card.active {
      border-color: var(--ink);
      box-shadow: inset 0 0 0 1px var(--ink);
    }
    .stat-card strong {
      display: block;
      margin-top: 8px;
      font-size: 2rem;
      line-height: 1;
      color: var(--ink);
    }
    .side-panel {
      padding: 18px;
      display: grid;
      gap: 12px;
    }
    .side-head {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
    }
    .mini-stack {
      display: grid;
      gap: 10px;
    }
    .mini-card, .task-card, .result-card {
      display: grid;
      gap: 10px;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--panel-strong);
    }
    .mini-card, .task-card[data-task-id], .result-card[data-result-task] {
      cursor: pointer;
      transition: transform 120ms ease, border-color 120ms ease;
    }
    .mini-card:hover, .task-card[data-task-id]:hover, .result-card[data-result-task]:hover {
      transform: translateY(-1px);
      border-color: rgba(182, 106, 35, 0.45);
    }
    .mini-card p, .result-card p, .task-summary {
      color: var(--muted);
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .mini-top, .task-bar, .result-top, .lane-top, .section-head, .reader-head {
      display: flex;
      justify-content: space-between;
      align-items: start;
      gap: 12px;
    }
    .controls {
      margin-bottom: 18px;
      padding: 16px 18px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .search {
      flex: 1 1 320px;
      max-width: 480px;
      min-height: 42px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
      padding: 0 14px;
      color: var(--ink);
    }
    .board-panel {
      margin-bottom: 18px;
      padding: 18px;
    }
    .lane-board {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
      gap: 16px;
      margin-top: 16px;
      align-items: start;
    }
    .lane-column {
      display: grid;
      gap: 12px;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 22px;
      background: rgba(255, 255, 255, 0.54);
      min-height: 220px;
    }
    .lane-stack, .results-grid {
      display: grid;
      gap: 12px;
    }
    .task-code {
      font-family: "SF Mono", "Menlo", monospace;
      font-size: 0.88rem;
      color: var(--muted);
    }
    .lane-count {
      color: var(--muted);
      font-size: 0.9rem;
      white-space: nowrap;
    }
    .task-title {
      font-size: 1.04rem;
      line-height: 1.25;
    }
    .task-meta, .mini-meta, .result-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
      color: var(--muted);
      font-size: 0.86rem;
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      justify-content: flex-end;
    }
    .chip {
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      padding: 0 10px;
      border-radius: 999px;
      border: 1px solid transparent;
      font-size: 0.77rem;
      font-weight: 700;
      white-space: nowrap;
    }
    .chip-total { background: #efe7db; border-color: #d9cbbb; color: #62574a; }
    .chip-ready { background: var(--ready-bg); border-color: #e7bf85; color: var(--ready-fg); }
    .chip-claimed { background: var(--claimed-bg); border-color: #bec8ff; color: var(--claimed-fg); }
    .chip-running { background: var(--running-bg); border-color: #a9cbff; color: var(--running-fg); }
    .chip-waiting { background: var(--waiting-bg); border-color: #d1c5b8; color: var(--waiting-fg); }
    .chip-blocked, .chip-approval, .chip-high-risk { background: var(--blocked-bg); border-color: #eeb469; color: var(--blocked-fg); }
    .chip-failed, .chip-launch-failed, .chip-timed-out, .chip-interrupted { background: var(--failed-bg); border-color: #eca2a2; color: var(--failed-fg); }
    .chip-complete, .chip-done, .chip-success, .chip-finished { background: var(--complete-bg); border-color: #9ed0af; color: var(--complete-fg); }
    .chip-partial { background: var(--partial-bg); border-color: #e3bc84; color: var(--partial-fg); }
    .chip-subtle { background: #f0ebe3; border-color: #d8ccbc; color: var(--muted); }
    .artifact-link, .reader-action {
      min-height: 32px;
      padding: 0 11px;
      color: var(--ink);
      background: white;
    }
    .result-panel {
      padding: 18px;
    }
    .results-grid {
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      margin-top: 16px;
    }
    .empty {
      padding: 16px;
      border: 1px dashed var(--line);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.46);
      color: var(--muted);
    }
    dialog {
      width: min(1024px, calc(100vw - 32px));
      border: 0;
      border-radius: 24px;
      padding: 0;
      background: var(--panel);
      box-shadow: var(--shadow);
    }
    dialog::backdrop {
      background: rgba(19, 13, 9, 0.42);
    }
    .reader {
      padding: 22px;
      display: grid;
      gap: 16px;
    }
    .close-button {
      min-height: 36px;
      padding: 0 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--panel-strong);
      cursor: pointer;
    }
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .detail-block {
      min-width: 0;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: var(--panel-strong);
      color: var(--muted);
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .detail-block strong {
      display: block;
      margin-bottom: 8px;
      color: var(--ink);
    }
    .detail-block.wide {
      grid-column: 1 / -1;
    }
    .detail-block ul {
      margin: 0;
      padding-left: 18px;
    }
    .detail-block li {
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .activity-list {
      display: grid;
      gap: 10px;
    }
    .activity-row {
      display: grid;
      grid-template-columns: 160px 180px minmax(0, 1fr);
      gap: 12px;
      align-items: start;
      padding-bottom: 10px;
      border-bottom: 1px solid rgba(215, 202, 187, 0.75);
    }
    .activity-row:last-child {
      padding-bottom: 0;
      border-bottom: 0;
    }
    .activity-row span {
      min-width: 0;
      overflow-wrap: anywhere;
      word-break: break-word;
    }
    .mono {
      font-family: "SF Mono", "Menlo", monospace;
      font-size: 0.88rem;
    }
    @media (max-width: 980px) {
      .topbar { grid-template-columns: 1fr; }
      .page { padding: 16px; }
      .detail-grid { grid-template-columns: 1fr; }
      .detail-block.wide { grid-column: auto; }
      .activity-row { grid-template-columns: 1fr; gap: 4px; }
    }
  </style>
</head>
<body>
  <script id="blackdog-snapshot" type="application/json">__BLACKDOG_SNAPSHOT__</script>
  <div class="page">
    <section class="topbar">
      <article class="panel hero">
        <span class="eyebrow">Blackdog board</span>
        <h1 id="project-name">Backlog</h1>
        <p id="last-updated" class="last-updated"></p>
        <p id="hero-copy" class="hero-copy"></p>
        <div id="hero-meta" class="tag-row"></div>
        <div id="global-links" class="link-row"></div>
      </article>
      <aside class="panel side-panel">
        <div class="side-head">
          <div>
            <span class="eyebrow">Inbox</span>
            <h2 id="inbox-title">Open Messages</h2>
          </div>
          <a id="inbox-link" class="link-pill" href="#" target="_blank" rel="noreferrer">Inbox JSON</a>
        </div>
        <div id="inbox-list" class="mini-stack"></div>
      </aside>
    </section>

    <section class="panel top-stats">
      <div id="stats" class="stats"></div>
    </section>

    <section class="panel controls">
      <input id="task-search" class="search" type="search" placeholder="Search task id, title, lane, epic, or artifact status">
      <div class="tag-row">
        <span id="filter-summary" class="search-hint"></span>
      </div>
    </section>

    <section class="panel board-panel">
      <div class="section-head">
        <div>
          <span class="eyebrow">Lane View</span>
          <h2>Operator Board</h2>
        </div>
        <span id="board-summary" class="eyebrow"></span>
      </div>
      <div id="lane-board" class="lane-board"></div>
    </section>

    <section class="panel result-panel">
      <div class="section-head">
        <div>
          <span class="eyebrow">Recent Output</span>
          <h2>Results</h2>
        </div>
      </div>
      <div id="recent-results" class="results-grid"></div>
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
      <div id="reader-links" class="reader-links"></div>
      <div id="reader-grid" class="detail-grid"></div>
    </article>
  </dialog>

  <script>
    const snapshot = JSON.parse(document.getElementById("blackdog-snapshot").textContent);
    const allTasks = Array.isArray(snapshot.tasks) ? snapshot.tasks.slice() : [];
    const recentResults = Array.isArray(snapshot.recent_results) ? snapshot.recent_results.slice() : [];
    const openMessages = Array.isArray(snapshot.open_messages) ? snapshot.open_messages.slice() : [];
    const lanePlan = Array.isArray(snapshot.plan?.lanes) ? snapshot.plan.lanes.slice() : [];
    const filterState = { search: "", status: "total" };
    const statusMeta = {
      total: { label: "Total" },
      ready: { label: "Ready" },
      running: { label: "Running" },
      claimed: { label: "Claimed" },
      waiting: { label: "Waiting" },
      blocked: { label: "Blocked" },
      failed: { label: "Failed" },
      complete: { label: "Complete" }
    };
    const taskStatusLabels = {
      ready: "Ready",
      claimed: "Claimed",
      waiting: "Waiting",
      approval: "Approval",
      "high-risk": "High Risk",
      done: "Complete"
    };
    const resultStatusLabels = {
      success: "Success",
      partial: "Partial",
      blocked: "Blocked"
    };
    const runStatusLabels = {
      prepared: "Prepared",
      running: "Running",
      blocked: "Blocked",
      failed: "Failed",
      "launch-failed": "Launch Failed",
      "timed-out": "Timed Out",
      interrupted: "Interrupted",
      finished: "Finished",
      done: "Complete"
    };
    const statusOrder = {
      running: 0,
      claimed: 1,
      blocked: 2,
      failed: 3,
      waiting: 4,
      ready: 5,
      complete: 6
    };

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function normalizeStatus(value) {
      return String(value || "").trim().toLowerCase().replaceAll(" ", "-");
    }

    function chip(label, key) {
      if (!label) {
        return "";
      }
      const normalized = normalizeStatus(key || label) || "subtle";
      return `<span class="chip chip-${escapeHtml(normalized)}">${escapeHtml(label)}</span>`;
    }

    function artifactLink(label, href) {
      if (!href) {
        return "";
      }
      return `<a class="artifact-link" href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`;
    }

    function relativeTime(value) {
      if (!value) {
        return "just now";
      }
      const then = Date.parse(String(value));
      if (Number.isNaN(then)) {
        return String(value);
      }
      const delta = Math.max(0, Math.floor((Date.now() - then) / 1000));
      if (delta < 60) {
        return `${delta}s ago`;
      }
      const minutes = Math.floor(delta / 60);
      if (minutes < 60) {
        return `${minutes}m ago`;
      }
      const hours = Math.floor(minutes / 60);
      if (hours < 24) {
        return `${hours}h ago`;
      }
      const days = Math.floor(hours / 24);
      return `${days}d ago`;
    }

    function formatTimestamp(value) {
      if (!value) {
        return "";
      }
      const parsed = Date.parse(String(value));
      if (Number.isNaN(parsed)) {
        return String(value);
      }
      return new Date(parsed).toLocaleString();
    }

    function globalLinks() {
      const links = snapshot.links || {};
      return [
        ["Backlog", links.backlog],
        ["Events", links.events],
        ["Results", links.results],
        ["Inbox", links.inbox],
        ["HTML", links.html]
      ];
    }

    function countStatuses(tasks) {
      const counts = { total: tasks.length, ready: 0, running: 0, claimed: 0, waiting: 0, blocked: 0, failed: 0, complete: 0 };
      for (const task of tasks) {
        const key = normalizeStatus(task.operator_status_key || "ready");
        counts[key] = (counts[key] || 0) + 1;
      }
      return counts;
    }

    function taskSummary(task) {
      return task.latest_result_preview || task.operator_status_detail || task.detail || task.safe_first_slice || "";
    }

    function taskMatches(task) {
      if (filterState.status !== "total" && normalizeStatus(task.operator_status_key) !== filterState.status) {
        return false;
      }
      if (!filterState.search) {
        return true;
      }
      const haystack = [
        task.id,
        task.title,
        task.lane_title,
        task.epic_title,
        task.operator_status,
        task.operator_status_detail,
        task.detail,
        task.latest_result_preview,
        task.latest_run_status,
        task.latest_result_status
      ].join(" ").toLowerCase();
      return haystack.includes(filterState.search);
    }

    function laneRows(tasks) {
      const rows = new Map();
      for (const lane of lanePlan) {
        rows.set(String(lane.id), {
          id: String(lane.id),
          title: lane.title || "Unplanned",
          wave: lane.wave,
          tasks: []
        });
      }
      for (const task of tasks) {
        const key = String(task.lane_id || `lane:${task.id}`);
        if (!rows.has(key)) {
          rows.set(key, {
            id: key,
            title: task.lane_title || "Unplanned",
            wave: task.wave,
            tasks: []
          });
        }
        rows.get(key).tasks.push(task);
      }
      return Array.from(rows.values())
        .filter((lane) => lane.tasks.length)
        .map((lane) => ({
          ...lane,
          tasks: lane.tasks.sort((left, right) => {
            const leftStatus = statusOrder[normalizeStatus(left.operator_status_key)] ?? 99;
            const rightStatus = statusOrder[normalizeStatus(right.operator_status_key)] ?? 99;
            if (leftStatus !== rightStatus) {
              return leftStatus - rightStatus;
            }
            return String(left.id).localeCompare(String(right.id));
          })
        }))
        .sort((left, right) => {
          const leftWave = left.wave == null ? 9999 : Number(left.wave);
          const rightWave = right.wave == null ? 9999 : Number(right.wave);
          if (leftWave !== rightWave) {
            return leftWave - rightWave;
          }
          return String(left.title).localeCompare(String(right.title));
        });
    }

    function renderHeader() {
      document.getElementById("project-name").textContent = snapshot.project_name || "Blackdog";
      const objective = Array.isArray(snapshot.push_objective) ? snapshot.push_objective.join(" ") : "";
      document.getElementById("hero-copy").textContent =
        objective || "Static backlog board with direct links to task prompts, results, diffs, and run artifacts.";
      const activity = snapshot.last_activity || {};
      const actor = activity.actor ? ` by ${activity.actor}` : "";
      const role = activity.actor_role ? ` (${activity.actor_role})` : "";
      const source = activity.type_label ? ` via ${activity.type_label.toLowerCase()}` : "";
      document.getElementById("last-updated").textContent =
        `Last updated ${relativeTime(activity.at || snapshot.generated_at)}${actor}${role}${source}`;

      const contract = snapshot.workspace_contract || {};
      document.getElementById("hero-meta").innerHTML = [
        contract.target_branch ? `<span class="pill">Target ${escapeHtml(contract.target_branch)}</span>` : "",
        contract.primary_dirty === false ? `<span class="pill">Primary clean</span>` : `<span class="pill">Primary dirty</span>`,
        contract.workspace_has_local_blackdog ? `<span class="pill">Local .VE ready</span>` : `<span class="pill">Bootstrap .VE here</span>`,
        snapshot.generated_at ? `<span class="pill">Rendered ${escapeHtml(relativeTime(snapshot.generated_at))}</span>` : ""
      ].filter(Boolean).join("");

      document.getElementById("global-links").innerHTML = globalLinks()
        .map(([label, href]) => href ? `<a class="link-pill" href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>` : "")
        .join("");
    }

    function renderStats() {
      const counts = countStatuses(allTasks);
      const order = ["total", "ready", "running", "claimed", "waiting", "blocked", "failed", "complete"];
      document.getElementById("stats").innerHTML = order.map((key) => `
        <button class="stat-card ${filterState.status === key ? "active" : ""}" type="button" data-status-filter="${escapeHtml(key)}">
          <span class="eyebrow">${escapeHtml(statusMeta[key].label)}</span>
          <strong>${escapeHtml(counts[key] || 0)}</strong>
        </button>
      `).join("");
    }

    function renderInbox() {
      document.getElementById("inbox-title").textContent = `${openMessages.length} open`;
      const inboxHref = snapshot.links?.inbox || "#";
      const inboxLink = document.getElementById("inbox-link");
      inboxLink.href = inboxHref;
      inboxLink.style.visibility = inboxHref ? "visible" : "hidden";
      document.getElementById("inbox-list").innerHTML = openMessages.length
        ? openMessages.slice(0, 3).map((message) => `
            <article class="mini-card" data-message-id="${escapeHtml(message.message_id)}">
              <div class="mini-top">
                <strong>${escapeHtml(message.sender || "unknown")}</strong>
                <span class="eyebrow">${escapeHtml(relativeTime(message.at))}</span>
              </div>
              <div class="mini-meta">
                <span>${escapeHtml(message.recipient || "")}</span>
                ${message.task_id ? `<span>${escapeHtml(message.task_id)}</span>` : ""}
              </div>
              <p>${escapeHtml(message.body || "")}</p>
            </article>
          `).join("")
        : `<div class="empty">No open inbox items.</div>`;
    }

    function secondaryChips(task) {
      const rows = [];
      if (task.status && !["ready", "claimed", "waiting", "done"].includes(task.status)) {
        rows.push(chip(taskStatusLabels[task.status] || task.status, task.status));
      }
      if (task.latest_run_status) {
        const covered = {
          running: ["running"],
          claimed: ["prepared"],
          blocked: ["blocked"],
          failed: ["failed", "launch-failed", "timed-out", "interrupted"],
          complete: ["finished", "done"]
        }[normalizeStatus(task.operator_status_key)] || [];
        if (!covered.includes(task.latest_run_status)) {
          rows.push(chip(runStatusLabels[task.latest_run_status] || task.latest_run_status, task.latest_run_status));
        }
      }
      if (task.latest_result_status && !(task.latest_result_status === "blocked" && normalizeStatus(task.operator_status_key) === "blocked")) {
        rows.push(chip(resultStatusLabels[task.latest_result_status] || task.latest_result_status, task.latest_result_status));
      }
      if (task.priority) {
        rows.push(chip(task.priority, "subtle"));
      }
      return rows.join("");
    }

    function renderTaskLinks(task) {
      const links = Array.isArray(task.links) ? task.links : [];
      return links.map((row) => artifactLink(row.label, row.href)).join("");
    }

    function taskCard(task) {
      return `
        <article class="task-card" id="${escapeHtml(task.id)}" data-task-id="${escapeHtml(task.id)}">
          <div class="task-bar">
            <span class="task-code">${escapeHtml(task.id)}</span>
            <div class="chips">${chip(task.operator_status || "Ready", task.operator_status_key)}</div>
          </div>
          <h3 class="task-title">${escapeHtml(task.title)}</h3>
          <div class="task-meta">
            <span>Wave ${escapeHtml(task.wave ?? "unplanned")}</span>
            <span>${escapeHtml(task.epic_title || "No epic")}</span>
            ${task.claimed_by ? `<span>${escapeHtml(task.claimed_by)}</span>` : ""}
          </div>
          <p class="task-summary">${escapeHtml(taskSummary(task))}</p>
          <div class="chips">${secondaryChips(task)}</div>
          <div class="artifact-row">${renderTaskLinks(task)}</div>
        </article>
      `;
    }

    function renderBoard() {
      const visibleTasks = allTasks.filter(taskMatches);
      const lanes = laneRows(visibleTasks);
      document.getElementById("filter-summary").textContent =
        filterState.status === "total" ? "Filter: all tasks" : `Filter: ${statusMeta[filterState.status]?.label || filterState.status}`;
      document.getElementById("board-summary").textContent =
        `${visibleTasks.length} visible task(s) across ${lanes.length} lane(s)`;
      document.getElementById("lane-board").innerHTML = lanes.length
        ? lanes.map((lane) => {
            const counts = countStatuses(lane.tasks);
            const laneChips = ["running", "claimed", "waiting", "blocked", "failed", "complete", "ready"]
              .filter((key) => counts[key])
              .map((key) => chip(`${statusMeta[key].label} ${counts[key]}`, key))
              .join("");
            return `
              <section class="lane-column">
                <div class="lane-top">
                  <div>
                    <span class="eyebrow">Wave ${escapeHtml(lane.wave ?? "unplanned")}</span>
                    <h3>${escapeHtml(lane.title)}</h3>
                  </div>
                  <span class="lane-count">${lane.tasks.length} task(s)</span>
                </div>
                <div class="lane-summary">${laneChips}</div>
                <div class="lane-stack">${lane.tasks.map(taskCard).join("")}</div>
              </section>
            `;
          }).join("")
        : `<div class="empty">No tasks match the current status/search filter.</div>`;
    }

    function renderRecentResults() {
      document.getElementById("recent-results").innerHTML = recentResults.length
        ? recentResults.slice(0, 8).map((row) => `
            <article class="result-card" data-result-task="${escapeHtml(row.task_id || "")}">
              <div class="result-top">
                <strong>${escapeHtml(row.task_id || "?")}</strong>
                <div class="chips">${chip(resultStatusLabels[row.status] || row.status, row.status)}</div>
              </div>
              <div class="result-meta">
                <span>${escapeHtml(row.actor || "")}</span>
                <span>${escapeHtml(relativeTime(row.recorded_at))}</span>
              </div>
              <p>${escapeHtml(row.preview || "")}</p>
              <div class="artifact-row">
                ${artifactLink("Result", row.result_href)}
                ${row.task_id ? `<a class="artifact-link" href="#${escapeHtml(row.task_id)}">Task</a>` : ""}
              </div>
            </article>
          `).join("")
        : `<div class="empty">No task results recorded yet.</div>`;
    }

    function detailBlock(label, content, options = {}) {
      if (!content) {
        return "";
      }
      const className = options.wide ? "detail-block wide" : "detail-block";
      return `
        <section class="${className}">
          <strong>${escapeHtml(label)}</strong>
          ${content}
        </section>
      `;
    }

    function listBlock(items) {
      if (!Array.isArray(items) || !items.length) {
        return "";
      }
      return `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
    }

    function paragraphBlock(text) {
      if (!text) {
        return "";
      }
      return `<p>${escapeHtml(text)}</p>`;
    }

    function formatActivityTimestamp(value) {
      if (!value) {
        return "";
      }
      const parsed = Date.parse(String(value));
      if (Number.isNaN(parsed)) {
        return String(value);
      }
      const row = new Date(parsed);
      const pad = (item) => String(item).padStart(2, "0");
      return `${row.getFullYear()}-${pad(row.getMonth() + 1)}-${pad(row.getDate())} ${pad(row.getHours())}:${pad(row.getMinutes())}:${pad(row.getSeconds())}`;
    }

    function detailList(entries) {
      const rows = entries.filter(Boolean);
      if (!rows.length) {
        return "";
      }
      return `<ul>${rows.map((row) => `<li>${escapeHtml(row)}</li>`).join("")}</ul>`;
    }

    function activityList(entries) {
      const rows = entries.filter((row) => row && (row.at || row.actor || row.message));
      if (!rows.length) {
        return "";
      }
      return `
        <div class="activity-list">
          ${rows.map((row) => `
            <div class="activity-row">
              <span class="mono">${escapeHtml(formatActivityTimestamp(row.at))}</span>
              <span class="mono">${escapeHtml(row.actor || "")}</span>
              <span>${escapeHtml(row.message || "")}</span>
            </div>
          `).join("")}
        </div>
      `;
    }

    function openTaskReader(taskId) {
      const task = allTasks.find((row) => row.id === taskId);
      if (!task) {
        return;
      }
      document.getElementById("reader-eyebrow").textContent =
        `Wave ${task.wave ?? "unplanned"} · ${task.lane_title || "No lane"} · ${task.epic_title || "No epic"}`;
      document.getElementById("reader-title").textContent = `${task.id} ${task.title}`;
      document.getElementById("reader-links").innerHTML = renderTaskLinks(task);

      const activityRows = Array.isArray(task.activity) ? task.activity : [];

      const runtimeRows = [
        `Operator status: ${task.operator_status}${task.operator_status_detail ? ` · ${task.operator_status_detail}` : ""}`,
        task.child_agent ? `Child agent: ${task.child_agent}` : "",
        task.target_branch ? `Branch path: ${task.task_branch || "task"} -> ${task.target_branch}` : "",
        task.workspace_mode ? `Workspace mode: ${task.workspace_mode}` : "",
        task.total_compute_label ? `Total compute: ${task.total_compute_label}` : ""
      ];

      document.getElementById("reader-grid").innerHTML = [
        detailBlock("Summary", paragraphBlock(taskSummary(task))),
        detailBlock("Activity", activityList(activityRows), { wide: true }),
        detailBlock("Safe First Slice", paragraphBlock(task.safe_first_slice)),
        detailBlock("Runtime", detailList(runtimeRows)),
        detailBlock("Why", paragraphBlock(task.why)),
        detailBlock("Evidence", paragraphBlock(task.evidence)),
        detailBlock("Paths", listBlock(task.paths)),
        detailBlock("Checks", listBlock(task.checks)),
        detailBlock("Docs", listBlock(task.docs)),
        detailBlock("Latest Result Changes", listBlock(task.latest_result_what_changed), { wide: true }),
        detailBlock("Latest Result Validation", listBlock(task.latest_result_validation), { wide: true }),
        detailBlock("Latest Result Residual", listBlock(task.latest_result_residual), { wide: true })
      ].filter(Boolean).join("");
      document.getElementById("reader-dialog").showModal();
    }

    function openMessageReader(messageId) {
      const message = openMessages.find((row) => row.message_id === messageId);
      if (!message) {
        return;
      }
      document.getElementById("reader-eyebrow").textContent = "Inbox message";
      document.getElementById("reader-title").textContent = `${message.sender || "unknown"} -> ${message.recipient || "unknown"}`;
      document.getElementById("reader-links").innerHTML = [
        artifactLink("Inbox JSON", snapshot.links?.inbox)
      ].join("");
      document.getElementById("reader-grid").innerHTML = [
        detailBlock("Body", paragraphBlock(message.body)),
        detailBlock("Routing", detailList([
          message.kind ? `Kind: ${message.kind}` : "",
          message.task_id ? `Task: ${message.task_id}` : "",
          message.reply_to ? `Reply to: ${message.reply_to}` : "",
          message.at ? `Sent: ${formatTimestamp(message.at)}` : ""
        ])),
        detailBlock("Tags", listBlock(message.tags))
      ].filter(Boolean).join("");
      document.getElementById("reader-dialog").showModal();
    }

    function wireStaticEvents() {
      document.addEventListener("click", (event) => {
        const stat = event.target.closest("[data-status-filter]");
        if (stat) {
          const key = stat.getAttribute("data-status-filter") || "total";
          filterState.status = filterState.status === key ? "total" : key;
          renderStats();
          renderBoard();
          return;
        }

        const taskCard = event.target.closest("[data-task-id]");
        if (taskCard && !event.target.closest("a, button")) {
          openTaskReader(taskCard.getAttribute("data-task-id"));
          return;
        }

        const messageCard = event.target.closest("[data-message-id]");
        if (messageCard && !event.target.closest("a, button")) {
          openMessageReader(messageCard.getAttribute("data-message-id"));
          return;
        }

        const resultCard = event.target.closest("[data-result-task]");
        if (resultCard && !event.target.closest("a, button")) {
          openTaskReader(resultCard.getAttribute("data-result-task"));
        }
      });

      const search = document.getElementById("task-search");
      search.addEventListener("input", (event) => {
        filterState.search = String(event.target.value || "").trim().toLowerCase();
        renderBoard();
      });
    }

    renderHeader();
    renderStats();
    renderInbox();
    renderBoard();
    renderRecentResults();
    wireStaticEvents();
    window.setInterval(renderHeader, 30000);
  </script>
</body>
</html>
"""
    html = template.replace("__BLACKDOG_TITLE__", title).replace("__BLACKDOG_SNAPSHOT__", _snapshot_json(snapshot))
    output_path.write_text(html, encoding="utf-8")
