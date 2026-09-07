"""Canonical task, attempt, and event storage."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock, get_ident
from typing import Any, Iterator, Mapping, Protocol
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
import uuid

from .profile import BlackdogPaths


RUNTIME_SCHEMA_VERSION = 4
RUNTIME_STORE_VERSION = "blackdog.runtime/v4"
EXECUTION_MODEL_DIRECT_WTAM = "direct_wtam"
EXECUTION_MODELS = frozenset({EXECUTION_MODEL_DIRECT_WTAM})
WORKSPACE_MODE_GIT_WORKTREE = "git-worktree"
WORKSPACE_MODES = frozenset({WORKSPACE_MODE_GIT_WORKTREE})
WORKTREE_ROLE_TASK = "task"
WORKTREE_ROLES = frozenset({WORKTREE_ROLE_TASK})

TASK_STATUS_PLANNED = "planned"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_BLOCKED = "blocked"
TASK_STATUS_DONE = "done"
TASK_STATUS_CANCELED = "canceled"
TASK_STATUSES = frozenset(
    {TASK_STATUS_PLANNED, TASK_STATUS_IN_PROGRESS, TASK_STATUS_BLOCKED, TASK_STATUS_DONE, TASK_STATUS_CANCELED}
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
    {VALIDATION_STATUS_PASSED, VALIDATION_STATUS_FAILED, VALIDATION_STATUS_SKIPPED}
)

PROMPT_MODE_RAW = "raw"
PROMPT_MODE_SKILL = "skill"
PROMPT_MODES = frozenset({PROMPT_MODE_RAW, PROMPT_MODE_SKILL})

CODEX_CAPTURE_STATUS_CAPTURED = "captured"
CODEX_CAPTURE_STATUS_MISSING = "missing"
CODEX_CAPTURE_STATUSES = frozenset({CODEX_CAPTURE_STATUS_CAPTURED, CODEX_CAPTURE_STATUS_MISSING})
CODEX_CAPTURE_METHOD_EXACT_PROMPT_HASH = "exact_prompt_hash"
CODEX_CAPTURE_METHOD_EXACT_ACTIVE_TURN = "exact_active_turn"
CODEX_CAPTURE_METHODS = frozenset(
    {CODEX_CAPTURE_METHOD_EXACT_PROMPT_HASH, CODEX_CAPTURE_METHOD_EXACT_ACTIVE_TURN}
)
CODEX_CAPTURE_MISSING_REASON_SESSION_PATH_MISSING = "session_path_missing"
CODEX_CAPTURE_MISSING_REASON_SESSION_MISSING = "session_missing"
CODEX_CAPTURE_MISSING_REASON_SESSION_UNREADABLE = "session_unreadable"
CODEX_CAPTURE_MISSING_REASON_THREAD_MISMATCH = "thread_mismatch"
CODEX_CAPTURE_MISSING_REASON_PROMPT_HASH_AMBIGUOUS = "prompt_hash_ambiguous"
CODEX_CAPTURE_MISSING_REASON_NO_OPEN_TURN = "no_open_turn"
CODEX_CAPTURE_MISSING_REASON_MULTIPLE_OPEN_TURNS = "multiple_open_turns"
CODEX_CAPTURE_MISSING_REASON_CAPTURE_ERROR = "capture_error"
CODEX_CAPTURE_MISSING_REASONS = frozenset(
    {
        CODEX_CAPTURE_MISSING_REASON_SESSION_PATH_MISSING,
        CODEX_CAPTURE_MISSING_REASON_SESSION_MISSING,
        CODEX_CAPTURE_MISSING_REASON_SESSION_UNREADABLE,
        CODEX_CAPTURE_MISSING_REASON_THREAD_MISMATCH,
        CODEX_CAPTURE_MISSING_REASON_PROMPT_HASH_AMBIGUOUS,
        CODEX_CAPTURE_MISSING_REASON_NO_OPEN_TURN,
        CODEX_CAPTURE_MISSING_REASON_MULTIPLE_OPEN_TURNS,
        CODEX_CAPTURE_MISSING_REASON_CAPTURE_ERROR,
    }
)

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

_TASK_ID = re.compile(r"task-[0-9a-f]{32}\Z")
_ATTEMPT_ID = re.compile(r"attempt-[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

_RUNTIME_KEYS = frozenset({"schema_version", "store_version", "tasks"})
_TASK_KEYS = frozenset(
    {
        "id",
        "title",
        "created_at",
        "status",
        "updated_at",
        "actor",
        "note",
        "failure_class",
        "recovery_action",
        "prompt_issue",
        "operator_issue",
        "attempts",
    }
)
_ATTEMPT_KEYS = frozenset(
    {
        "attempt_id",
        "status",
        "actor",
        "started_at",
        "ended_at",
        "summary",
        "workspace_identity",
        "workspace_mode",
        "worktree_role",
        "worktree_path",
        "branch",
        "target_branch",
        "integration_branch",
        "start_commit",
        "execution_model",
        "model",
        "reasoning_effort",
        "codex_session",
        "prompt_receipt",
        "user_prompt_receipt",
        "changed_paths",
        "validations",
        "residuals",
        "followup_candidates",
        "note",
        "commit",
        "landed_commit",
        "elapsed_seconds",
        "failure_class",
        "recovery_action",
        "prompt_issue",
        "operator_issue",
        "setup_receipt",
    }
)
_PROMPT_RECEIPT_KEYS = frozenset(
    {"prompt_hash", "recorded_at", "source", "mode", "replay_artifact_path"}
)
_CODEX_SESSION_KEYS = frozenset(
    {
        "thread_id",
        "session_path",
        "turn_id",
        "turn_started_at",
        "user_prompt_hash",
        "execution_prompt_hash",
        "capture_status",
        "capture_method",
        "capture_missing_reason",
    }
)
_VALIDATION_KEYS = frozenset({"name", "status"})
_EVENT_KEYS = frozenset({"event_id", "type", "at", "actor", "payload"})


class StoreError(RuntimeError):
    pass


class StoreMigrationRequired(StoreError):
    """An old or partially migrated store needs the explicit lifecycle route."""


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
    replay_artifact_path: str | None = None


@dataclass(frozen=True, slots=True)
class CodexSessionRefRecord:
    thread_id: str
    session_path: str | None = None
    turn_id: str | None = None
    turn_started_at: str | None = None
    user_prompt_hash: str | None = None
    execution_prompt_hash: str | None = None
    capture_status: str | None = None
    capture_method: str | None = None
    capture_missing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TaskAttemptRecord:
    attempt_id: str
    status: str
    actor: str
    started_at: str
    task_id: str | None = None  # Derived context; not serialized in a task row.
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
    setup_receipt: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    title: str
    created_at: str | None = None
    status: str = TASK_STATUS_PLANNED
    updated_at: str | None = None
    actor: str | None = None
    note: str | None = None
    failure_class: str | None = None
    recovery_action: str | None = None
    prompt_issue: bool = False
    operator_issue: bool = False
    attempts: tuple[TaskAttemptRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeState:
    schema_version: int
    store_version: str
    tasks: tuple[TaskRecord, ...]


class RuntimeStore(Protocol):
    def load(self, path: Path) -> RuntimeState: ...

    def save(self, path: Path, state: RuntimeState) -> None: ...


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_task_id() -> str:
    return f"task-{uuid.uuid4().hex}"


def new_attempt_id() -> str:
    return f"attempt-{uuid.uuid4().hex}"


def is_canonical_task_id(value: str) -> bool:
    return isinstance(value, str) and _TASK_ID.fullmatch(value) is not None


def is_canonical_attempt_id(value: str) -> bool:
    return isinstance(value, str) and _ATTEMPT_ID.fullmatch(value) is not None


def create_prompt_receipt(
    text: str,
    *,
    source: str | None = None,
    mode: str | None = None,
    recorded_at: str | None = None,
    replay_artifact_path: str | None = None,
) -> PromptReceiptRecord:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return PromptReceiptRecord(
        text=normalized,
        prompt_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        recorded_at=recorded_at or now_iso(),
        source=source,
        mode=mode,
        replay_artifact_path=replay_artifact_path,
    )


def prompt_receipt_reference(receipt: PromptReceiptRecord | None) -> PromptReceiptRecord | None:
    return replace(receipt, text=None) if receipt is not None else None


def parse_iso(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


_LOCK_REGISTRY_GUARD = RLock()
_LOCK_REGISTRY: dict[tuple[str, int], tuple[int, Any, Path | None]] = {}


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold one thread-reentrant interprocess lock adjacent to ``path``."""

    target = path.expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    key = (str(target), get_ident())
    with _LOCK_REGISTRY_GUARD:
        existing = _LOCK_REGISTRY.get(key)
        if existing is not None:
            depth, handle, lockdir = existing
            _LOCK_REGISTRY[key] = (depth + 1, handle, lockdir)
            nested = True
        else:
            nested = False
    if nested:
        try:
            yield
        finally:
            with _LOCK_REGISTRY_GUARD:
                depth, handle, lockdir = _LOCK_REGISTRY[key]
                _LOCK_REGISTRY[key] = (depth - 1, handle, lockdir)
        return

    lock_path = target.with_name(f"{target.name}.lock")
    handle = lock_path.open("a+")
    lockdir: Path | None = None
    used_flock = False
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            used_flock = True
        except (ImportError, OSError):
            lockdir = target.with_name(f"{target.name}.lockdir")
            while True:
                try:
                    lockdir.mkdir()
                    break
                except FileExistsError:
                    time.sleep(0.01)
        with _LOCK_REGISTRY_GUARD:
            _LOCK_REGISTRY[key] = (1, handle, lockdir)
        yield
    finally:
        with _LOCK_REGISTRY_GUARD:
            _LOCK_REGISTRY.pop(key, None)
        if lockdir is not None:
            shutil.rmtree(lockdir, ignore_errors=True)
        elif used_flock:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
        handle.close()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _reject_unknown_keys(payload: Mapping[str, Any], *, allowed: frozenset[str], field: str, source: Path) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise StoreError(f"{field} has unknown fields {', '.join(unknown)} in {source}")


