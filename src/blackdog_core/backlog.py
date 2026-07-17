"""Typed planning semantics over a machine-owned workset store."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
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
    RuntimeState,
    RuntimeStore,
    StoreError,
    TaskClaimRecord,
    TaskAttemptRecord,
    TaskRuntimeRecord,
    ValidationRecord,
    WorksetClaimRecord,
    active_task_attempt,
    append_event,
    append_event_once,
    atomic_write_text,
    coerce_task_runtime_records,
    default_runtime_state,
    exclusive_file_lock,
    find_task_attempt,
    is_legacy_managed_execution_model,
    latest_task_attempt,
    load_events,
    load_runtime_state,
    merge_workset_runtime,
    mutate_runtime_state,
    now_iso,
    parse_iso,
    runtime_state_to_payload,
    task_claim_index,
    task_state_index,
    workset_claim,
)


PLANNING_SCHEMA_VERSION = 1
PLANNING_STORE_VERSION = "blackdog.planning/vnext1"
_UNSET = object()
_ATOMIC_START_RECEIPT_KEY = "atomic_start"
_ATOMIC_START_SCHEMA_VERSION = 2
_ATOMIC_START_KINDS = frozenset({"adoption", "resume"})


class BacklogError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TaskFinalizationEvidence:
    """Read-only proof for one deterministic task-finalization generation."""

    stage: str
    complete: bool
    request_event_id: str | None
    decision_event_id: str | None
    task_release_event_id: str | None
    workset_release_event_id: str | None
    task_finish_event_id: str | None
    runtime_finalized: bool
    release_workset_claim: bool | None
    successor_present: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "complete": self.complete,
            "request_event_id": self.request_event_id,
            "decision_event_id": self.decision_event_id,
            "task_release_event_id": self.task_release_event_id,
            "workset_release_event_id": self.workset_release_event_id,
            "task_finish_event_id": self.task_finish_event_id,
            "runtime_finalized": self.runtime_finalized,
            "release_workset_claim": self.release_workset_claim,
            "successor_present": self.successor_present,
        }


class TaskRuntimeTransitionFinalizationError(BacklogError):
    """A retryable task-state transition stopped after an uncertain write."""

    def __init__(
        self,
        message: str,
        *,
        mutation_started: bool,
        mutation_phase: str,
        request_event_id: str | None,
        decision_event_id: str | None,
        owned_event_id: str | None,
    ) -> None:
        super().__init__(message)
        self.mutation_started = mutation_started
        self.mutation_phase = mutation_phase
        self.request_event_id = request_event_id
        self.decision_event_id = decision_event_id
        self.owned_event_id = owned_event_id


class TaskRuntimeTransitionGuardConflictError(BacklogError):
    """An identity-bound transition retry no longer names repairable state."""


class _TaskRuntimeTransitionUncertainError(RuntimeError):
    """An exception occurred after or between durable transition writes."""


class StaleClaimReleaseConflictError(BacklogError):
    """A stale-claim release is reserved, superseded, or conflicts with evidence."""


class _StaleClaimReleaseUncertainError(RuntimeError):
    """An exception occurred after or between durable transaction writes."""


class StaleClaimReleaseFinalizationError(BacklogError):
    """A retryable stale-claim release stopped after an uncertain write."""

    def __init__(
        self,
        message: str,
        *,
        mutation_started: bool,
        mutation_phase: str,
        request_event_id: str | None,
        decision_event_id: str | None,
        task_release_event_id: str | None,
        workset_release_event_id: str | None,
    ) -> None:
        super().__init__(message)
        self.mutation_started = mutation_started
        self.mutation_phase = mutation_phase
        self.request_event_id = request_event_id
        self.decision_event_id = decision_event_id
        self.task_release_event_id = task_release_event_id
        self.workset_release_event_id = workset_release_event_id


@dataclass(frozen=True, slots=True)
class StaleClaimReleaseResult:
    stale_claim: TaskClaimRecord
    released_at: str
    status: str
    summary: str
    note: str | None
    release_workset_claim: bool
    repaired_runtime_status: str | None
    failure_class: str
    recovery_action: str
    prompt_issue: bool
    operator_issue: bool
    request_event_id: str
    decision_event_id: str
    task_release_event_id: str
    workset_release_event_id: str | None
    runtime_changed: bool
    request_event_appended: bool
    decision_event_appended: bool
    task_release_event_appended: bool
    workset_release_event_appended: bool

    @property
    def events_changed(self) -> bool:
        return any(
            (
                self.request_event_appended,
                self.decision_event_appended,
                self.task_release_event_appended,
                self.workset_release_event_appended,
            )
        )


@dataclass(frozen=True, slots=True)
class TaskRuntimeTransitionResult:
    record: TaskRuntimeRecord
    runtime_changed: bool
    request_event_appended: bool
    decision_event_appended: bool
    owned_event_appended: bool

    @property
    def events_changed(self) -> bool:
        return (
            self.request_event_appended
            or self.decision_event_appended
            or self.owned_event_appended
        )


@dataclass(frozen=True, slots=True)
class AbandonedLandingEligibility:
    """Product-proved native abort evidence required for abandoned correction."""

    attempt_id: str
    transaction_id: str
    canonical_candidate: str


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


def _require_quiescent_workset_membership_prune(
    runtime_state: RuntimeState,
    *,
    workset_id: str,
    retained_task_ids: set[str],
) -> None:
    """Reject a planning update that would erase live task ownership."""

    runtime_workset = next(
        (
            row
            for row in runtime_state.worksets
            if row.workset_id == workset_id
        ),
        None,
    )
    if runtime_workset is None:
        return
    removed_claims = tuple(
        claim
        for claim in runtime_workset.task_claims
        if claim.task_id not in retained_task_ids
    )
    if removed_claims:
        task_list = ", ".join(repr(claim.task_id) for claim in removed_claims)
        raise BacklogError(
            f"Workset {workset_id!r} cannot remove claimed task(s) {task_list}; "
            "finish or recover their lifecycle first"
        )
    nonterminal_attempts = tuple(
        attempt
        for attempt in runtime_workset.attempts
        if attempt.task_id not in retained_task_ids
        and attempt.status == ATTEMPT_STATUS_IN_PROGRESS
    )
    if nonterminal_attempts:
        task_list = ", ".join(
            repr(attempt.task_id) for attempt in nonterminal_attempts
        )
        raise BacklogError(
            f"Workset {workset_id!r} cannot remove task(s) {task_list} with "
            "nonterminal attempts"
        )
    in_progress_states = tuple(
        record
        for record in runtime_workset.task_states
        if record.task_id not in retained_task_ids
        and record.status == TASK_STATUS_IN_PROGRESS
    )
    if in_progress_states:
        task_list = ", ".join(
            repr(record.task_id) for record in in_progress_states
        )
        raise BacklogError(
            f"Workset {workset_id!r} cannot remove in-progress task(s) {task_list}"
        )


def upsert_workset(
    profile: RepoProfile,
    payload: Mapping[str, Any],
    *,
    planning_store: PlanningStore | None = None,
    runtime_store: RuntimeStore | None = None,
    event_id: str | None = None,
) -> Workset:
    source = profile.paths.planning_file
    workset = _workset_from_payload(payload, source=source)
    task_ids = {task.task_id for task in workset.tasks}
    incoming_task_states = None
    if "task_states" in payload:
        incoming_task_states = coerce_task_runtime_records(
            payload.get("task_states"),
            known_task_ids=task_ids,
            source_name=str(source),
        )

    event_payload = {
        "workset_id": workset.workset_id,
        "task_count": len(workset.tasks),
        "has_runtime_patch": incoming_task_states is not None,
    }

    def reserve_runtime_and_event(
        *,
        save_planning: Callable[[], None],
    ) -> None:
        def checked_merge(runtime_state: RuntimeState) -> RuntimeState:
            _require_quiescent_workset_membership_prune(
                runtime_state,
                workset_id=workset.workset_id,
                retained_task_ids=task_ids,
            )
            next_runtime = merge_workset_runtime(
                runtime_state,
                workset_id=workset.workset_id,
                task_ids=task_ids,
                incoming_records=incoming_task_states,
            )
            _require_workset_merge_preserves_pending_task_transitions(
                profile,
                workset_id=workset.workset_id,
                task_ids=task_ids,
                current_runtime=runtime_state,
                next_runtime=next_runtime,
            )
            # Planning is written only after the pending-transition check and
            # while the runtime lock still excludes a new transition.
            save_planning()
            return next_runtime

        mutate_runtime_state(
            profile.paths,
            checked_merge,
            store=runtime_store,
            save_unchanged=event_id is None,
            after_save=(
                (
                    lambda _runtime_state: append_event_once(
                        profile.paths.events_file,
                        event_id=event_id,
                        event_type="workset.put",
                        payload=event_payload,
                    )
                )
                if event_id is not None
                else None
            ),
        )
        if event_id is None:
            append_event(
                profile.paths.events_file,
                event_type="workset.put",
                payload=event_payload,
            )

    with exclusive_file_lock(profile.paths.planning_file):
        current = load_planning_state(profile.paths, planning_store)
        existing = find_workset(current, workset.workset_id)
        if event_id is not None and existing is not None and existing != workset:
            raise BacklogError(
                f"Workset {workset.workset_id!r} conflicts with its deterministic reservation"
            )
        if event_id is None:
            remaining = [
                item
                for item in current.worksets
                if item.workset_id != workset.workset_id
            ]
            next_planning = PlanningState(
                schema_version=current.schema_version,
                store_version=current.store_version,
                worksets=tuple([*remaining, workset]),
            )
        elif existing is None:
            next_planning = PlanningState(
                schema_version=current.schema_version,
                store_version=current.store_version,
                worksets=(*current.worksets, workset),
            )
        else:
            next_planning = current

        def save_checked_planning() -> None:
            if event_id is None or existing is None:
                save_planning_state(
                    profile.paths,
                    next_planning,
                    planning_store,
                )

        reserve_runtime_and_event(save_planning=save_checked_planning)
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


def _task_scoped_runtime_task_ids(
    profile: RepoProfile,
    runtime_state: RuntimeState,
    *,
    workset_id: str,
    task_id: str,
    planning_store: PlanningStore | None,
) -> set[str]:
    """Revalidate membership and preserve rows outside a targeted mutation.

    Task operations load planning before they wait for the runtime lock.
    ``upsert_workset`` owns membership and writes planning while holding that
    runtime lock.  An unlocked read of the atomically replaced planning file at
    this boundary observes any winning upsert without reversing lock order.
    """

    current_planning = load_planning_state(profile.paths, planning_store)
    current_workset, _task = _require_workset_and_task(
        current_planning,
        workset_id=workset_id,
        task_id=task_id,
    )
    task_ids = {item.task_id for item in current_workset.tasks}
    for runtime_workset in runtime_state.worksets:
        if runtime_workset.workset_id != workset_id:
            continue
        task_ids.update(record.task_id for record in runtime_workset.task_states)
        task_ids.update(claim.task_id for claim in runtime_workset.task_claims)
        task_ids.update(attempt.task_id for attempt in runtime_workset.attempts)
    return task_ids


def _merge_task_scoped_runtime(
    profile: RepoProfile,
    runtime_state: RuntimeState,
    *,
    workset_id: str,
    task_id: str,
    planning_store: PlanningStore | None,
    incoming_records: tuple[TaskRuntimeRecord, ...] | None,
    incoming_workset_claim: WorksetClaimRecord | None | object = _UNSET,
    incoming_task_claims: tuple[TaskClaimRecord, ...] | None = None,
    released_task_claim_ids: tuple[str, ...] = (),
    incoming_attempts: tuple[TaskAttemptRecord, ...] | None = None,
) -> RuntimeState:
    task_ids = _task_scoped_runtime_task_ids(
        profile,
        runtime_state,
        workset_id=workset_id,
        task_id=task_id,
        planning_store=planning_store,
    )
    options: dict[str, Any] = {}
    if incoming_workset_claim is not _UNSET:
        options["incoming_workset_claim"] = incoming_workset_claim
    return merge_workset_runtime(
        runtime_state,
        workset_id=workset_id,
        task_ids=task_ids,
        incoming_records=incoming_records,
        incoming_task_claims=incoming_task_claims,
        released_task_claim_ids=released_task_claim_ids,
        incoming_attempts=incoming_attempts,
        **options,
    )


def task_start_event_id(*, attempt_id: str, event_type: str) -> str:
    return hashlib.sha256(
        f"blackdog.task.start-event/v1\0{attempt_id}\0{event_type}".encode("utf-8")
    ).hexdigest()


def _task_start_event_id(*, attempt_id: str, event_type: str) -> str:
    """Compatibility alias for the now-public pure event identity helper."""
    return task_start_event_id(attempt_id=attempt_id, event_type=event_type)


def _without_atomic_start_receipt(value: Mapping[str, Any] | None) -> dict[str, Any]:
    receipt = dict(value or {})
    receipt.pop(_ATOMIC_START_RECEIPT_KEY, None)
    return receipt


def _atomic_start_receipt(
    *,
    attempt_id: str,
    expected_predecessor_attempt_id: str,
    start_kind: str,
    expected_task_actor: str,
    expected_execution_prompt_hash: str,
    expected_execution_prompt_mode: str,
    expected_request_prompt_hash: str,
    expected_request_prompt_mode: str,
    expected_task_updated_at: str,
    workset_claim_created: bool,
) -> dict[str, Any]:
    return {
        "schema_version": _ATOMIC_START_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "expected_predecessor_attempt_id": expected_predecessor_attempt_id,
        "start_kind": start_kind,
        "expected_task_actor": expected_task_actor,
        "expected_execution_prompt_hash": expected_execution_prompt_hash,
        "expected_execution_prompt_mode": expected_execution_prompt_mode,
        "expected_request_prompt_hash": expected_request_prompt_hash,
        "expected_request_prompt_mode": expected_request_prompt_mode,
        "expected_task_updated_at": expected_task_updated_at,
        "workset_claim_created": workset_claim_created,
    }


def task_resume_attempt_id(
    *,
    workset_id: str,
    task_id: str,
    predecessor_attempt_id: str,
    actor: str,
    execution_prompt_hash: str,
    execution_prompt_mode: str,
    request_prompt_hash: str,
    request_prompt_mode: str,
) -> str:
    """Return the stable successor identity for one ordinary task resume."""

    digest = hashlib.sha256(
        "\0".join(
            (
                "blackdog.task.resume/v1",
                str(workset_id).strip(),
                str(task_id).strip(),
                str(predecessor_attempt_id).strip(),
                str(actor).strip(),
                str(execution_prompt_hash).strip(),
                str(execution_prompt_mode).strip(),
                str(request_prompt_hash).strip(),
                str(request_prompt_mode).strip(),
            )
        ).encode("utf-8")
    ).hexdigest()
    return f"{task_id}-resume-{digest[:12]}"


def resume_predecessor_identity(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    predecessor: TaskAttemptRecord,
) -> tuple[str, str]:
    """Derive the durable actor and task generation for one resume predecessor."""

    predecessor_ended = parse_iso(predecessor.ended_at)
    if predecessor_ended is None:
        raise BacklogError("ordinary resume predecessor terminal generation is invalid")

    events = load_events(profile.paths.events_file)
    terminal_indexes = []
    for index, event in enumerate(events):
        if event.get("type") != "task.finish":
            continue
        payload = event.get("payload")
        if (
            isinstance(payload, Mapping)
            and payload.get("workset_id") == workset_id
            and payload.get("task_id") == task_id
            and payload.get("attempt_id") == predecessor.attempt_id
        ):
            terminal_indexes.append(index)
    if len(terminal_indexes) > 1:
        raise BacklogError("ordinary resume predecessor terminal evidence is ambiguous")

    # Native finalization gives every predecessor one exact task.finish row. Its
    # append position, unlike second-precision timestamps, is a durable boundary
    # for cancel/reopen transitions belonging to this resume cycle.
    if terminal_indexes:
        transition_events = events[terminal_indexes[0] + 1 :]
        for event in reversed(transition_events):
            if event.get("type") not in {"task.cancel", "task.reopen"}:
                continue
            payload = event.get("payload")
            if (
                not isinstance(payload, Mapping)
                or payload.get("workset_id") != workset_id
                or payload.get("task_id") != task_id
            ):
                continue
            if event.get("type") != "task.reopen":
                raise BacklogError("ordinary resume predecessor is not durably reopened")
            updated_at = str(payload.get("updated_at") or "").strip()
            actor = str(event.get("actor") or "").strip()
            if not actor or parse_iso(updated_at) is None:
                raise BacklogError("ordinary resume reopen evidence is incomplete")
            return actor, updated_at
    else:
        # Compatibility is intentionally bounded to ledgers predating exact
        # terminal attempt evidence. Native ledgers must use append order above.
        for event in reversed(events):
            if event.get("type") not in {"task.cancel", "task.reopen"}:
                continue
            payload = event.get("payload")
            if (
                not isinstance(payload, Mapping)
                or payload.get("workset_id") != workset_id
                or payload.get("task_id") != task_id
            ):
                continue
            updated_at = str(payload.get("updated_at") or "").strip()
            transition_at = parse_iso(updated_at)
            if transition_at is None or transition_at <= predecessor_ended:
                continue
            if event.get("type") != "task.reopen":
                raise BacklogError("ordinary resume predecessor is not durably reopened")
            actor = str(event.get("actor") or "").strip()
            if not actor:
                raise BacklogError("ordinary resume reopen evidence is incomplete")
            return actor, updated_at

    actor = str(predecessor.actor or "").strip()
    updated_at = str(predecessor.ended_at or "").strip()
    if not actor or not updated_at:
        raise BacklogError("ordinary resume predecessor identity is incomplete")
    return actor, updated_at


def task_start_event_contracts(
    *,
    workset_id: str,
    task_id: str,
    attempt: TaskAttemptRecord,
    workset_claim_record: WorksetClaimRecord,
    workset_claim_created: bool,
    deterministic: bool,
) -> tuple[dict[str, Any], ...]:
    contracts: list[dict[str, Any]] = []

    def add(*, event_type: str, payload: Mapping[str, Any]) -> None:
        contracts.append(
            {
                "event_id": (
                    task_start_event_id(
                        attempt_id=attempt.attempt_id,
                        event_type=event_type,
                    )
                    if deterministic
                    else None
                ),
                "event_type": event_type,
                "actor": attempt.actor,
                "payload": dict(payload),
            }
        )

    if workset_claim_created:
        add(
            event_type="workset.claim",
            payload={
                "workset_id": workset_id,
                "execution_model": attempt.execution_model,
                "claimed_at": workset_claim_record.claimed_at,
                "note": workset_claim_record.note,
            },
        )
    add(
        event_type="task.claim",
        payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt.attempt_id,
            "execution_model": attempt.execution_model,
            "claimed_at": attempt.started_at,
            "note": attempt.note,
        },
    )
    add(
        event_type="task.start",
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
    return tuple(contracts)


def _ensure_task_start_events(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt: TaskAttemptRecord,
    workset_claim_record: WorksetClaimRecord,
    workset_claim_created: bool,
    deterministic: bool,
) -> None:
    for contract in task_start_event_contracts(
        workset_id=workset_id,
        task_id=task_id,
        attempt=attempt,
        workset_claim_record=workset_claim_record,
        workset_claim_created=workset_claim_created,
        deterministic=deterministic,
    ):
        if deterministic:
            append_event_once(
                profile.paths.events_file,
                event_id=str(contract["event_id"]),
                event_type=str(contract["event_type"]),
                actor=str(contract["actor"]),
                payload=contract["payload"],
            )
        else:
            append_event(
                profile.paths.events_file,
                event_type=str(contract["event_type"]),
                actor=str(contract["actor"]),
                payload=contract["payload"],
            )


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
    attempt_id: str | None = None,
    expected_predecessor_attempt_id: str | None = None,
    atomic_start_kind: str | None = None,
    expected_task_actor: str | None = None,
    expected_execution_prompt_hash: str | None = None,
    expected_execution_prompt_mode: str | None = None,
    expected_request_prompt_hash: str | None = None,
    expected_request_prompt_mode: str | None = None,
    expected_task_updated_at: str | None = None,
    planning_store: PlanningStore | None = None,
    runtime_store: RuntimeStore | None = None,
) -> TaskAttemptRecord:
    planning_state = load_planning_state(profile.paths, planning_store)
    workset, _ = _require_workset_and_task(planning_state, workset_id=workset_id, task_id=task_id)
    if execution_model not in EXECUTION_MODELS:
        raise BacklogError(f"execution_model must be one of {', '.join(sorted(EXECUTION_MODELS))}")
    if prompt_receipt is None:
        raise BacklogError("task start requires a prompt receipt")
    resolved_attempt_id = str(attempt_id or "").strip() or None
    resolved_predecessor_id = str(expected_predecessor_attempt_id or "").strip() or None
    resolved_atomic_guards = {
        "start_kind": str(atomic_start_kind or "").strip() or None,
        "task_actor": str(expected_task_actor or "").strip() or None,
        "execution_prompt_hash": str(expected_execution_prompt_hash or "").strip() or None,
        "execution_prompt_mode": str(expected_execution_prompt_mode or "").strip() or None,
        "request_prompt_hash": str(expected_request_prompt_hash or "").strip() or None,
        "request_prompt_mode": str(expected_request_prompt_mode or "").strip() or None,
        "task_updated_at": str(expected_task_updated_at or "").strip() or None,
    }
    deterministic_values = (
        resolved_attempt_id,
        resolved_predecessor_id,
        *resolved_atomic_guards.values(),
    )
    if any(value is not None for value in deterministic_values) and any(
        value is None for value in deterministic_values
    ):
        raise BacklogError(
            "deterministic task start requires attempt/predecessor ids, start kind, task actor/state, "
            "and complete execution/request prompt lineage"
        )
    if (
        resolved_atomic_guards["start_kind"] is not None
        and resolved_atomic_guards["start_kind"] not in _ATOMIC_START_KINDS
    ):
        raise BacklogError(
            f"atomic_start_kind must be one of {', '.join(sorted(_ATOMIC_START_KINDS))}"
        )
    if resolved_atomic_guards["start_kind"] == "resume":
        expected_resume_id = task_resume_attempt_id(
            workset_id=workset_id,
            task_id=task_id,
            predecessor_attempt_id=str(resolved_predecessor_id),
            actor=str(resolved_atomic_guards["task_actor"]),
            execution_prompt_hash=str(resolved_atomic_guards["execution_prompt_hash"]),
            execution_prompt_mode=str(resolved_atomic_guards["execution_prompt_mode"]),
            request_prompt_hash=str(resolved_atomic_guards["request_prompt_hash"]),
            request_prompt_mode=str(resolved_atomic_guards["request_prompt_mode"]),
        )
        if resolved_attempt_id != expected_resume_id:
            raise BacklogError(
                "ordinary resume attempt_id does not match its deterministic predecessor/lineage identity"
            )
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
        require_no_pending_stale_claim_release_for_workset(
            profile,
            workset_id=workset_id,
            runtime_state=runtime_state,
        )
        require_no_pending_task_runtime_transition(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
        )
        runtime_index = {
            task_state.task_id: task_state
            for runtime_workset in runtime_state.worksets
            if runtime_workset.workset_id == workset_id
            for task_state in runtime_workset.task_states
        }
        runtime_task_claims = task_claim_index(runtime_state, workset_id)
        current = runtime_index.get(task_id, TaskRuntimeRecord(task_id=task_id, status=TASK_STATUS_PLANNED))
        current_workset_claim = workset_claim(runtime_state, workset_id)
        same_task_attempts = tuple(
            attempt_row
            for runtime_workset in runtime_state.worksets
            if runtime_workset.workset_id == workset_id
            for attempt_row in runtime_workset.attempts
            if attempt_row.task_id == task_id
        )
        if resolved_attempt_id is not None and resolved_predecessor_id is not None:
            existing_successor = next(
                (row for row in same_task_attempts if row.attempt_id == resolved_attempt_id),
                None,
            )
            if existing_successor is not None:
                if (
                    len(same_task_attempts) < 2
                    or same_task_attempts[-1].attempt_id != resolved_attempt_id
                    or same_task_attempts[-2].attempt_id != resolved_predecessor_id
                ):
                    raise BacklogError(
                        "deterministic task start successor is not immediately after its expected predecessor"
                    )
                predecessor = same_task_attempts[-2]
                if (
                    predecessor.prompt_receipt is None
                    or predecessor.user_prompt_receipt is None
                    or (
                        predecessor.prompt_receipt.prompt_hash,
                        predecessor.prompt_receipt.mode,
                        predecessor.user_prompt_receipt.prompt_hash,
                        predecessor.user_prompt_receipt.mode,
                    )
                    != (
                        resolved_atomic_guards["execution_prompt_hash"],
                        resolved_atomic_guards["execution_prompt_mode"],
                        resolved_atomic_guards["request_prompt_hash"],
                        resolved_atomic_guards["request_prompt_mode"],
                    )
                ):
                    raise BacklogError(
                        "deterministic task start retry conflicts with predecessor prompt lineage"
                    )
                if resolved_atomic_guards["start_kind"] == "resume":
                    expected_actor, expected_generation = resume_predecessor_identity(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        predecessor=predecessor,
                    )
                    if (
                        resolved_atomic_guards["task_actor"] != expected_actor
                        or resolved_atomic_guards["task_updated_at"]
                        != expected_generation
                    ):
                        raise BacklogError(
                            "deterministic task start retry conflicts with predecessor actor or generation"
                        )
                current_task_claim = runtime_task_claims.get(task_id)
                expected_base_receipt = _without_atomic_start_receipt(setup_receipt)
                actual_atomic_receipt = (
                    existing_successor.setup_receipt.get(_ATOMIC_START_RECEIPT_KEY)
                    if isinstance(existing_successor.setup_receipt, Mapping)
                    else None
                )
                expected_fields = {
                    "status": ATTEMPT_STATUS_IN_PROGRESS,
                    "ended_at": None,
                    "actor": actor,
                    "workspace_identity": workspace_identity
                    or str(workset.workspace.get("identity") or "").strip()
                    or None,
                    "workspace_mode": workspace_mode,
                    "worktree_role": worktree_role,
                    "worktree_path": worktree_path,
                    "branch": resolved_branch,
                    "target_branch": target_branch
                    or str(workset.branch_intent.get("target_branch") or "").strip()
                    or None,
                    "integration_branch": integration_branch
                    or str(workset.branch_intent.get("integration_branch") or "").strip()
                    or None,
                    "start_commit": start_commit,
                    "execution_model": execution_model,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                    "codex_session": codex_session,
                    "prompt_receipt": prompt_receipt,
                    "user_prompt_receipt": resolved_user_prompt_receipt,
                    "note": note,
                }
                mismatches = [
                    field
                    for field, expected in expected_fields.items()
                    if getattr(existing_successor, field) != expected
                ]
                if (
                    mismatches
                    or _without_atomic_start_receipt(existing_successor.setup_receipt)
                    != expected_base_receipt
                    or actual_atomic_receipt
                    != _atomic_start_receipt(
                        attempt_id=resolved_attempt_id,
                        expected_predecessor_attempt_id=resolved_predecessor_id,
                        start_kind=str(resolved_atomic_guards["start_kind"]),
                        expected_task_actor=str(resolved_atomic_guards["task_actor"]),
                        expected_execution_prompt_hash=str(
                            resolved_atomic_guards["execution_prompt_hash"]
                        ),
                        expected_execution_prompt_mode=str(
                            resolved_atomic_guards["execution_prompt_mode"]
                        ),
                        expected_request_prompt_hash=str(
                            resolved_atomic_guards["request_prompt_hash"]
                        ),
                        expected_request_prompt_mode=str(
                            resolved_atomic_guards["request_prompt_mode"]
                        ),
                        expected_task_updated_at=str(
                            resolved_atomic_guards["task_updated_at"]
                        ),
                        workset_claim_created=bool(
                            isinstance(actual_atomic_receipt, Mapping)
                            and actual_atomic_receipt.get("workset_claim_created") is True
                        ),
                    )
                ):
                    raise BacklogError(
                        "deterministic task start retry conflicts with the active successor"
                    )
                if (
                    current.status != TASK_STATUS_IN_PROGRESS
                    or current.actor != actor
                    or current.updated_at != existing_successor.started_at
                    or current.note != existing_successor.note
                    or current_task_claim is None
                    or current_task_claim.attempt_id != resolved_attempt_id
                    or current_task_claim.actor != actor
                    or current_task_claim.execution_model != execution_model
                    or current_task_claim.claimed_at != existing_successor.started_at
                    or current_task_claim.note != existing_successor.note
                ):
                    raise BacklogError(
                        "deterministic task start retry conflicts with runtime claim state"
                    )
                derived_workset_claim_created = (
                    current_workset_claim is not None
                    and current_workset_claim.claimed_at == existing_successor.started_at
                    and current_workset_claim.note == existing_successor.note
                )
                if (
                    actual_atomic_receipt["workset_claim_created"]
                    is not derived_workset_claim_created
                ):
                    raise BacklogError(
                        "deterministic task start retry conflicts with workset-claim ownership"
                    )
                created_workset_claim = derived_workset_claim_created
                if (
                    current_workset_claim is None
                    or current_workset_claim.actor != actor
                    or current_workset_claim.execution_model != execution_model
                    or (
                        created_workset_claim
                        and (
                            current_workset_claim.claimed_at != existing_successor.started_at
                            or current_workset_claim.note != existing_successor.note
                        )
                    )
                ):
                    raise BacklogError(
                        "deterministic task start retry conflicts with its reusable workset claim"
                    )
                attempt = existing_successor
                started_at = existing_successor.started_at
                next_workset_claim = current_workset_claim
                emit_workset_claim = created_workset_claim
                return runtime_state

            if any(row.attempt_id == resolved_attempt_id for row in same_task_attempts):
                raise BacklogError(f"Attempt {resolved_attempt_id!r} already exists")
            if (
                not same_task_attempts
                or same_task_attempts[-1].attempt_id != resolved_predecessor_id
            ):
                raise BacklogError(
                    "expected predecessor is not the latest appended same-task attempt"
                )
            predecessor = same_task_attempts[-1]
            if predecessor.status == ATTEMPT_STATUS_IN_PROGRESS or predecessor.ended_at is None:
                raise BacklogError("expected predecessor attempt is not terminal")
            if current.status not in {TASK_STATUS_PLANNED, TASK_STATUS_BLOCKED}:
                raise BacklogError(
                    f"Task {task_id!r} is not restartable from status {current.status!r}"
                )
            durable_task_actor = current.actor or predecessor.actor
            if (
                durable_task_actor != resolved_atomic_guards["task_actor"]
                or actor != resolved_atomic_guards["task_actor"]
                or current.updated_at != resolved_atomic_guards["task_updated_at"]
            ):
                raise BacklogError(
                    "deterministic task start task actor or runtime generation no longer matches"
                )
            if predecessor.prompt_receipt is None or predecessor.user_prompt_receipt is None:
                raise BacklogError(
                    "deterministic task start predecessor is missing execution or request prompt lineage"
                )
            expected_lineage = (
                resolved_atomic_guards["execution_prompt_hash"],
                resolved_atomic_guards["execution_prompt_mode"],
                resolved_atomic_guards["request_prompt_hash"],
                resolved_atomic_guards["request_prompt_mode"],
            )
            predecessor_lineage = (
                predecessor.prompt_receipt.prompt_hash,
                predecessor.prompt_receipt.mode,
                predecessor.user_prompt_receipt.prompt_hash,
                predecessor.user_prompt_receipt.mode,
            )
            incoming_lineage = (
                prompt_receipt.prompt_hash,
                prompt_receipt.mode,
                resolved_user_prompt_receipt.prompt_hash,
                resolved_user_prompt_receipt.mode,
            )
            if predecessor_lineage != expected_lineage or incoming_lineage != expected_lineage:
                raise BacklogError(
                    "deterministic task start prompt lineage no longer matches the predecessor"
                )
            if runtime_task_claims.get(task_id) is not None:
                raise BacklogError(f"Task {task_id!r} is already claimed")
        else:
            if current.status == TASK_STATUS_DONE:
                raise BacklogError(f"Task {task_id!r} is already done")
            if current.status == TASK_STATUS_IN_PROGRESS:
                raise BacklogError(f"Task {task_id!r} is already in progress")
            if current.status == TASK_STATUS_CANCELED:
                raise BacklogError(f"Task {task_id!r} is canceled; reopen it before starting")
            if same_task_attempts:
                raise BacklogError(
                    "task restart requires deterministic atomic start guards for its latest predecessor"
                )
            current_task_claim = runtime_task_claims.get(task_id)
            if current_task_claim is not None:
                raise BacklogError(f"Task {task_id!r} is already claimed by {current_task_claim.actor}")
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
        setup_payload = dict(setup_receipt) if setup_receipt is not None else None
        emit_workset_claim = reusable_workset_claim is None
        if resolved_attempt_id is not None and resolved_predecessor_id is not None:
            setup_payload = dict(setup_payload or {})
            setup_payload.pop(_ATOMIC_START_RECEIPT_KEY, None)
            setup_payload[_ATOMIC_START_RECEIPT_KEY] = _atomic_start_receipt(
                attempt_id=resolved_attempt_id,
                expected_predecessor_attempt_id=resolved_predecessor_id,
                start_kind=str(resolved_atomic_guards["start_kind"]),
                expected_task_actor=str(resolved_atomic_guards["task_actor"]),
                expected_execution_prompt_hash=str(
                    resolved_atomic_guards["execution_prompt_hash"]
                ),
                expected_execution_prompt_mode=str(
                    resolved_atomic_guards["execution_prompt_mode"]
                ),
                expected_request_prompt_hash=str(
                    resolved_atomic_guards["request_prompt_hash"]
                ),
                expected_request_prompt_mode=str(
                    resolved_atomic_guards["request_prompt_mode"]
                ),
                expected_task_updated_at=str(
                    resolved_atomic_guards["task_updated_at"]
                ),
                workset_claim_created=emit_workset_claim,
            )
        attempt = TaskAttemptRecord(
            attempt_id=resolved_attempt_id or f"{task_id}-{uuid.uuid4().hex[:12]}",
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
            setup_receipt=setup_payload,
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
            actor=actor,
            note=note,
        )
        return _merge_task_scoped_runtime(
            profile,
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            planning_store=planning_store,
            incoming_records=(task_runtime,),
            incoming_workset_claim=next_workset_claim,
            incoming_task_claims=(next_task_claim,),
            incoming_attempts=(attempt,),
        )

    mutate_runtime_state(
        profile.paths,
        mutate,
        store=runtime_store,
        save_unchanged=False,
    )
    if attempt is None or next_workset_claim is None or started_at is None:
        raise BacklogError("task start did not create an attempt")
    _ensure_task_start_events(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt=attempt,
        workset_claim_record=next_workset_claim,
        workset_claim_created=emit_workset_claim,
        deterministic=True,
    )
    return attempt


def repair_task_start_events(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
) -> TaskAttemptRecord:
    """Repair deterministic start events for one already-reserved active attempt."""

    planning_state = load_planning_state(profile.paths)
    workset, _task = _require_workset_and_task(
        planning_state,
        workset_id=workset_id,
        task_id=task_id,
    )
    runtime_state = load_runtime_state(profile.paths)
    attempt = find_task_attempt(runtime_state, workset_id, attempt_id)
    task_record = task_state_index(runtime_state, workset_id).get(task_id)
    task_claim = task_claim_index(runtime_state, workset_id).get(task_id)
    workset_claim_record = workset_claim(runtime_state, workset_id)
    if (
        attempt is None
        or attempt.task_id != task_id
        or attempt.status != ATTEMPT_STATUS_IN_PROGRESS
        or attempt.ended_at is not None
        or task_record is None
        or task_record.status != TASK_STATUS_IN_PROGRESS
        or task_record.updated_at != attempt.started_at
        or task_record.actor != attempt.actor
        or task_record.note != attempt.note
        or task_claim is None
        or task_claim.attempt_id != attempt.attempt_id
        or task_claim.actor != attempt.actor
        or task_claim.execution_model != attempt.execution_model
        or task_claim.claimed_at != attempt.started_at
        or task_claim.note != attempt.note
        or workset_claim_record is None
        or workset_claim_record.actor != attempt.actor
        or workset_claim_record.execution_model != attempt.execution_model
    ):
        raise BacklogError("reserved task start runtime and claim state is not canonical")
    workset_claim_created = (
        workset_claim_record.claimed_at == attempt.started_at
        and workset_claim_record.note == attempt.note
    )
    _ensure_task_start_events(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt=attempt,
        workset_claim_record=workset_claim_record,
        workset_claim_created=workset_claim_created,
        deterministic=True,
    )
    return attempt


def _derived_attempt_elapsed_seconds(
    attempt: TaskAttemptRecord,
    *,
    ended_at: str,
    elapsed_seconds: int | None,
) -> int | None:
    if elapsed_seconds is not None:
        return elapsed_seconds
    started_at_value = parse_iso(attempt.started_at)
    ended_at_value = parse_iso(ended_at)
    if started_at_value is None or ended_at_value is None:
        return None
    return max(0, int((ended_at_value - started_at_value).total_seconds()))


def _task_status_for_attempt_status(status: str) -> str:
    if status == ATTEMPT_STATUS_SUCCESS:
        return TASK_STATUS_DONE
    if status == ATTEMPT_STATUS_ABANDONED:
        return TASK_STATUS_CANCELED
    return TASK_STATUS_BLOCKED


_FINALIZATION_REQUEST_SCHEMA_VERSION = 1
_FINALIZATION_DECISION_SCHEMA_VERSION = 1
_FINALIZATION_DECISION_KEYS = frozenset(
    {
        "schema_version",
        "finalization_id",
        "request_event_id",
        "request_semantics_hash",
        "workset_id",
        "task_id",
        "attempt_id",
        "pre_runtime_workset_hash",
        "expected_pre_runtime_identity",
        "ended_at",
        "release_task_claim",
        "release_workset_claim",
        "expected_terminal_identity",
        "expected_post_runtime_workset_hash",
    }
)


def _canonical_payload_hash(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BacklogError(f"finalization semantics are not valid JSON: {exc}") from exc
    return hashlib.sha256(encoded).hexdigest()


def _canonical_payload_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's loose bool/int equality."""

    return _canonical_payload_hash({"value": left}) == _canonical_payload_hash(
        {"value": right}
    )


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _finalization_request_event_id(*, workset_id: str, task_id: str, attempt_id: str) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                "blackdog.task.finalization.request/v1",
                workset_id,
                task_id,
                attempt_id,
            )
        ).encode("utf-8")
    ).hexdigest()


