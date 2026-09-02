"""Stable JSON and text projections of the flat task read model."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .profile import RepoProfile
from .runtime_model import AttemptView, RuntimeModel, TaskView, load_runtime_model


SNAPSHOT_FORMAT = "blackdog.snapshot/v2"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _bounded_skill_provenance(setup_receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not setup_receipt:
        return None
    raw = setup_receipt.get("skill_provenance")
    if not isinstance(raw, dict):
        return None
    return {
        key: raw.get(key)
        for key in ("schema_version", "path", "sha256", "source")
        if raw.get(key) is not None
    }


def _task_payload(task: TaskView) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "created_at": task.created_at,
        "status": task.status,
        "updated_at": task.updated_at,
        "actor": task.actor,
        "note": task.note,
        "attempt_count": task.attempt_count,
        "latest_attempt_id": task.latest_attempt_id,
        "latest_attempt_status": task.latest_attempt_status,
        "latest_attempt_summary": task.latest_attempt_summary,
        "active_attempt_id": task.active_attempt_id,
        "failure_class": task.failure_class,
        "recovery_action": task.recovery_action,
        "prompt_issue": task.prompt_issue,
        "operator_issue": task.operator_issue,
    }


def _attempt_payload(attempt: AttemptView) -> dict[str, Any]:
    execution_receipt = attempt.prompt_receipt
    user_receipt = attempt.user_prompt_receipt
    payload = {
        "task_id": attempt.task_id,
        "attempt_id": attempt.attempt_id,
        "status": attempt.status,
        "actor": attempt.actor,
        "started_at": attempt.started_at,
        "ended_at": attempt.ended_at,
        "elapsed_seconds": attempt.elapsed_seconds,
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
        "codex_session": _jsonable(attempt.codex_session),
        "execution_prompt_hash": execution_receipt.prompt_hash if execution_receipt else None,
        "execution_prompt_source": execution_receipt.source if execution_receipt else None,
        "execution_prompt_mode": execution_receipt.mode if execution_receipt else None,
        "execution_prompt_replay_artifact_path": (
            execution_receipt.replay_artifact_path if execution_receipt else None
        ),
        "user_prompt_hash": user_receipt.prompt_hash if user_receipt else None,
        "user_prompt_source": user_receipt.source if user_receipt else None,
        "user_prompt_mode": user_receipt.mode if user_receipt else None,
        "user_prompt_replay_artifact_path": user_receipt.replay_artifact_path if user_receipt else None,
        "changed_paths": list(attempt.changed_paths),
        "validations": [_jsonable(item) for item in attempt.validations],
        "residuals": list(attempt.residuals),
        "followup_candidates": list(attempt.followup_candidates),
        "note": attempt.note,
        "commit": attempt.commit,
        "landed_commit": attempt.landed_commit,
        "failure_class": attempt.failure_class,
        "recovery_action": attempt.recovery_action,
        "prompt_issue": attempt.prompt_issue,
        "operator_issue": attempt.operator_issue,
        "setup_receipt": _jsonable(attempt.setup_receipt),
        "skill_provenance": _bounded_skill_provenance(attempt.setup_receipt),
    }
    return payload


def runtime_model_snapshot(model: RuntimeModel) -> dict[str, Any]:
    return {
        "format": SNAPSHOT_FORMAT,
        "schema_version": model.schema_version,
        "repository": _jsonable(model.repository),
        "counts": dict(model.counts),
        "tasks": [_task_payload(task) for task in model.tasks],
        "attempts": [_attempt_payload(attempt) for attempt in model.attempts],
        "recent_attempts": [_attempt_payload(attempt) for attempt in model.recent_attempts],
        "events": [_jsonable(event) for event in model.events],
    }


def build_runtime_snapshot(profile: RepoProfile) -> dict[str, Any]:
    return runtime_model_snapshot(load_runtime_model(profile))


def build_runtime_summary(profile: RepoProfile) -> dict[str, Any]:
    model = load_runtime_model(profile)
    return {
        "format": SNAPSHOT_FORMAT,
        "repository": model.repository.project_name,
        "counts": dict(model.counts),
        "tasks": [_task_payload(task) for task in model.tasks],
        "recent_attempts": [_attempt_payload(attempt) for attempt in model.recent_attempts[:10]],
    }


ATTEMPTS_TABLE_COLUMNS = (
    "task_id",
    "attempt_id",
    "status",
    "actor",
    "started_at",
    "ended_at",
    "elapsed_seconds",
    "execution_model",
    "model",
    "reasoning_effort",
    "codex_thread_id",
    "branch",
    "target_branch",
    "start_commit",
    "commit",
    "landed_commit",
    "execution_prompt_hash",
    "user_prompt_hash",
    "changed_paths_count",
    "validation_summary",
    "failure_class",
    "recovery_action",
    "prompt_issue",
    "operator_issue",
    "summary",
)


def _validation_summary(attempt: AttemptView) -> str:
    return ",".join(f"{item.name}={item.status}" for item in attempt.validations)


def _attempt_table_row(attempt: AttemptView) -> dict[str, Any]:
    execution_receipt = attempt.prompt_receipt
    user_receipt = attempt.user_prompt_receipt
    return {
        "task_id": attempt.task_id,
        "attempt_id": attempt.attempt_id,
        "status": attempt.status,
        "actor": attempt.actor,
        "started_at": attempt.started_at,
        "ended_at": attempt.ended_at,
        "elapsed_seconds": attempt.elapsed_seconds,
        "execution_model": attempt.execution_model,
        "model": attempt.model,
        "reasoning_effort": attempt.reasoning_effort,
        "codex_thread_id": attempt.codex_session.thread_id if attempt.codex_session else None,
        "branch": attempt.branch,
        "target_branch": attempt.target_branch,
        "start_commit": attempt.start_commit,
        "commit": attempt.commit,
        "landed_commit": attempt.landed_commit,
        "execution_prompt_hash": execution_receipt.prompt_hash if execution_receipt else None,
        "user_prompt_hash": user_receipt.prompt_hash if user_receipt else None,
        "changed_paths_count": len(attempt.changed_paths),
        "validation_summary": _validation_summary(attempt),
        "failure_class": attempt.failure_class,
        "recovery_action": attempt.recovery_action,
        "prompt_issue": attempt.prompt_issue,
        "operator_issue": attempt.operator_issue,
        "summary": attempt.summary,
    }


def build_attempts_table(profile: RepoProfile) -> dict[str, Any]:
    model = load_runtime_model(profile)
    rows = [_attempt_table_row(attempt) for attempt in model.recent_attempts]
    return {"columns": list(ATTEMPTS_TABLE_COLUMNS), "rows": rows, "counts": dict(model.counts)}


def build_attempts_summary(profile: RepoProfile) -> dict[str, Any]:
    model = load_runtime_model(profile)
    completed = [attempt for attempt in model.recent_attempts if not attempt.is_active]
    status_counts: dict[str, int] = {}
    elapsed = 0
    elapsed_count = 0
    for attempt in completed:
        status_counts[attempt.status] = status_counts.get(attempt.status, 0) + 1
        if attempt.elapsed_seconds is not None:
            elapsed += attempt.elapsed_seconds
            elapsed_count += 1
    return {
        "counts": dict(model.counts),
        "completed_attempts": len(completed),
        "status_counts": status_counts,
        "mean_elapsed_seconds": elapsed / elapsed_count if elapsed_count else None,
        "recent_attempts": [_attempt_payload(attempt) for attempt in completed[:10]],
    }


def render_summary_text(model: RuntimeModel) -> str:
    counts = model.counts
    lines = [
        f"Blackdog: {model.repository.project_name}",
        (
            f"Tasks: total={counts.get('tasks', 0)} planned={counts.get('planned', 0)} "
            f"in_progress={counts.get('in_progress', 0)} blocked={counts.get('blocked', 0)} "
            f"done={counts.get('done', 0)} canceled={counts.get('canceled', 0)}"
        ),
        f"Attempts: total={counts.get('attempts', 0)} active={counts.get('active_attempts', 0)}",
    ]
    if model.tasks:
        lines.append("Tasks:")
        for task in model.tasks:
            lines.append(f"  - [{task.status.upper()}] {task.task_id} {task.title}")
    return "\n".join(lines)


def render_attempts_summary_text(payload: dict[str, Any]) -> str:
    counts = payload.get("status_counts") or {}
    statuses = " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    mean = payload.get("mean_elapsed_seconds")
    return (
        f"Completed attempts: {payload.get('completed_attempts', 0)}"
        + (f" | {statuses}" if statuses else "")
        + (f" | mean_elapsed={mean:.1f}s" if isinstance(mean, (int, float)) else "")
    )


def render_attempts_table_text(payload: dict[str, Any]) -> str:
    columns = tuple(payload.get("columns") or ATTEMPTS_TABLE_COLUMNS)
    rows = payload.get("rows") or []
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join("" if row.get(column) is None else str(row.get(column)) for column in columns))
    return "\n".join(lines)


__all__ = [
    "SNAPSHOT_FORMAT",
    "ATTEMPTS_TABLE_COLUMNS",
    "build_runtime_snapshot",
    "build_runtime_summary",
    "build_attempts_summary",
    "build_attempts_table",
    "load_runtime_model",
    "render_attempts_summary_text",
    "render_attempts_table_text",
    "render_summary_text",
    "runtime_model_snapshot",
]
