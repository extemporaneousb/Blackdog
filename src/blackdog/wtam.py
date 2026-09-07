"""Task-only branch-backed execution and recovery.

This module is intentionally small enough to audit as one product protocol.  A
task owns its attempts; an in-progress attempt is the exclusive execution
claim.  Git worktrees are implementation details and never durable planning
objects.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import hashlib
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from blackdog.runtime_distribution import runtime_executable
from blackdog.contract import managed_skill_relative_path
from blackdog.guards import (
    GuardTaskInput,
    RepositoryGuardRefusal,
    evaluate_task_begin_guards,
)
from blackdog.handlers import (
    execute_worktree_handlers,
    plan_worktree_handlers,
    validate_existing_worktree_handlers,
)
from blackdog.landing import (
    LANDING_PHASES,
    LandingIntent,
    LandingTransaction,
    LandingTransactionError,
    append_worktree_land_once,
    attempt_lifecycle_lock,
    exact_worktree_land_event,
    landing_transaction_id,
    load_landing_transaction,
    record_landing_abort,
    record_landing_abort_cleanup,
    record_landing_abort_close_event,
    record_landing_abort_complete,
    record_landing_abort_runtime,
    record_landing_phase,
    strict_json_equal,
)
from blackdog.lifecycle import (
    CleanupEventFinalizationError,
    CleanupOwnershipError,
    CleanupPostMutationError,
    DirtyPrimaryWorktreeError,
    DirtyTargetWorktreeError,
    LifecycleAction,
    MissingTaskWorktreeError,
    NextAction,
    NoChangesToLandError,
    OperationResult,
    StaleTaskBranchError,
    WorktreeError,
    classify_lifecycle_exception,
)
from blackdog.prompt_artifacts import persist_prompt_receipts, verify_prompt_artifact
from blackdog.prompting import _compose_prompt
from blackdog.validation import run_validation_commands
from blackdog.codex_sessions import current_codex_runtime_context, current_codex_session_ref
from blackdog_core.profile import RepoProfile, slugify
from blackdog_core.state import (
    ATTEMPT_STATUS_ABANDONED,
    ATTEMPT_STATUS_BLOCKED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_IN_PROGRESS,
    ATTEMPT_STATUS_SUCCESS,
    CODEX_CAPTURE_MISSING_REASON_CAPTURE_ERROR,
    CODEX_CAPTURE_STATUS_MISSING,
    FAILURE_CLASS_STALE_BRANCH,
    PROMPT_MODE_RAW,
    PROMPT_MODE_SKILL,
    TASK_STATUS_BLOCKED,
    TASK_STATUS_CANCELED,
    TASK_STATUS_DONE,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_PLANNED,
    TaskAttemptRecord,
    TaskRecord,
    CodexSessionRefRecord,
    ValidationRecord,
    active_task_attempt,
    append_event_once,
    create_prompt_receipt,
    latest_task_attempt,
    load_events,
    load_runtime_state,
    new_task_id,
    now_iso,
    prompt_receipt_reference,
    task_record,
)
from blackdog_core.tasks import (
    TaskError,
    TaskFinalizationError,
    TaskRuntimeTransitionError,
    create_task,
    finish_task,
    inspect_task_finalization,
    inspect_task_runtime_transition,
    repair_task_start_events,
    reconcile_landed_attempt,
    set_task_runtime_status,
    start_task,
    task_resume_attempt_id,
)


WTAM_WORKTREE_VE_NOTE = (
    "Blackdog runs independently of .VE. Explicit project Python environments "
    "remain bound to their worktree; never copy virtual environments."
)
WORKSPACE_MODE_GIT_WORKTREE = "git-worktree"
WORKTREE_ROLE_PRIMARY = "primary"
WORKTREE_ROLE_TASK = "task"
WORKTREE_ROLE_LINKED = "linked"
SETUP_RECEIPT_SCHEMA_VERSION = 2
SKILL_PROVENANCE_SCHEMA_VERSION = 1
SKILL_PROVENANCE_SOURCE = "repo_managed"


class TaskBeginPreflightError(TaskError):
    """A task could not begin before an attempt was durably created."""

    def __init__(
        self,
        detail: str,
        *,
        failure_code: str = "setup_guard",
        action_id: str | None = None,
        reason_code: str | None = None,
        display: str | None = None,
        required_inputs: tuple[str, ...] = (),
    ) -> None:
        super().__init__(detail)
        self.failure_code = failure_code
        self.action_id = action_id or failure_code
        self.reason_code = reason_code or self.action_id
        self.display = display or "Repair task-begin preflight"
        self.required_inputs = required_inputs


@dataclass(frozen=True, slots=True)
class WorktreeSpec:
    task_id: str
    task_title: str
    task_slug: str
    branch: str
    base_ref: str
    base_commit: str
    target_branch: str
    worktree_path: str
    primary_worktree: str
    current_worktree: str
    attempt_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _AttemptWorktreeProof:
    valid: bool
    path: str | None
    branch: str | None
    registered_path: str | None
    head_commit: str | None
    start_commit: str | None
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_git(repo_root: Path, *args: str, input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _run_git_no_check(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _repo_root(path: Path) -> Path:
    return Path(_run_git(path, "rev-parse", "--show-toplevel")).resolve()


def _git_common_dir(path: Path) -> Path:
    root = _repo_root(path)
    value = Path(_run_git(root, "rev-parse", "--git-common-dir"))
    return value.resolve() if value.is_absolute() else (root / value).resolve()


def command_workspace_root(profile: RepoProfile, *, cwd: Path | None = None) -> Path:
    """Use the caller's linked worktree only when it belongs to this repository."""
    configured = _repo_root(profile.paths.project_root)
    candidate = (cwd or Path.cwd()).resolve()
    try:
        candidate_root = _repo_root(candidate)
        if _git_common_dir(candidate_root) == _git_common_dir(configured):
            return candidate_root
    except WorktreeError:
        pass
    return configured


