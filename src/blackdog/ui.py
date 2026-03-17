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


UI_SNAPSHOT_SCHEMA_VERSION = 4
PROGRESS_STATUS_KEYS = ("running", "claimed", "ready", "waiting", "blocked", "failed", "complete")


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


def _text_label(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _count_label(count: int, singular: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def _short_commit(value: Any, *, length: int = 12) -> str | None:
    text = _text_label(value)
    if text is None:
        return None
    return text[:length]


def _latest_task_with_timestamp(
    tasks: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> tuple[dict[str, Any] | None, str | None]:
    latest_task: dict[str, Any] | None = None
    latest_field: str | None = None
    latest_value = ""
    for task in tasks:
        for field in fields:
            value = _text_label(task.get(field))
            if not value:
                continue
            if value > latest_value:
                latest_task = task
                latest_field = field
                latest_value = value
            break
    return latest_task, latest_field


def _branch_summary(contract: dict[str, Any], tasks: list[dict[str, Any]]) -> str | None:
    latest_task, _ = _latest_task_with_timestamp(tasks, ("latest_run_at", "latest_result_at", "completed_at", "claimed_at"))
    branch = (
        _text_label((latest_task or {}).get("task_branch"))
        or _text_label(contract.get("current_branch"))
        or _text_label(contract.get("target_branch"))
    )
    target = _text_label((latest_task or {}).get("target_branch")) or _text_label(contract.get("target_branch"))
    if branch and target and branch != target:
        return f"{branch} -> {target}"
    return branch or target


def _latest_run_summary(tasks: list[dict[str, Any]]) -> str:
    latest_task, source = _latest_task_with_timestamp(
        tasks,
        ("latest_run_at", "latest_result_at", "completed_at", "claimed_at"),
    )
    if latest_task is None or source is None:
        return "No recorded work yet"

    task_id = _text_label(latest_task.get("id")) or "Unknown task"
    actor = _text_label(latest_task.get("child_agent")) or _text_label(latest_task.get("claimed_by"))
    elapsed = _text_label(latest_task.get("run_elapsed_label"))
    if source == "latest_run_at":
        parts = [
            task_id,
            _title_label(latest_task.get("latest_run_status") or "running"),
            actor,
            elapsed,
        ]
        return " · ".join(part for part in parts if part)
    if source == "latest_result_at":
        parts = [
            task_id,
            f"result {_title_label(latest_task.get('latest_result_status') or 'recorded').lower()}",
            actor,
        ]
        return " · ".join(part for part in parts if part)
    if source == "completed_at":
        return " · ".join(part for part in (task_id, "completed", actor) if part)
    return " · ".join(part for part in (task_id, "claimed", actor) if part)


def _time_on_task_summary(tasks: list[dict[str, Any]]) -> str:
    active_tasks = [
        task
        for task in tasks
        if str(task.get("operator_status_key") or "").strip().lower() in {"running", "claimed"}
    ]
    touched_tasks = [
        task
        for task in tasks
        if int(task.get("total_compute_seconds") or 0) > 0
        or task.get("claimed_at")
        or task.get("completed_at")
        or task.get("released_at")
    ]
    if active_tasks:
        active_seconds = sum(int(task.get("active_compute_seconds") or 0) for task in active_tasks)
        total_seconds = sum(int(task.get("total_compute_seconds") or 0) for task in touched_tasks)
        parts = [
            _count_label(len(active_tasks), "active task"),
            f"{_format_duration(active_seconds) or '0s'} live",
            f"{_format_duration(total_seconds) or '0s'} total",
        ]
        if touched_tasks:
            parts.append(f"across {_count_label(len(touched_tasks), 'task')}")
        return " · ".join(parts)
    if touched_tasks:
        total_seconds = sum(int(task.get("total_compute_seconds") or 0) for task in touched_tasks)
        return f"{_count_label(len(touched_tasks), 'task')} touched · {_format_duration(total_seconds) or '0s'} recorded"
    return "No claimed work recorded"


def _build_hero_highlights(
    *,
    contract: dict[str, Any],
    headers: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> dict[str, str]:
    return {
        "branch": _branch_summary(contract, tasks) or "",
        "commit": _short_commit(headers.get("Target commit")) or "",
        "latest_run": _latest_run_summary(tasks),
        "time_on_task": _time_on_task_summary(tasks),
    }


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
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_chip(label: str, key: str) -> None:
        normalized = str(key or "").strip().lower() or str(label or "").strip().lower()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        rows.append({"label": label, "key": key})

    current = str(task_row.get("operator_status_key") or "ready").strip().lower()
    task_status = str(task_row.get("status") or "").strip().lower()
    if current == "running" and (task_status == "claimed" or task_row.get("claimed_by")):
        add_chip("Claimed", "claimed")
    add_chip(str(task_row.get("operator_status") or "Ready"), str(task_row.get("operator_status_key") or "ready"))
    if current != "complete":
        covered = {
            "running": {"running", "claimed"},
            "claimed": {"prepared"},
            "blocked": {"blocked"},
            "failed": {"failed", "launch-failed", "timed-out", "interrupted"},
            "complete": {"finished", "done"},
        }.get(current, set())
        run_status = str(task_row.get("latest_run_status") or "").strip()
        if run_status and run_status not in covered:
            add_chip(_title_label(run_status), run_status)
        result_status = str(task_row.get("latest_result_status") or "").strip()
        if result_status and not (result_status == "blocked" and current == "blocked"):
            add_chip(_title_label(result_status), result_status)
    priority = str(task_row.get("priority") or "").strip()
    if priority:
        add_chip(priority, "subtle")
    return rows


def _card_status_chips(task_row: dict[str, Any]) -> list[dict[str, str]]:
    current = str(task_row.get("operator_status_key") or "ready").strip().lower()
    task_status = str(task_row.get("status") or "").strip().lower()
    rows: list[dict[str, str]] = []
    if current == "running" and (task_status == "claimed" or task_row.get("claimed_by")):
        rows.append({"label": "Claimed", "key": "claimed"})
    rows.append(
        {
            "label": str(task_row.get("operator_status") or "Ready"),
            "key": str(task_row.get("operator_status_key") or "ready"),
        }
    )
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


def _progress_for_task_rows(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {key: 0 for key in PROGRESS_STATUS_KEYS}
    for task in tasks:
        status_key = str(task.get("operator_status_key") or "ready").strip().lower() or "ready"
        if status_key not in counts:
            status_key = "ready"
        counts[status_key] += 1
    total = len(tasks)
    complete = counts["complete"]
    remaining = max(0, total - complete)
    percent = round((complete / total) * 100) if total else 0
    return {
        "counts": counts,
        "total": total,
        "complete": complete,
        "remaining": remaining,
        "percent": percent,
    }


def _build_objective_snapshot_rows(
    tasks: list[dict[str, Any]],
    base_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tasks_by_id = {str(task["id"]): task for task in tasks}
    objective_rows: list[dict[str, Any]] = []
    for row in base_rows:
        task_ids = [str(task_id) for task_id in row.get("task_ids", []) if str(task_id) in tasks_by_id]
        objective_tasks = [tasks_by_id[task_id] for task_id in task_ids]
        if not objective_tasks:
            continue
        progress = _progress_for_task_rows(objective_tasks)
        objective_rows.append(
            {
                "key": row.get("key"),
                "id": row.get("id"),
                "title": row.get("title"),
                "task_ids": task_ids,
                "active_task_ids": [
                    str(task["id"]) for task in objective_tasks if str(task.get("operator_status_key") or "") != "complete"
                ],
                "lane_ids": list(row.get("lane_ids") or []),
                "lane_titles": list(row.get("lane_titles") or []),
                "wave_ids": list(row.get("wave_ids") or []),
                "total": progress["total"],
                "done": progress["complete"],
                "remaining": progress["remaining"],
                "progress": progress,
            }
        )
    return objective_rows


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
    objective_titles = {
        str(row.get("id") or ""): str(row.get("title") or "")
        for row in summary.get("objective_rows", [])
        if str(row.get("id") or "").strip()
    }
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
            "objective_title": objective_titles.get(str(task.payload.get("objective") or "").strip())
            or str(task.payload.get("objective") or "").strip()
            or "Unassigned",
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
        task_row["card_status_chips"] = _card_status_chips(task_row)
        task_row["dialog_status_chips"] = _dialog_status_chips(task_row)
        task_row["links"] = _task_links(task_row)
        tasks.append(task_row)
        for predecessor_id in task.predecessor_ids:
            graph_edges.append({"from": predecessor_id, "to": task.id})

    objective_rows = _build_objective_snapshot_rows(tasks, list(summary.get("objective_rows") or []))
    focus_task_ids = {
        str(task_id)
        for row in objective_rows
        for task_id in row.get("task_ids", [])
    }
    focus_tasks = [task for task in tasks if str(task.get("id") or "") in focus_task_ids] or tasks
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
    board_tasks = [row for row in tasks if row.get("lane_id") or row.get("objective")]
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
    workspace_contract = worktree_contract(profile)
    headers = dict(snapshot.headers)

    return {
        "schema_version": UI_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "control_dir": str(profile.paths.control_dir),
        "profile_file": str(profile.paths.profile_file),
        "workspace_contract": workspace_contract,
        "headers": headers,
        "hero_highlights": _build_hero_highlights(contract=workspace_contract, headers=headers, tasks=focus_tasks),
        "last_activity": _latest_activity(events),
        "counts": summary["counts"],
        "total": summary["total"],
        "push_objective": summary["push_objective"],
        "objectives": summary["objectives"],
        "objective_rows": objective_rows,
        "focus_task_ids": sorted(focus_task_ids),
        "next_rows": summary["next_rows"],
        "release_gates": summary.get("release_gates", []),
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
            "state": _artifact_href(profile.paths, profile.paths.state_file, must_exist=True),
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
      <section id="hero-panel" class="hero" data-panel="hero">
        <article class="panel hero-main">
          <h1>Backlog Control</h1>
          <p id="hero-copy" class="subhead"></p>
          <div id="hero-meta-line" class="meta"></div>
          <div id="hero-progress" class="progress-slot"></div>
          <div id="hero-links" class="nav"></div>
        </article>
        <aside class="panel hero-side">
          <h2>Queue Health</h2>
          <div id="queue-stats" class="stats"></div>
        </aside>
      </section>

      <section id="objectives-panel" class="objectives" data-panel="objectives">
        <div id="objective-cards" class="objectives-grid"></div>
      </section>

      <section id="overview-panel" class="overview" data-panel="overview">
        <div id="overview-cards" class="overview-grid"></div>
      </section>

      <section id="domains-panel" class="panel domains-panel" data-panel="domains">
        <h2>Domains</h2>
        <div id="domain-chips" class="chips domain-chips"></div>
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
    const allTasksById = new Map(allTasks.map((task) => [String(task.id), task]));
    const objectiveRows = Array.isArray(snapshot.objective_rows) ? snapshot.objective_rows.slice() : [];
    const focusTaskIds = new Set(
      Array.isArray(snapshot.focus_task_ids) ? snapshot.focus_task_ids.map((taskId) => String(taskId)) : []
    );
    const focusTasks = focusTaskIds.size
      ? allTasks.filter((task) => focusTaskIds.has(String(task.id)))
      : allTasks.filter((task) => (task.objective || task.lane_id) && normalizeStatus(task.operator_status_key) !== "complete");
    const openMessages = Array.isArray(snapshot.open_messages) ? snapshot.open_messages.slice() : [];
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

    function textLink(label, href) {
      if (!href) {
        return "";
      }
      return `<a class="text-link" href="${escapeHtml(href)}">${escapeHtml(label)}</a>`;
    }

    function interactiveCardAttributes(taskId) {
      if (!taskId) {
        return "";
      }
      return ` data-task-id="${escapeHtml(taskId)}" role="button" tabindex="0"`;
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
        ["Backlog Source", links.backlog],
        ["State", links.state]
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

    function progressMetrics(tasks) {
      const counts = countStatuses(tasks);
      const total = Number(counts.total || 0);
      const complete = Number(counts.complete || 0);
      const remaining = Math.max(0, total - complete);
      const percent = total ? Math.max(0, Math.min(100, Math.round((complete / total) * 100))) : 0;
      return { counts, total, complete, remaining, percent };
    }

    function progressLabel(progress) {
      if (!progress.total) {
        return "No tasks yet";
      }
      return `${progress.complete} of ${progress.total} complete`;
    }

    function progressDetail(progress) {
      if (!progress.total) {
        return "Backlog is empty.";
      }
      if (!progress.remaining) {
        return "All tracked work is complete.";
      }
      const details = [];
      if (progress.counts.running) {
        details.push(`${progress.counts.running} running`);
      }
      if (progress.counts.claimed) {
        details.push(`${progress.counts.claimed} claimed`);
      }
      if (progress.counts.ready) {
        details.push(`${progress.counts.ready} ready`);
      }
      if (progress.counts.waiting) {
        details.push(`${progress.counts.waiting} waiting`);
      }
      if (progress.counts.blocked) {
        details.push(`${progress.counts.blocked} blocked`);
      }
      if (progress.counts.failed) {
        details.push(`${progress.counts.failed} failed`);
      }
      return details.length ? details.join(" · ") : `${progress.remaining} remaining`;
    }

    function renderProgressBar(progress, className = "") {
      const safeClassName = className ? ` ${escapeHtml(className)}` : "";
      return `
        <div class="progress-bar${safeClassName}" aria-hidden="true">
          <span class="progress-fill" data-progress="${escapeHtml(progress.percent)}"></span>
        </div>
      `;
    }

    function applyProgressBars(root = document) {
      root.querySelectorAll(".progress-fill[data-progress]").forEach((node) => {
        const value = Number(node.getAttribute("data-progress") || "0");
        const clamped = Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
        node.style.width = `${clamped}%`;
      });
    }

    function taskSummary(task) {
      return task.latest_result_preview || task.operator_status_detail || task.detail || task.safe_first_slice || "";
    }

    function objectiveTone(objective) {
      const progress = objective.progress || { counts: {}, remaining: 0 };
      if (!progress.remaining) {
        return "complete";
      }
      if (progress.counts.failed || progress.counts.blocked) {
        return "blocked";
      }
      if (progress.counts.running) {
        return "running";
      }
      if (progress.counts.claimed) {
        return "claimed";
      }
      if (progress.counts.waiting) {
        return "waiting";
      }
      return "ready";
    }

    function objectiveStateLabel(objective) {
      const tone = objectiveTone(objective);
      if (tone === "complete") {
        return "Complete";
      }
      return statusMeta[tone]?.label || "Ready";
    }

    function objectiveLeadTask(objective) {
      const orderedTaskIds = [
        ...(Array.isArray(objective.active_task_ids) ? objective.active_task_ids : []),
        ...(Array.isArray(objective.task_ids) ? objective.task_ids : [])
      ];
      for (const taskId of orderedTaskIds) {
        const task = allTasksById.get(String(taskId));
        if (task) {
          return task;
        }
      }
      return null;
    }

    function pluralize(count, noun) {
      return `${count} ${noun}${count === 1 ? "" : "s"}`;
    }

    function renderObjectiveCard(objective) {
      const progress = objective.progress || progressMetrics(
        (Array.isArray(objective.task_ids) ? objective.task_ids : [])
          .map((taskId) => allTasksById.get(String(taskId)))
          .filter(Boolean)
      );
      const leadTask = objectiveLeadTask(objective);
      const leadTaskId = leadTask ? String(leadTask.id || "") : "";
      const copy = leadTask
        ? taskSummary(leadTask) || leadTask.title
        : (progress.remaining ? progressDetail(progress) : "Complete");
      return `
        <article class="objective-card"${interactiveCardAttributes(leadTaskId)} data-objective-id="${escapeHtml(objective.id || objective.key || "objective")}">
          <div class="objective-top">
            <span class="objective-id">${escapeHtml(objective.id || "Unassigned")}</span>
            <span class="objective-meta">${escapeHtml(`${progress.complete}/${progress.total}`)}</span>
          </div>
          <h3>${escapeHtml(objective.title || objective.id || "Unassigned")}</h3>
          <div class="progress-cluster">
            ${renderProgressBar(progress)}
          </div>
          <p class="objective-copy">${escapeHtml(copy)}</p>
        </article>
      `;
    }

    function renderObjectiveCards() {
      document.getElementById("objective-cards").innerHTML = objectiveRows.length
        ? objectiveRows.map(renderObjectiveCard).join("")
        : `<div class="empty">No objective rows tagged in the backlog yet.</div>`;
      applyProgressBars(document.getElementById("objective-cards"));
    }

    function domainCounts(tasks) {
      const counts = new Map();
      for (const task of tasks) {
        const domains = Array.isArray(task.domains) ? task.domains : [];
        for (const rawDomain of domains) {
          const domain = String(rawDomain || "").trim();
          if (!domain) {
            continue;
          }
          counts.set(domain, Number(counts.get(domain) || 0) + 1);
        }
      }
      return Array.from(counts.entries()).sort((left, right) => {
        if (right[1] !== left[1]) {
          return right[1] - left[1];
        }
        return left[0].localeCompare(right[0]);
      });
    }

    function nextFocusRow() {
      const nextRows = Array.isArray(snapshot.next_rows) ? snapshot.next_rows : [];
      if (nextRows.length) {
        return nextRows[0];
      }
      const fallback = focusTasks.find((task) => normalizeStatus(task.operator_status_key) !== "complete");
      if (!fallback) {
        return null;
      }
      return {
        id: fallback.id,
        title: fallback.title,
        lane: fallback.lane_title,
        wave: fallback.wave,
        risk: fallback.risk
      };
    }

    function overviewCard(title, headline, copy, items = [], options = {}) {
      const filteredItems = items.filter(Boolean);
      const taskId = options.taskId ? String(options.taskId) : "";
      return `
        <article class="compact-card"${interactiveCardAttributes(taskId)}>
          <span class="eyebrow">${escapeHtml(title)}</span>
          ${headline ? `<strong>${escapeHtml(headline)}</strong>` : ""}
          ${copy ? `<p>${escapeHtml(copy)}</p>` : ""}
          ${filteredItems.length ? `<ul class="compact-list">${filteredItems.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>` : ""}
        </article>
      `;
    }

    function renderOverviewCards() {
      const currentObjective = objectiveRows.find((row) => Array.isArray(row.active_task_ids) && row.active_task_ids.length) || objectiveRows[0] || null;
      const currentObjectiveProgress = currentObjective?.progress || null;
      const currentObjectiveLeadTask = currentObjective ? objectiveLeadTask(currentObjective) : null;
      const next = nextFocusRow();
      const releaseGates = Array.isArray(snapshot.release_gates) ? snapshot.release_gates.slice(0, 4) : [];

      document.getElementById("overview-cards").innerHTML = [
        overviewCard(
          "What We Are Doing",
          currentObjective ? currentObjective.title || currentObjective.id || "Unassigned" : "No objective rows yet",
          currentObjectiveProgress ? objectiveStateLabel(currentObjective) : "Add objective tags to make the board lead with intent.",
          currentObjective
            ? [
                `${currentObjective.done}/${currentObjective.total} complete`,
                Array.isArray(currentObjective.lane_titles) && currentObjective.lane_titles.length
                  ? pluralize(currentObjective.lane_titles.length, "lane")
                  : "",
                Array.isArray(currentObjective.wave_ids) && currentObjective.wave_ids.length
                  ? pluralize(currentObjective.wave_ids.length, "wave")
                  : ""
              ]
            : [],
          { taskId: currentObjectiveLeadTask?.id }
        ),
        overviewCard(
          "What's Next",
          next ? `${next.id} ${next.title}` : "No runnable backlog task",
          next ? "This is the next claimable slice from the current backlog ordering." : "The tracked objective work is complete.",
          next
            ? [
                next.lane ? `Lane: ${next.lane}` : "",
                next.wave != null ? `Wave ${next.wave}` : "",
                next.risk ? `Risk: ${next.risk}` : ""
              ]
            : [],
          { taskId: next?.id }
        ),
        overviewCard(
          "Release Gates",
          "",
          "",
          releaseGates.length ? releaseGates : ["Add release gates to the backlog header to surface them here."]
        )
      ].join("");
    }

    function renderDomainChips() {
      const domains = domainCounts(focusTasks);
      document.getElementById("domain-chips").innerHTML = domains.length
        ? domains.map(([domain, count]) => `<span class="chip chip-domain">${escapeHtml(domain)} <strong>${escapeHtml(count)}</strong></span>`).join("")
        : `<div class="empty">No domain tags yet.</div>`;
    }

    function renderHeader() {
      const objective = Array.isArray(snapshot.push_objective) ? snapshot.push_objective.join(" ") : "";
      document.getElementById("hero-copy").textContent =
        objective || "Objective-first backlog control for the current push.";
      const heroHighlights = snapshot.hero_highlights || {};
      const headers = snapshot.headers || {};
      const overallProgress = progressMetrics(focusTasks);
      const metaItems = [
        heroHighlights.branch ? `Branch ${heroHighlights.branch}` : headers["Target branch"] ? `Branch ${headers["Target branch"]}` : "",
        heroHighlights.commit ? `Commit ${heroHighlights.commit}` : headers["Target commit"] ? `Commit ${headers["Target commit"]}` : "",
        overallProgress.total ? `${overallProgress.complete}/${overallProgress.total} finished` : "",
        heroHighlights.latest_run ? `Latest run ${heroHighlights.latest_run}` : "",
        heroHighlights.time_on_task ? `Time on task ${heroHighlights.time_on_task}` : ""
      ].filter(Boolean);
      document.getElementById("hero-meta-line").innerHTML = metaItems
        .map((item) => `<span>${escapeHtml(item)}</span>`)
        .join("");
      document.getElementById("hero-progress").innerHTML = renderProgressBar(overallProgress, "progress-hero");
      applyProgressBars(document.getElementById("hero-progress"));
      document.getElementById("hero-links").innerHTML = globalLinks()
        .map(([label, href]) => textLink(label, href))
        .join("");
    }

    function renderQueueHealth() {
      const counts = countStatuses(focusTasks);
      const stats = [
        ["Finished", counts.complete || 0],
        ["Running", (counts.running || 0) + (counts.claimed || 0)],
        ["Next", counts.ready || 0],
        ["Waiting", counts.waiting || 0],
        ["Blocked", (counts.blocked || 0) + (counts.failed || 0)],
      ];
      document.getElementById("queue-stats").innerHTML = stats.map(([label, value]) => `
        <div class="stat-card">
          <span class="eyebrow">${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `).join("");
    }

    function renderTaskLinks(task) {
      const links = Array.isArray(task.links) ? task.links : [];
      return links.map((row) => textLink(row.label, row.href)).join("");
    }

    function renderStatusChipRows(rows) {
      if (!Array.isArray(rows) || !rows.length) {
        return "";
      }
      return rows.map((row) => chip(row.label, row.key)).join("");
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
        textLink("Inbox JSON", snapshot.links?.inbox)
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
        const taskCard = event.target.closest("[data-task-id]");
        if (taskCard && !event.target.closest("a, button")) {
          openTaskReader(taskCard.getAttribute("data-task-id"));
          return;
        }
      });

      document.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") {
          return;
        }
        const taskCard = event.target.closest("[data-task-id]");
        if (!taskCard || event.target.closest("a, button")) {
          return;
        }
        event.preventDefault();
        openTaskReader(taskCard.getAttribute("data-task-id"));
      });
    }

    renderHeader();
    renderQueueHealth();
    renderObjectiveCards();
    renderOverviewCards();
    renderDomainChips();
    wireStaticEvents();
    window.setInterval(() => {
      renderHeader();
      renderQueueHealth();
    }, 30000);
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
