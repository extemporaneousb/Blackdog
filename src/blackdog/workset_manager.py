from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass, replace
from pathlib import Path
from typing import Any
import json
import shlex
import uuid

from blackdog_core.backlog import BacklogError, claim_workset_manager, release_workset_manager
from blackdog_core.profile import RepoProfile
from blackdog_core.runtime_model import AttemptView, TaskView, WorksetView, load_runtime_model, scope_runtime_model
from blackdog_core.state import (
    EXECUTION_MODEL_DIRECT_WTAM,
    EXECUTION_MODEL_WORKSET_MANAGER,
    VALIDATION_STATUSES,
    ValidationRecord,
    atomic_write_text,
    append_event,
    now_iso,
    parse_iso,
)
from blackdog.wtam import close_task, land_task, primary_worktree_dirty_paths, primary_worktree_is_dirty, show_task


class SupervisorError(RuntimeError):
    pass


SUPERVISOR_RUN_SCHEMA_VERSION = 1
SUPERVISOR_RUN_STORE_VERSION = "blackdog.supervisor-run/v1"
SUPERVISOR_RUN_STATUS_ACTIVE = "active"
SUPERVISOR_RUN_STATUS_RELEASED = "released"
SUPERVISOR_BINDING_KIND_GENERIC = "generic"
SUPERVISOR_BINDING_KINDS = frozenset({SUPERVISOR_BINDING_KIND_GENERIC})
SUPERVISOR_DECISION_ACTION_LAND = "land"
SUPERVISOR_DECISION_ACTION_REVISE = "revise"
SUPERVISOR_DECISION_ACTION_RESTART = "restart"
SUPERVISOR_DECISION_ACTION_CLOSE = "close"
SUPERVISOR_DECISION_ACTIONS = frozenset(
    {
        SUPERVISOR_DECISION_ACTION_LAND,
        SUPERVISOR_DECISION_ACTION_REVISE,
        SUPERVISOR_DECISION_ACTION_RESTART,
        SUPERVISOR_DECISION_ACTION_CLOSE,
    }
)


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


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_text(value: Any, *, field: str, source: Path) -> str:
    text = _optional_text(value)
    if text is None:
        raise SupervisorError(f"{field} is required in {source}")
    return text


def _require_parallelism(value: Any, *, field: str, source: Path) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise SupervisorError(f"{field} must be an integer in {source}") from exc
    if normalized < 1:
        raise SupervisorError(f"{field} must be at least 1 in {source}")
    return normalized


def _require_list(value: Any, *, field: str, source: Path) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SupervisorError(f"{field} must be a list in {source}")
    return value


def _string_list(value: Any, *, field: str, source: Path) -> tuple[str, ...]:
    rows: list[str] = []
    for item in _require_list(value, field=field, source=source):
        text = _optional_text(item)
        if text:
            rows.append(text)
    return tuple(rows)


def _validation_from_payload(payload: Any, *, source: Path) -> ValidationRecord:
    if not isinstance(payload, dict):
        raise SupervisorError(f"validation rows must be JSON objects in {source}")
    name = _require_text(payload.get("name"), field="name", source=source)
    status = _optional_text(payload.get("status")) or "passed"
    if status not in VALIDATION_STATUSES:
        raise SupervisorError(f"validation.status must be one of {sorted(VALIDATION_STATUSES)} in {source}")
    return ValidationRecord(name=name, status=status)


def _run_sort_key(timestamp: str | None, *, fallback: str) -> tuple[float, str]:
    parsed = parse_iso(timestamp)
    return ((parsed.timestamp() if parsed is not None else 0.0), fallback)


@dataclass(frozen=True, slots=True)
class SupervisorWorkerBinding:
    task_id: str
    worker_actor: str
    binding_kind: str
    binding_id: str
    bound_at: str
    attempt_id: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class SupervisorCheckpointRecord:
    checkpointed_at: str
    phase: str
    active_task_ids: tuple[str, ...]
    ready_task_ids: tuple[str, ...]
    dispatch_task_ids: tuple[str, ...]
    binding_task_ids: tuple[str, ...]
    note: str | None = None


@dataclass(frozen=True, slots=True)
class SupervisorSubmissionRecord:
    task_id: str
    attempt_id: str
    worker_actor: str
    submitted_at: str
    summary: str
    branch: str | None
    target_branch: str | None
    worktree_path: str | None
    changed_paths: tuple[str, ...]
    validations: tuple[ValidationRecord, ...] = ()
    residuals: tuple[str, ...] = ()
    followup_candidates: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True, slots=True)
class SupervisorDecisionRecord:
    task_id: str
    attempt_id: str
    actor: str
    decided_at: str
    action: str
    status: str
    summary: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class SupervisorRunRecord:
    schema_version: int
    store_version: str
    run_id: str
    workset_id: str
    actor: str
    parallelism: int
    status: str
    started_at: str
    updated_at: str
    released_at: str | None = None
    summary: str | None = None
    note: str | None = None
    bindings: tuple[SupervisorWorkerBinding, ...] = ()
    checkpoints: tuple[SupervisorCheckpointRecord, ...] = ()
    submissions: tuple[SupervisorSubmissionRecord, ...] = ()
    decisions: tuple[SupervisorDecisionRecord, ...] = ()