def _finalization_decision_event_id(*, request_event_id: str, pre_runtime_workset_hash: str) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                "blackdog.task.finalization.decision/v1",
                request_event_id,
                pre_runtime_workset_hash,
            )
        ).encode("utf-8")
    ).hexdigest()


def _finalization_owned_event_id(*, decision_event_id: str, event_type: str) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                "blackdog.task.finalization.owned-event/v1",
                decision_event_id,
                event_type,
            )
        ).encode("utf-8")
    ).hexdigest()


def task_finalization_request_event_id(
    *, workset_id: str, task_id: str, attempt_id: str
) -> str:
    """Return the core-owned request identity for one exact attempt finalization."""

    return _finalization_request_event_id(
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
    )


def task_finalization_decision_event_id(
    *, request_event_id: str, pre_runtime_workset_hash: str
) -> str:
    """Return the core-owned decision identity for one runtime pre-state."""

    return _finalization_decision_event_id(
        request_event_id=request_event_id,
        pre_runtime_workset_hash=pre_runtime_workset_hash,
    )


def task_finalization_owned_event_id(
    *, decision_event_id: str, event_type: str
) -> str:
    """Return an append-once identity owned by a finalization decision."""

    if event_type not in {"task.release", "workset.release", "task.finish"}:
        raise BacklogError(f"unsupported finalization-owned event type {event_type!r}")
    return _finalization_owned_event_id(
        decision_event_id=decision_event_id,
        event_type=event_type,
    )


def _runtime_workset_payload(runtime_state: RuntimeState, workset_id: str) -> dict[str, Any]:
    payload = runtime_state_to_payload(runtime_state)
    for workset_payload in payload["worksets"]:
        if workset_payload.get("id") == workset_id:
            return workset_payload
    return {"id": workset_id, "missing": True}


def _runtime_workset_hash(runtime_state: RuntimeState, workset_id: str) -> str:
    return _canonical_payload_hash(_runtime_workset_payload(runtime_state, workset_id))


def _terminal_runtime_identity(
    runtime_state: RuntimeState,
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
) -> str:
    workset_payload = _runtime_workset_payload(runtime_state, workset_id)
    attempts = [
        attempt
        for attempt in workset_payload.get("attempts", [])
        if attempt.get("attempt_id") == attempt_id
    ]
    task_states = [
        task_state
        for task_state in workset_payload.get("task_states", [])
        if task_state.get("task_id") == task_id
    ]
    task_claims = [
        task_claim
        for task_claim in workset_payload.get("task_claims", [])
        if task_claim.get("task_id") == task_id
    ]
    return _canonical_payload_hash(
        {
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "attempts": attempts,
            "task_states": task_states,
            "task_claims": task_claims,
        }
    )


def _finalization_request_payload(
    *,
    finalization_id: str,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    actor: str,
    status: str,
    summary: str | None,
    changed_paths: tuple[str, ...],
    validations: tuple[ValidationRecord, ...],
    residuals: tuple[str, ...],
    followup_candidates: tuple[str, ...],
    commit: str | None,
    landed_commit: str | None,
    elapsed_seconds: int | None,
    failure_class: str | None,
    recovery_action: str | None,
    prompt_issue: bool,
    operator_issue: bool,
    note: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": _FINALIZATION_REQUEST_SCHEMA_VERSION,
        "finalization_id": finalization_id,
        "workset_id": workset_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "actor": actor,
        "status": status,
        "summary": summary,
        "changed_paths": list(changed_paths),
        "validations": [
            {"name": validation.name, "status": validation.status}
            for validation in validations
        ],
        "residuals": list(residuals),
        "followup_candidates": list(followup_candidates),
        "commit": commit,
        "landed_commit": landed_commit,
        "elapsed_seconds": elapsed_seconds,
        "elapsed_seconds_mode": "derived" if elapsed_seconds is None else "explicit",
        "failure_class": default_failure_class_for_status(status, failure_class),
        "recovery_action": str(recovery_action or "").strip() or None,
        "prompt_issue": bool(prompt_issue),
        "operator_issue": bool(operator_issue or status == ATTEMPT_STATUS_ABANDONED),
        "note": note,
        "note_mode": "preserve" if not note else "replace",
    }


def _terminal_attempt_from_request(
    existing_attempt: TaskAttemptRecord,
    *,
    request: Mapping[str, Any],
    ended_at: str,
) -> TaskAttemptRecord:
    requested_elapsed_seconds = request.get("elapsed_seconds")
    derived_elapsed_seconds = _derived_attempt_elapsed_seconds(
        existing_attempt,
        ended_at=ended_at,
        elapsed_seconds=(
            int(requested_elapsed_seconds)
            if requested_elapsed_seconds is not None
            else None
        ),
    )
    return TaskAttemptRecord(
        attempt_id=existing_attempt.attempt_id,
        task_id=existing_attempt.task_id,
        status=str(request["status"]),
        actor=existing_attempt.actor,
        started_at=existing_attempt.started_at,
        ended_at=ended_at,
        summary=request.get("summary"),
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
        changed_paths=tuple(request.get("changed_paths") or ()),
        validations=tuple(
            ValidationRecord(name=item["name"], status=item["status"])
            for item in request.get("validations") or ()
        ),
        residuals=tuple(request.get("residuals") or ()),
        followup_candidates=tuple(request.get("followup_candidates") or ()),
        note=request.get("note") or existing_attempt.note,
        commit=request.get("commit"),
        landed_commit=request.get("landed_commit"),
        elapsed_seconds=derived_elapsed_seconds,
        failure_class=request.get("failure_class"),
        recovery_action=request.get("recovery_action"),
        prompt_issue=bool(request.get("prompt_issue")),
        operator_issue=bool(request.get("operator_issue")),
        setup_receipt=existing_attempt.setup_receipt,
    )


def _terminal_task_runtime_from_request(
    *,
    task_id: str,
    request: Mapping[str, Any],
    ended_at: str,
) -> TaskRuntimeRecord:
    return TaskRuntimeRecord(
        task_id=task_id,
        status=_task_status_for_attempt_status(str(request["status"])),
        updated_at=ended_at,
        actor=str(request["actor"]),
        note=request.get("summary") or request.get("note"),
        failure_class=request.get("failure_class"),
        recovery_action=request.get("recovery_action"),
        prompt_issue=bool(request.get("prompt_issue")),
        operator_issue=bool(request.get("operator_issue")),
    )


def _load_finalization_decisions(
    profile: RepoProfile,
    *,
    request_event_id: str,
    request_semantics_hash: str,
    finalization_id: str,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    actor: str,
) -> tuple[dict[str, Any], ...]:
    decisions: list[dict[str, Any]] = []
    seen_pre_hashes: set[str] = set()
    for event in load_events(profile.paths.events_file):
        if event.get("type") != "task.finalization.decision":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("request_event_id") != request_event_id:
            continue
        if set(payload) != _FINALIZATION_DECISION_KEYS:
            raise BacklogError("finalization decision row has conflicting fields")
        if event.get("actor") != actor:
            raise BacklogError("finalization decision row has a conflicting actor")
        if type(payload.get("schema_version")) is not int or payload.get("schema_version") != 1:
            raise BacklogError("finalization decision row has an unsupported schema version")
        expected_scalars = {
            "finalization_id": finalization_id,
            "request_semantics_hash": request_semantics_hash,
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
        }
        if any(payload.get(key) != value for key, value in expected_scalars.items()):
            raise BacklogError("finalization decision row conflicts with its request")
        pre_hash = payload.get("pre_runtime_workset_hash")
        if not _is_sha256(pre_hash):
            raise BacklogError("finalization decision row has an invalid pre-runtime hash")
        expected_event_id = _finalization_decision_event_id(
            request_event_id=request_event_id,
            pre_runtime_workset_hash=pre_hash,
        )
        if event.get("event_id") != expected_event_id:
            raise BacklogError("finalization decision row has a conflicting event identity")
        if pre_hash in seen_pre_hashes:
            raise BacklogError("finalization decision pre-state occurs more than once")
        seen_pre_hashes.add(pre_hash)
        ended_at = payload.get("ended_at")
        if not isinstance(ended_at, str) or parse_iso(ended_at) is None:
            raise BacklogError("finalization decision row has an invalid ended_at")
        if type(payload.get("release_task_claim")) is not bool or not payload.get("release_task_claim"):
            raise BacklogError("finalization decision row has an invalid task release decision")
        if type(payload.get("release_workset_claim")) is not bool:
            raise BacklogError("finalization decision row has an invalid workset release decision")
        for key in (
            "expected_pre_runtime_identity",
            "expected_terminal_identity",
            "expected_post_runtime_workset_hash",
        ):
            value = payload.get(key)
            if not _is_sha256(value):
                raise BacklogError(f"finalization decision row has an invalid {key}")
        decisions.append({"event_id": expected_event_id, "payload": dict(payload)})
    return tuple(decisions)


