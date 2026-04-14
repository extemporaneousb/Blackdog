from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any
import shlex

from blackdog_core.backlog import BacklogError, claim_workset_manager, release_workset_manager
from blackdog_core.profile import RepoProfile
from blackdog_core.runtime_model import AttemptView, TaskView, WorksetView, load_runtime_model, scope_runtime_model
from blackdog_core.state import EXECUTION_MODEL_WORKSET_MANAGER, append_event


class SupervisorError(RuntimeError):
    pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _task_payload(task: TaskView) -> dict[str, Any]:
    return {
        "workset_id": task.workset_id,
        "task_id": task.task_id,
        "title": task.title,
        "intent": task.intent,
        "description": task.description,
        "runtime_status": task.runtime_status,
        "readiness": task.readiness,
        "blocked_by": list(task.blocked_by),
        "claim_actor": task.claim_actor,
        "claim_execution_model": task.claim_execution_model,
        "claimed_at": task.claimed_at,
        "latest_attempt_id": task.latest_attempt_id,
        "latest_attempt_status": task.latest_attempt_status,
        "latest_attempt_summary": task.latest_attempt_summary,
        "active_attempt_id": task.active_attempt_id,
        "paths": list(task.paths),
        "docs": list(task.docs),
        "checks": list(task.checks),
        "metadata": dict(task.metadata),
    }


def _attempt_payload(attempt: AttemptView) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
        "task_id": attempt.task_id,
        "status": attempt.status,
        "actor": attempt.actor,
        "summary": attempt.summary,
        "execution_model": attempt.execution_model,
        "branch": attempt.branch,
        "worktree_path": attempt.worktree_path,
        "started_at": attempt.started_at,
        "ended_at": attempt.ended_at,
        "changed_paths": list(attempt.changed_paths),
    }


def _worker_request(workset: WorksetView, task: TaskView) -> str:
    lines = [
        f"Execute task {task.task_id}: {task.title}",
        f"Workset: {workset.workset_id} {workset.title}",
        f"Intent: {task.intent}",
    ]
    if task.description:
        lines.append(f"Description: {task.description}")
    if task.paths:
        lines.append(f"Focus paths: {', '.join(task.paths)}")
    if task.docs:
        lines.append(f"Review docs: {', '.join(task.docs)}")
    if task.checks:
        lines.append(f"Run checks: {', '.join(task.checks)}")
    lines.extend(
        [
            "Stay inside this task's scope.",
            "If the landed result would force upstream correction or task reshaping, stop and report back instead of broadening scope silently.",
            "Use `blackdog task begin` against the planned workset/task before making kept changes.",
        ]
    )
    return "\n".join(lines)


def _task_begin_command(profile: RepoProfile, task: TaskView) -> str:
    return (
        f"./.VE/bin/blackdog task begin --project-root {shlex.quote(str(profile.paths.project_root))} "
        f"--workset {shlex.quote(task.workset_id)} --task {shlex.quote(task.task_id)} "
        "--actor WORKER --prompt-file PROMPT.txt"
    )


@dataclass(frozen=True, slots=True)
class SupervisorDispatch:
    task_id: str
    title: str
    intent: str
    worker_request: str
    task_begin_command: str
    paths: tuple[str, ...]
    docs: tuple[str, ...]
    checks: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


@dataclass(frozen=True, slots=True)
class SupervisorStatus:
    action: str
    project_name: str
    project_root: str
    workset_id: str
    workset_title: str
    phase: str
    parallelism: int
    available_slots: int
    supervisor_active: bool
    claim: dict[str, Any] | None
    counts: dict[str, int]
    active_tasks: tuple[dict[str, Any], ...]
    ready_tasks: tuple[dict[str, Any], ...]
    blocked_tasks: tuple[dict[str, Any], ...]
    dispatches: tuple[SupervisorDispatch, ...]
    recent_attempts: tuple[dict[str, Any], ...]
    recommended_actions: tuple[str, ...]
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = _jsonable(self)
        if not isinstance(payload, dict):
            raise TypeError("supervisor payload must serialize to a dict")
        return payload


def _supervisor_workset(profile: RepoProfile, *, workset_id: str) -> WorksetView:
    model = scope_runtime_model(load_runtime_model(profile), workset_id=workset_id)
    return model.worksets[0]


def _ensure_parallelism(value: int) -> int:
    if value < 1:
        raise SupervisorError("parallelism must be at least 1")
    return value