def _binding_from_payload(payload: Any, *, source: Path) -> SupervisorWorkerBinding:
    if not isinstance(payload, dict):
        raise SupervisorError(f"bindings entries must be JSON objects in {source}")
    binding_kind = _require_text(payload.get("binding_kind"), field="binding_kind", source=source)
    if binding_kind not in SUPERVISOR_BINDING_KINDS:
        raise SupervisorError(f"binding_kind must be one of {sorted(SUPERVISOR_BINDING_KINDS)} in {source}")
    return SupervisorWorkerBinding(
        task_id=_require_text(payload.get("task_id"), field="task_id", source=source),
        worker_actor=_require_text(payload.get("worker_actor"), field="worker_actor", source=source),
        binding_kind=binding_kind,
        binding_id=_require_text(payload.get("binding_id"), field="binding_id", source=source),
        bound_at=_require_text(payload.get("bound_at"), field="bound_at", source=source),
        attempt_id=_require_text(payload.get("attempt_id"), field="attempt_id", source=source),
        note=_optional_text(payload.get("note")),
    )


def _checkpoint_from_payload(payload: Any, *, source: Path) -> SupervisorCheckpointRecord:
    if not isinstance(payload, dict):
        raise SupervisorError(f"checkpoints entries must be JSON objects in {source}")
    return SupervisorCheckpointRecord(
        checkpointed_at=_require_text(payload.get("checkpointed_at"), field="checkpointed_at", source=source),
        phase=_require_text(payload.get("phase"), field="phase", source=source),
        active_task_ids=_string_list(payload.get("active_task_ids"), field="active_task_ids", source=source),
        ready_task_ids=_string_list(payload.get("ready_task_ids"), field="ready_task_ids", source=source),
        dispatch_task_ids=_string_list(payload.get("dispatch_task_ids"), field="dispatch_task_ids", source=source),
        binding_task_ids=_string_list(payload.get("binding_task_ids"), field="binding_task_ids", source=source),
        note=_optional_text(payload.get("note")),
    )


def _submission_from_payload(payload: Any, *, source: Path) -> SupervisorSubmissionRecord:
    if not isinstance(payload, dict):
        raise SupervisorError(f"submissions entries must be JSON objects in {source}")
    return SupervisorSubmissionRecord(
        task_id=_require_text(payload.get("task_id"), field="task_id", source=source),
        attempt_id=_require_text(payload.get("attempt_id"), field="attempt_id", source=source),
        worker_actor=_require_text(payload.get("worker_actor"), field="worker_actor", source=source),
        submitted_at=_require_text(payload.get("submitted_at"), field="submitted_at", source=source),
        summary=_require_text(payload.get("summary"), field="summary", source=source),
        branch=_optional_text(payload.get("branch")),
        target_branch=_optional_text(payload.get("target_branch")),
        worktree_path=_optional_text(payload.get("worktree_path")),
        changed_paths=_string_list(payload.get("changed_paths"), field="changed_paths", source=source),
        validations=tuple(
            _validation_from_payload(item, source=source)
            for item in _require_list(payload.get("validations"), field="validations", source=source)
        ),
        residuals=_string_list(payload.get("residuals"), field="residuals", source=source),
        followup_candidates=_string_list(
            payload.get("followup_candidates"),
            field="followup_candidates",
            source=source,
        ),
        note=_optional_text(payload.get("note")),
    )


def _decision_from_payload(payload: Any, *, source: Path) -> SupervisorDecisionRecord:
    if not isinstance(payload, dict):
        raise SupervisorError(f"decisions entries must be JSON objects in {source}")
    action = _require_text(payload.get("action"), field="action", source=source)
    if action not in SUPERVISOR_DECISION_ACTIONS:
        raise SupervisorError(f"action must be one of {sorted(SUPERVISOR_DECISION_ACTIONS)} in {source}")
    return SupervisorDecisionRecord(
        task_id=_require_text(payload.get("task_id"), field="task_id", source=source),
        attempt_id=_require_text(payload.get("attempt_id"), field="attempt_id", source=source),
        actor=_require_text(payload.get("actor"), field="actor", source=source),
        decided_at=_require_text(payload.get("decided_at"), field="decided_at", source=source),
        action=action,
        status=_require_text(payload.get("status"), field="status", source=source),
        summary=_require_text(payload.get("summary"), field="summary", source=source),
        note=_optional_text(payload.get("note")),
    )


def _supervisor_run_from_payload(payload: Any, *, source: Path) -> SupervisorRunRecord:
    if not isinstance(payload, dict):
        raise SupervisorError(f"{source} must contain a JSON object")
    status = _require_text(payload.get("status"), field="status", source=source)
    if status not in {SUPERVISOR_RUN_STATUS_ACTIVE, SUPERVISOR_RUN_STATUS_RELEASED}:
        raise SupervisorError(
            f"status must be one of {[SUPERVISOR_RUN_STATUS_ACTIVE, SUPERVISOR_RUN_STATUS_RELEASED]} in {source}"
        )
    return SupervisorRunRecord(
        schema_version=_require_parallelism(payload.get("schema_version"), field="schema_version", source=source),
        store_version=_require_text(payload.get("store_version"), field="store_version", source=source),
        run_id=_require_text(payload.get("run_id"), field="run_id", source=source),
        workset_id=_require_text(payload.get("workset_id"), field="workset_id", source=source),
        actor=_require_text(payload.get("actor"), field="actor", source=source),
        parallelism=_require_parallelism(payload.get("parallelism"), field="parallelism", source=source),
        status=status,
        started_at=_require_text(payload.get("started_at"), field="started_at", source=source),
        updated_at=_require_text(payload.get("updated_at"), field="updated_at", source=source),
        released_at=_optional_text(payload.get("released_at")),
        summary=_optional_text(payload.get("summary")),
        note=_optional_text(payload.get("note")),
        bindings=tuple(
            _binding_from_payload(item, source=source)
            for item in _require_list(payload.get("bindings"), field="bindings", source=source)
        ),
        checkpoints=tuple(
            _checkpoint_from_payload(item, source=source)
            for item in _require_list(payload.get("checkpoints"), field="checkpoints", source=source)
        ),
        submissions=tuple(
            _submission_from_payload(item, source=source)
            for item in _require_list(payload.get("submissions"), field="submissions", source=source)
        ),
        decisions=tuple(
            _decision_from_payload(item, source=source)
            for item in _require_list(payload.get("decisions"), field="decisions", source=source)
        ),
    )


