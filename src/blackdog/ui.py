from __future__ import annotations

from datetime import datetime
from importlib import resources
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


def _ui_stylesheet() -> str:
    try:
        return resources.files("blackdog").joinpath("ui.css").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise UIError("Packaged UI stylesheet is missing") from exc


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


def _dialog_status_chips(task_row: dict[str, Any]) -> list[dict[str, str]]:
    rows = [
        {
            "label": str(task_row.get("operator_status") or "Ready"),
            "key": str(task_row.get("operator_status_key") or "ready"),
        }
    ]
    current = str(task_row.get("operator_status_key") or "ready").strip().lower()
    if current != "complete":
        covered = {
            "running": {"running"},
            "claimed": {"prepared"},
            "blocked": {"blocked"},
            "failed": {"failed", "launch-failed", "timed-out", "interrupted"},
            "complete": {"finished", "done"},
        }.get(current, set())
        run_status = str(task_row.get("latest_run_status") or "").strip()
        if run_status and run_status not in covered:
            rows.append({"label": _title_label(run_status), "key": run_status})
        result_status = str(task_row.get("latest_result_status") or "").strip()
        if result_status and not (result_status == "blocked" and current == "blocked"):
            rows.append({"label": _title_label(result_status), "key": result_status})
    priority = str(task_row.get("priority") or "").strip()
    if priority:
        rows.append({"label": priority, "key": "subtle"})
    return rows


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


def _lane_task_positions(plan: dict[str, Any]) -> dict[str, dict[str, int]]:
    positions: dict[str, dict[str, int]] = {}
    for lane_index, lane in enumerate(plan.get("lanes", [])):
        task_ids = [str(item) for item in lane.get("task_ids", [])]
        lane_size = len(task_ids)
        for task_index, task_id in enumerate(task_ids, start=1):
            positions[task_id] = {
                "lane_plan_index": lane_index,
                "lane_position": task_index,
                "lane_task_count": lane_size,
            }
    return positions


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
    lane_positions = _lane_task_positions(snapshot.plan)
    tasks: list[dict[str, Any]] = []
    graph_edges: list[dict[str, str]] = []
    ordered_tasks = sorted(
        snapshot.tasks.values(),
        key=lambda task: (
            task.wave if task.wave is not None else 9999,
            task.lane_order if task.lane_order is not None else 9999,
            task.lane_position if task.lane_position is not None else 9999,
            task.id,
        ),
    )
    for task in ordered_tasks:
        status, detail = classify_task_status(task, snapshot, state, allow_high_risk=False)
        activity = task_activity.get(task.id, _empty_task_activity())
        result_info = task_results.get(task.id, {})
        run_info = task_runs.get(task.id, {})
        lane_info = lane_positions.get(task.id, {})
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
            "lane_plan_index": lane_info.get("lane_plan_index", task.lane_order if task.lane_order is not None else 9999),
            "lane_position": lane_info.get("lane_position"),
            "lane_task_count": lane_info.get("lane_task_count"),
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
        task_row["dialog_status_chips"] = _dialog_status_chips(task_row)
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
    board_tasks = [row for row in tasks if row.get("lane_id")]
    active_tasks = [
        row
        for row in tasks
        if row["status"] == "claimed" or row.get("latest_run_status") in {"prepared", "running", "interrupted"}
    ]
    active_tasks.sort(
        key=lambda row: (
            row.get("wave") if row.get("wave") is not None else 9999,
            row.get("lane_plan_index") if row.get("lane_plan_index") is not None else 9999,
            row.get("lane_position") if row.get("lane_position") is not None else 9999,
            str(row["id"]),
        )
    )

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
        "board_tasks": board_tasks,
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
            {
                "name": "task",
                "meaning": "The executable unit. Claims, results, completion, and dependencies are tracked at task level.",
            },
            {"name": "epic", "meaning": "The thematic why for related tasks. Epics organize reporting, not runnable order."},
            {
                "name": "lane",
                "meaning": "A temporary ordered slot in the active execution map. Lanes are not executable state objects; they hold task order top to bottom.",
            },
            {
                "name": "wave",
                "meaning": "A temporary concurrency bucket for lanes. Waves are reused and compacted between runs; they are scheduler gates, not historical identities.",
            },
        ],
    }


def _snapshot_json(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True).replace("</", "<\\/")


