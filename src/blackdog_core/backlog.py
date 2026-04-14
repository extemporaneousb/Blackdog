"""Typed planning semantics over a machine-owned workset store."""

from __future__ import annotations

from dataclasses import dataclass
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
    EXECUTION_MODEL_WORKSET_MANAGER,
    TASK_STATUS_BLOCKED,
    TASK_STATUS_DONE,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_PLANNED,
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
    find_task_attempt,
    load_runtime_state,
    merge_workset_runtime,
    now_iso,
    parse_iso,
    save_runtime_state,
    task_claim_index,
    workset_claim,
)


PLANNING_SCHEMA_VERSION = 1
PLANNING_STORE_VERSION = "blackdog.planning/vnext1"
_UNSET = object()


class BacklogError(RuntimeError):
    pass


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
    runtime_state = load_runtime_state(profile.paths, runtime_store)
    incoming_task_states = None
    if "task_states" in payload:
        incoming_task_states = coerce_task_runtime_records(
            payload.get("task_states"),
            known_task_ids=task_ids,
            source_name=str(source),
        )
    runtime_state = merge_workset_runtime(
        runtime_state,
        workset_id=workset.workset_id,
        task_ids=task_ids,
        incoming_records=incoming_task_states,
    )
    save_runtime_state(profile.paths, runtime_state, runtime_store)
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


def claim_workset_manager(
    profile: RepoProfile,
    *,
    workset_id: str,
    actor: str,
    note: str | None = None,
    planning_store: PlanningStore | None = None,
    runtime_store: RuntimeStore | None = None,
) -> WorksetClaimRecord:
    planning_state = load_planning_state(profile.paths, planning_store)
    workset = find_workset(planning_state, workset_id)
    if workset is None:
        raise BacklogError(f"Unknown workset {workset_id!r}")
    runtime_state = load_runtime_state(profile.paths, runtime_store)
    current_workset_claim = workset_claim(runtime_state, workset_id)
    if current_workset_claim is not None:
        if current_workset_claim.execution_model != EXECUTION_MODEL_WORKSET_MANAGER:
            raise BacklogError(
                f"Workset {workset_id!r} is already claimed for execution_model "
                f"{current_workset_claim.execution_model!r}"
            )
        if current_workset_claim.actor != actor:
            raise BacklogError(f"Workset {workset_id!r} is already claimed by {current_workset_claim.actor}")
        return current_workset_claim

    claimed_at = now_iso()
    next_workset_claim = WorksetClaimRecord(
        actor=actor,
        execution_model=EXECUTION_MODEL_WORKSET_MANAGER,
        claimed_at=claimed_at,
        note=note,
    )
    next_runtime_state = merge_workset_runtime(
        runtime_state,
        workset_id=workset_id,
        task_ids={item.task_id for item in workset.tasks},
        incoming_records=None,
        incoming_workset_claim=next_workset_claim,
    )
    save_runtime_state(profile.paths, next_runtime_state, runtime_store)
    append_event(
        profile.paths.events_file,
        event_type="workset.claim",
        actor=actor,
        payload={
            "workset_id": workset_id,
            "execution_model": EXECUTION_MODEL_WORKSET_MANAGER,
            "claimed_at": claimed_at,
            "note": note,
        },
    )
    return next_workset_claim