def _supervisor_runs_dir(profile: RepoProfile) -> Path:
    return profile.paths.control_dir / "supervisor-runs"


def _supervisor_run_status_path(profile: RepoProfile, *, run_id: str) -> Path:
    return _supervisor_runs_dir(profile) / run_id / "status.json"


def _load_supervisor_run(path: Path) -> SupervisorRunRecord:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SupervisorError(f"Supervisor run file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SupervisorError(f"Invalid JSON in {path}: {exc}") from exc
    run = _supervisor_run_from_payload(payload, source=path)
    if run.schema_version != SUPERVISOR_RUN_SCHEMA_VERSION:
        raise SupervisorError(f"Unsupported supervisor run schema_version {run.schema_version} in {path}")
    if run.store_version != SUPERVISOR_RUN_STORE_VERSION:
        raise SupervisorError(f"Unsupported supervisor run store_version {run.store_version!r} in {path}")
    return run


def _save_supervisor_run(profile: RepoProfile, run: SupervisorRunRecord) -> Path:
    path = _supervisor_run_status_path(profile, run_id=run.run_id)
    atomic_write_text(path, json.dumps(_jsonable(run), indent=2, sort_keys=True) + "\n")
    return path


def _iter_supervisor_runs(profile: RepoProfile) -> tuple[SupervisorRunRecord, ...]:
    root = _supervisor_runs_dir(profile)
    if not root.exists():
        return ()
    runs = [_load_supervisor_run(path) for path in sorted(root.glob("*/status.json"))]
    runs.sort(key=lambda run: _run_sort_key(run.updated_at, fallback=run.run_id), reverse=True)
    return tuple(runs)


def _latest_supervisor_run(
    profile: RepoProfile,
    *,
    workset_id: str,
    actor: str | None = None,
    status: str | None = None,
) -> SupervisorRunRecord | None:
    for run in _iter_supervisor_runs(profile):
        if run.workset_id != workset_id:
            continue
        if actor is not None and run.actor != actor:
            continue
        if status is not None and run.status != status:
            continue
        return run
    return None


def _new_run_id(*, workset_id: str) -> str:
    token = uuid.uuid4().hex[:8]
    return f"{workset_id}-{token}"


def _active_binding_index(bindings: tuple[SupervisorWorkerBinding, ...]) -> dict[str, SupervisorWorkerBinding]:
    return {binding.task_id: binding for binding in bindings}


def _active_task_index(workset: WorksetView) -> dict[str, TaskView]:
    return {
        task.task_id: task
        for task in workset.tasks
        if task.active_attempt_id is not None or task.runtime_status == "in_progress" or task.claim_actor is not None
    }


def _prune_stale_bindings(
    workset: WorksetView,
    bindings: tuple[SupervisorWorkerBinding, ...],
) -> tuple[SupervisorWorkerBinding, ...]:
    active_tasks = _active_task_index(workset)
    kept: list[SupervisorWorkerBinding] = []
    for binding in bindings:
        task = active_tasks.get(binding.task_id)
        if task is None:
            continue
        if task.claim_actor != binding.worker_actor:
            continue
        if task.claim_execution_model != EXECUTION_MODEL_DIRECT_WTAM:
            continue
        if task.active_attempt_id is None:
            continue
        if task.active_attempt_id != binding.attempt_id:
            continue
        kept.append(binding)
    return tuple(kept)


def _worker_actor_suggestion(*, supervisor_actor: str, task: TaskView) -> str:
    return f"{supervisor_actor}/{task.task_id.lower()}"


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


def _task_begin_command(profile: RepoProfile, task: TaskView, *, worker_actor: str) -> str:
    return (
        f"./.VE/bin/blackdog task begin --project-root {shlex.quote(str(profile.paths.project_root))} "
        f"--workset {shlex.quote(task.workset_id)} --task {shlex.quote(task.task_id)} "
        f"--actor {shlex.quote(worker_actor)} --prompt-file PROMPT.txt"
    )


@dataclass(frozen=True, slots=True)
class SupervisorDispatch:
    task_id: str
    title: str
    intent: str
    worker_actor_suggestion: str
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
    landing_ready: bool
    landing_blockers: tuple[str, ...]
    claim: dict[str, Any] | None
    supervisor_run: dict[str, Any] | None
    counts: dict[str, int]
    active_tasks: tuple[dict[str, Any], ...]
    ready_tasks: tuple[dict[str, Any], ...]
    blocked_tasks: tuple[dict[str, Any], ...]
    review_queue: tuple[dict[str, Any], ...]
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
    supervisor_actor: str | None,
    available_slots: int,
    landing_ready: bool,
) -> tuple[SupervisorDispatch, ...]:
    if available_slots <= 0 or not landing_ready:
        return ()
    ready_tasks = [task for task in workset.tasks if task.is_ready]
    dispatches: list[SupervisorDispatch] = []
    for task in ready_tasks[:available_slots]:
        worker_actor = _worker_actor_suggestion(supervisor_actor=supervisor_actor or "worker", task=task)
        dispatches.append(
            SupervisorDispatch(
                task_id=task.task_id,
                title=task.title,
                intent=task.intent,
                worker_actor_suggestion=worker_actor,
                worker_request=_worker_request(workset, task),
                task_begin_command=_task_begin_command(profile, task, worker_actor=worker_actor),
                paths=task.paths,
                docs=task.docs,
                checks=task.checks,
            )
        )
    return tuple(dispatches)


