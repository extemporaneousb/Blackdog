"""Typed planning semantics over a machine-owned workset store."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
from typing import Any, Mapping, Protocol
import json
import uuid

from .profile import RepoProfile, BlackdogPaths, slugify
from .state import (
    ATTEMPT_STATUS_ABANDONED,
    ATTEMPT_STATUS_BLOCKED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_IN_PROGRESS,
    ATTEMPT_STATUS_SUCCESS,
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
    RuntimeStore,
    StoreError,
    TaskClaimRecord,
    TaskAttemptRecord,
    TaskRuntimeRecord,
    ValidationRecord,
    WorksetClaimRecord,
    append_event,
    atomic_write_text,
    coerce_task_runtime_records,
    default_runtime_state,
    exclusive_file_lock,
    find_task_attempt,
    is_legacy_managed_execution_model,
    latest_task_attempt,
    load_events,
    merge_workset_runtime,
    mutate_runtime_state,
    now_iso,
    parse_iso,
    task_claim_index,
    task_state_index,
    workset_claim,
)


PLANNING_SCHEMA_VERSION = 1
PLANNING_STORE_VERSION = "blackdog.planning/vnext1"
_UNSET = object()


class BacklogError(RuntimeError):
    pass


def normalize_failure_class(value: str | None) -> str | None:
    failure_class = str(value or "").strip() or None
    if failure_class is None:
        return None
    if failure_class not in FAILURE_CLASSES:
        raise BacklogError(f"failure_class must be one of {', '.join(sorted(FAILURE_CLASSES))}")
    return failure_class


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


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    title: str
    intent: str
    description: str | None
    depends_on: tuple[str, ...]
    paths: tuple[str, ...]
    docs: tuple[str, ...]
    checks: tuple[str, ...]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Workset:
    workset_id: str
    title: str
    scope: dict[str, Any]
    visibility: dict[str, Any]
    policies: dict[str, Any]
    workspace: dict[str, Any]
    branch_intent: dict[str, Any]
    tasks: tuple[TaskSpec, ...]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PlanningState:
    schema_version: int
    store_version: str
    worksets: tuple[Workset, ...]


class PlanningStore(Protocol):
    def load(self, path: Path) -> PlanningState:
        ...

    def save(self, path: Path, state: PlanningState) -> None:
        ...


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_list(value: Any, *, field: str, source: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise BacklogError(f"{field} must be a list in {source}")
    items: list[str] = []
    for item in value:
        text = _optional_text(item)
        if text:
            items.append(text)
    return tuple(items)


def _object(value: Any, *, field: str, source: Path) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise BacklogError(f"{field} must be an object in {source}")
    return dict(value)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise BacklogError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BacklogError(f"{path} must contain a JSON object")
    return payload


def _task_from_payload(payload: Mapping[str, Any], *, source: Path) -> TaskSpec:
    task_id = _optional_text(payload.get("id")) or _optional_text(payload.get("task_id"))
    if task_id is None:
        raise BacklogError(f"task.id is required in {source}")
    title = _optional_text(payload.get("title"))
    if title is None:
        raise BacklogError(f"task.title is required for {task_id} in {source}")
    intent = _optional_text(payload.get("intent")) or title
    return TaskSpec(
        task_id=task_id,
        title=title,
        intent=intent,
        description=_optional_text(payload.get("description")),
        depends_on=_string_list(payload.get("depends_on"), field=f"task[{task_id}].depends_on", source=source),
        paths=_string_list(payload.get("paths"), field=f"task[{task_id}].paths", source=source),
        docs=_string_list(payload.get("docs"), field=f"task[{task_id}].docs", source=source),
        checks=_string_list(payload.get("checks"), field=f"task[{task_id}].checks", source=source),
        metadata=_object(payload.get("metadata"), field=f"task[{task_id}].metadata", source=source),
    )


def _workset_from_payload(payload: Mapping[str, Any], *, source: Path) -> Workset:
    title = _optional_text(payload.get("title"))
    if title is None:
        raise BacklogError(f"workset.title is required in {source}")
    workset_id = _optional_text(payload.get("id")) or f"workset-{slugify(title)}"
    raw_tasks = payload.get("tasks")
    if raw_tasks is None:
        tasks: tuple[TaskSpec, ...] = ()
    else:
        if not isinstance(raw_tasks, list):
            raise BacklogError(f"workset.tasks must be a list in {source}")
        tasks = tuple(_task_from_payload(item, source=source) for item in raw_tasks if isinstance(item, Mapping))
        if len(tasks) != len(raw_tasks):
            raise BacklogError(f"workset.tasks must contain only objects in {source}")
    seen_task_ids: set[str] = set()
    for task in tasks:
        if task.task_id in seen_task_ids:
            raise BacklogError(f"duplicate task id {task.task_id!r} in {source}")
        seen_task_ids.add(task.task_id)
    for task in tasks:
        missing = [dependency for dependency in task.depends_on if dependency not in seen_task_ids]
        if missing:
            raise BacklogError(f"task {task.task_id} references unknown dependencies {missing} in {source}")
    return Workset(
        workset_id=workset_id,
        title=title,
        scope=_object(payload.get("scope"), field=f"workset[{workset_id}].scope", source=source),
        visibility=_object(payload.get("visibility"), field=f"workset[{workset_id}].visibility", source=source),
        policies=_object(payload.get("policies"), field=f"workset[{workset_id}].policies", source=source),
        workspace=_object(payload.get("workspace"), field=f"workset[{workset_id}].workspace", source=source),
        branch_intent=_object(payload.get("branch_intent"), field=f"workset[{workset_id}].branch_intent", source=source),
        tasks=tasks,
        metadata=_object(payload.get("metadata"), field=f"workset[{workset_id}].metadata", source=source),
    )


def default_planning_state() -> PlanningState:
    return PlanningState(
        schema_version=PLANNING_SCHEMA_VERSION,
        store_version=PLANNING_STORE_VERSION,
        worksets=(),
    )


def workset_to_payload(workset: Workset) -> dict[str, Any]:
    return {
        "id": workset.workset_id,
        "title": workset.title,
        "scope": dict(workset.scope),
        "visibility": dict(workset.visibility),
        "policies": dict(workset.policies),
        "workspace": dict(workset.workspace),
        "branch_intent": dict(workset.branch_intent),
        "tasks": [
            {
                "id": task.task_id,
                "title": task.title,
                "intent": task.intent,
                "description": task.description,
                "depends_on": list(task.depends_on),
                "paths": list(task.paths),
                "docs": list(task.docs),
                "checks": list(task.checks),
                "metadata": dict(task.metadata),
            }
            for task in workset.tasks
        ],
        "metadata": dict(workset.metadata),
    }


def planning_state_to_payload(state: PlanningState) -> dict[str, Any]:
    return {
        "schema_version": state.schema_version,
        "store_version": state.store_version,
        "worksets": [workset_to_payload(workset) for workset in state.worksets],
    }


class JsonPlanningStore:
    def load(self, path: Path) -> PlanningState:
        try:
            payload = _read_json_file(path)
        except FileNotFoundError:
            return default_planning_state()
        schema_version = int(payload.get("schema_version") or PLANNING_SCHEMA_VERSION)
        store_version = _optional_text(payload.get("store_version")) or PLANNING_STORE_VERSION
        if schema_version != PLANNING_SCHEMA_VERSION:
            raise BacklogError(f"Unsupported planning schema_version {schema_version} in {path}")
        if store_version != PLANNING_STORE_VERSION:
            raise BacklogError(f"Unsupported planning store_version {store_version!r} in {path}")
        raw_worksets = payload.get("worksets") or []
        if not isinstance(raw_worksets, list):
            raise BacklogError(f"worksets must be a list in {path}")
        worksets = tuple(_workset_from_payload(item, source=path) for item in raw_worksets if isinstance(item, Mapping))
        if len(worksets) != len(raw_worksets):
            raise BacklogError(f"worksets must contain only objects in {path}")
        seen_workset_ids: set[str] = set()
        for workset in worksets:
            if workset.workset_id in seen_workset_ids:
                raise BacklogError(f"duplicate workset id {workset.workset_id!r} in {path}")
            seen_workset_ids.add(workset.workset_id)
        return PlanningState(
            schema_version=schema_version,
            store_version=store_version,
            worksets=worksets,
        )

    def save(self, path: Path, state: PlanningState) -> None:
        atomic_write_text(path, json.dumps(planning_state_to_payload(state), indent=2, sort_keys=True) + "\n")


def load_planning_state(paths: BlackdogPaths, store: PlanningStore | None = None) -> PlanningState:
    return (store or JsonPlanningStore()).load(paths.planning_file)


def save_planning_state(paths: BlackdogPaths, state: PlanningState, store: PlanningStore | None = None) -> None:
    (store or JsonPlanningStore()).save(paths.planning_file, state)


def find_workset(state: PlanningState, workset_id: str) -> Workset | None:
    for workset in state.worksets:
        if workset.workset_id == workset_id:
            return workset
    return None


def upsert_workset(
    profile: RepoProfile,
    payload: Mapping[str, Any],
    *,
    planning_store: PlanningStore | None = None,
    runtime_store: RuntimeStore | None = None,
) -> Workset:
    source = profile.paths.planning_file
    workset = _workset_from_payload(payload, source=source)
    current = load_planning_state(profile.paths, planning_store)
    remaining = [item for item in current.worksets if item.workset_id != workset.workset_id]
    next_state = PlanningState(
        schema_version=current.schema_version,
        store_version=current.store_version,
        worksets=tuple([*remaining, workset]),
    )
    save_planning_state(profile.paths, next_state, planning_store)

    task_ids = {task.task_id for task in workset.tasks}
    incoming_task_states = None
    if "task_states" in payload:
        incoming_task_states = coerce_task_runtime_records(
            payload.get("task_states"),
            known_task_ids=task_ids,
            source_name=str(source),
        )

    mutate_runtime_state(
        profile.paths,
        lambda runtime_state: merge_workset_runtime(
            runtime_state,
            workset_id=workset.workset_id,
            task_ids=task_ids,
            incoming_records=incoming_task_states,
        ),
        store=runtime_store,
    )
    append_event(
        profile.paths.events_file,
        event_type="workset.put",
        payload={
            "workset_id": workset.workset_id,
            "task_count": len(workset.tasks),
            "has_runtime_patch": incoming_task_states is not None,
        },
    )
    return workset


def task_dependencies_ready(
    workset: Workset,
    *,
    task_id: str,
    runtime_index: Mapping[str, TaskRuntimeRecord],
) -> tuple[bool, tuple[str, ...]]:
    task_map = {task.task_id: task for task in workset.tasks}
    task = task_map[task_id]
    blocked_by = tuple(
        dependency
        for dependency in task.depends_on
        if runtime_index.get(dependency, TaskRuntimeRecord(task_id=dependency, status="planned")).status != "done"
    )
    return (not blocked_by, blocked_by)


def _require_workset_and_task(
    planning_state: PlanningState,
    *,
    workset_id: str,
    task_id: str,
) -> tuple[Workset, TaskSpec]:
    workset = find_workset(planning_state, workset_id)
    if workset is None:
        raise BacklogError(f"Unknown workset {workset_id!r}")
    for task in workset.tasks:
        if task.task_id == task_id:
            return workset, task
    raise BacklogError(f"Unknown task {task_id!r} in workset {workset_id!r}")


def start_task(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    execution_model: str = EXECUTION_MODEL_DIRECT_WTAM,
    workspace_identity: str | None = None,
    workspace_mode: str | None = None,
    worktree_role: str | None = None,
    worktree_path: str | None = None,
    branch: str | None | object = _UNSET,
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
    planning_store: PlanningStore | None = None,
    runtime_store: RuntimeStore | None = None,
) -> TaskAttemptRecord:
    planning_state = load_planning_state(profile.paths, planning_store)
    workset, _ = _require_workset_and_task(planning_state, workset_id=workset_id, task_id=task_id)
    if execution_model not in EXECUTION_MODELS:
        raise BacklogError(f"execution_model must be one of {', '.join(sorted(EXECUTION_MODELS))}")
    if prompt_receipt is None:
        raise BacklogError("task start requires a prompt receipt")
    resolved_user_prompt_receipt = user_prompt_receipt or prompt_receipt
    if branch is _UNSET:
        resolved_branch = str(
            workset.branch_intent.get("integration_branch") or workset.branch_intent.get("target_branch") or ""
        ).strip() or None
    else:
        resolved_branch = _optional_text(branch)

    attempt: TaskAttemptRecord | None = None
    next_workset_claim: WorksetClaimRecord | None = None
    started_at: str | None = None
    emit_workset_claim = False

    def mutate(runtime_state):
        nonlocal attempt, next_workset_claim, started_at, emit_workset_claim
        runtime_index = {
            task_state.task_id: task_state
            for runtime_workset in runtime_state.worksets
            if runtime_workset.workset_id == workset_id
            for task_state in runtime_workset.task_states
        }
        runtime_task_claims = task_claim_index(runtime_state, workset_id)
        current = runtime_index.get(task_id, TaskRuntimeRecord(task_id=task_id, status=TASK_STATUS_PLANNED))
        if current.status == TASK_STATUS_DONE:
            raise BacklogError(f"Task {task_id!r} is already done")
        if current.status == TASK_STATUS_IN_PROGRESS:
            raise BacklogError(f"Task {task_id!r} is already in progress")
        if current.status == TASK_STATUS_CANCELED:
            raise BacklogError(f"Task {task_id!r} is canceled; reopen it before starting")
        current_task_claim = runtime_task_claims.get(task_id)
        if current_task_claim is not None:
            raise BacklogError(f"Task {task_id!r} is already claimed by {current_task_claim.actor}")
        current_workset_claim = workset_claim(runtime_state, workset_id)
        migrate_legacy_workset_claim = (
            current_workset_claim is not None
            and is_legacy_managed_execution_model(current_workset_claim.execution_model)
        )
        reusable_workset_claim = None if migrate_legacy_workset_claim else current_workset_claim
        if reusable_workset_claim is not None:
            if reusable_workset_claim.actor != actor:
                raise BacklogError(f"Workset {workset_id!r} is already claimed by {reusable_workset_claim.actor}")
            if reusable_workset_claim.execution_model != execution_model:
                raise BacklogError(
                    f"Workset {workset_id!r} is already claimed for execution_model "
                    f"{reusable_workset_claim.execution_model!r}"
                )
        dependencies_ready, blocked_by = task_dependencies_ready(workset, task_id=task_id, runtime_index=runtime_index)
        if not dependencies_ready:
            raise BacklogError(f"Task {task_id!r} is blocked by {', '.join(blocked_by)}")

        started_at = now_iso()
        next_workset_claim_note = note
        if next_workset_claim_note is None and current_workset_claim is not None:
            next_workset_claim_note = current_workset_claim.note
        attempt = TaskAttemptRecord(
            attempt_id=f"{task_id}-{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            status=ATTEMPT_STATUS_IN_PROGRESS,
            actor=actor,
            started_at=started_at,
            workspace_identity=workspace_identity or str(workset.workspace.get("identity") or "").strip() or None,
            workspace_mode=workspace_mode,
            worktree_role=worktree_role,
            worktree_path=worktree_path,
            branch=resolved_branch,
            target_branch=target_branch or str(workset.branch_intent.get("target_branch") or "").strip() or None,
            integration_branch=integration_branch or str(workset.branch_intent.get("integration_branch") or "").strip() or None,
            start_commit=start_commit,
            execution_model=execution_model,
            model=model,
            reasoning_effort=reasoning_effort,
            codex_session=codex_session,
            prompt_receipt=prompt_receipt,
            user_prompt_receipt=resolved_user_prompt_receipt,
            note=note,
            setup_receipt=dict(setup_receipt) if setup_receipt is not None else None,
        )
        next_workset_claim = reusable_workset_claim or WorksetClaimRecord(
            actor=actor,
            execution_model=execution_model,
            claimed_at=started_at,
            note=next_workset_claim_note,
        )
        next_task_claim = TaskClaimRecord(
            task_id=task_id,
            actor=actor,
            execution_model=execution_model,
            claimed_at=started_at,
            attempt_id=attempt.attempt_id,
            note=note,
        )
        task_runtime = TaskRuntimeRecord(
            task_id=task_id,
            status=TASK_STATUS_IN_PROGRESS,
            updated_at=started_at,
            note=note,
        )
        emit_workset_claim = reusable_workset_claim is None
        return merge_workset_runtime(
            runtime_state,
            workset_id=workset_id,
            task_ids={item.task_id for item in workset.tasks},
            incoming_records=(task_runtime,),
            incoming_workset_claim=next_workset_claim,
            incoming_task_claims=(next_task_claim,),
            incoming_attempts=(attempt,),
        )

    mutate_runtime_state(profile.paths, mutate, store=runtime_store)
    if attempt is None or next_workset_claim is None or started_at is None:
        raise BacklogError("task start did not create an attempt")
    if emit_workset_claim:
        append_event(
            profile.paths.events_file,
            event_type="workset.claim",
            actor=actor,
            payload={
                "workset_id": workset_id,
                "execution_model": execution_model,
                "claimed_at": started_at,
                "note": next_workset_claim.note,
            },
        )
    append_event(
        profile.paths.events_file,
        event_type="task.claim",
        actor=actor,
        payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt.attempt_id,
            "execution_model": execution_model,
            "claimed_at": started_at,
            "note": note,
        },
    )
    append_event(
        profile.paths.events_file,
        event_type="task.start",
        actor=actor,
        payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt.attempt_id,
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
            "codex_thread_id": attempt.codex_session.thread_id if attempt.codex_session is not None else None,
            "codex_session_path": attempt.codex_session.session_path if attempt.codex_session is not None else None,
            "codex_turn_id": attempt.codex_session.turn_id if attempt.codex_session is not None else None,
            "prompt_hash": attempt.prompt_receipt.prompt_hash if attempt.prompt_receipt is not None else None,
            "prompt_source": attempt.prompt_receipt.source if attempt.prompt_receipt is not None else None,
            "prompt_mode": attempt.prompt_receipt.mode if attempt.prompt_receipt is not None else None,
            "user_prompt_hash": (
                attempt.user_prompt_receipt.prompt_hash if attempt.user_prompt_receipt is not None else None
            ),
            "user_prompt_source": (
                attempt.user_prompt_receipt.source if attempt.user_prompt_receipt is not None else None
            ),
            "user_prompt_mode": (
                attempt.user_prompt_receipt.mode if attempt.user_prompt_receipt is not None else None
            ),
            "setup_receipt": attempt.setup_receipt,
        },
    )
    return attempt


def finish_task(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    actor: str,
    status: str,
    summary: str | None = None,
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
    planning_store: PlanningStore | None = None,
    runtime_store: RuntimeStore | None = None,
) -> TaskAttemptRecord:
    if status not in {
        ATTEMPT_STATUS_SUCCESS,
        ATTEMPT_STATUS_BLOCKED,
        ATTEMPT_STATUS_FAILED,
        ATTEMPT_STATUS_ABANDONED,
    }:
        raise BacklogError(f"task finish status must be one of success, blocked, failed, abandoned; got {status!r}")
    planning_state = load_planning_state(profile.paths, planning_store)
    workset, _ = _require_workset_and_task(planning_state, workset_id=workset_id, task_id=task_id)

    finished_attempt: TaskAttemptRecord | None = None
    ended_at: str | None = None
    derived_elapsed_seconds: int | None = None
    resolved_failure_class: str | None = None
    resolved_recovery_action: str | None = None
    resolved_prompt_issue = False
    resolved_operator_issue = False
    release_workset_claim = False

    def mutate(runtime_state):
        nonlocal finished_attempt, ended_at, derived_elapsed_seconds, resolved_failure_class
        nonlocal resolved_recovery_action, resolved_prompt_issue, resolved_operator_issue, release_workset_claim

        existing_attempt = find_task_attempt(runtime_state, workset_id, attempt_id)
        if existing_attempt is None:
            raise BacklogError(f"Unknown attempt {attempt_id!r} in workset {workset_id!r}")
        if existing_attempt.task_id != task_id:
            raise BacklogError(f"Attempt {attempt_id!r} does not belong to task {task_id!r}")
        if existing_attempt.actor != actor:
            raise BacklogError(f"Attempt {attempt_id!r} is owned by {existing_attempt.actor}, not {actor}")
        if existing_attempt.status != ATTEMPT_STATUS_IN_PROGRESS or existing_attempt.ended_at is not None:
            raise BacklogError(f"Attempt {attempt_id!r} is not active")

        ended_at = now_iso()
        resolved_failure_class = default_failure_class_for_status(status, failure_class)
        resolved_prompt_issue = bool(prompt_issue)
        resolved_operator_issue = bool(operator_issue or status == ATTEMPT_STATUS_ABANDONED)
        resolved_recovery_action = str(recovery_action or "").strip() or None
        derived_elapsed_seconds = elapsed_seconds
        if derived_elapsed_seconds is None:
            started_at = parse_iso(existing_attempt.started_at)
            ended_at_value = parse_iso(ended_at)
            if started_at is not None and ended_at_value is not None:
                derived_elapsed_seconds = max(0, int((ended_at_value - started_at).total_seconds()))
        finished_attempt = TaskAttemptRecord(
            attempt_id=existing_attempt.attempt_id,
            task_id=existing_attempt.task_id,
            status=status,
            actor=existing_attempt.actor,
            started_at=existing_attempt.started_at,
            ended_at=ended_at,
            summary=summary,
            workspace_identity=existing_attempt.workspace_identity,
            workspace_mode=existing_attempt.workspace_mode,
            worktree_role=existing_attempt.worktree_role,
            worktree_path=existing_attempt.worktree_path,
            branch=existing_attempt.branch,
            target_branch=existing_attempt.target_branch,
            integration_branch=existing_attempt.integration_branch,
            start_commit=existing_attempt.start_commit,
            execution_model=existing_attempt.execution_model,
            model=existing_attempt.model,
            reasoning_effort=existing_attempt.reasoning_effort,
            codex_session=existing_attempt.codex_session,
            prompt_receipt=existing_attempt.prompt_receipt,
            user_prompt_receipt=existing_attempt.user_prompt_receipt,
            changed_paths=tuple(changed_paths),
            validations=tuple(validations),
            residuals=tuple(residuals),
            followup_candidates=tuple(followup_candidates),
            note=note or existing_attempt.note,
            commit=commit,
            landed_commit=landed_commit,
            elapsed_seconds=derived_elapsed_seconds,
            failure_class=resolved_failure_class,
            recovery_action=resolved_recovery_action,
            prompt_issue=resolved_prompt_issue,
            operator_issue=resolved_operator_issue,
            setup_receipt=existing_attempt.setup_receipt,
        )
        if status == ATTEMPT_STATUS_SUCCESS:
            task_runtime_status = TASK_STATUS_DONE
        elif status == ATTEMPT_STATUS_ABANDONED:
            task_runtime_status = TASK_STATUS_CANCELED
        else:
            task_runtime_status = TASK_STATUS_BLOCKED
        task_runtime = TaskRuntimeRecord(
            task_id=task_id,
            status=task_runtime_status,
            updated_at=ended_at,
            note=summary or note,
            failure_class=resolved_failure_class,
            recovery_action=resolved_recovery_action,
            prompt_issue=resolved_prompt_issue,
            operator_issue=resolved_operator_issue,
        )
        current_task_claims = task_claim_index(runtime_state, workset_id)
        remaining_task_claims = tuple(
            claim
            for claim_task_id, claim in current_task_claims.items()
            if claim_task_id != task_id
        )
        current_workset_claim = workset_claim(runtime_state, workset_id)
        release_workset_claim = current_workset_claim is not None and not remaining_task_claims
        return merge_workset_runtime(
            runtime_state,
            workset_id=workset_id,
            task_ids={item.task_id for item in workset.tasks},
            incoming_records=(task_runtime,),
            incoming_workset_claim=None if release_workset_claim else current_workset_claim,
            released_task_claim_ids=(task_id,),
            incoming_attempts=(finished_attempt,),
        )

    mutate_runtime_state(profile.paths, mutate, store=runtime_store)
    if finished_attempt is None or ended_at is None:
        raise BacklogError("task finish did not update an attempt")
    append_event(
        profile.paths.events_file,
        event_type="task.release",
        actor=actor,
        payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "released_at": ended_at,
            "status": status,
        },
    )
    if release_workset_claim:
        append_event(
            profile.paths.events_file,
            event_type="workset.release",
            actor=actor,
            payload={
                "workset_id": workset_id,
                "released_at": ended_at,
                "status": status,
            },
        )
    append_event(
        profile.paths.events_file,
        event_type="task.finish",
        actor=actor,
        payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "status": status,
            "summary": summary,
            "worktree_role": finished_attempt.worktree_role,
            "worktree_path": finished_attempt.worktree_path,
            "branch": finished_attempt.branch,
            "start_commit": finished_attempt.start_commit,
            "execution_model": finished_attempt.execution_model,
            "model": finished_attempt.model,
            "reasoning_effort": finished_attempt.reasoning_effort,
            "codex_thread_id": (
                finished_attempt.codex_session.thread_id if finished_attempt.codex_session is not None else None
            ),
            "codex_session_path": (
                finished_attempt.codex_session.session_path if finished_attempt.codex_session is not None else None
            ),
            "codex_turn_id": (
                finished_attempt.codex_session.turn_id if finished_attempt.codex_session is not None else None
            ),
            "prompt_hash": (
                finished_attempt.prompt_receipt.prompt_hash
                if finished_attempt.prompt_receipt is not None
                else None
            ),
            "prompt_source": (
                finished_attempt.prompt_receipt.source
                if finished_attempt.prompt_receipt is not None
                else None
            ),
            "prompt_mode": (
                finished_attempt.prompt_receipt.mode
                if finished_attempt.prompt_receipt is not None
                else None
            ),
            "user_prompt_hash": (
                finished_attempt.user_prompt_receipt.prompt_hash
                if finished_attempt.user_prompt_receipt is not None
                else None
            ),
            "user_prompt_source": (
                finished_attempt.user_prompt_receipt.source
                if finished_attempt.user_prompt_receipt is not None
                else None
            ),
            "user_prompt_mode": (
                finished_attempt.user_prompt_receipt.mode
                if finished_attempt.user_prompt_receipt is not None
                else None
            ),
            "changed_paths": list(changed_paths),
            "validations": [{"name": item.name, "status": item.status} for item in validations],
            "residuals": list(residuals),
            "followup_candidates": list(followup_candidates),
            "commit": commit,
            "landed_commit": landed_commit,
            "elapsed_seconds": derived_elapsed_seconds,
            "failure_class": resolved_failure_class,
            "recovery_action": resolved_recovery_action,
            "prompt_issue": resolved_prompt_issue,
            "operator_issue": resolved_operator_issue,
        },
    )
    return finished_attempt


def landing_reconciliation_id(
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    landed_commit: str,
) -> str:
    """Return the stable identity for one landing correction.

    The identity deliberately excludes operator-supplied prose and timestamps so a
    retry after the runtime write but before the event append targets the same
    correction.
    """
    parts = (
        "blackdog.task.landing.reconciliation/v1",
        str(workset_id).strip(),
        str(task_id).strip(),
        str(attempt_id).strip(),
        str(landed_commit).strip().lower(),
    )
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def reconcile_landed_attempt(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    landed_commit: str,
    actor: str,
    changed_paths: tuple[str, ...],
    reason: str | None = None,
    proof: Mapping[str, Any] | None = None,
    planning_store: PlanningStore | None = None,
    runtime_store: RuntimeStore | None = None,
) -> dict[str, Any]:
    """Correct a terminal attempt after product-layer Git landing proof.

    Runtime is the source of truth and is written before the append-only event.
    The deterministic reconciliation id makes retries repair a missing event
    without changing the corrected runtime a second time.
    """
    normalized_landed_commit = str(landed_commit).strip().lower()
    if not normalized_landed_commit:
        raise BacklogError("landed_commit is required for landing reconciliation")
    planning_state = load_planning_state(profile.paths, planning_store)
    workset, _ = _require_workset_and_task(planning_state, workset_id=workset_id, task_id=task_id)
    reconciliation_id = landing_reconciliation_id(
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
        landed_commit=normalized_landed_commit,
    )
    with exclusive_file_lock(profile.paths.events_file):
        events_before = load_events(profile.paths.events_file)
    matching_events = [
        event
        for event in events_before
        if event.get("type") == "task.landing.reconciled"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("reconciliation_id") == reconciliation_id
    ]
    historical_terminal_status = next(
        (
            str(event["payload"].get("status"))
            for event in reversed(events_before)
            if event.get("type") in {"task.finish", "worktree.close"}
            and isinstance(event.get("payload"), Mapping)
            and event["payload"].get("workset_id") == workset_id
            and event["payload"].get("task_id") == task_id
            and event["payload"].get("attempt_id") == attempt_id
            and event["payload"].get("status") in {ATTEMPT_STATUS_BLOCKED, ATTEMPT_STATUS_FAILED}
        ),
        None,
    )
    reconciled_at = now_iso()
    previous_status: str | None = None
    corrected_attempt: TaskAttemptRecord | None = None
    runtime_changed = False

    def mutate(runtime_state):
        nonlocal previous_status, corrected_attempt, runtime_changed
        existing_attempt = find_task_attempt(runtime_state, workset_id, attempt_id)
        if existing_attempt is None:
            raise BacklogError(f"Unknown attempt {attempt_id!r} in workset {workset_id!r}")
        if existing_attempt.task_id != task_id:
            raise BacklogError(f"Attempt {attempt_id!r} does not belong to task {task_id!r}")
        latest_attempt = latest_task_attempt(runtime_state, workset_id, task_id)
        if latest_attempt is None or latest_attempt.attempt_id != attempt_id:
            later_id = latest_attempt.attempt_id if latest_attempt is not None else "unknown"
            raise BacklogError(
                f"Attempt {attempt_id!r} is not the latest attempt for task {task_id!r}; latest is {later_id!r}"
            )
        if task_id in task_claim_index(runtime_state, workset_id):
            raise BacklogError(f"Task {task_id!r} has an active claim and cannot be reconciled")
        if existing_attempt.ended_at is None:
            raise BacklogError(f"Attempt {attempt_id!r} is not terminal")

        if existing_attempt.status == ATTEMPT_STATUS_SUCCESS:
            if str(existing_attempt.landed_commit or "").strip().lower() != normalized_landed_commit:
                raise BacklogError(
                    f"Attempt {attempt_id!r} is already successful with a different landed commit"
                )
            if tuple(existing_attempt.changed_paths) != tuple(changed_paths):
                raise BacklogError(
                    f"Attempt {attempt_id!r} is already successful with different changed paths"
                )
            if not matching_events and historical_terminal_status is None:
                raise BacklogError(
                    f"Attempt {attempt_id!r} is already successful and has no landing-reconciliation retry evidence"
                )
            previous_status = (
                str(matching_events[0]["payload"].get("previous_status"))
                if matching_events
                else historical_terminal_status
            )
            corrected_attempt = existing_attempt
            return runtime_state
        if existing_attempt.status not in {ATTEMPT_STATUS_BLOCKED, ATTEMPT_STATUS_FAILED}:
            raise BacklogError(
                f"Attempt {attempt_id!r} status {existing_attempt.status!r} is not failed or blocked"
            )
        previous_status = existing_attempt.status
        if existing_attempt.landed_commit:
            raise BacklogError(
                f"Attempt {attempt_id!r} already records landed commit {existing_attempt.landed_commit!r}"
            )

        corrected_attempt = replace(
            existing_attempt,
            status=ATTEMPT_STATUS_SUCCESS,
            landed_commit=normalized_landed_commit,
            changed_paths=tuple(changed_paths),
            failure_class=None,
            recovery_action=None,
            prompt_issue=False,
            operator_issue=False,
        )
        current_task_state = task_state_index(runtime_state, workset_id).get(task_id)
        corrected_task_state = TaskRuntimeRecord(
            task_id=task_id,
            status=TASK_STATUS_DONE,
            updated_at=reconciled_at,
            note=current_task_state.note if current_task_state is not None else existing_attempt.summary,
            failure_class=None,
            recovery_action=None,
            prompt_issue=False,
            operator_issue=False,
        )
        runtime_changed = True
        return merge_workset_runtime(
            runtime_state,
            workset_id=workset_id,
            task_ids={item.task_id for item in workset.tasks},
            incoming_records=(corrected_task_state,),
            incoming_attempts=(corrected_attempt,),
        )

    mutate_runtime_state(profile.paths, mutate, store=runtime_store)
    if corrected_attempt is None or previous_status is None:
        raise BacklogError("landing reconciliation did not resolve the attempt")

    event_appended = False
    with exclusive_file_lock(profile.paths.events_file):
        matching_events = [
            event
            for event in load_events(profile.paths.events_file)
            if event.get("type") == "task.landing.reconciled"
            and isinstance(event.get("payload"), Mapping)
            and event["payload"].get("reconciliation_id") == reconciliation_id
        ]
        if not matching_events:
            append_event(
                profile.paths.events_file,
                event_type="task.landing.reconciled",
                actor=actor,
                payload={
                    "reconciliation_id": reconciliation_id,
                    "workset_id": workset_id,
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "attempt_actor": corrected_attempt.actor,
                    "previous_status": previous_status,
                    "status": ATTEMPT_STATUS_SUCCESS,
                    "landed_commit": normalized_landed_commit,
                    "reconciled_at": reconciled_at,
                    "reason": str(reason or "").strip() or None,
                    "proof": dict(proof or {}),
                },
            )
            event_appended = True

    return {
        "reconciliation_id": reconciliation_id,
        "workset_id": workset_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "attempt_actor": corrected_attempt.actor,
        "previous_status": previous_status,
        "status": corrected_attempt.status,
        "landed_commit": corrected_attempt.landed_commit,
        "runtime_changed": runtime_changed,
        "event_appended": event_appended,
        "event_repaired": event_appended and not runtime_changed,
    }


def set_task_runtime_status(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    status: str,
    actor: str,
    summary: str | None = None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
    planning_store: PlanningStore | None = None,
    runtime_store: RuntimeStore | None = None,
) -> TaskRuntimeRecord:
    if status not in {TASK_STATUS_CANCELED, TASK_STATUS_PLANNED}:
        raise BacklogError("set_task_runtime_status only supports canceled or planned")
    planning_state = load_planning_state(profile.paths, planning_store)
    workset, _ = _require_workset_and_task(planning_state, workset_id=workset_id, task_id=task_id)
    current_status = TASK_STATUS_PLANNED
    record: TaskRuntimeRecord | None = None
    updated_at: str | None = None

    def mutate(runtime_state):
        nonlocal current_status, record, updated_at
        runtime_task_claims = task_claim_index(runtime_state, workset_id)
        if task_id in runtime_task_claims:
            raise BacklogError(f"Task {task_id!r} is claimed; close or recover it before changing state")
        current_status = {
            task_state.task_id: task_state.status
            for runtime_workset in runtime_state.worksets
            if runtime_workset.workset_id == workset_id
            for task_state in runtime_workset.task_states
        }.get(task_id, TASK_STATUS_PLANNED)
        if current_status == TASK_STATUS_IN_PROGRESS:
            raise BacklogError(f"Task {task_id!r} is in progress; close it before changing state")
        if current_status == TASK_STATUS_DONE and status == TASK_STATUS_CANCELED:
            raise BacklogError(f"Task {task_id!r} is done and cannot be canceled")
        if status == TASK_STATUS_PLANNED and current_status != TASK_STATUS_CANCELED:
            raise BacklogError(f"Task {task_id!r} is not canceled")
        updated_at = now_iso()
        resolved_failure_class = normalize_failure_class(failure_class)
        if status == TASK_STATUS_CANCELED and resolved_failure_class is None:
            resolved_failure_class = FAILURE_CLASS_UNKNOWN
        record = TaskRuntimeRecord(
            task_id=task_id,
            status=status,
            updated_at=updated_at,
            note=summary,
            failure_class=resolved_failure_class if status == TASK_STATUS_CANCELED else None,
            recovery_action=(str(recovery_action or "").strip() or None) if status == TASK_STATUS_CANCELED else None,
            prompt_issue=bool(prompt_issue) if status == TASK_STATUS_CANCELED else False,
            operator_issue=bool(operator_issue) if status == TASK_STATUS_CANCELED else False,
        )
        return merge_workset_runtime(
            runtime_state,
            workset_id=workset_id,
            task_ids={item.task_id for item in workset.tasks},
            incoming_records=(record,),
        )

    mutate_runtime_state(profile.paths, mutate, store=runtime_store)
    if record is None or updated_at is None:
        raise BacklogError("task state update did not write a runtime record")
    append_event(
        profile.paths.events_file,
        event_type="task.cancel" if status == TASK_STATUS_CANCELED else "task.reopen",
        actor=actor,
        payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "status": status,
            "updated_at": updated_at,
            "summary": summary,
            "previous_status": current_status,
            "failure_class": record.failure_class,
            "recovery_action": record.recovery_action,
            "prompt_issue": record.prompt_issue,
            "operator_issue": record.operator_issue,
        },
    )
    return record


def next_ready_tasks(
    planning_state: PlanningState,
    *,
    runtime_state=None,
    workset_id: str | None = None,
) -> list[tuple[Workset, TaskSpec]]:
    if runtime_state is None:
        runtime_state = default_runtime_state()
    ready: list[tuple[Workset, TaskSpec]] = []
    for workset in planning_state.worksets:
        if workset_id and workset.workset_id != workset_id:
            continue
        runtime_index = {
            task_state.task_id: task_state
            for runtime_workset in runtime_state.worksets
            if runtime_workset.workset_id == workset.workset_id
            for task_state in runtime_workset.task_states
        }
        for task in workset.tasks:
            current_status = runtime_index.get(task.task_id, TaskRuntimeRecord(task_id=task.task_id, status="planned")).status
            if current_status in {"done", "in_progress", "blocked", "canceled"}:
                continue
            dependencies_ready, _ = task_dependencies_ready(
                workset,
                task_id=task.task_id,
                runtime_index=runtime_index,
            )
            if dependencies_ready:
                ready.append((workset, task))
    return ready


__all__ = [
    "PLANNING_SCHEMA_VERSION",
    "PLANNING_STORE_VERSION",
    "BacklogError",
    "JsonPlanningStore",
    "PlanningState",
    "PlanningStore",
    "TaskSpec",
    "Workset",
    "default_failure_class_for_status",
    "default_planning_state",
    "find_workset",
    "finish_task",
    "load_planning_state",
    "landing_reconciliation_id",
    "next_ready_tasks",
    "normalize_failure_class",
    "planning_state_to_payload",
    "save_planning_state",
    "reconcile_landed_attempt",
    "set_task_runtime_status",
    "start_task",
    "task_dependencies_ready",
    "upsert_workset",
    "workset_to_payload",
]