def release_workset_manager(
    profile: RepoProfile,
    *,
    workset_id: str,
    actor: str,
    summary: str | None = None,
    note: str | None = None,
    planning_store: PlanningStore | None = None,
    runtime_store: RuntimeStore | None = None,
) -> WorksetClaimRecord:
    planning_state = load_planning_state(profile.paths, planning_store)
    workset = find_workset(planning_state, workset_id)
    if workset is None:
        raise BacklogError(f"Unknown workset {workset_id!r}")
    runtime_state = load_runtime_state(profile.paths, runtime_store)
    current_workset_claim = workset_claim(runtime_state, workset_id)
    if current_workset_claim is None:
        raise BacklogError(f"Workset {workset_id!r} is not currently claimed")
    if current_workset_claim.execution_model != EXECUTION_MODEL_WORKSET_MANAGER:
        raise BacklogError(
            f"Workset {workset_id!r} is claimed for execution_model "
            f"{current_workset_claim.execution_model!r}, not {EXECUTION_MODEL_WORKSET_MANAGER!r}"
        )
    if current_workset_claim.actor != actor:
        raise BacklogError(f"Workset {workset_id!r} is owned by {current_workset_claim.actor}, not {actor}")
    current_task_claims = task_claim_index(runtime_state, workset_id)
    if current_task_claims:
        active_task_list = ", ".join(sorted(current_task_claims))
        raise BacklogError(
            f"Workset {workset_id!r} still has active task claims: {active_task_list}"
        )
    next_runtime_state = merge_workset_runtime(
        runtime_state,
        workset_id=workset_id,
        task_ids={item.task_id for item in workset.tasks},
        incoming_records=None,
        incoming_workset_claim=None,
    )
    save_runtime_state(profile.paths, next_runtime_state, runtime_store)
    released_at = now_iso()
    append_event(
        profile.paths.events_file,
        event_type="workset.release",
        actor=actor,
        payload={
            "workset_id": workset_id,
            "released_at": released_at,
            "status": "released",
            "execution_model": EXECUTION_MODEL_WORKSET_MANAGER,
            "summary": summary,
            "note": note,
        },
    )
    return current_workset_claim


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
    prompt_receipt: PromptReceiptRecord | None = None,
    user_prompt_receipt: PromptReceiptRecord | None = None,
    note: str | None = None,
    planning_store: PlanningStore | None = None,
    runtime_store: RuntimeStore | None = None,
) -> TaskAttemptRecord:
    planning_state = load_planning_state(profile.paths, planning_store)
    workset, _ = _require_workset_and_task(planning_state, workset_id=workset_id, task_id=task_id)
    runtime_state = load_runtime_state(profile.paths, runtime_store)
    if execution_model not in EXECUTION_MODELS:
        raise BacklogError(f"execution_model must be one of {', '.join(sorted(EXECUTION_MODELS))}")
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
    current_task_claim = runtime_task_claims.get(task_id)
    if current_task_claim is not None:
        raise BacklogError(f"Task {task_id!r} is already claimed by {current_task_claim.actor}")
    current_workset_claim = workset_claim(runtime_state, workset_id)
    if current_workset_claim is not None:
        if current_workset_claim.execution_model == EXECUTION_MODEL_WORKSET_MANAGER:
            if execution_model != EXECUTION_MODEL_DIRECT_WTAM:
                raise BacklogError(
                    f"Workset {workset_id!r} is supervisor-claimed; child tasks must use "
                    f"{EXECUTION_MODEL_DIRECT_WTAM!r}"
                )
        else:
            if current_workset_claim.actor != actor:
                raise BacklogError(f"Workset {workset_id!r} is already claimed by {current_workset_claim.actor}")
            if current_workset_claim.execution_model != execution_model:
                raise BacklogError(
                    f"Workset {workset_id!r} is already claimed for execution_model "
                    f"{current_workset_claim.execution_model!r}"
                )
    dependencies_ready, blocked_by = task_dependencies_ready(workset, task_id=task_id, runtime_index=runtime_index)
    if not dependencies_ready:
        raise BacklogError(f"Task {task_id!r} is blocked by {', '.join(blocked_by)}")
    if prompt_receipt is None:
        raise BacklogError("task start requires a prompt receipt")
    resolved_user_prompt_receipt = user_prompt_receipt or prompt_receipt
    if branch is _UNSET:
        resolved_branch = str(
            workset.branch_intent.get("integration_branch") or workset.branch_intent.get("target_branch") or ""
        ).strip() or None
    else:
        resolved_branch = _optional_text(branch)

    started_at = now_iso()
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
        prompt_receipt=prompt_receipt,
        user_prompt_receipt=resolved_user_prompt_receipt,
        note=note,
    )
    next_workset_claim = current_workset_claim or WorksetClaimRecord(
        actor=actor,
        execution_model=execution_model,
        claimed_at=started_at,
        note=note,
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
    next_runtime_state = merge_workset_runtime(
        runtime_state,
        workset_id=workset_id,
        task_ids={item.task_id for item in workset.tasks},
        incoming_records=(task_runtime,),
        incoming_workset_claim=next_workset_claim,
        incoming_task_claims=(next_task_claim,),
        incoming_attempts=(attempt,),
    )
    save_runtime_state(profile.paths, next_runtime_state, runtime_store)
    if current_workset_claim is None:
        append_event(
            profile.paths.events_file,
            event_type="workset.claim",
            actor=actor,
            payload={
                "workset_id": workset_id,
                "execution_model": execution_model,
                "claimed_at": started_at,
                "note": note,
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
    runtime_state = load_runtime_state(profile.paths, runtime_store)
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
    )
    if status == ATTEMPT_STATUS_SUCCESS:
        task_runtime_status = TASK_STATUS_DONE
    elif status == ATTEMPT_STATUS_ABANDONED:
        task_runtime_status = TASK_STATUS_PLANNED
    else:
        task_runtime_status = TASK_STATUS_BLOCKED
    task_runtime = TaskRuntimeRecord(
        task_id=task_id,
        status=task_runtime_status,
        updated_at=ended_at,
        note=summary or note,
    )
    current_task_claims = task_claim_index(runtime_state, workset_id)
    remaining_task_claims = tuple(
        claim
        for claim_task_id, claim in current_task_claims.items()
        if claim_task_id != task_id
    )
    current_workset_claim = workset_claim(runtime_state, workset_id)
    release_workset_claim = (
        current_workset_claim is not None
        and current_workset_claim.execution_model != EXECUTION_MODEL_WORKSET_MANAGER
        and not remaining_task_claims
    )
    next_runtime_state = merge_workset_runtime(
        runtime_state,
        workset_id=workset_id,
        task_ids={item.task_id for item in workset.tasks},
        incoming_records=(task_runtime,),
        incoming_workset_claim=None if release_workset_claim else current_workset_claim,
        released_task_claim_ids=(task_id,),
        incoming_attempts=(finished_attempt,),
    )
    save_runtime_state(profile.paths, next_runtime_state, runtime_store)
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
        },
    )
    return finished_attempt


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
            if current_status in {"done", "in_progress", "blocked"}:
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
    "claim_workset_manager",
    "default_planning_state",
    "find_workset",
    "finish_task",
    "load_planning_state",
    "next_ready_tasks",
    "planning_state_to_payload",
    "release_workset_manager",
    "save_planning_state",
    "start_task",
    "task_dependencies_ready",
    "upsert_workset",
    "workset_to_payload",
]