def _supervisor_phase(
    *,
    supervisor_active: bool,
    landing_ready: bool,
    workset: WorksetView,
    active_tasks: list[TaskView],
    ready_tasks: list[TaskView],
    review_queue: tuple[dict[str, Any], ...],
    dispatches: tuple[SupervisorDispatch, ...],
) -> str:
    if workset.claim is not None and workset.claim.execution_model != EXECUTION_MODEL_WORKSET_MANAGER:
        return "occupied"
    if workset.counts["done"] == workset.counts["tasks"] and not active_tasks:
        return "complete"
    if review_queue and dispatches:
        return "dispatch_and_review"
    if review_queue:
        return "review"
    if not landing_ready and ready_tasks:
        return "blocked"
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
    landing_ready: bool,
    landing_blockers: tuple[str, ...],
    dispatches: tuple[SupervisorDispatch, ...],
    active_tasks: list[TaskView],
    review_queue: tuple[dict[str, Any], ...],
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
    if not landing_ready:
        detail = f" Clean the primary worktree first: {', '.join(landing_blockers)}." if landing_blockers else ""
        actions.append(f"Dispatch is blocked until the primary landing target is clean.{detail}")
    if review_queue:
        actions.append(f"Review {len(review_queue)} submitted worker result(s) with `blackdog supervisor decide`.")
    if dispatches:
        actions.append(f"Launch {len(dispatches)} worker task(s) from the dispatch set below.")
    if active_tasks:
        actions.append("Monitor active worker tasks and review land/close results before dispatching replacements.")
    if phase == "blocked":
        actions.append("Replan blocked work or patch runtime state before continuing this workset.")
    if phase == "complete" and supervisor_active:
        actions.append("Release the supervisor claim after summarizing the completed workset.")
    return tuple(actions)


def _latest_submission(
    run: SupervisorRunRecord,
    *,
    task_id: str,
    attempt_id: str,
) -> SupervisorSubmissionRecord | None:
    matches = [row for row in run.submissions if row.task_id == task_id and row.attempt_id == attempt_id]
    if not matches:
        return None
    return max(matches, key=lambda row: _run_sort_key(row.submitted_at, fallback=row.attempt_id))


def _latest_decision(
    run: SupervisorRunRecord,
    *,
    task_id: str,
    attempt_id: str,
) -> SupervisorDecisionRecord | None:
    matches = [row for row in run.decisions if row.task_id == task_id and row.attempt_id == attempt_id]
    if not matches:
        return None
    return max(matches, key=lambda row: _run_sort_key(row.decided_at, fallback=row.attempt_id))


def _review_queue(workset: WorksetView, run: SupervisorRunRecord | None) -> tuple[dict[str, Any], ...]:
    if run is None:
        return ()
    binding_index = _active_binding_index(run.bindings)
    queue: list[dict[str, Any]] = []
    for task in workset.tasks:
        if task.active_attempt_id is None:
            continue
        binding = binding_index.get(task.task_id)
        if binding is None or binding.attempt_id != task.active_attempt_id:
            continue
        submission = _latest_submission(run, task_id=task.task_id, attempt_id=task.active_attempt_id)
        if submission is None:
            continue
        decision = _latest_decision(run, task_id=task.task_id, attempt_id=task.active_attempt_id)
        if decision is not None:
            submission_at = parse_iso(submission.submitted_at)
            decision_at = parse_iso(decision.decided_at)
            if submission_at is not None and decision_at is not None and decision_at >= submission_at:
                continue
        queue.append(
            {
                "task_id": task.task_id,
                "task_title": task.title,
                "attempt_id": submission.attempt_id,
                "worker_actor": submission.worker_actor,
                "submitted_at": submission.submitted_at,
                "summary": submission.summary,
                "branch": submission.branch,
                "target_branch": submission.target_branch,
                "worktree_path": submission.worktree_path,
                "changed_paths": list(submission.changed_paths),
                "validations": [_jsonable(item) for item in submission.validations],
                "residuals": list(submission.residuals),
                "followup_candidates": list(submission.followup_candidates),
                "note": submission.note,
            }
        )
    queue.sort(key=lambda item: _run_sort_key(item.get("submitted_at"), fallback=str(item.get("attempt_id"))), reverse=True)
    return tuple(queue)


def _landing_blockers(profile: RepoProfile) -> tuple[str, ...]:
    if not primary_worktree_is_dirty(profile, ignore_runtime=True):
        return ()
    return tuple(primary_worktree_dirty_paths(profile, ignore_runtime=True))


def _render_supervisor_run(
    workset: WorksetView,
    run: SupervisorRunRecord | None,
) -> SupervisorRunRecord | None:
    if run is None:
        return None
    return replace(run, bindings=_prune_stale_bindings(workset, run.bindings))


def _require_active_supervisor_claim(status: SupervisorStatus, *, actor: str, workset_id: str) -> None:
    if status.claim is None or status.claim.get("execution_model") != EXECUTION_MODEL_WORKSET_MANAGER:
        raise SupervisorError(f"Workset {workset_id!r} is not currently claimed for supervisor execution")
    if status.claim.get("actor") != actor:
        raise SupervisorError(f"Workset {workset_id!r} is supervised by {status.claim.get('actor')}, not {actor}")


def _require_active_supervisor_run(
    profile: RepoProfile,
    *,
    workset_id: str,
    actor: str,
) -> SupervisorRunRecord:
    run = _latest_supervisor_run(
        profile,
        workset_id=workset_id,
        actor=actor,
        status=SUPERVISOR_RUN_STATUS_ACTIVE,
    )
    if run is None:
        raise SupervisorError(f"Workset {workset_id!r} has no active supervisor run for {actor}")
    return run


