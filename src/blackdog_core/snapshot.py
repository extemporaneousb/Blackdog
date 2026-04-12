"""Read models and renderers for the vNext Blackdog runtime."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .profile import RepoProfile
from .runtime_model import RuntimeModel, TaskView, WorksetView, load_runtime_model
from .state import now_iso


SNAPSHOT_FORMAT = "blackdog.snapshot/vnext1"


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


def runtime_model_snapshot(model: RuntimeModel) -> dict[str, Any]:
    payload = _jsonable(model)
    if not isinstance(payload, dict):
        raise TypeError("runtime model payload must serialize to a dict")
    return payload


def build_runtime_snapshot(profile: RepoProfile) -> dict[str, Any]:
    model = load_runtime_model(profile)
    return {
        "schema_version": model.schema_version,
        "format": SNAPSHOT_FORMAT,
        "generated_at": now_iso(),
        "runtime_model": runtime_model_snapshot(model),
    }


def build_runtime_summary(profile: RepoProfile) -> dict[str, Any]:
    model = load_runtime_model(profile)
    return {
        "project_name": model.repository.project_name,
        "counts": dict(model.counts),
        "worksets": [
            {
                "id": workset.workset_id,
                "title": workset.title,
                "counts": dict(workset.counts),
                "claim": _jsonable(workset.claim),
                "target_branch": workset.branch_intent.get("target_branch"),
                "integration_branch": workset.branch_intent.get("integration_branch"),
                "workspace": dict(workset.workspace),
                "next_task_ids": list(workset.next_task_ids),
                "recent_attempts": [
                    {
                        "attempt_id": attempt.attempt_id,
                        "task_id": attempt.task_id,
                        "status": attempt.status,
                        "actor": attempt.actor,
                        "worktree_role": attempt.worktree_role,
                        "branch": attempt.branch,
                        "start_commit": attempt.start_commit,
                        "execution_model": attempt.execution_model,
                        "prompt_hash": attempt.prompt_receipt.prompt_hash if attempt.prompt_receipt else None,
                        "summary": attempt.summary,
                        "elapsed_seconds": attempt.elapsed_seconds,
                    }
                    for attempt in workset.attempts[:3]
                ],
            }
            for workset in model.worksets
        ],
        "recent_attempts": [
            {
                "attempt_id": attempt.attempt_id,
                "task_id": attempt.task_id,
                "status": attempt.status,
                "actor": attempt.actor,
                "worktree_role": attempt.worktree_role,
                "branch": attempt.branch,
                "start_commit": attempt.start_commit,
                "execution_model": attempt.execution_model,
                "prompt_hash": attempt.prompt_receipt.prompt_hash if attempt.prompt_receipt else None,
                "summary": attempt.summary,
                "elapsed_seconds": attempt.elapsed_seconds,
            }
            for attempt in model.recent_attempts[:5]
        ],
    }


def _task_label(task: TaskView) -> str:
    if task.readiness == "blocked" and task.blocked_by:
        return f"{task.task_id} {task.title} ({', '.join(task.blocked_by)})"
    return f"{task.task_id} {task.title}"


def render_summary_text(model: RuntimeModel) -> str:
    lines = [
        f"Project: {model.repository.project_name}",
        f"Worksets: {model.counts['worksets']}",
        f"Tasks: {model.counts['tasks']}",
        f"Ready: {model.counts['ready']} | In progress: {model.counts['in_progress']} | Blocked: {model.counts['blocked']} | Done: {model.counts['done']}",
        f"Claimed worksets: {model.counts['claimed_worksets']} | Claimed tasks: {model.counts['claimed_tasks']}",
        f"Attempts: {model.counts['attempts']} | Active attempts: {model.counts['active_attempts']}",
    ]
    if not model.worksets:
        lines.append("")
        lines.append("No worksets have been defined.")
        return "\n".join(lines)
    for workset in model.worksets:
        lines.append("")
        lines.append(f"{workset.workset_id}: {workset.title}")
        target_branch = workset.branch_intent.get("target_branch") or "unset"
        integration_branch = workset.branch_intent.get("integration_branch") or "unset"
        workspace_identity = workset.workspace.get("identity") or "unset"
        claim_detail = (
            f" claim={workset.claim.actor}/{workset.claim.execution_model}"
            if workset.claim is not None
            else ""
        )
        lines.append(
            f"  target_branch={target_branch} integration_branch={integration_branch} workspace={workspace_identity}{claim_detail}"
        )
        lines.append(
            f"  ready={workset.counts['ready']} in_progress={workset.counts['in_progress']} blocked={workset.counts['blocked']} done={workset.counts['done']} claimed_tasks={workset.counts['claimed_tasks']} attempts={workset.counts['attempts']}"
        )
        for task in workset.tasks:
            detail = ""
            if task.latest_attempt_status:
                detail = f" latest_attempt={task.latest_attempt_status}"
            if task.claim_actor:
                detail = f"{detail} claim={task.claim_actor}/{task.claim_execution_model}"
            lines.append(f"  [{task.readiness.upper()}] {_task_label(task)}{detail}")
        if workset.attempts:
            lines.append("  Recent attempts:")
            for attempt in workset.attempts[:3]:
                detail = attempt.summary or attempt.note or ""
                elapsed = f" elapsed={attempt.elapsed_seconds}s" if attempt.elapsed_seconds is not None else ""
                branch = f" branch={attempt.branch}" if attempt.branch else ""
                worktree = f" worktree={attempt.worktree_role}" if attempt.worktree_role else ""
                execution_model = f" exec={attempt.execution_model}" if attempt.execution_model else ""
                prompt_hash = (
                    f" prompt={attempt.prompt_receipt.prompt_hash[:10]}"
                    if attempt.prompt_receipt is not None
                    else ""
                )
                lines.append(
                    (
                        f"    - {attempt.attempt_id} task={attempt.task_id} status={attempt.status} "
                        f"actor={attempt.actor}{branch}{worktree}{execution_model}{prompt_hash}{elapsed} {detail}"
                    ).rstrip()
                )
    return "\n".join(lines)


def render_next_text(model: RuntimeModel) -> str:
    if not model.next_tasks:
        return "No ready tasks."
    lines = []
    for task in model.next_tasks:
        lines.append(f"{task.task_id} {task.title}")
    return "\n".join(lines)


__all__ = [
    "SNAPSHOT_FORMAT",
    "RuntimeModel",
    "build_runtime_snapshot",
    "build_runtime_summary",
    "load_runtime_model",
    "render_next_text",
    "render_summary_text",
    "runtime_model_snapshot",
]