def _parse_worktree_list(repo_root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in _run_git(repo_root, "worktree", "list", "--porcelain").splitlines():
        if not line.strip():
            if current:
                rows.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value.strip()
    if current:
        rows.append(current)
    return rows


def find_primary_worktree(project_root: Path) -> Path:
    root = _repo_root(project_root)
    for row in _parse_worktree_list(root):
        candidate = Path(str(row.get("worktree") or "")).resolve()
        if (candidate / ".git").is_dir():
            return candidate
    raise WorktreeError("could not find the primary worktree")


def _profile_rooted_at_primary_worktree(profile: RepoProfile) -> RepoProfile:
    """Retain loaded policy while moving command-local paths to the surviving root."""
    primary = find_primary_worktree(profile.paths.project_root)
    if profile.paths.project_root.resolve() == primary:
        return profile
    return replace(
        profile,
        paths=replace(
            profile.paths,
            project_root=primary,
            profile_file=primary / profile.paths.profile_file.name,
        ),
    )


def _find_worktree_for_branch(project_root: Path, branch: str) -> Path | None:
    root = _repo_root(project_root)
    branch_ref = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
    for row in _parse_worktree_list(root):
        if row.get("branch") == branch_ref:
            return Path(row["worktree"]).resolve()
    return None


def find_worktree_for_branch(profile: RepoProfile, branch: str) -> str | None:
    path = _find_worktree_for_branch(profile.paths.project_root, branch)
    return str(path) if path is not None else None


def _current_branch(repo_root: Path) -> str:
    branch = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise WorktreeError(f"detached HEAD at {repo_root}")
    return branch


def _is_within(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _status_entries(repo_root: Path) -> tuple[str, ...]:
    completed = _run_git_no_check(repo_root, "status", "--porcelain")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git status --porcelain failed: {detail}")
    paths: list[str] = []
    for line in completed.stdout.splitlines():
        paths.extend(item.strip() for item in line[3:].split(" -> ") if item.strip())
    return tuple(sorted(dict.fromkeys(paths)))


def _runtime_ignore_prefixes(profile: RepoProfile, repo_root: Path) -> tuple[str, ...]:
    try:
        relative = profile.paths.control_dir.resolve().relative_to(repo_root.resolve()).as_posix().rstrip("/")
    except ValueError:
        return ()
    return (f"{relative}/",)


def dirty_paths(
    repo_root: Path,
    *,
    ignore_paths: frozenset[str] = frozenset(),
    ignore_prefixes: tuple[str, ...] = (),
) -> list[str]:
    return [
        path
        for path in _status_entries(repo_root)
        if path not in ignore_paths and not any(path.startswith(prefix) for prefix in ignore_prefixes)
    ]


def _implementation_dirty_paths(profile: RepoProfile, repo_root: Path) -> list[str]:
    return dirty_paths(repo_root, ignore_prefixes=_runtime_ignore_prefixes(profile, repo_root))


def _task_slug(task: TaskRecord) -> str:
    return slugify(f"{task.task_id}-{task.title}")


def default_task_branch(task: TaskRecord) -> str:
    return f"agent/{_task_slug(task)}"


def default_task_worktree_path(profile: RepoProfile, *, task: TaskRecord) -> Path:
    return profile.paths.worktrees_dir / f"wt-{_task_slug(task)}"


def _resolve_from_ref(primary: Path, value: str | None, target_branch: str) -> str:
    if value is None:
        return target_branch
    for candidate in (value, f"origin/{value}"):
        if _run_git_no_check(primary, "rev-parse", "--verify", f"{candidate}^{{commit}}").returncode == 0:
            return candidate
    raise WorktreeError(f"could not resolve --from ref: {value}")


def _task_for_id(profile: RepoProfile, task_id: str) -> TaskRecord:
    task = task_record(load_runtime_state(profile.paths), task_id)
    if task is None:
        raise TaskError(f"Unknown task {task_id!r}")
    return task


def _task_for_command(
    profile: RepoProfile,
    *,
    task_id: str | None,
    cwd: Path | None,
    require_certified_cwd: bool = False,
) -> TaskRecord:
    state = load_runtime_state(profile.paths)
    selected = task_record(state, task_id) if task_id else None
    if task_id and selected is None:
        raise TaskError(f"Unknown task {task_id!r}")

    resolved_cwd = (cwd or profile.paths.project_root).resolve()
    configured = _repo_root(profile.paths.project_root)
    primary = find_primary_worktree(configured)
    expected_common = _git_common_dir(configured)
    try:
        current_root = _repo_root(resolved_cwd)
        same_repository = _git_common_dir(current_root) == expected_common
    except WorktreeError:
        current_root = None
        same_repository = False
    trusted_project_context = bool(same_repository and current_root == primary)
    certified_matches: list[TaskRecord] = []
    for task in state.tasks:
        attempt = active_task_attempt(state, task.task_id) or latest_task_attempt(state, task.task_id)
        if attempt is None:
            continue
        proof = _attempt_worktree_proof(profile, attempt)
        if (
            proof.valid
            and proof.path is not None
            and current_root == Path(proof.path)
            and same_repository
            and _is_within(Path(proof.path), resolved_cwd)
        ):
            certified_matches.append(task)

    if selected is not None:
        if not require_certified_cwd:
            return selected
        if trusted_project_context or selected in certified_matches:
            return selected
        raise TaskError(
            "task mutation requires the canonical primary workspace or the exact registered "
            f"durable worktree for task {selected.task_id!r} in the same Git repository"
        )
    if len(certified_matches) == 1:
        return certified_matches[0]
    if trusted_project_context and len(state.tasks) == 1:
        return state.tasks[0]
    raise TaskError(
        "task identity is not certified: use an explicit task id for read-only inspection, "
        "or run from the canonical primary workspace or exact registered durable task worktree"
    )


def _complete_action(action_id: str, detail: str, display: str) -> NextAction:
    return NextAction.terminal(
        action_id=action_id,
        kind="complete",
        disposition="complete",
        reason_code=action_id,
        reason_detail=detail,
        display=display,
    )


def _blocked_action(
    action_id: str,
    detail: str,
    display: str,
    *,
    required_inputs: tuple[str, ...] = (),
    reason_code: str | None = None,
) -> NextAction:
    return NextAction.terminal(
        action_id=action_id,
        kind="blocked",
        disposition="repair_required",
        reason_code=reason_code or action_id,
        reason_detail=detail,
        display=display,
        required_inputs=required_inputs,
    )


def _command_action(
    *,
    action_id: str,
    detail: str,
    display: str,
    argv: tuple[str, ...],
    safety: str,
    mutation: str,
    disposition: str = "continue",
) -> NextAction:
    return NextAction.command(
        LifecycleAction(
            action_id=action_id,
            disposition=disposition,
            reason_code=action_id,
            reason_detail=detail,
            argv=argv,
            safety_class=safety,
            mutation_class=mutation,
            display=display,
        )
    )


def _result(
    *,
    operation: str,
    operation_status: str,
    task_status: str | None,
    attempt_status: str | None,
    next_action: NextAction,
    payload: Mapping[str, Any],
    mutation_started: bool = False,
    mutation_completed: bool = False,
    mutation_phase: str = "none",
    failure_code: str | None = None,
) -> OperationResult:
    return OperationResult(
        operation=operation,
        operation_status=operation_status,
        task_status=task_status,
        attempt_status=attempt_status,
        disposition=next_action.disposition,
        mutation_started=mutation_started,
        mutation_completed=mutation_completed,
        mutation_phase=mutation_phase,
        failure_code=failure_code,
        next_action=next_action,
        legacy_payload=dict(payload),
    )


def _receipt_payload(receipt: Any) -> dict[str, Any] | None:
    if receipt is None:
        return None
    return {
        "prompt_hash": receipt.prompt_hash,
        "recorded_at": receipt.recorded_at,
        "source": receipt.source,
        "mode": receipt.mode,
        "replay_artifact_path": receipt.replay_artifact_path,
    }


def _attempt_payload(attempt: TaskAttemptRecord | None) -> dict[str, Any] | None:
    if attempt is None:
        return None
    return {
        "task_id": attempt.task_id,
        "attempt_id": attempt.attempt_id,
        "status": attempt.status,
        "actor": attempt.actor,
        "started_at": attempt.started_at,
        "ended_at": attempt.ended_at,
        "summary": attempt.summary,
        "worktree_path": attempt.worktree_path,
        "branch": attempt.branch,
        "target_branch": attempt.target_branch,
        "start_commit": attempt.start_commit,
        "commit": attempt.commit,
        "landed_commit": attempt.landed_commit,
        "changed_paths": list(attempt.changed_paths),
        "validations": [asdict(item) for item in attempt.validations],
        "failure_class": attempt.failure_class,
        "recovery_action": attempt.recovery_action,
        "prompt_receipt": _receipt_payload(attempt.prompt_receipt),
        "user_prompt_receipt": _receipt_payload(attempt.user_prompt_receipt),
        "setup_receipt": attempt.setup_receipt,
    }


def _task_payload(task: TaskRecord) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "status": task.status,
        "actor": task.actor,
        "note": task.note,
        "failure_class": task.failure_class,
        "recovery_action": task.recovery_action,
        "attempt_count": len(task.attempts),
    }


def _resolve_prompt_receipts(
    profile: RepoProfile,
    *,
    prompt: str,
    prompt_source: str | None,
    user_prompt: str | None,
    user_prompt_source: str | None,
    prompt_mode: str,
    canonical_execution_replay: Any | None = None,
) -> tuple[Any, Any]:
    if prompt_mode not in {PROMPT_MODE_RAW, PROMPT_MODE_SKILL}:
        raise TaskError(f"prompt mode must be one of {PROMPT_MODE_RAW}, {PROMPT_MODE_SKILL}")
    request_text = user_prompt if user_prompt is not None else prompt
    request_source = user_prompt_source if user_prompt is not None else prompt_source
    request_receipt = create_prompt_receipt(request_text, source=request_source, mode=PROMPT_MODE_RAW)
    execution_text = prompt
    if canonical_execution_replay is not None:
        execution_receipt = canonical_execution_replay
    elif prompt_mode == PROMPT_MODE_SKILL:
        execution_text, _documents = _compose_prompt(
            profile,
            request=prompt,
            include_skill_text=False,
            include_doc_text=False,
        )
        execution_receipt = create_prompt_receipt(
            execution_text,
            source=prompt_source,
            mode=prompt_mode,
        )
    else:
        execution_receipt = create_prompt_receipt(
            execution_text,
            source=prompt_source,
            mode=prompt_mode,
        )
    return request_receipt, execution_receipt


def _canonical_execution_replay(
    profile: RepoProfile,
    *,
    task: TaskRecord | None,
    prompt: str,
    prompt_source: str | None,
    prompt_mode: str,
) -> Any | None:
    """Recognize only the exact artifact emitted by a prior begin action.

    Skill-mode artifacts contain the already composed canonical execution
    prompt.  Replaying that file through the ordinary skill compiler would
    compose it a second time and change both the receipt and attempt identity.
    """

    if task is None or prompt_source is None:
        return None
    attempt = active_task_attempt(load_runtime_state(profile.paths), task.task_id) or (
        task.attempts[-1] if task.attempts else None
    )
    recorded = attempt.prompt_receipt if attempt is not None else None
    if (
        recorded is None
        or recorded.mode != prompt_mode
        or not recorded.replay_artifact_path
    ):
        return None
    artifact = verify_prompt_artifact(
        profile.paths.control_dir,
        prompt_hash=recorded.prompt_hash,
        replay_artifact_path=recorded.replay_artifact_path,
    )
    try:
        supplied = Path(prompt_source).expanduser().resolve()
    except OSError:
        return None
    if supplied != artifact:
        return None
    replay = create_prompt_receipt(
        prompt,
        source=recorded.source,
        mode=recorded.mode,
    )
    if replay.prompt_hash != recorded.prompt_hash:
        raise TaskError("canonical execution replay artifact conflicts with recorded prompt lineage")
    return replay


def _managed_skill_provenance(
    profile: RepoProfile,
    *,
    workspace_root: Path,
) -> dict[str, Any]:
    relative = managed_skill_relative_path(profile)
    path = workspace_root / relative
    try:
        content = path.read_bytes()
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise TaskBeginPreflightError(
            "task begin --prompt-mode skill requires a readable repo-managed skill at "
            f"{relative.as_posix()}: {detail}",
            failure_code="managed_skill_missing",
            action_id="managed_skill_required",
            reason_code="managed_skill_missing",
            display="Restore or refresh the repo-managed Blackdog skill",
            required_inputs=("managed_skill",),
        ) from exc
    return {
        "schema_version": SKILL_PROVENANCE_SCHEMA_VERSION,
        "path": relative.as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "source": SKILL_PROVENANCE_SOURCE,
    }


def _guard_task_start(
    profile: RepoProfile,
    *,
    actor: str,
    prompt_mode: str,
    execution_receipt: Any,
    request_receipt: Any,
) -> dict[str, Any]:
    try:
        receipts = evaluate_task_begin_guards(
            profile,
            task=GuardTaskInput(
                actor=actor,
                prompt_mode=prompt_mode,
                execution_prompt_text=execution_receipt.text or "",
                execution_prompt_hash=execution_receipt.prompt_hash,
                request_prompt_text=request_receipt.text or "",
                request_prompt_hash=request_receipt.prompt_hash,
            ),
        )
    except RepositoryGuardRefusal as exc:
        raise TaskBeginPreflightError(
            f"task start refused by {exc}",
            failure_code="setup_guard",
            action_id=exc.action_id,
            reason_code=exc.reason_code,
            display=exc.message,
            required_inputs=exc.required_inputs,
        ) from exc
    return {
        "schema_version": SETUP_RECEIPT_SCHEMA_VERSION,
        "status": "passed",
        "guard_receipts": [dict(receipt) for receipt in receipts],
    }


def _setup_receipt(
    handlers: Any,
    *,
    guard_receipt: Mapping[str, Any],
    skill_provenance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    handler_payload = dict(handlers.to_dict())
    actions = handler_payload.get("actions")
    action_rows = actions if isinstance(actions, list) else []
    ready_statuses = {"validated", "created", "updated", "preserved", "skipped"}
    probes = [
        {
            "name": f"{row.get('id')}.{row.get('action')}",
            "status": "ok" if row.get("status") in ready_statuses else "blocked",
            "handler_id": row.get("id"),
            "kind": row.get("kind"),
            "action": row.get("action"),
            "target_path": row.get("target_path"),
            "required": True,
            "message": row.get("message"),
            "elapsed_ms": row.get("elapsed_ms"),
        }
        for row in action_rows
        if isinstance(row, Mapping)
    ]
    blockers = [str(row["name"]) for row in probes if row["status"] == "blocked"]
    receipt = {
        "schema_version": SETUP_RECEIPT_SCHEMA_VERSION,
        "checked_at": now_iso(),
        "status": "ok" if bool(handler_payload.get("ready")) and not blockers else "blocked",
        "blockers": blockers,
        "guard_receipts": [
            dict(item) for item in guard_receipt.get("guard_receipts", ())
        ],
        "workspace_ve": handler_payload.get("worktree_ve_path"),
        "workspace_blackdog_path": handler_payload.get("blackdog_path"),
        "runtime_mode": handler_payload.get("runtime_mode"),
        "source_mode": handler_payload.get("source_mode"),
        "script_policy": handler_payload.get("script_policy"),
        "probes": probes,
    }
    if skill_provenance is not None:
        receipt["skill_provenance"] = dict(skill_provenance)
    return receipt


def _persist_prompt_receipts(profile: RepoProfile, request_receipt: Any, execution_receipt: Any) -> tuple[Any, Any]:
    return persist_prompt_receipts(
        profile.paths.control_dir,
        (request_receipt, execution_receipt),
    )


def _same_prompt_lineage(attempt: TaskAttemptRecord, *, actor: str, request_receipt: Any, execution_receipt: Any) -> bool:
    return bool(
        attempt.actor == actor
        and attempt.prompt_receipt is not None
        and attempt.user_prompt_receipt is not None
        and attempt.prompt_receipt.prompt_hash == execution_receipt.prompt_hash
        and attempt.prompt_receipt.mode == execution_receipt.mode
        and attempt.prompt_receipt.source == execution_receipt.source
        and attempt.user_prompt_receipt.prompt_hash == request_receipt.prompt_hash
        and attempt.user_prompt_receipt.mode == request_receipt.mode
        and attempt.user_prompt_receipt.source == request_receipt.source
    )


def _begin_payload(
    task: TaskRecord,
    attempt: TaskAttemptRecord,
    *,
    primary: Path,
    current: Path,
    include_prompt: bool,
) -> dict[str, Any]:
    execution = _receipt_payload(attempt.prompt_receipt)
    request = _receipt_payload(attempt.user_prompt_receipt)
    return {
        **_task_payload(task),
        "attempt_id": attempt.attempt_id,
        "attempt_status": attempt.status,
        "branch": attempt.branch,
        "target_branch": attempt.target_branch,
        "base_commit": attempt.start_commit,
        "worktree_path": attempt.worktree_path,
        "primary_worktree": str(primary),
        "current_worktree": str(current),
        "workspace_role": WORKTREE_ROLE_TASK,
        "workspace_mode": WORKSPACE_MODE_GIT_WORKTREE,
        "prompt_hash": execution["prompt_hash"] if execution else None,
        "prompt_mode": execution["mode"] if execution else None,
        "prompt_source": execution["source"] if execution else None,
        "execution_prompt_replay_artifact_path": execution["replay_artifact_path"] if execution else None,
        "user_prompt_hash": request["prompt_hash"] if request else None,
        "user_prompt_mode": request["mode"] if request else None,
        "user_prompt_source": request["source"] if request else None,
        "user_prompt_replay_artifact_path": request["replay_artifact_path"] if request else None,
        "prompt_text": attempt.prompt_receipt.text if include_prompt and attempt.prompt_receipt else None,
        "setup_receipt": attempt.setup_receipt,
        "ve_expectation": WTAM_WORKTREE_VE_NOTE,
    }


def begin_task_worktree(
    profile: RepoProfile,
    *,
    actor: str,
    prompt: str,
    prompt_source: str | None = None,
    user_prompt: str | None = None,
    user_prompt_source: str | None = None,
    prompt_mode: str = PROMPT_MODE_RAW,
    task_id: str | None = None,
    title: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    branch: str | None = None,
    from_ref: str | None = None,
    path: str | None = None,
    cwd: Path | None = None,
    note: str | None = None,
    include_prompt: bool = False,
) -> OperationResult:
    """Create a task when needed and idempotently start its one active attempt."""
    current = command_workspace_root(profile, cwd=cwd)
    primary = find_primary_worktree(profile.paths.project_root)
    state = load_runtime_state(profile.paths)
    existing = task_record(state, task_id) if task_id is not None else None
    canonical_replay = _canonical_execution_replay(
        profile,
        task=existing,
        prompt=prompt,
        prompt_source=prompt_source,
        prompt_mode=prompt_mode,
    )
    request_receipt, execution_receipt = _resolve_prompt_receipts(
        profile,
        prompt=prompt,
        prompt_source=prompt_source,
        user_prompt=user_prompt,
        user_prompt_source=user_prompt_source,
        prompt_mode=prompt_mode,
        canonical_execution_replay=canonical_replay,
    )
    skill_provenance = (
        _managed_skill_provenance(profile, workspace_root=current)
        if prompt_mode == PROMPT_MODE_SKILL
        else None
    )
    resolved_task_id = task_id or new_task_id()
    task = existing or TaskRecord(
        task_id=resolved_task_id,
        title=title or _derive_task_title(user_prompt or prompt),
    )
    if existing is not None and title is not None and title.strip() != existing.title:
        raise TaskError(f"Task identity {task_id!r} already has a different title")

    active = active_task_attempt(state, task.task_id)
    if active is not None:
        if not _same_prompt_lineage(
            active,
            actor=actor,
            request_receipt=request_receipt,
            execution_receipt=execution_receipt,
        ):
            raise TaskError(
                f"Task {task.task_id!r} already has active attempt {active.attempt_id!r} with different ownership or prompt lineage"
            )
        worktree_proof = _attempt_worktree_proof(profile, active)
        if not worktree_proof.valid:
            return _result(
                operation="task.begin",
                operation_status="blocked",
                task_status=task.status,
                attempt_status=active.status,
                next_action=_close_choices(profile, task, active),
                payload={
                    **_task_payload(task),
                    "attempt": _attempt_payload(active),
                    "worktree_proof": worktree_proof.to_dict(),
                },
                failure_code=None,
            )
        assert active.worktree_path is not None
        setup = validate_existing_worktree_handlers(
            profile,
            worktree_path=Path(active.worktree_path),
        )
        if not setup.ready:
            return _result(
                operation="task.begin",
                operation_status="blocked",
                task_status=task.status,
                attempt_status=active.status,
                next_action=_blocked_action(
                    "handler_setup_invalid",
                    setup.remediation or "retained task handlers are not ready",
                    "Repair the retained task handler setup",
                ),
                payload={**_task_payload(task), "attempt": _attempt_payload(active), "setup_receipt": setup.to_dict()},
                failure_code="setup_guard",
            )
        events_before = profile.paths.events_file.read_bytes() if profile.paths.events_file.is_file() else b""
        try:
            repair_task_start_events(profile, task_id=task.task_id, attempt_id=active.attempt_id)
        except Exception as exc:
            argv = _resume_begin_argv(profile, task, active)
            next_action = (
                _command_action(
                    action_id="retry_task_start_finalization",
                    detail=f"The active attempt and workspace are durable, but start evidence is incomplete: {exc}",
                    display="Retry the exact task begin request",
                    argv=argv,
                    safety="proof_guarded_mutation",
                    mutation="runtime",
                )
                if argv is not None
                else _blocked_action(
                    "task_start_evidence_incomplete",
                    str(exc),
                    "Preserve and inspect the active task workspace",
                )
            )
            return _result(
                operation="task.begin",
                operation_status="partial",
                task_status=task.status,
                attempt_status=active.status,
                next_action=next_action,
                payload={**_task_payload(task), "attempt": _attempt_payload(active), "error": str(exc)},
                mutation_started=True,
                mutation_completed=False,
                mutation_phase="event_finalization_partial",
                failure_code=classify_lifecycle_exception(exc).failure_code,
            )
        events_after = profile.paths.events_file.read_bytes() if profile.paths.events_file.is_file() else b""
        payload = _begin_payload(task, active, primary=primary, current=current, include_prompt=include_prompt)
        return _result(
            operation="task.begin",
            operation_status="succeeded",
            task_status=TASK_STATUS_IN_PROGRESS,
            attempt_status=ATTEMPT_STATUS_IN_PROGRESS,
            next_action=_complete_action(
                "task_attempt_ready",
                "The exact task attempt and workspace already exist with matching prompt lineage.",
                "Continue in the task workspace",
            ),
            payload=payload,
            mutation_started=events_before != events_after,
            mutation_completed=events_before != events_after,
            mutation_phase="event_finalized" if events_before != events_after else "none",
        )
    if task.status == TASK_STATUS_DONE:
        raise TaskError(f"Task {task.task_id!r} is complete")
    if task.status == TASK_STATUS_CANCELED:
        raise TaskError(f"Task {task.task_id!r} is canceled; reopen it before beginning another attempt")
    if task.status != TASK_STATUS_PLANNED:
        raise TaskError(
            f"Task {task.task_id!r} is {task.status!r}; cancel and reopen it before beginning another attempt"
        )

    guard_receipt = _guard_task_start(
        profile,
        actor=actor,
        prompt_mode=prompt_mode,
        execution_receipt=execution_receipt,
        request_receipt=request_receipt,
    )

    latest = latest_task_attempt(state, task.task_id)
    if latest is not None and latest.worktree_path and latest.branch:
        retained_path = Path(latest.worktree_path).resolve()
        retained_proof = _attempt_worktree_proof(profile, latest)
        if retained_proof.valid:
            if not _same_prompt_lineage(
                latest,
                actor=actor,
                request_receipt=request_receipt,
                execution_receipt=execution_receipt,
            ):
                raise TaskError(
                    f"Task {task.task_id!r} retained workspace may only resume with the predecessor actor and exact prompt lineage"
                )
            if path is not None and Path(path).expanduser().resolve() != retained_path:
                raise TaskError("resume path conflicts with the retained task workspace")
            if branch is not None and branch != latest.branch:
                raise TaskError("resume branch conflicts with the retained task workspace")
            setup = validate_existing_worktree_handlers(profile, worktree_path=retained_path)
            if not setup.ready:
                raise TaskBeginPreflightError(setup.remediation or "retained task handlers are not ready")
            request_receipt, execution_receipt = _persist_prompt_receipts(
                profile, request_receipt, execution_receipt
            )
            runtime_context = current_codex_runtime_context()
            try:
                codex_session = current_codex_session_ref(
                    user_prompt_hash=request_receipt.prompt_hash,
                    execution_prompt_hash=execution_receipt.prompt_hash,
                )
            except Exception:
                codex_session = None
            resumed = start_task(
                profile,
                task_id=task.task_id,
                actor=actor,
                workspace_identity=task.task_id,
                workspace_mode=WORKSPACE_MODE_GIT_WORKTREE,
                worktree_role=WORKTREE_ROLE_TASK,
                worktree_path=str(retained_path),
                branch=latest.branch,
                target_branch=latest.target_branch,
                integration_branch=latest.integration_branch or latest.branch,
                start_commit=_run_git(retained_path, "rev-parse", "HEAD"),
                model=model or runtime_context.model,
                reasoning_effort=reasoning_effort or runtime_context.reasoning_effort,
                codex_session=codex_session,
                prompt_receipt=prompt_receipt_reference(execution_receipt),
                user_prompt_receipt=prompt_receipt_reference(request_receipt),
                note=note,
                setup_receipt=_setup_receipt(
                    setup,
                    guard_receipt=guard_receipt,
                    skill_provenance=skill_provenance,
                ),
                attempt_id=task_resume_attempt_id(
                    task_id=task.task_id,
                    predecessor_attempt_id=latest.attempt_id,
                    actor=actor,
                    execution_prompt_hash=execution_receipt.prompt_hash,
                    request_prompt_hash=request_receipt.prompt_hash,
                ),
            )
            task = _task_for_id(profile, task.task_id)
            return _result(
                operation="task.begin",
                operation_status="succeeded",
                task_status=task.status,
                attempt_status=resumed.status,
                next_action=_complete_action(
                    "task_attempt_ready",
                    "A new attempt now owns the exact retained task workspace.",
                    "Continue in the retained task workspace",
                ),
                payload=_begin_payload(task, resumed, primary=primary, current=current, include_prompt=include_prompt),
                mutation_started=True,
                mutation_completed=True,
                mutation_phase="workspace_started",
            )
        if retained_path.exists() or _find_worktree_for_branch(primary, latest.branch) is not None:
            raise TaskError(
                "retained task workspace failed exact Git ownership or start-lineage proof: "
                f"{retained_proof.reason}"
            )

    target_branch = _current_branch(current)
    if current == primary and _implementation_dirty_paths(profile, primary):
        raise TaskBeginPreflightError(
            "implementation changes are present in the primary worktree; task begin refuses to hide or move them"
        )
    base_ref = _resolve_from_ref(primary, from_ref, target_branch)
    base_commit = _run_git(primary, "rev-parse", f"{base_ref}^{{commit}}")
    resolved_branch = branch or default_task_branch(task)
    resolved_path = Path(path).expanduser().resolve() if path else default_task_worktree_path(profile, task=task).resolve()
    if _is_within(primary, resolved_path):
        raise WorktreeError(f"refusing worktree path inside the primary repository: {resolved_path}")
    if _find_worktree_for_branch(primary, resolved_branch) is not None:
        raise WorktreeError(f"branch already has a worktree: {resolved_branch}")
    if resolved_path.exists():
        raise WorktreeError(f"worktree path already exists: {resolved_path}")
    handler_plan = plan_worktree_handlers(profile, worktree_path=resolved_path)
    if not handler_plan.ready:
        raise TaskBeginPreflightError(
            handler_plan.remediation or "worktree handlers cannot produce a ready task workspace"
        )

    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        _run_git(primary, "worktree", "add", str(resolved_path), "-b", resolved_branch, base_ref)
        created = True
        setup = execute_worktree_handlers(profile, worktree_path=resolved_path)
        if not setup.ready:
            raise TaskBeginPreflightError(setup.remediation or "worktree handlers did not produce a ready workspace")
        request_receipt, execution_receipt = _persist_prompt_receipts(
            profile, request_receipt, execution_receipt
        )
        if existing is None:
            task = create_task(
                profile,
                task_id=task.task_id,
                title=task.title,
            )
        runtime_context = current_codex_runtime_context()
        try:
            codex_session = current_codex_session_ref(
                user_prompt_hash=request_receipt.prompt_hash,
                execution_prompt_hash=execution_receipt.prompt_hash,
            )
        except Exception:
            thread_id = str(os.environ.get("CODEX_THREAD_ID") or "").strip()
            codex_session = (
                CodexSessionRefRecord(
                    thread_id=thread_id,
                    user_prompt_hash=request_receipt.prompt_hash,
                    execution_prompt_hash=execution_receipt.prompt_hash,
                    capture_status=CODEX_CAPTURE_STATUS_MISSING,
                    capture_missing_reason=CODEX_CAPTURE_MISSING_REASON_CAPTURE_ERROR,
                )
                if thread_id
                else None
            )
        attempt = start_task(
            profile,
            task_id=task.task_id,
            actor=actor,
            workspace_identity=task.task_id,
            workspace_mode=WORKSPACE_MODE_GIT_WORKTREE,
            worktree_role=WORKTREE_ROLE_TASK,
            worktree_path=str(resolved_path),
            branch=resolved_branch,
            target_branch=target_branch,
            integration_branch=resolved_branch,
            start_commit=base_commit,
            model=model or runtime_context.model,
            reasoning_effort=reasoning_effort or runtime_context.reasoning_effort,
            codex_session=codex_session,
            prompt_receipt=prompt_receipt_reference(execution_receipt),
            user_prompt_receipt=prompt_receipt_reference(request_receipt),
            note=note,
            setup_receipt=_setup_receipt(
                setup,
                guard_receipt=guard_receipt,
                skill_provenance=skill_provenance,
            ),
        )
    except Exception as exc:
        observed_task = task_record(load_runtime_state(profile.paths), task.task_id)
        observed_active = (
            active_task_attempt(load_runtime_state(profile.paths), task.task_id)
            if observed_task is not None
            else None
        )
        if observed_task is not None and observed_active is not None:
            argv = _resume_begin_argv(profile, observed_task, observed_active)
            next_action = (
                _command_action(
                    action_id="retry_task_start_finalization",
                    detail=f"The attempt and task workspace are durable, but start evidence is incomplete: {exc}",
                    display="Retry the exact task begin request",
                    argv=argv,
                    safety="proof_guarded_mutation",
                    mutation="runtime",
                )
                if argv is not None
                else _blocked_action(
                    "task_start_evidence_incomplete",
                    str(exc),
                    "Preserve and inspect the active task workspace",
                )
            )
            return _result(
                operation="task.begin",
                operation_status="partial",
                task_status=observed_task.status,
                attempt_status=observed_active.status,
                next_action=next_action,
                payload={
                    **_task_payload(observed_task),
                    "attempt": _attempt_payload(observed_active),
                    "error": str(exc),
                },
                mutation_started=True,
                mutation_completed=False,
                mutation_phase="event_finalization_partial",
                failure_code=classify_lifecycle_exception(exc).failure_code,
            )
        if created:
            _run_git_no_check(primary, "worktree", "remove", "--force", str(resolved_path))
            _run_git_no_check(primary, "branch", "-D", resolved_branch)
        raise
    task = _task_for_id(profile, task.task_id)
    payload = _begin_payload(task, attempt, primary=primary, current=current, include_prompt=include_prompt)
    return _result(
        operation="task.begin",
        operation_status="succeeded",
        task_status=TASK_STATUS_IN_PROGRESS,
        attempt_status=ATTEMPT_STATUS_IN_PROGRESS,
        next_action=_complete_action(
            "task_attempt_ready",
            "The task attempt, prompt receipts, handler receipt, branch, and worktree are durable.",
            "Continue in the task workspace",
        ),
        payload=payload,
        mutation_started=True,
        mutation_completed=True,
        mutation_phase="workspace_started",
    )


def _derive_task_title(prompt: str) -> str:
    normalized = " ".join(str(prompt).split())
    return normalized[:96] if normalized else "Blackdog task"


def task_begin_preflight_result(
    exc: TaskBeginPreflightError,
    *,
    actor: str,
    prompt_mode: str,
    task_id: str | None,
) -> OperationResult:
    return _result(
        operation="task.begin",
        operation_status="blocked",
        task_status=None,
        attempt_status=None,
        next_action=_blocked_action(
            exc.action_id,
            str(exc),
            exc.display,
            required_inputs=exc.required_inputs,
            reason_code=exc.reason_code,
        ),
        payload={"task_id": task_id, "actor": actor, "prompt_mode": prompt_mode, "error": str(exc)},
        failure_code=exc.failure_code,
        mutation_phase="preflight",
    )


def worktree_contract(
    profile: RepoProfile,
    *,
    workspace: Path | None = None,
    workspace_mode: str | None = None,
) -> dict[str, Any]:
    current = command_workspace_root(profile, cwd=workspace)
    primary = find_primary_worktree(profile.paths.project_root)
    branch = _current_branch(current)
    state = load_runtime_state(profile.paths)
    task_branch = any(
        attempt.branch == branch
        for task in state.tasks
        for attempt in task.attempts
        if attempt.status == ATTEMPT_STATUS_IN_PROGRESS
    )
    role = WORKTREE_ROLE_PRIMARY if current == primary else WORKTREE_ROLE_TASK if task_branch else WORKTREE_ROLE_LINKED
    blackdog_path = Path(runtime_executable(profile.paths.project_root, profile=profile))
    return {
        "workspace_mode": workspace_mode or WORKSPACE_MODE_GIT_WORKTREE,
        "current_worktree": str(current),
        "current_branch": branch,
        "current_is_primary": current == primary,
        "workspace_role": role,
        "primary_worktree": str(primary),
        "primary_branch": _current_branch(primary),
        "target_branch": _current_branch(current),
        "primary_dirty": bool(_implementation_dirty_paths(profile, primary)),
        "primary_dirty_paths": _implementation_dirty_paths(profile, primary),
        "workspace_ve": str(current / ".VE"),
        "workspace_blackdog_path": str(blackdog_path),
        "workspace_has_local_blackdog": blackdog_path.is_file() and os.access(blackdog_path, os.X_OK),
        "ve_expectation": WTAM_WORKTREE_VE_NOTE,
    }


def worktree_preflight(profile: RepoProfile, *, cwd: Path | None = None) -> dict[str, Any]:
    resolved_cwd = (cwd or Path.cwd()).resolve()
    current = command_workspace_root(profile, cwd=resolved_cwd)
    contract = worktree_contract(profile, workspace=current)
    primary = Path(contract["primary_worktree"])
    worktrees = []
    for row in _parse_worktree_list(primary):
        path = Path(row.get("worktree") or "").resolve()
        branch = (row.get("branch") or "").removeprefix("refs/heads/")
        worktrees.append({"path": str(path), "branch": branch, "is_primary": path == primary})
    return {
        "project_root": str(profile.paths.project_root),
        "repo_root": str(current),
        "cwd": str(resolved_cwd),
        **contract,
        "dirty": bool(dirty_paths(current)),
        "implementation_dirty": bool(_implementation_dirty_paths(profile, current)),
        "landing_state": "blocked" if contract["primary_dirty"] else "ready",
        "worktrees_dir": str(profile.paths.worktrees_dir),
        "worktrees_dir_inside_repo": _is_within(primary, profile.paths.worktrees_dir),
        "worktrees": worktrees,
    }


def _git_exists(repo_root: Path, ref: str | None) -> bool:
    return bool(ref) and _run_git_no_check(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}").returncode == 0


def _ahead(repo_root: Path, branch: str | None, target: str | None) -> bool:
    if not _git_exists(repo_root, branch) or not _git_exists(repo_root, target):
        return False
    return _run_git_no_check(repo_root, "merge-base", "--is-ancestor", str(branch), str(target)).returncode != 0


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    return (
        _run_git_no_check(
            repo_root,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ).returncode
        == 0
    )


def _attempt_worktree_proof(
    profile: RepoProfile,
    attempt: TaskAttemptRecord,
) -> _AttemptWorktreeProof:
    path = Path(attempt.worktree_path).resolve() if attempt.worktree_path else None
    primary = find_primary_worktree(profile.paths.project_root)
    branch = attempt.branch
    registered = _find_worktree_for_branch(primary, branch) if branch else None

    def result(
        valid: bool,
        reason: str | None,
        *,
        head: str | None = None,
    ) -> _AttemptWorktreeProof:
        return _AttemptWorktreeProof(
            valid=valid,
            path=str(path) if path is not None else None,
            branch=branch,
            registered_path=str(registered) if registered is not None else None,
            head_commit=head,
            start_commit=attempt.start_commit,
            reason=reason,
        )

    if (
        attempt.workspace_mode != WORKSPACE_MODE_GIT_WORKTREE
        or attempt.worktree_role != WORKTREE_ROLE_TASK
    ):
        return result(False, "attempt does not declare the task Git-worktree contract")
    if path is None or branch is None or attempt.start_commit is None:
        return result(False, "attempt worktree, branch, or start lineage metadata is missing")
    if path == primary:
        return result(False, "attempt resolves to the primary worktree")
    if registered != path:
        return result(False, "attempt branch is not registered at the durable task-worktree path")
    if not path.is_dir():
        return result(False, "registered task-worktree path is missing")
    try:
        if _current_branch(path) != branch:
            return result(False, "registered task worktree is checked out on a different branch")
        head = _run_git(path, "rev-parse", "HEAD^{commit}")
        branch_head = _run_git(primary, "rev-parse", f"refs/heads/{branch}^{{commit}}")
        if head != branch_head:
            return result(False, "task branch and registered worktree HEAD disagree", head=head)
        lineage = _run_git_no_check(
            primary,
            "merge-base",
            "--is-ancestor",
            attempt.start_commit,
            head,
        )
        if lineage.returncode != 0:
            return result(
                False,
                "task worktree HEAD is not descended from its recorded start commit",
                head=head,
            )
    except (OSError, WorktreeError) as exc:
        return result(False, f"task-worktree Git proof failed: {exc}")
    return result(True, None, head=head)


def build_worktree_table(profile: RepoProfile) -> dict[str, Any]:
    primary = find_primary_worktree(profile.paths.project_root)
    state = load_runtime_state(profile.paths)
    rows: list[dict[str, Any]] = []
    for task in state.tasks:
        for attempt in reversed(task.attempts):
            path = Path(attempt.worktree_path).resolve() if attempt.worktree_path else None
            worktree_exists = bool(path and path.is_dir())
            if attempt.status != ATTEMPT_STATUS_IN_PROGRESS and not worktree_exists:
                continue
            rows.append(
                {
                    "task_id": task.task_id,
                    "task_title": task.title,
                    "task_status": task.status,
                    "attempt_id": attempt.attempt_id,
                    "attempt_status": attempt.status,
                    "actor": attempt.actor,
                    "branch": attempt.branch,
                    "target_branch": attempt.target_branch,
                    "worktree_path": str(path) if path else None,
                    "worktree_exists": worktree_exists,
                    "worktree_dirty": bool(worktree_exists and path and _implementation_dirty_paths(profile, path)),
                    "branch_exists": _git_exists(primary, attempt.branch),
                    "branch_ahead": _ahead(primary, attempt.branch, attempt.target_branch),
                    "landed_commit": attempt.landed_commit,
                }
            )
    return {
        "project_root": str(profile.paths.project_root),
        "primary_worktree": str(primary),
        "rows": rows,
        "counts": {
            "tasks": len({row["task_id"] for row in rows}),
            "attempts": len(rows),
            "active_attempts": sum(row["attempt_status"] == ATTEMPT_STATUS_IN_PROGRESS for row in rows),
            "retained_worktrees": sum(
                row["attempt_status"] != ATTEMPT_STATUS_IN_PROGRESS and bool(row["worktree_exists"])
                for row in rows
            ),
        },
    }


def _task_executable(profile: RepoProfile) -> str:
    return runtime_executable(profile.paths.project_root, profile=profile)


def _resume_begin_argv(profile: RepoProfile, task: TaskRecord, attempt: TaskAttemptRecord) -> tuple[str, ...] | None:
    execution = attempt.prompt_receipt
    request = attempt.user_prompt_receipt
    if (
        execution is None
        or request is None
        or not execution.replay_artifact_path
        or not request.replay_artifact_path
    ):
        return None
    execution_path = verify_prompt_artifact(
        profile.paths.control_dir,
        prompt_hash=execution.prompt_hash,
        replay_artifact_path=execution.replay_artifact_path,
    )
    request_path = verify_prompt_artifact(
        profile.paths.control_dir,
        prompt_hash=request.prompt_hash,
        replay_artifact_path=request.replay_artifact_path,
    )
    return (
        _task_executable(profile),
        "task",
        "begin",
        f"--project-root={profile.paths.project_root}",
        f"--task={task.task_id}",
        f"--actor={attempt.actor}",
        f"--execution-prompt-file={execution_path}",
        f"--prompt-mode={execution.mode or PROMPT_MODE_RAW}",
        f"--request-file={request_path}",
        f"--request-source={request.source or ''}",
    )


def _durable_task_bytes(profile: RepoProfile) -> tuple[bytes, bytes]:
    runtime = profile.paths.runtime_file.read_bytes() if profile.paths.runtime_file.is_file() else b""
    events = profile.paths.events_file.read_bytes() if profile.paths.events_file.is_file() else b""
    return runtime, events


def _recover_argv(
    profile: RepoProfile,
    *,
    task_id: str,
    status: str,
    summary: str,
    note: str | None,
) -> tuple[str, ...]:
    argv = [
        _task_executable(profile),
        "task",
        "recover",
        f"--project-root={profile.paths.project_root}",
        f"--task={task_id}",
        f"--status={status}",
        f"--summary={summary}",
    ]
    if note is not None:
        argv.append(f"--note={note}")
    return tuple(argv)


def _cancel_argv(
    profile: RepoProfile,
    *,
    task_id: str,
    actor: str,
    summary: str | None,
    failure_class: str | None,
    recovery_action: str | None,
    prompt_issue: bool,
    operator_issue: bool,
) -> tuple[str, ...]:
    argv = [
        _task_executable(profile),
        "task",
        "cancel",
        f"--project-root={profile.paths.project_root}",
        f"--task={task_id}",
        f"--actor={actor}",
    ]
    if summary is not None:
        argv.append(f"--summary={summary}")
    if failure_class is not None:
        argv.append(f"--failure-class={failure_class}")
    if recovery_action is not None:
        argv.append(f"--recovery-action={recovery_action}")
    if prompt_issue:
        argv.append("--prompt-issue")
    if operator_issue:
        argv.append("--operator-issue")
    return tuple(argv)


def _reopen_argv(
    profile: RepoProfile,
    *,
    task_id: str,
    actor: str,
    summary: str | None,
) -> tuple[str, ...]:
    argv = [
        _task_executable(profile),
        "task",
        "reopen",
        f"--project-root={profile.paths.project_root}",
        f"--task={task_id}",
        f"--actor={actor}",
    ]
    if summary is not None:
        argv.append(f"--summary={summary}")
    return tuple(argv)


def _transition_request_exists(
    profile: RepoProfile,
    *,
    task_id: str,
    actor: str,
    status: str,
    summary: str | None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
) -> bool:
    desired = {
        "task_id": task_id,
        "actor": actor.strip(),
        "status": status,
        "summary": summary.strip() if summary is not None else None,
        "failure_class": failure_class,
        "recovery_action": recovery_action.strip() if recovery_action is not None else None,
        "prompt_issue": prompt_issue,
        "operator_issue": operator_issue,
    }
    return any(
        event.get("type") == "task.runtime-transition.request"
        and isinstance(event.get("payload"), Mapping)
        and all(event["payload"].get(key) == value for key, value in desired.items())
        for event in load_events(profile.paths.events_file)
    )


def _transition_conflict_result(
    profile: RepoProfile,
    *,
    operation: str,
    task_id: str,
    exc: TaskRuntimeTransitionError,
    evidence: Mapping[str, Any] | None = None,
) -> OperationResult:
    current = _task_for_id(profile, task_id)
    return _result(
        operation=operation,
        operation_status="blocked",
        task_status=current.status,
        attempt_status=current.attempts[-1].status if current.attempts else None,
        next_action=_blocked_action(
            "task_transition_retry_conflict",
            str(exc),
            "Retry only the exact durable task transition request",
            required_inputs=("exact_transition_request",),
        ),
        payload={
            **_task_payload(current),
            "transition": dict(evidence or {}),
            "error": str(exc),
        },
        mutation_started=False,
        mutation_completed=False,
        mutation_phase="none",
        failure_code=classify_lifecycle_exception(exc).failure_code,
    )


def _close_choices(profile: RepoProfile, task: TaskRecord, attempt: TaskAttemptRecord) -> NextAction:
    del profile, task, attempt
    return _blocked_action(
        "close_evidence_required",
        "Closing an active attempt requires an explicit terminal status, nonblank completion summary, and validation evidence.",
        "Supply exact close evidence",
        required_inputs=("status", "summary", "validation"),
    )


def _close_argv(
    profile: RepoProfile,
    *,
    task_id: str,
    actor: str,
    status: str,
    summary: str,
    validations: tuple[ValidationRecord, ...],
    residuals: tuple[str, ...],
    followup_candidates: tuple[str, ...],
    note: str | None,
    cleanup: bool | None,
    failure_class: str | None,
    recovery_action: str | None,
    prompt_issue: bool,
    operator_issue: bool,
) -> tuple[str, ...]:
    argv = [
        _task_executable(profile),
        "task",
        "close",
        f"--project-root={profile.paths.project_root}",
        f"--task={task_id}",
        f"--actor={actor}",
        f"--status={status}",
        f"--summary={summary}",
    ]
    argv.extend(f"--validation={item.name}={item.status}" for item in validations)
    argv.extend(f"--residual={item}" for item in residuals)
    argv.extend(f"--followup={item}" for item in followup_candidates)
    if note is not None:
        argv.append(f"--note={note}")
    if cleanup:
        argv.append("--cleanup")
    if failure_class is not None:
        argv.append(f"--failure-class={failure_class}")
    if recovery_action is not None:
        argv.append(f"--recovery-action={recovery_action}")
    if prompt_issue:
        argv.append("--prompt-issue")
    if operator_issue:
        argv.append("--operator-issue")
    return tuple(argv)


def _cancel_blocked_task_action(profile: RepoProfile, task: TaskRecord, attempt: TaskAttemptRecord) -> NextAction:
    return _command_action(
        action_id="cancel_inactive_blocked_task",
        detail="The terminal blocked attempt must be canceled before its task can be reopened.",
        display="Cancel the inactive blocked task",
        argv=(
            _task_executable(profile),
            "task",
            "cancel",
            f"--project-root={profile.paths.project_root}",
            f"--task={task.task_id}",
            f"--actor={attempt.actor}",
            "--summary=Cancel inactive blocked task before retry",
        ),
        safety="proof_guarded_mutation",
        mutation="runtime",
    )


def _task_state_result(profile: RepoProfile, task: TaskRecord, *, operation: str) -> OperationResult:
    state = load_runtime_state(profile.paths)
    active = active_task_attempt(state, task.task_id)
    latest = latest_task_attempt(state, task.task_id)
    attempt = active or latest
    path = Path(attempt.worktree_path).resolve() if attempt and attempt.worktree_path else None
    primary = find_primary_worktree(profile.paths.project_root)
    proof = _attempt_worktree_proof(profile, attempt) if attempt is not None else None
    proven = bool(proof and proof.valid)
    payload = {
        **_task_payload(task),
        "active_attempt": _attempt_payload(active),
        "latest_attempt": _attempt_payload(latest),
        "worktree_exists": bool(path and path.is_dir()),
        "worktree_proven": proven,
        "worktree_proof": proof.to_dict() if proof is not None else None,
        "worktree_dirty": bool(proven and path and _implementation_dirty_paths(profile, path)),
        "branch_exists": bool(attempt and _git_exists(primary, attempt.branch)),
        "branch_ahead_of_target": bool(attempt and _ahead(primary, attempt.branch, attempt.target_branch)),
    }
    if active is not None and proven:
        next_action = _complete_action(
            "continue_active_attempt",
            "The active attempt has a valid retained task workspace.",
            "Continue in the retained task workspace",
        )
    elif active is not None:
        next_action = _close_choices(profile, task, active)
    elif attempt is not None and task.status == TASK_STATUS_BLOCKED:
        next_action = _cancel_blocked_task_action(profile, task, attempt)
    elif attempt is not None and proven:
        next_action = _command_action(
            action_id="cleanup_terminal_task",
            detail="The task is terminal and its retained workspace can be inspected or cleaned explicitly.",
            display="Clean the retained task workspace",
            argv=(
                _task_executable(profile),
                "task",
                "cleanup",
                f"--project-root={profile.paths.project_root}",
                f"--task={task.task_id}",
            ),
            safety="proof_guarded_mutation",
            mutation="git_and_filesystem",
        )
    elif attempt is not None and path is not None and path.exists():
        next_action = _blocked_action(
            "inspect_task_workspace_ownership",
            proof.reason if proof is not None and proof.reason else "task workspace ownership is unproven",
            "Preserve and inspect the retained task workspace",
            required_inputs=("worktree_registration", "branch", "head_lineage"),
        )
    else:
        next_action = _complete_action(
            "task_state_terminal",
            "The task has no active attempt requiring a protocol action.",
            "No task action is required",
        )
    return _result(
        operation=operation,
        operation_status="observed",
        task_status=task.status,
        attempt_status=attempt.status if attempt else None,
        next_action=next_action,
        payload=payload,
    )


def show_task(profile: RepoProfile, *, task_id: str | None, cwd: Path | None = None) -> OperationResult:
    return _task_state_result(
        profile,
        _task_for_command(profile, task_id=task_id, cwd=cwd),
        operation="task.show",
    )


def recover_task(
    profile: RepoProfile,
    *,
    task_id: str | None,
    status: str | None = None,
    summary: str | None = None,
    note: str | None = None,
    cwd: Path | None = None,
    **removed: Any,
) -> OperationResult:
    del removed
    task = _task_for_command(
        profile,
        task_id=task_id,
        cwd=cwd,
        require_certified_cwd=status is not None,
    )
    state = load_runtime_state(profile.paths)
    active = active_task_attempt(state, task.task_id)
    if status is None:
        return _task_state_result(profile, task, operation="task.recover")
    if status not in {ATTEMPT_STATUS_BLOCKED, ATTEMPT_STATUS_FAILED, ATTEMPT_STATUS_ABANDONED}:
        raise TaskError("recovery status must be blocked, failed, or abandoned")
    resolved_summary = str(summary).strip() if summary is not None else f"Recovered interrupted attempt as {status}"
    resolved_note = str(note).strip() if note is not None else None
    candidate = active
    if candidate is None:
        latest = latest_task_attempt(state, task.task_id)
        evidence = (
            inspect_task_finalization(
                profile,
                task_id=task.task_id,
                attempt_id=latest.attempt_id,
            )
            if latest is not None
            else None
        )
        if (
            latest is not None
            and latest.status == status
            and evidence is not None
            and evidence.request_event_id is not None
        ):
            candidate = latest
    if candidate is None:
        raise TaskError(f"Task {task.task_id!r} has no active attempt to recover as {status}")
    retry_argv = _recover_argv(
        profile,
        task_id=task.task_id,
        status=status,
        summary=resolved_summary,
        note=resolved_note,
    )
    before = _durable_task_bytes(profile)
    try:
        finished = finish_task(
            profile,
            task_id=task.task_id,
            attempt_id=candidate.attempt_id,
            actor=candidate.actor,
            status=status,
            summary=resolved_summary,
            note=resolved_note,
        )
    except Exception as exc:
        evidence = inspect_task_finalization(
            profile,
            task_id=task.task_id,
            attempt_id=candidate.attempt_id,
        )
        retained_partial = (
            isinstance(exc, TaskFinalizationError)
            or evidence.request_event_id is not None
            or _durable_task_bytes(profile) != before
        )
        if not retained_partial:
            raise
        current_task = _task_for_id(profile, task.task_id)
        current_attempt = next(
            item
            for item in current_task.attempts
            if item.attempt_id == candidate.attempt_id
        )
        return _result(
            operation="task.recover",
            operation_status="partial",
            task_status=current_task.status,
            attempt_status=current_attempt.status,
            next_action=_command_action(
                action_id="retry_task_recovery_finalization",
                detail=(
                    f"Recovery retained durable finalization evidence at {evidence.stage!r}: {exc}"
                ),
                display="Retry the exact recovery finalization",
                argv=retry_argv,
                safety="proof_guarded_mutation",
                mutation="runtime",
            ),
            payload={
                **_task_payload(current_task),
                "attempt": _attempt_payload(current_attempt),
                "finalization": evidence.to_dict(),
                "error": str(exc),
            },
            mutation_started=True,
            mutation_completed=False,
            mutation_phase="event_finalization_partial",
            failure_code=classify_lifecycle_exception(exc).failure_code,
        )
    task = _task_for_id(profile, task.task_id)
    return _result(
        operation="task.recover",
        operation_status="succeeded",
        task_status=task.status,
        attempt_status=finished.status,
        next_action=_complete_action(
            "recovery_complete",
            "The active attempt was durably finalized with an exact terminal classification.",
            "Recovery is complete",
        ),
        payload={**_task_payload(task), "attempt": _attempt_payload(finished)},
        mutation_started=True,
        mutation_completed=True,
        mutation_phase="runtime_and_event_finalized",
    )


def cancel_task(
    profile: RepoProfile,
    *,
    task_id: str,
    actor: str,
    summary: str | None = None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
    cwd: Path | None = None,
) -> OperationResult:
    task = _task_for_command(
        profile,
        task_id=task_id,
        cwd=cwd,
        require_certified_cwd=True,
    )
    active = active_task_attempt(load_runtime_state(profile.paths), task_id)
    if active is not None:
        return _result(
            operation="task.cancel",
            operation_status="blocked",
            task_status=task.status,
            attempt_status=active.status,
            next_action=_close_choices(profile, task, active),
            payload={**_task_payload(task), "attempt": _attempt_payload(active)},
            failure_code=None,
        )
    if task.status == TASK_STATUS_DONE:
        return _result(
            operation="task.cancel",
            operation_status="blocked",
            task_status=task.status,
            attempt_status=task.attempts[-1].status if task.attempts else None,
            next_action=_blocked_action(
                "terminal_task_immutable",
                "A completed task cannot be reclassified as canceled.",
                "Preserve the completed task",
            ),
            payload=_task_payload(task),
            failure_code=None,
        )
    try:
        transition_evidence = inspect_task_runtime_transition(
            profile,
            task_id=task_id,
        )
    except TaskRuntimeTransitionError as exc:
        return _transition_conflict_result(
            profile,
            operation="task.cancel",
            task_id=task_id,
            exc=exc,
        )
    matching_retry = _transition_request_exists(
        profile,
        task_id=task_id,
        actor=actor,
        status=TASK_STATUS_CANCELED,
        summary=summary,
        failure_class=failure_class,
        recovery_action=recovery_action,
        prompt_issue=prompt_issue,
        operator_issue=operator_issue,
    )
    if (
        task.status == TASK_STATUS_CANCELED
        and not matching_retry
        and not transition_evidence.unfinished
    ):
        return _result(
            operation="task.cancel",
            operation_status="succeeded",
            task_status=task.status,
            attempt_status=task.attempts[-1].status if task.attempts else None,
            next_action=_complete_action("task_canceled", "The task is already canceled.", "Task cancellation is complete"),
            payload=_task_payload(task),
        )
    prior_status = task.status
    retry_argv = _cancel_argv(
        profile,
        task_id=task_id,
        actor=actor,
        summary=summary,
        failure_class=failure_class,
        recovery_action=recovery_action,
        prompt_issue=prompt_issue,
        operator_issue=operator_issue,
    )
    before = _durable_task_bytes(profile)
    try:
        transition = set_task_runtime_status(
            profile,
            task_id=task_id,
            actor=actor,
            status=TASK_STATUS_CANCELED,
            summary=summary,
            failure_class=failure_class,
            recovery_action=recovery_action,
            prompt_issue=prompt_issue,
            operator_issue=operator_issue,
            return_transition_result=True,
        )
    except TaskRuntimeTransitionError as exc:
        if transition_evidence.unfinished:
            return _transition_conflict_result(
                profile,
                operation="task.cancel",
                task_id=task_id,
                exc=exc,
                evidence=transition_evidence.to_dict(),
            )
        raise
    except Exception as exc:
        retained_partial = _transition_request_exists(
            profile,
            task_id=task_id,
            actor=actor,
            status=TASK_STATUS_CANCELED,
            summary=summary,
            failure_class=failure_class,
            recovery_action=recovery_action,
            prompt_issue=prompt_issue,
            operator_issue=operator_issue,
        ) or _durable_task_bytes(profile) != before
        if not retained_partial:
            raise
        current = _task_for_id(profile, task_id)
        return _result(
            operation="task.cancel",
            operation_status="partial",
            task_status=current.status,
            attempt_status=current.attempts[-1].status if current.attempts else None,
            next_action=_command_action(
                action_id="retry_task_cancel_finalization",
                detail=f"Cancellation retained durable transition evidence: {exc}",
                display="Retry the exact task cancellation",
                argv=retry_argv,
                safety="proof_guarded_mutation",
                mutation="runtime",
            ),
            payload={**_task_payload(current), "error": str(exc)},
            mutation_started=True,
            mutation_completed=False,
            mutation_phase="event_finalization_partial",
            failure_code=classify_lifecycle_exception(exc).failure_code,
        )
    task = _task_for_id(profile, task_id)
    changed = transition.runtime_changed or transition.events_changed
    latest = latest_task_attempt(load_runtime_state(profile.paths), task_id)
    replay_argv = _resume_begin_argv(profile, task, latest) if latest is not None else None
    next_action = (
        _command_action(
            action_id="reopen_canceled_task",
            detail="The inactive blocked task is now canceled and has exact prompt replay evidence.",
            display="Reopen the canceled task",
            argv=(
                _task_executable(profile),
                "task",
                "reopen",
                f"--project-root={profile.paths.project_root}",
                f"--task={task_id}",
                f"--actor={actor}",
                "--summary=Reopen canceled task for exact retry",
            ),
            safety="proof_guarded_mutation",
            mutation="runtime",
        )
        if prior_status == TASK_STATUS_BLOCKED and replay_argv is not None
        else _complete_action("task_canceled", "The task is durably canceled.", "Task cancellation is complete")
    )
    return _result(
        operation="task.cancel",
        operation_status="succeeded",
        task_status=task.status,
        attempt_status=task.attempts[-1].status if task.attempts else None,
        next_action=next_action,
        payload=_task_payload(task),
        mutation_started=changed,
        mutation_completed=changed,
        mutation_phase="runtime_and_event_finalized" if changed else "none",
    )


def reopen_task(
    profile: RepoProfile,
    *,
    task_id: str,
    actor: str,
    summary: str | None = None,
    cwd: Path | None = None,
) -> OperationResult:
    task = _task_for_command(
        profile,
        task_id=task_id,
        cwd=cwd,
        require_certified_cwd=True,
    )
    if active_task_attempt(load_runtime_state(profile.paths), task_id) is not None:
        return _result(
            operation="task.reopen",
            operation_status="blocked",
            task_status=task.status,
            attempt_status=ATTEMPT_STATUS_IN_PROGRESS,
            next_action=_blocked_action(
                "active_attempt",
                "A task with an active attempt cannot be reopened.",
                "Continue or close the active attempt",
            ),
            payload=_task_payload(task),
            failure_code=None,
        )
    try:
        transition_evidence = inspect_task_runtime_transition(
            profile,
            task_id=task_id,
        )
    except TaskRuntimeTransitionError as exc:
        return _transition_conflict_result(
            profile,
            operation="task.reopen",
            task_id=task_id,
            exc=exc,
        )
    matching_retry = _transition_request_exists(
        profile,
        task_id=task_id,
        actor=actor,
        status=TASK_STATUS_PLANNED,
        summary=summary,
    )
    if task.status != TASK_STATUS_CANCELED and not (
        task.status == TASK_STATUS_PLANNED
        and (matching_retry or transition_evidence.unfinished)
    ):
        return _result(
            operation="task.reopen",
            operation_status="blocked",
            task_status=task.status,
            attempt_status=task.attempts[-1].status if task.attempts else None,
            next_action=_blocked_action(
                "task_not_canceled",
                "Only a canceled task can be reopened.",
                "Preserve the current task state",
            ),
            payload=_task_payload(task),
            failure_code=None,
        )
    retry_argv = _reopen_argv(
        profile,
        task_id=task_id,
        actor=actor,
        summary=summary,
    )
    before = _durable_task_bytes(profile)
    try:
        transition = set_task_runtime_status(
            profile,
            task_id=task_id,
            actor=actor,
            status=TASK_STATUS_PLANNED,
            summary=summary,
            return_transition_result=True,
        )
    except Exception as exc:
        if isinstance(exc, TaskRuntimeTransitionError) and transition_evidence.unfinished:
            return _transition_conflict_result(
                profile,
                operation="task.reopen",
                task_id=task_id,
                exc=exc,
                evidence=transition_evidence.to_dict(),
            )
        retained_partial = _transition_request_exists(
            profile,
            task_id=task_id,
            actor=actor,
            status=TASK_STATUS_PLANNED,
            summary=summary,
        ) or _durable_task_bytes(profile) != before
        if not retained_partial:
            raise
        current = _task_for_id(profile, task_id)
        return _result(
            operation="task.reopen",
            operation_status="partial",
            task_status=current.status,
            attempt_status=current.attempts[-1].status if current.attempts else None,
            next_action=_command_action(
                action_id="retry_task_reopen_finalization",
                detail=f"Reopen retained durable transition evidence: {exc}",
                display="Retry the exact task reopen",
                argv=retry_argv,
                safety="proof_guarded_mutation",
                mutation="runtime",
            ),
            payload={**_task_payload(current), "error": str(exc)},
            mutation_started=True,
            mutation_completed=False,
            mutation_phase="event_finalization_partial",
            failure_code=classify_lifecycle_exception(exc).failure_code,
        )
    task = _task_for_id(profile, task_id)
    latest = latest_task_attempt(load_runtime_state(profile.paths), task_id)
    argv = _resume_begin_argv(profile, task, latest) if latest is not None else None
    next_action = (
        _command_action(
            action_id="begin_reopened_task",
            detail="The task is planned and its exact prompt replay artifacts are available.",
            display="Begin the reopened task",
            argv=argv,
            safety="validated_mutation",
            mutation="git_and_runtime",
        )
        if argv is not None
        else _complete_action(
            "task_reopened",
            "The task is planned; begin it with a new explicit execution prompt.",
            "Task reopen is complete",
        )
    )
    return _result(
        operation="task.reopen",
        operation_status="succeeded",
        task_status=task.status,
        attempt_status=latest.status if latest else None,
        next_action=next_action,
        payload=_task_payload(task),
        mutation_started=transition.runtime_changed or transition.events_changed,
        mutation_completed=transition.runtime_changed or transition.events_changed,
        mutation_phase="runtime_and_event_finalized",
    )


def _canonical_commit_message(
    *,
    summary: str,
    task_id: str,
    attempt: TaskAttemptRecord,
    landing_transaction_id: str,
    target_base_commit: str,
    source_commit: str,
    source_tree: str,
    changed_paths: tuple[str, ...],
    validations: tuple[ValidationRecord, ...],
    residuals: tuple[str, ...],
    followup_candidates: tuple[str, ...],
) -> str:
    lines = [" ".join(line.split()) for line in summary.splitlines() if line.strip()]
    if not lines:
        raise TaskError("landing summary must contain a nonempty human-readable line")
    subject = lines[0]
    body = lines[1:]
    trailers = [
        f"Blackdog-Task: {task_id}",
        f"Blackdog-Attempt: {attempt.attempt_id}",
        f"Blackdog-Actor: {attempt.actor}",
        f"Blackdog-Status: {ATTEMPT_STATUS_SUCCESS}",
        "Blackdog-Commit-Format: 2",
        f"Blackdog-Landing-Transaction: {landing_transaction_id}",
        f"Blackdog-Target-Base-Commit: {target_base_commit}",
        f"Blackdog-Source-Commit: {source_commit}",
        f"Blackdog-Source-Tree: {source_tree}",
    ]
    if attempt.target_branch:
        trailers.append(f"Blackdog-Target-Branch: {attempt.target_branch}")
    if attempt.execution_model:
        trailers.append(f"Blackdog-Execution-Model: {attempt.execution_model}")
    if attempt.model:
        trailers.append(f"Blackdog-Model: {attempt.model}")
    if attempt.reasoning_effort:
        trailers.append(f"Blackdog-Reasoning-Effort: {attempt.reasoning_effort}")
    if attempt.prompt_receipt is not None:
        trailers.append(f"Blackdog-Execution-Prompt-SHA256: {attempt.prompt_receipt.prompt_hash}")
        if attempt.prompt_receipt.source:
            trailers.append(f"Blackdog-Execution-Prompt-Source: {attempt.prompt_receipt.source}")
        if attempt.prompt_receipt.mode:
            trailers.append(f"Blackdog-Execution-Prompt-Mode: {attempt.prompt_receipt.mode}")
    if attempt.user_prompt_receipt is not None:
        trailers.append(f"Blackdog-Request-Prompt-SHA256: {attempt.user_prompt_receipt.prompt_hash}")
        if attempt.user_prompt_receipt.source:
            trailers.append(f"Blackdog-Request-Prompt-Source: {attempt.user_prompt_receipt.source}")
        if attempt.user_prompt_receipt.mode:
            trailers.append(f"Blackdog-Request-Prompt-Mode: {attempt.user_prompt_receipt.mode}")
    trailers.extend(f"Blackdog-Changed-Path: {path}" for path in changed_paths)
    trailers.extend(f"Blackdog-Validation: {item.name}={item.status}" for item in validations)
    trailers.extend(f"Blackdog-Residual: {item}" for item in residuals)
    trailers.extend(f"Blackdog-Followup: {item}" for item in followup_candidates)
    sections = [subject]
    if body:
        sections.append("\n".join(body))
    sections.append("\n".join(trailers))
    return "\n\n".join(sections).rstrip() + "\n"


def _landing_prep_commit_message(task: TaskRecord, attempt: TaskAttemptRecord) -> str:
    return (
        f"blackdog-wip({task.task_id}): prepare land\n\n"
        f"Blackdog-Task: {task.task_id}\n"
        f"Blackdog-Attempt: {attempt.attempt_id}\n"
        "Blackdog-Status: staged-for-land\n"
    )


def _changed_paths(repo_root: Path, base: str, head: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            line.strip()
            for line in _run_git(repo_root, "diff", "--name-only", f"{base}..{head}").splitlines()
            if line.strip()
        )
    )


def _source_fingerprint(worktree: Path, head: str, tree: str) -> str:
    material = "\0".join((str(worktree.resolve()), head, tree, "\n".join(_status_entries(worktree))))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _prepare_source(
    profile: RepoProfile,
    task: TaskRecord,
    attempt: TaskAttemptRecord,
    *,
    summary: str,
) -> tuple[str, str, tuple[str, ...]]:
    if attempt.worktree_path is None or attempt.branch is None or attempt.target_branch is None:
        raise MissingTaskWorktreeError(attempt.worktree_path)
    source = Path(attempt.worktree_path).resolve()
    if not source.is_dir() or _repo_root(source) != source:
        raise MissingTaskWorktreeError(source)
    primary = find_primary_worktree(profile.paths.project_root)
    if source == primary:
        raise WorktreeError("task source workspace must not be the primary worktree")
    if _find_worktree_for_branch(primary, attempt.branch) != source:
        raise WorktreeError("task source branch is not registered to the durable task workspace")
    if _current_branch(source) != attempt.branch:
        raise WorktreeError("task source workspace is detached or switched to a different branch")
    target_head = _run_git(primary, "rev-parse", f"{attempt.target_branch}^{{commit}}")
    branch_head = _run_git(primary, "rev-parse", f"{attempt.branch}^{{commit}}")
    stale_validation_required = bool(
        profile.landing.automatic_stale_rebase
        and attempt.start_commit
        and attempt.start_commit != target_head
    )
    if _run_git(source, "rev-parse", "HEAD") != branch_head:
        raise WorktreeError("task source workspace HEAD does not match its durable branch")
    if _run_git_no_check(primary, "merge-base", "--is-ancestor", target_head, branch_head).returncode != 0:
        if not profile.landing.automatic_stale_rebase:
            raise StaleTaskBranchError(
                branch=attempt.branch,
                target_branch=attempt.target_branch,
                branch_worktree=source,
            )
        _run_git(source, "rebase", "--autostash", attempt.target_branch)
    if stale_validation_required:
        validation_run = run_validation_commands(
            profile.validation_commands,
            cwd=source,
            timeout_seconds=profile.landing.validation_timeout_seconds,
        )
        if not validation_run.all_passed:
            raise WorktreeError("automatic stale rebase validation failed; retained task workspace remains authoritative")
    if _implementation_dirty_paths(profile, source):
        _run_git(source, "add", "-A")
        provisional = _landing_prep_commit_message(task, attempt)
        _run_git(source, "commit", "--file=-", input_text=provisional)
    branch_head = _run_git(primary, "rev-parse", f"{attempt.branch}^{{commit}}")
    target_head = _run_git(primary, "rev-parse", f"{attempt.target_branch}^{{commit}}")
    changed = _changed_paths(primary, target_head, branch_head)
    if not changed:
        raise NoChangesToLandError(branch=attempt.branch, target_branch=attempt.target_branch)
    return branch_head, target_head, changed


def _landing_intent(
    profile: RepoProfile,
    task: TaskRecord,
    attempt: TaskAttemptRecord,
    *,
    actor: str,
    summary: str,
    validations: tuple[ValidationRecord, ...],
    residuals: tuple[str, ...],
    followup_candidates: tuple[str, ...],
    note: str | None,
    cleanup: bool,
) -> LandingIntent:
    source_head, target_head, changed = _prepare_source(profile, task, attempt, summary=summary)
    primary = find_primary_worktree(profile.paths.project_root)
    source = Path(attempt.worktree_path or "").resolve()
    source_tree = _run_git(primary, "rev-parse", f"{source_head}^{{tree}}")
    transaction_seed = hashlib.sha256(f"{task.task_id}\0{attempt.attempt_id}".encode()).hexdigest()[:16]
    temporary = profile.paths.worktrees_dir / f"wt-land-{transaction_seed}"
    transaction_id = landing_transaction_id(task_id=task.task_id, attempt_id=attempt.attempt_id)
    return LandingIntent(
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        actor=actor,
        branch=attempt.branch or "",
        target_branch=attempt.target_branch or "",
        worktree_path=str(source),
        primary_worktree=str(primary),
        target_base_commit=target_head,
        source_head_commit=source_head,
        source_fingerprint=_source_fingerprint(source, source_head, source_tree),
        expected_source_tree_hash=source_tree,
        source_dirty=False,
        summary=summary,
        note=note,
        validations=tuple((item.name, item.status) for item in validations),
        residuals=residuals,
        followup_candidates=followup_candidates,
        changed_paths=changed,
        cleanup=cleanup,
        commit_message=_canonical_commit_message(
            summary=summary,
            task_id=task.task_id,
            attempt=attempt,
            landing_transaction_id=transaction_id,
            target_base_commit=target_head,
            source_commit=source_head,
            source_tree=source_tree,
            changed_paths=changed,
            validations=validations,
            residuals=residuals,
            followup_candidates=followup_candidates,
        ),
        temporary_worktree_path=str(temporary.resolve()),
    )


def _landing_resume_action(profile: RepoProfile, intent: LandingIntent, *, phase: str) -> NextAction:
    return _command_action(
        action_id="resume_landing_transaction",
        detail=f"Landing reached durable phase {phase!r}; rerun the exact frozen request to continue idempotently.",
        display="Resume the landing transaction",
        argv=intent.task_land_argv(executable=_task_executable(profile), project_root=profile.paths.project_root),
        safety="requires_validation",
        mutation="git_and_runtime",
    )


def _landing_abort_resume_action(
    profile: RepoProfile,
    intent: LandingIntent,
    *,
    phase: str,
) -> NextAction:
    return _command_action(
        action_id="resume_landing_abort",
        detail=(
            f"Landing abort reached durable phase {phase!r}; rerun the exact frozen "
            "request to finish abort cleanup and terminal evidence only."
        ),
        display="Resume landing-abort finalization",
        argv=intent.task_land_argv(
            executable=_task_executable(profile),
            project_root=profile.paths.project_root,
        ),
        safety="proof_guarded_mutation",
        mutation="git_and_runtime",
    )


def _record_phase(profile: RepoProfile, intent: LandingIntent, phase: str, data: Mapping[str, Any]) -> None:
    transaction = load_landing_transaction(profile, task_id=intent.task_id, attempt_id=intent.attempt_id)
    if transaction is not None and phase in transaction.phases:
        recorded = transaction.data_for(phase)
        expected = intent.to_dict() if phase == "intent_recorded" else dict(data)
        if recorded != expected:
            raise LandingTransactionError(f"landing phase {phase!r} conflicts with durable evidence")
        return
    record_landing_phase(profile, intent=intent, phase=phase, data=data)


def _require_phase_data(transaction: LandingTransaction, phase: str, expected: Mapping[str, Any]) -> None:
    if not strict_json_equal(transaction.data_for(phase), dict(expected)):
        raise LandingTransactionError(f"landing phase {phase!r} conflicts with rederived durable evidence")


def _validate_landing_transaction(
    profile: RepoProfile,
    *,
    task: TaskRecord,
    attempt: TaskAttemptRecord,
    transaction: LandingTransaction,
) -> None:
    intent = transaction.intent
    if intent.task_id != task.task_id or intent.attempt_id != attempt.attempt_id:
        raise LandingTransactionError("landing transaction identity conflicts with task runtime")
    if intent.actor != attempt.actor or intent.branch != attempt.branch or intent.target_branch != attempt.target_branch:
        raise LandingTransactionError("landing transaction ownership conflicts with task runtime")
    primary = Path(intent.primary_worktree).resolve()
    if primary != find_primary_worktree(profile.paths.project_root):
        raise LandingTransactionError("landing transaction primary worktree conflicts with repository identity")
    _require_phase_data(transaction, "intent_recorded", intent.to_dict())
    if "source_prepared" in transaction.phases:
        if _run_git(primary, "rev-parse", f"{intent.source_head_commit}^{{tree}}") != intent.expected_source_tree_hash:
            raise LandingTransactionError("frozen landing source tree is no longer available")
        if _changed_paths(primary, intent.target_base_commit, intent.source_head_commit) != intent.changed_paths:
            raise LandingTransactionError("frozen landing source changed paths conflict with Git")
        _require_phase_data(
            transaction,
            "source_prepared",
            {
                "source_head_commit": intent.source_head_commit,
                "source_tree_hash": intent.expected_source_tree_hash,
                "changed_paths": list(intent.changed_paths),
            },
        )
        source_path = Path(intent.worktree_path)
        if source_path.exists():
            if (
                _find_worktree_for_branch(primary, intent.branch) != source_path.resolve()
                or _current_branch(source_path) != intent.branch
                or _run_git(source_path, "rev-parse", "HEAD") != intent.source_head_commit
                or _source_fingerprint(source_path, intent.source_head_commit, intent.expected_source_tree_hash)
                != intent.source_fingerprint
            ):
                raise LandingTransactionError("retained landing source workspace conflicts with frozen source evidence")
    canonical_commit: str | None = None
    if "canonical_commit_created" in transaction.phases:
        data = transaction.data_for("canonical_commit_created")
        canonical_commit = str(data.get("canonical_commit") or "")
        _verify_canonical_commit(primary, task=task, attempt=attempt, intent=intent, commit=canonical_commit)
        _require_phase_data(
            transaction,
            "canonical_commit_created",
            {
                "canonical_commit": canonical_commit,
                "canonical_tree": _run_git(primary, "rev-parse", f"{canonical_commit}^{{tree}}"),
            },
        )
    if "target_updated" in transaction.phases:
        if canonical_commit is None:
            raise LandingTransactionError("target update lacks a canonical commit phase")
        _require_phase_data(
            transaction,
            "target_updated",
            {"previous_target_commit": intent.target_base_commit, "landed_commit": canonical_commit},
        )
        target_head = _run_git(primary, "rev-parse", f"{intent.target_branch}^{{commit}}")
        if not _is_ancestor(primary, canonical_commit, target_head):
            raise LandingTransactionError(
                "recorded landing commit is no longer in target-branch ancestry"
            )
    temporary = Path(intent.temporary_worktree_path)
    canonical_branch = f"blackdog/land-{intent.transaction_id[:16]}"
    if "temporary_cleanup_complete" in transaction.phases:
        _require_phase_data(
            transaction,
            "temporary_cleanup_complete",
            {"temporary_worktree_removed": True, "temporary_branch_removed": True},
        )
        if temporary.exists() or _git_exists(primary, canonical_branch):
            raise LandingTransactionError("temporary landing state exists after recorded cleanup")
    if "runtime_finalized" in transaction.phases:
        if canonical_commit is None:
            raise LandingTransactionError("runtime finalization lacks canonical commit evidence")
        current_task = _task_for_id(profile, task.task_id)
        current_attempt = next(
            (item for item in current_task.attempts if item.attempt_id == attempt.attempt_id),
            None,
        )
        if (
            current_attempt is None
            or current_attempt.status != ATTEMPT_STATUS_SUCCESS
            or current_attempt.landed_commit != canonical_commit
            or current_attempt.commit != intent.source_head_commit
        ):
            raise LandingTransactionError("recorded runtime finalization conflicts with task state")
        if not inspect_task_finalization(
            profile,
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
        ).complete:
            raise LandingTransactionError("recorded runtime finalization lacks complete request, decision, and finish evidence")
        _require_phase_data(
            transaction,
            "runtime_finalized",
            {"attempt_status": ATTEMPT_STATUS_SUCCESS, "landed_commit": canonical_commit},
        )
    if "land_event_recorded" in transaction.phases:
        if canonical_commit is None:
            raise LandingTransactionError("land event phase lacks canonical commit evidence")
        payload = {
            "task_id": task.task_id,
            "attempt_id": attempt.attempt_id,
            "branch": intent.branch,
            "target_branch": intent.target_branch,
            "source_commit": intent.source_head_commit,
            "landed_commit": canonical_commit,
            "changed_paths": list(intent.changed_paths),
        }
        _require_phase_data(transaction, "land_event_recorded", payload)
        if not exact_worktree_land_event(profile, intent=intent, payload=payload):
            raise LandingTransactionError("recorded land event phase lacks exact event evidence")
    if "task_cleanup_complete" in transaction.phases:
        data = transaction.data_for("task_cleanup_complete")
        if intent.cleanup:
            expected = {
                "task_id": task.task_id,
                "attempt_id": attempt.attempt_id,
                "worktree_path": intent.worktree_path,
                "worktree_removed": True,
                "branch": intent.branch,
                "branch_deleted": True,
                "retained": False,
                "retained_reason": None,
            }
            _require_phase_data(transaction, "task_cleanup_complete", expected)
            if Path(intent.worktree_path).exists() or _git_exists(primary, intent.branch):
                raise LandingTransactionError("recorded task cleanup conflicts with retained Git state")
        else:
            _require_phase_data(
                transaction,
                "task_cleanup_complete",
                {"requested": False, "worktree_removed": False, "branch_deleted": False, "retained": True},
            )
    if "complete" in transaction.phases:
        if canonical_commit is None:
            raise LandingTransactionError("complete landing transaction lacks canonical commit evidence")
        _require_phase_data(transaction, "complete", {"landed_commit": canonical_commit})


def _run_landing_transaction(
    profile: RepoProfile,
    task: TaskRecord,
    attempt: TaskAttemptRecord,
    intent: LandingIntent,
) -> tuple[LandingTransaction, str]:
    primary = Path(intent.primary_worktree)
    source = Path(intent.worktree_path)
    temporary = Path(intent.temporary_worktree_path)
    canonical_branch = f"blackdog/land-{intent.transaction_id[:16]}"
    _record_phase(profile, intent, "intent_recorded", intent.to_dict())
    transaction = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if transaction is None:
        raise LandingTransactionError("landing intent disappeared")
    _validate_landing_transaction(profile, task=task, attempt=attempt, transaction=transaction)
    _record_phase(
        profile,
        intent,
        "source_prepared",
        {
            "source_head_commit": intent.source_head_commit,
            "source_tree_hash": intent.expected_source_tree_hash,
            "changed_paths": list(intent.changed_paths),
        },
    )

    transaction = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if transaction is None:
        raise LandingTransactionError("landing intent disappeared")
    _validate_landing_transaction(profile, task=task, attempt=attempt, transaction=transaction)
    if "canonical_commit_created" in transaction.phases:
        canonical_commit = str(transaction.data_for("canonical_commit_created").get("canonical_commit") or "")
        _verify_canonical_commit(primary, task=task, attempt=attempt, intent=intent, commit=canonical_commit)
    else:
        if temporary.exists() and _find_worktree_for_branch(primary, canonical_branch) != temporary:
            raise LandingTransactionError("temporary landing path is occupied by an unrelated workspace")
        if not temporary.exists() and _git_exists(primary, canonical_branch):
            raise LandingTransactionError("temporary landing branch exists without its owned worktree")
        if not temporary.exists():
            temporary.parent.mkdir(parents=True, exist_ok=True)
            _run_git(primary, "worktree", "add", "-b", canonical_branch, str(temporary), intent.target_base_commit)
        temporary_head = _run_git(temporary, "rev-parse", "HEAD")
        if temporary_head != intent.target_base_commit:
            canonical_commit = temporary_head
            _verify_canonical_commit(primary, task=task, attempt=attempt, intent=intent, commit=canonical_commit)
        else:
            if _status_entries(temporary):
                _run_git(primary, "worktree", "remove", "--force", str(temporary))
                _run_git(primary, "branch", "-D", canonical_branch)
                _run_git(primary, "worktree", "add", "-b", canonical_branch, str(temporary), intent.target_base_commit)
            _run_git(temporary, "merge", "--squash", intent.source_head_commit)
            _run_git(temporary, "commit", "--file=-", input_text=intent.commit_message)
            canonical_commit = _run_git(temporary, "rev-parse", "HEAD")
            _verify_canonical_commit(primary, task=task, attempt=attempt, intent=intent, commit=canonical_commit)
        _record_phase(
            profile,
            intent,
            "canonical_commit_created",
            {"canonical_commit": canonical_commit, "canonical_tree": _run_git(temporary, "rev-parse", "HEAD^{tree}")},
        )

    transaction = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if transaction is None:
        raise LandingTransactionError("landing transaction disappeared")
    _validate_landing_transaction(profile, task=task, attempt=attempt, transaction=transaction)
    if "target_updated" not in transaction.phases:
        target_path = _find_worktree_for_branch(primary, intent.target_branch)
        if target_path is None:
            raise WorktreeError(f"target branch {intent.target_branch!r} has no checked-out worktree")
        if _implementation_dirty_paths(profile, target_path):
            if target_path == primary:
                raise DirtyPrimaryWorktreeError(
                    primary_worktree=primary,
                    branch=intent.branch,
                    target_branch=intent.target_branch,
                    dirty_paths=_implementation_dirty_paths(profile, target_path),
                )
            raise DirtyTargetWorktreeError(target_path)
        observed_target = _run_git(primary, "rev-parse", f"{intent.target_branch}^{{commit}}")
        if observed_target == intent.target_base_commit:
            _run_git(target_path, "merge", "--ff-only", canonical_commit)
        elif observed_target != canonical_commit and not _is_ancestor(
            primary,
            canonical_commit,
            observed_target,
        ):
            raise StaleTaskBranchError(
                branch=intent.branch,
                target_branch=intent.target_branch,
                branch_worktree=source,
            )
        _record_phase(
            profile,
            intent,
            "target_updated",
            {"previous_target_commit": intent.target_base_commit, "landed_commit": canonical_commit},
        )
        transaction = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
        if transaction is None:
            raise LandingTransactionError("landing target update evidence disappeared")
        _validate_landing_transaction(profile, task=task, attempt=attempt, transaction=transaction)

    if "temporary_cleanup_complete" not in (load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id) or transaction).phases:
        if temporary.exists():
            _run_git(primary, "worktree", "remove", str(temporary))
        if _git_exists(primary, canonical_branch):
            _run_git(primary, "branch", "-d", canonical_branch)
        _record_phase(
            profile,
            intent,
            "temporary_cleanup_complete",
            {"temporary_worktree_removed": not temporary.exists(), "temporary_branch_removed": not _git_exists(primary, canonical_branch)},
        )
        transaction = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
        if transaction is None:
            raise LandingTransactionError("landing temporary cleanup evidence disappeared")
        _validate_landing_transaction(profile, task=task, attempt=attempt, transaction=transaction)

    current = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if current is None:
        raise LandingTransactionError("landing transaction disappeared")
    if "runtime_finalized" not in current.phases:
        finished = finish_task(
            profile,
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            actor=intent.actor,
            status=ATTEMPT_STATUS_SUCCESS,
            summary=intent.summary,
            changed_paths=intent.changed_paths,
            validations=tuple(ValidationRecord(name, status) for name, status in intent.validations),
            residuals=intent.residuals,
            followup_candidates=intent.followup_candidates,
            commit=intent.source_head_commit,
            landed_commit=canonical_commit,
            note=intent.note,
            finalization_id=f"landing-{intent.transaction_id}",
        )
        _record_phase(
            profile,
            intent,
            "runtime_finalized",
            {"attempt_status": finished.status, "landed_commit": canonical_commit},
        )
        current = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
        if current is None:
            raise LandingTransactionError("landing runtime evidence disappeared")
        _validate_landing_transaction(profile, task=task, attempt=attempt, transaction=current)

    event_payload = {
        "task_id": task.task_id,
        "attempt_id": attempt.attempt_id,
        "branch": intent.branch,
        "target_branch": intent.target_branch,
        "source_commit": intent.source_head_commit,
        "landed_commit": canonical_commit,
        "changed_paths": list(intent.changed_paths),
    }
    append_worktree_land_once(profile, intent=intent, payload=event_payload)
    _record_phase(profile, intent, "land_event_recorded", event_payload)
    current = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if current is None:
        raise LandingTransactionError("landing event evidence disappeared")
    _validate_landing_transaction(profile, task=task, attempt=attempt, transaction=current)

    current = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if current is None:
        raise LandingTransactionError("landing transaction disappeared")
    if "task_cleanup_complete" not in current.phases:
        cleanup_payload = (
            _cleanup_workspace(profile, task=task, attempt=attempt, explicit_path=None, explicit_branch=None)
            if intent.cleanup
            else {"requested": False, "worktree_removed": False, "branch_deleted": False, "retained": True}
        )
        _record_phase(profile, intent, "task_cleanup_complete", cleanup_payload)
    _record_phase(profile, intent, "complete", {"landed_commit": canonical_commit})
    final = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if final is None or not final.complete:
        raise LandingTransactionError("landing transaction did not reach complete")
    _validate_landing_transaction(profile, task=task, attempt=attempt, transaction=final)
    return final, canonical_commit