def _upsert_supervisor_run(
    profile: RepoProfile,
    *,
    workset_id: str,
    actor: str,
    parallelism: int,
    note: str | None,
) -> SupervisorRunRecord:
    timestamp = now_iso()
    current = _latest_supervisor_run(
        profile,
        workset_id=workset_id,
        actor=actor,
        status=SUPERVISOR_RUN_STATUS_ACTIVE,
    )
    if current is None:
        run = SupervisorRunRecord(
            schema_version=SUPERVISOR_RUN_SCHEMA_VERSION,
            store_version=SUPERVISOR_RUN_STORE_VERSION,
            run_id=_new_run_id(workset_id=workset_id),
            workset_id=workset_id,
            actor=actor,
            parallelism=parallelism,
            status=SUPERVISOR_RUN_STATUS_ACTIVE,
            started_at=timestamp,
            updated_at=timestamp,
            note=note,
        )
    else:
        run = replace(
            current,
            parallelism=parallelism,
            updated_at=timestamp,
            note=note if note is not None else current.note,
        )
    _save_supervisor_run(profile, run)
    return run


def _persist_checkpoint(
    profile: RepoProfile,
    *,
    workset: WorksetView,
    run: SupervisorRunRecord,
    status: SupervisorStatus,
    note: str | None,
) -> SupervisorRunRecord:
    timestamp = now_iso()
    resolved_run = _render_supervisor_run(workset, run) or run
    checkpoint = SupervisorCheckpointRecord(
        checkpointed_at=timestamp,
        phase=status.phase,
        active_task_ids=tuple(item["task_id"] for item in status.active_tasks),
        ready_task_ids=tuple(item["task_id"] for item in status.ready_tasks),
        dispatch_task_ids=tuple(item.task_id for item in status.dispatches),
        binding_task_ids=tuple(binding.task_id for binding in resolved_run.bindings),
        note=note,
    )
    next_run = replace(
        resolved_run,
        parallelism=status.parallelism,
        updated_at=timestamp,
        note=note if note is not None else resolved_run.note,
        checkpoints=tuple([*resolved_run.checkpoints, checkpoint]),
    )
    _save_supervisor_run(profile, next_run)
    return next_run


def _release_supervisor_run(
    profile: RepoProfile,
    *,
    workset: WorksetView,
    run: SupervisorRunRecord,
    parallelism: int,
    summary: str | None,
    note: str | None,
) -> SupervisorRunRecord:
    timestamp = now_iso()
    resolved_run = _render_supervisor_run(workset, run) or run
    released = replace(
        resolved_run,
        parallelism=parallelism,
        status=SUPERVISOR_RUN_STATUS_RELEASED,
        updated_at=timestamp,
        released_at=timestamp,
        summary=summary if summary is not None else resolved_run.summary,
        note=note if note is not None else resolved_run.note,
        bindings=(),
    )
    _save_supervisor_run(profile, released)
    return released


def _bind_worker(
    profile: RepoProfile,
    *,
    workset: WorksetView,
    run: SupervisorRunRecord,
    task: TaskView,
    worker_actor: str,
    binding_kind: str,
    binding_id: str,
    note: str | None,
) -> SupervisorRunRecord:
    if binding_kind not in SUPERVISOR_BINDING_KINDS:
        raise SupervisorError(f"binding_kind must be one of {', '.join(sorted(SUPERVISOR_BINDING_KINDS))}")
    if task.claim_actor != worker_actor:
        raise SupervisorError(f"Task {task.task_id!r} is claimed by {task.claim_actor!r}, not {worker_actor!r}")
    if task.claim_execution_model != EXECUTION_MODEL_DIRECT_WTAM:
        raise SupervisorError(
            f"Task {task.task_id!r} is claimed for {task.claim_execution_model!r}, not {EXECUTION_MODEL_DIRECT_WTAM!r}"
        )
    if task.active_attempt_id is None:
        raise SupervisorError(f"Task {task.task_id!r} does not have an active attempt to bind")
    timestamp = now_iso()
    resolved = _render_supervisor_run(workset, run) or run
    next_binding = SupervisorWorkerBinding(
        task_id=task.task_id,
        worker_actor=worker_actor,
        binding_kind=binding_kind,
        binding_id=binding_id,
        bound_at=timestamp,
        attempt_id=task.active_attempt_id,
        note=note,
    )
    next_bindings = [binding for binding in resolved.bindings if binding.task_id != task.task_id]
    next_bindings.append(next_binding)
    updated = replace(
        resolved,
        updated_at=timestamp,
        note=note if note is not None else resolved.note,
        bindings=tuple(sorted(next_bindings, key=lambda item: item.task_id)),
    )
    _save_supervisor_run(profile, updated)
    return updated


def _record_submission(
    profile: RepoProfile,
    *,
    workset: WorksetView,
    run: SupervisorRunRecord,
    task: TaskView,
    summary: str,
    validations: tuple[ValidationRecord, ...],
    residuals: tuple[str, ...],
    followup_candidates: tuple[str, ...],
    note: str | None,
) -> SupervisorSubmissionRecord:
    binding_index = _active_binding_index(run.bindings)
    binding = binding_index.get(task.task_id)
    if binding is None or task.active_attempt_id is None or binding.attempt_id != task.active_attempt_id:
        raise SupervisorError(f"Task {task.task_id!r} is not bound to the active supervisor run")
    inspection = show_task(
        profile,
        workset_id=task.workset_id,
        task_id=task.task_id,
    )
    timestamp = now_iso()
    submission = SupervisorSubmissionRecord(
        task_id=task.task_id,
        attempt_id=binding.attempt_id,
        worker_actor=binding.worker_actor,
        submitted_at=timestamp,
        summary=summary,
        branch=inspection.get("branch"),
        target_branch=inspection.get("target_branch"),
        worktree_path=inspection.get("worktree_path"),
        changed_paths=tuple(inspection.get("changed_paths") or ()),
        validations=validations,
        residuals=residuals,
        followup_candidates=followup_candidates,
        note=note,
    )
    updated = replace(
        _render_supervisor_run(workset, run) or run,
        updated_at=timestamp,
        note=note if note is not None else run.note,
        submissions=tuple([*run.submissions, submission]),
    )
    _save_supervisor_run(profile, updated)
    return submission