def _build_terminal_runtime_state(
    profile: RepoProfile,
    runtime_state: RuntimeState,
    *,
    workset: Workset,
    task_id: str,
    attempt_id: str,
    actor: str,
    request: Mapping[str, Any],
    ended_at: str,
    planning_store: PlanningStore | None,
) -> tuple[RuntimeState, TaskAttemptRecord, bool]:
    existing_attempt = find_task_attempt(runtime_state, workset.workset_id, attempt_id)
    if existing_attempt is None:
        raise BacklogError(f"Unknown attempt {attempt_id!r} in workset {workset.workset_id!r}")
    if existing_attempt.task_id != task_id:
        raise BacklogError(f"Attempt {attempt_id!r} does not belong to task {task_id!r}")
    if existing_attempt.actor != actor:
        raise BacklogError(f"Attempt {attempt_id!r} is owned by {existing_attempt.actor}, not {actor}")
    if existing_attempt.status != ATTEMPT_STATUS_IN_PROGRESS or existing_attempt.ended_at is not None:
        raise BacklogError(f"Attempt {attempt_id!r} is not active")

    current_task_state = task_state_index(runtime_state, workset.workset_id).get(task_id)
    if current_task_state is None or current_task_state.status != TASK_STATUS_IN_PROGRESS:
        raise BacklogError(f"Attempt {attempt_id!r} has conflicting active task runtime state")
    current_task_claims = task_claim_index(runtime_state, workset.workset_id)
    target_claim = current_task_claims.get(task_id)
    if target_claim is None:
        raise BacklogError(f"Attempt {attempt_id!r} has no active task claim")
    if target_claim.actor != actor or target_claim.attempt_id not in {None, attempt_id}:
        raise BacklogError(f"Attempt {attempt_id!r} has a conflicting task claim")

    current_workset_claim = workset_claim(runtime_state, workset.workset_id)
    if current_workset_claim is not None and current_workset_claim.actor != actor:
        raise BacklogError(f"Attempt {attempt_id!r} has a conflicting workset claim")
    remaining_task_claims = tuple(
        claim
        for claim_task_id, claim in current_task_claims.items()
        if claim_task_id != task_id
    )
    release_workset_claim = current_workset_claim is not None and not remaining_task_claims
    finished_attempt = _terminal_attempt_from_request(
        existing_attempt,
        request=request,
        ended_at=ended_at,
    )
    task_runtime = _terminal_task_runtime_from_request(
        task_id=task_id,
        request=request,
        ended_at=ended_at,
    )
    next_state = _merge_task_scoped_runtime(
        profile,
        runtime_state,
        workset_id=workset.workset_id,
        task_id=task_id,
        planning_store=planning_store,
        incoming_records=(task_runtime,),
        incoming_workset_claim=None if release_workset_claim else current_workset_claim,
        released_task_claim_ids=(task_id,),
        incoming_attempts=(finished_attempt,),
    )
    return next_state, finished_attempt, release_workset_claim


def _assert_terminal_runtime_matches_request(
    runtime_state: RuntimeState,
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    actor: str,
    request: Mapping[str, Any],
) -> TaskAttemptRecord:
    existing_attempt = find_task_attempt(runtime_state, workset_id, attempt_id)
    if existing_attempt is None:
        raise BacklogError(f"Unknown attempt {attempt_id!r} in workset {workset_id!r}")
    if existing_attempt.task_id != task_id:
        raise BacklogError(f"Attempt {attempt_id!r} does not belong to task {task_id!r}")
    if existing_attempt.actor != actor:
        raise BacklogError(f"Attempt {attempt_id!r} is owned by {existing_attempt.actor}, not {actor}")
    if existing_attempt.status == ATTEMPT_STATUS_IN_PROGRESS or existing_attempt.ended_at is None:
        raise BacklogError(f"Attempt {attempt_id!r} is not terminal")
    expected_attempt = _terminal_attempt_from_request(
        existing_attempt,
        request=request,
        ended_at=existing_attempt.ended_at,
    )
    if expected_attempt != existing_attempt:
        raise BacklogError(f"Attempt {attempt_id!r} finalization retry conflicts with terminal runtime state")
    expected_task_state = _terminal_task_runtime_from_request(
        task_id=task_id,
        request=request,
        ended_at=existing_attempt.ended_at,
    )
    if task_state_index(runtime_state, workset_id).get(task_id) != expected_task_state:
        raise BacklogError(f"Attempt {attempt_id!r} finalization retry conflicts with task runtime state")
    if task_id in task_claim_index(runtime_state, workset_id):
        raise BacklogError(f"Attempt {attempt_id!r} finalization retry found a retained task claim")
    return existing_attempt


def _finalization_owned_payloads(
    *,
    request_event_id: str,
    decision_event_id: str,
    request: Mapping[str, Any],
    finished_attempt: TaskAttemptRecord,
) -> dict[str, dict[str, Any]]:
    common = {
        "finalization_id": request["finalization_id"],
        "finalization_request_id": request_event_id,
        "finalization_decision_id": decision_event_id,
    }
    task_release = {
        **common,
        "workset_id": request["workset_id"],
        "task_id": request["task_id"],
        "attempt_id": request["attempt_id"],
        "released_at": finished_attempt.ended_at,
        "status": finished_attempt.status,
    }
    workset_release = {
        **common,
        "workset_id": request["workset_id"],
        "task_id": request["task_id"],
        "attempt_id": request["attempt_id"],
        "released_at": finished_attempt.ended_at,
        "status": finished_attempt.status,
    }
    task_finish = {
        **common,
        "workset_id": request["workset_id"],
        "task_id": request["task_id"],
        "attempt_id": request["attempt_id"],
        "status": finished_attempt.status,
        "summary": finished_attempt.summary,
        "worktree_role": finished_attempt.worktree_role,
        "worktree_path": finished_attempt.worktree_path,
        "branch": finished_attempt.branch,
        "start_commit": finished_attempt.start_commit,
        "execution_model": finished_attempt.execution_model,
        "model": finished_attempt.model,
        "reasoning_effort": finished_attempt.reasoning_effort,
        "codex_thread_id": (
            finished_attempt.codex_session.thread_id
            if finished_attempt.codex_session is not None
            else None
        ),
        "codex_session_path": (
            finished_attempt.codex_session.session_path
            if finished_attempt.codex_session is not None
            else None
        ),
        "codex_turn_id": (
            finished_attempt.codex_session.turn_id
            if finished_attempt.codex_session is not None
            else None
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
        "changed_paths": list(finished_attempt.changed_paths),
        "validations": [
            {"name": item.name, "status": item.status}
            for item in finished_attempt.validations
        ],
        "residuals": list(finished_attempt.residuals),
        "followup_candidates": list(finished_attempt.followup_candidates),
        "commit": finished_attempt.commit,
        "landed_commit": finished_attempt.landed_commit,
        "elapsed_seconds": finished_attempt.elapsed_seconds,
        "failure_class": finished_attempt.failure_class,
        "recovery_action": finished_attempt.recovery_action,
        "prompt_issue": finished_attempt.prompt_issue,
        "operator_issue": finished_attempt.operator_issue,
    }
    return {
        "task.release": task_release,
        "workset.release": workset_release,
        "task.finish": task_finish,
    }


def _validate_existing_owned_events(
    profile: RepoProfile,
    *,
    actor: str,
    decision_event_id: str,
    release_workset_claim: bool,
    expected_payloads: Mapping[str, Mapping[str, Any]],
) -> frozenset[str]:
    expected_ids = {
        event_type: _finalization_owned_event_id(
            decision_event_id=decision_event_id,
            event_type=event_type,
        )
        for event_type in expected_payloads
    }
    rows_by_id: dict[str, list[Mapping[str, Any]]] = {event_id: [] for event_id in expected_ids.values()}
    for event in load_events(profile.paths.events_file):
        event_id = event.get("event_id")
        if event_id in rows_by_id:
            rows_by_id[event_id].append(event)
    present: set[str] = set()
    for event_type, event_id in expected_ids.items():
        rows = rows_by_id[event_id]
        if len(rows) > 1:
            raise BacklogError(f"finalization-owned event {event_type} occurs more than once")
        if not rows:
            continue
        row = rows[0]
        payload = row.get("payload")
        if (
            row.get("type") != event_type
            or row.get("actor") != actor
            or not isinstance(payload, Mapping)
            or _canonical_payload_hash(payload) != _canonical_payload_hash(expected_payloads[event_type])
        ):
            raise BacklogError(f"finalization-owned event {event_type} has conflicting content")
        present.add(event_type)
    if not release_workset_claim and "workset.release" in present:
        raise BacklogError(
            "finalization-owned workset.release conflicts with a release_workset_claim=false decision"
        )
    return frozenset(present)


def _load_exact_finalization_request(
    profile: RepoProfile,
    *,
    request_event_id: str,
    actor: str,
    expected_request: Mapping[str, Any],
) -> bool:
    """Validate the sole request generation for one exact attempt."""

    target = {
        "workset_id": expected_request["workset_id"],
        "task_id": expected_request["task_id"],
        "attempt_id": expected_request["attempt_id"],
    }
    rows: list[Mapping[str, Any]] = []
    for event in load_events(profile.paths.events_file):
        payload = event.get("payload")
        same_target_request = bool(
            event.get("type") == "task.finalization.request"
            and isinstance(payload, Mapping)
            and all(payload.get(key) == value for key, value in target.items())
        )
        if event.get("event_id") == request_event_id or same_target_request:
            rows.append(event)
    if len(rows) > 1:
        raise BacklogError("task finalization request occurs more than once")
    if not rows:
        return False
    row = rows[0]
    if (
        row.get("event_id") != request_event_id
        or row.get("type") != "task.finalization.request"
        or row.get("actor") != actor
        or not isinstance(row.get("payload"), Mapping)
        or not _canonical_payload_equal(row["payload"], expected_request)
    ):
        raise BacklogError("task finalization request conflicts with its durable identity")
    return True


def _strict_finalization_owned_events(
    profile: RepoProfile,
    *,
    request_event_id: str,
    request: Mapping[str, Any],
    actor: str,
    decision: Mapping[str, Any],
    expected_payloads: Mapping[str, Mapping[str, Any]],
) -> frozenset[str]:
    """Validate exact identities and reject aliases for decision-owned rows."""

    decision_event_id = str(decision["event_id"])
    release_workset_claim = bool(decision["payload"]["release_workset_claim"])
    allowed_types = {"task.release", "task.finish"}
    if release_workset_claim:
        allowed_types.add("workset.release")
    expected_ids = {
        event_type: _finalization_owned_event_id(
            decision_event_id=decision_event_id,
            event_type=event_type,
        )
        for event_type in expected_payloads
    }
    rows_by_type: dict[str, list[Mapping[str, Any]]] = {
        event_type: [] for event_type in expected_payloads
    }
    for event in load_events(profile.paths.events_file):
        payload = event.get("payload")
        event_type = event.get("type")
        recognized_payload = bool(
            isinstance(payload, Mapping)
            and (
                payload.get("finalization_request_id") == request_event_id
                or (
                    payload.get("finalization_id") == request["finalization_id"]
                    and payload.get("workset_id") == request["workset_id"]
                    and payload.get("task_id") == request["task_id"]
                    and payload.get("attempt_id") == request["attempt_id"]
                    and payload.get("finalization_decision_id") == decision_event_id
                )
            )
        )
        event_id_collision = event.get("event_id") in expected_ids.values()
        if not recognized_payload and not event_id_collision:
            continue
        if event_type not in rows_by_type or event_type not in allowed_types:
            raise BacklogError("finalization-owned event has a conflicting type")
        rows_by_type[str(event_type)].append(event)
    for event_type, rows in rows_by_type.items():
        if len(rows) > 1:
            raise BacklogError(f"finalization-owned event {event_type} occurs more than once")
        if not rows:
            continue
        row = rows[0]
        if (
            row.get("event_id") != expected_ids[event_type]
            or row.get("actor") != actor
            or not isinstance(row.get("payload"), Mapping)
            or not _canonical_payload_equal(row["payload"], expected_payloads[event_type])
        ):
            raise BacklogError(
                f"finalization-owned event {event_type} has conflicting content"
            )
    return _validate_existing_owned_events(
        profile,
        actor=actor,
        decision_event_id=decision_event_id,
        release_workset_claim=release_workset_claim,
        expected_payloads=expected_payloads,
    )


def _require_no_orphan_finalization_owned_events(
    profile: RepoProfile,
    *,
    request_event_id: str,
    request: Mapping[str, Any],
) -> None:
    for event in load_events(profile.paths.events_file):
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if event.get("type") not in {"task.release", "workset.release", "task.finish"}:
            continue
        if (
            payload.get("finalization_request_id") == request_event_id
            or (
                payload.get("finalization_id") == request["finalization_id"]
                and payload.get("workset_id") == request["workset_id"]
                and payload.get("task_id") == request["task_id"]
                and payload.get("attempt_id") == request["attempt_id"]
            )
        ):
            raise BacklogError("finalization-owned event is orphaned from its decision")


def inspect_task_finalization(
    profile: RepoProfile,
    *,
    finalization_id: str,
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
    runtime_store: RuntimeStore | None = None,
) -> TaskFinalizationEvidence:
    """Re-derive durable finalization proof without changing runtime or events.

    This is the semantic owner for product-layer transactions that need to
    decide whether ``finish_task(finalization_id=...)`` must run again. It
    deliberately reuses the same request builder, decision validator,
    terminal-record builder, and owned-event validator as the writer.
    """

    request = _finalization_request_payload(
        finalization_id=finalization_id,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
        actor=actor,
        status=status,
        summary=summary,
        changed_paths=tuple(changed_paths),
        validations=tuple(validations),
        residuals=tuple(residuals),
        followup_candidates=tuple(followup_candidates),
        commit=commit,
        landed_commit=landed_commit,
        elapsed_seconds=elapsed_seconds,
        failure_class=failure_class,
        recovery_action=recovery_action,
        prompt_issue=prompt_issue,
        operator_issue=operator_issue,
        note=note,
    )
    request_event_id = _finalization_request_event_id(
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    request_present = _load_exact_finalization_request(
        profile,
        request_event_id=request_event_id,
        actor=actor,
        expected_request=request,
    )
    if not request_present:
        _require_no_orphan_finalization_owned_events(
            profile,
            request_event_id=request_event_id,
            request=request,
        )
        return TaskFinalizationEvidence(
            stage="not_started",
            complete=False,
            request_event_id=None,
            decision_event_id=None,
            task_release_event_id=None,
            workset_release_event_id=None,
            task_finish_event_id=None,
            runtime_finalized=False,
            release_workset_claim=None,
            successor_present=False,
        )

    request_semantics_hash = _canonical_payload_hash(request)
    decisions = _load_finalization_decisions(
        profile,
        request_event_id=request_event_id,
        request_semantics_hash=request_semantics_hash,
        finalization_id=finalization_id,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
        actor=actor,
    )
    runtime_state = load_runtime_state(profile.paths, store=runtime_store)
    attempt = find_task_attempt(runtime_state, workset_id, attempt_id)
    if attempt is None or attempt.task_id != task_id or attempt.actor != actor:
        raise BacklogError("task finalization request no longer names its exact attempt")
    latest = latest_task_attempt(runtime_state, workset_id, task_id)
    successor_present = bool(latest is not None and latest.attempt_id != attempt_id)
    current_identity = _terminal_runtime_identity(
        runtime_state,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    if not decisions:
        _require_no_orphan_finalization_owned_events(
            profile,
            request_event_id=request_event_id,
            request=request,
        )
        if successor_present:
            raise BacklogError(
                "incomplete task finalization conflicts with a later attempt"
            )
        return TaskFinalizationEvidence(
            stage="request_recorded",
            complete=False,
            request_event_id=request_event_id,
            decision_event_id=None,
            task_release_event_id=None,
            workset_release_event_id=None,
            task_finish_event_id=None,
            runtime_finalized=False,
            release_workset_claim=None,
            successor_present=False,
        )

    candidates: list[tuple[Mapping[str, Any], TaskAttemptRecord, frozenset[str]]] = []
    for decision in decisions:
        candidate_attempt = _terminal_attempt_from_request(
            attempt,
            request=request,
            ended_at=str(decision["payload"]["ended_at"]),
        )
        expected_payloads = _finalization_owned_payloads(
            request_event_id=request_event_id,
            decision_event_id=str(decision["event_id"]),
            request=request,
            finished_attempt=candidate_attempt,
        )
        present = _strict_finalization_owned_events(
            profile,
            request_event_id=request_event_id,
            request=request,
            actor=actor,
            decision=decision,
            expected_payloads=expected_payloads,
        )
        if attempt.status == ATTEMPT_STATUS_IN_PROGRESS and attempt.ended_at is None:
            if (
                decision["payload"]["expected_pre_runtime_identity"] == current_identity
                or present
            ):
                candidates.append((decision, candidate_attempt, present))
        elif candidate_attempt == attempt:
            candidates.append((decision, candidate_attempt, present))
    if not candidates:
        raise BacklogError("task finalization decisions do not match the exact attempt")
    if len(candidates) > 1:
        identity_matches = [
            row
            for row in candidates
            if row[0]["payload"]["expected_terminal_identity"] == current_identity
        ]
        event_backed = [row for row in candidates if row[2]]
        if len(identity_matches) == 1:
            candidates = identity_matches
        elif len(event_backed) == 1:
            candidates = event_backed
        else:
            raise BacklogError("multiple task finalization decisions match the attempt")
    decision, candidate_attempt, present = candidates[0]
    decision_event_id = str(decision["event_id"])
    release_workset_claim = bool(decision["payload"]["release_workset_claim"])

    runtime_finalized = False
    if attempt == candidate_attempt:
        expected_task_state = _terminal_task_runtime_from_request(
            task_id=task_id,
            request=request,
            ended_at=str(attempt.ended_at),
        )
        live_task_state_matches = bool(
            task_state_index(runtime_state, workset_id).get(task_id)
            == expected_task_state
            and task_id not in task_claim_index(runtime_state, workset_id)
        )
        runtime_finalized = live_task_state_matches or bool(present)

    task_release_event_id = (
        _finalization_owned_event_id(
            decision_event_id=decision_event_id,
            event_type="task.release",
        )
        if "task.release" in present
        else None
    )
    workset_release_event_id = (
        _finalization_owned_event_id(
            decision_event_id=decision_event_id,
            event_type="workset.release",
        )
        if "workset.release" in present
        else None
    )
    task_finish_event_id = (
        _finalization_owned_event_id(
            decision_event_id=decision_event_id,
            event_type="task.finish",
        )
        if "task.finish" in present
        else None
    )
    complete = bool(
        runtime_finalized
        and task_release_event_id is not None
        and (not release_workset_claim or workset_release_event_id is not None)
        and task_finish_event_id is not None
    )
    if successor_present and not complete:
        raise BacklogError("incomplete task finalization conflicts with a later attempt")
    if task_finish_event_id is not None:
        stage = "owned_events_complete" if complete else "task_finish_recorded"
    elif release_workset_claim and workset_release_event_id is not None:
        stage = "workset_release_recorded"
    elif task_release_event_id is not None:
        stage = "task_release_recorded"
    elif runtime_finalized:
        stage = "runtime_finalized"
    else:
        stage = "decision_recorded"
    return TaskFinalizationEvidence(
        stage=stage,
        complete=complete,
        request_event_id=request_event_id,
        decision_event_id=decision_event_id,
        task_release_event_id=task_release_event_id,
        workset_release_event_id=workset_release_event_id,
        task_finish_event_id=task_finish_event_id,
        runtime_finalized=runtime_finalized,
        release_workset_claim=release_workset_claim,
        successor_present=successor_present,
    )


def _append_decision_owned_events(
    profile: RepoProfile,
    *,
    actor: str,
    decision: Mapping[str, Any],
    request_event_id: str,
    request: Mapping[str, Any],
    runtime_state: RuntimeState,
    finished_attempt: TaskAttemptRecord,
) -> None:
    decision_event_id = str(decision["event_id"])
    decision_payload = decision["payload"]
    current_terminal_identity = _terminal_runtime_identity(
        runtime_state,
        workset_id=str(request["workset_id"]),
        task_id=str(request["task_id"]),
        attempt_id=str(request["attempt_id"]),
    )
    if current_terminal_identity != decision_payload["expected_terminal_identity"]:
        raise BacklogError("finalization decision no longer matches terminal runtime state")
    expected_payloads = _finalization_owned_payloads(
        request_event_id=request_event_id,
        decision_event_id=decision_event_id,
        request=request,
        finished_attempt=finished_attempt,
    )
    _validate_existing_owned_events(
        profile,
        actor=actor,
        decision_event_id=decision_event_id,
        release_workset_claim=bool(decision_payload["release_workset_claim"]),
        expected_payloads=expected_payloads,
    )
    event_types = ["task.release"]
    if decision_payload["release_workset_claim"]:
        event_types.append("workset.release")
    event_types.append("task.finish")
    for event_type in event_types:
        append_event_once(
            profile.paths.events_file,
            event_id=_finalization_owned_event_id(
                decision_event_id=decision_event_id,
                event_type=event_type,
            ),
            event_type=event_type,
            actor=actor,
            payload=expected_payloads[event_type],
        )


def _finish_task_with_finalization(
    profile: RepoProfile,
    *,
    workset: Workset,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    actor: str,
    finalization_id: str,
    request: Mapping[str, Any],
    planning_store: PlanningStore | None,
    runtime_store: RuntimeStore | None,
) -> TaskAttemptRecord:
    request_event_id = _finalization_request_event_id(
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    request_semantics_hash = _canonical_payload_hash(request)

    # Reject obvious ownership/identity mistakes before reserving the one
    # deterministic request identity for this attempt. The runtime-lock pass
    # below repeats every check at the mutation boundary.
    preflight_state = load_runtime_state(profile.paths, store=runtime_store)
    require_no_pending_task_runtime_transition(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        runtime_state=preflight_state,
    )
    require_no_pending_stale_claim_release_for_workset(
        profile,
        workset_id=workset_id,
        runtime_state=preflight_state,
    )
    preflight_attempt = find_task_attempt(preflight_state, workset_id, attempt_id)
    if preflight_attempt is None:
        raise BacklogError(f"Unknown attempt {attempt_id!r} in workset {workset_id!r}")
    if preflight_attempt.task_id != task_id:
        raise BacklogError(f"Attempt {attempt_id!r} does not belong to task {task_id!r}")
    if preflight_attempt.actor != actor:
        raise BacklogError(f"Attempt {attempt_id!r} is owned by {preflight_attempt.actor}, not {actor}")

    try:
        append_event_once(
            profile.paths.events_file,
            event_id=request_event_id,
            event_type="task.finalization.request",
            actor=actor,
            payload=request,
        )
    except StoreError as exc:
        raise BacklogError(
            f"Attempt {attempt_id!r} finalization request conflicts with its durable identity"
        ) from exc

    chosen_decision: dict[str, Any] | None = None
    finished_attempt: TaskAttemptRecord | None = None

    def mutate(runtime_state: RuntimeState) -> RuntimeState:
        nonlocal chosen_decision, finished_attempt
        _task_scoped_runtime_task_ids(
            profile,
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            planning_store=planning_store,
        )
        require_no_pending_stale_claim_release_for_workset(
            profile,
            workset_id=workset_id,
            runtime_state=runtime_state,
        )
        require_no_pending_task_runtime_transition(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
        )
        existing_attempt = find_task_attempt(runtime_state, workset_id, attempt_id)
        if existing_attempt is None:
            raise BacklogError(f"Unknown attempt {attempt_id!r} in workset {workset_id!r}")
        if existing_attempt.task_id != task_id:
            raise BacklogError(f"Attempt {attempt_id!r} does not belong to task {task_id!r}")
        if existing_attempt.actor != actor:
            raise BacklogError(f"Attempt {attempt_id!r} is owned by {existing_attempt.actor}, not {actor}")

        decisions = _load_finalization_decisions(
            profile,
            request_event_id=request_event_id,
            request_semantics_hash=request_semantics_hash,
            finalization_id=finalization_id,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor=actor,
        )
        current_workset_hash = _runtime_workset_hash(runtime_state, workset_id)
        current_task_identity = _terminal_runtime_identity(
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
        )

        if existing_attempt.status == ATTEMPT_STATUS_IN_PROGRESS and existing_attempt.ended_at is None:
            exact = [
                decision
                for decision in decisions
                if decision["payload"]["pre_runtime_workset_hash"] == current_workset_hash
            ]
            if len(exact) > 1:
                raise BacklogError("multiple finalization decisions match the active runtime state")
            decision = exact[0] if exact else None
            rollback_repair = False
            if decision is None:
                repair_candidates: list[dict[str, Any]] = []
                for candidate in decisions:
                    if candidate["payload"]["expected_pre_runtime_identity"] != current_task_identity:
                        continue
                    candidate_attempt = _terminal_attempt_from_request(
                        existing_attempt,
                        request=request,
                        ended_at=candidate["payload"]["ended_at"],
                    )
                    expected_payloads = _finalization_owned_payloads(
                        request_event_id=request_event_id,
                        decision_event_id=candidate["event_id"],
                        request=request,
                        finished_attempt=candidate_attempt,
                    )
                    present = _validate_existing_owned_events(
                        profile,
                        actor=actor,
                        decision_event_id=candidate["event_id"],
                        release_workset_claim=bool(
                            candidate["payload"]["release_workset_claim"]
                        ),
                        expected_payloads=expected_payloads,
                    )
                    if present:
                        repair_candidates.append(candidate)
                if len(repair_candidates) > 1:
                    raise BacklogError("multiple event-backed finalization decisions match rolled-back runtime")
                if repair_candidates:
                    decision = repair_candidates[0]
                    rollback_repair = True

            ended_at = decision["payload"]["ended_at"] if decision is not None else now_iso()
            next_state, next_attempt, current_release_workset = _build_terminal_runtime_state(
                profile,
                runtime_state,
                workset=workset,
                task_id=task_id,
                attempt_id=attempt_id,
                actor=actor,
                request=request,
                ended_at=ended_at,
                planning_store=planning_store,
            )
            terminal_identity = _terminal_runtime_identity(
                next_state,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=attempt_id,
            )
            post_workset_hash = _runtime_workset_hash(next_state, workset_id)

            if decision is None:
                decision_payload = {
                    "schema_version": _FINALIZATION_DECISION_SCHEMA_VERSION,
                    "finalization_id": finalization_id,
                    "request_event_id": request_event_id,
                    "request_semantics_hash": request_semantics_hash,
                    "workset_id": workset_id,
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "pre_runtime_workset_hash": current_workset_hash,
                    "expected_pre_runtime_identity": current_task_identity,
                    "ended_at": ended_at,
                    "release_task_claim": True,
                    "release_workset_claim": current_release_workset,
                    "expected_terminal_identity": terminal_identity,
                    "expected_post_runtime_workset_hash": post_workset_hash,
                }
                decision_event_id = _finalization_decision_event_id(
                    request_event_id=request_event_id,
                    pre_runtime_workset_hash=current_workset_hash,
                )
                append_event_once(
                    profile.paths.events_file,
                    event_id=decision_event_id,
                    event_type="task.finalization.decision",
                    actor=actor,
                    payload=decision_payload,
                )
                decision = {"event_id": decision_event_id, "payload": decision_payload}
            else:
                decision_payload = decision["payload"]
                if decision_payload["expected_pre_runtime_identity"] != current_task_identity:
                    raise BacklogError("finalization decision conflicts with active task identity")
                if decision_payload["expected_terminal_identity"] != terminal_identity:
                    raise BacklogError("finalization decision conflicts with terminal task identity")
                if not rollback_repair and (
                    decision_payload["release_workset_claim"] != current_release_workset
                    or decision_payload["expected_post_runtime_workset_hash"] != post_workset_hash
                ):
                    raise BacklogError("finalization decision conflicts with the runtime transition")
                append_event_once(
                    profile.paths.events_file,
                    event_id=decision["event_id"],
                    event_type="task.finalization.decision",
                    actor=actor,
                    payload=decision_payload,
                )
            expected_payloads = _finalization_owned_payloads(
                request_event_id=request_event_id,
                decision_event_id=decision["event_id"],
                request=request,
                finished_attempt=next_attempt,
            )
            _validate_existing_owned_events(
                profile,
                actor=actor,
                decision_event_id=decision["event_id"],
                release_workset_claim=bool(
                    decision["payload"]["release_workset_claim"]
                ),
                expected_payloads=expected_payloads,
            )
            chosen_decision = decision
            finished_attempt = next_attempt
            return next_state

        finished_attempt = _assert_terminal_runtime_matches_request(
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor=actor,
            request=request,
        )
        terminal_identity = _terminal_runtime_identity(
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
        )
        candidates = [
            decision
            for decision in decisions
            if decision["payload"]["expected_terminal_identity"] == terminal_identity
            and decision["payload"]["ended_at"] == finished_attempt.ended_at
        ]
        if not candidates:
            raise BacklogError(
                f"Attempt {attempt_id!r} terminal runtime has no matching finalization decision"
            )
        exact_post = [
            decision
            for decision in candidates
            if decision["payload"]["expected_post_runtime_workset_hash"] == current_workset_hash
        ]
        if len(exact_post) > 1:
            raise BacklogError("multiple finalization decisions match terminal runtime")
        if exact_post:
            chosen_decision = exact_post[0]
        elif len(candidates) == 1:
            chosen_decision = candidates[0]
        else:
            event_backed: list[dict[str, Any]] = []
            for candidate in candidates:
                candidate_payloads = _finalization_owned_payloads(
                    request_event_id=request_event_id,
                    decision_event_id=candidate["event_id"],
                    request=request,
                    finished_attempt=finished_attempt,
                )
                present = _validate_existing_owned_events(
                    profile,
                    actor=actor,
                    decision_event_id=candidate["event_id"],
                    release_workset_claim=bool(
                        candidate["payload"]["release_workset_claim"]
                    ),
                    expected_payloads=candidate_payloads,
                )
                if present:
                    event_backed.append(candidate)
            if len(event_backed) != 1:
                raise BacklogError("multiple finalization decisions match terminal runtime")
            chosen_decision = event_backed[0]
        expected_payloads = _finalization_owned_payloads(
            request_event_id=request_event_id,
            decision_event_id=chosen_decision["event_id"],
            request=request,
            finished_attempt=finished_attempt,
        )
        _validate_existing_owned_events(
            profile,
            actor=actor,
            decision_event_id=chosen_decision["event_id"],
            release_workset_claim=bool(
                chosen_decision["payload"]["release_workset_claim"]
            ),
            expected_payloads=expected_payloads,
        )
        return runtime_state

    def after_save(runtime_state: RuntimeState) -> None:
        if chosen_decision is None or finished_attempt is None:
            raise BacklogError("task finalization did not choose a durable decision")
        _append_decision_owned_events(
            profile,
            actor=actor,
            decision=chosen_decision,
            request_event_id=request_event_id,
            request=request,
            runtime_state=runtime_state,
            finished_attempt=finished_attempt,
        )

    mutate_runtime_state(
        profile.paths,
        mutate,
        store=runtime_store,
        after_save=after_save,
        save_unchanged=False,
    )
    if finished_attempt is None:
        raise BacklogError("task finalization did not return an attempt")
    return finished_attempt


def _finalization_event_id(
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    event_type: str,
) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                "blackdog.task.finalization.event/v1",
                workset_id,
                task_id,
                attempt_id,
                event_type,
            )
        ).encode("utf-8")
    ).hexdigest()


def _append_finalization_event(
    profile: RepoProfile,
    *,
    finalization_id: str | None,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    event_type: str,
    actor: str,
    payload: Mapping[str, Any],
) -> None:
    if finalization_id is None:
        append_event(
            profile.paths.events_file,
            event_type=event_type,
            actor=actor,
            payload=payload,
        )
        return
    append_event_once(
        profile.paths.events_file,
        event_id=_finalization_event_id(
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
            event_type=event_type,
        ),
        event_type=event_type,
        actor=actor,
        payload={**payload, "finalization_id": finalization_id},
    )


def _assert_finalization_event_identity(
    profile: RepoProfile,
    *,
    finalization_id: str,
    workset_id: str,
    task_id: str,
    attempt_id: str,
) -> None:
    expected_event_ids = {
        _finalization_event_id(
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
            event_type=event_type,
        )
        for event_type in ("task.release", "workset.release", "task.finish")
    }
    for event in load_events(profile.paths.events_file):
        if event.get("event_id") not in expected_event_ids:
            continue
        payload = event.get("payload")
        existing_finalization_id = (
            payload.get("finalization_id") if isinstance(payload, Mapping) else None
        )
        if existing_finalization_id != finalization_id:
            raise BacklogError(
                f"Attempt {attempt_id!r} already has canonical finalization events with a different finalization_id"
            )


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
    finalization_id: str | None = None,
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
    resolved_finalization_id = None
    if finalization_id is not None:
        resolved_finalization_id = str(finalization_id).strip()
        if not resolved_finalization_id:
            raise BacklogError("task finish finalization_id must be nonempty when supplied")
    planning_state = load_planning_state(profile.paths, planning_store)
    workset, _ = _require_workset_and_task(planning_state, workset_id=workset_id, task_id=task_id)
    if resolved_finalization_id is not None:
        request = _finalization_request_payload(
            finalization_id=resolved_finalization_id,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor=actor,
            status=status,
            summary=summary,
            changed_paths=tuple(changed_paths),
            validations=tuple(validations),
            residuals=tuple(residuals),
            followup_candidates=tuple(followup_candidates),
            commit=commit,
            landed_commit=landed_commit,
            elapsed_seconds=elapsed_seconds,
            failure_class=failure_class,
            recovery_action=recovery_action,
            prompt_issue=prompt_issue,
            operator_issue=operator_issue,
            note=note,
        )
        return _finish_task_with_finalization(
            profile,
            finalization_id=resolved_finalization_id,
            workset=workset,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor=actor,
            request=request,
            planning_store=planning_store,
            runtime_store=runtime_store,
        )

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

        _task_scoped_runtime_task_ids(
            profile,
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            planning_store=planning_store,
        )
        require_no_pending_stale_claim_release_for_workset(
            profile,
            workset_id=workset_id,
            runtime_state=runtime_state,
        )
        require_no_pending_task_runtime_transition(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
        )

        existing_attempt = find_task_attempt(runtime_state, workset_id, attempt_id)
        if existing_attempt is None:
            raise BacklogError(f"Unknown attempt {attempt_id!r} in workset {workset_id!r}")
        if existing_attempt.task_id != task_id:
            raise BacklogError(f"Attempt {attempt_id!r} does not belong to task {task_id!r}")
        if existing_attempt.actor != actor:
            raise BacklogError(f"Attempt {attempt_id!r} is owned by {existing_attempt.actor}, not {actor}")
        current_task_claims = task_claim_index(runtime_state, workset_id)
        remaining_task_claims = tuple(
            claim
            for claim_task_id, claim in current_task_claims.items()
            if claim_task_id != task_id
        )
        current_workset_claim = workset_claim(runtime_state, workset_id)

        if existing_attempt.status != ATTEMPT_STATUS_IN_PROGRESS or existing_attempt.ended_at is not None:
            if resolved_finalization_id is None:
                raise BacklogError(f"Attempt {attempt_id!r} is not active")
            if existing_attempt.ended_at is None:
                raise BacklogError(f"Attempt {attempt_id!r} has inconsistent terminal state")
            resolved_failure_class = default_failure_class_for_status(status, failure_class)
            resolved_recovery_action = str(recovery_action or "").strip() or None
            resolved_prompt_issue = bool(prompt_issue)
            resolved_operator_issue = bool(operator_issue or status == ATTEMPT_STATUS_ABANDONED)
            ended_at = existing_attempt.ended_at
            derived_elapsed_seconds = _derived_attempt_elapsed_seconds(
                existing_attempt,
                ended_at=ended_at,
                elapsed_seconds=elapsed_seconds,
            )
            expected_note = note or existing_attempt.note
            expected_fields = {
                "status": status,
                "summary": summary,
                "changed_paths": tuple(changed_paths),
                "validations": tuple(validations),
                "residuals": tuple(residuals),
                "followup_candidates": tuple(followup_candidates),
                "note": expected_note,
                "commit": commit,
                "landed_commit": landed_commit,
                "elapsed_seconds": derived_elapsed_seconds,
                "failure_class": resolved_failure_class,
                "recovery_action": resolved_recovery_action,
                "prompt_issue": resolved_prompt_issue,
                "operator_issue": resolved_operator_issue,
            }
            mismatches = [
                field
                for field, expected in expected_fields.items()
                if getattr(existing_attempt, field) != expected
            ]
            if mismatches:
                raise BacklogError(
                    f"Attempt {attempt_id!r} finalization retry conflicts on: {', '.join(mismatches)}"
                )
            if task_id in current_task_claims:
                raise BacklogError(
                    f"Attempt {attempt_id!r} finalization retry found a retained task claim"
                )
            current_task_state = task_state_index(runtime_state, workset_id).get(task_id)
            expected_task_status = _task_status_for_attempt_status(status)
            expected_task_note = summary or note
            if (
                current_task_state is None
                or current_task_state.status != expected_task_status
                or current_task_state.updated_at != ended_at
                or current_task_state.note != expected_task_note
                or current_task_state.failure_class != resolved_failure_class
                or current_task_state.recovery_action != resolved_recovery_action
                or current_task_state.prompt_issue != resolved_prompt_issue
                or current_task_state.operator_issue != resolved_operator_issue
            ):
                raise BacklogError(
                    f"Attempt {attempt_id!r} finalization retry conflicts with task runtime state"
                )
            if not remaining_task_claims and current_workset_claim is not None:
                raise BacklogError(
                    f"Attempt {attempt_id!r} finalization retry found an unreleased workset claim"
                )
            release_workset_claim = not remaining_task_claims and current_workset_claim is None
            finished_attempt = existing_attempt
            return runtime_state

        resolved_failure_class = default_failure_class_for_status(status, failure_class)
        resolved_recovery_action = str(recovery_action or "").strip() or None
        resolved_prompt_issue = bool(prompt_issue)
        resolved_operator_issue = bool(operator_issue or status == ATTEMPT_STATUS_ABANDONED)
        ended_at = now_iso()
        derived_elapsed_seconds = _derived_attempt_elapsed_seconds(
            existing_attempt,
            ended_at=ended_at,
            elapsed_seconds=elapsed_seconds,
        )
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
        task_runtime_status = _task_status_for_attempt_status(status)
        task_runtime = TaskRuntimeRecord(
            task_id=task_id,
            status=task_runtime_status,
            updated_at=ended_at,
            actor=existing_attempt.actor,
            note=summary or note,
            failure_class=resolved_failure_class,
            recovery_action=resolved_recovery_action,
            prompt_issue=resolved_prompt_issue,
            operator_issue=resolved_operator_issue,
        )
        release_workset_claim = current_workset_claim is not None and not remaining_task_claims
        return _merge_task_scoped_runtime(
            profile,
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            planning_store=planning_store,
            incoming_records=(task_runtime,),
            incoming_workset_claim=None if release_workset_claim else current_workset_claim,
            released_task_claim_ids=(task_id,),
            incoming_attempts=(finished_attempt,),
        )

    mutate_runtime_state(profile.paths, mutate, store=runtime_store)
    if finished_attempt is None or ended_at is None:
        raise BacklogError("task finish did not update an attempt")
    _append_finalization_event(
        profile,
        finalization_id=resolved_finalization_id,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
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
        _append_finalization_event(
            profile,
            finalization_id=resolved_finalization_id,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
            event_type="workset.release",
            actor=actor,
            payload={
                "workset_id": workset_id,
                "released_at": ended_at,
                "status": status,
            },
        )
    _append_finalization_event(
        profile,
        finalization_id=resolved_finalization_id,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
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


def _require_abandoned_landing_eligibility(
    eligibility: AbandonedLandingEligibility | None,
    *,
    attempt_id: str,
    landed_commit: str,
) -> None:
    if eligibility is None:
        raise BacklogError(
            "abandoned landing reconciliation requires explicit native abort-complete eligibility"
        )
    if (
        str(eligibility.attempt_id).strip() != attempt_id
        or not str(eligibility.transaction_id).strip()
        or str(eligibility.canonical_candidate).strip().lower() != landed_commit
    ):
        raise BacklogError(
            "abandoned landing reconciliation eligibility does not match the exact attempt and canonical candidate"
        )


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
    abandoned_eligibility: AbandonedLandingEligibility | None = None,
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
            and event["payload"].get("status")
            in {
                ATTEMPT_STATUS_BLOCKED,
                ATTEMPT_STATUS_FAILED,
                ATTEMPT_STATUS_ABANDONED,
            }
        ),
        None,
    )
    reconciled_at = now_iso()
    previous_status: str | None = None
    corrected_attempt: TaskAttemptRecord | None = None
    runtime_changed = False

    def mutate(runtime_state):
        nonlocal previous_status, corrected_attempt, runtime_changed
        _task_scoped_runtime_task_ids(
            profile,
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            planning_store=planning_store,
        )
        require_no_pending_stale_claim_release(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
        )
        require_no_pending_task_runtime_transition(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
        )
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
            if previous_status == ATTEMPT_STATUS_ABANDONED:
                _require_abandoned_landing_eligibility(
                    abandoned_eligibility,
                    attempt_id=attempt_id,
                    landed_commit=normalized_landed_commit,
                )
            corrected_attempt = existing_attempt
            return runtime_state
        if existing_attempt.status not in {
            ATTEMPT_STATUS_BLOCKED,
            ATTEMPT_STATUS_FAILED,
            ATTEMPT_STATUS_ABANDONED,
        }:
            raise BacklogError(
                f"Attempt {attempt_id!r} status {existing_attempt.status!r} is not failed, blocked, or abandoned"
            )
        previous_status = existing_attempt.status
        if previous_status == ATTEMPT_STATUS_ABANDONED:
            _require_abandoned_landing_eligibility(
                abandoned_eligibility,
                attempt_id=attempt_id,
                landed_commit=normalized_landed_commit,
            )
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
            actor=existing_attempt.actor,
            note=current_task_state.note if current_task_state is not None else existing_attempt.summary,
            failure_class=None,
            recovery_action=None,
            prompt_issue=False,
            operator_issue=False,
        )
        runtime_changed = True
        return _merge_task_scoped_runtime(
            profile,
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            planning_store=planning_store,
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


_STALE_CLAIM_RELEASE_REQUEST_SCHEMA_VERSION = 1
_STALE_CLAIM_RELEASE_DECISION_SCHEMA_VERSION = 1
_STALE_CLAIM_RELEASE_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "workset_id",
        "task_id",
        "actor",
        "status",
        "summary",
        "note",
        "stale_claim",
        "active_attempt_id",
        "pre_target_identity",
        "pre_target",
        "pre_claim_set_identity",
        "failure_class",
        "recovery_action",
        "prompt_issue",
        "operator_issue",
    }
)
_STALE_CLAIM_RELEASE_DECISION_KEYS = frozenset(
    {
        "schema_version",
        "request_event_id",
        "request_semantics_hash",
        "workset_id",
        "task_id",
        "actor",
        "released_at",
        "pre_target_identity",
        "expected_post_target_identity",
        "pre_target",
        "expected_post_target",
        "pre_claim_set_identity",
        "expected_post_claim_set_identity",
        "pre_claim_set",
        "expected_post_claim_set",
        "pre_runtime_workset_hash",
        "expected_post_runtime_workset_hash",
        "repaired_task_record",
        "repaired_runtime_status",
        "release_workset_claim",
        "pre_workset_claim",
        "expected_post_workset_claim",
        "task_release_event_id",
        "task_release_event_payload",
        "workset_release_event_id",
        "workset_release_event_payload",
    }
)