def _first_incomplete_landing(profile: RepoProfile, task: TaskRecord) -> tuple[TaskAttemptRecord, LandingTransaction] | None:
    for attempt in reversed(task.attempts):
        transaction = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
        if transaction is not None and not transaction.terminal:
            return attempt, transaction
    return None


def _abort_pre_target_landing(
    profile: RepoProfile,
    *,
    task: TaskRecord,
    attempt: TaskAttemptRecord,
    transaction: LandingTransaction,
    exc: Exception | None,
) -> LandingTransaction:
    """Make a pre-CAS landing failure terminal while retaining the task source."""
    intent = transaction.intent
    if not transaction.abort_requested:
        if exc is None:
            raise LandingTransactionError("new landing abort requires its triggering failure")
        mapping = classify_lifecycle_exception(exc)
        terminal_summary = f"Landing blocked before target update: {exc}"
        record_landing_abort(
            profile,
            intent=intent,
            data={
                "reason": str(exc),
                "last_phase": transaction.last_phase,
                "attempt_summary": terminal_summary,
                "failure_code": mapping.failure_code,
                "recovery_action": mapping.recovery_action,
                "operator_issue": mapping.operator_issue,
            },
        )
    temporary = Path(intent.temporary_worktree_path)
    primary = Path(intent.primary_worktree)
    canonical_branch = f"blackdog/land-{intent.transaction_id[:16]}"
    temporary_resolved = temporary.resolve()

    def temporary_registered() -> bool:
        return any(
            Path(row.get("worktree") or "").resolve() == temporary_resolved
            for row in _parse_worktree_list(primary)
            if row.get("worktree")
        )

    if temporary.exists() or temporary_registered():
        _run_git_no_check(primary, "worktree", "remove", "--force", str(temporary))
    if _git_exists(primary, canonical_branch):
        _run_git_no_check(primary, "branch", "-D", canonical_branch)
    path_absent = not temporary.exists()
    registration_absent = not temporary_registered()
    branch_absent = not _git_exists(primary, canonical_branch)
    if not (path_absent and registration_absent and branch_absent):
        retained = []
        if not path_absent:
            retained.append("temporary path")
        if not registration_absent:
            retained.append("temporary worktree registration")
        if not branch_absent:
            retained.append("temporary branch")
        raise LandingTransactionError(
            "landing abort cleanup retained owned state: " + ", ".join(retained)
        )
    current = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if current is None:
        raise LandingTransactionError("landing abort lost its durable intent")
    if not current.abort_cleanup_complete:
        record_landing_abort_cleanup(
            profile,
            intent=intent,
            data={
                "temporary_worktree_removed": path_absent,
                "temporary_worktree_registration_removed": registration_absent,
                "temporary_branch_removed": branch_absent,
                "task_workspace_retained": True,
            },
        )
    abort_data = current.abort_data or {}
    reason = str(abort_data.get("reason") or exc or "pre-target landing failure")
    fallback = classify_lifecycle_exception(
        exc if exc is not None else LandingTransactionError(reason)
    )
    failure_code = str(abort_data.get("failure_code") or fallback.failure_code)
    recovery_action = str(abort_data.get("recovery_action") or fallback.recovery_action)
    operator_issue = bool(abort_data.get("operator_issue", fallback.operator_issue))
    terminal_summary = str(
        abort_data.get("attempt_summary") or f"Landing blocked before target update: {reason}"
    )
    finish_task(
        profile,
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        actor=intent.actor,
        status=ATTEMPT_STATUS_BLOCKED,
        summary=terminal_summary,
        failure_class=failure_code,
        recovery_action=recovery_action,
        operator_issue=operator_issue,
        finalization_id=f"landing-abort-{intent.transaction_id}",
    )
    current = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if current is None:
        raise LandingTransactionError("landing abort disappeared")
    if not current.abort_runtime_finalized:
        record_landing_abort_runtime(
            profile,
            intent=intent,
            data={"attempt_status": ATTEMPT_STATUS_BLOCKED, "task_status": TASK_STATUS_BLOCKED},
        )
    current = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if current is None:
        raise LandingTransactionError("landing abort disappeared")
    if not current.abort_close_event_recorded:
        record_landing_abort_close_event(
            profile,
            intent=intent,
            data={"task_workspace_retained": True, "source_head_commit": intent.source_head_commit},
        )
    current = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if current is None:
        raise LandingTransactionError("landing abort disappeared")
    if not current.abort_complete:
        record_landing_abort_complete(
            profile,
            intent=intent,
            data={"outcome": "blocked_before_target_update", "task_workspace_retained": True},
        )
    final = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if final is None or not final.abort_complete:
        raise LandingTransactionError("landing abort did not become terminal")
    return final


