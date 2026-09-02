"""Task-only lifecycle semantics over the canonical runtime store."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping
import hashlib
import json

from .profile import RepoProfile
from .state import (
    ATTEMPT_STATUS_ABANDONED,
    ATTEMPT_STATUS_BLOCKED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_IN_PROGRESS,
    ATTEMPT_STATUS_SUCCESS,
    ATTEMPT_STATUSES,
    EXECUTION_MODELS,
    EXECUTION_MODEL_DIRECT_WTAM,
    FAILURE_CLASSES,
    FAILURE_CLASS_ABANDONED,
    FAILURE_CLASS_UNKNOWN,
    TASK_STATUS_BLOCKED,
    TASK_STATUS_CANCELED,
    TASK_STATUS_DONE,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_PLANNED,
    CodexSessionRefRecord,
    PromptReceiptRecord,
    RuntimeState,
    RuntimeStore,
    StoreError,
    TaskAttemptRecord,
    TaskRecord,
    ValidationRecord,
    _validate_state,
    append_event_once,
    exclusive_file_lock,
    find_task_attempt,
    is_canonical_attempt_id,
    is_canonical_task_id,
    load_events,
    load_runtime_state,
    new_attempt_id,
    new_task_id,
    now_iso,
    parse_iso,
    replace_task,
    runtime_state_to_payload,
    task_record,
    task_record_to_payload,
)


TASK_STORE_SCHEMA_VERSION = 4


class TaskError(RuntimeError):
    pass


class TaskFinalizationError(TaskError):
    def __init__(self, message: str, *, mutation_started: bool = False, mutation_phase: str = "none") -> None:
        super().__init__(message)
        self.mutation_started = mutation_started
        self.mutation_phase = mutation_phase


class TaskRuntimeTransitionError(TaskError):
    pass


@dataclass(frozen=True, slots=True)
class TaskRuntimeTransitionEvidence:
    stage: str
    unfinished: bool
    request_event_id: str | None
    decision_event_id: str | None
    owned_event_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "unfinished": self.unfinished,
            "request_event_id": self.request_event_id,
            "decision_event_id": self.decision_event_id,
            "owned_event_id": self.owned_event_id,
        }


@dataclass(frozen=True, slots=True)
class TaskFinalizationEvidence:
    stage: str
    complete: bool
    request_event_id: str | None
    decision_event_id: str | None
    task_finish_event_id: str | None
    runtime_finalized: bool
    successor_present: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "complete": self.complete,
            "request_event_id": self.request_event_id,
            "decision_event_id": self.decision_event_id,
            "task_finish_event_id": self.task_finish_event_id,
            "runtime_finalized": self.runtime_finalized,
            "successor_present": self.successor_present,
        }


@dataclass(frozen=True, slots=True)
class TaskRuntimeTransitionResult:
    record: TaskRecord
    runtime_changed: bool
    request_event_appended: bool
    decision_event_appended: bool
    owned_event_appended: bool

    @property
    def events_changed(self) -> bool:
        return self.request_event_appended or self.decision_event_appended or self.owned_event_appended


def _hash_parts(namespace: str, *parts: str) -> str:
    return hashlib.sha256("\0".join((namespace, *parts)).encode("utf-8")).hexdigest()


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _task_hash(task: TaskRecord) -> str:
    return _canonical_hash(task_record_to_payload(task))


def _inspect_task_runtime_transition_rows(
    task: TaskRecord,
    events: tuple[dict[str, Any], ...],
) -> tuple[
    TaskRuntimeTransitionEvidence,
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    current_hash = _task_hash(task)
    current_rows: list[
        tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, str]
    ] = []
    for request in events:
        payload = request.get("payload")
        if (
            request.get("type") != "task.runtime-transition.request"
            or not isinstance(payload, Mapping)
            or payload.get("task_id") != task.task_id
        ):
            continue
        decisions = [
            candidate
            for candidate in events
            if candidate.get("type") == "task.runtime-transition.decision"
            and isinstance(candidate.get("payload"), Mapping)
            and candidate["payload"].get("request_event_id") == request.get("event_id")
        ]
        if len(decisions) > 1:
            raise TaskRuntimeTransitionError(
                f"Task transition request {request.get('event_id')!r} has multiple decisions"
            )
        decision = decisions[0] if decisions else None
        pre_hash = payload.get("pre_task_hash")
        decision_payload = decision.get("payload") if decision is not None else None
        post_hash = (
            decision_payload.get("post_task_hash")
            if isinstance(decision_payload, Mapping)
            else None
        )
        if current_hash not in {pre_hash, post_hash}:
            continue
        if decision is not None:
            assert isinstance(decision_payload, Mapping)
            if (
                decision_payload.get("task_id") != task.task_id
                or decision_payload.get("pre_task_hash") != pre_hash
            ):
                raise TaskRuntimeTransitionError(
                    f"Task transition request {request.get('event_id')!r} has conflicting decision evidence"
                )
        owned_rows = (
            [
                candidate
                for candidate in events
                if candidate.get("type") == "task.transition"
                and isinstance(candidate.get("payload"), Mapping)
                and candidate["payload"].get("task_id") == task.task_id
                and candidate["payload"].get("request_event_id") == request.get("event_id")
                and candidate["payload"].get("decision_event_id") == decision.get("event_id")
            ]
            if decision is not None
            else []
        )
        if len(owned_rows) > 1:
            raise TaskRuntimeTransitionError(
                f"Task transition request {request.get('event_id')!r} has multiple owned events"
            )
        owned = owned_rows[0] if owned_rows else None
        if owned is not None:
            if current_hash != post_hash:
                raise TaskRuntimeTransitionError(
                    f"Task transition request {request.get('event_id')!r} has owned evidence before runtime application"
                )
            stage = "complete"
        elif decision is None:
            stage = "request_recorded"
        elif current_hash == pre_hash:
            stage = "decision_recorded"
        else:
            stage = "runtime_applied"
        current_rows.append((request, decision, owned, stage))

    unfinished = [row for row in current_rows if row[2] is None]
    if len(unfinished) > 1:
        raise TaskRuntimeTransitionError(
            f"Task {task.task_id!r} has ambiguous unfinished transition evidence"
        )
    selected = unfinished[0] if unfinished else (current_rows[-1] if current_rows else None)
    if selected is None:
        return (
            TaskRuntimeTransitionEvidence(
                stage="none",
                unfinished=False,
                request_event_id=None,
                decision_event_id=None,
                owned_event_id=None,
            ),
            None,
            None,
        )
    request, decision, owned, stage = selected
    return (
        TaskRuntimeTransitionEvidence(
            stage=stage,
            unfinished=owned is None,
            request_event_id=str(request["event_id"]),
            decision_event_id=str(decision["event_id"]) if decision is not None else None,
            owned_event_id=str(owned["event_id"]) if owned is not None else None,
        ),
        request,
        decision,
    )


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskError(f"{field} is required")
    if value != value.strip():
        raise TaskError(f"{field} must not have surrounding whitespace")
    return value


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field=field)


def _string_tuple(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TaskError(f"{field} must be an immutable string sequence")
    for item in value:
        _required_text(item, field=field)
    return value


def _require_task(state: RuntimeState, task_id: str) -> TaskRecord:
    task = task_record(state, task_id)
    if task is None:
        raise TaskError(f"Unknown task {task_id!r}")
    return task


def _replace_attempt(task: TaskRecord, attempt: TaskAttemptRecord) -> TaskRecord:
    found = False
    attempts: list[TaskAttemptRecord] = []
    for current in task.attempts:
        if current.attempt_id == attempt.attempt_id:
            attempts.append(attempt)
            found = True
        else:
            attempts.append(current)
    if not found:
        attempts.append(attempt)
    return replace(task, attempts=tuple(attempts))


def _same_start_request(left: TaskAttemptRecord, right: TaskAttemptRecord) -> bool:
    return replace(left, attempt_id=right.attempt_id, started_at=right.started_at) == right


def normalize_failure_class(value: str | None) -> str | None:
    resolved = _optional_text(value, field="failure_class")
    if resolved is not None and resolved not in FAILURE_CLASSES:
        raise TaskError(f"failure_class must be one of {', '.join(sorted(FAILURE_CLASSES))}")
    return resolved


def default_failure_class_for_status(status: str, failure_class: str | None = None) -> str | None:
    resolved = normalize_failure_class(failure_class)
    if resolved is not None:
        return resolved
    if status == ATTEMPT_STATUS_SUCCESS:
        return None
    if status == ATTEMPT_STATUS_ABANDONED:
        return FAILURE_CLASS_ABANDONED
    if status in {ATTEMPT_STATUS_BLOCKED, ATTEMPT_STATUS_FAILED}:
        return FAILURE_CLASS_UNKNOWN
    return None


def create_task(
    profile: RepoProfile,
    *,
    title: str,
    task_id: str | None = None,
    created_at: str | None = None,
    runtime_store: RuntimeStore | None = None,
) -> TaskRecord:
    resolved_title = str(title).strip()
    if not resolved_title:
        raise TaskError("task title is required")
    resolved_id = str(task_id or new_task_id()).strip()
    if not is_canonical_task_id(resolved_id):
        raise TaskError("task_id must be a canonical globally unique task identity")
    resolved_created_at = _optional_text(created_at, field="created_at") or now_iso()
    candidate = TaskRecord(
        task_id=resolved_id,
        title=resolved_title,
        created_at=resolved_created_at,
        updated_at=resolved_created_at,
    )
    event_id = _hash_parts("blackdog.task.create/v4", resolved_id)
    event_payload = {
        "schema_version": TASK_STORE_SCHEMA_VERSION,
        "task_id": resolved_id,
        "title": candidate.title,
        "created_at": candidate.created_at,
    }
    resolved_store = runtime_store
    result: TaskRecord | None = None
    with exclusive_file_lock(profile.paths.runtime_file):
        state = load_runtime_state(profile.paths, resolved_store)
        existing = task_record(state, resolved_id)
        if existing is not None:
            if (
                existing.title != candidate.title
                or (created_at is not None and existing.created_at != candidate.created_at)
            ):
                raise TaskError(f"Task identity {resolved_id!r} is already reserved with different content")
            result = existing
        else:
            prior_events = [
                row for row in load_events(profile.paths.events_file) if row.get("event_id") == event_id
            ]
            if prior_events:
                if len(prior_events) != 1 or prior_events[0].get("type") != "task.create":
                    raise TaskError(f"Task identity {resolved_id!r} has conflicting creation evidence")
                prior_payload = prior_events[0].get("payload")
                if (
                    not isinstance(prior_payload, Mapping)
                    or prior_payload.get("title") != candidate.title
                ):
                    raise TaskError(f"Task identity {resolved_id!r} has conflicting creation evidence")
                candidate = replace(
                    candidate,
                    created_at=str(prior_payload.get("created_at")),
                    updated_at=str(prior_payload.get("created_at")),
                )
                event_payload = dict(prior_payload)
            next_state = replace_task(state, candidate)
            _validate_state(next_state, source=profile.paths.runtime_file)
            append_event_once(
                profile.paths.events_file,
                event_id=event_id,
                event_type="task.create",
                actor="blackdog",
                payload=event_payload,
            )
            (resolved_store or _json_store()).save(profile.paths.runtime_file, next_state)
            result = candidate
    assert result is not None
    append_event_once(
        profile.paths.events_file,
        event_id=event_id,
        event_type="task.create",
        actor="blackdog",
        payload=event_payload,
    )
    return result


def _json_store():
    from .state import JsonRuntimeStore

    return JsonRuntimeStore()


def task_start_event_id(*, attempt_id: str, event_type: str = "task.start") -> str:
    if event_type != "task.start":
        raise TaskError(f"unsupported task start event type {event_type!r}")
    return _hash_parts("blackdog.task.start-event/v4", attempt_id, event_type)


def task_start_event_contracts(*, task_id: str, attempt: TaskAttemptRecord) -> tuple[dict[str, Any], ...]:
    return (
        {
            "event_id": task_start_event_id(attempt_id=attempt.attempt_id),
            "event_type": "task.start",
            "actor": attempt.actor,
            "payload": {
                "schema_version": TASK_STORE_SCHEMA_VERSION,
                "task_id": task_id,
                "attempt_id": attempt.attempt_id,
                "execution_model": attempt.execution_model,
                "started_at": attempt.started_at,
            },
        },
    )


def _repair_start_events(profile: RepoProfile, *, task_id: str, attempt: TaskAttemptRecord) -> None:
    for contract in task_start_event_contracts(task_id=task_id, attempt=attempt):
        append_event_once(
            profile.paths.events_file,
            event_id=contract["event_id"],
            event_type=contract["event_type"],
            actor=contract["actor"],
            payload=contract["payload"],
        )


def start_task(
    profile: RepoProfile,
    *,
    task_id: str,
    actor: str,
    execution_model: str = EXECUTION_MODEL_DIRECT_WTAM,
    workspace_identity: str | None = None,
    workspace_mode: str | None = None,
    worktree_role: str | None = None,
    worktree_path: str | None = None,
    branch: str | None = None,
    target_branch: str | None = None,
    integration_branch: str | None = None,
    start_commit: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    codex_session: CodexSessionRefRecord | None = None,
    prompt_receipt: PromptReceiptRecord | None = None,
    user_prompt_receipt: PromptReceiptRecord | None = None,
    note: str | None = None,
    setup_receipt: Mapping[str, Any] | None = None,
    attempt_id: str | None = None,
    started_at: str | None = None,
    runtime_store: RuntimeStore | None = None,
) -> TaskAttemptRecord:
    if execution_model not in EXECUTION_MODELS:
        raise TaskError(f"Unsupported execution_model {execution_model!r}")
    resolved_actor = _required_text(actor, field="actor")
    resolved_attempt_id = str(attempt_id or new_attempt_id()).strip()
    if not is_canonical_attempt_id(resolved_attempt_id):
        raise TaskError("attempt_id must be a canonical globally unique attempt identity")
    resolved_started_at = _optional_text(started_at, field="started_at") or now_iso()
    candidate = TaskAttemptRecord(
        task_id=task_id,
        attempt_id=resolved_attempt_id,
        status=ATTEMPT_STATUS_IN_PROGRESS,
        actor=resolved_actor,
        started_at=resolved_started_at,
        workspace_identity=_optional_text(workspace_identity, field="workspace_identity"),
        workspace_mode=_optional_text(workspace_mode, field="workspace_mode"),
        worktree_role=_optional_text(worktree_role, field="worktree_role"),
        worktree_path=_optional_text(worktree_path, field="worktree_path"),
        branch=_optional_text(branch, field="branch"),
        target_branch=_optional_text(target_branch, field="target_branch"),
        integration_branch=_optional_text(integration_branch, field="integration_branch"),
        start_commit=_optional_text(start_commit, field="start_commit"),
        execution_model=execution_model,
        model=_optional_text(model, field="model"),
        reasoning_effort=_optional_text(reasoning_effort, field="reasoning_effort"),
        codex_session=codex_session,
        prompt_receipt=prompt_receipt,
        user_prompt_receipt=user_prompt_receipt,
        note=_optional_text(note, field="note"),
        setup_receipt=dict(setup_receipt) if setup_receipt is not None else None,
    )
    result: TaskAttemptRecord | None = None
    store = runtime_store or _json_store()
    with exclusive_file_lock(profile.paths.runtime_file):
        state = store.load(profile.paths.runtime_file)
        task = _require_task(state, task_id)
        existing_by_id = find_task_attempt(state, resolved_attempt_id)
        active = next((item for item in reversed(task.attempts) if item.status == ATTEMPT_STATUS_IN_PROGRESS), None)
        if existing_by_id is not None:
            if started_at is None:
                candidate = replace(candidate, started_at=existing_by_id.started_at)
            if existing_by_id.task_id != task_id or existing_by_id != candidate:
                raise TaskError(f"Attempt identity {resolved_attempt_id!r} conflicts with durable state")
            result = existing_by_id
        elif active is not None and attempt_id is None and _same_start_request(active, candidate):
            result = active
        elif active is not None:
            raise TaskError(f"Task {task_id!r} is already executing attempt {active.attempt_id!r}")
        elif task.status != TASK_STATUS_PLANNED:
            raise TaskError(f"Task {task_id!r} is {task.status!r}; return it to planned before starting")
        else:
            updated = replace(
                task,
                status=TASK_STATUS_IN_PROGRESS,
                updated_at=resolved_started_at,
                actor=resolved_actor,
                note=candidate.note,
                failure_class=None,
                recovery_action=None,
                prompt_issue=False,
                operator_issue=False,
                attempts=tuple([*task.attempts, candidate]),
            )
            next_state = replace_task(state, updated)
            _validate_state(next_state, source=profile.paths.runtime_file)
            store.save(profile.paths.runtime_file, next_state)
            result = candidate
    assert result is not None
    _repair_start_events(profile, task_id=task_id, attempt=result)
    return result


def repair_task_start_events(profile: RepoProfile, *, task_id: str, attempt_id: str) -> TaskAttemptRecord:
    state = load_runtime_state(profile.paths)
    task = _require_task(state, task_id)
    attempt = next((item for item in task.attempts if item.attempt_id == attempt_id), None)
    if attempt is None or attempt.status != ATTEMPT_STATUS_IN_PROGRESS:
        raise TaskError(f"Attempt {attempt_id!r} is not the active attempt for task {task_id!r}")
    _repair_start_events(profile, task_id=task_id, attempt=attempt)
    return attempt


def task_resume_attempt_id(
    *,
    task_id: str,
    predecessor_attempt_id: str,
    actor: str,
    execution_prompt_hash: str,
    request_prompt_hash: str,
) -> str:
    digest = _hash_parts(
        "blackdog.task.resume-attempt/v4",
        task_id,
        predecessor_attempt_id,
        actor,
        execution_prompt_hash,
        request_prompt_hash,
    )
    return f"attempt-{digest[:32]}"


def _request_event_id(*, task_id: str, attempt_id: str, finalization_id: str) -> str:
    return _hash_parts("blackdog.task.finalization-request/v4", task_id, attempt_id, finalization_id)


def _decision_event_id(*, request_event_id: str, pre_task_hash: str) -> str:
    return _hash_parts("blackdog.task.finalization-decision/v4", request_event_id, pre_task_hash)


def _finish_event_id(*, decision_event_id: str) -> str:
    return _hash_parts("blackdog.task.finalization-owned/v4", decision_event_id, "task.finish")


def _terminal_task_status(status: str) -> str:
    if status == ATTEMPT_STATUS_SUCCESS:
        return TASK_STATUS_DONE
    if status == ATTEMPT_STATUS_ABANDONED:
        return TASK_STATUS_CANCELED
    return TASK_STATUS_BLOCKED


def _elapsed(started_at: str, ended_at: str) -> int | None:
    start = parse_iso(started_at)
    end = parse_iso(ended_at)
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _finished_attempt(attempt: TaskAttemptRecord, request: Mapping[str, Any], ended_at: str) -> TaskAttemptRecord:
    return replace(
        attempt,
        status=str(request["status"]),
        ended_at=ended_at,
        summary=request.get("summary"),
        changed_paths=tuple(request.get("changed_paths") or ()),
        validations=tuple(
            ValidationRecord(name=str(item["name"]), status=str(item["status"]))
            for item in request.get("validations") or ()
        ),
        residuals=tuple(request.get("residuals") or ()),
        followup_candidates=tuple(request.get("followup_candidates") or ()),
        note=request.get("note"),
        commit=request.get("commit"),
        landed_commit=request.get("landed_commit"),
        elapsed_seconds=(
            request.get("elapsed_seconds")
            if request.get("elapsed_seconds") is not None
            else _elapsed(attempt.started_at, ended_at)
        ),
        failure_class=request.get("failure_class"),
        recovery_action=request.get("recovery_action"),
        prompt_issue=bool(request.get("prompt_issue")),
        operator_issue=bool(request.get("operator_issue")),
    )


def _attempt_matches_request(attempt: TaskAttemptRecord, request: Mapping[str, Any]) -> bool:
    expected_elapsed = request.get("elapsed_seconds")
    if expected_elapsed is None and attempt.ended_at is not None:
        expected_elapsed = _elapsed(attempt.started_at, attempt.ended_at)
    return (
        attempt.status == request.get("status")
        and attempt.summary == request.get("summary")
        and attempt.changed_paths == tuple(request.get("changed_paths") or ())
        and tuple((item.name, item.status) for item in attempt.validations)
        == tuple((item["name"], item["status"]) for item in request.get("validations") or ())
        and attempt.residuals == tuple(request.get("residuals") or ())
        and attempt.followup_candidates == tuple(request.get("followup_candidates") or ())
        and attempt.note == request.get("note")
        and attempt.commit == request.get("commit")
        and attempt.landed_commit == request.get("landed_commit")
        and attempt.elapsed_seconds == expected_elapsed
        and attempt.failure_class == request.get("failure_class")
        and attempt.recovery_action == request.get("recovery_action")
        and attempt.prompt_issue is bool(request.get("prompt_issue"))
        and attempt.operator_issue is bool(request.get("operator_issue"))
    )


def _matching_decision(events: tuple[dict[str, Any], ...], request_event_id: str) -> dict[str, Any] | None:
    rows = [
        row
        for row in events
        if row.get("type") == "task.finalization.decision"
        and isinstance(row.get("payload"), Mapping)
        and row["payload"].get("request_event_id") == request_event_id
    ]
    if len(rows) > 1:
        raise TaskError(f"Finalization request {request_event_id!r} has multiple decisions")
    return rows[0] if rows else None


def finish_task(
    profile: RepoProfile,
    *,
    task_id: str,
    attempt_id: str,
    actor: str,
    status: str,
    summary: str,
    changed_paths: tuple[str, ...] = (),
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    commit: str | None = None,
    landed_commit: str | None = None,
    elapsed_seconds: int | None = None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
    note: str | None = None,
    finalization_id: str | None = None,
    runtime_store: RuntimeStore | None = None,
) -> TaskAttemptRecord:
    if status not in ATTEMPT_STATUSES - {ATTEMPT_STATUS_IN_PROGRESS}:
        raise TaskError("finish status must be terminal")
    resolved_actor = _required_text(actor, field="actor")
    resolved_summary = _required_text(summary, field="summary")
    resolved_changed_paths = _string_tuple(changed_paths, field="changed_paths")
    resolved_residuals = _string_tuple(residuals, field="residuals")
    resolved_followups = _string_tuple(followup_candidates, field="followup_candidates")
    if not isinstance(validations, tuple) or any(
        not isinstance(item, ValidationRecord)
        or item.status not in {"passed", "failed", "skipped"}
        or not isinstance(item.name, str)
        or not item.name.strip()
        or item.name != item.name.strip()
        for item in validations
    ):
        raise TaskError("validation evidence is invalid")
    if elapsed_seconds is not None and (type(elapsed_seconds) is not int or elapsed_seconds < 0):
        raise TaskError("elapsed_seconds must be a non-negative integer")
    if type(prompt_issue) is not bool or type(operator_issue) is not bool:
        raise TaskError("issue flags must be booleans")
    resolved_note = _optional_text(note, field="note")
    resolved_recovery = _optional_text(recovery_action, field="recovery_action")
    resolved_commit = _optional_text(commit, field="commit")
    resolved_landed_commit = _optional_text(landed_commit, field="landed_commit")
    resolved_failure = default_failure_class_for_status(status, failure_class)
    resolved_finalization = _required_text(
        finalization_id or f"finish-{attempt_id}",
        field="finalization_id",
    )
    request_event_id = _request_event_id(
        task_id=task_id, attempt_id=attempt_id, finalization_id=resolved_finalization
    )
    request = {
        "schema_version": TASK_STORE_SCHEMA_VERSION,
        "finalization_id": resolved_finalization,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "actor": resolved_actor,
        "status": status,
        "summary": resolved_summary,
        "changed_paths": list(resolved_changed_paths),
        "validations": [{"name": item.name, "status": item.status} for item in validations],
        "residuals": list(resolved_residuals),
        "followup_candidates": list(resolved_followups),
        "commit": resolved_commit,
        "landed_commit": resolved_landed_commit,
        "elapsed_seconds": elapsed_seconds,
        "failure_class": resolved_failure,
        "recovery_action": resolved_recovery,
        "prompt_issue": prompt_issue,
        "operator_issue": operator_issue,
        "note": resolved_note,
    }
    store = runtime_store or _json_store()
    result: TaskAttemptRecord | None = None
    decision_event_id: str | None = None
    try:
        with exclusive_file_lock(profile.paths.runtime_file):
            state = store.load(profile.paths.runtime_file)
            task = _require_task(state, task_id)
            attempt = next((item for item in task.attempts if item.attempt_id == attempt_id), None)
            if attempt is None:
                raise TaskError(f"Unknown attempt {attempt_id!r} for task {task_id!r}")
            if attempt.actor != resolved_actor:
                raise TaskError(f"Attempt {attempt_id!r} belongs to actor {attempt.actor!r}")
            events = load_events(profile.paths.events_file)
            existing_requests = [
                row
                for row in events
                if row.get("type") == "task.finalization.request"
                and isinstance(row.get("payload"), Mapping)
                and row["payload"].get("task_id") == task_id
                and row["payload"].get("attempt_id") == attempt_id
            ]
            if len(existing_requests) > 1:
                raise TaskError(f"Attempt {attempt_id!r} has multiple finalization requests")
            if existing_requests and existing_requests[0].get("event_id") != request_event_id:
                raise TaskError(f"Attempt {attempt_id!r} already has a different finalization identity")
            if not existing_requests and attempt.status != ATTEMPT_STATUS_IN_PROGRESS:
                raise TaskError(f"Terminal attempt {attempt_id!r} has no matching finalization request")
            if attempt.status == ATTEMPT_STATUS_IN_PROGRESS:
                preview_ended_at = now_iso()
                preview_attempt = _finished_attempt(attempt, request, preview_ended_at)
                preview_task = replace(
                    _replace_attempt(task, preview_attempt),
                    status=_terminal_task_status(status),
                    updated_at=preview_ended_at,
                    actor=resolved_actor,
                    note=resolved_summary,
                    failure_class=resolved_failure,
                    recovery_action=resolved_recovery,
                    prompt_issue=prompt_issue,
                    operator_issue=operator_issue,
                )
                try:
                    _validate_state(
                        replace_task(state, preview_task),
                        source=profile.paths.runtime_file,
                    )
                except StoreError as exc:
                    raise TaskError(f"invalid task finalization request: {exc}") from exc
            append_event_once(
                profile.paths.events_file,
                event_id=request_event_id,
                event_type="task.finalization.request",
                actor=resolved_actor,
                payload=request,
            )
            events = load_events(profile.paths.events_file)
            decision = _matching_decision(events, request_event_id)
            if decision is not None:
                payload = decision["payload"]
                decision_event_id = str(decision["event_id"])
                ended_at = str(payload["ended_at"])
                terminal = _finished_attempt(attempt, request, ended_at)
                if attempt.status == ATTEMPT_STATUS_IN_PROGRESS:
                    if _task_hash(task) != payload.get("pre_task_hash"):
                        raise TaskError("Finalization decision no longer names the active task generation")
                    updated_task = replace(
                        _replace_attempt(task, terminal),
                        status=_terminal_task_status(status),
                        updated_at=ended_at,
                        actor=resolved_actor,
                        note=resolved_summary,
                        failure_class=resolved_failure,
                        recovery_action=resolved_recovery,
                        prompt_issue=prompt_issue,
                        operator_issue=operator_issue,
                    )
                    if _task_hash(updated_task) != payload.get("post_task_hash"):
                        raise TaskError("Finalization decision post-state hash conflicts")
                    store.save(profile.paths.runtime_file, replace_task(state, updated_task))
                    result = terminal
                elif _attempt_matches_request(attempt, request):
                    result = attempt
                else:
                    raise TaskError("Terminal attempt conflicts with finalization request")
            else:
                if attempt.status != ATTEMPT_STATUS_IN_PROGRESS:
                    if not _attempt_matches_request(attempt, request):
                        raise TaskError("Terminal attempt conflicts with finalization request")
                    result = attempt
                    ended_at = attempt.ended_at or now_iso()
                else:
                    ended_at = now_iso()
                    terminal = _finished_attempt(attempt, request, ended_at)
                    updated_task = replace(
                        _replace_attempt(task, terminal),
                        status=_terminal_task_status(status),
                        updated_at=ended_at,
                        actor=resolved_actor,
                        note=resolved_summary,
                        failure_class=resolved_failure,
                        recovery_action=resolved_recovery,
                        prompt_issue=prompt_issue,
                        operator_issue=operator_issue,
                    )
                    pre_hash = _task_hash(task)
                    decision_event_id = _decision_event_id(
                        request_event_id=request_event_id, pre_task_hash=pre_hash
                    )
                    decision_payload = {
                        "schema_version": TASK_STORE_SCHEMA_VERSION,
                        "request_event_id": request_event_id,
                        "task_id": task_id,
                        "attempt_id": attempt_id,
                        "ended_at": ended_at,
                        "pre_task_hash": pre_hash,
                        "post_task_hash": _task_hash(updated_task),
                    }
                    append_event_once(
                        profile.paths.events_file,
                        event_id=decision_event_id,
                        event_type="task.finalization.decision",
                        actor=resolved_actor,
                        payload=decision_payload,
                    )
                    store.save(profile.paths.runtime_file, replace_task(state, updated_task))
                    result = terminal
                if decision_event_id is None:
                    decision = _matching_decision(load_events(profile.paths.events_file), request_event_id)
                    if decision is not None:
                        decision_event_id = str(decision["event_id"])
        if result is None:
            raise TaskError("task finalization produced no attempt")
        if decision_event_id is None:
            raise TaskError("task finalization has no durable decision")
        append_event_once(
            profile.paths.events_file,
            event_id=_finish_event_id(decision_event_id=decision_event_id),
            event_type="task.finish",
            actor=resolved_actor,
            payload={
                "schema_version": TASK_STORE_SCHEMA_VERSION,
                "request_event_id": request_event_id,
                "decision_event_id": decision_event_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "status": status,
                "ended_at": result.ended_at,
            },
        )
        return result
    except (StoreError, OSError) as exc:
        evidence = inspect_task_finalization(profile, task_id=task_id, attempt_id=attempt_id)
        raise TaskFinalizationError(
            f"task finalization interrupted: {exc}",
            mutation_started=evidence.request_event_id is not None,
            mutation_phase=evidence.stage,
        ) from exc


def inspect_task_finalization(
    profile: RepoProfile,
    *,
    task_id: str,
    attempt_id: str,
) -> TaskFinalizationEvidence:
    events = load_events(profile.paths.events_file)
    requests = [
        row
        for row in events
        if row.get("type") == "task.finalization.request"
        and isinstance(row.get("payload"), Mapping)
        and row["payload"].get("task_id") == task_id
        and row["payload"].get("attempt_id") == attempt_id
    ]
    if len(requests) > 1:
        raise TaskError(f"Attempt {attempt_id!r} has multiple finalization requests")
    request = requests[0] if requests else None
    request_id = str(request["event_id"]) if request else None
    decision = _matching_decision(events, request_id) if request_id else None
    decision_id = str(decision["event_id"]) if decision else None
    finish = next(
        (
            row
            for row in events
            if row.get("type") == "task.finish"
            and isinstance(row.get("payload"), Mapping)
            and row["payload"].get("decision_event_id") == decision_id
        ),
        None,
    )
    state = load_runtime_state(profile.paths)
    task = task_record(state, task_id)
    attempt = (
        next((item for item in task.attempts if item.attempt_id == attempt_id), None)
        if task is not None
        else None
    )
    runtime_finalized = attempt is not None and attempt.status != ATTEMPT_STATUS_IN_PROGRESS
    successor_present = bool(
        task is not None
        and attempt is not None
        and any(item.started_at >= attempt.started_at and item.attempt_id != attempt_id for item in task.attempts)
    )
    if finish is not None:
        stage = "complete"
    elif runtime_finalized:
        stage = "runtime_finalized"
    elif decision is not None:
        stage = "decision_recorded"
    elif request is not None:
        stage = "request_recorded"
    else:
        stage = "none"
    return TaskFinalizationEvidence(
        stage=stage,
        complete=finish is not None and runtime_finalized,
        request_event_id=request_id,
        decision_event_id=decision_id,
        task_finish_event_id=str(finish["event_id"]) if finish else None,
        runtime_finalized=runtime_finalized,
        successor_present=successor_present,
    )


def inspect_task_runtime_transition(
    profile: RepoProfile,
    *,
    task_id: str,
) -> TaskRuntimeTransitionEvidence:
    with exclusive_file_lock(profile.paths.runtime_file):
        state = load_runtime_state(profile.paths)
        task = _require_task(state, task_id)
        events = load_events(profile.paths.events_file)
        evidence, _, _ = _inspect_task_runtime_transition_rows(task, events)
        return evidence


def set_task_runtime_status(
    profile: RepoProfile,
    *,
    task_id: str,
    actor: str,
    status: str,
    summary: str | None = None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
    return_transition_result: bool = False,
    runtime_store: RuntimeStore | None = None,
) -> TaskRecord | TaskRuntimeTransitionResult:
    if status not in {TASK_STATUS_PLANNED, TASK_STATUS_BLOCKED, TASK_STATUS_CANCELED}:
        raise TaskRuntimeTransitionError(f"unsupported explicit task status {status!r}")
    try:
        resolved_actor = _required_text(actor, field="actor")
        resolved_summary = _optional_text(summary, field="summary")
        resolved_recovery = _optional_text(recovery_action, field="recovery_action")
        resolved_failure = normalize_failure_class(failure_class)
    except TaskError as exc:
        raise TaskRuntimeTransitionError(str(exc)) from exc
    if type(prompt_issue) is not bool or type(operator_issue) is not bool:
        raise TaskRuntimeTransitionError("issue flags must be booleans")
    desired = {
        "schema_version": TASK_STORE_SCHEMA_VERSION,
        "task_id": task_id,
        "actor": resolved_actor,
        "status": status,
        "summary": resolved_summary,
        "failure_class": resolved_failure,
        "recovery_action": resolved_recovery,
        "prompt_issue": prompt_issue,
        "operator_issue": operator_issue,
    }
    store = runtime_store or _json_store()
    runtime_changed = False
    request_event_appended = False
    decision_event_appended = False
    owned_event_appended = False
    request_event_id: str | None = None
    decision_event_id: str | None = None
    updated: TaskRecord | None = None
    with exclusive_file_lock(profile.paths.runtime_file):
        state = store.load(profile.paths.runtime_file)
        task = _require_task(state, task_id)
        if any(item.status == ATTEMPT_STATUS_IN_PROGRESS for item in task.attempts):
            raise TaskRuntimeTransitionError(f"Task {task_id!r} has an in-progress attempt")
        allowed_sources = {
            TASK_STATUS_PLANNED: {TASK_STATUS_PLANNED, TASK_STATUS_CANCELED},
            TASK_STATUS_BLOCKED: {TASK_STATUS_PLANNED, TASK_STATUS_BLOCKED},
            TASK_STATUS_CANCELED: {TASK_STATUS_PLANNED, TASK_STATUS_BLOCKED, TASK_STATUS_CANCELED},
        }
        if task.status not in allowed_sources[status]:
            raise TaskRuntimeTransitionError(
                f"Task {task_id!r} cannot transition from {task.status!r} to {status!r}"
            )
        already_matches = (
            task.status == status
            and task.actor == resolved_actor
            and task.note == resolved_summary
            and task.failure_class == resolved_failure
            and task.recovery_action == resolved_recovery
            and task.prompt_issue is prompt_issue
            and task.operator_issue is operator_issue
        )
        events = load_events(profile.paths.events_file)
        current_hash = _task_hash(task)
        transition_evidence, pending_request, _ = _inspect_task_runtime_transition_rows(task, events)
        if transition_evidence.unfinished:
            assert pending_request is not None
            pending_payload = pending_request["payload"]
            assert isinstance(pending_payload, Mapping)
            expected_payload = {
                **desired,
                "pre_task_hash": pending_payload.get("pre_task_hash"),
            }
            if dict(pending_payload) != expected_payload:
                raise TaskRuntimeTransitionError(
                    f"Task {task_id!r} has an unfinished runtime transition; only the exact original request may retry"
                )
        candidates: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        for row in events:
            payload = row.get("payload")
            if row.get("type") != "task.runtime-transition.request" or not isinstance(payload, Mapping):
                continue
            if any(payload.get(key) != value for key, value in desired.items()):
                continue
            decision_rows = [
                candidate
                for candidate in events
                if candidate.get("type") == "task.runtime-transition.decision"
                and isinstance(candidate.get("payload"), Mapping)
                and candidate["payload"].get("request_event_id") == row.get("event_id")
            ]
            if len(decision_rows) > 1:
                raise TaskRuntimeTransitionError(
                    f"Task transition request {row.get('event_id')!r} has multiple decisions"
                )
            decision = decision_rows[0] if decision_rows else None
            pre_hash = payload.get("pre_task_hash")
            post_hash = decision["payload"].get("post_task_hash") if decision is not None else None
            if current_hash in {pre_hash, post_hash}:
                candidates.append((row, decision))
        if len(candidates) > 1:
            raise TaskRuntimeTransitionError(f"Task {task_id!r} has ambiguous transition evidence")
        selected_request, selected_decision = candidates[0] if candidates else (None, None)

        if selected_request is None and already_matches:
            updated = task
        else:
            if selected_request is None:
                request_payload = {**desired, "pre_task_hash": current_hash}
                request_event_id = _hash_parts(
                    "blackdog.task.runtime-transition-request/v4",
                    task_id,
                    current_hash,
                    _canonical_hash(request_payload),
                )
            else:
                request_payload = dict(selected_request["payload"])
                request_event_id = str(selected_request["event_id"])

            if selected_decision is None:
                updated_at = now_iso()
                updated = replace(
                    task,
                    status=status,
                    updated_at=updated_at,
                    actor=resolved_actor,
                    note=resolved_summary,
                    failure_class=resolved_failure,
                    recovery_action=resolved_recovery,
                    prompt_issue=prompt_issue,
                    operator_issue=operator_issue,
                )
                next_state = replace_task(state, updated)
                try:
                    _validate_state(next_state, source=profile.paths.runtime_file)
                except StoreError as exc:
                    raise TaskRuntimeTransitionError(str(exc)) from exc
                if selected_request is None:
                    request_event_appended = append_event_once(
                        profile.paths.events_file,
                        event_id=request_event_id,
                        event_type="task.runtime-transition.request",
                        actor=resolved_actor,
                        payload=request_payload,
                    )
                decision_payload = {
                    "schema_version": TASK_STORE_SCHEMA_VERSION,
                    "request_event_id": request_event_id,
                    "task_id": task_id,
                    "updated_at": updated_at,
                    "pre_task_hash": request_payload["pre_task_hash"],
                    "post_task_hash": _task_hash(updated),
                }
                decision_event_id = _hash_parts(
                    "blackdog.task.runtime-transition-decision/v4",
                    request_event_id,
                    str(request_payload["pre_task_hash"]),
                )
                decision_event_appended = append_event_once(
                    profile.paths.events_file,
                    event_id=decision_event_id,
                    event_type="task.runtime-transition.decision",
                    actor=resolved_actor,
                    payload=decision_payload,
                )
            else:
                decision_payload = dict(selected_decision["payload"])
                decision_event_id = str(selected_decision["event_id"])
                updated = replace(
                    task,
                    status=status,
                    updated_at=str(decision_payload["updated_at"]),
                    actor=resolved_actor,
                    note=resolved_summary,
                    failure_class=resolved_failure,
                    recovery_action=resolved_recovery,
                    prompt_issue=prompt_issue,
                    operator_issue=operator_issue,
                )

            if current_hash == decision_payload["pre_task_hash"]:
                if _task_hash(updated) != decision_payload["post_task_hash"]:
                    raise TaskRuntimeTransitionError("Task transition decision post-state hash conflicts")
                if updated != task:
                    store.save(profile.paths.runtime_file, replace_task(state, updated))
                    runtime_changed = True
            elif current_hash == decision_payload["post_task_hash"]:
                updated = task
            else:
                raise TaskRuntimeTransitionError("Task transition decision no longer names this task generation")

    if updated is None:
        raise TaskRuntimeTransitionError("Task transition produced no result")
    if request_event_id is not None and decision_event_id is not None:
        owned_event_appended = append_event_once(
            profile.paths.events_file,
            event_id=_hash_parts(
                "blackdog.task.runtime-transition-owned/v4", decision_event_id, "task.transition"
            ),
            event_type="task.transition",
            actor=resolved_actor,
            payload={
                "schema_version": TASK_STORE_SCHEMA_VERSION,
                "request_event_id": request_event_id,
                "decision_event_id": decision_event_id,
                "task_id": task_id,
                "status": status,
                "updated_at": updated.updated_at,
            },
        )
    result = TaskRuntimeTransitionResult(
        record=updated,
        runtime_changed=runtime_changed,
        request_event_appended=request_event_appended,
        decision_event_appended=decision_event_appended,
        owned_event_appended=owned_event_appended,
    )
    return result if return_transition_result else updated


def reconcile_landed_attempt(
    profile: RepoProfile,
    *,
    task_id: str,
    attempt_id: str,
    landed_commit: str,
    actor: str,
    summary: str,
    source_commit: str,
    target_commit: str,
    target_branch: str,
    landing_transaction_id: str,
    landing_transaction_phases: tuple[str, ...],
    current_format: bool,
    changed_paths: tuple[str, ...] = (),
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
    reason: str | None = None,
    runtime_store: RuntimeStore | None = None,
) -> dict[str, Any]:
    if current_format is not True:
        raise TaskError("landing reconciliation requires current-format proof")
    resolved_actor = _required_text(actor, field="actor")
    resolved_summary = _required_text(summary, field="summary")
    resolved_source_commit = _required_text(source_commit, field="source_commit")
    resolved_target_commit = _required_text(target_commit, field="target_commit")
    resolved_landed_commit = _required_text(landed_commit, field="landed_commit")
    resolved_target_branch = _required_text(target_branch, field="target_branch")
    resolved_transaction_id = _required_text(landing_transaction_id, field="landing_transaction_id")
    resolved_phases = _string_tuple(landing_transaction_phases, field="landing_transaction_phases")
    resolved_reason = _optional_text(reason, field="reason")
    if resolved_target_commit != resolved_landed_commit:
        raise TaskError("landing reconciliation target commit must equal the landed commit")
    if "target_updated" not in resolved_phases or "runtime_finalized" in resolved_phases:
        raise TaskError("landing reconciliation requires target_updated before runtime_finalized")
    for field, value in (
        ("source_commit", resolved_source_commit),
        ("target_commit", resolved_target_commit),
    ):
        if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
            raise TaskError(f"landing reconciliation {field} must be a canonical commit id")

    state = load_runtime_state(profile.paths, runtime_store)
    task = _require_task(state, task_id)
    attempt = next((item for item in task.attempts if item.attempt_id == attempt_id), None)
    if attempt is None:
        raise TaskError(f"Unknown attempt {attempt_id!r}")
    if attempt.target_branch != resolved_target_branch:
        raise TaskError("landing reconciliation target branch conflicts with the active attempt")
    finalization_id = f"landing-reconcile-{resolved_transaction_id}"
    request_event_id = _request_event_id(
        task_id=task_id,
        attempt_id=attempt_id,
        finalization_id=finalization_id,
    )
    runtime_changed = attempt.status == ATTEMPT_STATUS_IN_PROGRESS
    if not runtime_changed:
        exact_retry = any(
            row.get("event_id") == request_event_id and row.get("type") == "task.finalization.request"
            for row in load_events(profile.paths.events_file)
        )
        if not exact_retry:
            raise TaskError("landing reconciliation cannot rewrite a terminal attempt")

    finished = finish_task(
        profile,
        task_id=task_id,
        attempt_id=attempt_id,
        actor=resolved_actor,
        status=ATTEMPT_STATUS_SUCCESS,
        summary=resolved_summary,
        changed_paths=changed_paths,
        validations=validations,
        residuals=residuals,
        followup_candidates=followup_candidates,
        commit=resolved_source_commit,
        landed_commit=resolved_landed_commit,
        note=note,
        finalization_id=finalization_id,
        runtime_store=runtime_store,
    )
    event_payload = {
        "schema_version": TASK_STORE_SCHEMA_VERSION,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "actor": resolved_actor,
        "landing_transaction_id": resolved_transaction_id,
        "landing_transaction_phases": list(resolved_phases),
        "source_commit": resolved_source_commit,
        "target_commit": resolved_target_commit,
        "target_branch": resolved_target_branch,
        "landed_commit": resolved_landed_commit,
        "changed_paths": list(changed_paths),
        "current_format": current_format,
        "reason": resolved_reason,
    }
    event_id = _hash_parts(
        "blackdog.task.landing-reconciled/v4",
        task_id,
        attempt_id,
        resolved_transaction_id,
        resolved_landed_commit,
    )
    event_appended = append_event_once(
        profile.paths.events_file,
        event_id=event_id,
        event_type="task.landing.reconciled",
        actor=resolved_actor,
        payload=event_payload,
    )
    return {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "landed_commit": resolved_landed_commit,
        "source_commit": resolved_source_commit,
        "landing_transaction_id": resolved_transaction_id,
        "attempt_status": finished.status,
        "runtime_changed": runtime_changed,
        "event_appended": event_appended,
        "event_id": event_id,
    }


__all__ = [
    "TASK_STORE_SCHEMA_VERSION",
    "TaskError",
    "TaskFinalizationError",
    "TaskRuntimeTransitionError",
    "TaskFinalizationEvidence",
    "TaskRuntimeTransitionEvidence",
    "TaskRuntimeTransitionResult",
    "normalize_failure_class",
    "default_failure_class_for_status",
    "create_task",
    "start_task",
    "repair_task_start_events",
    "finish_task",
    "inspect_task_finalization",
    "inspect_task_runtime_transition",
    "set_task_runtime_status",
    "reconcile_landed_attempt",
    "task_start_event_id",
    "task_start_event_contracts",
    "task_resume_attempt_id",
]