def _stale_claim_payload(claim: TaskClaimRecord) -> dict[str, Any]:
    return {
        "task_id": claim.task_id,
        "actor": claim.actor,
        "execution_model": claim.execution_model,
        "claimed_at": claim.claimed_at,
        "attempt_id": claim.attempt_id,
        "note": claim.note,
    }


def _stale_workset_claim_payload(claim: WorksetClaimRecord | None) -> dict[str, Any] | None:
    if claim is None:
        return None
    return {
        "actor": claim.actor,
        "execution_model": claim.execution_model,
        "claimed_at": claim.claimed_at,
        "note": claim.note,
    }


def _canonical_optional_text(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and bool(value) and value.strip() == value
    )


def _validate_stale_task_claim_payload(
    payload: Any,
    *,
    task_id: str | None = None,
) -> None:
    if (
        not isinstance(payload, Mapping)
        or set(payload)
        != {"task_id", "actor", "execution_model", "claimed_at", "attempt_id", "note"}
        or not isinstance(payload.get("task_id"), str)
        or not payload.get("task_id")
        or str(payload["task_id"]).strip() != payload["task_id"]
        or task_id is not None
        and payload["task_id"] != task_id
        or not isinstance(payload.get("actor"), str)
        or not payload.get("actor")
        or str(payload["actor"]).strip() != payload["actor"]
        or not isinstance(payload.get("execution_model"), str)
        or not payload.get("execution_model")
        or str(payload["execution_model"]).strip() != payload["execution_model"]
        or not isinstance(payload.get("claimed_at"), str)
        or not payload.get("claimed_at")
        or str(payload["claimed_at"]).strip() != payload["claimed_at"]
        or payload.get("attempt_id") is not None
        and (
            not isinstance(payload.get("attempt_id"), str)
            or not payload.get("attempt_id")
            or str(payload["attempt_id"]).strip() != payload["attempt_id"]
        )
        or not _canonical_optional_text(payload.get("note"))
    ):
        raise StaleClaimReleaseConflictError(
            "stale-claim release has an invalid canonical task claim"
        )


def _validate_stale_workset_claim_payload(payload: Any) -> None:
    if payload is None:
        return
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"actor", "execution_model", "claimed_at", "note"}
        or not isinstance(payload.get("actor"), str)
        or not payload.get("actor")
        or str(payload["actor"]).strip() != payload["actor"]
        or not isinstance(payload.get("execution_model"), str)
        or not payload.get("execution_model")
        or str(payload["execution_model"]).strip() != payload["execution_model"]
        or not isinstance(payload.get("claimed_at"), str)
        or not payload.get("claimed_at")
        or str(payload["claimed_at"]).strip() != payload["claimed_at"]
        or not _canonical_optional_text(payload.get("note"))
    ):
        raise StaleClaimReleaseConflictError(
            "stale-claim release has an invalid canonical workset claim"
        )


def _validate_stale_claim_set_projection(
    projection: Any,
    *,
    workset_id: str,
) -> None:
    if (
        not isinstance(projection, Mapping)
        or set(projection) != {"workset_id", "workset_claim", "task_claims"}
        or projection.get("workset_id") != workset_id
        or not isinstance(projection.get("task_claims"), list)
    ):
        raise StaleClaimReleaseConflictError(
            "stale-claim release has an invalid claim-set projection"
        )
    _validate_stale_workset_claim_payload(projection.get("workset_claim"))
    seen: set[str] = set()
    ordered: list[str] = []
    for claim in projection["task_claims"]:
        _validate_stale_task_claim_payload(claim)
        claim_task_id = str(claim["task_id"])
        if claim_task_id in seen:
            raise StaleClaimReleaseConflictError(
                "stale-claim release claim-set projection has duplicate tasks"
            )
        seen.add(claim_task_id)
        ordered.append(claim_task_id)
    if ordered != sorted(ordered):
        raise StaleClaimReleaseConflictError(
            "stale-claim release claim-set projection is not sorted"
        )