def render_static_html(snapshot: dict[str, Any], output_path: Path) -> None:
    title = html_lib.escape(str(snapshot["project_name"]))
    stylesheet = _ui_stylesheet()
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__BLACKDOG_TITLE__ Backlog</title>
  <style>
__BLACKDOG_STYLES__
  </style>
</head>
<body>
  <script id="blackdog-snapshot" type="application/json">__BLACKDOG_SNAPSHOT__</script>
  <div class="page">
    <div class="page-shell">
      <article class="panel panel-hero">
        <div class="hero-head">
          <div class="hero-title-block">
            <span class="eyebrow">Blackdog Backlog</span>
            <h1 id="project-name">Backlog</h1>
            <p id="hero-copy" class="hero-copy"></p>
          </div>
          <div class="hero-render-block">
            <span class="eyebrow">Last Rendered</span>
            <p id="last-rendered" class="last-rendered"></p>
            <p id="render-note" class="hero-activity"></p>
          </div>
        </div>
        <div class="hero-meta-grid">
          <section class="hero-subpanel">
            <span class="eyebrow">Workspace</span>
            <div id="hero-meta" class="tag-row"></div>
          </section>
          <section class="hero-subpanel">
            <span class="eyebrow">Board Snapshot</span>
            <div id="hero-summary" class="meta-list"></div>
          </section>
          <section class="hero-subpanel">
            <span class="eyebrow">Artifacts</span>
            <div id="global-links" class="link-row"></div>
          </section>
        </div>
      </article>

      <section class="panel board-panel" data-legacy-title="Execution Map">
        <div class="section-head backlog-head">
          <div>
            <span class="eyebrow">Active Work</span>
            <h2>Backlog</h2>
            <p id="board-guide" class="section-copy"></p>
          </div>
          <div class="section-toolbar">
            <div class="toolbar-topline">
              <span id="board-summary" class="section-meta"></span>
              <a id="inbox-link" class="link-pill" href="#" target="_blank" rel="noreferrer">Inbox JSON</a>
            </div>
            <input id="task-search" class="search" type="search" placeholder="Search task id, title, lane, epic, or artifact status">
            <span id="filter-summary" class="search-hint"></span>
            <div id="stats" class="stats"></div>
          </div>
        </div>
        <div id="lane-board" class="lane-board"></div>
      </section>

      <section class="panel result-panel">
        <div class="section-head">
          <div>
            <span class="eyebrow">Completed Tasks</span>
            <h2>Completed Tasks</h2>
            <p class="section-copy">Completed work stays visible here with its latest recorded outcome and artifact links in a scrollable recent-history view.</p>
          </div>
          <span id="history-summary" class="section-meta"></span>
        </div>
        <div class="result-history">
          <div id="recent-results" class="results-grid"></div>
        </div>
      </section>
    </div>
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
      <div id="reader-statuses" class="reader-statuses"></div>
      <div id="reader-links" class="reader-links"></div>
      <div id="reader-grid" class="detail-grid"></div>
    </article>
  </dialog>

  <script>
    const snapshot = JSON.parse(document.getElementById("blackdog-snapshot").textContent);
    const allTasks = Array.isArray(snapshot.tasks) ? snapshot.tasks.slice() : [];
    const boardTasks = Array.isArray(snapshot.board_tasks)
      ? snapshot.board_tasks.filter((task) => normalizeStatus(task.operator_status_key) !== "complete")
      : allTasks.filter((task) => task.lane_id && normalizeStatus(task.operator_status_key) !== "complete");
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
    const resultStatusLabels = {
      success: "Success",
      partial: "Partial",
      blocked: "Blocked"
    };
    const COMPLETED_HISTORY_LIMIT = 30;

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
      lanePlan.forEach((lane, index) => {
        rows.set(String(lane.id), {
          id: String(lane.id),
          title: lane.title || "Unplanned",
          wave: lane.wave,
          plan_index: index,
          tasks: []
        });
      });
      for (const task of tasks) {
        const key = String(task.lane_id || `lane:${task.id}`);
        if (!rows.has(key)) {
          rows.set(key, {
            id: key,
            title: task.lane_title || "Unplanned",
            wave: task.wave,
            plan_index: Number(task.lane_plan_index ?? 9999),
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
            const leftPosition = left.lane_position == null ? 9999 : Number(left.lane_position);
            const rightPosition = right.lane_position == null ? 9999 : Number(right.lane_position);
            if (leftPosition !== rightPosition) {
              return leftPosition - rightPosition;
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
          const leftPlan = left.plan_index == null ? 9999 : Number(left.plan_index);
          const rightPlan = right.plan_index == null ? 9999 : Number(right.plan_index);
          if (leftPlan !== rightPlan) {
            return leftPlan - rightPlan;
          }
          return String(left.title).localeCompare(String(right.title));
        });
    }

    function waveRows(lanes) {
      const rows = new Map();
      for (const lane of lanes) {
        const key = lane.wave == null ? "unplanned" : String(lane.wave);
        if (!rows.has(key)) {
          rows.set(key, {
            key,
            wave: lane.wave,
            lanes: []
          });
        }
        rows.get(key).lanes.push(lane);
      }
      return Array.from(rows.values()).sort((left, right) => {
        const leftWave = left.wave == null ? 9999 : Number(left.wave);
        const rightWave = right.wave == null ? 9999 : Number(right.wave);
        return leftWave - rightWave;
      });
    }

    function heroSummary(activity) {
      const latestSummary = [activity.task_id, activity.summary || activity.type_label].filter(Boolean).join(" · ");
      const rows = [
        ["Backlog", `${boardTasks.length} active task(s)`],
        ["Completed", `${completedTasks().length} task(s)`],
        ["Inbox", `${openMessages.length} open message(s)`],
        latestSummary ? ["Latest Event", latestSummary] : null
      ].filter(Boolean);
      return rows.map(([label, value]) => `
        <div class="meta-item">
          <span class="eyebrow">${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `).join("");
    }

    function renderHeader() {
      document.getElementById("project-name").textContent = snapshot.project_name || "Blackdog";
      const objective = Array.isArray(snapshot.push_objective) ? snapshot.push_objective.join(" ") : "";
      document.getElementById("hero-copy").textContent =
        objective || "Static backlog board with lane-ordered task stacks, task detail dialogs, and direct artifact links.";
      const activity = snapshot.last_activity || {};
      const actor = activity.actor ? ` by ${activity.actor}` : "";
      const source = activity.type_label ? ` via ${activity.type_label.toLowerCase()}` : "";
      const renderedAt = snapshot.generated_at || activity.at || "";
      const lastRendered = document.getElementById("last-rendered");
      lastRendered.textContent = renderedAt ? relativeTime(renderedAt) : "just now";
      lastRendered.title = formatTimestamp(renderedAt);
      document.getElementById("render-note").textContent = activity.at
        ? `Latest activity ${relativeTime(activity.at)}${actor}${source}`
        : formatTimestamp(renderedAt);

      const contract = snapshot.workspace_contract || {};
      document.getElementById("hero-meta").innerHTML = [
        contract.target_branch ? `<span class="pill">Target ${escapeHtml(contract.target_branch)}</span>` : "",
        contract.workspace_mode ? `<span class="pill">Mode ${escapeHtml(contract.workspace_mode)}</span>` : "",
        contract.primary_dirty === false ? `<span class="pill">Primary clean</span>` : `<span class="pill">Primary dirty</span>`,
        contract.workspace_has_local_blackdog ? `<span class="pill">Local .VE ready</span>` : `<span class="pill">Bootstrap .VE here</span>`
      ].filter(Boolean).join("");
      document.getElementById("hero-summary").innerHTML = heroSummary(activity);

      document.getElementById("global-links").innerHTML = globalLinks()
        .map(([label, href]) => href ? `<a class="link-pill" href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>` : "")
        .join("");

      const inboxHref = snapshot.links?.inbox || "#";
      const inboxLink = document.getElementById("inbox-link");
      inboxLink.href = inboxHref;
      inboxLink.style.visibility = inboxHref ? "visible" : "hidden";
      inboxLink.textContent = `Inbox JSON · ${openMessages.length} open`;

      document.getElementById("board-guide").textContent =
        "Search and status filters apply only to the active backlog. Waves open concurrent lane groups, and completed tasks move into the history panel.";
    }

    function renderStats() {
      const counts = countStatuses(boardTasks);
      const order = ["total", "ready", "running", "claimed", "waiting", "blocked", "failed"];
      document.getElementById("stats").innerHTML = order.map((key) => `
        <button class="stat-card ${filterState.status === key ? "active" : ""}" type="button" data-status-filter="${escapeHtml(key)}">
          <span class="eyebrow">${escapeHtml(statusMeta[key].label)}</span>
          <strong>${escapeHtml(counts[key] || 0)}</strong>
        </button>
      `).join("");
    }

    function renderTaskLinks(task) {
      const links = Array.isArray(task.links) ? task.links : [];
      return links.map((row) => artifactLink(row.label, row.href)).join("");
    }

    function groupStatusChips(tasks) {
      const keys = [];
      const seen = new Set();
      for (const key of ["running", "claimed", "blocked", "failed", "waiting", "ready", "complete"]) {
        if (tasks.some((task) => normalizeStatus(task.operator_status_key) === key) && !seen.has(key)) {
          seen.add(key);
          keys.push(key);
        }
      }
      return keys.map((key) => chip(statusMeta[key].label, key)).join("");
    }

    function taskSequence(task) {
      if (task.lane_position && task.lane_task_count) {
        return `Step ${task.lane_position} of ${task.lane_task_count} in ${task.lane_title || "lane"}`;
      }
      if (task.lane_title) {
        return `Single task in ${task.lane_title}`;
      }
      return "Unplanned task";
    }

    function dependencyLabel(task) {
      return Array.isArray(task.predecessor_ids) && task.predecessor_ids.length
        ? `After ${task.predecessor_ids.join(", ")}`
        : "Lane opener";
    }

    function renderStatusChipRows(rows) {
      if (!Array.isArray(rows) || !rows.length) {
        return "";
      }
      return rows.map((row) => chip(row.label, row.key)).join("");
    }

    function taskCard(task) {
      const tone = normalizeStatus(task.operator_status_key || "ready");
      const showOwner = ["claimed", "running"].includes(tone) && task.claimed_by;
      return `
        <article class="task-card tone-${escapeHtml(tone)}" id="${escapeHtml(task.id)}" data-task-id="${escapeHtml(task.id)}">
          <div class="task-card-top">
            <div class="task-id-group">
              <span class="task-code">${escapeHtml(task.id)}</span>
              ${task.priority ? `<span class="mini-chip">${escapeHtml(task.priority)}</span>` : ""}
            </div>
            <span class="status-pill tone-${escapeHtml(tone)}">${escapeHtml(task.operator_status || "Ready")}</span>
          </div>
          <h3 class="task-title">${escapeHtml(task.title)}</h3>
          <p class="task-route">${escapeHtml(taskSequence(task))}</p>
          <div class="task-meta">
            <span>${escapeHtml(task.epic_title || "No epic")}</span>
            ${showOwner ? `<span>Owner ${escapeHtml(task.claimed_by)}</span>` : ""}
          </div>
          <p class="task-summary">${escapeHtml(taskSummary(task))}</p>
          <p class="task-dependency">${escapeHtml(dependencyLabel(task))}</p>
        </article>
      `;
    }

    function renderLaneColumn(lane) {
      return `
        <section class="lane-column">
          <div class="lane-head">
            <h3>${escapeHtml(lane.title)}</h3>
          </div>
          <div class="lane-stack">${lane.tasks.map(taskCard).join("")}</div>
        </section>
      `;
    }

    function renderBoard() {
      const visibleTasks = boardTasks.filter(taskMatches);
      const lanes = laneRows(visibleTasks);
      const waves = waveRows(lanes);
      document.getElementById("filter-summary").textContent =
        filterState.status === "total" ? "Filter: all backlog tasks" : `Filter: ${statusMeta[filterState.status]?.label || filterState.status}`;
      document.getElementById("board-summary").textContent =
        `${visibleTasks.length} visible task(s) across ${lanes.length} lane(s) in ${waves.length} wave(s)`;
      document.getElementById("lane-board").innerHTML = waves.length
        ? waves.map((wave) => {
            return `
              <section class="wave-section">
                <div class="wave-head">
                  <div>
                    <span class="eyebrow">Wave</span>
                    <h3>Wave ${escapeHtml(wave.wave ?? "unplanned")}</h3>
                    <p class="wave-copy">Lanes in this wave can run concurrently. Tasks inside each lane advance top to bottom.</p>
                  </div>
                </div>
                <div class="wave-grid">${wave.lanes.map(renderLaneColumn).join("")}</div>
              </section>
            `;
          }).join("")
        : `<div class="empty">${filterState.status === "total" && !filterState.search ? "Backlog empty." : "No backlog tasks match the current status/search filter."}</div>`;
    }

    function completedTasks() {
      return allTasks
        .filter((task) => normalizeStatus(task.operator_status_key) === "complete")
        .sort((left, right) => {
          const leftTime = Date.parse(String(left.completed_at || left.latest_result_at || left.latest_run_at || ""));
          const rightTime = Date.parse(String(right.completed_at || right.latest_result_at || right.latest_run_at || ""));
          if (!Number.isNaN(leftTime) || !Number.isNaN(rightTime)) {
            return (Number.isNaN(rightTime) ? 0 : rightTime) - (Number.isNaN(leftTime) ? 0 : leftTime);
          }
          return String(left.id).localeCompare(String(right.id));
        });
    }

    function renderRecentResults() {
      const completed = completedTasks();
      const visibleCount = Math.min(completed.length, COMPLETED_HISTORY_LIMIT);
      document.getElementById("history-summary").textContent = completed.length > visibleCount
        ? `Showing latest ${visibleCount} of ${completed.length} completed`
        : `${completed.length} completed`;
      document.getElementById("recent-results").innerHTML = completed.length
        ? completed.slice(0, COMPLETED_HISTORY_LIMIT).map((task) => {
            const statusKey = normalizeStatus(task.latest_result_status || task.operator_status_key || "complete");
            const statusLabel = task.latest_result_status
              ? (resultStatusLabels[task.latest_result_status] || task.latest_result_status)
              : "Complete";
            return `
              <article class="result-card" data-result-task="${escapeHtml(task.id || "")}">
                <div class="result-top">
                  <strong>${escapeHtml(task.id || "?")}</strong>
                  <div class="chips">${chip(statusLabel, statusKey)}</div>
                </div>
                <h3 class="result-title">${escapeHtml(task.title || "")}</h3>
                <div class="result-meta">
                  <span>${escapeHtml(relativeTime(task.completed_at || task.latest_result_at || task.latest_run_at))}</span>
                  ${task.total_compute_label ? `<span>Compute ${escapeHtml(task.total_compute_label)}</span>` : ""}
                </div>
                <p>${escapeHtml(task.latest_result_preview || task.operator_status_detail || task.safe_first_slice || "")}</p>
                <div class="artifact-row">
                  ${artifactLink("Result", task.latest_result_href)}
                  ${artifactLink("Run", task.run_dir_href)}
                  ${task.id ? `<a class="artifact-link" href="#${escapeHtml(task.id)}">Task</a>` : ""}
                </div>
              </article>
            `;
          }).join("")
        : `<div class="empty">No completed tasks recorded yet.</div>`;
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
        `${task.lane_title || "No lane"} · ${task.epic_title || "No epic"}`;
      document.getElementById("reader-title").textContent = `${task.id} ${task.title}`;
      document.getElementById("reader-statuses").innerHTML = renderStatusChipRows(task.dialog_status_chips);
      document.getElementById("reader-links").innerHTML = renderTaskLinks(task);

      const activityRows = Array.isArray(task.activity) ? task.activity : [];
      const sequenceRows = [
        task.lane_position && task.lane_task_count ? `Step ${task.lane_position} of ${task.lane_task_count} in ${task.lane_title || "lane"}` : "",
        Array.isArray(task.predecessor_ids) && task.predecessor_ids.length ? `Depends on ${task.predecessor_ids.join(", ")}` : "Lane opener"
      ];

      const runtimeRows = [
        task.operator_status_detail ? `Current detail: ${task.operator_status_detail}` : "",
        task.child_agent ? `Child agent: ${task.child_agent}` : "",
        task.target_branch ? `Branch path: ${task.task_branch || "task"} -> ${task.target_branch}` : "",
        task.workspace_mode ? `Workspace mode: ${task.workspace_mode}` : "",
        task.total_compute_label ? `Total compute: ${task.total_compute_label}` : ""
      ];

      document.getElementById("reader-grid").innerHTML = [
        detailBlock("Summary", paragraphBlock(taskSummary(task))),
        detailBlock("Activity", activityList(activityRows), { wide: true }),
        detailBlock("Sequence", detailList(sequenceRows)),
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
      document.getElementById("reader-statuses").innerHTML = "";
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
    renderBoard();
    renderRecentResults();
    wireStaticEvents();
    window.setInterval(renderHeader, 30000);
  </script>
</body>
</html>
"""
    html = (
        template.replace("__BLACKDOG_TITLE__", title)
        .replace("__BLACKDOG_STYLES__", stylesheet)
        .replace("__BLACKDOG_SNAPSHOT__", _snapshot_json(snapshot))
    )
    output_path.write_text(html, encoding="utf-8")
