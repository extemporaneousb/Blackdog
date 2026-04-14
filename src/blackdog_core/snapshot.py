"""Read models and renderers for the vNext Blackdog runtime."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .profile import RepoProfile
from .runtime_model import AttemptView, RuntimeModel, TaskView, WorksetView, load_runtime_model, scope_runtime_model
from .state import now_iso, parse_iso


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


def _scoped_model(profile: RepoProfile, *, workset_id: str | None = None) -> RuntimeModel:
    return scope_runtime_model(load_runtime_model(profile), workset_id=workset_id)


def build_runtime_snapshot(profile: RepoProfile, *, workset_id: str | None = None) -> dict[str, Any]:
    model = _scoped_model(profile, workset_id=workset_id)
    return {
        "schema_version": model.schema_version,
        "format": SNAPSHOT_FORMAT,
        "generated_at": now_iso(),
        "runtime_model": runtime_model_snapshot(model),
    }


def build_runtime_summary(profile: RepoProfile, *, workset_id: str | None = None) -> dict[str, Any]:
    model = _scoped_model(profile, workset_id=workset_id)
    return {
        "project_name": model.repository.project_name,
        "workset_scope": workset_id,
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
                        "summary": attempt.summary,
                        "elapsed_seconds": attempt.elapsed_seconds,
                        **_prompt_lineage_payload(attempt),
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
                "summary": attempt.summary,
                "elapsed_seconds": attempt.elapsed_seconds,
                **_prompt_lineage_payload(attempt),
            }
            for attempt in model.recent_attempts[:5]
        ],
    }


ATTEMPTS_TABLE_COLUMNS = (
    "workset_id",
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
    "execution_prompt_source",
    "user_prompt_source",
    "prompt_source",
    "execution_prompt_mode",
    "user_prompt_mode",
    "prompt_mode",
    "branch",
    "target_branch",
    "start_commit",
    "commit",
    "landed_commit",
    "execution_prompt_hash",
    "user_prompt_hash",
    "prompt_hash",
    "changed_paths_count",
    "validation_summary",
    "summary",
)


def _prompt_lineage_payload(attempt: AttemptView) -> dict[str, Any]:
    execution_prompt = attempt.prompt_receipt
    user_prompt = attempt.user_prompt_receipt or execution_prompt
    return {
        "execution_prompt_source": execution_prompt.source if execution_prompt else None,
        "execution_prompt_hash": execution_prompt.prompt_hash if execution_prompt else None,
        "execution_prompt_mode": execution_prompt.mode if execution_prompt else None,
        "user_prompt_source": user_prompt.source if user_prompt else None,
        "user_prompt_hash": user_prompt.prompt_hash if user_prompt else None,
        "user_prompt_mode": user_prompt.mode if user_prompt else None,
        "prompt_source": execution_prompt.source if execution_prompt else None,
        "prompt_hash": execution_prompt.prompt_hash if execution_prompt else None,
        "prompt_mode": execution_prompt.mode if execution_prompt else None,
    }


def _prompt_receipt_label(source: str | None, prompt_hash: str | None, mode: str | None) -> str | None:
    if prompt_hash is None:
        return None
    label = prompt_hash[:10]
    if source:
        label = f"{source}:{label}"
    if mode:
        label = f"{label}/{mode}"
    return label


def _attempt_prompt_lineage_text(attempt: AttemptView) -> str:
    payload = _prompt_lineage_payload(attempt)
    execution_label = _prompt_receipt_label(
        payload["execution_prompt_source"],
        payload["execution_prompt_hash"],
        payload["execution_prompt_mode"],
    )
    user_label = _prompt_receipt_label(
        payload["user_prompt_source"],
        payload["user_prompt_hash"],
        payload["user_prompt_mode"],
    )
    if execution_label is None:
        return ""
    if user_label is None or user_label == execution_label:
        return f" prompt={execution_label}"
    return f" user_prompt={user_label} execution_prompt={execution_label}"


def _completed_attempt_items(model: RuntimeModel) -> list[tuple[WorksetView, AttemptView]]:
    rows = [
        (workset, attempt)
        for workset in model.worksets
        for attempt in workset.attempts
        if not attempt.is_active
    ]
    rows.sort(
        key=lambda item: (
            parse_iso(item[1].ended_at or item[1].started_at)
            or parse_iso("1970-01-01T00:00:00+00:00")
        ).timestamp(),
        reverse=True,
    )
    return rows


def _validation_summary(attempt: AttemptView) -> str:
    if not attempt.validations:
        return "none"
    passed = sum(1 for item in attempt.validations if item.status == "passed")
    failed = sum(1 for item in attempt.validations if item.status == "failed")
    skipped = sum(1 for item in attempt.validations if item.status == "skipped")
    return f"passed={passed} failed={failed} skipped={skipped}"


def build_attempts_table(profile: RepoProfile, *, workset_id: str | None = None) -> dict[str, Any]:
    model = _scoped_model(profile, workset_id=workset_id)
    rows = []
    for workset, attempt in _completed_attempt_items(model):
        rows.append(
            {
                "workset_id": workset.workset_id,
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
                "branch": attempt.branch,
                "target_branch": attempt.target_branch,
                "start_commit": attempt.start_commit,
                "commit": attempt.commit,
                "landed_commit": attempt.landed_commit,
                "changed_paths_count": len(attempt.changed_paths),
                "validation_summary": _validation_summary(attempt),
                "summary": attempt.summary,
                **_prompt_lineage_payload(attempt),
            }
        )
    return {
        "project_name": model.repository.project_name,
        "workset_scope": workset_id,
        "columns": list(ATTEMPTS_TABLE_COLUMNS),
        "rows": rows,
    }


def build_attempts_summary(profile: RepoProfile, *, workset_id: str | None = None) -> dict[str, Any]:
    model = _scoped_model(profile, workset_id=workset_id)
    completed = _completed_attempt_items(model)
    validation_totals = {"passed": 0, "failed": 0, "skipped": 0}
    by_workset = []
    landed_total = 0
    not_landed_total = 0
    for workset in model.worksets:
        attempts = [attempt for item_workset, attempt in completed if item_workset.workset_id == workset.workset_id]
        landed = sum(1 for attempt in attempts if attempt.landed_commit)
        not_landed = len(attempts) - landed
        by_workset.append(
            {
                "workset_id": workset.workset_id,
                "title": workset.title,
                "completed_attempts": len(attempts),
                "landed": landed,
                "not_landed": not_landed,
            }
        )
    for _, attempt in completed:
        if attempt.landed_commit:
            landed_total += 1
        else:
            not_landed_total += 1
        for validation in attempt.validations:
            validation_totals[validation.status] = validation_totals.get(validation.status, 0) + 1
    return {
        "project_name": model.repository.project_name,
        "workset_scope": workset_id,
        "counts": {
            "completed_attempts": len(completed),
            "landed": landed_total,
            "not_landed": not_landed_total,
            "validation_passed": validation_totals["passed"],
            "validation_failed": validation_totals["failed"],
            "validation_skipped": validation_totals["skipped"],
        },
        "worksets": by_workset,
        "recent_completed_attempts": [
            {
                "workset_id": workset.workset_id,
                "task_id": attempt.task_id,
                "attempt_id": attempt.attempt_id,
                "status": attempt.status,
                "actor": attempt.actor,
                "execution_model": attempt.execution_model,
                "model": attempt.model,
                "reasoning_effort": attempt.reasoning_effort,
                "branch": attempt.branch,
                "commit": attempt.commit,
                "landed_commit": attempt.landed_commit,
                "validation_summary": _validation_summary(attempt),
                "elapsed_seconds": attempt.elapsed_seconds,
                "summary": attempt.summary,
                **_prompt_lineage_payload(attempt),
            }
            for workset, attempt in completed[:10]
        ],
    }


def _task_payload(task: TaskView) -> dict[str, Any]:
    return {
        "workset_id": task.workset_id,
        "task_id": task.task_id,
        "title": task.title,
        "intent": task.intent,
        "runtime_status": task.runtime_status,
        "readiness": task.readiness,
        "blocked_by": list(task.blocked_by),
        "claim_actor": task.claim_actor,
        "claim_execution_model": task.claim_execution_model,
        "latest_attempt_id": task.latest_attempt_id,
        "latest_attempt_status": task.latest_attempt_status,
        "latest_attempt_summary": task.latest_attempt_summary,
        "active_attempt_id": task.active_attempt_id,
    }


def build_next_payload(model: RuntimeModel, *, workset_id: str) -> dict[str, Any]:
    scoped = scope_runtime_model(model, workset_id=workset_id)
    workset = scoped.worksets[0]
    active_tasks = [
        task
        for task in workset.tasks
        if task.active_attempt_id is not None or task.runtime_status == "in_progress" or task.claim_actor is not None
    ]
    ready_tasks = [task for task in workset.tasks if task.is_ready]
    blocked_tasks = [task for task in workset.tasks if task.readiness == "blocked"]
    if active_tasks:
        selection_mode = "continue"
        selected_task = active_tasks[0]
    elif ready_tasks:
        selection_mode = "start"
        selected_task = ready_tasks[0]
    else:
        selection_mode = "blocked"
        selected_task = None
    return {
        "project_name": scoped.repository.project_name,
        "workset_id": workset.workset_id,
        "workset_title": workset.title,
        "selection_mode": selection_mode,
        "counts": dict(workset.counts),
        "selected_task": _task_payload(selected_task) if selected_task is not None else None,
        "ready_tasks": [_task_payload(task) for task in ready_tasks],
        "blocked_tasks": [_task_payload(task) for task in blocked_tasks],
        "active_tasks": [_task_payload(task) for task in active_tasks],
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
                prompt_hash = _attempt_prompt_lineage_text(attempt)
                lines.append(
                    (
                        f"    - {attempt.attempt_id} task={attempt.task_id} status={attempt.status} "
                        f"actor={attempt.actor}{branch}{worktree}{execution_model}{prompt_hash}{elapsed} {detail}"
                    ).rstrip()
                )
    return "\n".join(lines)


def render_next_text(payload: dict[str, Any]) -> str:
    lines = [f"Workset: {payload['workset_id']} {payload['workset_title']}"]
    selected = payload["selected_task"]
    if selected is None:
        lines.append("Selected: none")
    else:
        lines.append(
            f"Selected ({payload['selection_mode']}): {selected['task_id']} {selected['title']}"
        )
        lines.append(f"Intent: {selected['intent']}")
        if selected["claim_actor"]:
            lines.append(
                f"Claim: {selected['claim_actor']}/{selected['claim_execution_model'] or 'unknown'}"
            )
        if selected["latest_attempt_status"]:
            lines.append(
                f"Latest attempt: {selected['latest_attempt_status']} {selected['latest_attempt_id'] or ''}".rstrip()
            )
    if payload["blocked_tasks"]:
        lines.append("")
        lines.append("Blocked tasks:")
        for task in payload["blocked_tasks"]:
            blockers = ", ".join(task["blocked_by"]) if task["blocked_by"] else "unspecified"
            lines.append(f"  - {task['task_id']} {task['title']} ({blockers})")
    elif selected is None:
        lines.append("")
        lines.append("No blocked tasks are recorded.")
    return "\n".join(lines)


def render_attempts_summary_text(payload: dict[str, Any]) -> str:
    counts = payload["counts"]
    scope = payload.get("workset_scope")
    header = f"Project: {payload['project_name']}"
    if scope:
        header = f"{header} | Workset: {scope}"
    lines = [
        header,
        (
            "Completed attempts: "
            f"{counts['completed_attempts']} | Landed: {counts['landed']} | Not landed: {counts['not_landed']}"
        ),
        (
            "Validations: "
            f"passed={counts['validation_passed']} failed={counts['validation_failed']} skipped={counts['validation_skipped']}"
        ),
    ]
    if payload["worksets"]:
        lines.append("")
        lines.append("By workset:")
        for workset in payload["worksets"]:
            lines.append(
                (
                    f"  - {workset['workset_id']}: completed={workset['completed_attempts']} "
                    f"landed={workset['landed']} not_landed={workset['not_landed']}"
                )
            )
    if payload["recent_completed_attempts"]:
        lines.append("")
        lines.append("Recent completed attempts:")
        for attempt in payload["recent_completed_attempts"]:
            landed = f" landed={attempt['landed_commit']}" if attempt["landed_commit"] else ""
            commit = f" commit={attempt['commit']}" if attempt["commit"] else ""
            model = (
                f" model={attempt['model']}/{attempt['reasoning_effort']}"
                if attempt["model"] or attempt["reasoning_effort"]
                else ""
            )
            if attempt["user_prompt_hash"] and attempt["user_prompt_hash"] != attempt["execution_prompt_hash"]:
                prompt = (
                    " "
                    f"user_prompt={_prompt_receipt_label(attempt['user_prompt_source'], attempt['user_prompt_hash'], attempt['user_prompt_mode'])} "
                    f"execution_prompt={_prompt_receipt_label(attempt['execution_prompt_source'], attempt['execution_prompt_hash'], attempt['execution_prompt_mode'])}"
                )
            else:
                label = _prompt_receipt_label(
                    attempt["execution_prompt_source"],
                    attempt["execution_prompt_hash"],
                    attempt["execution_prompt_mode"],
                )
                prompt = f" prompt={label}" if label else ""
            summary = f" {attempt['summary']}" if attempt["summary"] else ""
            lines.append(
                (
                    f"  - {attempt['attempt_id']} workset={attempt['workset_id']} task={attempt['task_id']} "
                    f"status={attempt['status']} actor={attempt['actor']} validation={attempt['validation_summary']}{model}{prompt}{commit}{landed}{summary}"
                ).rstrip()
            )
    elif counts["completed_attempts"] == 0:
        lines.append("")
        lines.append("No completed attempts.")
    return "\n".join(lines)


def render_attempts_table_text(payload: dict[str, Any]) -> str:
    rows = payload["rows"]
    columns = payload["columns"]
    if not rows:
        return "\t".join(columns) + "\n"
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append(
            "\t".join("" if row.get(column) is None else str(row.get(column)) for column in columns)
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "SNAPSHOT_FORMAT",
    "RuntimeModel",
    "build_runtime_snapshot",
    "build_runtime_summary",
    "build_attempts_summary",
    "build_attempts_table",
    "build_next_payload",
    "load_runtime_model",
    "render_attempts_summary_text",
    "render_attempts_table_text",
    "render_next_text",
    "render_summary_text",
    "runtime_model_snapshot",
]