_STALE_TARGET_ATTEMPT_KEYS = frozenset(
    {
        "attempt_id",
        "task_id",
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


def _validate_stale_target_projection(
    projection: Any,
    *,
    workset_id: str,
    task_id: str,
    require_stale_claim: Mapping[str, Any] | None,
) -> None:
    if (
        not isinstance(projection, Mapping)
        or set(projection)
        != {"workset_id", "task_id", "task_states", "task_claims", "attempts"}
        or projection.get("workset_id") != workset_id
        or projection.get("task_id") != task_id
        or not isinstance(projection.get("task_states"), list)
        or not isinstance(projection.get("task_claims"), list)
        or not isinstance(projection.get("attempts"), list)
        or len(projection["task_states"]) > 1
    ):
        raise StaleClaimReleaseConflictError(
            "stale-claim release has an invalid target projection"
        )
    for state in projection["task_states"]:
        if (
            not isinstance(state, Mapping)
            or set(state)
            != {
                "task_id",
                "status",
                "updated_at",
                "actor",
                "note",
                "failure_class",
                "recovery_action",
                "prompt_issue",
                "operator_issue",
            }
            or state.get("task_id") != task_id
            or state.get("status")
            not in {
                TASK_STATUS_PLANNED,
                TASK_STATUS_IN_PROGRESS,
                TASK_STATUS_BLOCKED,
                TASK_STATUS_DONE,
                TASK_STATUS_CANCELED,
            }
            or state.get("updated_at") is not None
            and not _canonical_optional_text(state.get("updated_at"))
            or not _canonical_optional_text(state.get("actor"))
            or not _canonical_optional_text(state.get("note"))
            or state.get("failure_class") is not None
            and state.get("failure_class") not in FAILURE_CLASSES
            or not _canonical_optional_text(state.get("recovery_action"))
            or type(state.get("prompt_issue")) is not bool
            or type(state.get("operator_issue")) is not bool
        ):
            raise StaleClaimReleaseConflictError(
                "stale-claim release target has an invalid task state"
            )
    expected_claims = [] if require_stale_claim is None else [dict(require_stale_claim)]
    if not _canonical_payload_equal(projection["task_claims"], expected_claims):
        raise StaleClaimReleaseConflictError(
            "stale-claim release target has an invalid task claim projection"
        )
    if require_stale_claim is not None:
        _validate_stale_task_claim_payload(require_stale_claim, task_id=task_id)
    seen_attempts: set[str] = set()
    for attempt in projection["attempts"]:
        if (
            not isinstance(attempt, Mapping)
            or set(attempt) != _STALE_TARGET_ATTEMPT_KEYS
            or attempt.get("task_id") != task_id
            or not isinstance(attempt.get("attempt_id"), str)
            or not attempt.get("attempt_id")
            or str(attempt["attempt_id"]).strip() != attempt["attempt_id"]
            or attempt["attempt_id"] in seen_attempts
            or attempt.get("status") == ATTEMPT_STATUS_IN_PROGRESS
            and attempt.get("ended_at") is None
            or type(attempt.get("prompt_issue")) is not bool
            or type(attempt.get("operator_issue")) is not bool
        ):
            raise StaleClaimReleaseConflictError(
                "stale-claim release target does not prove terminal attempt history"
            )
        seen_attempts.add(str(attempt["attempt_id"]))


def _derived_stale_claim_release_post_target(
    pre_target: Mapping[str, Any],
    *,
    workset_id: str,
    task_id: str,
    stale_claim: Mapping[str, Any],
    actor: str,
    released_at: str,
    status: str,
    summary: str,
    failure_details: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    _validate_stale_target_projection(
        pre_target,
        workset_id=workset_id,
        task_id=task_id,
        require_stale_claim=stale_claim,
    )
    task_states = [dict(row) for row in pre_target["task_states"]]
    repaired_record: dict[str, Any] | None = None
    repaired_status: str | None = None
    if task_states and task_states[0]["status"] == TASK_STATUS_IN_PROGRESS:
        repaired_status = (
            TASK_STATUS_CANCELED
            if status == ATTEMPT_STATUS_ABANDONED
            else TASK_STATUS_BLOCKED
        )
        repaired_record = {
            "task_id": task_id,
            "status": repaired_status,
            "updated_at": released_at,
            "actor": actor,
            "note": summary,
            "failure_class": failure_details["failure_class"],
            "recovery_action": failure_details["recovery_action"],
            "prompt_issue": failure_details["prompt_issue"],
            "operator_issue": failure_details["operator_issue"],
        }
        task_states = [dict(repaired_record)]
    post_target = {
        "workset_id": workset_id,
        "task_id": task_id,
        "task_states": task_states,
        "task_claims": [],
        "attempts": [dict(row) for row in pre_target["attempts"]],
    }
    _validate_stale_target_projection(
        post_target,
        workset_id=workset_id,
        task_id=task_id,
        require_stale_claim=None,
    )
    return post_target, repaired_record, repaired_status


def _derived_stale_claim_release_post_claim_set(
    pre_claim_set: Mapping[str, Any],
    *,
    workset_id: str,
    task_id: str,
    stale_claim: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    _validate_stale_claim_set_projection(pre_claim_set, workset_id=workset_id)
    target_claims = [
        dict(row)
        for row in pre_claim_set["task_claims"]
        if row["task_id"] == task_id
    ]
    if not _canonical_payload_equal(target_claims, [dict(stale_claim)]):
        raise StaleClaimReleaseConflictError(
            "stale-claim release claim set does not contain its exact target claim"
        )
    remaining_claims = [
        dict(row)
        for row in pre_claim_set["task_claims"]
        if row["task_id"] != task_id
    ]
    pre_workset_claim = pre_claim_set.get("workset_claim")
    release_workset = pre_workset_claim is not None and not remaining_claims
    post_claim_set = {
        "workset_id": workset_id,
        "workset_claim": (
            None if release_workset else (
                dict(pre_workset_claim)
                if isinstance(pre_workset_claim, Mapping)
                else None
            )
        ),
        "task_claims": remaining_claims,
    }
    _validate_stale_claim_set_projection(post_claim_set, workset_id=workset_id)
    return post_claim_set, release_workset


def _stale_claim_release_failure_details(status: str) -> dict[str, Any]:
    if status not in {
        ATTEMPT_STATUS_BLOCKED,
        ATTEMPT_STATUS_FAILED,
        ATTEMPT_STATUS_ABANDONED,
    }:
        raise BacklogError(
            "stale-claim release status must be one of blocked, failed, abandoned"
        )
    return {
        "failure_class": (
            FAILURE_CLASS_ABANDONED
            if status == ATTEMPT_STATUS_ABANDONED
            else FAILURE_CLASS_UNKNOWN
        ),
        "recovery_action": "release_stale_claim",
        "prompt_issue": False,
        "operator_issue": status == ATTEMPT_STATUS_ABANDONED,
    }


def _stale_claim_release_target_payload(
    runtime_state: RuntimeState,
    *,
    workset_id: str,
    task_id: str,
) -> dict[str, Any]:
    workset_payload = _runtime_workset_payload(runtime_state, workset_id)
    return {
        "workset_id": workset_id,
        "task_id": task_id,
        "task_states": [
            row
            for row in workset_payload.get("task_states", [])
            if row.get("task_id") == task_id
        ],
        "task_claims": [
            row
            for row in workset_payload.get("task_claims", [])
            if row.get("task_id") == task_id
        ],
        "attempts": [
            row
            for row in workset_payload.get("attempts", [])
            if row.get("task_id") == task_id
        ],
    }


def _stale_claim_release_target_identity(
    runtime_state: RuntimeState,
    *,
    workset_id: str,
    task_id: str,
) -> str:
    return _canonical_payload_hash(
        _stale_claim_release_target_payload(
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
        )
    )


def _stale_claim_release_claim_set_payload(
    runtime_state: RuntimeState,
    *,
    workset_id: str,
) -> dict[str, Any]:
    workset_payload = _runtime_workset_payload(runtime_state, workset_id)
    return {
        "workset_id": workset_id,
        "workset_claim": workset_payload.get("workset_claim"),
        "task_claims": workset_payload.get("task_claims", []),
    }


def _stale_claim_release_claim_set_identity(
    runtime_state: RuntimeState,
    *,
    workset_id: str,
) -> str:
    return _canonical_payload_hash(
        _stale_claim_release_claim_set_payload(
            runtime_state,
            workset_id=workset_id,
        )
    )


def _stale_claim_release_request_payload(
    *,
    workset_id: str,
    task_id: str,
    status: str,
    summary: str,
    note: str | None,
    stale_claim: TaskClaimRecord,
    pre_target_identity: str,
    pre_target: Mapping[str, Any],
    pre_claim_set_identity: str,
    failure_details: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": _STALE_CLAIM_RELEASE_REQUEST_SCHEMA_VERSION,
        "workset_id": workset_id,
        "task_id": task_id,
        "actor": stale_claim.actor,
        "status": status,
        "summary": summary,
        "note": note,
        "stale_claim": _stale_claim_payload(stale_claim),
        "active_attempt_id": None,
        "pre_target_identity": pre_target_identity,
        "pre_target": dict(pre_target),
        "pre_claim_set_identity": pre_claim_set_identity,
        **dict(failure_details),
    }


def _stale_claim_release_request_event_id(
    *,
    workset_id: str,
    task_id: str,
    pre_target_identity: str,
    request_semantics_hash: str,
) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                "blackdog.task.stale-claim-release.request/v1",
                workset_id,
                task_id,
                pre_target_identity,
                request_semantics_hash,
            )
        ).encode("utf-8")
    ).hexdigest()


def _stale_claim_release_decision_event_id(
    *,
    request_event_id: str,
    pre_claim_set_identity: str,
) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                "blackdog.task.stale-claim-release.decision/v1",
                request_event_id,
                pre_claim_set_identity,
            )
        ).encode("utf-8")
    ).hexdigest()


def _stale_claim_release_owned_event_id(
    *,
    decision_event_id: str,
    event_type: str,
) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                "blackdog.task.stale-claim-release.owned-event/v1",
                decision_event_id,
                event_type,
            )
        ).encode("utf-8")
    ).hexdigest()