def _finish_landing_abort_result(
    profile: RepoProfile,
    *,
    task: TaskRecord,
    attempt: TaskAttemptRecord,
    transaction: LandingTransaction,
    exc: Exception | None,
) -> OperationResult:
    intent = transaction.intent
    try:
        with attempt_lifecycle_lock(
            profile,
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
        ):
            aborted = _abort_pre_target_landing(
                profile,
                task=task,
                attempt=attempt,
                transaction=transaction,
                exc=exc,
            )
    except Exception as abort_exc:
        observed = load_landing_transaction(
            profile,
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
        )
        if observed is None or not observed.abort_requested:
            raise
        current_task = _task_for_id(profile, task.task_id)
        current_attempt = latest_task_attempt(
            load_runtime_state(profile.paths),
            task.task_id,
        ) or attempt
        return _result(
            operation="task.land",
            operation_status="partial",
            task_status=current_task.status,
            attempt_status=current_attempt.status,
            next_action=_landing_abort_resume_action(
                profile,
                intent,
                phase=observed.last_phase,
            ),
            payload={
                **_task_payload(current_task),
                "attempt_id": attempt.attempt_id,
                "landing_transaction": observed.to_dict(),
                "error": str(abort_exc),
            },
            mutation_started=True,
            mutation_completed=False,
            mutation_phase=f"landing_{observed.last_phase}",
            failure_code=classify_lifecycle_exception(abort_exc).failure_code,
        )
    updated_task = _task_for_id(profile, task.task_id)
    updated_attempt = latest_task_attempt(
        load_runtime_state(profile.paths),
        task.task_id,
    ) or attempt
    failure_code = str(
        (aborted.abort_data or {}).get("failure_code")
        or classify_lifecycle_exception(exc or LandingTransactionError("landing aborted")).failure_code
    )
    return _result(
        operation="task.land",
        operation_status="blocked",
        task_status=TASK_STATUS_BLOCKED,
        attempt_status=ATTEMPT_STATUS_BLOCKED,
        next_action=_cancel_blocked_task_action(profile, updated_task, updated_attempt),
        payload={
            **_task_payload(updated_task),
            "attempt_id": attempt.attempt_id,
            "landing_transaction": aborted.to_dict(),
            "error": str((aborted.abort_data or {}).get("reason") or exc or "landing aborted"),
        },
        mutation_started=True,
        mutation_completed=True,
        mutation_phase="landing_abort_complete",
        failure_code=failure_code,
    )


