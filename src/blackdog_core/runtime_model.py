"""Typed runtime-model projections for current Blackdog artifacts.

This module is intentionally stdlib-only in its implementation style and keeps
the new vocabulary as a compatibility projection over the current backlog,
state, inbox, event, result, and supervisor artifacts.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
import subprocess

from .backlog import (
    BacklogSnapshot,
    BacklogTask,
    blocking_reason,
    classify_task_status,
    load_backlog,
    load_task_results,
)
from .profile import RepoProfile
from .state import load_events, load_inbox, load_state


class RuntimeModelError(RuntimeError):
    pass


def _text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        items = value
    elif isinstance(value, list):
        items = tuple(value)
    else:
        return (_text(value),) if _text(value) else ()
    output: list[str] = []
    for item in items:
        text = _text(item)
        if text:
            output.append(text)
    return tuple(output)


def _mapping_copy(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _composite_id(*parts: str | None) -> str:
    normalized = [_text(part) for part in parts if _text(part)]
    return ":".join(normalized)


def _path_or_none(value: Any) -> Path | None:
    text = _optional_text(value)
    return Path(text) if text else None


def _run_git(project_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise RuntimeModelError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _git_dirty(project_root: Path) -> bool | None:
    try:
        return bool(_run_git(project_root, "status", "--porcelain"))
    except RuntimeModelError:
        return None


def _git_branch(project_root: Path) -> str | None:
    try:
        branch = _run_git(project_root, "rev-parse", "--abbrev-ref", "HEAD")
    except RuntimeModelError:
        return None
    return branch if branch and branch != "HEAD" else None


def _git_commit(project_root: Path) -> str | None:
    try:
        commit = _run_git(project_root, "rev-parse", "HEAD")
    except RuntimeModelError:
        return None
    return commit or None


def _result_attempt_status(status: str | None) -> str:
    normalized = _text(status)
    if normalized in {"done", "success", "partial", "blocked", "failed", "launch-failed", "released"}:
        return normalized
    if normalized:
        return normalized
    return "unknown"


def _attempt_status_from_events(events: Sequence[Event], *, result_status: str | None = None) -> str:
    if result_status:
        return _result_attempt_status(result_status)
    event_types = {event.type for event in events}
    if "child_launch_failed" in event_types:
        return "launch-failed"
    if "child_finish" in event_types or "task_result" in event_types:
        return "done"
    if "child_launch" in event_types:
        return "running"
    if "release" in event_types:
        return "released"
    if "claim" in event_types:
        return "prepared"
    return "unknown"


def _ordered_task_items(snapshot: BacklogSnapshot) -> list[BacklogTask]:
    return sorted(
        snapshot.tasks.values(),
        key=lambda task: (
            task.wave if task.wave is not None else 9999,
            task.lane_order if task.lane_order is not None else 9999,
            task.lane_position if task.lane_position is not None else 9999,
            task.id,
        ),
    )


@dataclass(frozen=True, slots=True)
class Repository:
    project_name: str
    project_root: Path
    profile_file: Path
    control_dir: Path
    backlog_file: Path
    state_file: Path
    events_file: Path
    inbox_file: Path
    results_dir: Path
    threads_dir: Path
    supervisor_runs_dir: Path
    html_file: Path
    doc_routing_defaults: tuple[str, ...]
    validation_commands: tuple[str, ...]
    integration_branches: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Workspace:
    project_root: Path
    checkout_root: Path
    branch: str | None
    commit: str | None
    dirty: bool | None
    role: str
    workspace_mode: str
    target_branch: str | None
    task_id: str | None = None
    is_primary: bool = False


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    type: str
    at: str
    actor: str
    task_id: str | None
    attempt_id: str | None
    run_id: str | None
    wait_condition_id: str | None
    control_message_id: str | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Result:
    task_id: str
    attempt_id: str
    run_id: str
    actor: str
    recorded_at: str
    status: str
    result_file: Path | None
    what_changed: tuple[str, ...]
    validation: tuple[str, ...]
    residual: tuple[str, ...]
    needs_user_input: bool
    followup_candidates: tuple[str, ...]
    metadata: dict[str, Any]
    task_shaping_telemetry: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ControlMessage:
    message_id: str
    action: str
    at: str
    sender: str | None
    recipient: str | None
    kind: str | None
    task_id: str | None
    reply_to: str | None
    body: str
    tags: tuple[str, ...]
    status: str
    resolved_at: str | None
    resolved_by: str | None
    resolution_note: str | None


@dataclass(frozen=True, slots=True)
class PromptReceipt:
    receipt_id: str
    attempt_id: str
    task_id: str | None
    run_id: str | None
    actor: str | None
    at: str | None
    workspace_root: Path | None
    checkout_root: Path | None
    branch: str | None
    workspace_mode: str | None
    prompt_file: Path | None
    prompt_text: str | None
    prompt_hash: str | None
    prompt_template_version: str | None
    prompt_template_hash: str | None
    launch_command: tuple[str, ...]
    launch_command_strategy: str | None
    launch_settings: dict[str, Any]
    packet: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WaitCondition:
    wait_id: str
    kind: str
    reason: str
    status: str
    task_id: str | None
    attempt_id: str | None
    workset_id: str
    execution_id: str
    source: str
    at: str | None
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TaskAttempt:
    attempt_id: str
    task_id: str
    run_id: str
    actor: str
    status: str
    started_at: str | None
    ended_at: str | None
    workspace: Workspace | None
    result: Result | None
    prompt_receipt: PromptReceipt | None
    events: tuple[Event, ...]


@dataclass(frozen=True, slots=True)
class TaskState:
    task_id: str
    title: str
    status: str
    detail: str
    block_reason: str | None
    claim_status: str
    approval_status: str
    claimed_by: str | None
    claimed_at: str | None
    completed_by: str | None
    completed_at: str | None
    latest_attempt_id: str | None
    latest_result: Result | None
    result_count: int
    attempt_count: int
    predecessor_ids: tuple[str, ...]
    spec: dict[str, Any]
    is_runnable: bool
    is_blocked: bool


@dataclass(frozen=True, slots=True)
class Workset:
    workset_id: str
    title: str
    target_branch: str | None
    visibility: str
    task_ids: tuple[str, ...]
    task_edges: tuple[tuple[str, str], ...]
    scope_paths: tuple[str, ...]
    policies: dict[str, Any]


@dataclass(frozen=True, slots=True)
class WorksetExecution:
    execution_id: str
    workset_id: str
    mode: str
    status: str
    workspace: Workspace
    target_branch: str | None
    started_at: str | None
    updated_at: str | None
    task_attempts: tuple[TaskAttempt, ...]
    wait_conditions: tuple[WaitCondition, ...]
    control_messages: tuple[ControlMessage, ...]


@dataclass(frozen=True, slots=True)
class RuntimeModel:
    repository: Repository
    workspace: Workspace
    workset: Workset
    task_states: tuple[TaskState, ...]
    task_attempts: tuple[TaskAttempt, ...]
    workset_execution: WorksetExecution
    prompt_receipts: tuple[PromptReceipt, ...]
    wait_conditions: tuple[WaitCondition, ...]
    control_messages: tuple[ControlMessage, ...]
    results: tuple[Result, ...]
    events: tuple[Event, ...]


def project_repository(profile: RepoProfile, *, integration_branches: Sequence[str] = ()) -> Repository:
    return Repository(
        project_name=profile.project_name,
        project_root=profile.paths.project_root,
        profile_file=profile.paths.profile_file,
        control_dir=profile.paths.control_dir,
        backlog_file=profile.paths.backlog_file,
        state_file=profile.paths.state_file,
        events_file=profile.paths.events_file,
        inbox_file=profile.paths.inbox_file,
        results_dir=profile.paths.results_dir,
        threads_dir=profile.paths.threads_dir,
        supervisor_runs_dir=profile.paths.supervisor_runs_dir,
        html_file=profile.paths.html_file,
        doc_routing_defaults=tuple(profile.doc_routing_defaults),
        validation_commands=tuple(profile.validation_commands),
        integration_branches=tuple(_string_tuple(integration_branches)),
    )


def project_workspace(
    *,
    project_root: Path,
    checkout_root: Path | None = None,
    branch: str | None = None,
    commit: str | None = None,
    dirty: bool | None = None,
    role: str | None = None,
    workspace_mode: str = "git-worktree",
    target_branch: str | None = None,
    task_id: str | None = None,
) -> Workspace:
    resolved_checkout_root = (checkout_root or project_root).resolve()
    resolved_branch = branch if branch is not None else _git_branch(resolved_checkout_root)
    resolved_commit = commit if commit is not None else _git_commit(resolved_checkout_root)
    resolved_dirty = dirty if dirty is not None else _git_dirty(resolved_checkout_root)
    is_primary = resolved_checkout_root.resolve() == project_root.resolve()
    resolved_role = role or ("primary" if is_primary else "task")
    return Workspace(
        project_root=project_root.resolve(),
        checkout_root=resolved_checkout_root,
        branch=resolved_branch,
        commit=resolved_commit,
        dirty=resolved_dirty,
        role=resolved_role,
        workspace_mode=workspace_mode,
        target_branch=target_branch,
        task_id=task_id,
        is_primary=is_primary,
    )


def project_event(row: Mapping[str, Any]) -> Event:
    payload = row.get("payload") if isinstance(row.get("payload"), MappingABC) else {}
    return Event(
        event_id=_text(row.get("event_id")),
        type=_text(row.get("type")),
        at=_text(row.get("at")),
        actor=_text(row.get("actor")),
        task_id=_optional_text(row.get("task_id")),
        attempt_id=_optional_text(row.get("attempt_id")),
        run_id=_optional_text(row.get("run_id")),
        wait_condition_id=_optional_text(row.get("wait_condition_id")),
        control_message_id=_optional_text(row.get("control_message_id")),
        payload=_mapping_copy(payload),
    )


def project_result(row: Mapping[str, Any], *, result_file: Path | None = None) -> Result:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), MappingABC) else {}
    telemetry = row.get("task_shaping_telemetry") if isinstance(row.get("task_shaping_telemetry"), MappingABC) else {}
    metadata_payload = _mapping_copy(metadata)
    if isinstance(row.get("prompt_receipt"), MappingABC):
        metadata_payload.setdefault("prompt_receipt", _mapping_copy(row.get("prompt_receipt")))
    safe_result_file = result_file
    if safe_result_file is None and row.get("result_file"):
        safe_result_file = Path(str(row["result_file"]))
    task_id = _text(row.get("task_id"))
    run_id = _text(row.get("run_id"))
    return Result(
        task_id=task_id,
        attempt_id=_text(row.get("attempt_id")) or _composite_id(task_id, run_id),
        run_id=run_id,
        actor=_text(row.get("actor")),
        recorded_at=_text(row.get("recorded_at")),
        status=_text(row.get("status")),
        result_file=safe_result_file,
        what_changed=_string_tuple(row.get("what_changed")),
        validation=_string_tuple(row.get("validation")),
        residual=_string_tuple(row.get("residual")),
        needs_user_input=bool(row.get("needs_user_input")),
        followup_candidates=_string_tuple(row.get("followup_candidates")),
        metadata=metadata_payload,
        task_shaping_telemetry=_mapping_copy(telemetry),
    )


def project_prompt_receipt_from_entry(
    entry: Mapping[str, Any],
    *,
    fallback_workspace: Workspace | None = None,
) -> PromptReceipt:
    packet = _mapping_copy(entry)
    packet.pop("prompt_text", None)
    packet.pop("text", None)
    launch_settings = entry.get("launch_settings") if isinstance(entry.get("launch_settings"), MappingABC) else {}
    workspace = fallback_workspace
    if workspace is None and (
        _optional_text(entry.get("workspace"))
        or _optional_text(entry.get("branch"))
        or _optional_text(entry.get("target_branch"))
    ):
        workspace = Workspace(
            project_root=fallback_workspace.project_root if fallback_workspace else Path.cwd(),
            checkout_root=_path_or_none(entry.get("workspace")) or (fallback_workspace.checkout_root if fallback_workspace else Path.cwd()),
            branch=_optional_text(entry.get("branch")) or (fallback_workspace.branch if fallback_workspace else None),
            commit=fallback_workspace.commit if fallback_workspace else None,
            dirty=fallback_workspace.dirty if fallback_workspace else None,
            role=fallback_workspace.role if fallback_workspace else "task",
            workspace_mode=_optional_text(entry.get("workspace_mode")) or (fallback_workspace.workspace_mode if fallback_workspace else "git-worktree"),
            target_branch=_optional_text(entry.get("target_branch")) or (fallback_workspace.target_branch if fallback_workspace else None),
            task_id=_optional_text(entry.get("task_id")) or (fallback_workspace.task_id if fallback_workspace else None),
            is_primary=fallback_workspace.is_primary if fallback_workspace else False,
        )
    return project_prompt_receipt(
        task_id=_optional_text(entry.get("task_id")),
        run_id=_optional_text(entry.get("run_id")),
        actor=_optional_text(entry.get("child_agent")) or _optional_text(entry.get("actor")),
        workspace=workspace,
        at=_optional_text(entry.get("recorded_at")) or _optional_text(entry.get("launch_time")),
        prompt_text=_optional_text(entry.get("prompt_text")) or _optional_text(entry.get("text")),
        prompt_file=_path_or_none(entry.get("prompt_file")),
        prompt_hash=_optional_text(entry.get("prompt_hash")),
        prompt_template_version=_optional_text(entry.get("template_version")),
        prompt_template_hash=_optional_text(entry.get("template_hash")),
        launch_command=_string_tuple(entry.get("launch_command")),
        launch_command_strategy=_optional_text(entry.get("launch_command_strategy")),
        launch_settings=launch_settings if isinstance(launch_settings, MappingABC) else {},
        packet=packet,
    )


def project_prompt_receipts_from_artifacts(
    *,
    state: Mapping[str, Any],
    results: Sequence[Result],
    workspace: Workspace | None = None,
) -> tuple[PromptReceipt, ...]:
    receipts: dict[str, PromptReceipt] = {}
    state_attempts = state.get("task_attempts") if isinstance(state.get("task_attempts"), MappingABC) else {}
    for attempt_id, entry in dict(state_attempts or {}).items():
        if not isinstance(entry, MappingABC):
            continue
        prompt_entry = entry.get("prompt_receipt")
        if isinstance(prompt_entry, MappingABC):
            receipt = project_prompt_receipt_from_entry(prompt_entry, fallback_workspace=workspace)
            receipts[str(attempt_id) or receipt.receipt_id] = receipt
    for result in results:
        prompt_entry = result.metadata.get("prompt_receipt") if isinstance(result.metadata, dict) else None
        if not isinstance(prompt_entry, MappingABC):
            continue
        receipt = project_prompt_receipt_from_entry(prompt_entry, fallback_workspace=workspace)
        receipts[result.attempt_id or receipt.receipt_id] = receipt
    ordered = sorted(receipts.values(), key=lambda item: (item.at or "", item.receipt_id))
    return tuple(ordered)


def project_control_messages(rows: Sequence[Mapping[str, Any]]) -> tuple[ControlMessage, ...]:
    messages: dict[str, dict[str, Any]] = {}
    for row in rows:
        action = _text(row.get("action"))
        message_id = _text(row.get("message_id"))
        if not message_id:
            continue
        if action == "message":
            messages[message_id] = {
                "message_id": message_id,
                "action": action,
                "at": _text(row.get("at")),
                "sender": _optional_text(row.get("sender")),
                "recipient": _optional_text(row.get("recipient")),
                "kind": _optional_text(row.get("kind")),
                "task_id": _optional_text(row.get("task_id")),
                "reply_to": _optional_text(row.get("reply_to")),
                "body": _text(row.get("body")),
                "tags": _string_tuple(row.get("tags")),
                "status": "open",
                "resolved_at": None,
                "resolved_by": None,
                "resolution_note": None,
            }
            continue
        if action == "resolve" and message_id in messages:
            messages[message_id].update(
                {
                    "status": "resolved",
                    "resolved_at": _text(row.get("at")),
                    "resolved_by": _optional_text(row.get("actor")),
                    "resolution_note": _text(row.get("note")),
                }
            )
    return tuple(
        ControlMessage(
            message_id=row["message_id"],
            action=row["action"],
            at=row["at"],
            sender=row["sender"],
            recipient=row["recipient"],
            kind=row["kind"],
            task_id=row["task_id"],
            reply_to=row["reply_to"],
            body=row["body"],
            tags=row["tags"],
            status=row["status"],
            resolved_at=row["resolved_at"],
            resolved_by=row["resolved_by"],
            resolution_note=row["resolution_note"],
        )
        for row in sorted(messages.values(), key=lambda item: item["at"], reverse=True)
    )


def project_prompt_receipt(
    *,
    task_id: str | None,
    run_id: str | None,
    actor: str | None = None,
    workspace: Workspace | None = None,
    at: str | None = None,
    prompt_text: str | None = None,
    prompt_file: Path | None = None,
    prompt_hash: str | None = None,
    prompt_template_version: str | None = None,
    prompt_template_hash: str | None = None,
    launch_command: Sequence[str] = (),
    launch_command_strategy: str | None = None,
    launch_settings: Mapping[str, Any] | None = None,
    packet: Mapping[str, Any] | None = None,
) -> PromptReceipt:
    attempt_id = _composite_id(task_id, run_id)
    workspace_root = workspace.checkout_root if workspace else None
    branch = workspace.branch if workspace else None
    workspace_mode = workspace.workspace_mode if workspace else None
    payload = _mapping_copy(packet)
    if prompt_text is not None and "prompt_text" not in payload:
        payload["prompt_text"] = prompt_text
    if prompt_hash is not None and "prompt_hash" not in payload:
        payload["prompt_hash"] = prompt_hash
    if prompt_template_version is not None and "prompt_template_version" not in payload:
        payload["prompt_template_version"] = prompt_template_version
    if prompt_template_hash is not None and "prompt_template_hash" not in payload:
        payload["prompt_template_hash"] = prompt_template_hash
    if prompt_file is not None:
        payload.setdefault("prompt_file", str(prompt_file))
    return PromptReceipt(
        receipt_id=attempt_id or _composite_id(task_id, run_id, prompt_hash, at),
        attempt_id=attempt_id,
        task_id=_optional_text(task_id),
        run_id=_optional_text(run_id),
        actor=_optional_text(actor),
        at=_optional_text(at),
        workspace_root=workspace_root,
        checkout_root=workspace_root,
        branch=branch,
        workspace_mode=workspace_mode,
        prompt_file=prompt_file,
        prompt_text=prompt_text,
        prompt_hash=_optional_text(prompt_hash),
        prompt_template_version=_optional_text(prompt_template_version),
        prompt_template_hash=_optional_text(prompt_template_hash),
        launch_command=tuple(_text(item) for item in launch_command if _text(item)),
        launch_command_strategy=_optional_text(launch_command_strategy),
        launch_settings=_mapping_copy(launch_settings),
        packet=payload,
    )


def project_task_attempts(
    *,
    results: Sequence[Result],
    events: Sequence[Event],
    workspace: Workspace | None,
    state_attempts: Sequence[Mapping[str, Any]] = (),
    prompt_receipts: Sequence[PromptReceipt] = (),
) -> tuple[TaskAttempt, ...]:
    results_by_key: dict[tuple[str, str], list[Result]] = {}
    events_by_key: dict[tuple[str, str], list[Event]] = {}
    prompt_by_attempt_id = {receipt.attempt_id: receipt for receipt in prompt_receipts}
    state_attempts_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for result in results:
        if not result.task_id or not result.run_id:
            continue
        results_by_key.setdefault((result.task_id, result.run_id), []).append(result)

    for event in events:
        task_id = _optional_text(event.task_id)
        run_id = _optional_text(event.run_id) or _optional_text((event.payload or {}).get("run_id"))
        if not task_id or not run_id:
            continue
        events_by_key.setdefault((task_id, run_id), []).append(event)

    for entry in state_attempts:
        task_id = _optional_text(entry.get("task_id"))
        run_id = _optional_text(entry.get("run_id"))
        if not task_id or not run_id:
            continue
        state_attempts_by_key[(task_id, run_id)] = dict(entry)

    attempts: list[TaskAttempt] = []
    seen_keys: set[tuple[str, str]] = set(results_by_key) | set(events_by_key) | set(state_attempts_by_key)
    for task_id, run_id in sorted(seen_keys):
        related_results = sorted(results_by_key.get((task_id, run_id), []), key=lambda item: item.recorded_at)
        related_events = sorted(events_by_key.get((task_id, run_id), []), key=lambda item: item.at)
        state_entry = state_attempts_by_key.get((task_id, run_id)) or {}
        latest_result = related_results[-1] if related_results else None
        prompt_receipt = prompt_by_attempt_id.get(_composite_id(task_id, run_id))
        if prompt_receipt is None and isinstance(state_entry.get("prompt_receipt"), MappingABC):
            prompt_receipt = project_prompt_receipt_from_entry(state_entry["prompt_receipt"], fallback_workspace=workspace)
        actor = (
            latest_result.actor
            if latest_result is not None
            else (
                _optional_text(state_entry.get("child_agent"))
                or _optional_text(state_entry.get("actor"))
                or (related_events[0].actor if related_events else (prompt_receipt.actor if prompt_receipt else "unknown"))
            )
        )
        started_candidates = [item.at for item in related_events if item.at]  # type: ignore[attr-defined]
        if prompt_receipt and prompt_receipt.at:
            started_candidates.append(prompt_receipt.at)
        if latest_result is not None and latest_result.recorded_at:
            started_candidates.append(latest_result.recorded_at)
        if _optional_text(state_entry.get("launched_at")):
            started_candidates.append(str(state_entry["launched_at"]))
        if _optional_text(state_entry.get("started_at")):
            started_candidates.append(str(state_entry["started_at"]))
        started_at = min(started_candidates) if started_candidates else None
        ended_candidates = [item.at for item in related_events if item.at]  # type: ignore[attr-defined]
        if latest_result is not None and latest_result.recorded_at:
            ended_candidates.append(latest_result.recorded_at)
        if _optional_text(state_entry.get("completed_at")):
            ended_candidates.append(str(state_entry["completed_at"]))
        if _optional_text(state_entry.get("updated_at")):
            ended_candidates.append(str(state_entry["updated_at"]))
        ended_at = max(ended_candidates) if ended_candidates else started_at
        attempts.append(
            TaskAttempt(
                attempt_id=_composite_id(task_id, run_id),
                task_id=task_id,
                run_id=run_id,
                actor=actor,
                status=_optional_text(state_entry.get("status"))
                or _attempt_status_from_events(
                    related_events,
                    result_status=latest_result.status if latest_result is not None else None,
                ),
                started_at=started_at or _optional_text(state_entry.get("launched_at")),
                ended_at=ended_at,
                workspace=workspace,
                result=latest_result,
                prompt_receipt=prompt_receipt,
                events=tuple(related_events),
            )
        )
    return tuple(attempts)


def project_task_state(
    task: BacklogTask,
    snapshot: BacklogSnapshot,
    state: Mapping[str, Any],
    *,
    results_by_task: Mapping[str, Sequence[Result]],
    attempts_by_task: Mapping[str, Sequence[TaskAttempt]],
    allow_high_risk: bool = False,
) -> TaskState:
    state_payload = state if isinstance(state, dict) else dict(state)
    status, detail = classify_task_status(task, snapshot, state_payload, allow_high_risk=allow_high_risk)
    claim_entry = state_payload.get("task_claims", {}).get(task.id) if isinstance(state_payload.get("task_claims"), dict) else {}
    approval_entry = state_payload.get("approval_tasks", {}).get(task.id) if isinstance(state_payload.get("approval_tasks"), dict) else {}
    latest_result = (
        max(results_by_task.get(task.id, []), key=lambda item: item.recorded_at)
        if results_by_task.get(task.id)
        else None
    )
    latest_attempt = (
        max(attempts_by_task.get(task.id, []), key=lambda item: item.ended_at or item.started_at or "")
        if attempts_by_task.get(task.id)
        else None
    )
    return TaskState(
        task_id=task.id,
        title=task.title,
        status=status,
        detail=detail,
        block_reason=blocking_reason(task, snapshot, state_payload, allow_high_risk=allow_high_risk),
        claim_status=_text(claim_entry.get("status")) if isinstance(claim_entry, dict) else "absent",
        approval_status=_text(approval_entry.get("status")) if isinstance(approval_entry, dict) else "absent",
        claimed_by=_optional_text(claim_entry.get("claimed_by")) if isinstance(claim_entry, dict) else None,
        claimed_at=_optional_text(claim_entry.get("claimed_at")) if isinstance(claim_entry, dict) else None,
        completed_by=_optional_text(claim_entry.get("completed_by")) if isinstance(claim_entry, dict) else None,
        completed_at=_optional_text(claim_entry.get("completed_at")) if isinstance(claim_entry, dict) else None,
        latest_attempt_id=latest_attempt.attempt_id if latest_attempt is not None else None,
        latest_result=latest_result,
        result_count=len(results_by_task.get(task.id, [])),
        attempt_count=len(attempts_by_task.get(task.id, [])),
        predecessor_ids=task.predecessor_ids,
        spec=_mapping_copy(task.payload),
        is_runnable=status == "ready",
        is_blocked=status in {"waiting", "approval", "high-risk"},
    )


def project_task_states(
    snapshot: BacklogSnapshot,
    state: Mapping[str, Any],
    *,
    results: Sequence[Result],
    task_attempts: Sequence[TaskAttempt],
    allow_high_risk: bool = False,
) -> tuple[TaskState, ...]:
    results_by_task: dict[str, list[Result]] = {}
    attempts_by_task: dict[str, list[TaskAttempt]] = {}

    for result in results:
        results_by_task.setdefault(result.task_id, []).append(result)
    for attempt in task_attempts:
        attempts_by_task.setdefault(attempt.task_id, []).append(attempt)

    ordered_tasks = _ordered_task_items(snapshot)
    task_states: list[TaskState] = []
    for task in ordered_tasks:
        task_states.append(
            project_task_state(
                task,
                snapshot,
                state,
                results_by_task=results_by_task,
                attempts_by_task=attempts_by_task,
                allow_high_risk=allow_high_risk,
            )
        )
    return tuple(task_states)


def project_workset(
    *,
    snapshot: BacklogSnapshot,
    repository: Repository,
    task_states: Sequence[TaskState],
    target_branch: str | None = None,
    visibility: str = "default",
) -> Workset:
    task_ids = tuple(task.task_id for task in task_states)
    task_edges = tuple(
        (predecessor_id, task.task_id)
        for task in task_states
        for predecessor_id in task.predecessor_ids
    )
    scope_paths = tuple(
        dict.fromkeys(
            path
            for task in snapshot.tasks.values()
            for path in (
                *(_string_tuple(task.payload.get("paths"))),
                *(_string_tuple(task.payload.get("docs"))),
            )
        )
    )
    policies = {
        "validation_commands": repository.validation_commands,
        "doc_routing_defaults": repository.doc_routing_defaults,
    }
    title = _text(snapshot.headers.get("Project"), default=repository.project_name)
    if not title:
        title = repository.project_name
    return Workset(
        workset_id=repository.control_dir.name,
        title=title,
        target_branch=target_branch,
        visibility=visibility,
        task_ids=task_ids,
        task_edges=task_edges,
        scope_paths=scope_paths,
        policies=policies,
    )


def project_wait_conditions(
    *,
    workset: Workset,
    task_states: Sequence[TaskState],
    control_messages: Sequence[ControlMessage],
    persisted_wait_conditions: Sequence[Mapping[str, Any]] = (),
    execution_id: str,
) -> tuple[WaitCondition, ...]:
    waits: list[WaitCondition] = []
    seen_wait_ids: set[str] = set()
    for entry in persisted_wait_conditions:
        wait_id = _optional_text(entry.get("wait_id")) or _composite_id(
            workset.workset_id,
            _optional_text(entry.get("task_id")),
            _optional_text(entry.get("run_id")),
            _optional_text(entry.get("kind")),
        )
        if not wait_id:
            continue
        seen_wait_ids.add(wait_id)
        waits.append(
            WaitCondition(
                wait_id=wait_id,
                kind=_text(entry.get("kind"), default="unknown"),
                reason=_text(entry.get("reason") or entry.get("detail"), default="wait condition"),
                status=_text(entry.get("status"), default="unknown"),
                task_id=_optional_text(entry.get("task_id")),
                attempt_id=_optional_text(entry.get("attempt_id")),
                workset_id=workset.workset_id,
                execution_id=execution_id,
                source="state",
                at=_optional_text(entry.get("updated_at"))
                or _optional_text(entry.get("requested_at"))
                or _optional_text(entry.get("satisfied_at"))
                or _optional_text(entry.get("failed_at")),
                payload=_mapping_copy(entry.get("metadata") if isinstance(entry.get("metadata"), MappingABC) else {}),
            )
        )
    for task_state in task_states:
        if not task_state.is_blocked:
            continue
        wait_id = _composite_id(workset.workset_id, task_state.task_id, "task")
        if wait_id in seen_wait_ids:
            continue
        waits.append(
            WaitCondition(
                wait_id=wait_id,
                kind=f"task:{task_state.status}",
                reason=task_state.block_reason or task_state.detail,
                status="open",
                task_id=task_state.task_id,
                attempt_id=task_state.latest_attempt_id,
                workset_id=workset.workset_id,
                execution_id=execution_id,
                source="task-state",
                at=task_state.latest_result.recorded_at if task_state.latest_result else None,
                payload={
                    "claim_status": task_state.claim_status,
                    "approval_status": task_state.approval_status,
                },
            )
        )
    for message in control_messages:
        if message.status != "open":
            continue
        if message.kind not in {"pause", "stop", "request-input", "replan"}:
            continue
        wait_id = _composite_id(workset.workset_id, message.message_id, "control")
        if wait_id in seen_wait_ids:
            continue
        waits.append(
            WaitCondition(
                wait_id=wait_id,
                kind=f"control:{message.kind}",
                reason=message.body or message.kind or "control message",
                status="open",
                task_id=message.task_id,
                attempt_id=None,
                workset_id=workset.workset_id,
                execution_id=execution_id,
                source="control-message",
                at=message.at,
                payload={
                    "message_id": message.message_id,
                    "recipient": message.recipient,
                    "sender": message.sender,
                },
            )
        )
    return tuple(waits)


def project_workset_execution(
    *,
    workset: Workset,
    workspace: Workspace,
    task_attempts: Sequence[TaskAttempt],
    control_messages: Sequence[ControlMessage],
    wait_conditions: Sequence[WaitCondition],
    mode: str = "same-thread",
    execution_id: str | None = None,
) -> WorksetExecution:
    if execution_id is None:
        execution_id = _composite_id(workset.workset_id, workspace.branch or "workspace")
    active_task_ids = tuple(
        dict.fromkeys(
            attempt.task_id
            for attempt in task_attempts
            if attempt.status in {"prepared", "running", "blocked", "launch-failed", "partial"}
        )
    )
    if any(wait.status == "open" for wait in wait_conditions):
        status = "waiting"
    elif any(attempt.status in {"prepared", "running"} for attempt in task_attempts):
        status = "running"
    elif task_attempts and all(attempt.status in {"done", "released"} for attempt in task_attempts):
        status = "historical"
    else:
        status = "idle"
    started_candidates = [attempt.started_at for attempt in task_attempts if attempt.started_at]
    updated_candidates = [attempt.ended_at for attempt in task_attempts if attempt.ended_at]
    updated_candidates.extend(wait.at for wait in wait_conditions if wait.at)
    return WorksetExecution(
        execution_id=execution_id,
        workset_id=workset.workset_id,
        mode=mode,
        status=status,
        workspace=workspace,
        target_branch=workset.target_branch,
        started_at=min(started_candidates) if started_candidates else None,
        updated_at=max(updated_candidates) if updated_candidates else None,
        task_attempts=tuple(task_attempts),
        wait_conditions=tuple(wait_conditions),
        control_messages=tuple(control_messages),
    )


def project_runtime_model(
    profile: RepoProfile,
    snapshot: BacklogSnapshot,
    state: Mapping[str, Any],
    *,
    workspace: Workspace | None = None,
    events: Sequence[Mapping[str, Any]] | None = None,
    inbox: Sequence[Mapping[str, Any]] | None = None,
    results: Sequence[Mapping[str, Any]] | None = None,
    prompt_receipts: Sequence[PromptReceipt] = (),
    allow_high_risk: bool = False,
    workspace_mode: str = "git-worktree",
    execution_mode: str = "same-thread",
    execution_id: str | None = None,
) -> RuntimeModel:
    repository = project_repository(profile, integration_branches=(workspace.target_branch,) if workspace and workspace.target_branch else ())
    workspace = workspace or project_workspace(project_root=profile.paths.project_root, workspace_mode=workspace_mode)
    projected_events = tuple(project_event(row) for row in (events or load_events(profile.paths)))
    projected_results = tuple(project_result(row) for row in (results or load_task_results(profile.paths)))
    projected_control_messages = project_control_messages(inbox or load_inbox(profile.paths))
    state_attempts = tuple(
        dict(entry)
        for entry in (state.get("task_attempts", {}) or {}).values()
        if isinstance(entry, MappingABC)
    )
    projected_prompt_receipts = (
        tuple(prompt_receipts)
        if prompt_receipts
        else project_prompt_receipts_from_artifacts(state=state, results=projected_results, workspace=workspace)
    )
    task_attempts = project_task_attempts(
        results=projected_results,
        events=projected_events,
        workspace=workspace,
        state_attempts=state_attempts,
        prompt_receipts=projected_prompt_receipts,
    )
    task_states = project_task_states(
        snapshot,
        state,
        results=projected_results,
        task_attempts=task_attempts,
        allow_high_risk=allow_high_risk,
    )
    workset = project_workset(
        snapshot=snapshot,
        repository=repository,
        task_states=task_states,
        target_branch=workspace.target_branch,
    )
    wait_conditions = project_wait_conditions(
        workset=workset,
        task_states=task_states,
        control_messages=projected_control_messages,
        persisted_wait_conditions=tuple(
            dict(entry)
            for entry in (state.get("wait_conditions", {}) or {}).values()
            if isinstance(entry, MappingABC)
        ),
        execution_id=execution_id or _composite_id(workset.workset_id, workspace.branch or "workspace"),
    )
    workset_execution = project_workset_execution(
        workset=workset,
        workspace=workspace,
        task_attempts=task_attempts,
        control_messages=projected_control_messages,
        wait_conditions=wait_conditions,
        mode=execution_mode,
        execution_id=execution_id,
    )
    return RuntimeModel(
        repository=repository,
        workspace=workspace,
        workset=workset,
        task_states=task_states,
        task_attempts=task_attempts,
        workset_execution=workset_execution,
        prompt_receipts=projected_prompt_receipts,
        wait_conditions=wait_conditions,
        control_messages=projected_control_messages,
        results=projected_results,
        events=projected_events,
    )


def load_runtime_model(
    profile: RepoProfile,
    *,
    snapshot: BacklogSnapshot | None = None,
    state: Mapping[str, Any] | None = None,
    workspace: Workspace | None = None,
    workspace_mode: str = "git-worktree",
    execution_mode: str = "same-thread",
    allow_high_risk: bool = False,
) -> RuntimeModel:
    resolved_snapshot = snapshot or load_backlog(profile.paths, profile)
    resolved_state = state or load_state(profile.paths.state_file)
    return project_runtime_model(
        profile,
        resolved_snapshot,
        resolved_state,
        workspace=workspace,
        workspace_mode=workspace_mode,
        execution_mode=execution_mode,
        allow_high_risk=allow_high_risk,
    )


def load_current_artifacts(
    profile: RepoProfile,
    *,
    workspace: Workspace | None = None,
    allow_high_risk: bool = False,
) -> RuntimeModel:
    return load_runtime_model(
        profile,
        workspace=workspace,
        allow_high_risk=allow_high_risk,
    )


__all__ = [
    "ControlMessage",
    "Event",
    "PromptReceipt",
    "Repository",
    "Result",
    "RuntimeModel",
    "RuntimeModelError",
    "TaskAttempt",
    "TaskState",
    "WaitCondition",
    "Workset",
    "WorksetExecution",
    "Workspace",
    "load_current_artifacts",
    "load_runtime_model",
    "project_control_messages",
    "project_event",
    "project_prompt_receipt",
    "project_repository",
    "project_result",
    "project_runtime_model",
    "project_task_attempts",
    "project_task_state",
    "project_task_states",
    "project_wait_conditions",
    "project_workset",
    "project_workset_execution",
    "project_workspace",
]