def _load_stale_claim_release_requests(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for event in load_events(profile.paths.events_file):
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("workset_id") != workset_id or payload.get("task_id") != task_id:
            continue
        if event.get("type") != "task.stale-claim-release.request":
            if (
                set(payload) == _STALE_CLAIM_RELEASE_REQUEST_KEYS
                and _is_sha256(event.get("event_id"))
                and _is_sha256(payload.get("pre_target_identity"))
                and event.get("event_id")
                == _stale_claim_release_request_event_id(
                    workset_id=workset_id,
                    task_id=task_id,
                    pre_target_identity=str(payload["pre_target_identity"]),
                    request_semantics_hash=_canonical_payload_hash(payload),
                )
            ):
                raise StaleClaimReleaseConflictError(
                    "stale-claim release request identity has a conflicting event type"
                )
            continue
        if set(payload) != _STALE_CLAIM_RELEASE_REQUEST_KEYS:
            raise StaleClaimReleaseConflictError(
                "stale-claim release request has conflicting fields"
            )
        event_id = event.get("event_id")
        actor = payload.get("actor")
        summary = payload.get("summary")
        note = payload.get("note")
        claim_payload = payload.get("stale_claim")
        try:
            failure_details = _stale_claim_release_failure_details(
                str(payload.get("status") or "")
            )
            _validate_stale_task_claim_payload(claim_payload, task_id=task_id)
            _validate_stale_target_projection(
                payload.get("pre_target"),
                workset_id=workset_id,
                task_id=task_id,
                require_stale_claim=claim_payload,
            )
        except (BacklogError, StaleClaimReleaseConflictError) as exc:
            raise StaleClaimReleaseConflictError(
                "stale-claim release request is not canonical"
            ) from exc
        if (
            type(payload.get("schema_version")) is not int
            or payload.get("schema_version")
            != _STALE_CLAIM_RELEASE_REQUEST_SCHEMA_VERSION
            or not _is_sha256(event_id)
            or event_id in seen_ids
            or not isinstance(actor, str)
            or not actor
            or actor.strip() != actor
            or event.get("actor") != actor
            or not isinstance(summary, str)
            or not summary
            or summary.strip() != summary
            or not _canonical_optional_text(note)
            or payload.get("active_attempt_id") is not None
            or not _is_sha256(payload.get("pre_target_identity"))
            or not _is_sha256(payload.get("pre_claim_set_identity"))
            or claim_payload.get("actor") != actor
            or _canonical_payload_hash(payload["pre_target"])
            != payload["pre_target_identity"]
            or type(payload.get("prompt_issue")) is not bool
            or type(payload.get("operator_issue")) is not bool
            or any(payload.get(key) != value for key, value in failure_details.items())
        ):
            raise StaleClaimReleaseConflictError(
                "stale-claim release request is not canonical"
            )
        expected_id = _stale_claim_release_request_event_id(
            workset_id=workset_id,
            task_id=task_id,
            pre_target_identity=str(payload["pre_target_identity"]),
            request_semantics_hash=_canonical_payload_hash(payload),
        )
        if event_id != expected_id:
            raise StaleClaimReleaseConflictError(
                "stale-claim release request has a conflicting identity"
            )
        seen_ids.add(str(event_id))
        rows.append(
            {"event_id": str(event_id), "actor": actor, "payload": dict(payload)}
        )
    return tuple(rows)


def _load_stale_claim_release_decisions(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
) -> tuple[dict[str, Any], ...]:
    requests = _load_stale_claim_release_requests(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    requests_by_id = {row["event_id"]: row for row in requests}
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for event in load_events(profile.paths.events_file):
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("workset_id") != workset_id or payload.get("task_id") != task_id:
            continue
        if event.get("type") != "task.stale-claim-release.decision":
            if (
                set(payload) == _STALE_CLAIM_RELEASE_DECISION_KEYS
                and _is_sha256(event.get("event_id"))
                and _is_sha256(payload.get("request_event_id"))
                and _is_sha256(payload.get("pre_claim_set_identity"))
                and event.get("event_id")
                == _stale_claim_release_decision_event_id(
                    request_event_id=str(payload["request_event_id"]),
                    pre_claim_set_identity=str(payload["pre_claim_set_identity"]),
                )
            ):
                raise StaleClaimReleaseConflictError(
                    "stale-claim release decision identity has a conflicting event type"
                )
            continue
        if set(payload) != _STALE_CLAIM_RELEASE_DECISION_KEYS:
            raise StaleClaimReleaseConflictError(
                "stale-claim release decision has conflicting fields"
            )
        actor = payload.get("actor")
        if (
            type(payload.get("schema_version")) is not int
            or payload.get("schema_version")
            != _STALE_CLAIM_RELEASE_DECISION_SCHEMA_VERSION
            or not isinstance(actor, str)
            or not actor
            or actor.strip() != actor
            or event.get("actor") != actor
            or not isinstance(payload.get("release_workset_claim"), bool)
            or not isinstance(payload.get("released_at"), str)
            or parse_iso(str(payload.get("released_at"))) is None
        ):
            raise StaleClaimReleaseConflictError(
                "stale-claim release decision is not canonical"
            )
        for key in (
            "request_event_id",
            "request_semantics_hash",
            "pre_target_identity",
            "expected_post_target_identity",
            "pre_claim_set_identity",
            "expected_post_claim_set_identity",
            "pre_runtime_workset_hash",
            "expected_post_runtime_workset_hash",
            "task_release_event_id",
        ):
            if not _is_sha256(payload.get(key)):
                raise StaleClaimReleaseConflictError(
                    f"stale-claim release decision has an invalid {key}"
                )
        expected_id = _stale_claim_release_decision_event_id(
            request_event_id=str(payload["request_event_id"]),
            pre_claim_set_identity=str(payload["pre_claim_set_identity"]),
        )
        if event.get("event_id") != expected_id or expected_id in seen_ids:
            raise StaleClaimReleaseConflictError(
                "stale-claim release decision has a conflicting identity"
            )
        request = requests_by_id.get(str(payload["request_event_id"]))
        if request is None:
            raise StaleClaimReleaseConflictError(
                "stale-claim release decision has no durable request"
            )
        request_payload = request["payload"]
        released_at = str(payload["released_at"])
        pre_target = payload.get("pre_target")
        post_target = payload.get("expected_post_target")
        pre_claim_set = payload.get("pre_claim_set")
        post_claim_set = payload.get("expected_post_claim_set")
        try:
            if not isinstance(pre_target, Mapping):
                raise StaleClaimReleaseConflictError(
                    "stale-claim release decision is missing its pre-target projection"
                )
            if not isinstance(pre_claim_set, Mapping):
                raise StaleClaimReleaseConflictError(
                    "stale-claim release decision is missing its pre-claim projection"
                )
            expected_post_target, expected_repaired_record, expected_repaired_status = (
                _derived_stale_claim_release_post_target(
                    pre_target,
                    workset_id=workset_id,
                    task_id=task_id,
                    stale_claim=request_payload["stale_claim"],
                    actor=str(request_payload["actor"]),
                    released_at=released_at,
                    status=str(request_payload["status"]),
                    summary=str(request_payload["summary"]),
                    failure_details={
                        key: request_payload[key]
                        for key in (
                            "failure_class",
                            "recovery_action",
                            "prompt_issue",
                            "operator_issue",
                        )
                    },
                )
            )
            expected_post_claim_set, expected_release_workset = (
                _derived_stale_claim_release_post_claim_set(
                    pre_claim_set,
                    workset_id=workset_id,
                    task_id=task_id,
                    stale_claim=request_payload["stale_claim"],
                )
            )
            _validate_stale_target_projection(
                post_target,
                workset_id=workset_id,
                task_id=task_id,
                require_stale_claim=None,
            )
            _validate_stale_claim_set_projection(
                post_claim_set,
                workset_id=workset_id,
            )
        except StaleClaimReleaseConflictError as exc:
            raise StaleClaimReleaseConflictError(
                "stale-claim release decision has invalid projections"
            ) from exc
        repaired_status = payload.get("repaired_runtime_status")
        repaired_record = payload.get("repaired_task_record")
        if (
            repaired_status != expected_repaired_status
            or not _canonical_payload_equal(
                repaired_record, expected_repaired_record
            )
            or not _canonical_payload_equal(
                pre_target, request_payload["pre_target"]
            )
            or _canonical_payload_hash(pre_target)
            != payload["pre_target_identity"]
            or not _canonical_payload_equal(post_target, expected_post_target)
            or _canonical_payload_hash(post_target)
            != payload["expected_post_target_identity"]
            or _canonical_payload_hash(pre_claim_set)
            != payload["pre_claim_set_identity"]
            or not _canonical_payload_equal(
                post_claim_set, expected_post_claim_set
            )
            or _canonical_payload_hash(post_claim_set)
            != payload["expected_post_claim_set_identity"]
            or payload["release_workset_claim"] is not expected_release_workset
            or not _canonical_payload_equal(
                payload.get("pre_workset_claim"),
                pre_claim_set.get("workset_claim"),
            )
            or not _canonical_payload_equal(
                payload.get("expected_post_workset_claim"),
                post_claim_set.get("workset_claim"),
            )
        ):
            raise StaleClaimReleaseConflictError(
                "stale-claim release decision conflicts with its derived mutation"
            )
        task_event_id = _stale_claim_release_owned_event_id(
            decision_event_id=expected_id,
            event_type="task.release",
        )
        expected_task_payload = {
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": request_payload["stale_claim"]["attempt_id"],
            "released_at": released_at,
            "status": request_payload["status"],
            "summary": request_payload["summary"],
            "note": request_payload["note"],
            "recovery": "stale_claim",
            "repaired_runtime_status": repaired_status,
            "failure_class": request_payload["failure_class"],
            "recovery_action": request_payload["recovery_action"],
            "prompt_issue": request_payload["prompt_issue"],
            "operator_issue": request_payload["operator_issue"],
            "stale_claim_release_request_event_id": request["event_id"],
            "stale_claim_release_decision_event_id": expected_id,
        }
        release_workset = payload["release_workset_claim"]
        expected_workset_id = (
            _stale_claim_release_owned_event_id(
                decision_event_id=expected_id,
                event_type="workset.release",
            )
            if release_workset
            else None
        )
        expected_workset_payload = (
            {
                "workset_id": workset_id,
                "released_at": released_at,
                "status": request_payload["status"],
                "summary": request_payload["summary"],
                "note": request_payload["note"],
                "recovery": "stale_claim",
                "failure_class": request_payload["failure_class"],
                "recovery_action": request_payload["recovery_action"],
                "prompt_issue": request_payload["prompt_issue"],
                "operator_issue": request_payload["operator_issue"],
                "stale_claim_release_request_event_id": request["event_id"],
                "stale_claim_release_decision_event_id": expected_id,
            }
            if release_workset
            else None
        )
        if (
            payload["request_semantics_hash"]
            != _canonical_payload_hash(request_payload)
            or payload["actor"] != request_payload["actor"]
            or payload["pre_target_identity"]
            != request_payload["pre_target_identity"]
            or payload["pre_claim_set_identity"]
            != request_payload["pre_claim_set_identity"]
            or payload["task_release_event_id"] != task_event_id
            or not _canonical_payload_equal(
                payload["task_release_event_payload"], expected_task_payload
            )
            or payload.get("workset_release_event_id") != expected_workset_id
            or not _canonical_payload_equal(
                payload.get("workset_release_event_payload"),
                expected_workset_payload,
            )
        ):
            raise StaleClaimReleaseConflictError(
                "stale-claim release decision conflicts with its durable request"
            )
        seen_ids.add(expected_id)
        rows.append({"event_id": expected_id, "payload": dict(payload)})
    return tuple(rows)


def _validate_stale_claim_release_owned_events(
    profile: RepoProfile,
    *,
    decision: Mapping[str, Any],
) -> tuple[bool, bool]:
    events = load_events(profile.paths.events_file)
    decision_event_id = _stale_claim_release_decision_event_id(
        request_event_id=str(decision["request_event_id"]),
        pre_claim_set_identity=str(decision["pre_claim_set_identity"]),
    )

    def validate(event_id: str, event_type: str, payload: Mapping[str, Any]) -> bool:
        matches = []
        for event in events:
            event_payload = event.get("payload")
            claims_generation = (
                isinstance(event_payload, Mapping)
                and event_payload.get(
                    "stale_claim_release_decision_event_id"
                )
                == decision_event_id
                and (
                    event_payload.get("task_id") == decision["task_id"]
                    if event_type == "task.release"
                    else "task_id" not in event_payload
                )
            )
            if event.get("event_id") == event_id or claims_generation:
                matches.append(event)
        if len(matches) > 1:
            raise StaleClaimReleaseConflictError(
                f"stale-claim release owned {event_type} occurs more than once"
            )
        if not matches:
            return False
        event = matches[0]
        if (
            event.get("event_id") != event_id
            or event.get("type") != event_type
            or event.get("actor") != decision["actor"]
            or not isinstance(event.get("payload"), Mapping)
            or not _canonical_payload_equal(event["payload"], payload)
        ):
            raise StaleClaimReleaseConflictError(
                f"stale-claim release owned {event_type} conflicts with its decision"
            )
        return True

    task_durable = validate(
        str(decision["task_release_event_id"]),
        "task.release",
        decision["task_release_event_payload"],
    )
    workset_id = decision.get("workset_release_event_id")
    workset_payload = decision.get("workset_release_event_payload")
    if workset_id is None:
        if workset_payload is not None or decision.get("release_workset_claim") is not False:
            raise StaleClaimReleaseConflictError(
                "stale-claim release decision has invalid workset event ownership"
            )
        forbidden_event_id = _stale_claim_release_owned_event_id(
            decision_event_id=decision_event_id,
            event_type="workset.release",
        )
        if any(event.get("event_id") == forbidden_event_id for event in events):
            raise StaleClaimReleaseConflictError(
                "stale-claim release owns an unexpected workset.release row"
            )
        return task_durable, True
    if (
        decision.get("release_workset_claim") is not True
        or not _is_sha256(workset_id)
        or not isinstance(workset_payload, Mapping)
    ):
        raise StaleClaimReleaseConflictError(
            "stale-claim release decision has invalid workset event ownership"
        )
    return task_durable, validate(
        str(workset_id),
        "workset.release",
        workset_payload,
    )


def _stale_claim_release_runtime_side(
    runtime_state: RuntimeState,
    *,
    workset_id: str,
    task_id: str,
    decision: Mapping[str, Any],
) -> str | None:
    target_identity = _stale_claim_release_target_identity(
        runtime_state,
        workset_id=workset_id,
        task_id=task_id,
    )
    claim_identity = _stale_claim_release_claim_set_identity(
        runtime_state,
        workset_id=workset_id,
    )
    if (
        target_identity == decision["expected_post_target_identity"]
        and claim_identity == decision["expected_post_claim_set_identity"]
    ):
        return "post"
    if (
        target_identity == decision["pre_target_identity"]
        and claim_identity == decision["pre_claim_set_identity"]
    ):
        return "pre"
    return None


def _stale_claim_release_task_ids(
    profile: RepoProfile,
    *,
    workset_id: str,
) -> tuple[str, ...]:
    task_ids: set[str] = set()
    for event in load_events(profile.paths.events_file):
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("workset_id") != workset_id:
            continue
        is_request_type = event.get("type") == "task.stale-claim-release.request"
        is_request_identity = (
            set(payload) == _STALE_CLAIM_RELEASE_REQUEST_KEYS
            and _is_sha256(event.get("event_id"))
            and _is_sha256(payload.get("pre_target_identity"))
            and isinstance(payload.get("task_id"), str)
            and bool(payload.get("task_id"))
            and event.get("event_id")
            == _stale_claim_release_request_event_id(
                workset_id=workset_id,
                task_id=str(payload["task_id"]),
                pre_target_identity=str(payload["pre_target_identity"]),
                request_semantics_hash=_canonical_payload_hash(payload),
            )
        )
        is_decision_type = event.get("type") == "task.stale-claim-release.decision"
        is_decision_identity = (
            set(payload) == _STALE_CLAIM_RELEASE_DECISION_KEYS
            and _is_sha256(event.get("event_id"))
            and _is_sha256(payload.get("request_event_id"))
            and _is_sha256(payload.get("pre_claim_set_identity"))
            and isinstance(payload.get("task_id"), str)
            and bool(payload.get("task_id"))
            and event.get("event_id")
            == _stale_claim_release_decision_event_id(
                request_event_id=str(payload["request_event_id"]),
                pre_claim_set_identity=str(payload["pre_claim_set_identity"]),
            )
        )
        if not (
            is_request_type
            or is_request_identity
            or is_decision_type
            or is_decision_identity
        ):
            continue
        task_id = payload.get("task_id")
        if (
            not isinstance(task_id, str)
            or not task_id
            or task_id.strip() != task_id
        ):
            raise StaleClaimReleaseConflictError(
                "stale-claim release ledger has an invalid target task"
            )
        task_ids.add(task_id)
    return tuple(sorted(task_ids))


def pending_stale_claim_release(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: RuntimeState | None = None,
) -> dict[str, Any] | None:
    current = runtime_state or load_runtime_state(profile.paths)
    requests = _load_stale_claim_release_requests(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    decisions = _load_stale_claim_release_decisions(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    if not requests:
        return None
    decisions_by_request: dict[str, list[dict[str, Any]]] = {}
    for decision in decisions:
        decisions_by_request.setdefault(
            str(decision["payload"]["request_event_id"]), []
        ).append(decision)
    incomplete: list[
        tuple[
            int,
            dict[str, Any],
            dict[str, Any] | None,
            bool,
            bool,
        ]
    ] = []
    duplicate_request_ids: list[str] = []
    for index, request in enumerate(requests):
        related = decisions_by_request.get(request["event_id"], [])
        if len(related) > 1:
            duplicate_request_ids.append(str(request["event_id"]))
            continue
        decision = related[0] if related else None
        if decision is None:
            incomplete.append((index, request, None, False, False))
            continue
        task_durable, workset_durable = _validate_stale_claim_release_owned_events(
            profile,
            decision=decision["payload"],
        )
        if not (task_durable and workset_durable):
            incomplete.append(
                (index, request, decision, task_durable, workset_durable)
            )
    if duplicate_request_ids or len(incomplete) > 1 or (
        incomplete and incomplete[0][0] != len(requests) - 1
    ):
        if incomplete:
            _index, request, decision, task_durable, workset_durable = incomplete[0]
        else:
            request = next(
                row
                for row in requests
                if row["event_id"] == duplicate_request_ids[0]
            )
            decision = None
            task_durable = False
            workset_durable = False
        return {
            "stage": "ledger_conflict",
            "mutation_phase": "preflight",
            "request_event_id": request["event_id"],
            "decision_event_id": decision["event_id"] if decision else None,
            "task_release_event_id": (
                decision["payload"]["task_release_event_id"] if decision else None
            ),
            "workset_release_event_id": (
                decision["payload"].get("workset_release_event_id")
                if decision
                else None
            ),
            "task_release_event_durable": task_durable,
            "workset_release_event_durable": workset_durable,
            "request": dict(request["payload"]),
        }
    if not incomplete:
        return None
    _index, request, decision, task_durable, workset_durable = incomplete[0]
    if decision is None:
        request_payload = request["payload"]
        pre_matches = (
            _stale_claim_release_target_identity(
                current,
                workset_id=workset_id,
                task_id=task_id,
            )
            == request_payload["pre_target_identity"]
            and _stale_claim_release_claim_set_identity(
                current,
                workset_id=workset_id,
            )
            == request_payload["pre_claim_set_identity"]
        )
        return {
            "stage": "request_recorded" if pre_matches else "runtime_conflict",
            "mutation_phase": "preflight",
            "request_event_id": request["event_id"],
            "decision_event_id": None,
            "task_release_event_id": None,
            "workset_release_event_id": None,
            "task_release_event_durable": False,
            "workset_release_event_durable": False,
            "request": dict(request_payload),
        }
    side = _stale_claim_release_runtime_side(
        current,
        workset_id=workset_id,
        task_id=task_id,
        decision=decision["payload"],
    )
    if side == "pre":
        stage = "decision_recorded"
        phase = "preflight"
    elif side == "post" and task_durable:
        stage = "task_event_recorded"
        phase = "event_finalization_partial"
    elif side == "post":
        stage = "runtime_recorded"
        phase = "runtime_finalized"
    else:
        stage = "runtime_conflict"
        phase = "preflight"
    return {
        "stage": stage,
        "mutation_phase": phase,
        "request_event_id": request["event_id"],
        "decision_event_id": decision["event_id"],
        "task_release_event_id": decision["payload"]["task_release_event_id"],
        "workset_release_event_id": decision["payload"].get(
            "workset_release_event_id"
        ),
        "task_release_event_durable": task_durable,
        "workset_release_event_durable": workset_durable,
        "request": dict(request["payload"]),
    }


def pending_stale_claim_release_for_workset(
    profile: RepoProfile,
    *,
    workset_id: str,
    runtime_state: RuntimeState | None = None,
) -> dict[str, Any] | None:
    """Return the one incomplete stale-claim release reserving a workset.

    A stale-claim decision owns the workset claim-set projection, so callers
    that may add or remove a claim must gate on the owning task even when they
    are operating on a sibling task.  The release transaction prevents a
    second generation from being reserved while one is incomplete.  Treat
    multiple incomplete generations as conflicting durable evidence rather
    than choosing one arbitrarily.
    """

    current = runtime_state or load_runtime_state(profile.paths)
    pending_rows: list[dict[str, Any]] = []
    for owner_task_id in _stale_claim_release_task_ids(
        profile,
        workset_id=workset_id,
    ):
        pending = pending_stale_claim_release(
            profile,
            workset_id=workset_id,
            task_id=owner_task_id,
            runtime_state=current,
        )
        if pending is not None:
            pending_rows.append(
                {
                    "owner_task_id": owner_task_id,
                    "release": pending,
                }
            )
    if len(pending_rows) > 1:
        owners = ", ".join(
            repr(row["owner_task_id"]) for row in pending_rows
        )
        raise StaleClaimReleaseConflictError(
            f"Workset {workset_id!r} has multiple incomplete stale-claim "
            f"releases owned by {owners}"
        )
    return pending_rows[0] if pending_rows else None


def require_no_pending_stale_claim_release(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: RuntimeState,
) -> None:
    pending = pending_stale_claim_release(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        runtime_state=runtime_state,
    )
    if pending is not None:
        raise StaleClaimReleaseConflictError(
            f"Task {task_id!r} has an incomplete stale-claim release; "
            "retry its exact durable request before another mutation"
        )


def require_no_pending_stale_claim_release_for_workset(
    profile: RepoProfile,
    *,
    workset_id: str,
    runtime_state: RuntimeState,
    except_task_id: str | None = None,
) -> None:
    pending = pending_stale_claim_release_for_workset(
        profile,
        workset_id=workset_id,
        runtime_state=runtime_state,
    )
    if pending is None or pending["owner_task_id"] == except_task_id:
        return
    owner_task_id = str(pending["owner_task_id"])
    raise StaleClaimReleaseConflictError(
        f"Task {owner_task_id!r} has an incomplete stale-claim release; "
        "retry its exact durable request before another claim-set mutation"
    )


def _next_stale_claim_release_at(
    decisions: tuple[dict[str, Any], ...],
) -> str:
    candidate = now_iso()
    used = {str(row["payload"]["released_at"]) for row in decisions}
    while candidate in used:
        parsed = parse_iso(candidate)
        if parsed is None:
            raise StaleClaimReleaseConflictError(
                "stale-claim release timestamp is invalid"
            )
        candidate = (parsed + timedelta(seconds=1)).isoformat(timespec="seconds")
    return candidate


def _stale_claim_release_evidence(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    decision: Mapping[str, Any] | None,
    request_event_id: str | None,
    decision_event_id: str | None,
    task_release_event_id: str | None,
    workset_release_event_id: str | None,
) -> tuple[bool, str]:
    requests = _load_stale_claim_release_requests(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    decisions = _load_stale_claim_release_decisions(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    durable_request = next(
        (
            row
            for row in requests
            if request_event_id is not None and row["event_id"] == request_event_id
        ),
        None,
    )
    durable_decision = next(
        (
            row
            for row in decisions
            if decision_event_id is not None and row["event_id"] == decision_event_id
        ),
        None,
    )
    if durable_decision is not None and (
        decision is None
        or not _canonical_payload_equal(durable_decision["payload"], decision)
    ):
        raise StaleClaimReleaseConflictError(
            "stale-claim release durable decision conflicts with in-memory evidence"
        )
    task_durable = False
    workset_durable = workset_release_event_id is None
    if decision is not None:
        task_durable, workset_durable = _validate_stale_claim_release_owned_events(
            profile,
            decision=decision,
        )
        if task_release_event_id != decision.get("task_release_event_id") or (
            workset_release_event_id != decision.get("workset_release_event_id")
        ):
            raise StaleClaimReleaseConflictError(
                "stale-claim release evidence names foreign owned events"
            )
    if task_durable and workset_durable:
        return True, "event_finalized"
    if task_durable:
        return True, "event_finalization_partial"
    if decision is not None:
        runtime = load_runtime_state(profile.paths)
        if (
            _stale_claim_release_runtime_side(
                runtime,
                workset_id=workset_id,
                task_id=task_id,
                decision=decision,
            )
            == "post"
        ):
            return True, "runtime_finalized"
    if durable_request is not None or durable_decision is not None:
        return True, "preflight"
    return False, "none"


def release_stale_task_claim(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    status: str,
    summary: str,
    note: str | None = None,
    planning_store: PlanningStore | None = None,
    runtime_store: RuntimeStore | None = None,
    expected_request_event_id: str | None = None,
    expected_decision_event_id: str | None = None,
) -> StaleClaimReleaseResult:
    resolved_summary = str(summary or "").strip()
    if not resolved_summary:
        raise BacklogError("stale-claim release requires a nonempty summary")
    resolved_note = str(note or "").strip() or None
    failure_details = _stale_claim_release_failure_details(status)
    planning = load_planning_state(profile.paths, planning_store)
    workset, _task = _require_workset_and_task(
        planning,
        workset_id=workset_id,
        task_id=task_id,
    )
    guarded_request_id = str(expected_request_event_id or "").strip() or None
    guarded_decision_id = str(expected_decision_event_id or "").strip() or None
    if guarded_decision_id is not None and guarded_request_id is None:
        raise BacklogError(
            "stale-claim release decision guard requires its request guard"
        )
    if guarded_request_id is not None and not _is_sha256(guarded_request_id):
        raise StaleClaimReleaseConflictError(
            "stale-claim release request guard is not a durable identity"
        )
    if guarded_decision_id is not None and not _is_sha256(guarded_decision_id):
        raise StaleClaimReleaseConflictError(
            "stale-claim release decision guard is not a durable identity"
        )

    stale_claim: TaskClaimRecord | None = None
    released_at: str | None = None
    repaired_runtime_status: str | None = None
    release_workset_claim = False
    chosen_decision: dict[str, Any] | None = None
    request_event_id: str | None = None
    decision_event_id: str | None = None
    task_release_event_id: str | None = None
    workset_release_event_id: str | None = None
    runtime_changed = False
    request_event_appended = False
    decision_event_appended = False
    task_release_event_appended = False
    workset_release_event_appended = False

    def request_matches(request: Mapping[str, Any]) -> bool:
        return (
            request.get("workset_id") == workset_id
            and request.get("task_id") == task_id
            and request.get("status") == status
            and request.get("summary") == resolved_summary
            and request.get("note") == resolved_note
            and all(
                request.get(key) == value
                for key, value in failure_details.items()
            )
        )

    def request_for(
        claim: TaskClaimRecord,
        *,
        pre_target_identity: str,
        pre_target: Mapping[str, Any],
        pre_claim_set_identity: str,
    ) -> dict[str, Any]:
        return _stale_claim_release_request_payload(
            workset_id=workset_id,
            task_id=task_id,
            status=status,
            summary=resolved_summary,
            note=resolved_note,
            stale_claim=claim,
            pre_target_identity=pre_target_identity,
            pre_target=pre_target,
            pre_claim_set_identity=pre_claim_set_identity,
            failure_details=failure_details,
        )

    def claim_from_request(request: Mapping[str, Any]) -> TaskClaimRecord:
        claim_payload = request.get("stale_claim")
        if not isinstance(claim_payload, Mapping):
            raise StaleClaimReleaseConflictError(
                "stale-claim release request is missing its claim"
            )
        try:
            return TaskClaimRecord(**dict(claim_payload))
        except TypeError as exc:
            raise StaleClaimReleaseConflictError(
                "stale-claim release request has an invalid claim"
            ) from exc

    def mutate(runtime_state: RuntimeState) -> RuntimeState:
        nonlocal stale_claim, released_at, repaired_runtime_status
        nonlocal release_workset_claim, chosen_decision
        nonlocal request_event_id, decision_event_id
        nonlocal task_release_event_id, workset_release_event_id
        nonlocal runtime_changed, request_event_appended, decision_event_appended

        current_target = _stale_claim_release_target_payload(
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
        )
        current_target_identity = _canonical_payload_hash(current_target)
        current_claim_set = _stale_claim_release_claim_set_payload(
            runtime_state,
            workset_id=workset_id,
        )
        current_claim_set_identity = _canonical_payload_hash(current_claim_set)
        current_claim = task_claim_index(runtime_state, workset_id).get(task_id)
        current_active = active_task_attempt(runtime_state, workset_id, task_id)
        _task_scoped_runtime_task_ids(
            profile,
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            planning_store=planning_store,
        )
        pending = pending_stale_claim_release(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
        )
        requests = _load_stale_claim_release_requests(
            profile,
            workset_id=workset_id,
            task_id=task_id,
        )
        decisions = _load_stale_claim_release_decisions(
            profile,
            workset_id=workset_id,
            task_id=task_id,
        )

        if guarded_request_id is not None:
            guarded_index = next(
                (
                    index
                    for index, row in enumerate(requests)
                    if row["event_id"] == guarded_request_id
                ),
                None,
            )
            if guarded_index is None:
                if current_claim is None or current_active is not None:
                    raise StaleClaimReleaseConflictError(
                        "guarded stale-claim release request is no longer reservable"
                    )
                derived_request = request_for(
                    current_claim,
                    pre_target_identity=current_target_identity,
                    pre_target=current_target,
                    pre_claim_set_identity=current_claim_set_identity,
                )
                derived_id = _stale_claim_release_request_event_id(
                    workset_id=workset_id,
                    task_id=task_id,
                    pre_target_identity=current_target_identity,
                    request_semantics_hash=_canonical_payload_hash(derived_request),
                )
                if (
                    guarded_decision_id is not None
                    or pending is not None
                    or derived_id != guarded_request_id
                ):
                    raise StaleClaimReleaseConflictError(
                        "guarded stale-claim release request is no longer reservable"
                    )
            else:
                guarded_request = requests[guarded_index]
                if not request_matches(guarded_request["payload"]):
                    raise StaleClaimReleaseConflictError(
                        "guarded stale-claim release semantics conflict with its request"
                    )
                if guarded_index != len(requests) - 1:
                    raise StaleClaimReleaseConflictError(
                        "guarded stale-claim release was superseded"
                    )
                guarded_decisions = [
                    row
                    for row in decisions
                    if row["payload"]["request_event_id"] == guarded_request_id
                ]
                if len(guarded_decisions) > 1:
                    raise StaleClaimReleaseConflictError(
                        "guarded stale-claim release has conflicting decisions"
                    )
                guarded_decision = (
                    guarded_decisions[0] if guarded_decisions else None
                )
                if guarded_decision_id is not None and (
                    guarded_decision is None
                    or guarded_decision["event_id"] != guarded_decision_id
                ):
                    raise StaleClaimReleaseConflictError(
                        "guarded stale-claim release decision no longer matches"
                    )
                if guarded_decision is not None:
                    task_durable, workset_durable = (
                        _validate_stale_claim_release_owned_events(
                            profile,
                            decision=guarded_decision["payload"],
                        )
                    )
                    if task_durable and workset_durable:
                        if (
                            _stale_claim_release_runtime_side(
                                runtime_state,
                                workset_id=workset_id,
                                task_id=task_id,
                                decision=guarded_decision["payload"],
                            )
                            != "post"
                        ):
                            raise StaleClaimReleaseConflictError(
                                "guarded stale-claim release completed before later progress"
                            )
                    elif (
                        pending is None
                        or pending["stage"]
                        in {"runtime_conflict", "ledger_conflict"}
                        or pending["request_event_id"] != guarded_request_id
                        or guarded_decision_id is not None
                        and pending["decision_event_id"] != guarded_decision_id
                    ):
                        raise StaleClaimReleaseConflictError(
                            "guarded stale-claim release is no longer repairable"
                        )

        if pending is not None and (
            pending["stage"] in {"runtime_conflict", "ledger_conflict"}
            or not request_matches(pending["request"])
        ):
            raise StaleClaimReleaseConflictError(
                "stale-claim release is reserved by different durable semantics"
            )
        require_no_pending_stale_claim_release_for_workset(
            profile,
            workset_id=workset_id,
            runtime_state=runtime_state,
            except_task_id=task_id,
        )
        try:
            require_no_pending_task_runtime_transition(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                runtime_state=runtime_state,
            )
        except BacklogError as exc:
            raise StaleClaimReleaseConflictError(str(exc)) from exc

        post_candidates = [
            row
            for row in decisions
            if _stale_claim_release_runtime_side(
                runtime_state,
                workset_id=workset_id,
                task_id=task_id,
                decision=row["payload"],
            )
            == "post"
        ]
        exact_post = [
            row
            for row in post_candidates
            if request_matches(
                next(
                    request["payload"]
                    for request in requests
                    if request["event_id"]
                    == row["payload"]["request_event_id"]
                )
            )
        ]
        pre_candidates = [
            row
            for row in decisions
            if _stale_claim_release_runtime_side(
                runtime_state,
                workset_id=workset_id,
                task_id=task_id,
                decision=row["payload"],
            )
            == "pre"
        ]
        exact_pre = [
            row
            for row in pre_candidates
            if request_matches(
                next(
                    request["payload"]
                    for request in requests
                    if request["event_id"]
                    == row["payload"]["request_event_id"]
                )
            )
        ]
        if (post_candidates and len(exact_post) != 1) or (
            not exact_post and pre_candidates and len(exact_pre) != 1
        ):
            raise StaleClaimReleaseConflictError(
                "stale-claim release runtime state is reserved by different semantics"
            )
        selected = exact_post[0] if exact_post else exact_pre[0] if exact_pre else None

        if selected is not None:
            decision = selected["payload"]
            if guarded_request_id is not None and (
                decision["request_event_id"] != guarded_request_id
                or guarded_decision_id is not None
                and selected["event_id"] != guarded_decision_id
            ):
                raise StaleClaimReleaseConflictError(
                    "guarded stale-claim release selected a different generation"
                )
            request = next(
                row
                for row in requests
                if row["event_id"] == decision["request_event_id"]
            )
            stale_claim = claim_from_request(request["payload"])
            released_at = str(decision["released_at"])
            repaired_runtime_status = decision.get("repaired_runtime_status")
            release_workset_claim = bool(decision["release_workset_claim"])
            request_event_id = str(request["event_id"])
            decision_event_id = str(selected["event_id"])
            task_release_event_id = str(decision["task_release_event_id"])
            workset_release_event_id = decision.get("workset_release_event_id")
            chosen_decision = dict(decision)
            request_event_appended = append_event_once(
                profile.paths.events_file,
                event_id=request_event_id,
                event_type="task.stale-claim-release.request",
                actor=stale_claim.actor,
                payload=request["payload"],
            )
            decision_event_appended = append_event_once(
                profile.paths.events_file,
                event_id=decision_event_id,
                event_type="task.stale-claim-release.decision",
                actor=stale_claim.actor,
                payload=decision,
            )
            _validate_stale_claim_release_owned_events(
                profile,
                decision=decision,
            )
            side = _stale_claim_release_runtime_side(
                runtime_state,
                workset_id=workset_id,
                task_id=task_id,
                decision=decision,
            )
            if side == "post":
                return runtime_state
            if side != "pre":
                raise StaleClaimReleaseConflictError(
                    "stale-claim release decision conflicts with runtime"
                )
            repaired_payload = decision.get("repaired_task_record")
            incoming_records = (
                (TaskRuntimeRecord(**dict(repaired_payload)),)
                if isinstance(repaired_payload, Mapping)
                else None
            )
            next_state = _merge_task_scoped_runtime(
                profile,
                runtime_state,
                workset_id=workset_id,
                task_id=task_id,
                planning_store=planning_store,
                incoming_records=incoming_records,
                incoming_workset_claim=(
                    None
                    if release_workset_claim
                    else workset_claim(runtime_state, workset_id)
                ),
                released_task_claim_ids=(task_id,),
            )
            if (
                _stale_claim_release_runtime_side(
                    next_state,
                    workset_id=workset_id,
                    task_id=task_id,
                    decision=decision,
                )
                != "post"
            ):
                raise StaleClaimReleaseConflictError(
                    "stale-claim release decision conflicts with runtime mutation"
                )
            if (
                _runtime_workset_hash(runtime_state, workset_id)
                == decision["pre_runtime_workset_hash"]
                and _runtime_workset_hash(next_state, workset_id)
                != decision["expected_post_runtime_workset_hash"]
            ):
                raise StaleClaimReleaseConflictError(
                    "stale-claim release decision conflicts with its original workset mutation"
                )
            runtime_changed = next_state != runtime_state
            return next_state

        if current_active is not None:
            raise StaleClaimReleaseConflictError(
                "stale-claim release requires no active WTAM attempt"
            )
        if current_claim is None:
            raise StaleClaimReleaseConflictError(
                "stale-claim release did not find a stale task claim"
            )
        stale_claim = current_claim
        exact_request = request_for(
            current_claim,
            pre_target_identity=current_target_identity,
            pre_target=current_target,
            pre_claim_set_identity=current_claim_set_identity,
        )
        request_semantics_hash = _canonical_payload_hash(exact_request)
        request_event_id = _stale_claim_release_request_event_id(
            workset_id=workset_id,
            task_id=task_id,
            pre_target_identity=current_target_identity,
            request_semantics_hash=request_semantics_hash,
        )
        if guarded_request_id is not None and request_event_id != guarded_request_id:
            raise StaleClaimReleaseConflictError(
                "guarded stale-claim release would reserve a different generation"
            )
        released_at = _next_stale_claim_release_at(decisions)
        expected_post_target, repaired_payload, repaired_runtime_status = (
            _derived_stale_claim_release_post_target(
                current_target,
                workset_id=workset_id,
                task_id=task_id,
                stale_claim=exact_request["stale_claim"],
                actor=current_claim.actor,
                released_at=released_at,
                status=status,
                summary=resolved_summary,
                failure_details=failure_details,
            )
        )
        repaired_record = (
            TaskRuntimeRecord(**repaired_payload)
            if repaired_payload is not None
            else None
        )
        expected_post_claim_set, release_workset_claim = (
            _derived_stale_claim_release_post_claim_set(
                current_claim_set,
                workset_id=workset_id,
                task_id=task_id,
                stale_claim=exact_request["stale_claim"],
            )
        )
        current_workset_claim = workset_claim(runtime_state, workset_id)
        next_state = _merge_task_scoped_runtime(
            profile,
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            planning_store=planning_store,
            incoming_records=(repaired_record,) if repaired_record else None,
            incoming_workset_claim=(
                None if release_workset_claim else current_workset_claim
            ),
            released_task_claim_ids=(task_id,),
        )
        actual_post_target = _stale_claim_release_target_payload(
            next_state,
            workset_id=workset_id,
            task_id=task_id,
        )
        actual_post_claim_set = _stale_claim_release_claim_set_payload(
            next_state,
            workset_id=workset_id,
        )
        if (
            not _canonical_payload_equal(actual_post_target, expected_post_target)
            or not _canonical_payload_equal(
                actual_post_claim_set, expected_post_claim_set
            )
        ):
            raise StaleClaimReleaseConflictError(
                "stale-claim release mutation differs from its derived projections"
            )
        pre_claim_set_identity = current_claim_set_identity
        post_claim_set_identity = _canonical_payload_hash(expected_post_claim_set)
        decision_event_id = _stale_claim_release_decision_event_id(
            request_event_id=request_event_id,
            pre_claim_set_identity=pre_claim_set_identity,
        )
        if guarded_decision_id is not None and decision_event_id != guarded_decision_id:
            raise StaleClaimReleaseConflictError(
                "guarded stale-claim release would reserve a different decision"
            )
        task_release_event_id = _stale_claim_release_owned_event_id(
            decision_event_id=decision_event_id,
            event_type="task.release",
        )
        workset_release_event_id = (
            _stale_claim_release_owned_event_id(
                decision_event_id=decision_event_id,
                event_type="workset.release",
            )
            if release_workset_claim
            else None
        )
        task_release_payload = {
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": current_claim.attempt_id,
            "released_at": released_at,
            "status": status,
            "summary": resolved_summary,
            "note": resolved_note,
            "recovery": "stale_claim",
            "repaired_runtime_status": repaired_runtime_status,
            **failure_details,
            "stale_claim_release_request_event_id": request_event_id,
            "stale_claim_release_decision_event_id": decision_event_id,
        }
        workset_release_payload = (
            {
                "workset_id": workset_id,
                "released_at": released_at,
                "status": status,
                "summary": resolved_summary,
                "note": resolved_note,
                "recovery": "stale_claim",
                **failure_details,
                "stale_claim_release_request_event_id": request_event_id,
                "stale_claim_release_decision_event_id": decision_event_id,
            }
            if release_workset_claim
            else None
        )
        chosen_decision = {
            "schema_version": _STALE_CLAIM_RELEASE_DECISION_SCHEMA_VERSION,
            "request_event_id": request_event_id,
            "request_semantics_hash": request_semantics_hash,
            "workset_id": workset_id,
            "task_id": task_id,
            "actor": current_claim.actor,
            "released_at": released_at,
            "pre_target_identity": current_target_identity,
            "expected_post_target_identity": _canonical_payload_hash(
                expected_post_target
            ),
            "pre_target": current_target,
            "expected_post_target": expected_post_target,
            "pre_claim_set_identity": pre_claim_set_identity,
            "expected_post_claim_set_identity": post_claim_set_identity,
            "pre_claim_set": current_claim_set,
            "expected_post_claim_set": expected_post_claim_set,
            "pre_runtime_workset_hash": _runtime_workset_hash(
                runtime_state, workset_id
            ),
            "expected_post_runtime_workset_hash": _runtime_workset_hash(
                next_state, workset_id
            ),
            "repaired_task_record": repaired_payload,
            "repaired_runtime_status": repaired_runtime_status,
            "release_workset_claim": release_workset_claim,
            "pre_workset_claim": current_claim_set["workset_claim"],
            "expected_post_workset_claim": expected_post_claim_set[
                "workset_claim"
            ],
            "task_release_event_id": task_release_event_id,
            "task_release_event_payload": task_release_payload,
            "workset_release_event_id": workset_release_event_id,
            "workset_release_event_payload": workset_release_payload,
        }
        _validate_stale_claim_release_owned_events(
            profile,
            decision=chosen_decision,
        )
        request_event_appended = append_event_once(
            profile.paths.events_file,
            event_id=request_event_id,
            event_type="task.stale-claim-release.request",
            actor=current_claim.actor,
            payload=exact_request,
        )
        decision_event_appended = append_event_once(
            profile.paths.events_file,
            event_id=decision_event_id,
            event_type="task.stale-claim-release.decision",
            actor=current_claim.actor,
            payload=chosen_decision,
        )
        runtime_changed = next_state != runtime_state
        return next_state

    def after_save(runtime_state: RuntimeState) -> None:
        nonlocal task_release_event_appended, workset_release_event_appended
        try:
            if chosen_decision is None or stale_claim is None:
                raise StaleClaimReleaseConflictError(
                    "stale-claim release did not choose a durable decision"
                )
            if (
                _stale_claim_release_runtime_side(
                    runtime_state,
                    workset_id=workset_id,
                    task_id=task_id,
                    decision=chosen_decision,
                )
                != "post"
            ):
                raise StaleClaimReleaseConflictError(
                    "stale-claim release decision no longer matches runtime"
                )
            task_release_event_appended = append_event_once(
                profile.paths.events_file,
                event_id=str(chosen_decision["task_release_event_id"]),
                event_type="task.release",
                actor=stale_claim.actor,
                payload=chosen_decision["task_release_event_payload"],
            )
            if chosen_decision.get("workset_release_event_id") is not None:
                workset_release_event_appended = append_event_once(
                    profile.paths.events_file,
                    event_id=str(chosen_decision["workset_release_event_id"]),
                    event_type="workset.release",
                    actor=stale_claim.actor,
                    payload=chosen_decision["workset_release_event_payload"],
                )
        except Exception as exc:
            raise _StaleClaimReleaseUncertainError(
                "stale-claim release stopped during post-save finalization"
            ) from exc

    try:
        mutate_runtime_state(
            profile.paths,
            mutate,
            store=runtime_store,
            after_save=after_save,
            save_unchanged=False,
        )
    except _StaleClaimReleaseUncertainError as exc:
        try:
            mutation_started, mutation_phase = _stale_claim_release_evidence(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                decision=chosen_decision,
                request_event_id=request_event_id,
                decision_event_id=decision_event_id,
                task_release_event_id=task_release_event_id,
                workset_release_event_id=workset_release_event_id,
            )
        except Exception:
            mutation_started, mutation_phase = True, "runtime_finalized"
        raise StaleClaimReleaseFinalizationError(
            f"stale-claim release finalization interrupted: {exc.__cause__ or exc}",
            mutation_started=True,
            mutation_phase=(
                mutation_phase if mutation_started else "runtime_finalized"
            ),
            request_event_id=request_event_id,
            decision_event_id=decision_event_id,
            task_release_event_id=task_release_event_id,
            workset_release_event_id=workset_release_event_id,
        ) from (exc.__cause__ or exc)
    except (BacklogError, StaleClaimReleaseConflictError):
        raise
    except StoreError as exc:
        try:
            mutation_started, mutation_phase = _stale_claim_release_evidence(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                decision=chosen_decision,
                request_event_id=request_event_id,
                decision_event_id=decision_event_id,
                task_release_event_id=task_release_event_id,
                workset_release_event_id=workset_release_event_id,
            )
        except StaleClaimReleaseConflictError:
            raise
        raise StaleClaimReleaseFinalizationError(
            f"stale-claim release finalization interrupted: {exc}",
            mutation_started=mutation_started,
            mutation_phase=mutation_phase,
            request_event_id=request_event_id,
            decision_event_id=decision_event_id,
            task_release_event_id=task_release_event_id,
            workset_release_event_id=workset_release_event_id,
        ) from exc
    except Exception as exc:
        try:
            mutation_started, mutation_phase = _stale_claim_release_evidence(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                decision=chosen_decision,
                request_event_id=request_event_id,
                decision_event_id=decision_event_id,
                task_release_event_id=task_release_event_id,
                workset_release_event_id=workset_release_event_id,
            )
        except Exception:
            mutation_started, mutation_phase = False, "none"
        raise StaleClaimReleaseFinalizationError(
            f"stale-claim release finalization interrupted: {exc}",
            mutation_started=mutation_started,
            mutation_phase=mutation_phase,
            request_event_id=request_event_id,
            decision_event_id=decision_event_id,
            task_release_event_id=task_release_event_id,
            workset_release_event_id=workset_release_event_id,
        ) from exc
    if (
        stale_claim is None
        or released_at is None
        or request_event_id is None
        or decision_event_id is None
        or task_release_event_id is None
    ):
        raise StaleClaimReleaseConflictError(
            "stale-claim release did not finalize its durable identity"
        )
    return StaleClaimReleaseResult(
        stale_claim=stale_claim,
        released_at=released_at,
        status=status,
        summary=resolved_summary,
        note=resolved_note,
        release_workset_claim=release_workset_claim,
        repaired_runtime_status=repaired_runtime_status,
        failure_class=str(failure_details["failure_class"]),
        recovery_action=str(failure_details["recovery_action"]),
        prompt_issue=bool(failure_details["prompt_issue"]),
        operator_issue=bool(failure_details["operator_issue"]),
        request_event_id=request_event_id,
        decision_event_id=decision_event_id,
        task_release_event_id=task_release_event_id,
        workset_release_event_id=workset_release_event_id,
        runtime_changed=runtime_changed,
        request_event_appended=request_event_appended,
        decision_event_appended=decision_event_appended,
        task_release_event_appended=task_release_event_appended,
        workset_release_event_appended=workset_release_event_appended,
    )


_TASK_RUNTIME_TRANSITION_REQUEST_SCHEMA_VERSION = 1
_TASK_RUNTIME_TRANSITION_DECISION_SCHEMA_VERSION = 1
_TASK_RUNTIME_TRANSITION_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "workset_id",
        "task_id",
        "actor",
        "status",
        "summary",
        "previous_status",
        "failure_class",
        "recovery_action",
        "prompt_issue",
        "operator_issue",
    }
)
_TASK_RUNTIME_TRANSITION_DECISION_KEYS = frozenset(
    {
        "schema_version",
        "request_event_id",
        "request_semantics_hash",
        "workset_id",
        "task_id",
        "actor",
        "event_type",
        "previous_status",
        "target_status",
        "pre_runtime_workset_hash",
        "expected_pre_runtime_identity",
        "updated_at",
        "target_record",
        "expected_post_runtime_identity",
        "expected_post_runtime_workset_hash",
        "owned_event_id",
        "owned_event_payload",
    }
)


def _task_runtime_transition_identity(
    runtime_state: RuntimeState,
    *,
    workset_id: str,
    task_id: str,
) -> str:
    workset_payload = _runtime_workset_payload(runtime_state, workset_id)
    task_states = [
        row
        for row in workset_payload.get("task_states", [])
        if row.get("task_id") == task_id
    ]
    task_claims = [
        row
        for row in workset_payload.get("task_claims", [])
        if row.get("task_id") == task_id
    ]
    return _canonical_payload_hash(
        {
            "workset_id": workset_id,
            "task_id": task_id,
            "task_states": task_states,
            "task_claims": task_claims,
        }
    )


def _task_runtime_transition_request_event_id(
    *,
    workset_id: str,
    task_id: str,
    expected_pre_runtime_identity: str,
    request_semantics_hash: str,
) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                "blackdog.task.runtime-transition.request/v1",
                workset_id,
                task_id,
                expected_pre_runtime_identity,
                request_semantics_hash,
            )
        ).encode("utf-8")
    ).hexdigest()


def _task_runtime_transition_decision_event_id(
    *,
    request_event_id: str,
    pre_runtime_workset_hash: str,
) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                "blackdog.task.runtime-transition.decision/v1",
                request_event_id,
                pre_runtime_workset_hash,
            )
        ).encode("utf-8")
    ).hexdigest()