def land_task(
    profile: RepoProfile,
    *,
    task_id: str | None,
    actor: str | None,
    summary: str | None,
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
    cleanup: bool = True,
    cwd: Path | None = None,
) -> OperationResult:
    task = _task_for_command(
        profile,
        task_id=task_id,
        cwd=cwd,
        require_certified_cwd=True,
    )
    # Default landing removes the task workspace.  From this point onward all
    # validation, result construction, and exact retry commands must use the
    # canonical primary checkout, which survives task cleanup.  Keep the
    # already-loaded policy so an unlanded profile edit cannot change the
    # transaction that is about to freeze that source.
    profile = _profile_rooted_at_primary_worktree(profile)
    state = load_runtime_state(profile.paths)
    active = active_task_attempt(state, task.task_id)
    incomplete = _first_incomplete_landing(profile, task)
    transaction = incomplete[1] if incomplete is not None else None
    attempt = active or (incomplete[0] if incomplete is not None else None)
    if attempt is None:
        latest = latest_task_attempt(state, task.task_id)
        terminal_transaction = (
            load_landing_transaction(
                profile,
                task_id=task.task_id,
                attempt_id=latest.attempt_id,
            )
            if latest is not None
            else None
        )
        if terminal_transaction is not None and terminal_transaction.abort_requested:
            attempt = latest
            transaction = terminal_transaction
        elif latest is not None and latest.status == ATTEMPT_STATUS_SUCCESS and latest.landed_commit:
            return _result(
                operation="task.land",
                operation_status="succeeded",
                task_status=TASK_STATUS_DONE,
                attempt_status=ATTEMPT_STATUS_SUCCESS,
                next_action=_complete_action("landing_complete", "The task is already durably landed.", "Landing is complete"),
                payload={**_task_payload(task), "attempt": _attempt_payload(latest), "landed_commit": latest.landed_commit},
            )
        else:
            raise TaskError(f"Task {task.task_id!r} has no active or recoverable landing attempt")
    assert attempt is not None
    resolved_actor = actor or attempt.actor
    if resolved_actor != attempt.actor:
        raise TaskError(f"attempt {attempt.attempt_id!r} is owned by {attempt.actor!r}, not {resolved_actor!r}")
    if transaction is None and not str(summary or "").strip():
        raise TaskError("task land requires an explicit nonblank --summary")
    if transaction is None and not validations:
        raise TaskError("task land requires at least one --validation NAME=passed|failed|skipped record")
    resolved_summary = str(summary).strip() if transaction is None else (summary or transaction.intent.summary)
    if transaction is None:
        intent = _landing_intent(
            profile,
            task,
            attempt,
            actor=resolved_actor,
            summary=resolved_summary,
            validations=validations,
            residuals=residuals,
            followup_candidates=followup_candidates,
            note=note,
            cleanup=cleanup,
        )
    else:
        intent = transaction.intent
        supplied = {
            "actor": resolved_actor,
            "summary": resolved_summary,
            "note": note,
            "validations": tuple((item.name, item.status) for item in validations) if validations else intent.validations,
            "residuals": residuals if residuals else intent.residuals,
            "followup_candidates": followup_candidates if followup_candidates else intent.followup_candidates,
            "cleanup": cleanup,
        }
        if supplied != intent.request_identity():
            raise LandingTransactionError("landing retry conflicts with the immutable request; execute next_action.argv exactly")
    if transaction is not None and transaction.abort_requested:
        return _finish_landing_abort_result(
            profile,
            task=task,
            attempt=attempt,
            transaction=transaction,
            exc=None,
        )
    try:
        with attempt_lifecycle_lock(profile, task_id=task.task_id, attempt_id=attempt.attempt_id):
            final, landed_commit = _run_landing_transaction(profile, task, attempt, intent)
    except Exception as exc:
        observed = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
        recoverable_transaction_fault = isinstance(
            exc,
            (
                OSError,
                TaskFinalizationError,
                CleanupEventFinalizationError,
                CleanupPostMutationError,
            ),
        )
        if observed is not None and observed.abort_requested:
            return _finish_landing_abort_result(
                profile,
                task=task,
                attempt=attempt,
                transaction=observed,
                exc=exc,
            )
        if observed is not None and (observed.target_updated or recoverable_transaction_fault):
            next_action = _landing_resume_action(profile, intent, phase=observed.last_phase)
            return _result(
                operation="task.land",
                operation_status="partial",
                task_status=_task_for_id(profile, task.task_id).status,
                attempt_status=(latest_task_attempt(load_runtime_state(profile.paths), task.task_id) or attempt).status,
                next_action=next_action,
                payload={
                    **_task_payload(_task_for_id(profile, task.task_id)),
                    "attempt_id": attempt.attempt_id,
                    "landing_transaction": observed.to_dict(),
                    "error": str(exc),
                },
                mutation_started=True,
                mutation_completed=False,
                mutation_phase=f"landing_{observed.last_phase}",
                failure_code=classify_lifecycle_exception(exc).failure_code,
            )
        if observed is not None:
            return _finish_landing_abort_result(
                profile,
                task=task,
                attempt=attempt,
                transaction=observed,
                exc=exc,
            )
        raise
    task = _task_for_id(profile, task.task_id)
    latest = latest_task_attempt(load_runtime_state(profile.paths), task.task_id)
    return _result(
        operation="task.land",
        operation_status="succeeded",
        task_status=task.status,
        attempt_status=latest.status if latest else ATTEMPT_STATUS_SUCCESS,
        next_action=_complete_action("landing_complete", "Git, runtime, event, and cleanup phases are complete.", "Landing is complete"),
        payload={
            **_task_payload(task),
            "attempt_id": attempt.attempt_id,
            "branch": intent.branch,
            "target_branch": intent.target_branch,
            "source_commit": intent.source_head_commit,
            "landed_commit": landed_commit,
            "changed_paths": list(intent.changed_paths),
            "landing_transaction": final.to_dict(),
        },
        mutation_started=True,
        mutation_completed=True,
        mutation_phase="landing_complete",
    )