def _string_tuple(value: Any, *, field: str, source: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise StoreError(f"{field} must be a list of nonempty strings in {source}")
    return tuple(item.strip() for item in value)


def _bool(value: Any, *, field: str, source: Path) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise StoreError(f"{field} must be a boolean in {source}")
    return value


def _nonnegative_int(value: Any, *, field: str, source: Path) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise StoreError(f"{field} must be a non-negative integer in {source}")
    return value


def _mapping(value: Any, *, field: str, source: Path) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise StoreError(f"{field} must be an object in {source}")
    return dict(value)


def _validation_from_payload(payload: Any, *, source: Path) -> ValidationRecord:
    if not isinstance(payload, Mapping):
        raise StoreError(f"validation must be an object in {source}")
    _reject_unknown_keys(payload, allowed=_VALIDATION_KEYS, field="validation", source=source)
    name = _optional_text(payload.get("name"))
    status = _optional_text(payload.get("status"))
    if name is None or status not in VALIDATION_STATUSES:
        raise StoreError(f"validation requires name and valid status in {source}")
    return ValidationRecord(name=name, status=status)


def _receipt_from_payload(payload: Any, *, field: str, source: Path) -> PromptReceiptRecord | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise StoreError(f"{field} must be an object in {source}")
    _reject_unknown_keys(payload, allowed=_PROMPT_RECEIPT_KEYS, field=field, source=source)
    prompt_hash = _optional_text(payload.get("prompt_hash"))
    recorded_at = _optional_text(payload.get("recorded_at"))
    if prompt_hash is None or recorded_at is None:
        raise StoreError(f"{field} requires prompt_hash and recorded_at in {source}")
    mode = _optional_text(payload.get("mode"))
    if mode is not None and mode not in PROMPT_MODES:
        raise StoreError(f"{field}.mode is invalid in {source}")
    return PromptReceiptRecord(
        text=None,
        prompt_hash=prompt_hash,
        recorded_at=recorded_at,
        source=_optional_text(payload.get("source")),
        mode=mode,
        replay_artifact_path=_optional_text(payload.get("replay_artifact_path")),
    )


def _session_from_payload(payload: Any, *, source: Path) -> CodexSessionRefRecord | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise StoreError(f"codex_session must be an object in {source}")
    _reject_unknown_keys(payload, allowed=_CODEX_SESSION_KEYS, field="codex_session", source=source)
    thread_id = _optional_text(payload.get("thread_id"))
    if thread_id is None:
        raise StoreError(f"codex_session.thread_id is required in {source}")
    return CodexSessionRefRecord(
        thread_id=thread_id,
        session_path=_optional_text(payload.get("session_path")),
        turn_id=_optional_text(payload.get("turn_id")),
        turn_started_at=_optional_text(payload.get("turn_started_at")),
        user_prompt_hash=_optional_text(payload.get("user_prompt_hash")),
        execution_prompt_hash=_optional_text(payload.get("execution_prompt_hash")),
        capture_status=_optional_text(payload.get("capture_status")),
        capture_method=_optional_text(payload.get("capture_method")),
        capture_missing_reason=_optional_text(payload.get("capture_missing_reason")),
    )


def _attempt_from_payload(payload: Any, *, task_id: str, source: Path) -> TaskAttemptRecord:
    if not isinstance(payload, Mapping):
        raise StoreError(f"task[{task_id}].attempts must contain objects in {source}")
    _reject_unknown_keys(payload, allowed=_ATTEMPT_KEYS, field=f"task[{task_id}].attempt", source=source)
    attempt_id = _optional_text(payload.get("attempt_id"))
    status = _optional_text(payload.get("status"))
    actor = _optional_text(payload.get("actor"))
    started_at = _optional_text(payload.get("started_at"))
    if attempt_id is None or status not in ATTEMPT_STATUSES or actor is None or started_at is None:
        raise StoreError(f"task[{task_id}] has an invalid attempt in {source}")
    validations = payload.get("validations") or []
    if not isinstance(validations, list):
        raise StoreError(f"validations must be a list in {source}")
    return TaskAttemptRecord(
        task_id=task_id,
        attempt_id=attempt_id,
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
        execution_model=_optional_text(payload.get("execution_model")),
        model=_optional_text(payload.get("model")),
        reasoning_effort=_optional_text(payload.get("reasoning_effort")),
        codex_session=_session_from_payload(payload.get("codex_session"), source=source),
        prompt_receipt=_receipt_from_payload(payload.get("prompt_receipt"), field="prompt_receipt", source=source),
        user_prompt_receipt=_receipt_from_payload(payload.get("user_prompt_receipt"), field="user_prompt_receipt", source=source),
        changed_paths=_string_tuple(payload.get("changed_paths"), field="changed_paths", source=source),
        validations=tuple(_validation_from_payload(item, source=source) for item in validations),
        residuals=_string_tuple(payload.get("residuals"), field="residuals", source=source),
        followup_candidates=_string_tuple(payload.get("followup_candidates"), field="followup_candidates", source=source),
        note=_optional_text(payload.get("note")),
        commit=_optional_text(payload.get("commit")),
        landed_commit=_optional_text(payload.get("landed_commit")),
        elapsed_seconds=_nonnegative_int(payload.get("elapsed_seconds"), field="elapsed_seconds", source=source),
        failure_class=_optional_text(payload.get("failure_class")),
        recovery_action=_optional_text(payload.get("recovery_action")),
        prompt_issue=_bool(payload.get("prompt_issue"), field="prompt_issue", source=source),
        operator_issue=_bool(payload.get("operator_issue"), field="operator_issue", source=source),
        setup_receipt=_mapping(payload.get("setup_receipt"), field="setup_receipt", source=source),
    )


def _task_from_payload(payload: Any, *, source: Path) -> TaskRecord:
    if not isinstance(payload, Mapping):
        raise StoreError(f"tasks must contain objects in {source}")
    _reject_unknown_keys(payload, allowed=_TASK_KEYS, field="task", source=source)
    task_id = _optional_text(payload.get("id"))
    title = _optional_text(payload.get("title"))
    status = _optional_text(payload.get("status"))
    if task_id is None or title is None or status not in TASK_STATUSES:
        raise StoreError(f"task id, title, and valid status are required in {source}")
    raw_attempts = payload.get("attempts") or []
    if not isinstance(raw_attempts, list):
        raise StoreError(f"task[{task_id}].attempts must be a list in {source}")
    attempts = tuple(_attempt_from_payload(item, task_id=task_id, source=source) for item in raw_attempts)
    if len({attempt.attempt_id for attempt in attempts}) != len(attempts):
        raise StoreError(f"task {task_id!r} has duplicate attempt ids in {source}")
    return TaskRecord(
        task_id=task_id,
        title=title,
        created_at=_optional_text(payload.get("created_at")),
        status=status,
        updated_at=_optional_text(payload.get("updated_at")),
        actor=_optional_text(payload.get("actor")),
        note=_optional_text(payload.get("note")),
        failure_class=_optional_text(payload.get("failure_class")),
        recovery_action=_optional_text(payload.get("recovery_action")),
        prompt_issue=_bool(payload.get("prompt_issue"), field="prompt_issue", source=source),
        operator_issue=_bool(payload.get("operator_issue"), field="operator_issue", source=source),
        attempts=attempts,
    )


def default_runtime_state() -> RuntimeState:
    return RuntimeState(RUNTIME_SCHEMA_VERSION, RUNTIME_STORE_VERSION, ())


def _receipt_payload(receipt: PromptReceiptRecord | None) -> dict[str, Any] | None:
    if receipt is None:
        return None
    return {
        "prompt_hash": receipt.prompt_hash,
        "recorded_at": receipt.recorded_at,
        "source": receipt.source,
        "mode": receipt.mode,
        "replay_artifact_path": receipt.replay_artifact_path,
    }


def _session_payload(session: CodexSessionRefRecord | None) -> dict[str, Any] | None:
    if session is None:
        return None
    return {
        "thread_id": session.thread_id,
        "session_path": session.session_path,
        "turn_id": session.turn_id,
        "turn_started_at": session.turn_started_at,
        "user_prompt_hash": session.user_prompt_hash,
        "execution_prompt_hash": session.execution_prompt_hash,
        "capture_status": session.capture_status,
        "capture_method": session.capture_method,
        "capture_missing_reason": session.capture_missing_reason,
    }


def _attempt_payload(attempt: TaskAttemptRecord) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
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
        "codex_session": _session_payload(attempt.codex_session),
        "prompt_receipt": _receipt_payload(attempt.prompt_receipt),
        "user_prompt_receipt": _receipt_payload(attempt.user_prompt_receipt),
        "changed_paths": list(attempt.changed_paths),
        "validations": [{"name": item.name, "status": item.status} for item in attempt.validations],
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
        "setup_receipt": attempt.setup_receipt,
    }


