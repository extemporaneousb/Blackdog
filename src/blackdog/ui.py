from __future__ import annotations

from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import quote
import html as html_lib
import json
import os
import re
import subprocess

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


UI_SNAPSHOT_SCHEMA_VERSION = 6
EMBEDDED_RESPONSE_CHAR_LIMIT = 24_000
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


def _run_git_capture(repo_root: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    text = completed.stdout.strip()
    return text or None


def _github_repo_url(repo_root: Path) -> str | None:
    remote_url = _run_git_capture(repo_root, "remote", "get-url", "origin")
    if not remote_url:
        return None
    patterns = (
        r"^(?:https?://)?github\.com/(?P<path>[^?#]+?)(?:\.git)?/?$",
        r"^(?:ssh://)?git@github\.com[:/](?P<path>[^?#]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.match(pattern, remote_url)
        if match is None:
            continue
        repo_path = match.group("path").strip("/")
        if repo_path:
            return f"https://github.com/{repo_path}"
    return None


def _read_artifact_text(path: Path | None, *, char_limit: int = EMBEDDED_RESPONSE_CHAR_LIMIT) -> tuple[str | None, bool]:
    if path is None or not path.is_file():
        return None, False
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None, False
    if not text:
        return None, False
    truncated = len(text) > char_limit
    if truncated:
        text = text[:char_limit].rstrip() + "\n\n[truncated in reader; open Stdout for the full response]"
    return text, truncated


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
            "model_response": None,
            "model_response_truncated": False,
        }
    child_dir = run_dir / task_id
    stdout_path = child_dir / "stdout.log"
    model_response, model_response_truncated = _read_artifact_text(stdout_path)
    return {
        "run_dir_href": _artifact_href(paths, child_dir, must_exist=True),
        "prompt_href": _artifact_href(paths, child_dir / "prompt.txt", must_exist=True),
        "stdout_href": _artifact_href(paths, stdout_path, must_exist=True),
        "stderr_href": _artifact_href(paths, child_dir / "stderr.log", must_exist=True),
        "metadata_href": _artifact_href(paths, child_dir / "metadata.json", must_exist=True),
        "diff_href": _artifact_href(paths, child_dir / "changes.diff", must_exist=True),
        "diffstat_href": _artifact_href(paths, child_dir / "changes.stat.txt", must_exist=True),
        "model_response": model_response,
        "model_response_truncated": model_response_truncated,
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


def _latest_supervisor_check_at(profile: Profile) -> str | None:
    latest_check = None
    latest_parsed = None
    for status_file in profile.paths.supervisor_runs_dir.glob("*/status.json"):
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        raw = None
        if payload.get("last_checked_at"):
            raw = payload.get("last_checked_at")
        elif payload.get("completed_at"):
            raw = payload.get("completed_at")
        elif isinstance(payload.get("steps"), list) and payload["steps"]:
            last_step = payload["steps"][-1]
            if isinstance(last_step, dict):
                raw = last_step.get("at")
        if not raw:
            continue
        parsed = _parse_iso(raw)
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if latest_parsed is None or parsed > latest_parsed:
            latest_check = str(raw)
            latest_parsed = parsed
    return latest_check


def _latest_timestamp(*values: Any) -> str | None:
    latest_value = None
    latest_parsed = None
    for value in values:
        parsed = _parse_iso(value)
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if latest_parsed is None or parsed > latest_parsed:
            latest_parsed = parsed
            latest_value = str(value)
    return latest_value


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
            "latest_result_run_id": row.get("run_id"),
            "latest_result_actor": row.get("actor"),
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
                "latest_result_run_id": None,
                "latest_result_actor": None,
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
    ordered_events = sorted(events, key=lambda row: str(row.get("at") or ""))
    branch_task_ids: dict[str, str] = {}
    for event in ordered_events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        branch = str(payload.get("branch") or "")
        task_id = str(event.get("task_id") or "")
        if branch and task_id:
            branch_task_ids.setdefault(branch, task_id)

    relevant_events = {"worktree_start", "worktree_land", "child_launch", "child_launch_failed", "child_finish"}
    for event in ordered_events:
        event_type = str(event.get("type") or "")
        if event_type not in relevant_events:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        task_id = str(event.get("task_id") or "") or branch_task_ids.get(str(payload.get("branch") or ""), "")
        run_id = str(payload.get("run_id") or "")
        if not task_id:
            continue
        if event_type in {"child_launch", "child_launch_failed", "child_finish"} and not run_id:
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
                "model_response": None,
                "model_response_truncated": False,
                "branch_ahead": None,
                "landed": None,
                "land_error": None,
                "landed_commit": None,
                "landed_commit_short": None,
                "landed_commit_url": None,
                "landed_commit_message": None,
            },
        )
        entry["last_event_at"] = str(event.get("at") or entry["last_event_at"])
        if run_id:
            entry["run_id"] = run_id
        if payload.get("child_agent"):
            entry["child_agent"] = payload.get("child_agent")
        if payload.get("workspace_mode"):
            entry["workspace_mode"] = payload.get("workspace_mode")
        if "branch_ahead" in payload:
            entry["branch_ahead"] = bool(payload.get("branch_ahead"))
        if "landed" in payload:
            entry["landed"] = bool(payload.get("landed"))
        if payload.get("branch"):
            entry["task_branch"] = payload.get("branch")
        if payload.get("target_branch"):
            entry["target_branch"] = payload.get("target_branch")
        if payload.get("primary_worktree"):
            entry["primary_worktree"] = payload.get("primary_worktree")
        if payload.get("land_error"):
            entry["land_error"] = payload.get("land_error")
        if event_type == "worktree_start":
            entry["run_status"] = entry.get("run_status") or "prepared"
        elif event_type == "worktree_land":
            entry["finished_at"] = str(event.get("at") or "")
            if payload.get("landed_commit"):
                entry["landed"] = True
                entry["landed_commit"] = str(payload.get("landed_commit"))
        elif event_type == "child_launch":
            entry["run_status"] = "running"
            entry["pid"] = payload.get("pid")
            entry["started_at"] = str(event.get("at") or "")
        elif event_type == "child_launch_failed":
            entry["run_status"] = "launch-failed"
            entry["finished_at"] = str(event.get("at") or "")
        elif event_type == "child_finish":
            if payload.get("missing_process"):
                entry["run_status"] = "interrupted"
            elif payload.get("land_error"):
                entry["run_status"] = "blocked"
            elif payload.get("exit_code") not in {0, None}:
                entry["run_status"] = "failed"
            else:
                entry["run_status"] = str(payload.get("final_task_status") or "finished")
            entry["finished_at"] = str(event.get("at") or "")
            if payload.get("landed_commit"):
                entry["landed"] = True
            if payload.get("landed_commit"):
                entry["landed_commit"] = str(payload.get("landed_commit"))

        run_dir = _find_run_dir(paths, run_id) if run_id else None
        entry.update(_child_artifacts(paths, run_dir, task_id))

    github_repo_url = _github_repo_url(paths.project_root)
    commit_messages: dict[str, str | None] = {}
    for entry in rows.values():
        if entry.get("run_status") == "running" and not _pid_alive(entry.get("pid")):
            entry["run_status"] = "interrupted"
            entry["finished_at"] = entry.get("finished_at") or entry.get("last_event_at")
        landed_commit = _text_label(entry.get("landed_commit"))
        if landed_commit:
            entry["landed_commit_short"] = _short_commit(landed_commit)
            entry["landed_commit_url"] = f"{github_repo_url}/commit/{landed_commit}" if github_repo_url else None
            if landed_commit not in commit_messages:
                commit_messages[landed_commit] = _run_git_capture(paths.project_root, "show", "-s", "--format=%B", landed_commit)
            entry["landed_commit_message"] = commit_messages[landed_commit]
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

    if run_status in {"failed", "launch-failed", "interrupted"}:
        run_detail = {
            "failed": "Child run failed",
            "launch-failed": "Child launch failed",
            "interrupted": "Child run interrupted",
        }[run_status]
        return {
            "operator_status": "Failed",
            "operator_status_key": "failed",
            "operator_status_detail": run_detail,
        }

    if run_status == "blocked":
        if task_row.get("latest_run_branch_ahead") and not task_row.get("latest_run_landed"):
            return {
                "operator_status": "Failed to land",
                "operator_status_key": "blocked",
                "operator_status_detail": str(task_row.get("latest_run_land_error") or "Landing blocked by the target branch state"),
            }
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