def _commit_trailers(repo_root: Path, commit: str) -> dict[str, tuple[str, ...]]:
    message = _run_git(repo_root, "show", "-s", "--format=%B", commit)
    parsed = _run_git(repo_root, "interpret-trailers", "--parse", input_text=message)
    trailers: dict[str, list[str]] = {}
    for line in parsed.splitlines():
        key, separator, value = line.partition(":")
        normalized_key = key.strip()
        if separator and normalized_key.startswith("Blackdog-") and value.strip():
            trailers.setdefault(normalized_key, []).append(value.strip())
    return {key: tuple(values) for key, values in trailers.items()}


def _expected_commit_trailers(
    *,
    task_id: str,
    attempt: TaskAttemptRecord,
    intent: LandingIntent,
) -> dict[str, tuple[str, ...]]:
    expected: dict[str, tuple[str, ...]] = {
        "Blackdog-Task": (task_id,),
        "Blackdog-Attempt": (attempt.attempt_id,),
        "Blackdog-Actor": (attempt.actor,),
        "Blackdog-Status": (ATTEMPT_STATUS_SUCCESS,),
        "Blackdog-Commit-Format": ("2",),
        "Blackdog-Landing-Transaction": (intent.transaction_id,),
        "Blackdog-Target-Base-Commit": (intent.target_base_commit,),
        "Blackdog-Source-Commit": (intent.source_head_commit,),
        "Blackdog-Source-Tree": (intent.expected_source_tree_hash,),
        "Blackdog-Target-Branch": (intent.target_branch,),
        "Blackdog-Changed-Path": intent.changed_paths,
        "Blackdog-Validation": tuple(f"{name}={status}" for name, status in intent.validations),
    }
    optional = {
        "Blackdog-Execution-Model": attempt.execution_model,
        "Blackdog-Model": attempt.model,
        "Blackdog-Reasoning-Effort": attempt.reasoning_effort,
    }
    expected.update({key: (value,) for key, value in optional.items() if value})
    if attempt.prompt_receipt is not None:
        expected["Blackdog-Execution-Prompt-SHA256"] = (attempt.prompt_receipt.prompt_hash,)
        if attempt.prompt_receipt.source:
            expected["Blackdog-Execution-Prompt-Source"] = (attempt.prompt_receipt.source,)
        if attempt.prompt_receipt.mode:
            expected["Blackdog-Execution-Prompt-Mode"] = (attempt.prompt_receipt.mode,)
    if attempt.user_prompt_receipt is not None:
        expected["Blackdog-Request-Prompt-SHA256"] = (attempt.user_prompt_receipt.prompt_hash,)
        if attempt.user_prompt_receipt.source:
            expected["Blackdog-Request-Prompt-Source"] = (attempt.user_prompt_receipt.source,)
        if attempt.user_prompt_receipt.mode:
            expected["Blackdog-Request-Prompt-Mode"] = (attempt.user_prompt_receipt.mode,)
    if intent.residuals:
        expected["Blackdog-Residual"] = intent.residuals
    if intent.followup_candidates:
        expected["Blackdog-Followup"] = intent.followup_candidates
    return expected


