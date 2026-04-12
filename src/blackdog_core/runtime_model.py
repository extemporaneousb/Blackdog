"""Typed runtime views projected from the vNext planning and runtime stores."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backlog import PlanningState, TaskSpec, Workset, load_planning_state, next_ready_tasks, task_dependencies_ready
from .profile import RepoProfile
from .state import (
    ATTEMPT_ACTIVE_STATUSES,
    TASK_STATUS_BLOCKED,
    TASK_STATUS_DONE,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_PLANNED,
    RuntimeState,
    TaskClaimRecord,
    TaskAttemptRecord,
    TaskRuntimeRecord,
    ValidationRecord,
    WorksetClaimRecord,
    load_events,
    load_runtime_state,
    parse_iso,
    task_claim_index,
    task_attempts_for_workset,
    task_state_index,
    workset_claim,
)


SNAPSHOT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class Repository:
    project_name: str
    project_root: Path
    control_dir: Path
    planning_file: Path
    runtime_file: Path
    events_file: Path
    validation_commands: tuple[str, ...]
    doc_routing_defaults: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ValidationView:
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class PromptReceiptView:
    text: str
    prompt_hash: str
    recorded_at: str
    source: str | None


@dataclass(frozen=True, slots=True)
class WorksetClaimView:
    actor: str
    execution_model: str
    claimed_at: str
    note: str | None


@dataclass(frozen=True, slots=True)
class TaskClaimView:
    task_id: str
    actor: str
    execution_model: str
    claimed_at: str
    attempt_id: str | None
    note: str | None


@dataclass(frozen=True, slots=True)
class AttemptView:
    attempt_id: str
    task_id: str
    status: str
    actor: str
    started_at: str
    ended_at: str | None
    elapsed_seconds: int | None
    summary: str | None
    workspace_identity: str | None
    workspace_mode: str | None
    worktree_role: str | None
    worktree_path: str | None
    branch: str | None
    target_branch: str | None
    integration_branch: str | None
    start_commit: str | None
    execution_model: str | None
    model: str | None
    reasoning_effort: str | None
    prompt_receipt: PromptReceiptView | None
    changed_paths: tuple[str, ...]
    validations: tuple[ValidationView, ...]
    residuals: tuple[str, ...]
    followup_candidates: tuple[str, ...]
    note: str | None
    commit: str | None
    landed_commit: str | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class TaskView:
    task_id: str
    title: str
    intent: str
    description: str | None
    depends_on: tuple[str, ...]
    paths: tuple[str, ...]
    docs: tuple[str, ...]
    checks: tuple[str, ...]
    metadata: dict[str, Any]
    runtime_status: str
    readiness: str
    blocked_by: tuple[str, ...]
    claim_actor: str | None
    claim_execution_model: str | None
    claimed_at: str | None
    note: str | None
    updated_at: str | None
    is_ready: bool
    attempt_count: int
    latest_attempt_id: str | None
    latest_attempt_status: str | None
    latest_attempt_summary: str | None
    active_attempt_id: str | None


@dataclass(frozen=True, slots=True)
class WorksetView:
    workset_id: str
    title: str
    scope: dict[str, Any]
    visibility: dict[str, Any]
    policies: dict[str, Any]
    workspace: dict[str, Any]
    branch_intent: dict[str, Any]
    metadata: dict[str, Any]
    claim: WorksetClaimView | None
    task_claims: tuple[TaskClaimView, ...]
    tasks: tuple[TaskView, ...]
    attempts: tuple[AttemptView, ...]
    counts: dict[str, int]
    next_task_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeModel:
    schema_version: int
    repository: Repository
    worksets: tuple[WorksetView, ...]
    counts: dict[str, int]
    next_tasks: tuple[TaskView, ...]
    recent_attempts: tuple[AttemptView, ...]
    events: tuple[dict[str, Any], ...]


def project_repository(profile: RepoProfile) -> Repository:
    return Repository(
        project_name=profile.project_name,
        project_root=profile.paths.project_root,
        control_dir=profile.paths.control_dir,
        planning_file=profile.paths.planning_file,
        runtime_file=profile.paths.runtime_file,
        events_file=profile.paths.events_file,
        validation_commands=profile.validation_commands,
        doc_routing_defaults=profile.doc_routing_defaults,
    )


def _default_runtime(task_id: str) -> TaskRuntimeRecord:
    return TaskRuntimeRecord(task_id=task_id, status=TASK_STATUS_PLANNED)


def _attempt_sort_key(attempt: TaskAttemptRecord) -> tuple[float, str]:
    ended_at = parse_iso(attempt.ended_at)
    started_at = parse_iso(attempt.started_at)
    anchor = ended_at or started_at
    timestamp = anchor.timestamp() if anchor is not None else 0.0
    return (timestamp, attempt.attempt_id)


def _validation_view(validation: ValidationRecord) -> ValidationView:
    return ValidationView(name=validation.name, status=validation.status)


def _prompt_receipt_view(prompt_receipt) -> PromptReceiptView | None:
    if prompt_receipt is None:
        return None
    return PromptReceiptView(
        text=prompt_receipt.text,
        prompt_hash=prompt_receipt.prompt_hash,
        recorded_at=prompt_receipt.recorded_at,
        source=prompt_receipt.source,
    )


def _workset_claim_view(claim: WorksetClaimRecord | None) -> WorksetClaimView | None:
    if claim is None:
        return None
    return WorksetClaimView(
        actor=claim.actor,
        execution_model=claim.execution_model,
        claimed_at=claim.claimed_at,
        note=claim.note,
    )


def _task_claim_view(claim: TaskClaimRecord) -> TaskClaimView:
    return TaskClaimView(
        task_id=claim.task_id,
        actor=claim.actor,
        execution_model=claim.execution_model,
        claimed_at=claim.claimed_at,
        attempt_id=claim.attempt_id,
        note=claim.note,
    )


def _attempt_view(attempt: TaskAttemptRecord) -> AttemptView:
    return AttemptView(
        attempt_id=attempt.attempt_id,
        task_id=attempt.task_id,
        status=attempt.status,
        actor=attempt.actor,
        started_at=attempt.started_at,
        ended_at=attempt.ended_at,
        elapsed_seconds=attempt.elapsed_seconds,
        summary=attempt.summary,
        workspace_identity=attempt.workspace_identity,
        workspace_mode=attempt.workspace_mode,
        worktree_role=attempt.worktree_role,
        worktree_path=attempt.worktree_path,
        branch=attempt.branch,
        target_branch=attempt.target_branch,
        integration_branch=attempt.integration_branch,
        start_commit=attempt.start_commit,
        execution_model=attempt.execution_model,
        model=attempt.model,
        reasoning_effort=attempt.reasoning_effort,
        prompt_receipt=_prompt_receipt_view(attempt.prompt_receipt),
        changed_paths=attempt.changed_paths,
        validations=tuple(_validation_view(item) for item in attempt.validations),
        residuals=attempt.residuals,
        followup_candidates=attempt.followup_candidates,
        note=attempt.note,
        commit=attempt.commit,
        landed_commit=attempt.landed_commit,
        is_active=attempt.status in ATTEMPT_ACTIVE_STATUSES,
    )


def _task_view(
    workset: Workset,
    task: TaskSpec,
    runtime_index: dict[str, TaskRuntimeRecord],
    task_claims_by_task: dict[str, TaskClaimRecord],
    attempts_by_task: dict[str, tuple[TaskAttemptRecord, ...]],
) -> TaskView:
    runtime = runtime_index.get(task.task_id, _default_runtime(task.task_id))
    task_claim = task_claims_by_task.get(task.task_id)
    task_attempts = attempts_by_task.get(task.task_id, ())
    latest_attempt = task_attempts[0] if task_attempts else None
    active_attempt = next((attempt for attempt in task_attempts if attempt.status in ATTEMPT_ACTIVE_STATUSES), None)
    if runtime.status == TASK_STATUS_DONE:
        readiness = "done"
        blocked_by: tuple[str, ...] = ()
    elif runtime.status == TASK_STATUS_IN_PROGRESS:
        readiness = "in_progress"
        blocked_by = ()
    elif runtime.status == TASK_STATUS_BLOCKED:
        readiness = "blocked"
        blocked_by = (runtime.note,) if runtime.note else ()
    else:
        dependencies_ready, missing_dependencies = task_dependencies_ready(
            workset,
            task_id=task.task_id,
            runtime_index=runtime_index,
        )
        readiness = "ready" if dependencies_ready else "blocked"
        blocked_by = missing_dependencies
    return TaskView(
        task_id=task.task_id,
        title=task.title,
        intent=task.intent,
        description=task.description,
        depends_on=task.depends_on,
        paths=task.paths,
        docs=task.docs,
        checks=task.checks,
        metadata=dict(task.metadata),
        runtime_status=runtime.status,
        readiness=readiness,
        blocked_by=blocked_by,
        claim_actor=task_claim.actor if task_claim is not None else None,
        claim_execution_model=task_claim.execution_model if task_claim is not None else None,
        claimed_at=task_claim.claimed_at if task_claim is not None else None,
        note=runtime.note,
        updated_at=runtime.updated_at,
        is_ready=readiness == "ready",
        attempt_count=len(task_attempts),
        latest_attempt_id=latest_attempt.attempt_id if latest_attempt else None,
        latest_attempt_status=latest_attempt.status if latest_attempt else None,
        latest_attempt_summary=latest_attempt.summary if latest_attempt else None,
        active_attempt_id=active_attempt.attempt_id if active_attempt else None,
    )


def _count_workset(tasks: tuple[TaskView, ...], attempts: tuple[AttemptView, ...]) -> dict[str, int]:
    counts = {
        "tasks": len(tasks),
        "ready": 0,
        "in_progress": 0,
        "blocked": 0,
        "done": 0,
        "claimed_tasks": 0,
        "attempts": len(attempts),
        "active_attempts": 0,
    }
    for task in tasks:
        if task.claim_actor:
            counts["claimed_tasks"] += 1
        if task.readiness == "ready":
            counts["ready"] += 1
        elif task.readiness == "in_progress":
            counts["in_progress"] += 1
        elif task.readiness == "done":
            counts["done"] += 1
        else:
            counts["blocked"] += 1
    for attempt in attempts:
        if attempt.is_active:
            counts["active_attempts"] += 1
    return counts


def _workset_view(workset: Workset, runtime_state: RuntimeState) -> WorksetView:
    runtime_index = task_state_index(runtime_state, workset.workset_id)
    runtime_task_claims = task_claim_index(runtime_state, workset.workset_id)
    raw_attempts = sorted(
        task_attempts_for_workset(runtime_state, workset.workset_id),
        key=_attempt_sort_key,
        reverse=True,
    )
    attempts_by_task: dict[str, list[TaskAttemptRecord]] = {}
    for attempt in raw_attempts:
        attempts_by_task.setdefault(attempt.task_id, []).append(attempt)
    task_views = tuple(
        _task_view(
            workset,
            task,
            runtime_index,
            runtime_task_claims,
            {task_id: tuple(items) for task_id, items in attempts_by_task.items()},
        )
        for task in workset.tasks
    )
    attempt_views = tuple(_attempt_view(attempt) for attempt in raw_attempts)
    next_task_ids = tuple(task_view.task_id for task_view in task_views if task_view.is_ready)
    return WorksetView(
        workset_id=workset.workset_id,
        title=workset.title,
        scope=dict(workset.scope),
        visibility=dict(workset.visibility),
        policies=dict(workset.policies),
        workspace=dict(workset.workspace),
        branch_intent=dict(workset.branch_intent),
        metadata=dict(workset.metadata),
        claim=_workset_claim_view(workset_claim(runtime_state, workset.workset_id)),
        task_claims=tuple(_task_claim_view(item) for item in runtime_task_claims.values()),
        tasks=task_views,
        attempts=attempt_views,
        counts=_count_workset(task_views, attempt_views),
        next_task_ids=next_task_ids,
    )


def project_runtime_model(
    profile: RepoProfile,
    planning_state: PlanningState,
    runtime_state: RuntimeState,
    *,
    events: tuple[dict[str, Any], ...] = (),
) -> RuntimeModel:
    worksets = tuple(_workset_view(workset, runtime_state) for workset in planning_state.worksets)
    next_tasks_lookup = {
        (workset.workset_id, task.task_id): task
        for workset in worksets
        for task in workset.tasks
    }
    next_tasks = tuple(
        next_tasks_lookup[(workset.workset_id, task.task_id)]
        for workset, task in next_ready_tasks(planning_state, runtime_state=runtime_state)
    )
    recent_attempts = tuple(
        attempt
        for workset in worksets
        for attempt in workset.attempts
    )
    recent_attempts = tuple(sorted(recent_attempts, key=lambda item: (parse_iso(item.ended_at or item.started_at) or parse_iso("1970-01-01T00:00:00+00:00")).timestamp(), reverse=True))
    counts = {
        "worksets": len(worksets),
        "claimed_worksets": 0,
        "tasks": 0,
        "ready": 0,
        "in_progress": 0,
        "blocked": 0,
        "done": 0,
        "claimed_tasks": 0,
        "attempts": 0,
        "active_attempts": 0,
    }
    for workset in worksets:
        if workset.claim is not None:
            counts["claimed_worksets"] += 1
        for key, value in workset.counts.items():
            counts[key] = counts.get(key, 0) + value
    return RuntimeModel(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        repository=project_repository(profile),
        worksets=worksets,
        counts=counts,
        next_tasks=next_tasks,
        recent_attempts=recent_attempts,
        events=events,
    )


def load_runtime_model(profile: RepoProfile) -> RuntimeModel:
    planning_state = load_planning_state(profile.paths)
    runtime_state = load_runtime_state(profile.paths)
    events = load_events(profile.paths.events_file)
    return project_runtime_model(profile, planning_state, runtime_state, events=events)


__all__ = [
    "AttemptView",
    "TaskClaimView",
    "Repository",
    "RuntimeModel",
    "SNAPSHOT_SCHEMA_VERSION",
    "TaskView",
    "ValidationView",
    "WorksetClaimView",
    "WorksetView",
    "load_runtime_model",
    "project_repository",
    "project_runtime_model",
]