def _build_dispatches(
    profile: RepoProfile,
    workset: WorksetView,
    *,
    available_slots: int,
) -> tuple[SupervisorDispatch, ...]:
    if available_slots <= 0:
        return ()
    ready_tasks = [task for task in workset.tasks if task.is_ready]
    dispatches: list[SupervisorDispatch] = []
    for task in ready_tasks[:available_slots]:
        dispatches.append(
            SupervisorDispatch(
                task_id=task.task_id,
                title=task.title,
                intent=task.intent,
                worker_request=_worker_request(workset, task),
                task_begin_command=_task_begin_command(profile, task),
                paths=task.paths,
                docs=task.docs,
                checks=task.checks,
            )
        )
    return tuple(dispatches)


def _supervisor_phase(
    *,
    supervisor_active: bool,
    workset: WorksetView,
    active_tasks: list[TaskView],
    ready_tasks: list[TaskView],
    dispatches: tuple[SupervisorDispatch, ...],
) -> str:
    if workset.claim is not None and workset.claim.execution_model != EXECUTION_MODEL_WORKSET_MANAGER:
        return "occupied"
    if workset.counts["done"] == workset.counts["tasks"] and not active_tasks:
        return "complete"
    if active_tasks and dispatches:
        return "dispatch_and_monitor"
    if active_tasks:
        return "monitor"
    if dispatches:
        return "dispatch"
    if ready_tasks:
        return "ready"
    if supervisor_active:
        return "blocked"
    return "idle"


def _recommended_actions(
    *,
    profile: RepoProfile,
    workset: WorksetView,
    phase: str,
    supervisor_active: bool,
    dispatches: tuple[SupervisorDispatch, ...],
    active_tasks: list[TaskView],
) -> tuple[str, ...]:
    actions: list[str] = []
    if phase == "occupied" and workset.claim is not None:
        actions.append(
            f"Resolve the existing workset claim owned by {workset.claim.actor}/{workset.claim.execution_model} "
            "before starting supervisor mode."
        )
        return tuple(actions)
    if not supervisor_active:
        actions.append(
            f"Run `./.VE/bin/blackdog supervisor start --project-root {profile.paths.project_root} "
            f"--workset {workset.workset_id} --actor SUPERVISOR` to claim this workset for managed execution."
        )
    if dispatches:
        actions.append(f"Launch {len(dispatches)} worker task(s) from the dispatch set below.")
    if active_tasks:
        actions.append("Monitor active worker tasks and review land/close results before dispatching replacements.")
    if phase == "blocked":
        actions.append("Replan blocked work or patch runtime state before continuing this workset.")
    if phase == "complete" and supervisor_active:
        actions.append("Release the supervisor claim after summarizing the completed workset.")
    return tuple(actions)


def show_supervisor(
    profile: RepoProfile,
    *,
    workset_id: str,
    parallelism: int = 1,
    action: str = "show",
    note: str | None = None,
) -> SupervisorStatus:
    resolved_parallelism = _ensure_parallelism(parallelism)
    workset = _supervisor_workset(profile, workset_id=workset_id)
    active_tasks = [
        task
        for task in workset.tasks
        if task.active_attempt_id is not None or task.runtime_status == "in_progress" or task.claim_actor is not None
    ]
    ready_tasks = [task for task in workset.tasks if task.is_ready]
    available_slots = max(0, resolved_parallelism - len(active_tasks))
    dispatches = _build_dispatches(profile, workset, available_slots=available_slots)
    supervisor_active = workset.claim is not None and workset.claim.execution_model == EXECUTION_MODEL_WORKSET_MANAGER
    phase = _supervisor_phase(
        supervisor_active=supervisor_active,
        workset=workset,
        active_tasks=active_tasks,
        ready_tasks=ready_tasks,
        dispatches=dispatches,
    )
    return SupervisorStatus(
        action=action,
        project_name=profile.project_name,
        project_root=str(profile.paths.project_root),
        workset_id=workset.workset_id,
        workset_title=workset.title,
        phase=phase,
        parallelism=resolved_parallelism,
        available_slots=available_slots,
        supervisor_active=supervisor_active,
        claim=_jsonable(workset.claim),
        counts=dict(workset.counts),
        active_tasks=tuple(_task_payload(task) for task in active_tasks),
        ready_tasks=tuple(_task_payload(task) for task in ready_tasks),
        blocked_tasks=tuple(_task_payload(task) for task in workset.tasks if task.readiness == "blocked"),
        dispatches=dispatches,
        recent_attempts=tuple(_attempt_payload(attempt) for attempt in workset.attempts[:5]),
        recommended_actions=_recommended_actions(
            profile=profile,
            workset=workset,
            phase=phase,
            supervisor_active=supervisor_active,
            dispatches=dispatches,
            active_tasks=active_tasks,
        ),
        note=note,
    )