def _verify_canonical_commit(
    primary: Path,
    *,
    task: TaskRecord,
    attempt: TaskAttemptRecord,
    intent: LandingIntent,
    commit: str,
) -> None:
    resolved = _run_git(primary, "rev-parse", f"{commit}^{{commit}}")
    parents = _run_git(primary, "rev-list", "--parents", "-n", "1", resolved).split()
    if len(parents) != 2 or parents[1] != intent.target_base_commit:
        raise LandingTransactionError("canonical landing commit does not have the frozen target base as its sole parent")
    if _run_git(primary, "rev-parse", f"{resolved}^{{tree}}") != intent.expected_source_tree_hash:
        raise LandingTransactionError("canonical landing commit tree conflicts with the frozen source tree")
    if _changed_paths(primary, parents[1], resolved) != intent.changed_paths:
        raise LandingTransactionError("canonical landing commit changed paths conflict with the frozen intent")
    message = _run_git(primary, "show", "-s", "--format=%B", resolved).rstrip() + "\n"
    if message != intent.commit_message:
        raise LandingTransactionError("canonical landing commit message conflicts with the frozen intent")
    actual = _commit_trailers(primary, resolved)
    expected = _expected_commit_trailers(task_id=task.task_id, attempt=attempt, intent=intent)
    if actual != expected:
        raise LandingTransactionError("canonical landing commit trailers are missing, duplicated, or conflict with the frozen intent")
    if _stable_patch_id(primary, intent.target_base_commit, intent.source_head_commit) != _stable_patch_id(
        primary, parents[1], resolved
    ):
        raise LandingTransactionError("canonical landing commit patch does not match the frozen source patch")


def _stable_patch_id(repo_root: Path, base_commit: str, head_commit: str) -> str:
    patch = _run_git(repo_root, "diff", "--binary", base_commit, head_commit)
    if not patch:
        raise WorktreeError(f"commit range {base_commit[:12]}..{head_commit[:12]} has no patch")
    row = _run_git(repo_root, "patch-id", "--stable", input_text=patch)
    patch_id = row.split(maxsplit=1)[0] if row else ""
    if not patch_id:
        raise WorktreeError("could not derive stable patch identity")
    return patch_id


def _branch_cleanup_is_proven(
    profile: RepoProfile,
    *,
    primary: Path,
    task: TaskRecord,
    attempt: TaskAttemptRecord,
    branch: str,
) -> bool:
    target = attempt.target_branch
    if not target or not _git_exists(primary, target):
        return False
    if _run_git_no_check(primary, "merge-base", "--is-ancestor", branch, target).returncode == 0:
        return True
    transaction = load_landing_transaction(profile, task_id=task.task_id, attempt_id=attempt.attempt_id)
    if transaction is None or not transaction.target_updated:
        return False
    target_data = transaction.data_for("target_updated")
    landed_commit = str(target_data.get("landed_commit") or "")
    if not landed_commit or attempt.branch != transaction.intent.branch:
        return False
    if _run_git_no_check(primary, "merge-base", "--is-ancestor", landed_commit, target).returncode != 0:
        return False
    branch_head = _run_git(primary, "rev-parse", f"{branch}^{{commit}}")
    if branch_head != transaction.intent.source_head_commit:
        return False
    parents = _run_git(primary, "rev-list", "--parents", "-n", "1", landed_commit).split()
    if len(parents) != 2:
        return False
    source_patch = _stable_patch_id(
        primary,
        transaction.intent.target_base_commit,
        transaction.intent.source_head_commit,
    )
    landed_patch = _stable_patch_id(primary, parents[1], landed_commit)
    return source_patch == landed_patch


@contextmanager
def _capture_reconciliation_fault(faults: list[Exception]):
    """Capture apply faults so callers can return the exact typed retry."""

    try:
        yield
    except Exception as exc:
        faults.append(exc)


def reconcile_task_landing(
    profile: RepoProfile,
    *,
    task_id: str,
    attempt_id: str,
    landed_commit: str,
    actor: str,
    apply: bool = False,
    reason: str | None = None,
) -> OperationResult:
    task = _task_for_id(profile, task_id)
    attempt = next((item for item in task.attempts if item.attempt_id == attempt_id), None)
    if attempt is None:
        raise TaskError(f"Unknown attempt {attempt_id!r} for task {task_id!r}")
    transaction = load_landing_transaction(profile, task_id=task_id, attempt_id=attempt_id)
    if transaction is None or not transaction.target_updated:
        raise TaskError("landing reconciliation requires the exact current-format target-updated transaction")
    if transaction.complete:
        return _result(
            operation="task.reconcile-landing",
            operation_status="succeeded",
            task_status=task.status,
            attempt_status=attempt.status,
            next_action=_complete_action(
                "landing_reconciliation_complete",
                "The exact landing transaction is already complete.",
                "Landing reconciliation is complete",
            ),
            payload={
                "task_id": task_id,
                "attempt_id": attempt_id,
                "landed_commit": attempt.landed_commit,
                "landing_transaction": transaction.to_dict(),
                "current_format": True,
            },
        )
    intent = transaction.intent
    if actor != attempt.actor or actor != intent.actor:
        raise TaskError("landing reconciliation actor conflicts with the attempt and transaction owner")
    primary = find_primary_worktree(profile.paths.project_root)
    resolved_commit = _run_git(primary, "rev-parse", f"{landed_commit}^{{commit}}")
    target_data = transaction.data_for("target_updated")
    canonical_data = transaction.data_for("canonical_commit_created")
    if (
        resolved_commit != str(target_data.get("landed_commit") or "")
        or resolved_commit != str(canonical_data.get("canonical_commit") or "")
    ):
        raise TaskError("reconciliation commit conflicts with the exact target-update transaction")
    target_head = _run_git(primary, "rev-parse", f"{intent.target_branch}^{{commit}}")
    if not _is_ancestor(primary, resolved_commit, target_head):
        raise TaskError(
            "reconciliation requires the exact landed commit to remain in target-branch ancestry"
        )
    source = Path(intent.worktree_path).resolve()
    if "task_cleanup_complete" not in transaction.phases:
        if (
            not source.is_dir()
            or _find_worktree_for_branch(primary, intent.branch) != source
            or _current_branch(source) != intent.branch
            or _run_git(source, "rev-parse", "HEAD") != intent.source_head_commit
        ):
            raise TaskError("landing reconciliation requires exact registered source-workspace ownership")
    _validate_landing_transaction(profile, task=task, attempt=attempt, transaction=transaction)
    _verify_canonical_commit(primary, task=task, attempt=attempt, intent=intent, commit=resolved_commit)
    parent = _run_git(primary, "rev-parse", f"{resolved_commit}^")
    changed = _changed_paths(primary, parent, resolved_commit)
    if changed != intent.changed_paths:
        raise TaskError("reconciliation changed paths conflict with the immutable landing request")
    proof = {
        "task_id": task_id,
        "attempt_id": attempt_id,
        "landed_commit": resolved_commit,
        "source_commit": intent.source_head_commit,
        "target_branch": intent.target_branch,
        "landing_transaction_id": transaction.transaction_id,
        "landing_transaction_phases": list(transaction.phases),
        "changed_paths": list(changed),
        "current_format": True,
        "apply": apply,
    }
    apply_argv = (
        _task_executable(profile),
        "task",
        "reconcile-landing",
        f"--project-root={profile.paths.project_root}",
        f"--task={task_id}",
        f"--attempt={attempt_id}",
        f"--landed-commit={resolved_commit}",
        f"--actor={actor}",
        *((f"--reason={reason}",) if reason else ()),
        "--apply",
    )
    if not apply:
        next_action = _command_action(
            action_id="apply_landing_reconciliation",
            detail="The exact current-format commit is reachable from the recorded target branch.",
            display="Apply the verified reconciliation",
            argv=apply_argv,
            safety="proof_guarded_mutation",
            mutation="runtime",
        )
        return _result(
            operation="task.reconcile-landing",
            operation_status="observed",
            task_status=task.status,
            attempt_status=attempt.status,
            next_action=next_action,
            payload=proof,
        )
    result: dict[str, Any]
    final_transaction: LandingTransaction
    faults: list[Exception] = []
    before_apply = _durable_task_bytes(profile)
    before_phases = transaction.phases
    with _capture_reconciliation_fault(faults), attempt_lifecycle_lock(
        profile, task_id=task_id, attempt_id=attempt_id
    ):
        transaction = load_landing_transaction(profile, task_id=task_id, attempt_id=attempt_id)
        if transaction is None or not transaction.target_updated:
            raise LandingTransactionError("landing reconciliation lost target-update evidence")
        _validate_landing_transaction(profile, task=task, attempt=attempt, transaction=transaction)
        if "temporary_cleanup_complete" not in transaction.phases:
            temporary = Path(intent.temporary_worktree_path)
            canonical_branch = f"blackdog/land-{intent.transaction_id[:16]}"
            registered = _find_worktree_for_branch(primary, canonical_branch)
            if temporary.exists() and registered != temporary:
                raise LandingTransactionError("temporary landing path is not owned by the transaction")
            if registered is not None and registered != temporary:
                raise LandingTransactionError("temporary landing branch is registered to another worktree")
            if _git_exists(primary, canonical_branch) and (
                _run_git(primary, "rev-parse", f"{canonical_branch}^{{commit}}") != resolved_commit
            ):
                raise LandingTransactionError("temporary landing branch does not name the canonical commit")
            if temporary.exists():
                _run_git(primary, "worktree", "remove", str(temporary))
            if _git_exists(primary, canonical_branch):
                _run_git(primary, "branch", "-d", canonical_branch)
            _record_phase(
                profile,
                intent,
                "temporary_cleanup_complete",
                {"temporary_worktree_removed": True, "temporary_branch_removed": True},
            )
            transaction = load_landing_transaction(profile, task_id=task_id, attempt_id=attempt_id)
            if transaction is None:
                raise LandingTransactionError("landing reconciliation lost temporary cleanup evidence")
        if "runtime_finalized" not in transaction.phases:
            result = reconcile_landed_attempt(
                profile,
                task_id=task_id,
                attempt_id=attempt_id,
                landed_commit=resolved_commit,
                actor=actor,
                summary=intent.summary,
                source_commit=intent.source_head_commit,
                target_commit=resolved_commit,
                target_branch=intent.target_branch,
                landing_transaction_id=transaction.transaction_id,
                landing_transaction_phases=transaction.phases,
                current_format=True,
                changed_paths=changed,
                validations=tuple(ValidationRecord(name, status) for name, status in intent.validations),
                residuals=intent.residuals,
                followup_candidates=intent.followup_candidates,
                note=intent.note,
                reason=reason,
            )
            _record_phase(
                profile,
                intent,
                "runtime_finalized",
                {"attempt_status": ATTEMPT_STATUS_SUCCESS, "landed_commit": resolved_commit},
            )
        else:
            result = {
                "task_id": task_id,
                "attempt_id": attempt_id,
                "landed_commit": resolved_commit,
                "source_commit": intent.source_head_commit,
                "landing_transaction_id": transaction.transaction_id,
                "attempt_status": ATTEMPT_STATUS_SUCCESS,
                "runtime_changed": False,
                "event_appended": False,
            }
        transaction = load_landing_transaction(profile, task_id=task_id, attempt_id=attempt_id)
        if transaction is None:
            raise LandingTransactionError("landing reconciliation lost runtime evidence")
        event_payload = {
            "task_id": task_id,
            "attempt_id": attempt_id,
            "branch": intent.branch,
            "target_branch": intent.target_branch,
            "source_commit": intent.source_head_commit,
            "landed_commit": resolved_commit,
            "changed_paths": list(intent.changed_paths),
        }
        if "land_event_recorded" not in transaction.phases:
            append_worktree_land_once(profile, intent=intent, payload=event_payload)
            _record_phase(profile, intent, "land_event_recorded", event_payload)
        transaction = load_landing_transaction(profile, task_id=task_id, attempt_id=attempt_id)
        if transaction is None:
            raise LandingTransactionError("landing reconciliation lost land-event evidence")
        current_task = _task_for_id(profile, task_id)
        current_attempt = next(item for item in current_task.attempts if item.attempt_id == attempt_id)
        if "task_cleanup_complete" not in transaction.phases:
            cleanup_payload = (
                _cleanup_workspace(
                    profile,
                    task=current_task,
                    attempt=current_attempt,
                    explicit_path=None,
                    explicit_branch=None,
                )
                if intent.cleanup
                else {"requested": False, "worktree_removed": False, "branch_deleted": False, "retained": True}
            )
            _record_phase(profile, intent, "task_cleanup_complete", cleanup_payload)
        transaction = load_landing_transaction(profile, task_id=task_id, attempt_id=attempt_id)
        if transaction is None:
            raise LandingTransactionError("landing reconciliation lost cleanup evidence")
        if "complete" not in transaction.phases:
            _record_phase(profile, intent, "complete", {"landed_commit": resolved_commit})
        final_transaction = load_landing_transaction(profile, task_id=task_id, attempt_id=attempt_id)
        if final_transaction is None or not final_transaction.complete:
            raise LandingTransactionError("landing reconciliation did not complete the transaction")
        _validate_landing_transaction(
            profile,
            task=_task_for_id(profile, task_id),
            attempt=current_attempt,
            transaction=final_transaction,
        )
    if faults:
        fault = faults[0]
        observed = load_landing_transaction(profile, task_id=task_id, attempt_id=attempt_id)
        if observed is None:
            raise fault
        current_task = _task_for_id(profile, task_id)
        current_attempt = next(
            item for item in current_task.attempts if item.attempt_id == attempt_id
        )
        target_head_result = _run_git_no_check(
            primary,
            "rev-parse",
            f"{intent.target_branch}^{{commit}}",
        )
        target_head_after = target_head_result.stdout.strip()
        post_cas_proven = bool(
            observed.target_updated
            and str(observed.data_for("target_updated").get("landed_commit") or "")
            == resolved_commit
            and str(observed.data_for("canonical_commit_created").get("canonical_commit") or "")
            == resolved_commit
            and target_head_result.returncode == 0
            and _run_git_no_check(
                primary,
                "merge-base",
                "--is-ancestor",
                resolved_commit,
                target_head_after,
            ).returncode
            == 0
        )
        runtime_reconciled = bool(
            current_attempt.status == ATTEMPT_STATUS_SUCCESS
            and current_attempt.landed_commit == resolved_commit
            and current_attempt.commit == intent.source_head_commit
        )
        mutation_started = bool(
            _durable_task_bytes(profile) != before_apply
            or observed.phases != before_phases
            or runtime_reconciled
        )
        retryable = post_cas_proven
        next_action = (
            _command_action(
                action_id="apply_landing_reconciliation",
                detail=(
                    f"Reconciliation reached durable landing phase {observed.last_phase!r}; "
                    "rerun the exact proof-guarded apply request."
                ),
                display="Resume the verified reconciliation",
                argv=apply_argv,
                safety="proof_guarded_mutation",
                mutation="runtime",
            )
            if retryable
            else _blocked_action(
                "landing_reconciliation_conflict",
                str(fault),
                "Preserve the transaction evidence for inspection",
            )
        )
        return _result(
            operation="task.reconcile-landing",
            operation_status="partial" if retryable else "blocked",
            task_status=current_task.status,
            attempt_status=current_attempt.status,
            next_action=next_action,
            payload={
                **proof,
                "landing_transaction": observed.to_dict(),
                "post_cas_proven": post_cas_proven,
                "runtime_reconciled": runtime_reconciled,
                "error": str(fault),
            },
            mutation_started=mutation_started,
            mutation_completed=False,
            mutation_phase=(
                "landing_runtime_finalized"
                if runtime_reconciled and "runtime_finalized" not in observed.phases
                else f"landing_{observed.last_phase}"
                if mutation_started
                else "none"
            ),
            failure_code=classify_lifecycle_exception(fault).failure_code,
        )
    task = _task_for_id(profile, task_id)
    return _result(
        operation="task.reconcile-landing",
        operation_status="succeeded",
        task_status=task.status,
        attempt_status=ATTEMPT_STATUS_SUCCESS,
        next_action=_complete_action(
            "landing_reconciliation_complete",
            "The verified commit and terminal runtime now agree.",
            "Landing reconciliation is complete",
        ),
        payload={**proof, **result, "landing_transaction": final_transaction.to_dict()},
        mutation_started=True,
        mutation_completed=True,
        mutation_phase="runtime_and_event_finalized",
    )


