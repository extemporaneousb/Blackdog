"""Machine-native runtime state and append-only event helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol
import hashlib
import json
import os
import tempfile
import uuid

from .profile import BlackdogPaths


RUNTIME_SCHEMA_VERSION = 3
RUNTIME_STORE_VERSION = "blackdog.runtime/vnext3"
_READABLE_RUNTIME_FORMATS = frozenset(
    {
        (2, "blackdog.runtime/vnext2"),
        (RUNTIME_SCHEMA_VERSION, RUNTIME_STORE_VERSION),
    }
)
_UNSET = object()

EXECUTION_MODEL_DIRECT_WTAM = "direct_wtam"
EXECUTION_MODELS = frozenset({EXECUTION_MODEL_DIRECT_WTAM})
# Older runtime.json files may still carry the removed managed-claim token.
# Keep it readable at the storage boundary, but do not expose it as part of
# the writable runtime contract.
_LEGACY_MANAGED_EXECUTION_MODEL = "workset_manager"
_READABLE_EXECUTION_MODELS = frozenset({*EXECUTION_MODELS, _LEGACY_MANAGED_EXECUTION_MODEL})

TASK_STATUS_PLANNED = "planned"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_BLOCKED = "blocked"
TASK_STATUS_DONE = "done"
TASK_STATUS_CANCELED = "canceled"
TASK_STATUSES = frozenset(
    {
        TASK_STATUS_PLANNED,
        TASK_STATUS_IN_PROGRESS,
        TASK_STATUS_BLOCKED,
        TASK_STATUS_DONE,
        TASK_STATUS_CANCELED,
    }
)

ATTEMPT_STATUS_IN_PROGRESS = "in_progress"
ATTEMPT_STATUS_SUCCESS = "success"
ATTEMPT_STATUS_BLOCKED = "blocked"
ATTEMPT_STATUS_FAILED = "failed"
ATTEMPT_STATUS_ABANDONED = "abandoned"
ATTEMPT_STATUSES = frozenset(
    {
        ATTEMPT_STATUS_IN_PROGRESS,
        ATTEMPT_STATUS_SUCCESS,
        ATTEMPT_STATUS_BLOCKED,
        ATTEMPT_STATUS_FAILED,
        ATTEMPT_STATUS_ABANDONED,
    }
)
ATTEMPT_ACTIVE_STATUSES = frozenset({ATTEMPT_STATUS_IN_PROGRESS})

VALIDATION_STATUS_PASSED = "passed"
VALIDATION_STATUS_FAILED = "failed"
VALIDATION_STATUS_SKIPPED = "skipped"
VALIDATION_STATUSES = frozenset(
    {
        VALIDATION_STATUS_PASSED,
        VALIDATION_STATUS_FAILED,
        VALIDATION_STATUS_SKIPPED,
    }
)
PROMPT_MODE_RAW = "raw"
PROMPT_MODE_TUNED = "tuned"
PROMPT_MODE_SKILL = "skill"
PROMPT_MODES = frozenset({PROMPT_MODE_RAW, PROMPT_MODE_TUNED, PROMPT_MODE_SKILL})

FAILURE_CLASS_DIRTY_PRIMARY = "dirty_primary"
FAILURE_CLASS_STALE_BRANCH = "stale_branch"
FAILURE_CLASS_MISSING_WORKTREE = "missing_worktree"
FAILURE_CLASS_NO_CHANGES = "no_changes"
FAILURE_CLASS_SUPERSEDED = "superseded"
FAILURE_CLASS_ABANDONED = "abandoned"
FAILURE_CLASS_UNKNOWN = "unknown"
FAILURE_CLASSES = frozenset(
    {
        FAILURE_CLASS_DIRTY_PRIMARY,
        FAILURE_CLASS_STALE_BRANCH,
        FAILURE_CLASS_MISSING_WORKTREE,
        FAILURE_CLASS_NO_CHANGES,
        FAILURE_CLASS_SUPERSEDED,
        FAILURE_CLASS_ABANDONED,
        FAILURE_CLASS_UNKNOWN,
    }
)


class StoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TaskRuntimeRecord:
    task_id: str
    status: str
    updated_at: str | None = None
    note: str | None = None
    failure_class: str | None = None
    recovery_action: str | None = None
    prompt_issue: bool = False
    operator_issue: bool = False


@dataclass(frozen=True, slots=True)
class WorksetClaimRecord:
    actor: str
    execution_model: str
    claimed_at: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class TaskClaimRecord:
    task_id: str
    actor: str
    execution_model: str
    claimed_at: str
    attempt_id: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationRecord:
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class PromptReceiptRecord:
    text: str | None
    prompt_hash: str
    recorded_at: str
    source: str | None = None
    mode: str | None = None


@dataclass(frozen=True, slots=True)
class CodexSessionRefRecord:
    thread_id: str
    session_path: str | None = None
    turn_id: str | None = None
    turn_started_at: str | None = None
    user_prompt_hash: str | None = None
    execution_prompt_hash: str | None = None


@dataclass(frozen=True, slots=True)
class TaskAttemptRecord:
    attempt_id: str
    task_id: str
    status: str
    actor: str
    started_at: str
    ended_at: str | None = None
    summary: str | None = None
    workspace_identity: str | None = None
    workspace_mode: str | None = None
    worktree_role: str | None = None
    worktree_path: str | None = None
    branch: str | None = None
    target_branch: str | None = None
    integration_branch: str | None = None
    start_commit: str | None = None
    execution_model: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    codex_session: CodexSessionRefRecord | None = None
    prompt_receipt: PromptReceiptRecord | None = None
    user_prompt_receipt: PromptReceiptRecord | None = None
    changed_paths: tuple[str, ...] = ()
    validations: tuple[ValidationRecord, ...] = ()
    residuals: tuple[str, ...] = ()
    followup_candidates: tuple[str, ...] = ()
    note: str | None = None
    commit: str | None = None
    landed_commit: str | None = None
    elapsed_seconds: int | None = None
    failure_class: str | None = None
    recovery_action: str | None = None
    prompt_issue: bool = False
    operator_issue: bool = False


@dataclass(frozen=True, slots=True)
class WorksetRuntime:
    workset_id: str
    workset_claim: WorksetClaimRecord | None
    task_claims: tuple[TaskClaimRecord, ...]
    task_states: tuple[TaskRuntimeRecord, ...]
    attempts: tuple[TaskAttemptRecord, ...]


@dataclass(frozen=True, slots=True)
class RuntimeState:
    schema_version: int
    store_version: str
    worksets: tuple[WorksetRuntime, ...]


class RuntimeStore(Protocol):
    def load(self, path: Path) -> RuntimeState:
        ...

    def save(self, path: Path, state: RuntimeState) -> None:
        ...


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def create_prompt_receipt(
    text: str,
    *,
    recorded_at: str | None = None,
    source: str | None = None,
    mode: str | None = PROMPT_MODE_RAW,
) -> PromptReceiptRecord:
    normalized = str(text).strip()
    if not normalized:
        raise ValueError("prompt receipt text is required")
    resolved_mode = _optional_text(mode)
    if resolved_mode is not None and resolved_mode not in PROMPT_MODES:
        raise ValueError(f"prompt receipt mode must be one of {', '.join(sorted(PROMPT_MODES))}")
    return PromptReceiptRecord(
        text=normalized,
        prompt_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        recorded_at=recorded_at or now_iso(),
        source=_optional_text(source),
        mode=resolved_mode,
    )


def prompt_receipt_reference(prompt_receipt: PromptReceiptRecord | None) -> PromptReceiptRecord | None:
    if prompt_receipt is None or prompt_receipt.text is None:
        return prompt_receipt
    return replace(prompt_receipt, text=None)


def parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise StoreError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StoreError(f"{path} must contain a JSON object")
    return payload


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_non_negative_int(value: Any, *, field: str, source: Path) -> int | None:
    if value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"{field} must be an integer in {source}") from exc
    if normalized < 0:
        raise StoreError(f"{field} must be non-negative in {source}")
    return normalized


def _normalize_string_list(value: Any, *, field: str, source: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise StoreError(f"{field} must be a list in {source}")
    items: list[str] = []
    for item in value:
        text = _optional_text(item)
        if text:
            items.append(text)
    return tuple(items)


def _normalize_bool(value: Any, *, field: str, source: Path) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    raise StoreError(f"{field} must be a boolean in {source}")


def _normalize_failure_class(value: Any, *, field: str, source: Path) -> str | None:
    failure_class = _optional_text(value)
    if failure_class is None:
        return None
    if failure_class not in FAILURE_CLASSES:
        raise StoreError(f"{field} must be one of {sorted(FAILURE_CLASSES)} in {source}")
    return failure_class


def _normalize_execution_model(value: Any, *, field: str, source: Path) -> str:
    return _normalize_execution_model_for_runtime(value, field=field, source=source, allow_legacy=False)


def _normalize_execution_model_for_runtime(
    value: Any,
    *,
    field: str,
    source: Path,
    allow_legacy: bool,
) -> str:
    execution_model = _optional_text(value)
    if execution_model is None:
        raise StoreError(f"{field} is required in {source}")
    allowed = _READABLE_EXECUTION_MODELS if allow_legacy else EXECUTION_MODELS
    if execution_model not in allowed:
        raise StoreError(f"{field} must be one of {sorted(allowed)} in {source}")
    return execution_model


def is_legacy_managed_execution_model(value: str | None) -> bool:
    return value == _LEGACY_MANAGED_EXECUTION_MODEL


def _validation_from_payload(payload: Mapping[str, Any], *, source: Path) -> ValidationRecord:
    name = _optional_text(payload.get("name"))
    if name is None:
        raise StoreError(f"validation.name is required in {source}")
    status = _optional_text(payload.get("status")) or VALIDATION_STATUS_PASSED
    if status not in VALIDATION_STATUSES:
        raise StoreError(f"validation.status must be one of {sorted(VALIDATION_STATUSES)} in {source}")
    return ValidationRecord(name=name, status=status)


def _validations_from_payload(payload: Any, *, field: str, source: Path) -> tuple[ValidationRecord, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise StoreError(f"{field} must be a list in {source}")
    rows = tuple(_validation_from_payload(item, source=source) for item in payload if isinstance(item, Mapping))
    if len(rows) != len(payload):
        raise StoreError(f"{field} must contain only objects in {source}")
    return rows


def _prompt_receipt_from_payload(payload: Any, *, field: str, source: Path) -> PromptReceiptRecord | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise StoreError(f"{field} must be an object in {source}")
    text = _optional_text(payload.get("text"))
    prompt_hash = _optional_text(payload.get("prompt_hash"))
    if prompt_hash is None:
        if text is None:
            raise StoreError(f"{field}.text or {field}.prompt_hash is required in {source}")
        prompt_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    recorded_at = _optional_text(payload.get("recorded_at"))
    if recorded_at is None:
        raise StoreError(f"{field}.recorded_at is required in {source}")
    mode = _optional_text(payload.get("mode"))
    if mode is not None and mode not in PROMPT_MODES:
        raise StoreError(f"{field}.mode must be one of {sorted(PROMPT_MODES)} in {source}")
    return PromptReceiptRecord(
        text=text,
        prompt_hash=prompt_hash,
        recorded_at=recorded_at,
        source=_optional_text(payload.get("source")),
        mode=mode,
    )


def _codex_session_ref_from_payload(payload: Any, *, field: str, source: Path) -> CodexSessionRefRecord | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise StoreError(f"{field} must be an object in {source}")
    thread_id = _optional_text(payload.get("thread_id"))
    if thread_id is None:
        raise StoreError(f"{field}.thread_id is required in {source}")
    return CodexSessionRefRecord(
        thread_id=thread_id,
        session_path=_optional_text(payload.get("session_path")),
        turn_id=_optional_text(payload.get("turn_id")),
        turn_started_at=_optional_text(payload.get("turn_started_at")),
        user_prompt_hash=_optional_text(payload.get("user_prompt_hash")),
        execution_prompt_hash=_optional_text(payload.get("execution_prompt_hash")),
    )


def _task_runtime_from_payload(payload: Mapping[str, Any], *, source: Path) -> TaskRuntimeRecord:
    task_id = _optional_text(payload.get("task_id"))
    if task_id is None:
        raise StoreError(f"task_state.task_id is required in {source}")
    status = _optional_text(payload.get("status")) or TASK_STATUS_PLANNED
    if status not in TASK_STATUSES:
        raise StoreError(f"task_state.status must be one of {sorted(TASK_STATUSES)} in {source}")
    return TaskRuntimeRecord(
        task_id=task_id,
        status=status,
        updated_at=_optional_text(payload.get("updated_at")),
        note=_optional_text(payload.get("note")),
        failure_class=_normalize_failure_class(payload.get("failure_class"), field="task_state.failure_class", source=source),
        recovery_action=_optional_text(payload.get("recovery_action")),
        prompt_issue=_normalize_bool(payload.get("prompt_issue"), field="task_state.prompt_issue", source=source),
        operator_issue=_normalize_bool(payload.get("operator_issue"), field="task_state.operator_issue", source=source),
    )


def _workset_claim_from_payload(payload: Any, *, field: str, source: Path) -> WorksetClaimRecord | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise StoreError(f"{field} must be an object in {source}")
    actor = _optional_text(payload.get("actor"))
    if actor is None:
        raise StoreError(f"{field}.actor is required in {source}")
    claimed_at = _optional_text(payload.get("claimed_at"))
    if claimed_at is None:
        raise StoreError(f"{field}.claimed_at is required in {source}")
    return WorksetClaimRecord(
        actor=actor,
        execution_model=_normalize_execution_model_for_runtime(
            payload.get("execution_model"),
            field=f"{field}.execution_model",
            source=source,
            allow_legacy=True,
        ),
        claimed_at=claimed_at,
        note=_optional_text(payload.get("note")),
    )


def _task_claim_from_payload(payload: Any, *, field: str, source: Path) -> TaskClaimRecord:
    if not isinstance(payload, Mapping):
        raise StoreError(f"{field} must be an object in {source}")
    task_id = _optional_text(payload.get("task_id"))
    if task_id is None:
        raise StoreError(f"{field}.task_id is required in {source}")
    actor = _optional_text(payload.get("actor"))
    if actor is None:
        raise StoreError(f"{field}.actor is required in {source}")
    claimed_at = _optional_text(payload.get("claimed_at"))
    if claimed_at is None:
        raise StoreError(f"{field}.claimed_at is required in {source}")
    return TaskClaimRecord(
        task_id=task_id,
        actor=actor,
        execution_model=_normalize_execution_model_for_runtime(
            payload.get("execution_model"),
            field=f"{field}.execution_model",
            source=source,
            allow_legacy=True,
        ),
        claimed_at=claimed_at,
        attempt_id=_optional_text(payload.get("attempt_id")),
        note=_optional_text(payload.get("note")),
    )


def _task_attempt_from_payload(payload: Mapping[str, Any], *, source: Path) -> TaskAttemptRecord:
    attempt_id = _optional_text(payload.get("attempt_id"))
    if attempt_id is None:
        raise StoreError(f"attempt.attempt_id is required in {source}")
    task_id = _optional_text(payload.get("task_id"))
    if task_id is None:
        raise StoreError(f"attempt.task_id is required in {source}")
    actor = _optional_text(payload.get("actor"))
    if actor is None:
        raise StoreError(f"attempt.actor is required in {source}")
    started_at = _optional_text(payload.get("started_at"))
    if started_at is None:
        raise StoreError(f"attempt.started_at is required in {source}")
    status = _optional_text(payload.get("status")) or ATTEMPT_STATUS_IN_PROGRESS
    if status not in ATTEMPT_STATUSES:
        raise StoreError(f"attempt.status must be one of {sorted(ATTEMPT_STATUSES)} in {source}")
    prompt_receipt = _prompt_receipt_from_payload(
        payload.get("prompt_receipt"),
        field="attempt.prompt_receipt",
        source=source,
    )
    user_prompt_receipt = _prompt_receipt_from_payload(
        payload.get("user_prompt_receipt"),
        field="attempt.user_prompt_receipt",
        source=source,
    ) or prompt_receipt
    return TaskAttemptRecord(
        attempt_id=attempt_id,
        task_id=task_id,
        status=status,
        actor=actor,
        started_at=started_at,
        ended_at=_optional_text(payload.get("ended_at")),
        summary=_optional_text(payload.get("summary")),
        workspace_identity=_optional_text(payload.get("workspace_identity")),
        workspace_mode=_optional_text(payload.get("workspace_mode")),
        worktree_role=_optional_text(payload.get("worktree_role")),
        worktree_path=_optional_text(payload.get("worktree_path")),
        branch=_optional_text(payload.get("branch")),
        target_branch=_optional_text(payload.get("target_branch")),
        integration_branch=_optional_text(payload.get("integration_branch")),
        start_commit=_optional_text(payload.get("start_commit")),
        execution_model=(
            _normalize_execution_model_for_runtime(
                payload.get("execution_model"),
                field="attempt.execution_model",
                source=source,
                allow_legacy=True,
            )
            if payload.get("execution_model") is not None
            else None
        ),
        model=_optional_text(payload.get("model")),
        reasoning_effort=_optional_text(payload.get("reasoning_effort")),
        codex_session=_codex_session_ref_from_payload(
            payload.get("codex_session"),
            field="attempt.codex_session",
            source=source,
        ),
        prompt_receipt=prompt_receipt,
        user_prompt_receipt=user_prompt_receipt,
        changed_paths=_normalize_string_list(payload.get("changed_paths"), field="attempt.changed_paths", source=source),
        validations=_validations_from_payload(payload.get("validations"), field="attempt.validations", source=source),
        residuals=_normalize_string_list(payload.get("residuals"), field="attempt.residuals", source=source),
        followup_candidates=_normalize_string_list(
            payload.get("followup_candidates"),
            field="attempt.followup_candidates",
            source=source,
        ),
        note=_optional_text(payload.get("note")),
        commit=_optional_text(payload.get("commit")),
        landed_commit=_optional_text(payload.get("landed_commit")),
        elapsed_seconds=_normalize_non_negative_int(payload.get("elapsed_seconds"), field="attempt.elapsed_seconds", source=source),
        failure_class=_normalize_failure_class(payload.get("failure_class"), field="attempt.failure_class", source=source),
        recovery_action=_optional_text(payload.get("recovery_action")),
        prompt_issue=_normalize_bool(payload.get("prompt_issue"), field="attempt.prompt_issue", source=source),
        operator_issue=_normalize_bool(payload.get("operator_issue"), field="attempt.operator_issue", source=source),
    )


def _workset_runtime_from_payload(payload: Mapping[str, Any], *, source: Path) -> WorksetRuntime:
    workset_id = _optional_text(payload.get("id")) or _optional_text(payload.get("workset_id"))
    if workset_id is None:
        raise StoreError(f"runtime workset id is required in {source}")
    raw_states = payload.get("task_states")
    if raw_states is None:
        task_states: tuple[TaskRuntimeRecord, ...] = ()
    else:
        if not isinstance(raw_states, list):
            raise StoreError(f"runtime workset task_states must be a list in {source}")
        task_states = tuple(_task_runtime_from_payload(item, source=source) for item in raw_states if isinstance(item, Mapping))
        if len(task_states) != len(raw_states):
            raise StoreError(f"runtime workset task_states must contain only objects in {source}")
    raw_task_claims = payload.get("task_claims")
    if raw_task_claims is None:
        task_claims: tuple[TaskClaimRecord, ...] = ()
    else:
        if not isinstance(raw_task_claims, list):
            raise StoreError(f"runtime workset task_claims must be a list in {source}")
        task_claims = tuple(_task_claim_from_payload(item, field="task_claim", source=source) for item in raw_task_claims)
    raw_attempts = payload.get("attempts")
    if raw_attempts is None:
        attempts: tuple[TaskAttemptRecord, ...] = ()
    else:
        if not isinstance(raw_attempts, list):
            raise StoreError(f"runtime workset attempts must be a list in {source}")
        attempts = tuple(_task_attempt_from_payload(item, source=source) for item in raw_attempts if isinstance(item, Mapping))
        if len(attempts) != len(raw_attempts):
            raise StoreError(f"runtime workset attempts must contain only objects in {source}")
    seen_task_ids: set[str] = set()
    for state in task_states:
        if state.task_id in seen_task_ids:
            raise StoreError(f"duplicate runtime task state {state.task_id!r} in {source}")
        seen_task_ids.add(state.task_id)
    seen_claim_task_ids: set[str] = set()
    for claim in task_claims:
        if claim.task_id in seen_claim_task_ids:
            raise StoreError(f"duplicate runtime task claim {claim.task_id!r} in {source}")
        seen_claim_task_ids.add(claim.task_id)
    seen_attempt_ids: set[str] = set()
    for attempt in attempts:
        if attempt.attempt_id in seen_attempt_ids:
            raise StoreError(f"duplicate runtime attempt {attempt.attempt_id!r} in {source}")
        seen_attempt_ids.add(attempt.attempt_id)
    return WorksetRuntime(
        workset_id=workset_id,
        workset_claim=_workset_claim_from_payload(payload.get("workset_claim"), field="workset_claim", source=source),
        task_claims=task_claims,
        task_states=task_states,
        attempts=attempts,
    )


def default_runtime_state() -> RuntimeState:
    return RuntimeState(
        schema_version=RUNTIME_SCHEMA_VERSION,
        store_version=RUNTIME_STORE_VERSION,
        worksets=(),
    )


def runtime_state_to_payload(state: RuntimeState) -> dict[str, Any]:
    def prompt_payload(prompt_receipt: PromptReceiptRecord | None) -> dict[str, Any] | None:
        if prompt_receipt is None:
            return None
        payload: dict[str, Any] = {
            "prompt_hash": prompt_receipt.prompt_hash,
            "recorded_at": prompt_receipt.recorded_at,
            "source": prompt_receipt.source,
            "mode": prompt_receipt.mode,
        }
        if prompt_receipt.text is not None:
            payload["text"] = prompt_receipt.text
        return payload

    def codex_session_payload(codex_session: CodexSessionRefRecord | None) -> dict[str, Any] | None:
        if codex_session is None:
            return None
        return {
            "thread_id": codex_session.thread_id,
            "session_path": codex_session.session_path,
            "turn_id": codex_session.turn_id,
            "turn_started_at": codex_session.turn_started_at,
            "user_prompt_hash": codex_session.user_prompt_hash,
            "execution_prompt_hash": codex_session.execution_prompt_hash,
        }

    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "store_version": RUNTIME_STORE_VERSION,
        "worksets": [
            {
                "id": workset.workset_id,
                "workset_claim": (
                    {
                        "actor": workset.workset_claim.actor,
                        "execution_model": workset.workset_claim.execution_model,
                        "claimed_at": workset.workset_claim.claimed_at,
                        "note": workset.workset_claim.note,
                    }
                    if workset.workset_claim is not None
                    else None
                ),
                "task_claims": [
                    {
                        "task_id": task_claim.task_id,
                        "actor": task_claim.actor,
                        "execution_model": task_claim.execution_model,
                        "claimed_at": task_claim.claimed_at,
                        "attempt_id": task_claim.attempt_id,
                        "note": task_claim.note,
                    }
                    for task_claim in workset.task_claims
                ],
                "task_states": [
                    {
                        "task_id": task_state.task_id,
                        "status": task_state.status,
                        "updated_at": task_state.updated_at,
                        "note": task_state.note,
                        "failure_class": task_state.failure_class,
                        "recovery_action": task_state.recovery_action,
                        "prompt_issue": task_state.prompt_issue,
                        "operator_issue": task_state.operator_issue,
                    }
                    for task_state in workset.task_states
                ],
                "attempts": [
                    {
                        "attempt_id": attempt.attempt_id,
                        "task_id": attempt.task_id,
                        "status": attempt.status,
                        "actor": attempt.actor,
                        "started_at": attempt.started_at,
                        "ended_at": attempt.ended_at,
                        "summary": attempt.summary,
                        "workspace_identity": attempt.workspace_identity,
                        "workspace_mode": attempt.workspace_mode,
                        "worktree_role": attempt.worktree_role,
                        "worktree_path": attempt.worktree_path,
                        "branch": attempt.branch,
                        "target_branch": attempt.target_branch,
                        "integration_branch": attempt.integration_branch,
                        "start_commit": attempt.start_commit,
                        "execution_model": attempt.execution_model,
                        "model": attempt.model,
                        "reasoning_effort": attempt.reasoning_effort,
                        "codex_session": codex_session_payload(attempt.codex_session),
                        "prompt_receipt": prompt_payload(attempt.prompt_receipt),
                        "user_prompt_receipt": prompt_payload(attempt.user_prompt_receipt),
                        "changed_paths": list(attempt.changed_paths),
                        "validations": [
                            {"name": validation.name, "status": validation.status}
                            for validation in attempt.validations
                        ],
                        "residuals": list(attempt.residuals),
                        "followup_candidates": list(attempt.followup_candidates),
                        "note": attempt.note,
                        "commit": attempt.commit,
                        "landed_commit": attempt.landed_commit,
                        "elapsed_seconds": attempt.elapsed_seconds,
                        "failure_class": attempt.failure_class,
                        "recovery_action": attempt.recovery_action,
                        "prompt_issue": attempt.prompt_issue,
                        "operator_issue": attempt.operator_issue,
                    }
                    for attempt in workset.attempts
                ],
            }
            for workset in state.worksets
        ],
    }


class JsonRuntimeStore:
    def load(self, path: Path) -> RuntimeState:
        try:
            payload = _read_json_file(path)
        except FileNotFoundError:
            return default_runtime_state()
        schema_version = int(payload.get("schema_version") or RUNTIME_SCHEMA_VERSION)
        store_version = _optional_text(payload.get("store_version")) or RUNTIME_STORE_VERSION
        if (schema_version, store_version) not in _READABLE_RUNTIME_FORMATS:
            raise StoreError(
                f"Unsupported runtime format schema_version={schema_version} store_version={store_version!r} in {path}"
            )
        raw_worksets = payload.get("worksets") or []
        if not isinstance(raw_worksets, list):
            raise StoreError(f"worksets must be a list in {path}")
        worksets = tuple(_workset_runtime_from_payload(item, source=path) for item in raw_worksets if isinstance(item, Mapping))
        if len(worksets) != len(raw_worksets):
            raise StoreError(f"runtime worksets must contain only objects in {path}")
        seen_workset_ids: set[str] = set()
        for workset in worksets:
            if workset.workset_id in seen_workset_ids:
                raise StoreError(f"duplicate runtime workset {workset.workset_id!r} in {path}")
            seen_workset_ids.add(workset.workset_id)
        return RuntimeState(
            schema_version=RUNTIME_SCHEMA_VERSION,
            store_version=RUNTIME_STORE_VERSION,
            worksets=worksets,
        )

    def save(self, path: Path, state: RuntimeState) -> None:
        atomic_write_text(path, json.dumps(runtime_state_to_payload(state), indent=2, sort_keys=True) + "\n")


def load_runtime_state(paths: BlackdogPaths, store: RuntimeStore | None = None) -> RuntimeState:
    return (store or JsonRuntimeStore()).load(paths.runtime_file)


def save_runtime_state(paths: BlackdogPaths, state: RuntimeState, store: RuntimeStore | None = None) -> None:
    (store or JsonRuntimeStore()).save(paths.runtime_file, state)


def workset_runtime(state: RuntimeState, workset_id: str) -> WorksetRuntime | None:
    for workset in state.worksets:
        if workset.workset_id == workset_id:
            return workset
    return None


def workset_claim(state: RuntimeState, workset_id: str) -> WorksetClaimRecord | None:
    runtime = workset_runtime(state, workset_id)
    if runtime is None:
        return None
    return runtime.workset_claim


def task_claim_index(state: RuntimeState, workset_id: str) -> dict[str, TaskClaimRecord]:
    runtime = workset_runtime(state, workset_id)
    if runtime is None:
        return {}
    return {task_claim.task_id: task_claim for task_claim in runtime.task_claims}


def task_state_index(state: RuntimeState, workset_id: str) -> dict[str, TaskRuntimeRecord]:
    runtime = workset_runtime(state, workset_id)
    if runtime is None:
        return {}
    return {task_state.task_id: task_state for task_state in runtime.task_states}


def task_attempts_for_workset(state: RuntimeState, workset_id: str) -> tuple[TaskAttemptRecord, ...]:
    runtime = workset_runtime(state, workset_id)
    if runtime is None:
        return ()
    return runtime.attempts


def find_task_attempt(state: RuntimeState, workset_id: str, attempt_id: str) -> TaskAttemptRecord | None:
    for attempt in task_attempts_for_workset(state, workset_id):
        if attempt.attempt_id == attempt_id:
            return attempt
    return None


def active_task_attempt(state: RuntimeState, workset_id: str, task_id: str) -> TaskAttemptRecord | None:
    for attempt in task_attempts_for_workset(state, workset_id):
        if (
            attempt.task_id == task_id
            and attempt.status in ATTEMPT_ACTIVE_STATUSES
            and attempt.ended_at is None
        ):
            return attempt
    return None


def latest_task_attempt(state: RuntimeState, workset_id: str, task_id: str) -> TaskAttemptRecord | None:
    candidates = [attempt for attempt in task_attempts_for_workset(state, workset_id) if attempt.task_id == task_id]
    if not candidates:
        return None
    candidates.sort(
        key=lambda attempt: (
            (parse_iso(attempt.ended_at or attempt.started_at).timestamp() if parse_iso(attempt.ended_at or attempt.started_at) is not None else 0.0),
            attempt.attempt_id,
        ),
        reverse=True,
    )
    return candidates[0]


def coerce_task_runtime_records(
    payload: Any,
    *,
    known_task_ids: set[str],
    source_name: str,
) -> tuple[TaskRuntimeRecord, ...]:
    if payload is None:
        return ()
    source = Path(source_name)
    if not isinstance(payload, list):
        raise StoreError(f"task_states must be a list in {source}")
    rows = tuple(_task_runtime_from_payload(item, source=source) for item in payload if isinstance(item, Mapping))
    if len(rows) != len(payload):
        raise StoreError(f"task_states must contain only objects in {source}")
    for row in rows:
        if row.task_id not in known_task_ids:
            raise StoreError(f"task_states references unknown task {row.task_id!r} in {source}")
    return rows


def merge_workset_runtime(
    state: RuntimeState,
    *,
    workset_id: str,
    task_ids: set[str],
    incoming_records: tuple[TaskRuntimeRecord, ...] | None,
    incoming_workset_claim: WorksetClaimRecord | None | object = _UNSET,
    incoming_task_claims: tuple[TaskClaimRecord, ...] | None = None,
    released_task_claim_ids: tuple[str, ...] = (),
    incoming_attempts: tuple[TaskAttemptRecord, ...] | None = None,
) -> RuntimeState:
    current_runtime = workset_runtime(state, workset_id)
    preserved_workset_claim = current_runtime.workset_claim if current_runtime is not None else None
    if incoming_workset_claim is not _UNSET:
        preserved_workset_claim = incoming_workset_claim

    preserved_records = dict(task_state_index(state, workset_id))
    if incoming_records is not None:
        for record in incoming_records:
            preserved_records[record.task_id] = record
    filtered_records = tuple(
        preserved_records[task_id]
        for task_id in sorted(task_ids)
        if task_id in preserved_records
    )

    preserved_task_claims = {
        task_claim.task_id: task_claim
        for task_claim in (current_runtime.task_claims if current_runtime is not None else ())
        if task_claim.task_id in task_ids
    }
    for task_id in released_task_claim_ids:
        preserved_task_claims.pop(task_id, None)
    if incoming_task_claims is not None:
        for task_claim in incoming_task_claims:
            if task_claim.task_id in task_ids:
                preserved_task_claims[task_claim.task_id] = task_claim
    filtered_task_claims = tuple(
        preserved_task_claims[task_id]
        for task_id in sorted(task_ids)
        if task_id in preserved_task_claims
    )

    preserved_attempts = [
        attempt
        for attempt in (current_runtime.attempts if current_runtime is not None else ())
        if attempt.task_id in task_ids
    ]
    attempt_positions = {attempt.attempt_id: index for index, attempt in enumerate(preserved_attempts)}
    if incoming_attempts is not None:
        for attempt in incoming_attempts:
            if attempt.attempt_id in attempt_positions:
                preserved_attempts[attempt_positions[attempt.attempt_id]] = attempt
            else:
                attempt_positions[attempt.attempt_id] = len(preserved_attempts)
                preserved_attempts.append(attempt)
    updated_workset = WorksetRuntime(
        workset_id=workset_id,
        workset_claim=preserved_workset_claim,
        task_claims=filtered_task_claims,
        task_states=filtered_records,
        attempts=tuple(preserved_attempts),
    )
    remaining_worksets = [workset for workset in state.worksets if workset.workset_id != workset_id]
    return RuntimeState(
        schema_version=state.schema_version,
        store_version=state.store_version,
        worksets=tuple([*remaining_worksets, updated_workset]),
    )


def load_events(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    rows: list[dict[str, Any]] = []
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StoreError(f"Invalid JSONL row at {path}:{lineno}: {exc}") from exc
        if not isinstance(payload, dict):
            raise StoreError(f"Event row at {path}:{lineno} must be a JSON object")
        rows.append(payload)
    return tuple(rows)


def append_event(path: Path, *, event_type: str, payload: Mapping[str, Any], actor: str = "blackdog") -> dict[str, Any]:
    row = {
        "event_id": uuid.uuid4().hex,
        "type": event_type,
        "at": now_iso(),
        "actor": actor,
        "payload": dict(payload),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return row


__all__ = [
    "ATTEMPT_ACTIVE_STATUSES",
    "ATTEMPT_STATUSES",
    "ATTEMPT_STATUS_ABANDONED",
    "ATTEMPT_STATUS_BLOCKED",
    "ATTEMPT_STATUS_FAILED",
    "ATTEMPT_STATUS_IN_PROGRESS",
    "ATTEMPT_STATUS_SUCCESS",
    "EXECUTION_MODELS",
    "EXECUTION_MODEL_DIRECT_WTAM",
    "FAILURE_CLASSES",
    "FAILURE_CLASS_ABANDONED",
    "FAILURE_CLASS_DIRTY_PRIMARY",
    "FAILURE_CLASS_MISSING_WORKTREE",
    "FAILURE_CLASS_NO_CHANGES",
    "FAILURE_CLASS_STALE_BRANCH",
    "FAILURE_CLASS_SUPERSEDED",
    "FAILURE_CLASS_UNKNOWN",
    "PROMPT_MODES",
    "PROMPT_MODE_RAW",
    "PROMPT_MODE_SKILL",
    "PROMPT_MODE_TUNED",
    "RUNTIME_SCHEMA_VERSION",
    "RUNTIME_STORE_VERSION",
    "TASK_STATUSES",
    "TASK_STATUS_BLOCKED",
    "TASK_STATUS_CANCELED",
    "TASK_STATUS_DONE",
    "TASK_STATUS_IN_PROGRESS",
    "TASK_STATUS_PLANNED",
    "VALIDATION_STATUSES",
    "VALIDATION_STATUS_FAILED",
    "VALIDATION_STATUS_PASSED",
    "VALIDATION_STATUS_SKIPPED",
    "JsonRuntimeStore",
    "CodexSessionRefRecord",
    "RuntimeState",
    "RuntimeStore",
    "StoreError",
    "TaskClaimRecord",
    "TaskAttemptRecord",
    "TaskRuntimeRecord",
    "ValidationRecord",
    "WorksetClaimRecord",
    "WorksetRuntime",
    "append_event",
    "atomic_write_text",
    "coerce_task_runtime_records",
    "create_prompt_receipt",
    "default_runtime_state",
    "find_task_attempt",
    "load_events",
    "load_runtime_state",
    "merge_workset_runtime",
    "now_iso",
    "parse_iso",
    "prompt_receipt_reference",
    "save_runtime_state",
    "task_claim_index",
    "task_attempts_for_workset",
    "task_state_index",
    "workset_claim",
    "workset_runtime",
]