def start_supervisor(
    profile: RepoProfile,
    *,
    workset_id: str,
    actor: str,
    parallelism: int = 1,
    note: str | None = None,
) -> SupervisorStatus:
    claim_workset_manager(profile, workset_id=workset_id, actor=actor, note=note)
    return show_supervisor(
        profile,
        workset_id=workset_id,
        parallelism=parallelism,
        action="start",
        note=note,
    )


def checkpoint_supervisor(
    profile: RepoProfile,
    *,
    workset_id: str,
    actor: str,
    parallelism: int = 1,
    note: str | None = None,
) -> SupervisorStatus:
    status = show_supervisor(
        profile,
        workset_id=workset_id,
        parallelism=parallelism,
        action="checkpoint",
        note=note,
    )
    if status.claim is None or status.claim.get("execution_model") != EXECUTION_MODEL_WORKSET_MANAGER:
        raise SupervisorError(f"Workset {workset_id!r} is not currently claimed for supervisor execution")
    if status.claim.get("actor") != actor:
        raise SupervisorError(f"Workset {workset_id!r} is supervised by {status.claim.get('actor')}, not {actor}")
    append_event(
        profile.paths.events_file,
        event_type="supervisor.checkpoint",
        actor=actor,
        payload={
            "workset_id": status.workset_id,
            "parallelism": status.parallelism,
            "phase": status.phase,
            "available_slots": status.available_slots,
            "ready_task_ids": [item["task_id"] for item in status.ready_tasks],
            "active_task_ids": [item["task_id"] for item in status.active_tasks],
            "dispatch_task_ids": [item.task_id for item in status.dispatches],
            "note": note,
        },
    )
    return status


def release_supervisor(
    profile: RepoProfile,
    *,
    workset_id: str,
    actor: str,
    summary: str | None = None,
    parallelism: int = 1,
    note: str | None = None,
) -> SupervisorStatus:
    release_workset_manager(profile, workset_id=workset_id, actor=actor, summary=summary, note=note)
    return show_supervisor(
        profile,
        workset_id=workset_id,
        parallelism=parallelism,
        action="release",
        note=summary or note,
    )


def render_supervisor_text(status: SupervisorStatus) -> str:
    lines = [
        f"[blackdog-supervisor] workset: {status.workset_id} {status.workset_title}",
        f"[blackdog-supervisor] action: {status.action}",
        f"[blackdog-supervisor] phase: {status.phase}",
        f"[blackdog-supervisor] parallelism: {status.parallelism} available_slots={status.available_slots}",
    ]
    if status.claim is not None:
        lines.append(
            "[blackdog-supervisor] claim: "
            f"{status.claim.get('actor')}/{status.claim.get('execution_model')} "
            f"claimed_at={status.claim.get('claimed_at')}"
        )
    else:
        lines.append("[blackdog-supervisor] claim: none")
    lines.append(
        "[blackdog-supervisor] counts: "
        f"tasks={status.counts['tasks']} ready={status.counts['ready']} "
        f"in_progress={status.counts['in_progress']} blocked={status.counts['blocked']} "
        f"done={status.counts['done']} attempts={status.counts['attempts']}"
    )
    if status.dispatches:
        lines.append("")
        lines.append("Dispatch:")
        for dispatch in status.dispatches:
            lines.append(f"  - {dispatch.task_id} {dispatch.title}")
            lines.append(f"    begin: {dispatch.task_begin_command}")
    if status.active_tasks:
        lines.append("")
        lines.append("Active:")
        for task in status.active_tasks:
            label = f"  - {task['task_id']} {task['title']}"
            if task["active_attempt_id"]:
                label = f"{label} attempt={task['active_attempt_id']}"
            if task["claim_actor"]:
                label = f"{label} claim={task['claim_actor']}/{task['claim_execution_model']}"
            lines.append(label)
    if status.blocked_tasks:
        lines.append("")
        lines.append("Blocked:")
        for task in status.blocked_tasks[:10]:
            detail = ", ".join(task["blocked_by"]) if task["blocked_by"] else task["runtime_status"]
            lines.append(f"  - {task['task_id']} {task['title']} ({detail})")
        if len(status.blocked_tasks) > 10:
            lines.append(f"  ... {len(status.blocked_tasks) - 10} more blocked task(s)")
    if status.recommended_actions:
        lines.append("")
        lines.append("Recommended actions:")
        lines.extend(f"  - {item}" for item in status.recommended_actions)
    return "\n".join(lines)


__all__ = [
    "SupervisorDispatch",
    "SupervisorError",
    "SupervisorStatus",
    "checkpoint_supervisor",
    "release_supervisor",
    "render_supervisor_text",
    "show_supervisor",
    "start_supervisor",
]