def _cleanup_workspace(
    profile: RepoProfile,
    *,
    task: TaskRecord,
    attempt: TaskAttemptRecord,
    explicit_path: str | None,
    explicit_branch: str | None,
    retain_unlanded: bool = False,
) -> dict[str, Any]:
    primary = find_primary_worktree(profile.paths.project_root)
    if attempt.worktree_path is None or attempt.branch is None:
        raise WorktreeError("task attempt has no durable worktree identity")
    expected_path = Path(attempt.worktree_path).resolve()
    expected_branch = attempt.branch
    path = Path(explicit_path).expanduser().resolve() if explicit_path else expected_path
    branch = explicit_branch or attempt.branch
    if path != expected_path or branch != expected_branch:
        raise CleanupOwnershipError(
            worktree_path=path,
            branch=branch,
            expected_worktree_path=expected_path,
            expected_branch=expected_branch,
            detail="explicit cleanup inputs do not match the durable task workspace identity",
        )
    if path == primary:
        raise CleanupOwnershipError(
            worktree_path=path,
            branch=branch,
            expected_worktree_path=expected_path,
            expected_branch=expected_branch,
            detail="the durable task workspace resolves to the primary worktree",
        )
    registered = _find_worktree_for_branch(primary, branch)
    branch_exists = _git_exists(primary, branch)
    if path.exists():
        if registered != path:
            raise CleanupOwnershipError(
                worktree_path=path,
                branch=branch,
                expected_worktree_path=expected_path,
                expected_branch=expected_branch,
                detail="the durable path is not registered for the durable task branch",
            )
        if _repo_root(path) != path or _git_common_dir(path) != _git_common_dir(primary):
            raise CleanupOwnershipError(
                worktree_path=path,
                branch=branch,
                expected_worktree_path=expected_path,
                expected_branch=expected_branch,
                detail="the durable path is not an owned worktree of this repository",
            )
        if _current_branch(path) != branch:
            raise CleanupOwnershipError(
                worktree_path=path,
                branch=branch,
                expected_worktree_path=expected_path,
                expected_branch=expected_branch,
                detail="the durable task workspace is detached or checked out on a different branch",
            )
        if not branch_exists or _run_git(path, "rev-parse", "HEAD") != _run_git(primary, "rev-parse", f"{branch}^{{commit}}"):
            raise CleanupOwnershipError(
                worktree_path=path,
                branch=branch,
                expected_worktree_path=expected_path,
                expected_branch=expected_branch,
                detail="the durable task workspace HEAD does not match its branch",
            )
        if _implementation_dirty_paths(profile, path):
            raise CleanupOwnershipError(
                worktree_path=path,
                branch=branch,
                expected_worktree_path=expected_path,
                expected_branch=expected_branch,
                detail="the durable task workspace has uncommitted implementation changes",
            )
    elif registered is not None:
        raise CleanupOwnershipError(
            worktree_path=path,
            branch=branch,
            expected_worktree_path=expected_path,
            expected_branch=expected_branch,
            detail="the registered durable task worktree path is missing from the filesystem",
        )

    cleanup_proven = not branch_exists or _branch_cleanup_is_proven(
        profile,
        primary=primary,
        task=task,
        attempt=attempt,
        branch=branch,
    )
    retained_reason: str | None = None
    if branch_exists and not cleanup_proven:
        retained_reason = "branch contains work not proven represented on the recorded target"
        if not retain_unlanded:
            raise CleanupOwnershipError(
                worktree_path=path,
                branch=branch,
                expected_worktree_path=expected_path,
                expected_branch=expected_branch,
                detail=f"{retained_reason}; cleanup made no changes",
            )

    # Every refusal above is read-only.  Once removal begins, the exact retry
    # converges from either the before- or after-side-effect boundary.
    if retained_reason is None and path.exists():
        _run_git(primary, "worktree", "remove", str(path))
    if retained_reason is None and _git_exists(primary, branch):
        try:
            # Patch-equivalent canonical landing commits do not contain the
            # source commit in their ancestry.  The read-only proof above is
            # therefore the authority for deleting the exact owned branch.
            _run_git(primary, "branch", "-D", branch)
        except WorktreeError as exc:
            raise CleanupPostMutationError(
                worktree_path=path,
                branch=branch,
                branch_cleanup_reason="delete the proven durable task branch",
                branch_cleanup_proof="contained_or_patch_equivalent",
                force_delete=True,
                detail=str(exc),
            ) from exc

    payload = {
        "task_id": task.task_id,
        "attempt_id": attempt.attempt_id,
        "worktree_path": str(path),
        "worktree_removed": not path.exists(),
        "branch": branch,
        "branch_deleted": not _git_exists(primary, branch),
        "retained": path.exists() or _git_exists(primary, branch),
        "retained_reason": retained_reason,
    }
    cleanup_disposition = "retained" if payload["retained"] else "removed"
    event_id = hashlib.sha256(
        (
            f"blackdog.task.cleanup/v3\0{task.task_id}\0{attempt.attempt_id}"
            f"\0{path}\0{branch}\0{cleanup_disposition}"
        ).encode()
    ).hexdigest()
    try:
        append_event_once(
            profile.paths.events_file,
            event_id=event_id,
            event_type="task.cleanup",
            actor="blackdog",
            payload=payload,
        )
    except Exception as exc:
        raise CleanupEventFinalizationError(cleanup_payload=payload, detail=str(exc)) from exc
    return payload


def cleanup_task(
    profile: RepoProfile,
    *,
    task_id: str | None,
    path: str | None = None,
    branch: str | None = None,
    cwd: Path | None = None,
) -> OperationResult:
    task = _task_for_command(
        profile,
        task_id=task_id,
        cwd=cwd,
        require_certified_cwd=True,
    )
    state = load_runtime_state(profile.paths)
    active = active_task_attempt(state, task.task_id)
    if active is not None:
        return _result(
            operation="task.cleanup",
            operation_status="blocked",
            task_status=task.status,
            attempt_status=active.status,
            next_action=_close_choices(profile, task, active),
            payload={**_task_payload(task), "attempt": _attempt_payload(active)},
        )
    attempt = latest_task_attempt(state, task.task_id)
    if attempt is None:
        raise TaskError(f"Task {task.task_id!r} has no attempt workspace to clean")
    durable_path = Path(attempt.worktree_path).resolve() if attempt.worktree_path else None
    primary = find_primary_worktree(profile.paths.project_root)
    before_path = bool(durable_path and durable_path.exists())
    before_branch = _git_exists(primary, attempt.branch)
    try:
        payload = _cleanup_workspace(
            profile,
            task=task,
            attempt=attempt,
            explicit_path=path,
            explicit_branch=branch,
        )
    except CleanupOwnershipError as exc:
        return _result(
            operation="task.cleanup",
            operation_status="blocked",
            task_status=task.status,
            attempt_status=attempt.status,
            next_action=_command_action(
                action_id="inspect_cleanup_ownership",
                detail=str(exc),
                display="Inspect the durable task workspace identity",
                argv=(
                    _task_executable(profile),
                    "task",
                    "show",
                    f"--project-root={profile.paths.project_root}",
                    f"--task={task.task_id}",
                ),
                safety="read_only",
                mutation="none",
                disposition="repair_required",
            ),
            payload={**_task_payload(task), **exc.refusal_payload()},
            failure_code=exc.failure_code,
        )
    except Exception as exc:
        after_path = bool(durable_path and durable_path.exists())
        after_branch = _git_exists(primary, attempt.branch)
        mutated = (before_path, before_branch) != (after_path, after_branch)
        retry_argv = [
            _task_executable(profile),
            "task",
            "cleanup",
            f"--project-root={profile.paths.project_root}",
            f"--task={task.task_id}",
        ]
        if path is not None:
            retry_argv.append(f"--path={path}")
        if branch is not None:
            retry_argv.append(f"--branch={branch}")
        resources_absent = not after_path and not after_branch
        next_action = (
            _command_action(
                action_id="retry_task_cleanup_finalization",
                detail=f"Cleanup changed or already proved final Git state but durable cleanup evidence is incomplete: {exc}",
                display="Retry the exact task cleanup request",
                argv=tuple(retry_argv),
                safety="proof_guarded_mutation",
                mutation="git_and_filesystem",
            )
            if mutated or resources_absent
            else _blocked_action(
                "cleanup_safety_refusal",
                str(exc),
                "Preserve the task workspace and branch",
            )
        )
        return _result(
            operation="task.cleanup",
            operation_status="partial" if mutated or resources_absent else "blocked",
            task_status=task.status,
            attempt_status=attempt.status,
            next_action=next_action,
            payload={**_task_payload(task), "attempt": _attempt_payload(attempt), "error": str(exc)},
            mutation_started=mutated,
            mutation_completed=False,
            mutation_phase="cleanup_event_finalization_pending" if mutated or resources_absent else "none",
            failure_code=classify_lifecycle_exception(exc).failure_code,
        )
    return _result(
        operation="task.cleanup",
        operation_status="succeeded",
        task_status=task.status,
        attempt_status=attempt.status,
        next_action=_complete_action("cleanup_complete", "Cleanup evidence is durable and the safe cleanup is complete.", "Task cleanup is complete"),
        payload=payload,
        mutation_started=before_path or before_branch,
        mutation_completed=before_path or before_branch,
        mutation_phase="git_and_filesystem_and_event_finalized" if before_path or before_branch else "none",
    )


def close_task(
    profile: RepoProfile,
    *,
    task_id: str | None,
    actor: str | None,
    status: str | None,
    summary: str | None,
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
    cleanup: bool | None = None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
    cwd: Path | None = None,
) -> OperationResult:
    task = _task_for_command(
        profile,
        task_id=task_id,
        cwd=cwd,
        require_certified_cwd=status is not None,
    )
    state = load_runtime_state(profile.paths)
    active = active_task_attempt(state, task.task_id)
    if active is None:
        latest = latest_task_attempt(state, task.task_id)
        if latest is None:
            raise TaskError(f"Task {task.task_id!r} has no attempt to close")
        if status is None:
            return _result(
                operation="task.close",
                operation_status="succeeded",
                task_status=task.status,
                attempt_status=latest.status,
                next_action=_complete_action("close_complete", "The task attempt is already terminal.", "Task close is complete"),
                payload={**_task_payload(task), "attempt": _attempt_payload(latest)},
            )
        active = latest
    if status is None:
        next_action = _close_choices(profile, task, active)
        return _result(
            operation="task.close",
            operation_status="blocked",
            task_status=task.status,
            attempt_status=active.status,
            next_action=next_action,
            payload={**_task_payload(task), "attempt": _attempt_payload(active)},
        )
    resolved_summary = str(summary or "").strip()
    if not resolved_summary:
        return _result(
            operation="task.close",
            operation_status="blocked",
            task_status=task.status,
            attempt_status=active.status,
            next_action=_blocked_action(
                "close_evidence_required",
                "Task close requires an explicit nonblank completion summary.",
                "Supply exact close evidence",
                required_inputs=("summary", "validation"),
            ),
            payload={**_task_payload(task), "attempt": _attempt_payload(active)},
        )
    if not validations:
        return _result(
            operation="task.close",
            operation_status="blocked",
            task_status=task.status,
            attempt_status=active.status,
            next_action=_blocked_action(
                "close_evidence_required",
                "Task close requires at least one explicit validation record.",
                "Supply exact close evidence",
                required_inputs=("validation",),
            ),
            payload={**_task_payload(task), "attempt": _attempt_payload(active)},
        )
    resolved_actor = actor or active.actor
    if resolved_actor != active.actor:
        raise TaskError(f"attempt {active.attempt_id!r} is owned by {active.actor!r}")
    retry_argv = _close_argv(
        profile,
        task_id=task.task_id,
        actor=resolved_actor,
        status=status,
        summary=resolved_summary,
        validations=validations,
        residuals=residuals,
        followup_candidates=followup_candidates,
        note=note,
        cleanup=cleanup,
        failure_class=failure_class,
        recovery_action=recovery_action,
        prompt_issue=prompt_issue,
        operator_issue=operator_issue,
    )
    with attempt_lifecycle_lock(profile, task_id=task.task_id, attempt_id=active.attempt_id):
        try:
            finished = finish_task(
                profile,
                task_id=task.task_id,
                attempt_id=active.attempt_id,
                actor=resolved_actor,
                status=status,
                summary=resolved_summary,
                validations=validations,
                residuals=residuals,
                followup_candidates=followup_candidates,
                failure_class=failure_class,
                recovery_action=recovery_action,
                prompt_issue=prompt_issue,
                operator_issue=operator_issue,
                note=note,
                finalization_id=f"close-{active.attempt_id}",
            )
        except TaskFinalizationError as exc:
            next_action = _command_action(
                action_id="retry_task_close_finalization",
                detail=str(exc),
                display="Retry the exact task close finalization",
                argv=retry_argv,
                safety="proof_guarded_mutation",
                mutation="runtime",
            )
            return _result(
                operation="task.close",
                operation_status="partial",
                task_status=_task_for_id(profile, task.task_id).status,
                attempt_status=active.status,
                next_action=next_action,
                payload={**_task_payload(task), "attempt_id": active.attempt_id, "error": str(exc)},
                mutation_started=exc.mutation_started,
                mutation_completed=False,
                mutation_phase=exc.mutation_phase if exc.mutation_phase in {
                    "close_core_request_recorded",
                    "close_core_decision_recorded",
                    "close_runtime_finalized",
                } else ("close_runtime_finalized" if exc.mutation_started else "close_core_request_recorded"),
                failure_code=classify_lifecycle_exception(exc).failure_code,
            )
        except TaskError as exc:
            current_task = _task_for_id(profile, task.task_id)
            current_attempt = next(
                (item for item in current_task.attempts if item.attempt_id == active.attempt_id),
                active,
            )
            return _result(
                operation="task.close",
                operation_status="blocked",
                task_status=current_task.status,
                attempt_status=current_attempt.status,
                next_action=_blocked_action(
                    "close_request_conflict",
                    str(exc),
                    "Preserve the immutable task finalization",
                ),
                payload={
                    **_task_payload(current_task),
                    "attempt": _attempt_payload(current_attempt),
                    "error": str(exc),
                },
            )
        cleanup_payload = None
        if cleanup:
            try:
                cleanup_payload = _cleanup_workspace(
                    profile,
                    task=task,
                    attempt=active,
                    explicit_path=None,
                    explicit_branch=None,
                    retain_unlanded=True,
                )
            except Exception as exc:
                return _result(
                    operation="task.close",
                    operation_status="partial",
                    task_status=_task_for_id(profile, task.task_id).status,
                    attempt_status=finished.status,
                    next_action=_command_action(
                        action_id="retry_task_close_finalization",
                        detail=f"Task finalization is durable, but requested cleanup is incomplete: {exc}",
                        display="Retry the exact task close request",
                        argv=retry_argv,
                        safety="proof_guarded_mutation",
                        mutation="git_and_runtime",
                    ),
                    payload={**_task_payload(_task_for_id(profile, task.task_id)), "attempt": _attempt_payload(finished), "error": str(exc)},
                    mutation_started=True,
                    mutation_completed=False,
                    mutation_phase="close_cleanup_pending",
                    failure_code=classify_lifecycle_exception(exc).failure_code,
                )
    task = _task_for_id(profile, task.task_id)
    return _result(
        operation="task.close",
        operation_status="succeeded",
        task_status=task.status,
        attempt_status=finished.status,
        next_action=_complete_action("close_complete", "Task finalization and requested cleanup are durable.", "Task close is complete"),
        payload={**_task_payload(task), "attempt": _attempt_payload(finished), "cleanup": cleanup_payload},
        mutation_started=True,
        mutation_completed=True,
        mutation_phase="close_complete",
    )


def render_preflight_text(payload: dict[str, Any]) -> str:
    lines = [
        f"[blackdog-worktree] current: {payload['current_worktree']} [{payload['current_branch']}]",
        f"[blackdog-worktree] workspace role: {payload['workspace_role']}",
        f"[blackdog-worktree] primary: {payload['primary_worktree']} [{payload['primary_branch']}]",
        f"[blackdog-worktree] implementation dirty: {'yes' if payload['implementation_dirty'] else 'no'}",
        f"[blackdog-worktree] landing state: {payload['landing_state']}",
        f"[blackdog-worktree] .VE rule: {payload['ve_expectation']}",
    ]
    return "\n".join(lines) + "\n"


def _render_operation(result: OperationResult, label: str) -> str:
    payload = result.to_dict()
    lines = [
        f"[blackdog-{label}] operation: {payload['operation']}",
        f"[blackdog-{label}] status: {payload['operation_status']}",
    ]
    if payload.get("task_id"):
        lines.append(f"[blackdog-{label}] task: {payload['task_id']}")
    if payload.get("attempt_id"):
        lines.append(f"[blackdog-{label}] attempt: {payload['attempt_id']}")
    if payload.get("worktree_path"):
        lines.append(f"[blackdog-{label}] workspace: {payload['worktree_path']}")
    if payload.get("landed_commit"):
        lines.append(f"[blackdog-{label}] landed commit: {payload['landed_commit']}")
    action = payload["next_action"]
    lines.append(f"[blackdog-{label}] next: {action['display']} ({action['kind']})")
    if action.get("command"):
        lines.append(f"[blackdog-{label}] command: {action['command']}")
    return "\n".join(lines) + "\n"


def render_task_begin_text(spec: OperationResult, *, show_prompt: bool = False) -> str:
    del show_prompt
    return _render_operation(spec, "task")


def render_show_text(payload: OperationResult, *, surface: str = "task") -> str:
    return _render_operation(payload, surface)


def render_recover_text(payload: OperationResult) -> str:
    return _render_operation(payload, "task")


def render_task_state_text(payload: OperationResult) -> str:
    return _render_operation(payload, "task")


def render_land_text(payload: OperationResult, *, surface: str = "task") -> str:
    return _render_operation(payload, surface)


def render_landing_reconciliation_text(payload: OperationResult) -> str:
    return _render_operation(payload, "task")


def render_close_text(payload: OperationResult, *, surface: str = "task") -> str:
    return _render_operation(payload, surface)


def render_cleanup_text(payload: OperationResult, *, surface: str = "task") -> str:
    return _render_operation(payload, surface)


def render_worktree_table_text(payload: dict[str, Any]) -> str:
    lines = ["task\tattempt\ttask_status\tattempt_status\tbranch\tworktree"]
    for row in payload["rows"]:
        lines.append(
            "\t".join(
                str(row.get(key) or "")
                for key in ("task_id", "attempt_id", "task_status", "attempt_status", "branch", "worktree_path")
            )
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "TaskBeginPreflightError",
    "WorktreeError",
    "WorktreeSpec",
    "WTAM_WORKTREE_VE_NOTE",
    "begin_task_worktree",
    "build_worktree_table",
    "cancel_task",
    "cleanup_task",
    "close_task",
    "default_task_branch",
    "default_task_worktree_path",
    "dirty_paths",
    "find_primary_worktree",
    "find_worktree_for_branch",
    "land_task",
    "reconcile_task_landing",
    "recover_task",
    "render_cleanup_text",
    "render_close_text",
    "render_land_text",
    "render_landing_reconciliation_text",
    "render_preflight_text",
    "render_recover_text",
    "render_show_text",
    "render_task_begin_text",
    "render_task_state_text",
    "render_worktree_table_text",
    "reopen_task",
    "show_task",
    "task_begin_preflight_result",
    "worktree_contract",
    "worktree_preflight",
]