def _landing_status_chip(task_row: dict[str, Any]) -> dict[str, str] | None:
    if task_row.get("latest_run_landed"):
        return {
            "label": "Landed",
            "key": "landed",
            "href": str(task_row.get("landed_commit_url") or ""),
        }
    if task_row.get("latest_run_status") == "blocked" and task_row.get("operator_status_key") != "complete":
        return {
            "label": "Failed to land",
            "key": "failed-to-land",
        }
    return None


def _dialog_status_chips(task_row: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_chip(label: str, key: str, href: str | None = None) -> None:
        normalized = str(key or "").strip().lower() or str(label or "").strip().lower()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        chip: dict[str, Any] = {"label": str(label), "key": str(key)}
        if href:
            chip["href"] = str(href)
        rows.append(chip)

    current = str(task_row.get("operator_status_key") or "ready").strip().lower()
    task_status = str(task_row.get("status") or "").strip().lower()
    if current == "running" and (task_status == "claimed" or task_row.get("claimed_by")):
        add_chip("Claimed", "claimed")
    add_chip(str(task_row.get("operator_status") or "Ready"), str(task_row.get("operator_status_key") or "ready"))
    landing_chip = _landing_status_chip(task_row)
    if landing_chip is not None:
        add_chip(
            str(landing_chip["label"]),
            str(landing_chip["key"]),
            str(landing_chip.get("href") or ""),
        )
    if current != "complete":
        covered = {
            "running": {"running", "claimed"},
            "claimed": {"prepared"},
            "blocked": {"blocked"},
            "failed": {"failed", "launch-failed", "interrupted"},
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
    landing_chip = _landing_status_chip(task_row)
    if landing_chip is not None:
        rows.append({str(k): str(v) for k, v in landing_chip.items() if v is not None})
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
        if payload.get("missing_process"):
            return "run interrupted"
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
        ("Commit", task_row.get("landed_commit_url")),
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


def _last_supervisor_sweep_completed_count(events: list[dict[str, Any]]) -> int:
    last_removed_task_ids: list[str] = []
    for event in sorted(events, key=lambda row: str(row.get("at") or "")):
        if str(event.get("type") or "") != "supervisor_run_sweep":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        removed_task_ids = [
            str(task_id)
            for task_id in payload.get("removed_task_ids", [])
            if str(task_id).strip()
        ]
        last_removed_task_ids = removed_task_ids
    return len(last_removed_task_ids)


def _build_queue_status(task_rows: list[dict[str, Any]]) -> dict[str, int]:
    today = datetime.now().astimezone().date()
    running = 0
    waiting = 0
    blocked = 0
    completed_all_time = 0
    completed_today = 0

    for task in task_rows:
        status_key = str(task.get("operator_status_key") or "").strip().lower()
        if status_key == "running":
            running += 1
        elif status_key == "waiting":
            waiting += 1
        elif status_key in {"blocked", "failed"}:
            blocked += 1
        if str(task.get("status") or "") != "done":
            continue
        completed_all_time += 1
        completed_at = _parse_iso(task.get("completed_at"))
        if completed_at is not None and completed_at.date() == today:
            completed_today += 1

    return {
        "running": running,
        "waiting": waiting,
        "blocked": blocked,
        "last_sweep_completed": 0,
        "completed_today": completed_today,
        "completed_all_time": completed_all_time,
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
            "latest_result_run_id": result_info.get("latest_result_run_id"),
            "latest_result_actor": result_info.get("latest_result_actor"),
            "latest_result_href": result_info.get("latest_result_href") or activity.get("latest_result_href"),
            "latest_result_dir_href": result_info.get("latest_result_dir_href"),
            "latest_result_preview": result_info.get("latest_result_preview"),
            "latest_result_what_changed": result_info.get("latest_result_what_changed") or [],
            "latest_result_validation": result_info.get("latest_result_validation") or [],
            "latest_result_residual": result_info.get("latest_result_residual") or [],
            "latest_result_needs_user_input": bool(result_info.get("latest_result_needs_user_input")),
            "result_count": int(result_info.get("result_count") or 0),
            "latest_run_id": run_info.get("run_id"),
            "latest_run_status": run_info.get("run_status"),
            "latest_run_branch_ahead": run_info.get("branch_ahead"),
            "latest_run_landed": run_info.get("landed"),
            "latest_run_land_error": run_info.get("land_error"),
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
            "model_response": run_info.get("model_response"),
            "model_response_truncated": bool(run_info.get("model_response_truncated")),
            "landed_commit": run_info.get("landed_commit"),
            "landed_commit_short": run_info.get("landed_commit_short"),
            "landed_commit_url": run_info.get("landed_commit_url"),
            "landed_commit_message": run_info.get("landed_commit_message"),
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
    generated_at = now_iso()
    supervisor_last_checked_at = _latest_supervisor_check_at(profile)
    last_checked_at = _latest_timestamp(supervisor_last_checked_at, generated_at) or generated_at

    return {
        "schema_version": UI_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "content_updated_at": generated_at,
        "last_checked_at": last_checked_at,
        "supervisor_last_checked_at": supervisor_last_checked_at,
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
        "queue_status": {
            **_build_queue_status(tasks),
            "last_sweep_completed": _last_supervisor_sweep_completed_count(events),
        },
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
      <section id="top-band" class="panel-row panel-row-top" data-panel="hero">
        <article id="hero-panel" class="panel control-panel">
          <h1 id="project-name">Blackdog Backlog</h1>
          <div id="hero-meta-line" class="meta-line"></div>
          <p id="hero-note" class="hero-note"></p>
          <div class="progress-cluster progress-cluster-hero">
            <div class="progress-copy progress-copy-hero">
              <span id="hero-progress-detail" class="progress-detail"></span>
            </div>
            <div id="hero-progress" class="progress-slot"></div>
          </div>
          <div id="hero-links" class="link-row"></div>
        </article>
        <aside id="status-panel" class="panel status-panel">
          <h2>Status</h2>
          <div id="queue-stats" class="stats"></div>
          <div class="status-next">
            <p class="status-next-head">Next in line</p>
            <div id="status-next-lines" class="status-next-lines"></div>
          </div>
        </aside>
      </section>

      <section id="middle-band" class="panel-row panel-row-middle">
        <section id="objectives-panel" class="panel objectives-panel" data-panel="objectives">
          <div class="section-head section-head-inline">
            <div>
              <h2>Objectives</h2>
              <p id="objective-intro" class="section-copy"></p>
            </div>
            <span id="objective-summary" class="section-meta"></span>
          </div>
          <div class="objective-table-shell">
            <table class="objective-table">
              <thead>
                <tr>
                  <th scope="col">Objective</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">Progress</th>
                </tr>
              </thead>
              <tbody id="objectives-table-body"></tbody>
            </table>
          </div>
        </section>

        <section id="release-gates-panel" class="panel gates-panel" data-panel="release-gates">
          <div class="section-head section-head-inline">
            <div>
              <h2>Release Gates</h2>
            </div>
            <span id="release-gates-summary" class="section-meta"></span>
          </div>
          <div class="gate-table-shell">
            <table class="gate-table">
              <thead>
                <tr>
                  <th scope="col">Gate</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody id="release-gates-table-body"></tbody>
            </table>
          </div>
        </section>
      </section>

      <section id="surface-grid" class="panel-row panel-row-bottom">
        <section id="execution-panel" class="panel board-panel" data-panel="execution">
          <div class="section-head backlog-head">
            <div>
              <h2>Execution Map</h2>
            </div>
            <div class="toolbar-topline">
              <span id="board-summary" class="section-meta"></span>
              <a id="inbox-link" class="text-link" href="#">Inbox JSON</a>
            </div>
          </div>
          <div id="lane-board" class="lane-board"></div>
        </section>

        <section id="completed-panel" class="panel result-panel" data-panel="completed">
          <div class="section-head">
            <div>
              <h2>Completed Tasks</h2>
              <p id="completed-copy" class="section-copy"></p>
            </div>
            <span id="completed-summary" class="section-meta"></span>
          </div>
          <div id="completed-history-scroll" class="result-history"></div>
        </section>
      </section>
    </div>
  </div>

  <dialog id="reader-dialog">
    <article class="reader">
      <div class="reader-head">
        <div>
          <p id="reader-context" class="reader-context"></p>
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
    const lanePlan = Array.isArray(snapshot.plan?.lanes) ? snapshot.plan.lanes.slice() : [];
    const objectiveRows = Array.isArray(snapshot.objective_rows) ? snapshot.objective_rows.slice() : [];
    const activeObjectiveRows = objectiveRows.filter((row) => Array.isArray(row.active_task_ids) && row.active_task_ids.length);
    const focusTaskIds = new Set(
      Array.isArray(snapshot.focus_task_ids) ? snapshot.focus_task_ids.map((taskId) => String(taskId)) : []
    );
    const boardTasks = Array.isArray(snapshot.board_tasks)
      ? snapshot.board_tasks.filter((task) => normalizeStatus(task.operator_status_key) !== "complete")
      : allTasks.filter((task) => (task.objective || task.lane_id) && normalizeStatus(task.operator_status_key) !== "complete");
    const focusTasks = focusTaskIds.size
      ? allTasks.filter((task) => focusTaskIds.has(String(task.id)))
      : boardTasks.length
        ? boardTasks
        : allTasks;
    const trackedTasks = focusTasks.length ? focusTasks : allTasks;
    const completedTasks = allTasks
      .filter((task) => normalizeStatus(task.operator_status_key) === "complete")
      .sort((left, right) => completionEpoch(right) - completionEpoch(left));
    const openMessages = Array.isArray(snapshot.open_messages) ? snapshot.open_messages.slice() : [];
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

    function chip(label, key, href) {
      if (!label) {
        return "";
      }
      const normalized = normalizeStatus(key || label) || "subtle";
      if (href) {
        return `<a class="chip chip-${escapeHtml(normalized)} chip-link" href="${escapeHtml(href)}">${escapeHtml(label)}</a>`;
      }
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
        ["Backlog", links.backlog],
        ["State", links.state],
        ["Events", links.events],
        ["Results", links.results]
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

    function groupedWaveRows(tasks) {
      const waves = new Map();
      for (const lane of laneRows(tasks)) {
        const waveId = lane.wave == null ? "Unassigned" : `Wave ${lane.wave}`;
        if (!waves.has(waveId)) {
          waves.set(waveId, { id: waveId, wave: lane.wave, lanes: [] });
        }
        waves.get(waveId).lanes.push(lane);
      }
      return Array.from(waves.values()).sort((left, right) => {
        const leftWave = left.wave == null ? 9999 : Number(left.wave);
        const rightWave = right.wave == null ? 9999 : Number(right.wave);
        return leftWave - rightWave;
      });
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

    function objectiveTaskRows(objective) {
      return (Array.isArray(objective.task_ids) ? objective.task_ids : [])
        .map((taskId) => allTasksById.get(String(taskId)))
        .filter(Boolean);
    }

    function renderObjectiveQuanta(tasks) {
      if (!tasks.length) {
        return `<div class="objective-quanta"><span class="quantum quantum-empty"></span></div>`;
      }
      return `
        <div class="objective-quanta" aria-hidden="true">
          ${tasks.map((task) => `<span class="quantum quantum-${escapeHtml(normalizeStatus(task.operator_status_key || "ready"))}"></span>`).join("")}
        </div>
      `;
    }

    function objectiveProgressSummary(progress) {
      if (!progress.total) {
        return "No tasks";
      }
      return `${progress.complete}/${progress.total}`;
    }

    function objectiveProgressState(progress) {
      if (!progress.total) {
        return "No tracked work";
      }
      if (!progress.remaining) {
        return "Complete";
      }
      if (progress.counts.running) {
        return `${progress.counts.running} running`;
      }
      if (progress.counts.claimed) {
        return `${progress.counts.claimed} claimed`;
      }
      if (progress.counts.ready) {
        return `${progress.counts.ready} ready`;
      }
      if (progress.counts.waiting) {
        return `${progress.counts.waiting} waiting`;
      }
      if (progress.counts.blocked || progress.counts.failed) {
        return `${(progress.counts.blocked || 0) + (progress.counts.failed || 0)} blocked`;
      }
      return `${progress.remaining} remaining`;
    }

    function renderObjectiveRow(objective) {
      const taskRows = objectiveTaskRows(objective);
      const progress = objective.progress || progressMetrics(
        (Array.isArray(objective.task_ids) ? objective.task_ids : [])
          .map((taskId) => allTasksById.get(String(taskId)))
          .filter(Boolean)
      );
      const leadTask = objectiveLeadTask(objective);
      return `
        <tr data-objective-id="${escapeHtml(objective.id || objective.key || "objective")}"${interactiveCardAttributes(leadTask?.id)}>
          <td class="objective-key">${escapeHtml(objective.id || "Unassigned")}</td>
          <td class="objective-outcome">
            <span class="objective-title">${escapeHtml(objective.title || objective.id || "Unassigned")}</span>
          </td>
          <td class="objective-progress-cell">
            <div class="objective-progress-copy">
              <span>${escapeHtml(objectiveProgressSummary(progress))}</span>
              <span>${escapeHtml(objectiveProgressState(progress))}</span>
            </div>
            ${renderObjectiveQuanta(taskRows)}
          </td>
        </tr>
      `;
    }

    function nextRows() {
      const next = Array.isArray(snapshot.next_rows) ? snapshot.next_rows.slice(0, 2) : [];
      if (next.length) {
        return next;
      }
      return trackedTasks
        .filter((task) => normalizeStatus(task.operator_status_key) === "ready")
        .slice(0, 2)
        .map((task) => ({
          id: task.id,
          title: task.title,
          lane: task.lane_title,
          wave: task.wave,
          risk: task.risk,
        }));
    }

    function nextLine(row) {
      const taskId = row?.id ? String(row.id) : "";
      const meta = [
        row?.lane ? `Lane ${row.lane}` : "",
        row?.wave != null ? `Wave ${row.wave}` : "",
        row?.risk ? `Risk ${row.risk}` : "",
      ].filter(Boolean).join(" · ");
      return `
        <div class="status-next-line"${interactiveCardAttributes(taskId)}>
          <strong>${escapeHtml(taskId ? `${taskId} ${row.title || ""}` : row?.title || "No queued work")}</strong>
          <span>${escapeHtml(meta || "No additional scheduling detail")}</span>
        </div>
      `;
    }

    function renderObjectivesTable() {
      const objective = Array.isArray(snapshot.push_objective) ? snapshot.push_objective.join(" ") : "";
      const completedObjectiveCount = Math.max(0, objectiveRows.length - activeObjectiveRows.length);
      document.getElementById("objective-intro").textContent =
        activeObjectiveRows.length
          ? (objective || "Active outcomes for the current push.")
          : objectiveRows.length
            ? "No active objective rows. Completed objective context is listed in the history below."
            : "No objective rows tagged in the backlog yet.";
      document.getElementById("objective-summary").textContent = objectiveRows.length
        ? `${activeObjectiveRows.length} active · ${completedObjectiveCount} completed`
        : "No objective rows";
      document.getElementById("objectives-table-body").innerHTML = activeObjectiveRows.length
        ? activeObjectiveRows.map(renderObjectiveRow).join("")
        : `<tr><td colspan="3"><div class="empty">No active objective rows. Completed objective context moves to the history below.</div></td></tr>`;
      applyProgressBars(document.getElementById("objectives-panel"));
    }

    function taskTone(task) {
      return normalizeStatus(task.operator_status_key || "ready") || "ready";
    }

    function renderStatusChipRows(rows) {
      if (!Array.isArray(rows) || !rows.length) {
        return "";
      }
      return rows.map((row) => chip(row.label, row.key, row.href)).join("");
    }

    function renderTaskLinks(task) {
      const links = Array.isArray(task.links) ? task.links : [];
      return links.map((row) => textLink(row.label, row.href)).join("");
    }

    function relativeTimeFromNow(value) {
      if (!value) {
        return "just now";
      }
      const parsed = Date.parse(String(value));
      if (Number.isNaN(parsed)) {
        return String(value);
      }
      const seconds = Math.max(0, Math.round((Date.now() - parsed) / 1000));
      if (seconds < 10) {
        return "just now";
      }
      if (seconds < 60) {
        return `${seconds}s ago`;
      }
      const minutes = Math.round(seconds / 60);
      if (minutes < 60) {
        return `${minutes}m ago`;
      }
      const hours = Math.round(minutes / 60);
      if (hours < 24) {
        return `${hours}h ago`;
      }
      const days = Math.round(hours / 24);
      return `${days}d ago`;
    }

    function renderMetaItem(label, value, options = {}) {
      if (!value) {
        return "";
      }
      const valueClass = options.mono ? "meta-value meta-value-mono" : "meta-value";
      return `
        <span class="meta-item">
          <span class="meta-label">${escapeHtml(label)}:</span>
          <span class="${valueClass}">${escapeHtml(value)}</span>
        </span>
      `;
    }

    function summarizeTimeOnTask(value) {
      const raw = String(value || "").trim();
      if (!raw) {
        return "";
      }
      const parts = raw.split("·").map((part) => part.trim()).filter(Boolean);
      return parts.find((part) => part.includes("recorded") || part.includes("total")) || raw;
    }

    function heroProgressSummary(progress) {
      if (!progress.total) {
        return "No tracked tasks";
      }
      const noun = progress.total === 1 ? "task" : "tasks";
      return `${progress.complete}/${progress.total} ${noun} complete`;
    }

    function renderHeader() {
      const heroHighlights = snapshot.hero_highlights || {};
      const headers = snapshot.headers || {};
      const activity = snapshot.last_activity || {};
      const overallProgress = progressMetrics(trackedTasks);
      const metaItems = [
        renderMetaItem("Active Branch", heroHighlights.branch || headers["Target branch"] || "", { mono: true }),
        renderMetaItem("Commit", heroHighlights.commit || headers["Target commit"] || "", { mono: true }),
        renderMetaItem("Time on task", summarizeTimeOnTask(heroHighlights.time_on_task || "")),
        renderMetaItem("Last content updated", relativeTimeFromNow(snapshot.content_updated_at || snapshot.generated_at || activity.at || "")),
        renderMetaItem("Last checked", relativeTimeFromNow(snapshot.last_checked_at || snapshot.generated_at || activity.at || ""))
      ].filter(Boolean);
      document.getElementById("hero-meta-line").innerHTML = metaItems
        .join("");
      document.getElementById("hero-note").textContent = activity.summary
        ? `Latest activity: ${activity.task_id ? `${activity.task_id} ` : ""}${activity.summary}`
        : "Snapshot follows committed backlog state and recorded task results.";
      document.getElementById("hero-progress-detail").textContent = heroProgressSummary(overallProgress);
      document.getElementById("hero-progress").innerHTML = renderProgressBar(overallProgress, "progress-hero");
      applyProgressBars(document.getElementById("hero-progress"));
      document.getElementById("hero-links").innerHTML = globalLinks()
        .map(([label, href]) => textLink(label, href))
        .join("");
    }

    function renderStatusPanel() {
      const panelCounts = snapshot.queue_status || {};
      const counts = countStatuses(trackedTasks);
      const stats = [
        ["Running", Number(panelCounts.running != null ? panelCounts.running : (counts.running || 0))],
        ["Waiting", Number(panelCounts.waiting != null ? panelCounts.waiting : (counts.waiting || 0))],
        ["Blocked", Number(panelCounts.blocked != null ? panelCounts.blocked : ((counts.blocked || 0) + (counts.failed || 0)))],
        ["Last sweep completed", Number(panelCounts.last_sweep_completed || 0)],
        ["Completed today", Number(panelCounts.completed_today || 0)],
        ["Completed all-time", Number(panelCounts.completed_all_time || 0)],
      ];
      document.getElementById("queue-stats").innerHTML = stats.map(([label, value]) => `
        <div class="stat-card">
          <span class="stat-label">${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `).join("");
      const lines = nextRows();
      document.getElementById("status-next-lines").innerHTML = lines.length
        ? lines.map(nextLine).join("")
        : `<div class="status-next-line"><strong>No queued work</strong><span>The tracked objective work is complete.</span></div>`;
    }

    function parseReleaseGate(entry, fallbackLabel) {
      const raw = String(entry || "").trim();
      const explicit = raw.match(/^\\[(x|X| )\\]\\s*(.*)$/);
      if (explicit) {
        return {
          label: explicit[2] || fallbackLabel,
          passed: explicit[1].toLowerCase() === "x",
          explicit: true,
        };
      }
      const trackedProgress = progressMetrics(trackedTasks);
      return {
        label: raw || fallbackLabel,
        passed: trackedProgress.total > 0 && trackedProgress.remaining === 0,
        explicit: false,
      };
    }

    function releaseGateRows() {
      const releaseGates = Array.isArray(snapshot.release_gates) ? snapshot.release_gates : [];
      return releaseGates.map((entry, index) => parseReleaseGate(entry, `Gate ${index + 1}`));
    }

    function renderReleaseGateRow(row) {
      const gateState = row.passed
        ? `<span class="gate-status gate-status-passed"><span class="gate-mark" aria-hidden="true">&#10003;</span>Passed</span>`
        : `<span class="gate-status gate-status-open"><span class="gate-mark" aria-hidden="true">&#9711;</span>Open</span>`;
      return `
        <tr>
          <td class="gate-copy">${escapeHtml(row.label)}</td>
          <td class="gate-status-cell">${gateState}</td>
        </tr>
      `;
    }

    function renderReleaseGatesPanel() {
      const gateRows = releaseGateRows();
      const passedCount = gateRows.filter((row) => row.passed).length;
      document.getElementById("release-gates-summary").textContent = gateRows.length
        ? `${passedCount}/${gateRows.length} passed`
        : "No gates";
      document.getElementById("release-gates-table-body").innerHTML = gateRows.length
        ? gateRows.map(renderReleaseGateRow).join("")
        : `<tr><td colspan="2"><div class="empty">Add release gates to the backlog header to track them here.</div></td></tr>`;
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

    function preBlock(text, className = "detail-pre") {
      if (!text) {
        return "";
      }
      return `<pre class="${escapeHtml(className)}">${escapeHtml(text)}</pre>`;
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

    function commitBlock(task) {
      if (!task.landed_commit && !task.landed_commit_message) {
        return "";
      }
      const parts = [];
      const commitLabel = task.landed_commit_short || task.landed_commit || "";
      if (task.landed_commit_url) {
        parts.push(`<a class="text-link mono" href="${escapeHtml(task.landed_commit_url)}">${escapeHtml(commitLabel ? `Commit ${commitLabel}` : "Commit")}</a>`);
      } else if (task.landed_commit) {
        parts.push(`<p class="mono">${escapeHtml(commitLabel ? `Commit ${commitLabel}` : task.landed_commit)}</p>`);
      }
      if (task.landed_commit_message) {
        parts.push(preBlock(task.landed_commit_message, "detail-pre detail-pre-compact"));
      }
      return parts.join("");
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

    function renderTaskCard(task) {
      const tone = taskTone(task);
      const laneMeta = [
        task.lane_title || "",
        task.wave != null ? `Wave ${task.wave}` : "",
        task.total_compute_label ? `Compute ${task.total_compute_label}` : "",
      ].filter(Boolean);
      const dependency = Array.isArray(task.predecessor_ids) && task.predecessor_ids.length
        ? `Depends on ${task.predecessor_ids.join(", ")}`
        : "Lane opener";
      return `
        <article class="task-card tone-${escapeHtml(tone)}"${interactiveCardAttributes(task.id)}>
          <div class="task-card-top">
            <div class="task-id-group">
              <span class="task-code">${escapeHtml(task.id)}</span>
              ${renderStatusChipRows(task.card_status_chips)}
            </div>
            ${task.priority ? `<span class="mini-chip">${escapeHtml(task.priority)}</span>` : ""}
          </div>
          <h3 class="task-title">${escapeHtml(task.title)}</h3>
          <p class="task-summary">${escapeHtml(taskSummary(task) || "No current summary recorded.")}</p>
          <div class="task-meta">${laneMeta.map((row) => `<span>${escapeHtml(row)}</span>`).join("")}</div>
          <div class="task-dependency">${escapeHtml(dependency)}</div>
        </article>
      `;
    }

    function renderLaneColumn(lane) {
      return `
        <section class="lane-column" data-lane-id="${escapeHtml(lane.id)}">
          <div class="lane-head">
            <span class="lane-phase">${escapeHtml(lane.wave == null ? "Unassigned" : `Wave ${lane.wave}`)}</span>
            <h3>${escapeHtml(lane.title)}</h3>
            <div class="lane-meta">
              <span>${escapeHtml(pluralize(lane.tasks.length, "task"))}</span>
            </div>
          </div>
          <div class="lane-stack">
            ${lane.tasks.map(renderTaskCard).join("")}
          </div>
        </section>
      `;
    }

    function renderExecutionMap() {
      const waveRows = groupedWaveRows(boardTasks);
      document.getElementById("board-summary").textContent = boardTasks.length
        ? `${pluralize(waveRows.length, "wave")} · ${pluralize(laneRows(boardTasks).length, "lane")} · ${pluralize(boardTasks.length, "task")}`
        : "No active execution map";
      document.getElementById("lane-board").innerHTML = waveRows.length
        ? waveRows.map((waveRow) => `
            <section class="wave-section">
              <div class="wave-head">
                <h3>${escapeHtml(waveRow.id)}</h3>
                <div class="wave-meta">
                  <span>${escapeHtml(pluralize(waveRow.lanes.length, "lane"))}</span>
                  <span>${escapeHtml(pluralize(waveRow.lanes.reduce((count, lane) => count + lane.tasks.length, 0), "task"))}</span>
                </div>
              </div>
              <div class="wave-grid">
                ${waveRow.lanes.map(renderLaneColumn).join("")}
              </div>
            </section>
          `).join("")
        : `<div class="empty">No active lanes remain in the current execution map.</div>`;
      document.getElementById("inbox-link").href = snapshot.links?.inbox || "#";
    }

    function completionStamp(task) {
      return task.completed_at || task.latest_result_at || task.latest_run_at || "";
    }

    function completionEpoch(task) {
      const value = completionStamp(task);
      const parsed = Date.parse(String(value || ""));
      return Number.isNaN(parsed) ? 0 : parsed;
    }

    function completionSweep(task) {
      return String(task.latest_result_run_id || task.latest_run_id || "direct");
    }

    function completionSweepLabel(task) {
      const sweep = completionSweep(task);
      if (sweep === "direct") {
        return "Direct updates";
      }
      return `Sweep ${sweep}`;
    }

    function completedObjectiveGroup(task) {
      const objectiveId = String(task.objective || "").trim();
      const objectiveTitle = String(task.objective_title || "").trim();
      if (objectiveId) {
        return {
          key: objectiveId,
          id: objectiveId,
          title: objectiveTitle && objectiveTitle !== objectiveId ? objectiveTitle : objectiveId
        };
      }
      if (objectiveTitle && objectiveTitle !== "Unassigned") {
        return {
          key: `title:${objectiveTitle}`,
          id: "",
          title: objectiveTitle
        };
      }
      return {
        key: "unassigned",
        id: "",
        title: "Unassigned objective"
      };
    }

    function groupedCompletedTasks(tasks) {
      const groups = [];
      for (const task of tasks) {
        const key = completionSweep(task);
        const label = completionSweepLabel(task);
        const existing = groups[groups.length - 1];
        if (!existing || existing.key !== key) {
          groups.push({ key, label, tasks: [task] });
          continue;
        }
        existing.tasks.push(task);
      }
      return groups;
    }

    function groupedCompletedObjectives(tasks) {
      const groups = [];
      const groupsByKey = new Map();
      for (const task of tasks) {
        const objective = completedObjectiveGroup(task);
        let group = groupsByKey.get(objective.key);
        if (!group) {
          group = { ...objective, tasks: [] };
          groupsByKey.set(objective.key, group);
          groups.push(group);
        }
        group.tasks.push(task);
      }
      return groups;
    }

    function renderCompletedCard(task) {
      const meta = [
        completionStamp(task) ? formatTimestamp(completionStamp(task)) : "",
        task.latest_result_actor ? `Actor ${task.latest_result_actor}` : "",
        task.total_compute_label ? `Compute ${task.total_compute_label}` : "",
      ].filter(Boolean);
      return `
        <article class="result-card tone-complete"${interactiveCardAttributes(task.id)}>
          <div class="result-top">
            <span class="task-code">${escapeHtml(task.id)}</span>
            <div class="result-chips">
              ${renderStatusChipRows(task.card_status_chips)}
            </div>
          </div>
          <h3 class="result-title">${escapeHtml(task.title)}</h3>
          <p>${escapeHtml(taskSummary(task) || "Completed task with no additional summary.")}</p>
          <div class="result-meta">${meta.map((row) => `<span>${escapeHtml(row)}</span>`).join("")}</div>
        </article>
      `;
    }

    function renderCompletedPanel() {
      document.getElementById("completed-copy").textContent =
        "Completed work stays visible here, grouped by sweep and objective so finished outcomes keep their context.";
      const visibleCompleted = completedTasks.slice(0, 60);
      const grouped = groupedCompletedTasks(visibleCompleted);
      document.getElementById("completed-summary").textContent = visibleCompleted.length
        ? `Showing ${visibleCompleted.length} of ${completedTasks.length}`
        : "No completed tasks";
      document.getElementById("completed-history-scroll").innerHTML = grouped.length
        ? grouped.map((group) => `
            <section class="completed-group" data-sweep="${escapeHtml(group.key)}">
              <div class="completed-group-head">
                <span class="completed-group-label">${escapeHtml(group.label)}</span>
                <span class="section-meta">${escapeHtml(pluralize(group.tasks.length, "task"))}</span>
              </div>
              <div class="completed-objective-stack">
                ${groupedCompletedObjectives(group.tasks).map((objectiveGroup) => `
                  <section class="completed-objective-group" data-objective-key="${escapeHtml(objectiveGroup.key)}">
                    <div class="completed-objective-head">
                      <div class="completed-objective-copy">
                        ${objectiveGroup.id ? `<span class="completed-objective-key">${escapeHtml(objectiveGroup.id)}</span>` : ""}
                        <span class="completed-objective-title">${escapeHtml(objectiveGroup.title)}</span>
                      </div>
                      <span class="section-meta">${escapeHtml(pluralize(objectiveGroup.tasks.length, "task"))}</span>
                    </div>
                    <div class="results-grid">
                      ${objectiveGroup.tasks.map(renderCompletedCard).join("")}
                    </div>
                  </section>
                `).join("")}
              </div>
            </section>
          `).join("")
        : `<div class="empty">Completed tasks will appear here once work lands.</div>`;
    }

    function openTaskReader(taskId) {
      const task = allTasks.find((row) => String(row.id) === String(taskId));
      if (!task) {
        return;
      }
      document.getElementById("reader-context").textContent =
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
        detailBlock("What Changed", listBlock(task.latest_result_what_changed), { wide: true }),
        detailBlock("Activity", activityList(activityRows), { wide: true }),
        detailBlock("Sequence", detailList(sequenceRows)),
        detailBlock("Safe First Slice", paragraphBlock(task.safe_first_slice)),
        detailBlock("Runtime", detailList(runtimeRows)),
        detailBlock("Model Response", preBlock(task.model_response), { wide: true }),
        detailBlock("Landed Commit", commitBlock(task), { wide: true }),
        detailBlock("Why", paragraphBlock(task.why)),
        detailBlock("Evidence", paragraphBlock(task.evidence)),
        detailBlock("Paths", listBlock(task.paths)),
        detailBlock("Checks", listBlock(task.checks)),
        detailBlock("Docs", listBlock(task.docs)),
        detailBlock("Validation", listBlock(task.latest_result_validation), { wide: true }),
        detailBlock("Residual", listBlock(task.latest_result_residual), { wide: true })
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
    renderStatusPanel();
    renderObjectivesTable();
    renderReleaseGatesPanel();
    renderExecutionMap();
    renderCompletedPanel();
    wireStaticEvents();
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