def task_record_to_payload(task: TaskRecord) -> dict[str, Any]:
    return {
        "id": task.task_id,
        "title": task.title,
        "created_at": task.created_at,
        "status": task.status,
        "updated_at": task.updated_at,
        "actor": task.actor,
        "note": task.note,
        "failure_class": task.failure_class,
        "recovery_action": task.recovery_action,
        "prompt_issue": task.prompt_issue,
        "operator_issue": task.operator_issue,
        "attempts": [_attempt_payload(attempt) for attempt in task.attempts],
    }


def runtime_state_to_payload(state: RuntimeState) -> dict[str, Any]:
    return {
        "schema_version": state.schema_version,
        "store_version": state.store_version,
        "tasks": [task_record_to_payload(task) for task in state.tasks],
    }


def _require_timestamp(value: str | None, *, field: str, source: Path, required: bool = False) -> datetime | None:
    if value is None:
        if required:
            raise StoreError(f"{field} requires a timestamp in {source}")
        return None
    parsed = parse_iso(value)
    if parsed is None:
        raise StoreError(f"{field} has an invalid timestamp in {source}")
    return parsed


def _require_canonical_text(value: Any, *, field: str, source: Path) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise StoreError(f"{field} must be a canonical nonempty string in {source}")


def _require_canonical_optional_text(value: Any, *, field: str, source: Path) -> None:
    if value is None:
        return
    _require_canonical_text(value, field=field, source=source)