def _task_runtime_transition_owned_event_id(*, decision_event_id: str) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                "blackdog.task.runtime-transition.owned-event/v1",
                decision_event_id,
            )
        ).encode("utf-8")
    ).hexdigest()


def _task_runtime_record_payload(record: TaskRuntimeRecord) -> dict[str, Any]:
    return {
        "task_id": record.task_id,
        "status": record.status,
        "updated_at": record.updated_at,
        "actor": record.actor,
        "note": record.note,
        "failure_class": record.failure_class,
        "recovery_action": record.recovery_action,
        "prompt_issue": record.prompt_issue,
        "operator_issue": record.operator_issue,
    }


def _task_runtime_transition_request_payload(
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    status: str,
    summary: str | None,
    previous_status: str,
    failure_class: str | None,
    recovery_action: str | None,
    prompt_issue: bool,
    operator_issue: bool,
) -> dict[str, Any]:
    return {
        "schema_version": _TASK_RUNTIME_TRANSITION_REQUEST_SCHEMA_VERSION,
        "workset_id": workset_id,
        "task_id": task_id,
        "actor": actor,
        "status": status,
        "summary": summary,
        "previous_status": previous_status,
        "failure_class": failure_class,
        "recovery_action": recovery_action,
        "prompt_issue": prompt_issue,
        "operator_issue": operator_issue,
    }


def _load_task_runtime_transition_requests(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
) -> tuple[dict[str, Any], ...]:
    requests: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for event in load_events(profile.paths.events_file):
        if event.get("type") != "task.runtime-transition.request":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("workset_id") != workset_id or payload.get("task_id") != task_id:
            continue
        if set(payload) != _TASK_RUNTIME_TRANSITION_REQUEST_KEYS:
            raise BacklogError("task runtime transition request has conflicting fields")
        event_id = event.get("event_id")
        actor = payload.get("actor")
        status = payload.get("status")
        summary = payload.get("summary")
        previous_status = payload.get("previous_status")
        if (
            payload.get("schema_version")
            != _TASK_RUNTIME_TRANSITION_REQUEST_SCHEMA_VERSION
            or not _is_sha256(event_id)
            or event_id in seen_ids
            or not isinstance(actor, str)
            or not actor
            or actor.strip() != actor
            or event.get("actor") != actor
            or status not in {TASK_STATUS_CANCELED, TASK_STATUS_PLANNED}
            or summary is not None
            and (not isinstance(summary, str) or not summary or summary.strip() != summary)
            or not isinstance(previous_status, str)
            or type(payload.get("prompt_issue")) is not bool
            or type(payload.get("operator_issue")) is not bool
        ):
            raise BacklogError("task runtime transition request is not canonical")
        if status == TASK_STATUS_PLANNED:
            if (
                previous_status != TASK_STATUS_CANCELED
                or payload.get("failure_class") is not None
                or payload.get("recovery_action") is not None
                or payload.get("prompt_issue")
                or payload.get("operator_issue")
            ):
                raise BacklogError("task reopen transition request is not canonical")
        else:
            if previous_status not in {
                TASK_STATUS_PLANNED,
                TASK_STATUS_BLOCKED,
                TASK_STATUS_CANCELED,
            }:
                raise BacklogError("task cancel transition request has invalid source status")
            normalized_failure = normalize_failure_class(payload.get("failure_class"))
            if normalized_failure is None or normalized_failure != payload.get("failure_class"):
                raise BacklogError("task cancel transition request is missing failure class")
            recovery = payload.get("recovery_action")
            if recovery is not None and (
                not isinstance(recovery, str)
                or not recovery
                or recovery.strip() != recovery
            ):
                raise BacklogError("task cancel transition request has invalid recovery action")
        seen_ids.add(str(event_id))
        requests.append(
            {"event_id": str(event_id), "actor": actor, "payload": dict(payload)}
        )
    return tuple(requests)


