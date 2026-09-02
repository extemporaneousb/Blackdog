"""Flat derived read model for canonical task state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .profile import RepoProfile
from .state import (
    ATTEMPT_STATUS_IN_PROGRESS,
    CodexSessionRefRecord,
    PromptReceiptRecord,
    RuntimeState,
    TaskAttemptRecord,
    TaskRecord,
    ValidationRecord,
    load_events,
    load_runtime_state,
    parse_iso,
)


SNAPSHOT_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class Repository:
    project_name: str
    project_root: Path
    control_dir: Path
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
    prompt_hash: str
    recorded_at: str
    source: str | None
    mode: str | None
    replay_artifact_path: str | None


@dataclass(frozen=True, slots=True)
class CodexSessionRefView:
    thread_id: str
    session_path: str | None
    turn_id: str | None
    turn_started_at: str | None
    user_prompt_hash: str | None
    execution_prompt_hash: str | None
    capture_status: str | None
    capture_method: str | None
    capture_missing_reason: str | None


@dataclass(frozen=True, slots=True)
class AttemptView:
    task_id: str
    attempt_id: str
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
    codex_session: CodexSessionRefView | None
    prompt_receipt: PromptReceiptView | None
    user_prompt_receipt: PromptReceiptView | None
    changed_paths: tuple[str, ...]
    validations: tuple[ValidationView, ...]
    residuals: tuple[str, ...]
    followup_candidates: tuple[str, ...]
    note: str | None
    commit: str | None
    landed_commit: str | None
    is_active: bool
    failure_class: str | None
    recovery_action: str | None
    prompt_issue: bool
    operator_issue: bool
    setup_receipt: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class TaskView:
    task_id: str
    title: str
    created_at: str | None
    status: str
    updated_at: str | None
    actor: str | None
    note: str | None
    attempt_count: int
    latest_attempt_id: str | None
    latest_attempt_status: str | None
    latest_attempt_summary: str | None
    active_attempt_id: str | None
    failure_class: str | None
    recovery_action: str | None
    prompt_issue: bool
    operator_issue: bool


@dataclass(frozen=True, slots=True)
class RuntimeModel:
    schema_version: int
    repository: Repository
    tasks: tuple[TaskView, ...]
    attempts: tuple[AttemptView, ...]
    counts: dict[str, int]
    recent_attempts: tuple[AttemptView, ...]
    events: tuple[dict[str, Any], ...]


def project_repository(profile: RepoProfile) -> Repository:
    return Repository(
        project_name=profile.project_name,
        project_root=profile.paths.project_root,
        control_dir=profile.paths.control_dir,
        runtime_file=profile.paths.runtime_file,
        events_file=profile.paths.events_file,
        validation_commands=profile.validation_commands,
        doc_routing_defaults=profile.doc_routing_defaults,
    )


def _receipt_view(receipt: PromptReceiptRecord | None) -> PromptReceiptView | None:
    if receipt is None:
        return None
    return PromptReceiptView(
        prompt_hash=receipt.prompt_hash,
        recorded_at=receipt.recorded_at,
        source=receipt.source,
        mode=receipt.mode,
        replay_artifact_path=receipt.replay_artifact_path,
    )


def _session_view(session: CodexSessionRefRecord | None) -> CodexSessionRefView | None:
    if session is None:
        return None
    return CodexSessionRefView(
        thread_id=session.thread_id,
        session_path=session.session_path,
        turn_id=session.turn_id,
        turn_started_at=session.turn_started_at,
        user_prompt_hash=session.user_prompt_hash,
        execution_prompt_hash=session.execution_prompt_hash,
        capture_status=session.capture_status,
        capture_method=session.capture_method,
        capture_missing_reason=session.capture_missing_reason,
    )


def _validation_view(validation: ValidationRecord) -> ValidationView:
    return ValidationView(validation.name, validation.status)


def _attempt_view(task_id: str, attempt: TaskAttemptRecord) -> AttemptView:
    return AttemptView(
        task_id=task_id,
        attempt_id=attempt.attempt_id,
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
        codex_session=_session_view(attempt.codex_session),
        prompt_receipt=_receipt_view(attempt.prompt_receipt),
        user_prompt_receipt=_receipt_view(attempt.user_prompt_receipt),
        changed_paths=attempt.changed_paths,
        validations=tuple(_validation_view(item) for item in attempt.validations),
        residuals=attempt.residuals,
        followup_candidates=attempt.followup_candidates,
        note=attempt.note,
        commit=attempt.commit,
        landed_commit=attempt.landed_commit,
        is_active=attempt.status == ATTEMPT_STATUS_IN_PROGRESS,
        failure_class=attempt.failure_class,
        recovery_action=attempt.recovery_action,
        prompt_issue=attempt.prompt_issue,
        operator_issue=attempt.operator_issue,
        setup_receipt=dict(attempt.setup_receipt) if attempt.setup_receipt is not None else None,
    )


def _task_view(task: TaskRecord) -> TaskView:
    latest = task.attempts[-1] if task.attempts else None
    active = next((item for item in reversed(task.attempts) if item.status == ATTEMPT_STATUS_IN_PROGRESS), None)
    return TaskView(
        task_id=task.task_id,
        title=task.title,
        created_at=task.created_at,
        status=task.status,
        updated_at=task.updated_at,
        actor=task.actor,
        note=task.note,
        attempt_count=len(task.attempts),
        latest_attempt_id=latest.attempt_id if latest is not None else None,
        latest_attempt_status=latest.status if latest is not None else None,
        latest_attempt_summary=latest.summary if latest is not None else None,
        active_attempt_id=active.attempt_id if active is not None else None,
        failure_class=task.failure_class,
        recovery_action=task.recovery_action,
        prompt_issue=task.prompt_issue,
        operator_issue=task.operator_issue,
    )


def _counts(tasks: tuple[TaskView, ...], attempts: tuple[AttemptView, ...]) -> dict[str, int]:
    counts = {
        "tasks": len(tasks),
        "planned": 0,
        "in_progress": 0,
        "blocked": 0,
        "done": 0,
        "canceled": 0,
        "attempts": len(attempts),
        "active_attempts": sum(1 for attempt in attempts if attempt.is_active),
    }
    for task in tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
    return counts


def project_runtime_model(
    profile: RepoProfile,
    runtime_state: RuntimeState,
    *,
    events: tuple[dict[str, Any], ...] = (),
) -> RuntimeModel:
    tasks = tuple(_task_view(task) for task in runtime_state.tasks)
    attempts = tuple(
        _attempt_view(task.task_id, attempt)
        for task in runtime_state.tasks
        for attempt in task.attempts
    )
    epoch = parse_iso("1970-01-01T00:00:00+00:00")
    assert epoch is not None
    recent = tuple(
        sorted(
            attempts,
            key=lambda item: (parse_iso(item.ended_at or item.started_at) or epoch).timestamp(),
            reverse=True,
        )
    )
    return RuntimeModel(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        repository=project_repository(profile),
        tasks=tasks,
        attempts=attempts,
        counts=_counts(tasks, attempts),
        recent_attempts=recent,
        events=events,
    )


def load_runtime_model(profile: RepoProfile) -> RuntimeModel:
    return project_runtime_model(
        profile,
        load_runtime_state(profile.paths),
        events=load_events(profile.paths.events_file),
    )


__all__ = [
    "AttemptView",
    "CodexSessionRefView",
    "PromptReceiptView",
    "Repository",
    "RuntimeModel",
    "SNAPSHOT_SCHEMA_VERSION",
    "TaskView",
    "ValidationView",
    "load_runtime_model",
    "project_repository",
    "project_runtime_model",
]
