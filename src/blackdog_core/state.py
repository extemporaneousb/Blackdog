"""Canonical mutable state and append-only record helpers for Blackdog."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator
import fcntl
import json
import os
import tempfile
import uuid

from .profile import BlackdogPaths


class StoreError(RuntimeError):
    pass


APPROVAL_STATE_ABSENT = "absent"
APPROVAL_STATUS_PENDING = "pending"
APPROVAL_STATUS_APPROVED = "approved"
APPROVAL_STATUS_DENIED = "denied"
APPROVAL_STATUS_DEFERRED = "deferred"
APPROVAL_STATUS_DONE = "done"
APPROVAL_STATUSES = frozenset(
    {
        APPROVAL_STATUS_PENDING,
        APPROVAL_STATUS_APPROVED,
        APPROVAL_STATUS_DENIED,
        APPROVAL_STATUS_DEFERRED,
        APPROVAL_STATUS_DONE,
    }
)
APPROVAL_STATE_MACHINE_STATES = frozenset({APPROVAL_STATE_ABSENT, *APPROVAL_STATUSES})
APPROVAL_SATISFIED_STATUSES = frozenset({APPROVAL_STATUS_APPROVED, APPROVAL_STATUS_DONE})

CLAIM_STATE_ABSENT = "absent"
CLAIM_STATUS_CLAIMED = "claimed"
CLAIM_STATUS_RELEASED = "released"
CLAIM_STATUS_DONE = "done"
CLAIM_STATUSES = frozenset({CLAIM_STATUS_CLAIMED, CLAIM_STATUS_RELEASED, CLAIM_STATUS_DONE})
CLAIM_STATE_MACHINE_STATES = frozenset({CLAIM_STATE_ABSENT, *CLAIM_STATUSES})
CLAIM_ACTIVE_STATUSES = frozenset({CLAIM_STATUS_CLAIMED})
CLAIM_TERMINAL_STATUSES = frozenset({CLAIM_STATUS_DONE})

INBOX_ACTION_MESSAGE = "message"
INBOX_ACTION_RESOLVE = "resolve"
INBOX_ACTIONS = frozenset({INBOX_ACTION_MESSAGE, INBOX_ACTION_RESOLVE})
INBOX_STATUS_OPEN = "open"
INBOX_STATUS_RESOLVED = "resolved"
INBOX_STATE_MACHINE_STATES = frozenset({INBOX_STATUS_OPEN, INBOX_STATUS_RESOLVED})
INBOX_MESSAGE_REQUIRED_FIELDS = ("sender", "recipient", "kind")
INBOX_RESOLVE_REQUIRED_FIELDS = ("actor",)

CONTROL_ACTION_STOP = "stop"
CONTROL_ACTION_RESUME = "resume"
CONTROL_ACTION_PAUSE = "pause"
CONTROL_ACTION_REPLAN = "replan"
CONTROL_ACTION_TAKEOVER = "takeover"
CONTROL_ACTION_STATUS = "status"
CONTROL_ACTION_NEEDS_INPUT = "needs-input"
CONTROL_ACTION_NOTE = "note"
CONTROL_ACTIONS = frozenset(
    {
        CONTROL_ACTION_STOP,
        CONTROL_ACTION_RESUME,
        CONTROL_ACTION_PAUSE,
        CONTROL_ACTION_REPLAN,
        CONTROL_ACTION_TAKEOVER,
        CONTROL_ACTION_STATUS,
        CONTROL_ACTION_NEEDS_INPUT,
        CONTROL_ACTION_NOTE,
    }
)
CONTROL_ACTION_ALIASES = {
    "status-request": CONTROL_ACTION_STATUS,
    "status_request": CONTROL_ACTION_STATUS,
    "inspect": CONTROL_ACTION_STATUS,
    "request-input": CONTROL_ACTION_NEEDS_INPUT,
    "request_input": CONTROL_ACTION_NEEDS_INPUT,
    "needs_input": CONTROL_ACTION_NEEDS_INPUT,
}

TASK_ATTEMPT_STATE_ABSENT = "absent"
TASK_ATTEMPT_STATUS_PREPARED = "prepared"
TASK_ATTEMPT_STATUS_RUNNING = "running"
TASK_ATTEMPT_STATUS_WAITING = "waiting"
TASK_ATTEMPT_STATUS_BLOCKED = "blocked"
TASK_ATTEMPT_STATUS_INTERRUPTED = "interrupted"
TASK_ATTEMPT_STATUS_FAILED = "failed"
TASK_ATTEMPT_STATUS_DONE = "done"
TASK_ATTEMPT_STATUS_UNKNOWN = "unknown"
TASK_ATTEMPT_STATUSES = frozenset(
    {
        TASK_ATTEMPT_STATUS_PREPARED,
        TASK_ATTEMPT_STATUS_RUNNING,
        TASK_ATTEMPT_STATUS_WAITING,
        TASK_ATTEMPT_STATUS_BLOCKED,
        TASK_ATTEMPT_STATUS_INTERRUPTED,
        TASK_ATTEMPT_STATUS_FAILED,
        TASK_ATTEMPT_STATUS_DONE,
        TASK_ATTEMPT_STATUS_UNKNOWN,
    }
)
TASK_ATTEMPT_ACTIVE_STATUSES = frozenset(
    {
        TASK_ATTEMPT_STATUS_PREPARED,
        TASK_ATTEMPT_STATUS_RUNNING,
        TASK_ATTEMPT_STATUS_WAITING,
    }
)
TASK_ATTEMPT_TERMINAL_STATUSES = frozenset(
    {
        TASK_ATTEMPT_STATUS_BLOCKED,
        TASK_ATTEMPT_STATUS_INTERRUPTED,
        TASK_ATTEMPT_STATUS_FAILED,
        TASK_ATTEMPT_STATUS_DONE,
    }
)

WAIT_CONDITION_STATE_ABSENT = "absent"
WAIT_CONDITION_STATUS_WAITING = "waiting"
WAIT_CONDITION_STATUS_SATISFIED = "satisfied"
WAIT_CONDITION_STATUS_BLOCKED = "blocked"
WAIT_CONDITION_STATUS_CANCELLED = "cancelled"
WAIT_CONDITION_STATUS_FAILED = "failed"
WAIT_CONDITION_STATUS_UNKNOWN = "unknown"
WAIT_CONDITION_KIND_CHILD_PROCESS = "child-process"
WAIT_CONDITION_KIND_LAND = "land"
WAIT_CONDITION_KIND_CONTROL = "control"
WAIT_CONDITION_KIND_RESULT = "result"
WAIT_CONDITION_KIND_INPUT = "input"
WAIT_CONDITION_KIND_APPROVAL = "approval"
WAIT_CONDITION_KIND_RECOVERY = "recovery"
WAIT_CONDITION_KIND_UNKNOWN = "unknown"
WAIT_CONDITION_KINDS = frozenset(
    {
        WAIT_CONDITION_KIND_CHILD_PROCESS,
        WAIT_CONDITION_KIND_LAND,
        WAIT_CONDITION_KIND_CONTROL,
        WAIT_CONDITION_KIND_RESULT,
        WAIT_CONDITION_KIND_INPUT,
        WAIT_CONDITION_KIND_APPROVAL,
        WAIT_CONDITION_KIND_RECOVERY,
        WAIT_CONDITION_KIND_UNKNOWN,
    }
)
WAIT_CONDITION_KIND_ALIASES = {
    "child": WAIT_CONDITION_KIND_CHILD_PROCESS,
    "child_process": WAIT_CONDITION_KIND_CHILD_PROCESS,
    "launch": WAIT_CONDITION_KIND_CHILD_PROCESS,
    "landing": WAIT_CONDITION_KIND_LAND,
    "landed": WAIT_CONDITION_KIND_LAND,
    "control-message": WAIT_CONDITION_KIND_CONTROL,
    "message": WAIT_CONDITION_KIND_CONTROL,
    "result-record": WAIT_CONDITION_KIND_RESULT,
    "result_record": WAIT_CONDITION_KIND_RESULT,
    "input-required": WAIT_CONDITION_KIND_INPUT,
    "input_required": WAIT_CONDITION_KIND_INPUT,
    "needs-input": WAIT_CONDITION_KIND_INPUT,
    "approval-required": WAIT_CONDITION_KIND_APPROVAL,
    "approval_required": WAIT_CONDITION_KIND_APPROVAL,
    "recovery-needed": WAIT_CONDITION_KIND_RECOVERY,
    "recovery_needed": WAIT_CONDITION_KIND_RECOVERY,
}
WAIT_CONDITION_STATUSES = frozenset(
    {
        WAIT_CONDITION_STATUS_WAITING,
        WAIT_CONDITION_STATUS_SATISFIED,
        WAIT_CONDITION_STATUS_BLOCKED,
        WAIT_CONDITION_STATUS_CANCELLED,
        WAIT_CONDITION_STATUS_FAILED,
        WAIT_CONDITION_STATUS_UNKNOWN,
    }
)
WAIT_CONDITION_ACTIVE_STATUSES = frozenset({WAIT_CONDITION_STATUS_WAITING})
WAIT_CONDITION_TERMINAL_STATUSES = frozenset(
    {
        WAIT_CONDITION_STATUS_SATISFIED,
        WAIT_CONDITION_STATUS_BLOCKED,
        WAIT_CONDITION_STATUS_CANCELLED,
        WAIT_CONDITION_STATUS_FAILED,
    }
)


def normalize_wait_condition_kind(value: Any, *, source: Path) -> str:
    kind = _normalized_optional_string(value)
    if kind is None:
        raise StoreError(f"wait condition kind is required in {source}")
    kind = WAIT_CONDITION_KIND_ALIASES.get(kind, kind)
    if kind not in WAIT_CONDITION_KINDS:
        raise StoreError(f"wait condition kind must be one of {sorted(WAIT_CONDITION_KINDS)} in {source}")
    return kind


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "approval_tasks": {},
        "task_claims": {},
        "task_attempts": {},
        "wait_conditions": {},
    }


def _normalized_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_string_list(value: Any, *, field: str, source: Path) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise StoreError(f"{field} must be a list in {source}")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise StoreError(f"{field} must contain strings in {source}")
        text = item.strip()
        if text:
            normalized.append(text)
    return normalized


def _normalize_optional_object(value: Any, *, field: str, source: Path) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise StoreError(f"{field} must be an object in {source}")
    return dict(value)


def _normalize_positive_int(value: Any, *, field: str, source: Path) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"{field} must be an integer in {source}") from exc
    if normalized < 1:
        raise StoreError(f"{field} must be positive in {source}")
    return normalized


def _normalize_non_negative_int(value: Any, *, field: str, source: Path) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"{field} must be an integer in {source}") from exc
    if normalized < 0:
        raise StoreError(f"{field} must be non-negative in {source}")
    return normalized


def _normalize_optional_bool(value: Any, *, field: str, source: Path) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise StoreError(f"{field} must be a boolean in {source}")


def _normalize_choice(
    value: Any,
    *,
    field: str,
    source: Path,
    allowed: frozenset[str],
    default: str | None = None,
) -> str:
    normalized = _normalized_optional_string(value)
    if normalized is None:
        if default is None:
            raise StoreError(f"{field} is required in {source}")
        normalized = default
    if normalized not in allowed:
        raise StoreError(f"{field} must be one of {sorted(allowed)} in {source}")
    return normalized


def approval_is_satisfied(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return False
    return str(entry.get("status") or "").strip() in APPROVAL_SATISFIED_STATUSES


def claim_is_done(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return False
    return str(entry.get("status") or "").strip() in CLAIM_TERMINAL_STATUSES


def normalize_approval_entry(task_id: str, entry: dict[str, Any], *, state_file: Path) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise StoreError(f"approval_tasks[{task_id}] must be an object in {state_file}")
    normalized = dict(entry)
    status = str(normalized.get("status") or APPROVAL_STATUS_PENDING).strip()
    if status not in APPROVAL_STATUSES:
        raise StoreError(
            f"approval_tasks[{task_id}].status must be one of {sorted(APPROVAL_STATUSES)} in {state_file}"
        )
    normalized["status"] = status
    first_seen = _normalized_optional_string(normalized.get("first_seen"))
    if first_seen is not None:
        normalized["first_seen"] = first_seen
    else:
        normalized.pop("first_seen", None)
    last_seen = _normalized_optional_string(normalized.get("last_seen"))
    if last_seen is not None:
        normalized["last_seen"] = last_seen
    else:
        normalized.pop("last_seen", None)
    title = _normalized_optional_string(normalized.get("title"))
    if title is not None:
        normalized["title"] = title
    else:
        normalized.pop("title", None)
    bucket = _normalized_optional_string(normalized.get("bucket"))
    if bucket is not None:
        normalized["bucket"] = bucket
    else:
        normalized.pop("bucket", None)
    approval_reason = _normalized_optional_string(normalized.get("approval_reason"))
    if approval_reason is not None:
        normalized["approval_reason"] = approval_reason
    else:
        normalized.pop("approval_reason", None)
    normalized["paths"] = _normalize_string_list(normalized.get("paths"), field=f"approval_tasks[{task_id}].paths", source=state_file)
    return normalized


def normalize_claim_entry(task_id: str, entry: dict[str, Any], *, state_file: Path) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise StoreError(f"task_claims[{task_id}] must be an object in {state_file}")
    normalized = dict(entry)
    status = str(normalized.get("status") or "").strip()
    if status not in CLAIM_STATUSES:
        raise StoreError(f"task_claims[{task_id}].status must be one of {sorted(CLAIM_STATUSES)} in {state_file}")
    normalized["status"] = status
    for field in (
        "title",
        "bucket",
        "priority",
        "risk",
        "attempt_id",
        "run_id",
        "workspace",
        "workspace_mode",
        "branch",
        "target_branch",
        "wait_condition_id",
        "control_message_id",
        "claimed_by",
        "claimed_at",
        "released_by",
        "released_at",
        "release_note",
        "completed_by",
        "completed_at",
        "completion_note",
    ):
        value = _normalized_optional_string(normalized.get(field))
        if value is not None:
            normalized[field] = value
        else:
            normalized.pop(field, None)
    normalized["paths"] = _normalize_string_list(normalized.get("paths"), field=f"task_claims[{task_id}].paths", source=state_file)
    normalized.pop("claim_expires_at", None)
    if status != CLAIM_STATUS_CLAIMED:
        normalized.pop("claimed_pid", None)
        normalized.pop("claimed_process_missing_scans", None)
        normalized.pop("claimed_process_last_seen_at", None)
        normalized.pop("claimed_process_last_checked_at", None)
        return normalized
    if "claimed_pid" in normalized and normalized.get("claimed_pid") is not None:
        normalized["claimed_pid"] = _normalize_positive_int(
            normalized.get("claimed_pid"),
            field=f"task_claims[{task_id}].claimed_pid",
            source=state_file,
        )
    else:
        normalized.pop("claimed_pid", None)
    if "claimed_process_missing_scans" in normalized and normalized.get("claimed_process_missing_scans") is not None:
        normalized["claimed_process_missing_scans"] = _normalize_non_negative_int(
            normalized.get("claimed_process_missing_scans"),
            field=f"task_claims[{task_id}].claimed_process_missing_scans",
            source=state_file,
        )
    else:
        normalized.pop("claimed_process_missing_scans", None)
    for field in ("claimed_process_last_seen_at", "claimed_process_last_checked_at"):
        value = _normalized_optional_string(normalized.get(field))
        if value is not None:
            normalized[field] = value
        else:
            normalized.pop(field, None)
    return normalized


def normalize_event_row(payload: dict[str, Any], *, events_file: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StoreError(f"Event row must be an object in {events_file}")
    normalized = dict(payload)
    event_id = _normalized_optional_string(normalized.get("event_id"))
    if event_id is None:
        raise StoreError(f"event_id is required in {events_file}")
    event_type = _normalized_optional_string(normalized.get("type"))
    if event_type is None:
        raise StoreError(f"type is required in {events_file}")
    actor = _normalized_optional_string(normalized.get("actor"))
    if actor is None:
        raise StoreError(f"actor is required in {events_file}")
    at = _normalized_optional_string(normalized.get("at"))
    if at is None:
        raise StoreError(f"at is required in {events_file}")
    normalized["event_id"] = event_id
    normalized["type"] = event_type
    normalized["actor"] = actor
    normalized["at"] = at
    normalized["task_id"] = _normalized_optional_string(normalized.get("task_id"))
    normalized["attempt_id"] = _normalized_optional_string(normalized.get("attempt_id"))
    normalized["run_id"] = _normalized_optional_string(normalized.get("run_id"))
    normalized["wait_condition_id"] = _normalized_optional_string(normalized.get("wait_condition_id"))
    normalized["control_message_id"] = _normalized_optional_string(normalized.get("control_message_id"))
    normalized["payload"] = _normalize_optional_object(normalized.get("payload"), field="payload", source=events_file)
    return normalized


def _infer_control_action_from_message(
    *,
    control_action: str | None,
    kind: str,
    tags: list[str],
    body: str,
) -> str | None:
    if control_action:
        return control_action
    normalized_kind = kind.strip().lower()
    if normalized_kind == CONTROL_ACTION_NOTE:
        return CONTROL_ACTION_NOTE
    normalized_tags = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
    if CONTROL_ACTION_STOP in normalized_tags or body.startswith("stop"):
        return CONTROL_ACTION_STOP
    for candidate in (
        CONTROL_ACTION_RESUME,
        CONTROL_ACTION_PAUSE,
        CONTROL_ACTION_REPLAN,
        CONTROL_ACTION_TAKEOVER,
        CONTROL_ACTION_STATUS,
        CONTROL_ACTION_NEEDS_INPUT,
    ):
        if candidate in normalized_tags or body.startswith(candidate):
            return candidate
    return None


def normalize_control_action(value: Any, *, inbox_file: Path) -> str:
    action = _normalized_optional_string(value)
    if action is None:
        raise StoreError(f"control_action is required in {inbox_file}")
    action = CONTROL_ACTION_ALIASES.get(action, action)
    if action not in CONTROL_ACTIONS:
        raise StoreError(f"control_action must be one of {sorted(CONTROL_ACTIONS)} in {inbox_file}")
    return action


def normalize_inbox_row(payload: dict[str, Any], *, inbox_file: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StoreError(f"Inbox row must be an object in {inbox_file}")
    normalized = dict(payload)
    action = _normalized_optional_string(normalized.get("action"))
    if action not in INBOX_ACTIONS:
        raise StoreError(
            f"action must be one of {sorted(INBOX_ACTIONS)} in {inbox_file}"
        )
    message_id = _normalized_optional_string(normalized.get("message_id"))
    if message_id is None:
        raise StoreError(f"message_id is required in {inbox_file}")
    at = _normalized_optional_string(normalized.get("at"))
    if at is None:
        raise StoreError(f"at is required in {inbox_file}")
    normalized["action"] = action
    normalized["message_id"] = message_id
    normalized["at"] = at
    if action == INBOX_ACTION_MESSAGE:
        for field in INBOX_MESSAGE_REQUIRED_FIELDS:
            value = _normalized_optional_string(normalized.get(field))
            if value is None:
                raise StoreError(f"{field} is required for message rows in {inbox_file}")
            normalized[field] = value
        normalized["task_id"] = _normalized_optional_string(normalized.get("task_id"))
        normalized["reply_to"] = _normalized_optional_string(normalized.get("reply_to"))
        normalized["body"] = str(normalized.get("body") or "")
        normalized["tags"] = _normalize_string_list(normalized.get("tags"), field="tags", source=inbox_file)
        control_action = _normalized_optional_string(normalized.get("control_action"))
        control_action = _infer_control_action_from_message(
            control_action=control_action,
            kind=normalized["kind"],
            tags=normalized["tags"],
            body=normalized["body"].strip().lower(),
        )
        if control_action is not None:
            normalized["control_action"] = normalize_control_action(control_action, inbox_file=inbox_file)
        else:
            normalized.pop("control_action", None)
        for field in ("control_scope", "control_target", "control_state", "control_reason"):
            value = _normalized_optional_string(normalized.get(field))
            if value is not None:
                normalized[field] = value
            else:
                normalized.pop(field, None)
        return normalized
    for field in INBOX_RESOLVE_REQUIRED_FIELDS:
        value = _normalized_optional_string(normalized.get(field))
        if value is None:
            raise StoreError(f"{field} is required for resolve rows in {inbox_file}")
        normalized[field] = value
    normalized["note"] = str(normalized.get("note") or "")
    return normalized


def control_message_action(message: dict[str, Any]) -> str | None:
    if not isinstance(message, dict):
        return None
    action = _normalized_optional_string(message.get("control_action"))
    if action is not None:
        action = CONTROL_ACTION_ALIASES.get(action, action)
        return action if action in CONTROL_ACTIONS else None
    if str(message.get("action") or "").strip() != INBOX_ACTION_MESSAGE:
        return None
    kind = str(message.get("kind") or "")
    tags = list(message.get("tags") or [])
    body = str(message.get("body") or "").strip().lower()
    action = _infer_control_action_from_message(
        control_action=None,
        kind=kind,
        tags=tags if isinstance(tags, list) else [],
        body=body,
    )
    return action if action in CONTROL_ACTIONS else None


def normalize_prompt_receipt(payload: dict[str, Any] | None, *, source: Path, field: str = "prompt_receipt") -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise StoreError(f"{field} must be an object in {source}")
    normalized = dict(payload)
    prompt_text = _normalized_optional_string(normalized.get("prompt_text"))
    if prompt_text is None:
        prompt_text = _normalized_optional_string(normalized.get("text"))
    if prompt_text is None:
        raise StoreError(f"{field}.prompt_text is required in {source}")
    normalized["prompt_text"] = prompt_text
    normalized["text"] = prompt_text
    for key in (
        "prompt_hash",
        "template_hash",
        "template_version",
        "prompt_file",
        "attempt_id",
        "task_id",
        "run_id",
        "child_agent",
        "workspace",
        "workspace_mode",
        "protocol_command",
        "launch_command_strategy",
        "recorded_at",
        "launch_time",
        "branch",
        "target_branch",
        "wait_condition_id",
        "control_message_id",
    ):
        value = _normalized_optional_string(normalized.get(key))
        if value is not None:
            normalized[key] = value
        else:
            normalized.pop(key, None)
    if "launch_command" in normalized and normalized.get("launch_command") is not None:
        normalized["launch_command"] = _normalize_string_list(
            normalized.get("launch_command"),
            field=f"{field}.launch_command",
            source=source,
        )
    else:
        normalized.pop("launch_command", None)
    for key in ("launch_settings", "worktree_spec", "telemetry"):
        if key in normalized and normalized[key] is not None:
            normalized[key] = _normalize_optional_object(normalized.get(key), field=f"{field}.{key}", source=source)
        else:
            normalized.pop(key, None)
    return normalized


def normalize_task_attempt_entry(attempt_id: str, entry: dict[str, Any], *, state_file: Path) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise StoreError(f"task_attempts[{attempt_id}] must be an object in {state_file}")
    normalized = dict(entry)
    normalized["attempt_id"] = attempt_id
    task_id = _normalized_optional_string(normalized.get("task_id"))
    if task_id is None:
        raise StoreError(f"task_attempts[{attempt_id}].task_id is required in {state_file}")
    normalized["task_id"] = task_id
    status = _normalized_optional_string(normalized.get("status"))
    if status is None:
        status = TASK_ATTEMPT_STATUS_UNKNOWN
    if status not in TASK_ATTEMPT_STATUSES:
        raise StoreError(
            f"task_attempts[{attempt_id}].status must be one of {sorted(TASK_ATTEMPT_STATUSES)} in {state_file}"
        )
    normalized["status"] = status
    for field in (
        "run_id",
        "actor",
        "child_agent",
        "workspace",
        "workspace_mode",
        "branch",
        "target_branch",
        "prompt_file",
        "run_dir",
        "result_file",
        "result_status",
        "final_task_status",
        "launch_command_strategy",
        "prompt_hash",
        "wait_condition_id",
        "control_message_id",
        "landed_commit",
        "launch_error",
        "land_error",
    ):
        value = _normalized_optional_string(normalized.get(field))
        if value is not None:
            normalized[field] = value
        else:
            normalized.pop(field, None)
    for field in ("landed", "result_recorded", "branch_ahead"):
        value = _normalize_optional_bool(
            normalized.get(field),
            field=f"task_attempts[{attempt_id}].{field}",
            source=state_file,
        )
        if value is not None:
            normalized[field] = value
        else:
            normalized.pop(field, None)
    for field in ("attempted_at", "launched_at", "started_at", "updated_at", "completed_at", "resolved_at"):
        value = _normalized_optional_string(normalized.get(field))
        if value is not None:
            normalized[field] = value
        else:
            normalized.pop(field, None)
    if "launch_command" in normalized and normalized.get("launch_command") is not None:
        normalized["launch_command"] = _normalize_string_list(
            normalized.get("launch_command"),
            field=f"task_attempts[{attempt_id}].launch_command",
            source=state_file,
        )
    else:
        normalized.pop("launch_command", None)
    if "launch_settings" in normalized and normalized.get("launch_settings") is not None:
        normalized["launch_settings"] = _normalize_optional_object(
            normalized.get("launch_settings"),
            field=f"task_attempts[{attempt_id}].launch_settings",
            source=state_file,
        )
    else:
        normalized.pop("launch_settings", None)
    if "metadata" in normalized and normalized.get("metadata") is not None:
        normalized["metadata"] = _normalize_optional_object(
            normalized.get("metadata"),
            field=f"task_attempts[{attempt_id}].metadata",
            source=state_file,
        )
    else:
        normalized.pop("metadata", None)
    normalized["prompt_receipt"] = normalize_prompt_receipt(
        normalized.get("prompt_receipt"),
        source=state_file,
        field=f"task_attempts[{attempt_id}].prompt_receipt",
    )
    if not normalized["prompt_receipt"]:
        normalized.pop("prompt_receipt", None)
    return normalized


def normalize_wait_condition_entry(wait_id: str, entry: dict[str, Any], *, state_file: Path) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise StoreError(f"wait_conditions[{wait_id}] must be an object in {state_file}")
    normalized = dict(entry)
    normalized["wait_id"] = wait_id
    normalized["kind"] = normalize_wait_condition_kind(normalized.get("kind"), source=state_file)
    status = _normalized_optional_string(normalized.get("status"))
    if status is None:
        status = WAIT_CONDITION_STATUS_WAITING
    if status not in WAIT_CONDITION_STATUSES:
        raise StoreError(
            f"wait_conditions[{wait_id}].status must be one of {sorted(WAIT_CONDITION_STATUSES)} in {state_file}"
        )
    normalized["status"] = status
    for field in (
        "task_id",
        "attempt_id",
        "run_id",
        "actor",
        "child_agent",
        "workspace",
        "workspace_mode",
        "reason",
        "detail",
        "resume_hint",
        "blocked_by",
        "control_message_id",
    ):
        value = _normalized_optional_string(normalized.get(field))
        if value is not None:
            normalized[field] = value
        else:
            normalized.pop(field, None)
    for field in ("requested_at", "updated_at", "satisfied_at", "blocked_at", "cancelled_at", "failed_at"):
        value = _normalized_optional_string(normalized.get(field))
        if value is not None:
            normalized[field] = value
        else:
            normalized.pop(field, None)
    if "metadata" in normalized and normalized.get("metadata") is not None:
        normalized["metadata"] = _normalize_optional_object(
            normalized.get("metadata"),
            field=f"wait_conditions[{wait_id}].metadata",
            source=state_file,
        )
    else:
        normalized.pop("metadata", None)
    return normalized


def normalize_task_result(payload: dict[str, Any], *, result_file: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StoreError(f"Task-result payload must be an object: {result_file}")
    normalized = dict(payload)
    normalized["schema_version"] = int(normalized.get("schema_version") or 1)
    for field in ("task_id", "recorded_at", "actor", "run_id", "status"):
        value = _normalized_optional_string(normalized.get(field))
        if value is None:
            raise StoreError(f"{field} is required in {result_file}")
        normalized[field] = value
    for field in ("attempt_id", "wait_condition_id", "control_message_id"):
        value = _normalized_optional_string(normalized.get(field))
        if value is not None:
            normalized[field] = value
        else:
            normalized.pop(field, None)
    for field in ("what_changed", "validation", "residual", "followup_candidates"):
        normalized[field] = _normalize_string_list(normalized.get(field), field=field, source=result_file)
    if not isinstance(normalized.get("needs_user_input"), bool):
        raise StoreError(f"needs_user_input must be a boolean in {result_file}")
    normalized["metadata"] = _normalize_optional_object(normalized.get("metadata"), field="metadata", source=result_file)
    if normalized.get("prompt_receipt"):
        normalized["prompt_receipt"] = normalize_prompt_receipt(
            normalized.get("prompt_receipt"),
            source=result_file,
            field="prompt_receipt",
        )
    else:
        normalized.pop("prompt_receipt", None)
    normalized["task_shaping_telemetry"] = _normalize_optional_object(
        normalized.get("task_shaping_telemetry"),
        field="task_shaping_telemetry",
        source=result_file,
    )
    return normalized


def normalize_state(payload: dict[str, Any], *, state_file: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StoreError(f"State file must be a JSON object: {state_file}")
    payload.setdefault("schema_version", 1)
    payload.setdefault("approval_tasks", {})
    payload.setdefault("task_claims", {})
    payload.setdefault("task_attempts", {})
    payload.setdefault("wait_conditions", {})
    if not isinstance(payload["approval_tasks"], dict):
        raise StoreError(f"approval_tasks must be an object in {state_file}")
    if not isinstance(payload["task_claims"], dict):
        raise StoreError(f"task_claims must be an object in {state_file}")
    if not isinstance(payload["task_attempts"], dict):
        raise StoreError(f"task_attempts must be an object in {state_file}")
    if not isinstance(payload["wait_conditions"], dict):
        raise StoreError(f"wait_conditions must be an object in {state_file}")
    payload["approval_tasks"] = {
        str(task_id): normalize_approval_entry(str(task_id), entry, state_file=state_file)
        for task_id, entry in payload["approval_tasks"].items()
    }
    payload["task_claims"] = {
        str(task_id): normalize_claim_entry(str(task_id), entry, state_file=state_file)
        for task_id, entry in payload["task_claims"].items()
    }
    payload["task_attempts"] = {
        str(attempt_id): normalize_task_attempt_entry(str(attempt_id), entry, state_file=state_file)
        for attempt_id, entry in payload["task_attempts"].items()
    }
    payload["wait_conditions"] = {
        str(wait_id): normalize_wait_condition_entry(str(wait_id), entry, state_file=state_file)
        for wait_id, entry in payload["wait_conditions"].items()
    }
    return payload


def load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return default_state()
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreError(f"Invalid JSON in {state_file}: {exc}") from exc
    return normalize_state(payload, state_file=state_file)


def load_task_attempts(state: dict[str, Any], *, state_file: Path) -> dict[str, dict[str, Any]]:
    attempts = state.get("task_attempts", {})
    if not isinstance(attempts, dict):
        raise StoreError(f"task_attempts must be an object in {state_file}")
    return {
        str(attempt_id): normalize_task_attempt_entry(str(attempt_id), entry, state_file=state_file)
        for attempt_id, entry in attempts.items()
        if isinstance(entry, dict)
    }


def load_wait_conditions(state: dict[str, Any], *, state_file: Path) -> dict[str, dict[str, Any]]:
    wait_conditions = state.get("wait_conditions", {})
    if not isinstance(wait_conditions, dict):
        raise StoreError(f"wait_conditions must be an object in {state_file}")
    return {
        str(wait_id): normalize_wait_condition_entry(str(wait_id), entry, state_file=state_file)
        for wait_id, entry in wait_conditions.items()
        if isinstance(entry, dict)
    }


def task_attempt_is_active(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return False
    return str(entry.get("status") or "").strip() in TASK_ATTEMPT_ACTIVE_STATUSES


def wait_condition_is_active(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return False
    return str(entry.get("status") or "").strip() in WAIT_CONDITION_ACTIVE_STATUSES


def upsert_task_attempt(
    state: dict[str, Any],
    attempt_id: str,
    entry: dict[str, Any],
    *,
    state_file: Path,
) -> dict[str, Any]:
    attempts = state.setdefault("task_attempts", {})
    if not isinstance(attempts, dict):
        raise StoreError(f"task_attempts must be an object in {state_file}")
    normalized = normalize_task_attempt_entry(attempt_id, entry, state_file=state_file)
    attempts[attempt_id] = normalized
    state["task_attempts"] = attempts
    return normalized


def upsert_wait_condition(
    state: dict[str, Any],
    wait_id: str,
    entry: dict[str, Any],
    *,
    state_file: Path,
) -> dict[str, Any]:
    wait_conditions = state.setdefault("wait_conditions", {})
    if not isinstance(wait_conditions, dict):
        raise StoreError(f"wait_conditions must be an object in {state_file}")
    normalized = normalize_wait_condition_entry(wait_id, entry, state_file=state_file)
    wait_conditions[wait_id] = normalized
    state["wait_conditions"] = wait_conditions
    return normalized


def close_wait_condition(
    state: dict[str, Any],
    wait_id: str,
    *,
    state_file: Path,
    status: str = WAIT_CONDITION_STATUS_SATISFIED,
    reason: str | None = None,
    detail: str | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    wait_conditions = state.setdefault("wait_conditions", {})
    if not isinstance(wait_conditions, dict):
        raise StoreError(f"wait_conditions must be an object in {state_file}")
    entry = dict(wait_conditions.get(wait_id) or {})
    entry["status"] = status
    if reason is not None:
        entry["reason"] = reason
    if detail is not None:
        entry["detail"] = detail
    timestamp = updated_at or now_iso()
    entry["updated_at"] = timestamp
    if status == WAIT_CONDITION_STATUS_SATISFIED:
        entry.setdefault("satisfied_at", timestamp)
    elif status == WAIT_CONDITION_STATUS_BLOCKED:
        entry.setdefault("blocked_at", timestamp)
    elif status == WAIT_CONDITION_STATUS_CANCELLED:
        entry.setdefault("cancelled_at", timestamp)
    elif status == WAIT_CONDITION_STATUS_FAILED:
        entry.setdefault("failed_at", timestamp)
    return upsert_wait_condition(state, wait_id, entry, state_file=state_file)


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextmanager
def locked_path(path: Path) -> Iterator[None]:
    lock_path = _lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    before_replace: Callable[[Path], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace(temp_path)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    payload = json.dumps(normalize_state(dict(state), state_file=state_file), indent=2, sort_keys=True) + "\n"
    with locked_path(state_file):
        atomic_write_text(state_file, payload)


@contextmanager
def locked_state(state_file: Path) -> Iterator[dict[str, Any]]:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with locked_path(state_file):
        if not state_file.exists():
            atomic_write_text(state_file, json.dumps(default_state(), indent=2, sort_keys=True) + "\n")
        raw = state_file.read_text(encoding="utf-8").strip()
        payload = default_state() if not raw else json.loads(raw)
        state = normalize_state(payload, state_file=state_file)
        yield state
        atomic_write_text(state_file, json.dumps(state, indent=2, sort_keys=True) + "\n")


def claim_is_active(entry: dict[str, Any]) -> bool:
    return isinstance(entry, dict) and str(entry.get("status") or "").strip() in CLAIM_ACTIVE_STATUSES


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    row = json.dumps(payload, sort_keys=True) + "\n"
    with locked_path(path):
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        atomic_write_text(path, existing + row)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StoreError(f"Invalid JSONL in {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise StoreError(f"JSONL row must be an object in {path}")
        rows.append(payload)
    return rows


def append_event(
    paths: BlackdogPaths,
    *,
    event_type: str,
    actor: str,
    task_id: str | None = None,
    attempt_id: str | None = None,
    run_id: str | None = None,
    wait_condition_id: str | None = None,
    control_message_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = normalize_event_row(
        {
            "event_id": uuid.uuid4().hex,
            "type": event_type,
            "at": now_iso(),
            "actor": actor,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "run_id": run_id,
            "wait_condition_id": wait_condition_id,
            "control_message_id": control_message_id,
            "payload": payload or {},
        },
        events_file=paths.events_file,
    )
    append_jsonl(paths.events_file, event)
    return event


def record_comment(
    paths: BlackdogPaths,
    *,
    actor: str,
    body: str,
    task_id: str | None = None,
    kind: str = "comment",
) -> dict[str, Any]:
    return append_event(
        paths,
        event_type="comment",
        actor=actor,
        task_id=task_id,
        payload={"kind": kind, "body": body},
    )


def load_events(
    paths: BlackdogPaths,
    *,
    task_id: str | None = None,
    attempt_id: str | None = None,
    run_id: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = [normalize_event_row(row, events_file=paths.events_file) for row in load_jsonl(paths.events_file)]
    if task_id:
        rows = [row for row in rows if row.get("task_id") == task_id]
    if attempt_id:
        rows = [row for row in rows if row.get("attempt_id") == attempt_id]
    if run_id:
        rows = [row for row in rows if row.get("run_id") == run_id]
    rows.sort(key=lambda row: str(row.get("at") or ""))
    if limit is not None:
        rows = rows[-limit:]
    return rows


def send_message(
    paths: BlackdogPaths,
    *,
    sender: str,
    recipient: str,
    body: str,
    kind: str,
    task_id: str | None = None,
    reply_to: str | None = None,
    tags: list[str] | None = None,
    control_action: str | None = None,
    control_scope: str | None = None,
    control_target: str | None = None,
    control_state: str | None = None,
    control_reason: str | None = None,
) -> dict[str, Any]:
    resolved_control_action = normalize_control_action(control_action, inbox_file=paths.inbox_file) if control_action else None
    message = normalize_inbox_row(
        {
            "action": INBOX_ACTION_MESSAGE,
            "message_id": uuid.uuid4().hex,
            "at": now_iso(),
            "sender": sender,
            "recipient": recipient,
            "kind": kind,
            "task_id": task_id,
            "reply_to": reply_to,
            "tags": tags or [],
            "body": body,
            "control_action": resolved_control_action,
            "control_scope": control_scope,
            "control_target": control_target,
            "control_state": control_state,
            "control_reason": control_reason,
        },
        inbox_file=paths.inbox_file,
    )
    append_jsonl(paths.inbox_file, message)
    append_event(
        paths,
        event_type="message",
        actor=sender,
        task_id=task_id,
        control_message_id=message["message_id"],
        payload={
            "message_id": message["message_id"],
            "recipient": recipient,
            "kind": kind,
            "control_action": message.get("control_action"),
            "control_scope": message.get("control_scope"),
            "control_target": message.get("control_target"),
        },
    )
    return message


def send_control_message(
    paths: BlackdogPaths,
    *,
    sender: str,
    recipient: str,
    action: str,
    body: str,
    task_id: str | None = None,
    reply_to: str | None = None,
    tags: list[str] | None = None,
    scope: str | None = None,
    target: str | None = None,
    state: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return send_message(
        paths,
        sender=sender,
        recipient=recipient,
        body=body,
        kind="control",
        task_id=task_id,
        reply_to=reply_to,
        tags=tags or ["control", action],
        control_action=action,
        control_scope=scope,
        control_target=target,
        control_state=state,
        control_reason=reason,
    )


def resolve_message(
    paths: BlackdogPaths,
    *,
    message_id: str,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    row = normalize_inbox_row(
        {
            "action": INBOX_ACTION_RESOLVE,
            "message_id": message_id,
            "at": now_iso(),
            "actor": actor,
            "note": note,
        },
        inbox_file=paths.inbox_file,
    )
    append_jsonl(paths.inbox_file, row)
    append_event(
        paths,
        event_type="message_resolved",
        actor=actor,
        payload={"message_id": message_id, "note": note},
    )
    return row


def load_inbox(
    paths: BlackdogPaths,
    *,
    recipient: str | None = None,
    status: str | None = None,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    rows = [normalize_inbox_row(row, inbox_file=paths.inbox_file) for row in load_jsonl(paths.inbox_file)]
    messages: dict[str, dict[str, Any]] = {}
    for row in rows:
        action = str(row.get("action") or "")
        if action == INBOX_ACTION_MESSAGE:
            messages[str(row["message_id"])] = {
                **row,
                "status": INBOX_STATUS_OPEN,
                "resolved_at": None,
                "resolved_by": None,
                "resolution_note": None,
            }
        elif action == INBOX_ACTION_RESOLVE:
            message = messages.get(str(row["message_id"]))
            if message is None:
                continue
            message["status"] = INBOX_STATUS_RESOLVED
            message["resolved_at"] = row.get("at")
            message["resolved_by"] = row.get("actor")
            message["resolution_note"] = row.get("note")
    output = list(messages.values())
    if recipient:
        output = [row for row in output if row.get("recipient") == recipient]
    if status:
        output = [row for row in output if row.get("status") == status]
    if task_id:
        output = [row for row in output if row.get("task_id") == task_id]
    output.sort(key=lambda row: str(row.get("at") or ""), reverse=True)
    return output


def load_control_messages(
    paths: BlackdogPaths,
    *,
    recipient: str | None = None,
    status: str | None = None,
    task_id: str | None = None,
    control_action: str | None = None,
) -> list[dict[str, Any]]:
    rows = load_inbox(paths, recipient=recipient, status=status, task_id=task_id)
    output = [row for row in rows if row.get("control_action")]
    if control_action is not None:
        normalized_action = normalize_control_action(control_action, inbox_file=paths.inbox_file)
        output = [row for row in output if row.get("control_action") == normalized_action]
    return output


def record_task_result(
    paths: BlackdogPaths,
    *,
    task_id: str,
    actor: str,
    status: str,
    what_changed: list[str],
    validation: list[str],
    residual: list[str],
    needs_user_input: bool,
    followup_candidates: list[str],
    run_id: str | None = None,
    attempt_id: str | None = None,
    prompt_receipt: dict[str, Any] | None = None,
    wait_condition_id: str | None = None,
    control_message_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    task_shaping_telemetry: dict[str, Any] | None = None,
) -> Path:
    if metadata is not None and not isinstance(metadata, dict):
        raise StoreError("metadata must be an object when present")
    if task_shaping_telemetry is not None and not isinstance(task_shaping_telemetry, dict):
        raise StoreError("task_shaping_telemetry must be an object when present")
    result_dir = paths.results_dir / task_id
    result_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    safe_run = run_id or uuid.uuid4().hex[:8]
    result_path = result_dir / f"{timestamp}-{safe_run}.json"
    payload = normalize_task_result(
        {
            "schema_version": 1,
            "task_id": task_id,
            "recorded_at": now_iso(),
            "actor": actor,
            "run_id": safe_run,
            "status": status,
            "what_changed": what_changed,
            "validation": validation,
            "residual": residual,
            "needs_user_input": needs_user_input,
            "followup_candidates": followup_candidates,
            "metadata": metadata or {},
            "task_shaping_telemetry": task_shaping_telemetry or {},
            "attempt_id": attempt_id,
            "prompt_receipt": prompt_receipt,
            "wait_condition_id": wait_condition_id,
            "control_message_id": control_message_id,
        },
        result_file=result_path,
    )
    atomic_write_text(result_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    append_event(
        paths,
        event_type="task_result",
        actor=actor,
        task_id=task_id,
        attempt_id=attempt_id,
        run_id=safe_run,
        wait_condition_id=wait_condition_id,
        control_message_id=control_message_id,
        payload={
            "status": status,
            "run_id": safe_run,
            "result_file": str(result_path),
            "needs_user_input": needs_user_input,
            "attempt_id": attempt_id,
        },
    )
    return result_path


def load_task_results(paths: BlackdogPaths, *, task_id: str | None = None) -> list[dict[str, Any]]:
    if not paths.results_dir.exists():
        return []
    files: list[Path] = []
    if task_id:
        task_dir = paths.results_dir / task_id
        files = sorted(task_dir.glob("*.json")) if task_dir.exists() else []
    else:
        files = sorted(paths.results_dir.glob("*/*.json"))
    results: list[dict[str, Any]] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"Invalid task-result JSON {path}: {exc}") from exc
        payload = normalize_task_result(payload, result_file=path)
        payload["result_file"] = str(path)
        results.append(payload)
    results.sort(key=lambda row: str(row.get("recorded_at") or ""), reverse=True)
    return results


def claim_task_entry(
    entry: dict[str, Any],
    *,
    agent: str,
    title: str,
    summary: dict[str, Any],
    claimed_pid: int | None = None,
) -> None:
    claimed_at = now_iso()
    entry.update(
        {
            "status": CLAIM_STATUS_CLAIMED,
            "title": title,
            "claimed_by": agent,
            "claimed_at": claimed_at,
            **summary,
        }
    )
    entry.pop("claim_expires_at", None)
    if claimed_pid is None:
        entry.pop("claimed_pid", None)
        entry.pop("claimed_process_missing_scans", None)
        entry.pop("claimed_process_last_seen_at", None)
        entry.pop("claimed_process_last_checked_at", None)
        return
    entry["claimed_pid"] = claimed_pid
    entry["claimed_process_missing_scans"] = 0
    entry["claimed_process_last_seen_at"] = claimed_at
    entry.pop("claimed_process_last_checked_at", None)