def _load_task_runtime_transition_decisions(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
) -> tuple[dict[str, Any], ...]:
    requests = _load_task_runtime_transition_requests(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    requests_by_id = {row["event_id"]: row for row in requests}
    decisions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for event in load_events(profile.paths.events_file):
        if event.get("type") != "task.runtime-transition.decision":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("workset_id") != workset_id or payload.get("task_id") != task_id:
            continue
        if set(payload) != _TASK_RUNTIME_TRANSITION_DECISION_KEYS:
            raise BacklogError("task runtime transition decision has conflicting fields")
        if (
            payload.get("schema_version")
            != _TASK_RUNTIME_TRANSITION_DECISION_SCHEMA_VERSION
        ):
            raise BacklogError("task runtime transition decision has an unsupported schema version")
        actor = payload.get("actor")
        if not isinstance(actor, str) or not actor or event.get("actor") != actor:
            raise BacklogError("task runtime transition decision has a conflicting actor")
        for key in (
            "request_event_id",
            "request_semantics_hash",
            "pre_runtime_workset_hash",
            "expected_pre_runtime_identity",
            "expected_post_runtime_identity",
            "expected_post_runtime_workset_hash",
            "owned_event_id",
        ):
            if not _is_sha256(payload.get(key)):
                raise BacklogError(f"task runtime transition decision has an invalid {key}")
        expected_id = _task_runtime_transition_decision_event_id(
            request_event_id=str(payload["request_event_id"]),
            pre_runtime_workset_hash=str(payload["pre_runtime_workset_hash"]),
        )
        if event.get("event_id") != expected_id or expected_id in seen_ids:
            raise BacklogError("task runtime transition decision has a conflicting identity")
        seen_ids.add(expected_id)
        target_record = payload.get("target_record")
        owned_payload = payload.get("owned_event_payload")
        if not isinstance(target_record, Mapping) or not isinstance(owned_payload, Mapping):
            raise BacklogError("task runtime transition decision has invalid durable payloads")
        if payload.get("event_type") not in {"task.cancel", "task.reopen"}:
            raise BacklogError("task runtime transition decision has an invalid event type")
        if payload.get("target_status") not in {TASK_STATUS_CANCELED, TASK_STATUS_PLANNED}:
            raise BacklogError("task runtime transition decision has an invalid target status")
        updated_at = payload.get("updated_at")
        if not isinstance(updated_at, str) or parse_iso(updated_at) is None:
            raise BacklogError("task runtime transition decision has an invalid updated_at")
        request = requests_by_id.get(str(payload["request_event_id"]))
        if request is None:
            raise BacklogError("task runtime transition decision has no durable request")
        request_payload = request["payload"]
        expected_event_type = (
            "task.cancel"
            if request_payload["status"] == TASK_STATUS_CANCELED
            else "task.reopen"
        )
        expected_request_id = _task_runtime_transition_request_event_id(
            workset_id=workset_id,
            task_id=task_id,
            expected_pre_runtime_identity=str(
                payload["expected_pre_runtime_identity"]
            ),
            request_semantics_hash=_canonical_payload_hash(request_payload),
        )
        expected_target_record = {
            "task_id": task_id,
            "status": request_payload["status"],
            "updated_at": updated_at,
            "actor": request_payload["actor"],
            "note": request_payload["summary"],
            "failure_class": request_payload["failure_class"],
            "recovery_action": request_payload["recovery_action"],
            "prompt_issue": request_payload["prompt_issue"],
            "operator_issue": request_payload["operator_issue"],
        }
        expected_owned_event_id = _task_runtime_transition_owned_event_id(
            decision_event_id=expected_id
        )
        expected_owned_payload = {
            "workset_id": workset_id,
            "task_id": task_id,
            "status": request_payload["status"],
            "updated_at": updated_at,
            "summary": request_payload["summary"],
            "previous_status": request_payload["previous_status"],
            "failure_class": request_payload["failure_class"],
            "recovery_action": request_payload["recovery_action"],
            "prompt_issue": request_payload["prompt_issue"],
            "operator_issue": request_payload["operator_issue"],
            "transition_request_event_id": expected_request_id,
            "transition_decision_event_id": expected_id,
        }
        if (
            payload["request_event_id"] != expected_request_id
            or payload["request_semantics_hash"]
            != _canonical_payload_hash(request_payload)
            or payload["actor"] != request_payload["actor"]
            or payload["event_type"] != expected_event_type
            or payload["previous_status"] != request_payload["previous_status"]
            or payload["target_status"] != request_payload["status"]
            or not _canonical_payload_equal(target_record, expected_target_record)
            or payload["owned_event_id"] != expected_owned_event_id
            or not _canonical_payload_equal(owned_payload, expected_owned_payload)
        ):
            raise BacklogError(
                "task runtime transition decision conflicts with its durable request"
            )
        decisions.append({"event_id": expected_id, "payload": dict(payload)})
    return tuple(decisions)


def _task_runtime_transition_evidence(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    decision: Mapping[str, Any] | None,
    request_event_id: str | None,
    decision_event_id: str | None,
    owned_event_id: str | None,
) -> tuple[bool, str]:
    requests = _load_task_runtime_transition_requests(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    decisions = _load_task_runtime_transition_decisions(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    durable_request = next(
        (
            row for row in requests
            if request_event_id is not None and row["event_id"] == request_event_id
        ),
        None,
    )
    durable_decision = next(
        (
            row for row in decisions
            if decision_event_id is not None and row["event_id"] == decision_event_id
        ),
        None,
    )
    if durable_decision is not None and (
        decision is None
        or not _canonical_payload_equal(durable_decision["payload"], decision)
    ):
        raise BacklogError(
            "task runtime transition durable decision conflicts with in-memory evidence"
        )
    owned_durable = False
    if decision is not None:
        if owned_event_id != decision.get("owned_event_id"):
            raise BacklogError(
                "task runtime transition evidence names a foreign owned event"
            )
        owned_durable = _validate_task_runtime_transition_owned_event(
            profile,
            decision=decision,
        )
    runtime_durable = False
    if decision is not None:
        current = load_runtime_state(profile.paths)
        runtime_durable = (
            _task_runtime_transition_identity(
                current,
                workset_id=workset_id,
                task_id=task_id,
            )
            == decision.get("expected_post_runtime_identity")
        )
    ledger_durable = durable_request is not None or durable_decision is not None
    if owned_durable:
        return True, "event_finalized"
    if runtime_durable:
        return True, "runtime_finalized"
    if ledger_durable:
        return True, "preflight"
    return False, "none"


def _validate_task_runtime_transition_owned_event(
    profile: RepoProfile,
    *,
    decision: Mapping[str, Any],
) -> bool:
    event_id = str(decision["owned_event_id"])
    matches = [
        event
        for event in load_events(profile.paths.events_file)
        if event.get("event_id") == event_id
    ]
    if len(matches) > 1:
        raise BacklogError("task runtime transition owned event occurs more than once")
    if not matches:
        return False
    event = matches[0]
    payload = event.get("payload")
    if (
        event.get("type") != decision["event_type"]
        or event.get("actor") != decision["actor"]
        or not isinstance(payload, Mapping)
        or _canonical_payload_hash(payload)
        != _canonical_payload_hash(decision["owned_event_payload"])
    ):
        raise BacklogError(
            "task runtime transition owned event conflicts with its durable decision"
        )
    return True


def pending_task_runtime_transition(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: RuntimeState | None = None,
) -> dict[str, Any] | None:
    """Return the one durable task-state transition still requiring repair."""
    current = runtime_state or load_runtime_state(profile.paths)
    requests = _load_task_runtime_transition_requests(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    decisions = _load_task_runtime_transition_decisions(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    decisions_by_request: dict[str, list[dict[str, Any]]] = {}
    for decision in decisions:
        decisions_by_request.setdefault(
            str(decision["payload"]["request_event_id"]), []
        ).append(decision)
    if not requests:
        return None
    current_identity = _task_runtime_transition_identity(
        current,
        workset_id=workset_id,
        task_id=task_id,
    )
    incomplete: list[tuple[int, dict[str, Any], dict[str, Any] | None]] = []
    conflicting_request_ids: list[str] = []
    for index, request_row in enumerate(requests):
        related = decisions_by_request.get(request_row["event_id"], [])
        if len(related) > 1:
            conflicting_request_ids.append(str(request_row["event_id"]))
            continue
        decision_row = related[0] if related else None
        if decision_row is None or not _validate_task_runtime_transition_owned_event(
            profile,
            decision=decision_row["payload"],
        ):
            incomplete.append((index, request_row, decision_row))

    if conflicting_request_ids or len(incomplete) > 1 or (
        incomplete and incomplete[0][0] != len(requests) - 1
    ):
        conflict_rows = incomplete or [
            (
                next(
                    index
                    for index, request_row in enumerate(requests)
                    if request_row["event_id"] == conflicting_request_ids[0]
                ),
                next(
                    request_row
                    for request_row in requests
                    if request_row["event_id"] == conflicting_request_ids[0]
                ),
                None,
            )
        ]
        _index, request, decision = conflict_rows[0]
        return {
            "stage": "ledger_conflict",
            "mutation_phase": "preflight",
            "request_event_id": request["event_id"],
            "decision_event_id": decision["event_id"] if decision else None,
            "owned_event_id": (
                decision["payload"]["owned_event_id"] if decision else None
            ),
            "request": dict(request["payload"]),
            "conflicting_request_event_ids": tuple(
                dict.fromkeys(
                    [
                        *conflicting_request_ids,
                        *(str(row[1]["event_id"]) for row in incomplete),
                    ]
                )
            ),
        }
    if not incomplete:
        return None

    _index, request, decision = incomplete[0]
    related = () if decision is None else (decision,)
    if not related:
        expected_request_id = _task_runtime_transition_request_event_id(
            workset_id=workset_id,
            task_id=task_id,
            expected_pre_runtime_identity=current_identity,
            request_semantics_hash=_canonical_payload_hash(request["payload"]),
        )
        return {
            "stage": (
                "request_recorded"
                if request["event_id"] == expected_request_id
                else "runtime_conflict"
            ),
            "mutation_phase": "preflight",
            "request_event_id": request["event_id"],
            "decision_event_id": None,
            "owned_event_id": None,
            "request": dict(request["payload"]),
        }
    decision = related[0]
    decision_payload = decision["payload"]
    if current_identity == decision_payload["expected_post_runtime_identity"]:
        stage = "runtime_recorded"
        mutation_phase = "runtime_finalized"
    elif current_identity == decision_payload["expected_pre_runtime_identity"]:
        stage = "decision_recorded"
        mutation_phase = "preflight"
    else:
        stage = "runtime_conflict"
        mutation_phase = "preflight"
    return {
        "stage": stage,
        "mutation_phase": mutation_phase,
        "request_event_id": request["event_id"],
        "decision_event_id": decision["event_id"],
        "owned_event_id": decision_payload["owned_event_id"],
        "request": dict(request["payload"]),
    }


def _task_runtime_transition_task_ids(
    profile: RepoProfile,
    *,
    workset_id: str,
) -> tuple[str, ...]:
    task_ids: set[str] = set()
    for event in load_events(profile.paths.events_file):
        if event.get("type") != "task.runtime-transition.request":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("workset_id") != workset_id:
            continue
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise BacklogError(
                "task runtime transition request has an invalid target task"
            )
        task_ids.add(task_id)
    return tuple(sorted(task_ids))


def _task_runtime_transition_target_slice(
    runtime_state: RuntimeState,
    *,
    workset_id: str,
    task_id: str,
) -> tuple[
    TaskRuntimeRecord | None,
    TaskClaimRecord | None,
    tuple[TaskAttemptRecord, ...],
]:
    runtime_workset = next(
        (
            row
            for row in runtime_state.worksets
            if row.workset_id == workset_id
        ),
        None,
    )
    return (
        task_state_index(runtime_state, workset_id).get(task_id),
        task_claim_index(runtime_state, workset_id).get(task_id),
        tuple(
            attempt
            for attempt in (runtime_workset.attempts if runtime_workset else ())
            if attempt.task_id == task_id
        ),
    )


def _require_workset_merge_preserves_pending_task_transitions(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_ids: set[str],
    current_runtime: RuntimeState,
    next_runtime: RuntimeState,
) -> None:
    for task_id in _task_runtime_transition_task_ids(
        profile,
        workset_id=workset_id,
    ):
        pending = pending_task_runtime_transition(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=current_runtime,
        )
        if pending is None:
            continue
        if task_id not in task_ids or _task_runtime_transition_target_slice(
            current_runtime,
            workset_id=workset_id,
            task_id=task_id,
        ) != _task_runtime_transition_target_slice(
            next_runtime,
            workset_id=workset_id,
            task_id=task_id,
        ):
            raise BacklogError(
                f"Workset {workset_id!r} cannot overwrite or prune task {task_id!r} "
                "while its runtime transition is incomplete"
            )
    for task_id in _stale_claim_release_task_ids(
        profile,
        workset_id=workset_id,
    ):
        pending = pending_stale_claim_release(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=current_runtime,
        )
        if pending is None:
            continue
        if (
            task_id not in task_ids
            or _task_runtime_transition_target_slice(
                current_runtime,
                workset_id=workset_id,
                task_id=task_id,
            )
            != _task_runtime_transition_target_slice(
                next_runtime,
                workset_id=workset_id,
                task_id=task_id,
            )
            or _stale_claim_release_claim_set_identity(
                current_runtime,
                workset_id=workset_id,
            )
            != _stale_claim_release_claim_set_identity(
                next_runtime,
                workset_id=workset_id,
            )
        ):
            raise StaleClaimReleaseConflictError(
                f"Workset {workset_id!r} cannot overwrite or prune stale-release "
                f"target {task_id!r} while finalization is incomplete"
            )


def require_no_pending_task_runtime_transition(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: RuntimeState,
) -> None:
    pending = pending_task_runtime_transition(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        runtime_state=runtime_state,
    )
    if pending is not None:
        operation = (
            "cancel"
            if pending["request"]["status"] == TASK_STATUS_CANCELED
            else "reopen"
        )
        raise BacklogError(
            f"Task {task_id!r} has an incomplete task {operation} transition; "
            "retry its exact durable request before another mutation"
        )


def _next_task_runtime_transition_at(
    decisions: tuple[dict[str, Any], ...],
) -> str:
    candidate = now_iso()
    used = {str(row["payload"]["updated_at"]) for row in decisions}
    while candidate in used:
        parsed = parse_iso(candidate)
        if parsed is None:  # Defensive: now_iso is always parseable.
            raise BacklogError("task runtime transition timestamp is invalid")
        candidate = (parsed + timedelta(seconds=1)).isoformat(timespec="seconds")
    return candidate


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
    return_transition_result: bool = False,
    expected_transition_request_event_id: str | None = None,
    expected_transition_decision_event_id: str | None = None,
) -> TaskRuntimeRecord | TaskRuntimeTransitionResult:
    if status not in {TASK_STATUS_CANCELED, TASK_STATUS_PLANNED}:
        raise BacklogError("set_task_runtime_status only supports canceled or planned")
    resolved_actor = str(actor or "").strip()
    if not resolved_actor:
        raise BacklogError("task runtime transition requires a nonempty actor")
    resolved_summary = str(summary or "").strip() or None
    planning_state = load_planning_state(profile.paths, planning_store)
    workset, _ = _require_workset_and_task(planning_state, workset_id=workset_id, task_id=task_id)
    event_type = "task.cancel" if status == TASK_STATUS_CANCELED else "task.reopen"
    resolved_failure_class = normalize_failure_class(failure_class)
    if status == TASK_STATUS_CANCELED and resolved_failure_class is None:
        resolved_failure_class = FAILURE_CLASS_UNKNOWN
    resolved_recovery_action = (
        (str(recovery_action or "").strip() or None)
        if status == TASK_STATUS_CANCELED
        else None
    )
    resolved_prompt_issue = bool(prompt_issue) if status == TASK_STATUS_CANCELED else False
    resolved_operator_issue = bool(operator_issue) if status == TASK_STATUS_CANCELED else False
    expected_request_event_id = (
        str(expected_transition_request_event_id or "").strip() or None
    )
    expected_decision_event_id = (
        str(expected_transition_decision_event_id or "").strip() or None
    )
    if expected_decision_event_id is not None and expected_request_event_id is None:
        raise BacklogError(
            "task runtime transition decision guard requires its request guard"
        )
    if expected_request_event_id is not None and not _is_sha256(
        expected_request_event_id
    ):
        raise TaskRuntimeTransitionGuardConflictError(
            "task runtime transition request guard is not a durable event identity"
        )
    if expected_decision_event_id is not None and not _is_sha256(
        expected_decision_event_id
    ):
        raise TaskRuntimeTransitionGuardConflictError(
            "task runtime transition decision guard is not a durable event identity"
        )
    current_status = TASK_STATUS_PLANNED
    record: TaskRuntimeRecord | None = None
    updated_at: str | None = None
    chosen_decision: dict[str, Any] | None = None
    request_event_id: str | None = None
    decision_event_id: str | None = None
    owned_event_id: str | None = None
    runtime_changed = False
    request_event_appended = False
    decision_event_appended = False
    owned_event_appended = False

    def request_for(previous_status: str) -> dict[str, Any]:
        return _task_runtime_transition_request_payload(
            workset_id=workset_id,
            task_id=task_id,
            actor=resolved_actor,
            status=status,
            summary=resolved_summary,
            previous_status=previous_status,
            failure_class=(
                resolved_failure_class if status == TASK_STATUS_CANCELED else None
            ),
            recovery_action=resolved_recovery_action,
            prompt_issue=resolved_prompt_issue,
            operator_issue=resolved_operator_issue,
        )

    def request_matches(decision: Mapping[str, Any]) -> bool:
        previous = decision.get("previous_status")
        return isinstance(previous, str) and _canonical_payload_hash(
            request_for(previous)
        ) == decision.get("request_semantics_hash")

    def record_from_decision(decision: Mapping[str, Any]) -> TaskRuntimeRecord:
        payload = decision.get("target_record")
        expected_keys = {
            "task_id",
            "status",
            "updated_at",
            "actor",
            "note",
            "failure_class",
            "recovery_action",
            "prompt_issue",
            "operator_issue",
        }
        if not isinstance(payload, Mapping) or set(payload) != expected_keys:
            raise BacklogError("task runtime transition decision has an invalid target record")
        try:
            candidate = TaskRuntimeRecord(**dict(payload))
        except TypeError as exc:
            raise BacklogError(
                "task runtime transition decision has an invalid target record"
            ) from exc
        if (
            candidate.task_id != task_id
            or candidate.status != status
            or candidate.actor != resolved_actor
            or candidate.updated_at != decision.get("updated_at")
        ):
            raise BacklogError("task runtime transition decision conflicts with its request")
        return candidate

    def mutate(runtime_state: RuntimeState) -> RuntimeState:
        nonlocal current_status, record, updated_at, chosen_decision
        nonlocal request_event_id, decision_event_id, owned_event_id
        nonlocal runtime_changed, request_event_appended, decision_event_appended
        _task_scoped_runtime_task_ids(
            profile,
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            planning_store=planning_store,
        )
        require_no_pending_stale_claim_release(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
        )
        current_status = {
            task_state.task_id: task_state.status
            for runtime_workset in runtime_state.worksets
            if runtime_workset.workset_id == workset_id
            for task_state in runtime_workset.task_states
        }.get(task_id, TASK_STATUS_PLANNED)
        pending_transition = pending_task_runtime_transition(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
        )
        current_identity = _task_runtime_transition_identity(
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
        )
        requests = _load_task_runtime_transition_requests(
            profile,
            workset_id=workset_id,
            task_id=task_id,
        )
        decisions = _load_task_runtime_transition_decisions(
            profile,
            workset_id=workset_id,
            task_id=task_id,
        )
        if expected_request_event_id is not None:
            guarded_request_index = next(
                (
                    index
                    for index, request in enumerate(requests)
                    if request["event_id"] == expected_request_event_id
                ),
                None,
            )
            if guarded_request_index is None:
                guarded_request_payload = request_for(current_status)
                derived_guard_id = _task_runtime_transition_request_event_id(
                    workset_id=workset_id,
                    task_id=task_id,
                    expected_pre_runtime_identity=current_identity,
                    request_semantics_hash=_canonical_payload_hash(
                        guarded_request_payload
                    ),
                )
                if (
                    expected_decision_event_id is not None
                    or pending_transition is not None
                    or derived_guard_id != expected_request_event_id
                ):
                    raise TaskRuntimeTransitionGuardConflictError(
                        "guarded task runtime transition request is no longer reservable"
                    )
            else:
                guarded_request = requests[guarded_request_index]
                guarded_request_payload = guarded_request["payload"]
                if (
                    _canonical_payload_hash(
                        request_for(str(guarded_request_payload["previous_status"]))
                    )
                    != _canonical_payload_hash(guarded_request_payload)
                ):
                    raise TaskRuntimeTransitionGuardConflictError(
                        "guarded task runtime transition semantics do not match the durable request"
                    )
                if guarded_request_index != len(requests) - 1:
                    raise TaskRuntimeTransitionGuardConflictError(
                        "guarded task runtime transition was superseded by a later generation"
                    )
                guarded_decisions = [
                    decision
                    for decision in decisions
                    if decision["payload"]["request_event_id"]
                    == expected_request_event_id
                ]
                if len(guarded_decisions) > 1:
                    raise TaskRuntimeTransitionGuardConflictError(
                        "guarded task runtime transition has conflicting durable decisions"
                    )
                guarded_decision = guarded_decisions[0] if guarded_decisions else None
                if expected_decision_event_id is not None and (
                    guarded_decision is None
                    or guarded_decision["event_id"] != expected_decision_event_id
                ):
                    raise TaskRuntimeTransitionGuardConflictError(
                        "guarded task runtime transition decision no longer matches"
                    )
                guarded_owned = bool(
                    guarded_decision is not None
                    and _validate_task_runtime_transition_owned_event(
                        profile,
                        decision=guarded_decision["payload"],
                    )
                )
                if guarded_owned:
                    if (
                        current_identity
                        != guarded_decision["payload"]["expected_post_runtime_identity"]
                    ):
                        raise TaskRuntimeTransitionGuardConflictError(
                            "guarded task runtime transition completed before a later runtime mutation"
                        )
                elif (
                    pending_transition is None
                    or pending_transition["stage"]
                    in {"runtime_conflict", "ledger_conflict"}
                    or pending_transition["request_event_id"]
                    != expected_request_event_id
                    or (
                        expected_decision_event_id is not None
                        and pending_transition["decision_event_id"]
                        != expected_decision_event_id
                    )
                ):
                    raise TaskRuntimeTransitionGuardConflictError(
                        "guarded task runtime transition is no longer repairable"
                    )
        if pending_transition is not None:
            pending_request = pending_transition["request"]
            if (
                pending_transition["stage"]
                in {"runtime_conflict", "ledger_conflict"}
                or _canonical_payload_hash(
                    request_for(str(pending_request["previous_status"]))
                )
                != _canonical_payload_hash(pending_request)
            ):
                error_type = (
                    TaskRuntimeTransitionGuardConflictError
                    if expected_request_event_id is not None
                    else BacklogError
                )
                raise error_type(
                    "task runtime transition is reserved by a different durable request"
                )
        runtime_task_claims = task_claim_index(runtime_state, workset_id)
        if task_id in runtime_task_claims:
            raise BacklogError(
                f"Task {task_id!r} is claimed; close or recover it before changing state"
            )
        post_candidates = [
            decision
            for decision in decisions
            if decision["payload"]["expected_post_runtime_identity"]
            == current_identity
        ]
        exact_post = [
            decision
            for decision in post_candidates
            if request_matches(decision["payload"])
        ]
        if post_candidates and current_status == status and len(exact_post) != 1:
            raise BacklogError(
                "task runtime transition retry conflicts with its durable request"
            )
        if current_status != status:
            exact_post = []
        pre_candidates = [
            decision
            for decision in decisions
            if decision["payload"]["expected_pre_runtime_identity"]
            == current_identity
        ]
        exact_pre = [
            decision
            for decision in pre_candidates
            if request_matches(decision["payload"])
        ]
        if not exact_post and pre_candidates and len(exact_pre) != 1:
            raise BacklogError(
                "task runtime transition source state is reserved by a different request"
            )

        selected = exact_post[0] if exact_post else exact_pre[0] if exact_pre else None
        if selected is not None:
            decision = selected["payload"]
            if expected_request_event_id is not None and (
                decision["request_event_id"] != expected_request_event_id
                or expected_decision_event_id is not None
                and selected["event_id"] != expected_decision_event_id
            ):
                raise TaskRuntimeTransitionGuardConflictError(
                    "guarded task runtime transition selected a different generation"
                )
            request_event_id = str(decision["request_event_id"])
            decision_event_id = str(selected["event_id"])
            owned_event_id = str(decision["owned_event_id"])
            chosen_decision = dict(decision)
            exact_request = request_for(str(decision["previous_status"]))
            derived_request_event_id = _task_runtime_transition_request_event_id(
                workset_id=workset_id,
                task_id=task_id,
                expected_pre_runtime_identity=str(
                    decision["expected_pre_runtime_identity"]
                ),
                request_semantics_hash=_canonical_payload_hash(exact_request),
            )
            if request_event_id != derived_request_event_id:
                raise BacklogError(
                    "task runtime transition decision has a conflicting request identity"
                )
            request_event_appended = append_event_once(
                profile.paths.events_file,
                event_id=request_event_id,
                event_type="task.runtime-transition.request",
                actor=resolved_actor,
                payload=exact_request,
            )
            decision_event_appended = append_event_once(
                profile.paths.events_file,
                event_id=decision_event_id,
                event_type="task.runtime-transition.decision",
                actor=resolved_actor,
                payload=decision,
            )
            record = record_from_decision(decision)
            _validate_task_runtime_transition_owned_event(
                profile,
                decision=decision,
            )
            updated_at = record.updated_at
            current_status = str(decision["previous_status"])
            if exact_post:
                current_record = task_state_index(runtime_state, workset_id).get(task_id)
                if current_record != record:
                    raise BacklogError(
                        "task runtime transition terminal state conflicts with its decision"
                    )
                return runtime_state
            next_state = _merge_task_scoped_runtime(
                profile,
                runtime_state,
                workset_id=workset_id,
                task_id=task_id,
                planning_store=planning_store,
                incoming_records=(record,),
            )
            if (
                _task_runtime_transition_identity(
                    next_state,
                    workset_id=workset_id,
                    task_id=task_id,
                )
                != decision["expected_post_runtime_identity"]
            ):
                raise BacklogError(
                    "task runtime transition decision conflicts with the runtime mutation"
                )
            if (
                _runtime_workset_hash(runtime_state, workset_id)
                == decision["pre_runtime_workset_hash"]
                and _runtime_workset_hash(next_state, workset_id)
                != decision["expected_post_runtime_workset_hash"]
            ):
                raise BacklogError(
                    "task runtime transition decision conflicts with its original workset mutation"
                )
            runtime_changed = next_state != runtime_state
            return next_state

        if current_status == TASK_STATUS_IN_PROGRESS:
            raise BacklogError(f"Task {task_id!r} is in progress; close it before changing state")
        if current_status == TASK_STATUS_DONE and status == TASK_STATUS_CANCELED:
            raise BacklogError(f"Task {task_id!r} is done and cannot be canceled")
        if status == TASK_STATUS_PLANNED and current_status != TASK_STATUS_CANCELED:
            raise BacklogError(f"Task {task_id!r} is not canceled")
        updated_at = _next_task_runtime_transition_at(decisions)
        record = TaskRuntimeRecord(
            task_id=task_id,
            status=status,
            updated_at=updated_at,
            actor=resolved_actor,
            note=resolved_summary,
            failure_class=resolved_failure_class if status == TASK_STATUS_CANCELED else None,
            recovery_action=resolved_recovery_action,
            prompt_issue=resolved_prompt_issue,
            operator_issue=resolved_operator_issue,
        )
        next_state = _merge_task_scoped_runtime(
            profile,
            runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            planning_store=planning_store,
            incoming_records=(record,),
        )
        exact_request = request_for(current_status)
        request_semantics_hash = _canonical_payload_hash(exact_request)
        pre_workset_hash = _runtime_workset_hash(runtime_state, workset_id)
        post_identity = _task_runtime_transition_identity(
            next_state,
            workset_id=workset_id,
            task_id=task_id,
        )
        request_event_id = _task_runtime_transition_request_event_id(
            workset_id=workset_id,
            task_id=task_id,
            expected_pre_runtime_identity=current_identity,
            request_semantics_hash=request_semantics_hash,
        )
        decision_event_id = _task_runtime_transition_decision_event_id(
            request_event_id=request_event_id,
            pre_runtime_workset_hash=pre_workset_hash,
        )
        owned_event_id = _task_runtime_transition_owned_event_id(
            decision_event_id=decision_event_id
        )
        if expected_request_event_id is not None and (
            request_event_id != expected_request_event_id
            or expected_decision_event_id is not None
            and decision_event_id != expected_decision_event_id
        ):
            raise TaskRuntimeTransitionGuardConflictError(
                "guarded task runtime transition would reserve a different generation"
            )
        owned_payload = {
            "workset_id": workset_id,
            "task_id": task_id,
            "status": status,
            "updated_at": updated_at,
            "summary": resolved_summary,
            "previous_status": current_status,
            "failure_class": record.failure_class,
            "recovery_action": record.recovery_action,
            "prompt_issue": record.prompt_issue,
            "operator_issue": record.operator_issue,
            "transition_request_event_id": request_event_id,
            "transition_decision_event_id": decision_event_id,
        }
        chosen_decision = {
            "schema_version": _TASK_RUNTIME_TRANSITION_DECISION_SCHEMA_VERSION,
            "request_event_id": request_event_id,
            "request_semantics_hash": request_semantics_hash,
            "workset_id": workset_id,
            "task_id": task_id,
            "actor": resolved_actor,
            "event_type": event_type,
            "previous_status": current_status,
            "target_status": status,
            "pre_runtime_workset_hash": pre_workset_hash,
            "expected_pre_runtime_identity": current_identity,
            "updated_at": updated_at,
            "target_record": _task_runtime_record_payload(record),
            "expected_post_runtime_identity": post_identity,
            "expected_post_runtime_workset_hash": _runtime_workset_hash(
                next_state, workset_id
            ),
            "owned_event_id": owned_event_id,
            "owned_event_payload": owned_payload,
        }
        _validate_task_runtime_transition_owned_event(
            profile,
            decision=chosen_decision,
        )
        request_event_appended = append_event_once(
            profile.paths.events_file,
            event_id=request_event_id,
            event_type="task.runtime-transition.request",
            actor=resolved_actor,
            payload=exact_request,
        )
        decision_event_appended = append_event_once(
            profile.paths.events_file,
            event_id=decision_event_id,
            event_type="task.runtime-transition.decision",
            actor=resolved_actor,
            payload=chosen_decision,
        )
        runtime_changed = next_state != runtime_state
        return next_state

    def after_save(runtime_state: RuntimeState) -> None:
        nonlocal owned_event_appended
        try:
            if chosen_decision is None or owned_event_id is None:
                raise BacklogError(
                    "task runtime transition did not choose a durable decision"
                )
            if (
                _task_runtime_transition_identity(
                    runtime_state,
                    workset_id=workset_id,
                    task_id=task_id,
                )
                != chosen_decision["expected_post_runtime_identity"]
            ):
                raise BacklogError(
                    "task runtime transition decision no longer matches runtime"
                )
            owned_event_appended = append_event_once(
                profile.paths.events_file,
                event_id=owned_event_id,
                event_type=event_type,
                actor=resolved_actor,
                payload=chosen_decision["owned_event_payload"],
            )
        except Exception as exc:
            raise _TaskRuntimeTransitionUncertainError(
                "task runtime transition stopped during post-save finalization"
            ) from exc

    try:
        mutate_runtime_state(
            profile.paths,
            mutate,
            store=runtime_store,
            after_save=after_save,
            save_unchanged=False,
        )
    except _TaskRuntimeTransitionUncertainError as exc:
        try:
            mutation_started, mutation_phase = _task_runtime_transition_evidence(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                decision=chosen_decision,
                request_event_id=request_event_id,
                decision_event_id=decision_event_id,
                owned_event_id=owned_event_id,
            )
        except Exception:
            mutation_started, mutation_phase = True, "runtime_finalized"
        raise TaskRuntimeTransitionFinalizationError(
            f"task runtime transition finalization interrupted: {exc.__cause__ or exc}",
            mutation_started=True,
            mutation_phase=(
                mutation_phase if mutation_started else "runtime_finalized"
            ),
            request_event_id=request_event_id,
            decision_event_id=decision_event_id,
            owned_event_id=owned_event_id,
        ) from (exc.__cause__ or exc)
    except BacklogError:
        raise
    except StoreError as exc:
        try:
            mutation_started, mutation_phase = _task_runtime_transition_evidence(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                decision=chosen_decision,
                request_event_id=request_event_id,
                decision_event_id=decision_event_id,
                owned_event_id=owned_event_id,
            )
        except BacklogError:
            raise
        raise TaskRuntimeTransitionFinalizationError(
            f"task runtime transition finalization interrupted: {exc}",
            mutation_started=mutation_started,
            mutation_phase=mutation_phase,
            request_event_id=request_event_id,
            decision_event_id=decision_event_id,
            owned_event_id=owned_event_id,
        ) from exc
    except Exception as exc:
        try:
            mutation_started, mutation_phase = _task_runtime_transition_evidence(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                decision=chosen_decision,
                request_event_id=request_event_id,
                decision_event_id=decision_event_id,
                owned_event_id=owned_event_id,
            )
        except Exception:
            mutation_started, mutation_phase = False, "none"
        raise TaskRuntimeTransitionFinalizationError(
            f"task runtime transition finalization interrupted: {exc}",
            mutation_started=mutation_started,
            mutation_phase=mutation_phase,
            request_event_id=request_event_id,
            decision_event_id=decision_event_id,
            owned_event_id=owned_event_id,
        ) from exc
    if record is None or updated_at is None:
        raise BacklogError("task state update did not write a runtime record")
    if return_transition_result:
        return TaskRuntimeTransitionResult(
            record=record,
            runtime_changed=runtime_changed,
            request_event_appended=request_event_appended,
            decision_event_appended=decision_event_appended,
            owned_event_appended=owned_event_appended,
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
    "AbandonedLandingEligibility",
    "JsonPlanningStore",
    "PlanningState",
    "PlanningStore",
    "TaskFinalizationEvidence",
    "TaskSpec",
    "Workset",
    "default_failure_class_for_status",
    "default_planning_state",
    "find_workset",
    "finish_task",
    "inspect_task_finalization",
    "load_planning_state",
    "landing_reconciliation_id",
    "next_ready_tasks",
    "normalize_failure_class",
    "pending_stale_claim_release",
    "pending_stale_claim_release_for_workset",
    "planning_state_to_payload",
    "save_planning_state",
    "reconcile_landed_attempt",
    "set_task_runtime_status",
    "start_task",
    "task_start_event_contracts",
    "task_start_event_id",
    "task_resume_attempt_id",
    "task_dependencies_ready",
    "task_finalization_decision_event_id",
    "task_finalization_owned_event_id",
    "task_finalization_request_event_id",
    "upsert_workset",
    "workset_to_payload",
]