def _require_string_tuple(value: Any, *, field: str, source: Path) -> None:
    if not isinstance(value, tuple):
        raise StoreError(f"{field} must be an immutable string sequence in {source}")
    for item in value:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise StoreError(f"{field} contains a non-canonical string in {source}")


def _validate_prompt_receipt(
    receipt: PromptReceiptRecord | None,
    *,
    field: str,
    source: Path,
    artifact_root: Path,
) -> None:
    if receipt is None:
        return
    if not isinstance(receipt, PromptReceiptRecord):
        raise StoreError(f"{field} must be a prompt receipt in {source}")
    _require_canonical_optional_text(receipt.source, field=f"{field}.source", source=source)
    _require_canonical_optional_text(receipt.mode, field=f"{field}.mode", source=source)
    _require_canonical_optional_text(
        receipt.replay_artifact_path,
        field=f"{field}.replay_artifact_path",
        source=source,
    )
    if not isinstance(receipt.prompt_hash, str) or _SHA256.fullmatch(receipt.prompt_hash) is None:
        raise StoreError(f"{field}.prompt_hash must be lowercase SHA-256 in {source}")
    _require_timestamp(receipt.recorded_at, field=f"{field}.recorded_at", source=source, required=True)
    if receipt.mode is not None and receipt.mode not in PROMPT_MODES:
        raise StoreError(f"{field}.mode is invalid in {source}")
    if receipt.text is not None:
        if not isinstance(receipt.text, str):
            raise StoreError(f"{field}.text must be a string in {source}")
        if hashlib.sha256(receipt.text.encode("utf-8")).hexdigest() != receipt.prompt_hash:
            raise StoreError(f"{field}.text does not match prompt_hash in {source}")
    if receipt.replay_artifact_path is None:
        return
    expected = Path("prompts") / "sha256" / f"{receipt.prompt_hash}.txt"
    relative = Path(receipt.replay_artifact_path)
    if relative.is_absolute() or relative.as_posix() != expected.as_posix():
        raise StoreError(f"{field}.replay_artifact_path is not content-addressed in {source}")
    artifact = artifact_root / relative
    try:
        artifact.parent.resolve(strict=False).relative_to(artifact_root)
    except (OSError, ValueError) as exc:
        raise StoreError(f"{field}.replay_artifact_path escapes the control directory in {source}") from exc
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(artifact, flags)
    except OSError as exc:
        raise StoreError(f"{field} replay artifact is unavailable at {artifact}: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise StoreError(f"{field} replay artifact must be a mode-0600 regular file in {source}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if hashlib.sha256(raw).hexdigest() != receipt.prompt_hash:
        raise StoreError(f"{field} replay artifact content does not match prompt_hash in {source}")


def _validate_codex_session(session: CodexSessionRefRecord | None, *, source: Path) -> None:
    if session is None:
        return
    if not isinstance(session, CodexSessionRefRecord):
        raise StoreError(f"codex_session must be a session reference in {source}")
    _require_canonical_text(session.thread_id, field="codex_session.thread_id", source=source)
    for field, value in (
        ("codex_session.session_path", session.session_path),
        ("codex_session.turn_id", session.turn_id),
        ("codex_session.capture_status", session.capture_status),
        ("codex_session.capture_method", session.capture_method),
        ("codex_session.capture_missing_reason", session.capture_missing_reason),
    ):
        _require_canonical_optional_text(value, field=field, source=source)
    if session.capture_status is not None and session.capture_status not in CODEX_CAPTURE_STATUSES:
        raise StoreError(f"codex_session.capture_status is invalid in {source}")
    if session.capture_method is not None and session.capture_method not in CODEX_CAPTURE_METHODS:
        raise StoreError(f"codex_session.capture_method is invalid in {source}")
    if session.capture_missing_reason is not None and session.capture_missing_reason not in CODEX_CAPTURE_MISSING_REASONS:
        raise StoreError(f"codex_session.capture_missing_reason is invalid in {source}")
    _require_timestamp(session.turn_started_at, field="codex_session.turn_started_at", source=source)
    for field, value in (
        ("codex_session.user_prompt_hash", session.user_prompt_hash),
        ("codex_session.execution_prompt_hash", session.execution_prompt_hash),
    ):
        if value is not None and (not isinstance(value, str) or _SHA256.fullmatch(value) is None):
            raise StoreError(f"{field} must be lowercase SHA-256 in {source}")
    if session.capture_status == CODEX_CAPTURE_STATUS_CAPTURED:
        if session.capture_method is None or session.capture_missing_reason is not None:
            raise StoreError(f"captured codex_session requires method and no missing reason in {source}")
    if session.capture_status == CODEX_CAPTURE_STATUS_MISSING:
        if session.capture_missing_reason is None or session.capture_method is not None:
            raise StoreError(f"missing codex_session requires reason and no capture method in {source}")
    if session.capture_status is None and (
        session.capture_method is not None or session.capture_missing_reason is not None
    ):
        raise StoreError(f"codex_session capture details require capture_status in {source}")


def _validate_state(state: RuntimeState, *, source: Path, artifact_root: Path | None = None) -> None:
    if state.schema_version != RUNTIME_SCHEMA_VERSION or state.store_version != RUNTIME_STORE_VERSION:
        raise StoreError(f"unsupported runtime state {state.schema_version!r}/{state.store_version!r} in {source}")
    resolved_artifact_root = (artifact_root or source.parent).resolve(strict=False)
    if not isinstance(state.tasks, tuple):
        raise StoreError(f"runtime tasks must be an immutable sequence in {source}")
    if any(not isinstance(task, TaskRecord) for task in state.tasks):
        raise StoreError(f"runtime tasks must contain task records in {source}")
    ids = [task.task_id for task in state.tasks]
    if len(set(ids)) != len(ids):
        raise StoreError(f"duplicate task ids in {source}")
    attempts: set[str] = set()
    for task in state.tasks:
        if not is_canonical_task_id(task.task_id):
            raise StoreError(f"task id {task.task_id!r} is not a canonical globally unique id in {source}")
        _require_canonical_text(task.title, field=f"task[{task.task_id}].title", source=source)
        if not isinstance(task.status, str) or task.status not in TASK_STATUSES:
            raise StoreError(f"task {task.task_id!r} has invalid status in {source}")
        created_at = _require_timestamp(task.created_at, field=f"task[{task.task_id}].created_at", source=source)
        updated_at = _require_timestamp(task.updated_at, field=f"task[{task.task_id}].updated_at", source=source)
        if created_at is not None and updated_at is not None and updated_at < created_at:
            raise StoreError(f"task {task.task_id!r} is updated before it is created in {source}")
        for field, value in (
            ("actor", task.actor),
            ("note", task.note),
            ("failure_class", task.failure_class),
            ("recovery_action", task.recovery_action),
        ):
            _require_canonical_optional_text(value, field=f"task[{task.task_id}].{field}", source=source)
        if type(task.prompt_issue) is not bool or type(task.operator_issue) is not bool:
            raise StoreError(f"task {task.task_id!r} issue flags must be booleans in {source}")
        if task.failure_class is not None and task.failure_class not in FAILURE_CLASSES:
            raise StoreError(f"task {task.task_id!r} has invalid failure_class in {source}")
        if not isinstance(task.attempts, tuple):
            raise StoreError(f"task {task.task_id!r} attempts must be an immutable sequence in {source}")
        if any(not isinstance(attempt, TaskAttemptRecord) for attempt in task.attempts):
            raise StoreError(f"task {task.task_id!r} attempts must contain attempt records in {source}")
        active = [attempt for attempt in task.attempts if attempt.status in ATTEMPT_ACTIVE_STATUSES]
        if len(active) > 1:
            raise StoreError(f"task {task.task_id!r} has multiple in-progress attempts in {source}")
        if bool(active) != (task.status == TASK_STATUS_IN_PROGRESS):
            raise StoreError(f"task {task.task_id!r} status and active attempt disagree in {source}")
        successful = [attempt for attempt in task.attempts if attempt.status == ATTEMPT_STATUS_SUCCESS]
        if successful and (len(successful) != 1 or successful[0] is not task.attempts[-1]):
            raise StoreError(f"task {task.task_id!r} has execution after successful completion in {source}")
        if successful and task.status != TASK_STATUS_DONE:
            raise StoreError(f"task {task.task_id!r} completion status and successful attempt disagree in {source}")
        if task.status == TASK_STATUS_DONE and task.attempts and not successful:
            raise StoreError(f"task {task.task_id!r} is done without a successful retained attempt in {source}")
        previous_started_at: datetime | None = None
        for attempt in task.attempts:
            if attempt.task_id != task.task_id:
                raise StoreError(f"attempt {attempt.attempt_id!r} has conflicting task context in {source}")
            if not isinstance(attempt.status, str) or attempt.status not in ATTEMPT_STATUSES:
                raise StoreError(f"attempt {attempt.attempt_id!r} has invalid status in {source}")
            _require_canonical_text(
                attempt.actor,
                field=f"attempt[{attempt.attempt_id}].actor",
                source=source,
            )
            if not is_canonical_attempt_id(attempt.attempt_id):
                raise StoreError(
                    f"attempt id {attempt.attempt_id!r} is not a canonical globally unique id in {source}"
                )
            if attempt.attempt_id in attempts:
                raise StoreError(f"duplicate attempt id {attempt.attempt_id!r} in {source}")
            attempts.add(attempt.attempt_id)
            started_at = _require_timestamp(
                attempt.started_at,
                field=f"attempt[{attempt.attempt_id}].started_at",
                source=source,
                required=True,
            )
            ended_at = _require_timestamp(
                attempt.ended_at,
                field=f"attempt[{attempt.attempt_id}].ended_at",
                source=source,
                required=attempt.status != ATTEMPT_STATUS_IN_PROGRESS,
            )
            assert started_at is not None
            if previous_started_at is not None and started_at < previous_started_at:
                raise StoreError(f"task {task.task_id!r} attempts are not append-ordered in {source}")
            previous_started_at = started_at
            if attempt.status == ATTEMPT_STATUS_IN_PROGRESS and ended_at is not None:
                raise StoreError(f"active attempt {attempt.attempt_id!r} has ended_at in {source}")
            if ended_at is not None and ended_at < started_at:
                raise StoreError(f"attempt {attempt.attempt_id!r} ends before it starts in {source}")
            if attempt.execution_model is not None and (
                not isinstance(attempt.execution_model, str)
                or attempt.execution_model not in EXECUTION_MODELS
            ):
                raise StoreError(f"attempt {attempt.attempt_id!r} has invalid execution_model in {source}")
            if attempt.workspace_mode is not None and (
                not isinstance(attempt.workspace_mode, str)
                or attempt.workspace_mode not in WORKSPACE_MODES
            ):
                raise StoreError(f"attempt {attempt.attempt_id!r} has invalid workspace_mode in {source}")
            if attempt.worktree_role is not None and (
                not isinstance(attempt.worktree_role, str)
                or attempt.worktree_role not in WORKTREE_ROLES
            ):
                raise StoreError(f"attempt {attempt.attempt_id!r} has invalid worktree_role in {source}")
            if attempt.failure_class is not None and (
                not isinstance(attempt.failure_class, str)
                or attempt.failure_class not in FAILURE_CLASSES
            ):
                raise StoreError(f"attempt {attempt.attempt_id!r} has invalid failure_class in {source}")
            for field, value in (
                ("summary", attempt.summary),
                ("workspace_identity", attempt.workspace_identity),
                ("workspace_mode", attempt.workspace_mode),
                ("worktree_role", attempt.worktree_role),
                ("worktree_path", attempt.worktree_path),
                ("branch", attempt.branch),
                ("target_branch", attempt.target_branch),
                ("integration_branch", attempt.integration_branch),
                ("execution_model", attempt.execution_model),
                ("model", attempt.model),
                ("reasoning_effort", attempt.reasoning_effort),
                ("note", attempt.note),
                ("failure_class", attempt.failure_class),
                ("recovery_action", attempt.recovery_action),
            ):
                _require_canonical_optional_text(
                    value,
                    field=f"attempt[{attempt.attempt_id}].{field}",
                    source=source,
                )
            if type(attempt.prompt_issue) is not bool or type(attempt.operator_issue) is not bool:
                raise StoreError(f"attempt {attempt.attempt_id!r} issue flags must be booleans in {source}")
            if attempt.elapsed_seconds is not None and (
                type(attempt.elapsed_seconds) is not int or attempt.elapsed_seconds < 0
            ):
                raise StoreError(f"attempt {attempt.attempt_id!r} has invalid elapsed_seconds in {source}")
            for field, value in (
                ("start_commit", attempt.start_commit),
                ("commit", attempt.commit),
                ("landed_commit", attempt.landed_commit),
            ):
                if value is not None and (
                    not isinstance(value, str)
                    or len(value) != 40
                    or any(item not in "0123456789abcdef" for item in value)
                ):
                    raise StoreError(f"attempt {attempt.attempt_id!r} has invalid {field} in {source}")
            _require_string_tuple(
                attempt.changed_paths,
                field=f"attempt[{attempt.attempt_id}].changed_paths",
                source=source,
            )
            _require_string_tuple(
                attempt.residuals,
                field=f"attempt[{attempt.attempt_id}].residuals",
                source=source,
            )
            _require_string_tuple(
                attempt.followup_candidates,
                field=f"attempt[{attempt.attempt_id}].followup_candidates",
                source=source,
            )
            if not isinstance(attempt.validations, tuple) or any(
                not isinstance(item, ValidationRecord)
                or item.status not in VALIDATION_STATUSES
                or not isinstance(item.name, str)
                or not item.name.strip()
                or item.name != item.name.strip()
                for item in attempt.validations
            ):
                raise StoreError(f"attempt {attempt.attempt_id!r} has invalid validation evidence in {source}")
            if attempt.setup_receipt is not None:
                if not isinstance(attempt.setup_receipt, dict):
                    raise StoreError(f"attempt {attempt.attempt_id!r} setup_receipt must be an object in {source}")
                _normalize_json(
                    attempt.setup_receipt,
                    source=f"attempt[{attempt.attempt_id}].setup_receipt",
                )
            _validate_codex_session(attempt.codex_session, source=source)
            _validate_prompt_receipt(
                attempt.prompt_receipt,
                field=f"attempt[{attempt.attempt_id}].prompt_receipt",
                source=source,
                artifact_root=resolved_artifact_root,
            )
            _validate_prompt_receipt(
                attempt.user_prompt_receipt,
                field=f"attempt[{attempt.attempt_id}].user_prompt_receipt",
                source=source,
                artifact_root=resolved_artifact_root,
            )
class JsonRuntimeStore:
    def load(self, path: Path) -> RuntimeState:
        if path.with_name("migration-pending.json").exists():
            raise StoreMigrationRequired("Store migration is pending; run blackdog repo migrate --project-root PROJECT --json")
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid number {value}")),
            )
        except FileNotFoundError:
            return default_runtime_state()
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            raise StoreError(f"Invalid runtime store {path}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise StoreError(f"{path} must contain a JSON object")
        schema = payload.get("schema_version")
        version = payload.get("store_version")
        if schema != RUNTIME_SCHEMA_VERSION or version != RUNTIME_STORE_VERSION:
            raise StoreMigrationRequired(
                f"Unsupported runtime store {schema!r}/{version!r} in {path}; run blackdog repo migrate --project-root PROJECT --json"
            )
        return runtime_state_from_payload(payload, source=path)

    def save(self, path: Path, state: RuntimeState) -> None:
        if state.schema_version != RUNTIME_SCHEMA_VERSION or state.store_version != RUNTIME_STORE_VERSION:
            raise StoreError("refusing to save an unsupported runtime state")
        _validate_state(state, source=path)
        try:
            serialized = json.dumps(
                runtime_state_to_payload(state),
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise StoreError(f"runtime state is not strict JSON: {exc}") from exc
        atomic_write_text(path, serialized + "\n")


def load_runtime_state(paths: BlackdogPaths, store: RuntimeStore | None = None) -> RuntimeState:
    return (store or JsonRuntimeStore()).load(paths.runtime_file)


def runtime_state_from_payload(payload: Mapping[str, Any], *, source: Path) -> RuntimeState:
    """Validate a current-format payload, including its private replay artifacts."""
    _reject_unknown_keys(payload, allowed=_RUNTIME_KEYS, field="runtime", source=source)
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise StoreError(f"tasks must be a list in {source}")
    state = RuntimeState(
        payload.get("schema_version"), payload.get("store_version"),
        tuple(_task_from_payload(item, source=source) for item in raw_tasks),
    )
    _validate_state(state, source=source)
    return state


def task_index(state: RuntimeState) -> dict[str, TaskRecord]:
    return {task.task_id: task for task in state.tasks}


def task_record(state: RuntimeState, task_id: str) -> TaskRecord | None:
    return task_index(state).get(task_id)


find_task = task_record


def task_attempts(state: RuntimeState, task_id: str) -> tuple[TaskAttemptRecord, ...]:
    task = task_record(state, task_id)
    return task.attempts if task is not None else ()


def find_task_attempt(state: RuntimeState, attempt_id: str) -> TaskAttemptRecord | None:
    for task in state.tasks:
        for attempt in task.attempts:
            if attempt.attempt_id == attempt_id:
                return attempt
    return None


def active_task_attempt(state: RuntimeState, task_id: str) -> TaskAttemptRecord | None:
    return next((item for item in reversed(task_attempts(state, task_id)) if item.status in ATTEMPT_ACTIVE_STATUSES), None)


def latest_task_attempt(state: RuntimeState, task_id: str) -> TaskAttemptRecord | None:
    attempts = task_attempts(state, task_id)
    return attempts[-1] if attempts else None


def replace_task(state: RuntimeState, task: TaskRecord) -> RuntimeState:
    found = False
    rows: list[TaskRecord] = []
    for current in state.tasks:
        if current.task_id == task.task_id:
            rows.append(task)
            found = True
        else:
            rows.append(current)
    if not found:
        rows.append(task)
    return replace(state, tasks=tuple(rows))


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def load_events(path: Path) -> tuple[dict[str, Any], ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    rows: list[dict[str, Any]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(
                raw,
                object_pairs_hook=_unique_json_object,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid number {value}")),
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise StoreError(f"Invalid JSONL row at {path}:{lineno}: {exc}") from exc
        if not isinstance(row, dict):
            raise StoreError(f"Event row at {path}:{lineno} must be an object")
        _reject_unknown_keys(row, allowed=_EVENT_KEYS, field=f"event row {lineno}", source=path)
        event_id = row.get("event_id")
        event_type = row.get("type")
        actor = row.get("actor")
        payload = row.get("payload")
        if not isinstance(event_id, str) or not event_id.strip() or event_id != event_id.strip():
            raise StoreError(f"Event row at {path}:{lineno} has invalid event_id")
        _event_semantics(
            event_type=event_type,
            actor=actor,
            payload=payload,
            source=f"{path}:{lineno}",
        )
        if not isinstance(row.get("at"), str) or parse_iso(row["at"]) is None:
            raise StoreError(f"Event row at {path}:{lineno} has invalid at timestamp")
        rows.append(row)
    event_ids = [row["event_id"] for row in rows]
    if len(set(event_ids)) != len(event_ids):
        raise StoreError(f"Event identities are not unique in {path}")
    return tuple(rows)


def append_event(
    path: Path,
    *,
    event_type: str,
    payload: Mapping[str, Any],
    actor: str = "blackdog",
    event_id: str | None = None,
    durable: bool = False,
) -> dict[str, Any]:
    normalized, _ = _event_semantics(
        event_type=event_type,
        actor=actor,
        payload=payload,
        source=str(event_id or event_type),
    )
    resolved_event_id = event_id or uuid.uuid4().hex
    if not isinstance(resolved_event_id, str) or not resolved_event_id.strip():
        raise StoreError("event_id must be a nonempty string")
    row = {
        "event_id": resolved_event_id,
        "type": event_type,
        "at": now_iso(),
        "actor": actor,
        "payload": normalized,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, allow_nan=False, sort_keys=True) + "\n")
        if durable:
            handle.flush()
            os.fsync(handle.fileno())
    if created and durable:
        _fsync_directory(path.parent)
    return row


def _normalize_json(value: Any, *, source: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StoreError(f"{source} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise StoreError(f"{source} contains a non-string key")
        return {key: _normalize_json(item, source=f"{source}.{key}") for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item, source=f"{source}[]") for item in value]
    raise StoreError(f"{source} contains non-JSON value {type(value).__name__}")


def _event_semantics(*, event_type: Any, actor: Any, payload: Any, source: str) -> tuple[dict[str, Any], str]:
    if not isinstance(event_type, str) or not event_type.strip():
        raise StoreError(f"{source} has invalid type")
    if not isinstance(actor, str) or not actor.strip():
        raise StoreError(f"{source} has invalid actor")
    if not isinstance(payload, Mapping):
        raise StoreError(f"{source} payload must be an object")
    normalized = _normalize_json(payload, source=f"{source}.payload")
    semantics = json.dumps(
        {"type": event_type, "actor": actor, "payload": normalized},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return normalized, semantics


def append_event_once(
    path: Path,
    *,
    event_id: str,
    event_type: str,
    payload: Mapping[str, Any],
    actor: str = "blackdog",
) -> bool:
    if not isinstance(event_id, str) or not event_id.strip():
        raise StoreError("append_event_once requires event_id")
    normalized, semantics = _event_semantics(event_type=event_type, actor=actor, payload=payload, source=event_id)
    with exclusive_file_lock(path):
        matches = [row for row in load_events(path) if row.get("event_id") == event_id]
        if len(matches) > 1:
            raise StoreError(f"Event identity {event_id!r} occurs more than once in {path}")
        if matches:
            row = matches[0]
            _, existing = _event_semantics(
                event_type=row.get("type"), actor=row.get("actor"), payload=row.get("payload"), source=event_id
            )
            if existing != semantics:
                raise StoreError(f"Event identity {event_id!r} already has different content")
            return False
        append_event(path, event_id=event_id, event_type=event_type, actor=actor, payload=normalized, durable=True)
        return True


__all__ = [name for name in globals() if name.isupper()] + [
    "StoreError",
    "ValidationRecord",
    "PromptReceiptRecord",
    "CodexSessionRefRecord",
    "TaskAttemptRecord",
    "TaskRecord",
    "RuntimeState",
    "RuntimeStore",
    "JsonRuntimeStore",
    "new_task_id",
    "new_attempt_id",
    "is_canonical_task_id",
    "is_canonical_attempt_id",
    "now_iso",
    "create_prompt_receipt",
    "prompt_receipt_reference",
    "parse_iso",
    "atomic_write_text",
    "exclusive_file_lock",
    "default_runtime_state",
    "task_record_to_payload",
    "runtime_state_to_payload",
    "load_runtime_state",
    "task_index",
    "task_record",
    "find_task",
    "task_attempts",
    "find_task_attempt",
    "active_task_attempt",
    "latest_task_attempt",
    "replace_task",
    "load_events",
    "append_event",
    "append_event_once",
]