def _record_decision(
    profile: RepoProfile,
    *,
    workset: WorksetView,
    run: SupervisorRunRecord,
    task_id: str,
    attempt_id: str,
    actor: str,
    action: str,
    status: str,
    summary: str,
    note: str | None,
) -> SupervisorDecisionRecord:
    timestamp = now_iso()
    decision = SupervisorDecisionRecord(
        task_id=task_id,
        attempt_id=attempt_id,
        actor=actor,
        decided_at=timestamp,
        action=action,
        status=status,
        summary=summary,
        note=note,
    )
    updated = replace(
        _render_supervisor_run(workset, run) or run,
        updated_at=timestamp,
        note=note if note is not None else run.note,
        decisions=tuple([*run.decisions, decision]),
    )
    _save_supervisor_run(profile, updated)
    return decision


def _resolve_review_submission(
    workset: WorksetView,
    run: SupervisorRunRecord,
    *,
    task_id: str,
) -> SupervisorSubmissionRecord:
    for item in _review_queue(workset, run):
        if item["task_id"] == task_id:
            submission = _latest_submission(run, task_id=task_id, attempt_id=item["attempt_id"])
            if submission is not None:
                return submission
    raise SupervisorError(f"Task {task_id!r} does not currently have a pending supervisor submission")


def _decision_summary(action: str, *, task_id: str, summary: str | None, fallback: str | None = None) -> str:
    resolved = _optional_text(summary) or _optional_text(fallback)
    if resolved is not None:
        return resolved
    return f"{action} {task_id}"


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
    landing_blockers = _landing_blockers(profile)
    landing_ready = not landing_blockers
    supervisor_run = _render_supervisor_run(
        workset,
        _latest_supervisor_run(
            profile,
            workset_id=workset_id,
            actor=workset.claim.actor if workset.claim is not None else None,
        ),
    )
    active_tasks = [
        task
        for task in workset.tasks
        if task.active_attempt_id is not None or task.runtime_status == "in_progress" or task.claim_actor is not None
    ]
    ready_tasks = [task for task in workset.tasks if task.is_ready]
    available_slots = max(0, resolved_parallelism - len(active_tasks))
    review_queue = _review_queue(workset, supervisor_run)
    dispatches = _build_dispatches(
        profile,
        workset,
        supervisor_actor=workset.claim.actor if workset.claim is not None else None,
        available_slots=available_slots,
        landing_ready=landing_ready,
    )
    supervisor_active = workset.claim is not None and workset.claim.execution_model == EXECUTION_MODEL_WORKSET_MANAGER
    phase = _supervisor_phase(
        supervisor_active=supervisor_active,
        landing_ready=landing_ready,
        workset=workset,
        active_tasks=active_tasks,
        ready_tasks=ready_tasks,
        review_queue=review_queue,
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
        landing_ready=landing_ready,
        landing_blockers=landing_blockers,
        claim=_jsonable(workset.claim),
        supervisor_run=_jsonable(supervisor_run),
        counts=dict(workset.counts),
        active_tasks=tuple(_task_payload(task) for task in active_tasks),
        ready_tasks=tuple(_task_payload(task) for task in ready_tasks),
        blocked_tasks=tuple(_task_payload(task) for task in workset.tasks if task.readiness == "blocked"),
        review_queue=review_queue,
        dispatches=dispatches,
        recent_attempts=tuple(_attempt_payload(attempt) for attempt in workset.attempts[:5]),
        recommended_actions=_recommended_actions(
            profile=profile,
            workset=workset,
            phase=phase,
            supervisor_active=supervisor_active,
            landing_ready=landing_ready,
            landing_blockers=landing_blockers,
            dispatches=dispatches,
            active_tasks=active_tasks,
            review_queue=review_queue,
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
    _upsert_supervisor_run(
        profile,
        workset_id=workset_id,
        actor=actor,
        parallelism=parallelism,
        note=note,
    )
    return show_supervisor(
        profile,
        workset_id=workset_id,
        parallelism=parallelism,
        action="start",
        note=note,
    )


def reconcile_supervisor(
    profile: RepoProfile,
    *,
    workset_id: str,
    parallelism: int | None = None,
) -> SupervisorStatus:
    active_run = _latest_supervisor_run(
        profile,
        workset_id=workset_id,
        status=SUPERVISOR_RUN_STATUS_ACTIVE,
    )
    resolved_parallelism = (
        _ensure_parallelism(parallelism)
        if parallelism is not None
        else (active_run.parallelism if active_run is not None else 1)
    )
    return show_supervisor(
        profile,
        workset_id=workset_id,
        parallelism=resolved_parallelism,
        action="reconcile",
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
    _require_active_supervisor_claim(status, actor=actor, workset_id=workset_id)
    workset = _supervisor_workset(profile, workset_id=workset_id)
    run = _require_active_supervisor_run(profile, workset_id=workset_id, actor=actor)
    persisted_run = _persist_checkpoint(
        profile,
        workset=workset,
        run=run,
        status=status,
        note=note,
    )
    append_event(
        profile.paths.events_file,
        event_type="supervisor.checkpoint",
        actor=actor,
        payload={
            "run_id": persisted_run.run_id,
            "workset_id": status.workset_id,
            "parallelism": status.parallelism,
            "phase": status.phase,
            "available_slots": status.available_slots,
            "ready_task_ids": [item["task_id"] for item in status.ready_tasks],
            "active_task_ids": [item["task_id"] for item in status.active_tasks],
            "dispatch_task_ids": [item.task_id for item in status.dispatches],
            "binding_task_ids": [binding.task_id for binding in persisted_run.bindings],
            "note": note,
        },
    )
    return show_supervisor(
        profile,
        workset_id=workset_id,
        parallelism=parallelism,
        action="checkpoint",
        note=note,
    )


def bind_supervisor_worker(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    worker_actor: str,
    binding_id: str,
    binding_kind: str = SUPERVISOR_BINDING_KIND_GENERIC,
    note: str | None = None,
) -> SupervisorStatus:
    status = show_supervisor(
        profile,
        workset_id=workset_id,
        parallelism=_ensure_parallelism(
            (
                _require_active_supervisor_run(profile, workset_id=workset_id, actor=actor).parallelism
                if _latest_supervisor_run(
                    profile,
                    workset_id=workset_id,
                    actor=actor,
                    status=SUPERVISOR_RUN_STATUS_ACTIVE,
                )
                is not None
                else 1
            )
        ),
        action="bind",
        note=note,
    )
    _require_active_supervisor_claim(status, actor=actor, workset_id=workset_id)
    workset = _supervisor_workset(profile, workset_id=workset_id)
    task = next((item for item in workset.tasks if item.task_id == task_id), None)
    if task is None:
        raise SupervisorError(f"Unknown task {task_id!r} in workset {workset_id!r}")
    if task.claim_actor is None or task.active_attempt_id is None:
        raise SupervisorError(f"Task {task_id!r} is not currently active")
    run = _require_active_supervisor_run(profile, workset_id=workset_id, actor=actor)
    persisted_run = _bind_worker(
        profile,
        workset=workset,
        run=run,
        task=task,
        worker_actor=worker_actor,
        binding_kind=binding_kind,
        binding_id=binding_id,
        note=note,
    )
    append_event(
        profile.paths.events_file,
        event_type="supervisor.bind",
        actor=actor,
        payload={
            "run_id": persisted_run.run_id,
            "workset_id": workset_id,
            "task_id": task_id,
            "worker_actor": worker_actor,
            "binding_kind": binding_kind,
            "binding_id": binding_id,
            "attempt_id": task.active_attempt_id,
            "note": note,
        },
    )
    return show_supervisor(
        profile,
        workset_id=workset_id,
        parallelism=persisted_run.parallelism,
        action="bind",
        note=note,
    )


def submit_supervisor(
    profile: RepoProfile,
    *,
    summary: str,
    workset_id: str | None = None,
    task_id: str | None = None,
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
    cwd: Path | None = None,
) -> tuple[SupervisorStatus, SupervisorSubmissionRecord]:
    inspection = show_task(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        cwd=cwd,
    )
    if not inspection.get("active_attempt"):
        raise SupervisorError(
            f"Task {inspection['task_id']!r} in workset {inspection['workset_id']!r} does not have an active attempt"
        )
    resolved_workset = str(inspection["workset_id"])
    resolved_task = str(inspection["task_id"])
    workset = _supervisor_workset(profile, workset_id=resolved_workset)
    if workset.claim is None or workset.claim.execution_model != EXECUTION_MODEL_WORKSET_MANAGER:
        raise SupervisorError(f"Workset {resolved_workset!r} is not currently claimed for supervisor execution")
    run = _require_active_supervisor_run(profile, workset_id=resolved_workset, actor=workset.claim.actor)
    task = next((item for item in workset.tasks if item.task_id == resolved_task), None)
    if task is None:
        raise SupervisorError(f"Unknown task {resolved_task!r} in workset {resolved_workset!r}")
    resolved_summary = _decision_summary("submit", task_id=resolved_task, summary=summary)
    submission = _record_submission(
        profile,
        workset=workset,
        run=run,
        task=task,
        summary=resolved_summary,
        validations=validations,
        residuals=residuals,
        followup_candidates=followup_candidates,
        note=note,
    )
    append_event(
        profile.paths.events_file,
        event_type="supervisor.submit",
        actor=submission.worker_actor,
        payload={
            "run_id": run.run_id,
            "workset_id": resolved_workset,
            "task_id": resolved_task,
            "attempt_id": submission.attempt_id,
            "worker_actor": submission.worker_actor,
            "submitted_at": submission.submitted_at,
            "summary": submission.summary,
            "changed_paths": list(submission.changed_paths),
            "validations": [_jsonable(item) for item in submission.validations],
            "residuals": list(submission.residuals),
            "followup_candidates": list(submission.followup_candidates),
            "note": note,
        },
    )
    return (
        show_supervisor(
            profile,
            workset_id=resolved_workset,
            parallelism=run.parallelism,
            action="submit",
            note=note or resolved_summary,
        ),
        submission,
    )


def decide_supervisor(
    profile: RepoProfile,
    *,
    action: str,
    actor: str,
    workset_id: str | None = None,
    task_id: str | None = None,
    summary: str | None = None,
    close_status: str | None = None,
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
    keep_worktree: bool = False,
    cleanup: bool = False,
    cwd: Path | None = None,
) -> tuple[SupervisorStatus, SupervisorDecisionRecord, dict[str, Any] | None]:
    if action not in SUPERVISOR_DECISION_ACTIONS:
        raise SupervisorError(f"action must be one of {', '.join(sorted(SUPERVISOR_DECISION_ACTIONS))}")
    inspection = show_task(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        cwd=cwd,
    )
    resolved_workset = str(inspection["workset_id"])
    resolved_task = str(inspection["task_id"])
    status = reconcile_supervisor(profile, workset_id=resolved_workset)
    _require_active_supervisor_claim(status, actor=actor, workset_id=resolved_workset)
    workset = _supervisor_workset(profile, workset_id=resolved_workset)
    run = _require_active_supervisor_run(profile, workset_id=resolved_workset, actor=actor)
    submission = _resolve_review_submission(workset, run, task_id=resolved_task)
    resolved_summary = _decision_summary(action, task_id=resolved_task, summary=summary, fallback=submission.summary)
    resolved_validations = validations or submission.validations
    resolved_residuals = residuals or submission.residuals
    resolved_followups = followup_candidates or submission.followup_candidates
    result: dict[str, Any] | None = None
    decision_status: str
    if action == SUPERVISOR_DECISION_ACTION_LAND:
        result = land_task(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            actor=submission.worker_actor,
            summary=resolved_summary,
            validations=resolved_validations,
            residuals=resolved_residuals,
            followup_candidates=resolved_followups,
            note=note,
            cleanup=not keep_worktree,
        )
        decision_status = str(result.get("status") or "success")
    elif action == SUPERVISOR_DECISION_ACTION_RESTART:
        result = close_task(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            actor=submission.worker_actor,
            status="abandoned",
            summary=resolved_summary,
            validations=resolved_validations,
            residuals=resolved_residuals,
            followup_candidates=resolved_followups,
            note=note,
            cleanup=cleanup,
        )
        decision_status = str(result.get("status") or "abandoned")
    elif action == SUPERVISOR_DECISION_ACTION_CLOSE:
        resolved_close_status = _optional_text(close_status) or "blocked"
        if resolved_close_status not in {"blocked", "failed", "abandoned"}:
            raise SupervisorError("close_status must be one of blocked, failed, abandoned")
        result = close_task(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            actor=submission.worker_actor,
            status=resolved_close_status,
            summary=resolved_summary,
            validations=resolved_validations,
            residuals=resolved_residuals,
            followup_candidates=resolved_followups,
            note=note,
            cleanup=cleanup,
        )
        decision_status = str(result.get("status") or resolved_close_status)
    else:
        decision_status = "active"
    decision = _record_decision(
        profile,
        workset=workset,
        run=run,
        task_id=resolved_task,
        attempt_id=submission.attempt_id,
        actor=actor,
        action=action,
        status=decision_status,
        summary=resolved_summary,
        note=note,
    )
    append_event(
        profile.paths.events_file,
        event_type="supervisor.decide",
        actor=actor,
        payload={
            "run_id": run.run_id,
            "workset_id": resolved_workset,
            "task_id": resolved_task,
            "attempt_id": submission.attempt_id,
            "action": action,
            "status": decision_status,
            "summary": resolved_summary,
            "note": note,
        },
    )
    return (
        show_supervisor(
            profile,
            workset_id=resolved_workset,
            parallelism=run.parallelism,
            action="decide",
            note=note or resolved_summary,
        ),
        decision,
        result,
    )


def release_supervisor(
    profile: RepoProfile,
    *,
    workset_id: str,
    actor: str,
    summary: str | None = None,
    parallelism: int = 1,
    note: str | None = None,
) -> SupervisorStatus:
    run = _require_active_supervisor_run(profile, workset_id=workset_id, actor=actor)
    release_workset_manager(profile, workset_id=workset_id, actor=actor, summary=summary, note=note)
    workset = _supervisor_workset(profile, workset_id=workset_id)
    _release_supervisor_run(
        profile,
        workset=workset,
        run=run,
        parallelism=parallelism,
        summary=summary,
        note=note,
    )
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
        f"[blackdog-supervisor] landing_ready: {'yes' if status.landing_ready else 'no'}",
    ]
    if status.claim is not None:
        lines.append(
            "[blackdog-supervisor] claim: "
            f"{status.claim.get('actor')}/{status.claim.get('execution_model')} "
            f"claimed_at={status.claim.get('claimed_at')}"
        )
    else:
        lines.append("[blackdog-supervisor] claim: none")
    if status.supervisor_run is not None:
        lines.append(
            "[blackdog-supervisor] run: "
            f"{status.supervisor_run.get('run_id')} status={status.supervisor_run.get('status')} "
            f"bindings={len(status.supervisor_run.get('bindings') or [])}"
        )
    else:
        lines.append("[blackdog-supervisor] run: none")
    if status.landing_blockers:
        lines.append(f"[blackdog-supervisor] landing blockers: {', '.join(status.landing_blockers)}")
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
            lines.append(f"    worker: {dispatch.worker_actor_suggestion}")
            lines.append(f"    begin: {dispatch.task_begin_command}")
    if status.review_queue:
        lines.append("")
        lines.append("Review queue:")
        for item in status.review_queue:
            lines.append(
                f"  - {item['task_id']} {item['task_title']} attempt={item['attempt_id']} worker={item['worker_actor']}"
            )
            lines.append(f"    summary: {item['summary']}")
    if status.active_tasks:
        lines.append("")
        lines.append("Active:")
        active_bindings = _active_binding_index(
            tuple(
                SupervisorWorkerBinding(**binding)
                for binding in (status.supervisor_run or {}).get("bindings", [])
                if isinstance(binding, dict)
            )
        )
        for task in status.active_tasks:
            label = f"  - {task['task_id']} {task['title']}"
            if task["active_attempt_id"]:
                label = f"{label} attempt={task['active_attempt_id']}"
            if task["claim_actor"]:
                label = f"{label} claim={task['claim_actor']}/{task['claim_execution_model']}"
            binding = active_bindings.get(task["task_id"])
            if binding is not None:
                label = f"{label} binding={binding.binding_kind}:{binding.binding_id}"
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
    "bind_supervisor_worker",
    "checkpoint_supervisor",
    "decide_supervisor",
    "reconcile_supervisor",
    "release_supervisor",
    "render_supervisor_text",
    "show_supervisor",
    "start_supervisor",
    "submit_supervisor",
]
