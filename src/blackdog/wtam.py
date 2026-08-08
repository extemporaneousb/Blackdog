from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Any
import os
import re
import stat
import subprocess
import time
import uuid

from blackdog.contract import ContractDocument, contract_documents, managed_skill_relative_path
from blackdog.closing import (
    CloseRequest,
    CloseTransactionError,
    close_requests_for_task,
    load_close_event,
    load_close_request,
    load_close_request_by_id,
    load_close_request_record_by_id,
    record_close_event,
    record_close_request,
    worktree_cleanup_event_id,
)
from blackdog.handlers import (
    HANDLER_STATUS_BLOCKED,
    HANDLER_STATUS_UPDATED,
    HANDLER_STATUS_VALIDATED,
    HandlerAction,
    HandlerPlanSummary,
    execute_worktree_handlers,
    plan_worktree_handlers,
    validate_existing_worktree_handlers,
)
from blackdog.guards import (
    GuardTaskInput,
    RepositoryGuardRefusal,
    evaluate_task_begin_guards,
)
from blackdog.landing import (
    LANDING_PHASES,
    LandingIntent,
    LandingProof,
    LandingTransaction,
    LandingTransactionError,
    append_worktree_land_once,
    attempt_lifecycle_lock,
    exact_worktree_land_event,
    landing_phase_event_id,
    landing_transaction_id,
    load_landing_transaction,
    record_landing_abort,
    record_landing_abort_close_event,
    record_landing_abort_complete,
    record_landing_abort_cleanup,
    record_landing_abort_runtime,
    record_landing_abort_superseded,
    record_landing_phase,
    strict_json_equal,
    worktree_land_event_id,
)
from blackdog.landing_correction import (
    PHASE_INTENT_RECORDED as CORRECTION_PHASE_INTENT_RECORDED,
    PHASE_REBASE_COMPLETED as CORRECTION_PHASE_REBASE_COMPLETED,
    PHASE_VALIDATION_COMPLETED as CORRECTION_PHASE_VALIDATION_COMPLETED,
    LandingCorrection,
    LandingCorrectionIntent,
    load_landing_correction,
    load_landing_correction_selection,
    record_landing_correction_blocked,
    record_landing_correction_handed_to_landing,
    record_landing_correction_intent,
    record_landing_correction_rebase_completed,
    record_landing_correction_validation_completed,
)
from blackdog.lifecycle import (
    CleanupEventFinalizationError,
    CleanupOwnershipError,
    CleanupPostMutationError,
    DirtyPrimaryWorktreeError,
    DirtyTargetWorktreeError,
    GitReferenceInspection,
    LifecycleAction,
    LifecycleContext,
    LifecycleGitError,
    MissingTaskWorktreeError,
    NextAction,
    NoChangesToLandError,
    OperationResult,
    StaleTaskBranchError,
    WorktreeError,
    classify_lifecycle_exception,
    decide_next_action,
    landing_evidence_required_action,
)
from blackdog.observability import observe_operation_result
from blackdog.prompt_artifacts import (
    PromptArtifactError,
    persist_prompt_receipts,
    verify_prompt_artifact,
)
from blackdog.prompting import tune_prompt
from blackdog.validation import ValidationRunResult, run_validation_commands
from blackdog_core.backlog import (
    AbandonedLandingEligibility,
    BacklogError,
    StaleClaimReleaseConflictError,
    StaleClaimReleaseFinalizationError,
    StaleClaimReleaseResult,
    TaskRuntimeTransitionFinalizationError,
    TaskRuntimeTransitionGuardConflictError,
    TaskRuntimeTransitionResult,
    TaskFinalizationEvidence,
    TaskSpec,
    Workset,
    find_workset,
    finish_task,
    inspect_task_finalization,
    landing_reconciliation_id,
    load_planning_state,
    pending_task_runtime_transition,
    pending_stale_claim_release,
    pending_stale_claim_release_for_workset,
    repair_task_start_events,
    reconcile_landed_attempt,
    release_stale_task_claim,
    resume_predecessor_identity,
    set_task_runtime_status,
    start_task,
    task_resume_attempt_id,
    task_start_event_contracts,
    task_start_event_id,
    upsert_workset,
    workset_to_payload,
)
from blackdog_core.codex_sessions import current_codex_runtime_context, current_codex_session_ref
from blackdog_core.profile import RepoProfile, load_profile, slugify
from blackdog_core.state import (
    ATTEMPT_STATUS_ABANDONED,
    ATTEMPT_STATUS_BLOCKED,
    CODEX_CAPTURE_MISSING_REASON_CAPTURE_ERROR,
    CODEX_CAPTURE_STATUS_MISSING,
    FAILURE_CLASS_ABANDONED,
    FAILURE_CLASS_DIRTY_PRIMARY,
    FAILURE_CLASS_MISSING_WORKTREE,
    FAILURE_CLASS_NO_CHANGES,
    FAILURE_CLASS_STALE_BRANCH,
    FAILURE_CLASS_UNKNOWN,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_IN_PROGRESS,
    ATTEMPT_STATUS_SUCCESS,
    PROMPT_MODE_RAW,
    PROMPT_MODE_SKILL,
    PROMPT_MODE_TUNED,
    CodexSessionRefRecord,
    PromptReceiptRecord,
    StoreError,
    TASK_STATUS_BLOCKED,
    TASK_STATUS_CANCELED,
    TASK_STATUS_DONE,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_PLANNED,
    TaskRuntimeRecord,
    VALIDATION_STATUSES,
    ValidationRecord,
    active_task_attempt,
    append_event,
    append_event_once,
    create_prompt_receipt,
    exclusive_file_lock,
    find_task_attempt,
    latest_task_attempt,
    load_events,
    load_runtime_state,
    merge_workset_runtime,
    mutate_runtime_state,
    now_iso,
    parse_iso,
    prompt_receipt_reference,
    task_claim_index,
    task_state_index,
    workset_claim,
)


WTAM_WORKTREE_VE_NOTE = (
    ".VE is unversioned and bound to this worktree path; bootstrap one per worktree and do not reuse another "
    "worktree's .VE."
)
WORKSPACE_MODE_GIT_WORKTREE = "git-worktree"
WORKTREE_ROLE_PRIMARY = "primary"
WORKTREE_ROLE_TASK = "task"
WORKTREE_ROLE_LINKED = "linked"
LEGACY_SETUP_RECEIPT_SCHEMA_VERSION = 1
SETUP_RECEIPT_SCHEMA_VERSION = 2
AUTO_TASK_ENVELOPE_RESERVATION_SCHEMA_VERSION = 1
_AUTO_TASK_ENVELOPE_RESERVATION_KEY = "task_begin_reservation"
CANONICAL_COMMIT_FORMAT_VERSION = "2"
AUTOMATIC_STALE_REBASE_MAX_ATTEMPTS = 1
AUTOMATIC_STALE_REBASE_VALIDATION_NAME = "blackdog-post-rebase-validation"


class AutomaticStaleRecoveryError(LifecycleGitError):
    """A pre-intent automatic stale correction that requires agent handoff."""

    operator_issue = True

    def __init__(
        self,
        *,
        state: str,
        detail: str,
        evidence: Mapping[str, Any],
    ) -> None:
        if state not in {"conflict", "validation_failed", "unsafe", "retry_exhausted"}:
            raise ValueError(f"unsupported automatic stale recovery state: {state}")
        self.state = state
        self.failure_code = (
            FAILURE_CLASS_UNKNOWN
            if state == "validation_failed"
            else FAILURE_CLASS_STALE_BRANCH
        )
        self.recovery_action = (
            "rebase_task_branch"
            if state == "retry_exhausted"
            else f"automatic_stale_recovery_{state}"
        )
        self.automatic_stale_recovery = dict(evidence)
        super().__init__(detail)


class TaskBeginPreflightError(BacklogError):
    """A typed, pre-mutation refusal to begin a task attempt."""

    def __init__(
        self,
        detail: str,
        *,
        failure_code: str,
        action_id: str,
        reason_code: str,
        display: str,
        required_inputs: tuple[str, ...],
    ) -> None:
        self.failure_code = failure_code
        self.action_id = action_id
        self.reason_code = reason_code
        self.display = display
        self.required_inputs = required_inputs
        super().__init__(detail)
SKILL_PROVENANCE_SCHEMA_VERSION = 1
SKILL_PROVENANCE_SOURCE = "repo_managed"
WORKSPACE_ADOPTION_SCHEMA_VERSION = 1
LEGACY_RECONCILIATION_SCAN_LIMIT = 64
LEGACY_RECONCILIATION_REASON = "Automatically detected canonical legacy landing"
_ATOMIC_START_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "expected_predecessor_attempt_id",
        "start_kind",
        "expected_task_actor",
        "expected_execution_prompt_hash",
        "expected_execution_prompt_mode",
        "expected_request_prompt_hash",
        "expected_request_prompt_mode",
        "expected_task_updated_at",
        "workset_claim_created",
    }
)
_WORKTREE_START_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "base_ref",
        "base_commit",
        "primary_worktree",
    }
)


class LandingReconciliationProofError(WorktreeError):
    """A candidate was inspected successfully but failed canonical proof."""


class LandingReconciliationInspectionError(WorktreeError):
    """Git or durable evidence could not be inspected reliably."""


class _WorkspaceAdoptionTargetChanged(WorktreeError):
    def __init__(self, *, candidate_contained: bool) -> None:
        self.candidate_contained = candidate_contained
        super().__init__(
            "workspace adoption target now contains the predecessor candidate"
            if candidate_contained
            else "workspace adoption target changed before successor reservation"
        )


class _TaskStartProofConflict(BacklogError):
    """A read-only task-start lineage conflict that must block before reservation."""


@dataclass(frozen=True, slots=True)
class _TaskResumeGuard:
    attempt_id: str
    predecessor_attempt_id: str
    task_actor: str
    execution_prompt_hash: str
    execution_prompt_mode: str
    execution_prompt_source: str | None
    execution_prompt_replay_artifact_path: str | None
    request_prompt_hash: str
    request_prompt_mode: str
    request_prompt_source: str | None
    request_prompt_replay_artifact_path: str | None
    task_updated_at: str
    retry_reserved_successor: bool = False
    start_kind: str = "resume"


@dataclass(frozen=True, slots=True)
class WorktreeSpec:
    workset_id: str
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
    prompt_hash: str
    prompt_source: str | None
    prompt_mode: str | None
    workspace_ve: str | None
    workspace_blackdog_path: str | None
    runtime_mode: str | None
    source_root: str | None
    source_mode: str | None
    script_policy: str | None
    setup_receipt: dict[str, Any]
    handlers: HandlerPlanSummary
    workspace_action: str = "created"
    predecessor_attempt_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["handlers"] = self.handlers.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class WorktreePreview:
    workset_id: str
    task_id: str
    task_title: str
    task_slug: str
    actor: str
    execution_model: str
    workspace_identity: str | None
    branch: str
    base_ref: str
    base_commit: str
    target_branch: str
    integration_branch: str
    worktree_path: str
    primary_worktree: str
    current_worktree: str
    model: str | None
    reasoning_effort: str | None
    note: str | None
    prompt_hash: str
    prompt_source: str | None
    prompt_mode: str | None
    prompt_text: str | None
    task_paths: tuple[str, ...]
    task_docs: tuple[str, ...]
    task_checks: tuple[str, ...]
    validation_commands: tuple[str, ...]
    doc_routing_defaults: tuple[str, ...]
    contract_documents: tuple[ContractDocument, ...]
    handlers: HandlerPlanSummary
    existing_branch_worktree: str | None
    path_exists: bool
    start_ready: bool
    conflicts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["contract_documents"] = [item.to_dict() for item in self.contract_documents]
        payload["handlers"] = self.handlers.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class TaskBeginSpec:
    workset_id: str
    task_id: str
    task_title: str
    actor: str
    created_workset: bool
    prompt_mode: str
    user_prompt_hash: str
    user_prompt_source: str | None
    user_prompt_replay_artifact_path: str | None
    execution_prompt_hash: str
    execution_prompt_source: str | None
    execution_prompt_replay_artifact_path: str | None
    execution_prompt_text: str | None
    worktree: WorktreeSpec

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["worktree"] = self.worktree.to_dict()
        skill_provenance = _bounded_skill_provenance(self.worktree.setup_receipt)
        if skill_provenance is not None:
            payload["skill_provenance"] = skill_provenance
        return payload


@dataclass(frozen=True, slots=True)
class _BranchCleanupPlan:
    branch_exists: bool
    force_delete: bool
    branch_tip: str | None
    reason: str
    proof_state: str


@dataclass(frozen=True, slots=True)
class _CleanupClassification:
    status: str
    reason: str | None
    proof_state: str | None


WORKTREE_TABLE_COLUMNS = (
    "workset_id",
    "task_id",
    "task_title",
    "state",
    "latest_attempt_status",
    "started_at",
    "ended_at",
    "last_commit_at",
    "last_commit",
    "last_commit_message",
    "branch",
    "target_branch",
    "worktree_path",
    "worktree_dirty_count",
    "branch_ahead_of_target",
    "changed_paths_count",
    "size_bytes",
    "size",
    "cleanup_status",
    "cleanup_reason",
    "cleanup_command",
    "recommended_action",
)


def _run_git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
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


def _run_git_with_input(repo_root: Path, *args: str, input_text: str) -> str:
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


def _run_git_bytes(
    repo_root: Path,
    *args: str,
    input_bytes: bytes | None = None,
) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        input=input_bytes,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr.decode("utf-8", errors="replace").strip()
            or completed.stdout.decode("utf-8", errors="replace").strip()
            or f"exit code {completed.returncode}"
        )
        raise WorktreeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout


def _repo_root(project_root: Path) -> Path:
    return Path(_run_git(project_root, "rev-parse", "--show-toplevel")).resolve()


def _is_primary_worktree(repo_root: Path) -> bool:
    return (repo_root / ".git").is_dir()


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _parse_worktree_list(repo_root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    raw = _run_git(repo_root, "worktree", "list", "--porcelain")
    for line in raw.splitlines():
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
    repo_root = _repo_root(project_root)
    for row in _parse_worktree_list(repo_root):
        path = Path(str(row.get("worktree") or "")).resolve()
        if path and _is_primary_worktree(path):
            return path
    raise WorktreeError("could not find primary worktree")


def _find_worktree_for_branch(project_root: Path, branch_ref: str) -> Path | None:
    repo_root = _repo_root(project_root)
    for row in _parse_worktree_list(repo_root):
        if str(row.get("branch") or "") == branch_ref:
            return Path(str(row["worktree"])).resolve()
    return None


def find_worktree_for_branch(profile: RepoProfile, branch: str) -> str | None:
    branch_ref = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
    resolved = _find_worktree_for_branch(profile.paths.project_root, branch_ref)
    return str(resolved) if resolved is not None else None


def _is_git_worktree_path(path: Path) -> bool:
    return (path / ".git").exists()


def _worktree_branch_map(repo_root: Path) -> dict[str, Path]:
    rows: dict[str, Path] = {}
    for row in _parse_worktree_list(repo_root):
        branch = str(row.get("branch") or "")
        worktree = row.get("worktree")
        if branch and worktree:
            rows[branch] = Path(str(worktree)).resolve()
    return rows


def _status_entries(repo_root: Path) -> list[list[str]]:
    completed = _run_git_no_check(repo_root, "status", "--porcelain")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git status --porcelain failed: {detail}")
    rows: list[list[str]] = []
    for line in (completed.stdout or "").splitlines():
        path_text = line[3:].strip()
        rows.append([item.strip() for item in path_text.split(" -> ") if item.strip()])
    return rows


def _runtime_ignore_prefixes(profile: RepoProfile, *, repo_root: Path | None = None) -> tuple[str, ...]:
    repo_root = (repo_root or _repo_root(profile.paths.project_root)).resolve()
    control_dir = profile.paths.control_dir.resolve()
    if not _is_within(repo_root, control_dir):
        return ()
    relative = control_dir.relative_to(repo_root).as_posix().rstrip("/")
    return (f"{relative}/",)


def _configured_generated_ignores(profile: RepoProfile, *, repo_root: Path) -> tuple[frozenset[str], tuple[str, ...]]:
    exact_paths: set[str] = set()
    prefixes: set[str] = set()

    def add_path(value: str | None, *, directory: bool) -> None:
        if not value:
            return
        candidate = Path(value)
        resolved = candidate.resolve() if candidate.is_absolute() else (repo_root / candidate).resolve()
        if not _is_within(repo_root, resolved):
            return
        relative = resolved.relative_to(repo_root).as_posix().rstrip("/")
        if not relative:
            return
        if directory:
            prefixes.add(f"{relative}/")
        exact_paths.add(relative)

    for handler in profile.handlers:
        if not handler.enabled:
            continue
        add_path(getattr(handler, "root_path", None), directory=True)
        add_path(getattr(handler, "worktree_path", None), directory=True)
        add_path(getattr(handler, "launcher_path", None), directory=False)
    return frozenset(exact_paths), tuple(sorted(prefixes))


def _status_ignores(
    profile: RepoProfile,
    *,
    repo_root: Path,
    include_generated: bool = True,
) -> tuple[frozenset[str], tuple[str, ...]]:
    runtime_prefixes = set(_runtime_ignore_prefixes(profile, repo_root=repo_root))
    if not include_generated:
        return frozenset(), tuple(sorted(runtime_prefixes))
    generated_paths, generated_prefixes = _configured_generated_ignores(profile, repo_root=repo_root)
    runtime_prefixes.update(generated_prefixes)
    return generated_paths, tuple(sorted(runtime_prefixes))


def dirty_paths(
    repo_root: Path,
    *,
    ignore_paths: frozenset[str] = frozenset(),
    ignore_prefixes: tuple[str, ...] = (),
) -> list[str]:
    rows = _status_entries(repo_root)
    dirty: list[str] = []
    for candidates in rows:
        for candidate in candidates:
            if candidate in ignore_paths:
                continue
            if any(candidate.startswith(prefix) for prefix in ignore_prefixes):
                continue
            dirty.append(candidate)
    return sorted(dict.fromkeys(dirty))


def _status_dirty(
    repo_root: Path,
    *,
    ignore_paths: frozenset[str] = frozenset(),
    ignore_prefixes: tuple[str, ...] = (),
) -> bool:
    return bool(dirty_paths(repo_root, ignore_paths=ignore_paths, ignore_prefixes=ignore_prefixes))


def _managed_dirty_paths(profile: RepoProfile, repo_root: Path, *, include_generated: bool = True) -> list[str]:
    ignore_paths, ignore_prefixes = _status_ignores(
        profile,
        repo_root=repo_root,
        include_generated=include_generated,
    )
    return dirty_paths(repo_root, ignore_paths=ignore_paths, ignore_prefixes=ignore_prefixes)


def _managed_status_dirty(profile: RepoProfile, repo_root: Path, *, include_generated: bool = True) -> bool:
    return bool(_managed_dirty_paths(profile, repo_root, include_generated=include_generated))


def _current_branch(repo_root: Path) -> str:
    branch = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise WorktreeError(f"detached HEAD at {repo_root}; specify --from explicitly")
    return branch


def command_workspace_root(profile: RepoProfile, *, cwd: Path | None = None) -> Path:
    candidate = (cwd or Path.cwd()).resolve()
    try:
        candidate_primary = find_primary_worktree(candidate)
        profile_primary = find_primary_worktree(profile.paths.project_root)
    except WorktreeError:
        return profile.paths.project_root
    if candidate_primary == profile_primary:
        return candidate
    return profile.paths.project_root


_NEW_TASK_BEGIN_HINT = (
    "For new work, use `blackdog task begin` without --workset/--task; "
    "low-level worktree commands only target existing task ids from `task show`, "
    "`task recover`, or a prior `task begin` response."
)


def _require_workset_and_task(profile: RepoProfile, *, workset_id: str, task_id: str) -> tuple[Workset, TaskSpec]:
    planning_state = load_planning_state(profile.paths)
    workset = find_workset(planning_state, workset_id)
    if workset is None:
        raise BacklogError(f"Unknown workset {workset_id!r}. {_NEW_TASK_BEGIN_HINT}")
    for task in workset.tasks:
        if task.task_id == task_id:
            return workset, task
    raise BacklogError(f"Unknown task {task_id!r} in workset {workset_id!r}. {_NEW_TASK_BEGIN_HINT}")


def _task_slug(workset_id: str, task: TaskSpec) -> str:
    return slugify(f"{workset_id}-{task.task_id}-{task.title}")


def _derive_task_title(prompt: str) -> str:
    normalized = " ".join(str(prompt).split())
    if not normalized:
        return "Task"
    title = normalized[:72].rstrip()
    if len(normalized) > 72 and " " in title:
        title = title.rsplit(" ", 1)[0]
    return title.rstrip(" .") or "Task"


def _guard_task_start(
    profile: RepoProfile,
    *,
    actor: str,
    prompt_mode: str,
    execution_receipt: PromptReceiptRecord,
    user_receipt: PromptReceiptRecord,
) -> dict[str, Any]:
    try:
        guard_receipts = evaluate_task_begin_guards(
            profile,
            task=GuardTaskInput(
                actor=actor,
                prompt_mode=prompt_mode,
                execution_prompt_text=execution_receipt.text or "",
                execution_prompt_hash=execution_receipt.prompt_hash,
                request_prompt_text=user_receipt.text or "",
                request_prompt_hash=user_receipt.prompt_hash,
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
        "schema_version": 1,
        "status": "passed",
        "guard_receipts": [dict(receipt) for receipt in guard_receipts],
    }


def _managed_skill_provenance(profile: RepoProfile, *, workspace_root: Path) -> dict[str, Any]:
    relative_path = managed_skill_relative_path(profile)
    skill_path = workspace_root / relative_path
    try:
        skill_bytes = skill_path.read_bytes()
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise TaskBeginPreflightError(
            "task begin --prompt-mode skill requires a readable repo-managed skill at "
            f"{relative_path.as_posix()}: {detail}",
            failure_code="managed_skill_missing",
            action_id="managed_skill_required",
            reason_code="managed_skill_missing",
            display="Restore or refresh the repo-managed Blackdog skill",
            required_inputs=("managed_skill",),
        ) from exc
    return {
        "schema_version": SKILL_PROVENANCE_SCHEMA_VERSION,
        "path": relative_path.as_posix(),
        "sha256": hashlib.sha256(skill_bytes).hexdigest(),
        "source": SKILL_PROVENANCE_SOURCE,
    }


def _bounded_skill_provenance(setup_receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(setup_receipt, dict):
        return None
    value = setup_receipt.get("skill_provenance")
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    sha256 = value.get("sha256")
    if (
        value.get("schema_version") != SKILL_PROVENANCE_SCHEMA_VERSION
        or value.get("source") != SKILL_PROVENANCE_SOURCE
        or not isinstance(path, str)
        or not path
        or path.startswith("/")
        or "\\" in path
        or ".." in path.split("/")
        or not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
    ):
        return None
    return {
        "schema_version": SKILL_PROVENANCE_SCHEMA_VERSION,
        "path": path,
        "sha256": sha256,
        "source": SKILL_PROVENANCE_SOURCE,
    }


def _handler_setup_receipt(
    guard_receipt: dict[str, Any],
    handlers: HandlerPlanSummary,
    *,
    skill_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    probes: list[dict[str, Any]] = []
    blockers: list[str] = []
    for action in handlers.actions:
        status = (
            "ok"
            if action.status
            in {"validated", "created", HANDLER_STATUS_UPDATED, "preserved", "skipped"}
            else "blocked"
        )
        probe_name = f"{action.handler_id}.{action.action}"
        probes.append(
            {
                "name": probe_name,
                "status": status,
                "handler_id": action.handler_id,
                "kind": action.kind,
                "action": action.action,
                "target_path": action.target_path,
                "required": True,
                "message": action.message,
                "elapsed_ms": action.elapsed_ms,
            }
        )
        if status == "blocked":
            blockers.append(probe_name)
    receipt = {
        "schema_version": SETUP_RECEIPT_SCHEMA_VERSION,
        "checked_at": now_iso(),
        "status": "ok" if handlers.ready and not blockers else "blocked",
        "blockers": blockers,
        "guard_receipts": list(guard_receipt.get("guard_receipts") or []),
        "workspace_ve": handlers.worktree_ve_path,
        "workspace_blackdog_path": handlers.blackdog_path,
        "runtime_mode": handlers.runtime_mode,
        "source_mode": handlers.source_mode,
        "script_policy": handlers.script_policy,
        "probes": probes,
    }
    if skill_provenance is not None:
        receipt["skill_provenance"] = skill_provenance
    return receipt


def _auto_task_workset_payload(
    profile: RepoProfile,
    *,
    prompt: str,
    title: str | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    resolved_title = str(title or "").strip() or _derive_task_title(prompt)
    title_slug = slugify(resolved_title) or "task"
    identity_suffix = uuid.uuid4().hex[:8]
    workset_id = f"task-{title_slug}-{identity_suffix}"
    task_id = f"TASK-{identity_suffix.upper()}"
    target_branch = _target_branch_for_current_worktree(profile, repo_root=workspace_root)
    return {
        "id": workset_id,
        "title": resolved_title,
        "scope": {"kind": "repo", "paths": []},
        "visibility": {"kind": "workset"},
        "policies": {"validation": list(profile.validation_commands)},
        "workspace": {
            "identity": workset_id,
            "exported_root": str(profile.paths.project_root),
        },
        "branch_intent": {
            "target_branch": target_branch,
            "integration_branch": target_branch,
        },
        "tasks": [
            {
                "id": task_id,
                "title": resolved_title,
                "intent": resolved_title,
                "description": prompt,
                "docs": list(profile.doc_routing_defaults),
                "checks": list(profile.validation_commands),
                "metadata": {
                    "created_by": "task.begin",
                    "prompt_mode": PROMPT_MODE_RAW,
                },
            }
        ],
        "metadata": {
            "created_by": "task.begin",
            _AUTO_TASK_ENVELOPE_RESERVATION_KEY: {
                "schema_version": AUTO_TASK_ENVELOPE_RESERVATION_SCHEMA_VERSION,
            },
        },
    }


def _task_begin_workset_event_id(workset_id: str) -> str:
    return hashlib.sha256(
        f"blackdog.task.begin.workset-put/v1\0{workset_id}".encode("utf-8")
    ).hexdigest()


def _reserved_auto_task_envelope_payload(
    profile: RepoProfile,
    *,
    workset_id: str | None,
    task_id: str | None,
) -> dict[str, Any] | None:
    planning = load_planning_state(profile.paths)
    workset = find_workset(planning, workset_id)
    if workset is None:
        return None
    task = next((item for item in workset.tasks if item.task_id == task_id), None)
    marker = workset.metadata.get(_AUTO_TASK_ENVELOPE_RESERVATION_KEY)
    if (
        task is None
        or task.metadata.get("created_by") != "task.begin"
        or not isinstance(marker, Mapping)
        or marker.get("schema_version")
        != AUTO_TASK_ENVELOPE_RESERVATION_SCHEMA_VERSION
    ):
        return None
    return workset_to_payload(workset)


def _reserve_auto_task_envelope(
    profile: RepoProfile,
    payload: Mapping[str, Any],
) -> Workset:
    workset_id = str(payload.get("id") or "").strip()
    if not workset_id:
        raise BacklogError("auto task envelope reservation requires a workset id")
    return upsert_workset(
        profile,
        payload,
        event_id=_task_begin_workset_event_id(workset_id),
    )


def _resolve_task_begin_prompts(
    profile: RepoProfile,
    *,
    prompt: str,
    prompt_source: str | None,
    user_prompt: str | None = None,
    user_prompt_source: str | None = None,
    prompt_mode: str,
) -> tuple[PromptReceiptRecord, PromptReceiptRecord]:
    resolved_user_prompt = user_prompt if user_prompt is not None else prompt
    resolved_user_source = user_prompt_source if user_prompt is not None else prompt_source
    user_receipt = create_prompt_receipt(resolved_user_prompt, source=resolved_user_source, mode=PROMPT_MODE_RAW)
    if prompt_mode == PROMPT_MODE_TUNED:
        tuned = tune_prompt(
            profile,
            request=prompt,
            prompt_source=prompt_source,
        )
        execution_receipt = create_prompt_receipt(
            tuned.tuned_prompt,
            source=prompt_source,
            mode=PROMPT_MODE_TUNED,
        )
        return user_receipt, execution_receipt
    if prompt_mode == PROMPT_MODE_SKILL:
        execution_receipt = create_prompt_receipt(prompt, source=prompt_source, mode=PROMPT_MODE_SKILL)
        return user_receipt, execution_receipt
    if user_prompt is not None:
        execution_receipt = create_prompt_receipt(prompt, source=prompt_source, mode=PROMPT_MODE_RAW)
        return user_receipt, execution_receipt
    return user_receipt, user_receipt


def _expected_resume_task_identity(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    predecessor: Any,
) -> tuple[str, str]:
    return resume_predecessor_identity(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        predecessor=predecessor,
    )


def _reserved_resume_guard(
    *,
    profile: RepoProfile,
    runtime_state: Any,
    workset_id: str,
    task_id: str,
    attempt: Any,
    actor: str,
    user_receipt: PromptReceiptRecord,
    execution_receipt: PromptReceiptRecord,
    supplied_expected: Sequence[str | None],
) -> _TaskResumeGuard | None:
    """Recognize one already-reserved ordinary successor for exact repair."""

    setup = attempt.setup_receipt
    atomic = setup.get("atomic_start") if isinstance(setup, Mapping) else None
    if not isinstance(atomic, Mapping) or atomic.get("start_kind") != "resume":
        return None
    if (
        set(atomic) != _ATOMIC_START_RECEIPT_KEYS
        or atomic.get("schema_version") != 2
        or atomic.get("attempt_id") != attempt.attempt_id
        or type(atomic.get("workset_claim_created")) is not bool
        or not isinstance(atomic.get("expected_task_updated_at"), str)
        or not str(atomic["expected_task_updated_at"]).strip()
        or attempt.status != ATTEMPT_STATUS_IN_PROGRESS
        or attempt.ended_at is not None
        or attempt.prompt_receipt is None
        or attempt.user_prompt_receipt is None
    ):
        raise _TaskStartProofConflict(
            "active ordinary resume has malformed deterministic start evidence"
        )
    attempts = tuple(
        row
        for runtime_workset in runtime_state.worksets
        if runtime_workset.workset_id == workset_id
        for row in runtime_workset.attempts
        if row.task_id == task_id
    )
    predecessor_id = str(atomic.get("expected_predecessor_attempt_id") or "").strip()
    if (
        len(attempts) < 2
        or attempts[-1].attempt_id != attempt.attempt_id
        or attempts[-2].attempt_id != predecessor_id
        or attempts[-2].status == ATTEMPT_STATUS_IN_PROGRESS
        or attempts[-2].ended_at is None
    ):
        raise _TaskStartProofConflict(
            "active ordinary resume is not immediately after its terminal predecessor"
        )
    predecessor = attempts[-2]
    if predecessor.prompt_receipt is None or predecessor.user_prompt_receipt is None:
        raise _TaskStartProofConflict(
            "ordinary resume predecessor is missing prompt lineage"
        )
    try:
        expected_actor, expected_generation = _expected_resume_task_identity(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            predecessor=predecessor,
        )
    except BacklogError as exc:
        raise _TaskStartProofConflict(str(exc)) from exc
    durable = (
        expected_actor,
        predecessor.prompt_receipt.prompt_hash,
        predecessor.prompt_receipt.mode,
        predecessor.user_prompt_receipt.prompt_hash,
        predecessor.user_prompt_receipt.mode,
    )
    guarded = (
        str(atomic.get("expected_task_actor") or "").strip(),
        str(atomic.get("expected_execution_prompt_hash") or "").strip(),
        str(atomic.get("expected_execution_prompt_mode") or "").strip(),
        str(atomic.get("expected_request_prompt_hash") or "").strip(),
        str(atomic.get("expected_request_prompt_mode") or "").strip(),
    )
    incoming = (
        actor,
        execution_receipt.prompt_hash,
        execution_receipt.mode,
        user_receipt.prompt_hash,
        user_receipt.mode,
    )
    if durable != guarded or atomic.get("expected_task_updated_at") != expected_generation:
        raise _TaskStartProofConflict(
            "active ordinary resume durable lineage conflicts with its reserved successor"
        )
    if incoming != durable:
        raise BacklogError(
            "task begin actor or prompt lineage does not match the reserved successor"
        )
    if all(value is not None for value in supplied_expected):
        expected = tuple(str(value) for value in supplied_expected)
        if expected != durable:
            raise BacklogError(
                "expected resume lineage no longer matches the reserved successor"
            )
    expected_attempt_id = task_resume_attempt_id(
        workset_id=workset_id,
        task_id=task_id,
        predecessor_attempt_id=predecessor_id,
        actor=expected_actor,
        execution_prompt_hash=predecessor.prompt_receipt.prompt_hash,
        execution_prompt_mode=str(predecessor.prompt_receipt.mode),
        request_prompt_hash=predecessor.user_prompt_receipt.prompt_hash,
        request_prompt_mode=str(predecessor.user_prompt_receipt.mode),
    )
    if attempt.attempt_id != expected_attempt_id:
        raise _TaskStartProofConflict(
            "active ordinary resume has a conflicting deterministic attempt id"
        )
    return _TaskResumeGuard(
        attempt_id=attempt.attempt_id,
        predecessor_attempt_id=predecessor_id,
        task_actor=expected_actor,
        execution_prompt_hash=predecessor.prompt_receipt.prompt_hash,
        execution_prompt_mode=str(predecessor.prompt_receipt.mode),
        execution_prompt_source=attempt.prompt_receipt.source,
        execution_prompt_replay_artifact_path=attempt.prompt_receipt.replay_artifact_path,
        request_prompt_hash=predecessor.user_prompt_receipt.prompt_hash,
        request_prompt_mode=str(predecessor.user_prompt_receipt.mode),
        request_prompt_source=attempt.user_prompt_receipt.source,
        request_prompt_replay_artifact_path=(
            attempt.user_prompt_receipt.replay_artifact_path
        ),
        task_updated_at=expected_generation,
        retry_reserved_successor=True,
    )


def _reserved_initial_start_guard(
    profile: RepoProfile,
    *,
    runtime_state: Any,
    workset_id: str,
    task_id: str,
    attempt: Any,
    actor: str,
    user_receipt: PromptReceiptRecord,
    execution_receipt: PromptReceiptRecord,
    supplied_expected: Sequence[str | None],
) -> _TaskResumeGuard | None:
    state, issue = _initial_start_evidence(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        runtime_state=runtime_state,
        successor=attempt,
    )
    if state is None:
        return None
    if state == "conflict":
        raise _TaskStartProofConflict(
            issue or "reserved initial task start evidence conflicts"
        )
    assert attempt.prompt_receipt is not None
    assert attempt.user_prompt_receipt is not None
    durable = (
        attempt.actor,
        attempt.prompt_receipt.prompt_hash,
        attempt.prompt_receipt.mode,
        attempt.user_prompt_receipt.prompt_hash,
        attempt.user_prompt_receipt.mode,
    )
    incoming = (
        actor,
        execution_receipt.prompt_hash,
        execution_receipt.mode,
        user_receipt.prompt_hash,
        user_receipt.mode,
    )
    if incoming != durable:
        raise BacklogError(
            "reserved initial task start actor or prompt lineage conflicts"
        )
    if all(value is not None for value in supplied_expected):
        expected = tuple(str(value) for value in supplied_expected)
        if expected != durable:
            raise BacklogError(
                "expected start lineage no longer matches the reserved initial attempt"
            )
    return _TaskResumeGuard(
        attempt_id=attempt.attempt_id,
        predecessor_attempt_id="",
        task_actor=attempt.actor,
        execution_prompt_hash=attempt.prompt_receipt.prompt_hash,
        execution_prompt_mode=str(attempt.prompt_receipt.mode),
        execution_prompt_source=attempt.prompt_receipt.source,
        execution_prompt_replay_artifact_path=attempt.prompt_receipt.replay_artifact_path,
        request_prompt_hash=attempt.user_prompt_receipt.prompt_hash,
        request_prompt_mode=str(attempt.user_prompt_receipt.mode),
        request_prompt_source=attempt.user_prompt_receipt.source,
        request_prompt_replay_artifact_path=(
            attempt.user_prompt_receipt.replay_artifact_path
        ),
        task_updated_at=attempt.started_at,
        retry_reserved_successor=True,
        start_kind="initial",
    )


def _validate_existing_task_resume_lineage(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    user_receipt: PromptReceiptRecord,
    execution_receipt: PromptReceiptRecord,
    expected_actor: str | None,
    expected_execution_prompt_hash: str | None,
    expected_execution_prompt_mode: str | None,
    expected_request_prompt_hash: str | None,
    expected_request_prompt_mode: str | None,
) -> _TaskResumeGuard | None:
    """Bind an existing-envelope begin to its durable owner and prompt lineage."""
    expected_values = (
        expected_actor,
        expected_execution_prompt_hash,
        expected_execution_prompt_mode,
        expected_request_prompt_hash,
        expected_request_prompt_mode,
    )
    supplied_expected = [str(value or "").strip() or None for value in expected_values]
    if any(value is not None for value in supplied_expected) and any(
        value is None for value in supplied_expected
    ):
        raise BacklogError(
            "existing-envelope task begin requires all expected actor and prompt-lineage fields together"
        )

    runtime_state = load_runtime_state(profile.paths)
    prior_attempt = latest_task_attempt(runtime_state, workset_id, task_id)
    if prior_attempt is None:
        if any(value is not None for value in supplied_expected):
            raise BacklogError("expected resume lineage was supplied for a task with no prior attempt")
        return None
    if prior_attempt.ended_at is None or prior_attempt.status == ATTEMPT_STATUS_IN_PROGRESS:
        retry_guard = _reserved_resume_guard(
            profile=profile,
            runtime_state=runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            attempt=prior_attempt,
            actor=actor,
            user_receipt=user_receipt,
            execution_receipt=execution_receipt,
            supplied_expected=supplied_expected,
        )
        if retry_guard is not None:
            return retry_guard
        initial_guard = _reserved_initial_start_guard(
            profile,
            runtime_state=runtime_state,
            workset_id=workset_id,
            task_id=task_id,
            attempt=prior_attempt,
            actor=actor,
            user_receipt=user_receipt,
            execution_receipt=execution_receipt,
            supplied_expected=supplied_expected,
        )
        if initial_guard is not None:
            return initial_guard
        raise BacklogError("task begin cannot resume an envelope whose latest attempt is still active")
    if prior_attempt.prompt_receipt is None or prior_attempt.user_prompt_receipt is None:
        raise BacklogError("latest terminal attempt is missing durable execution or request prompt lineage")
    execution_prompt_mode = str(prior_attempt.prompt_receipt.mode or "").strip()
    request_prompt_mode = str(prior_attempt.user_prompt_receipt.mode or "").strip()
    if not execution_prompt_mode or not request_prompt_mode:
        raise BacklogError("latest terminal attempt is missing durable execution or request prompt modes")

    task_state = task_state_index(runtime_state, workset_id).get(task_id)
    if (
        task_state is None
        or task_state.status not in {TASK_STATUS_PLANNED, TASK_STATUS_BLOCKED}
        or not str(task_state.updated_at or "").strip()
    ):
        raise BacklogError("latest terminal task state is not durably restartable")
    try:
        durable_actor, durable_generation = _expected_resume_task_identity(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            predecessor=prior_attempt,
        )
    except BacklogError as exc:
        raise _TaskStartProofConflict(str(exc)) from exc
    task_state_actor = str(task_state.actor or "").strip()
    if (
        (task_state_actor and task_state_actor != durable_actor)
        or task_state.updated_at != durable_generation
    ):
        raise _TaskStartProofConflict(
            "ordinary resume task state conflicts with its terminal/reopen evidence"
        )
    if actor != durable_actor:
        raise BacklogError(
            f"existing-envelope task begin actor {actor!r} does not match durable task actor {durable_actor!r}"
        )

    durable_lineage = (
        prior_attempt.prompt_receipt.prompt_hash,
        prior_attempt.prompt_receipt.mode,
        prior_attempt.user_prompt_receipt.prompt_hash,
        prior_attempt.user_prompt_receipt.mode,
    )
    incoming_lineage = (
        execution_receipt.prompt_hash,
        execution_receipt.mode,
        user_receipt.prompt_hash,
        user_receipt.mode,
    )
    if incoming_lineage != durable_lineage:
        raise BacklogError(
            "existing-envelope task begin prompt lineage does not match the latest terminal attempt"
        )
    if all(value is not None for value in supplied_expected):
        expected_lineage = (
            supplied_expected[1],
            supplied_expected[2],
            supplied_expected[3],
            supplied_expected[4],
        )
        if supplied_expected[0] != durable_actor or expected_lineage != durable_lineage:
            raise BacklogError("expected resume lineage no longer matches durable task state")
    attempt_id = task_resume_attempt_id(
        workset_id=workset_id,
        task_id=task_id,
        predecessor_attempt_id=prior_attempt.attempt_id,
        actor=durable_actor,
        execution_prompt_hash=prior_attempt.prompt_receipt.prompt_hash,
        execution_prompt_mode=execution_prompt_mode,
        request_prompt_hash=prior_attempt.user_prompt_receipt.prompt_hash,
        request_prompt_mode=request_prompt_mode,
    )
    return _TaskResumeGuard(
        attempt_id=attempt_id,
        predecessor_attempt_id=prior_attempt.attempt_id,
        task_actor=durable_actor,
        execution_prompt_hash=prior_attempt.prompt_receipt.prompt_hash,
        execution_prompt_mode=execution_prompt_mode,
        execution_prompt_source=prior_attempt.prompt_receipt.source,
        execution_prompt_replay_artifact_path=(
            prior_attempt.prompt_receipt.replay_artifact_path
        ),
        request_prompt_hash=prior_attempt.user_prompt_receipt.prompt_hash,
        request_prompt_mode=request_prompt_mode,
        request_prompt_source=prior_attempt.user_prompt_receipt.source,
        request_prompt_replay_artifact_path=(
            prior_attempt.user_prompt_receipt.replay_artifact_path
        ),
        task_updated_at=durable_generation,
    )


def _verify_recorded_prompt_artifact(
    profile: RepoProfile,
    *,
    role: str,
    prompt_hash: str,
    replay_artifact_path: str | None,
) -> None:
    if replay_artifact_path is None:
        return
    try:
        verify_prompt_artifact(
            profile.paths.control_dir,
            prompt_hash=prompt_hash,
            replay_artifact_path=replay_artifact_path,
        )
    except PromptArtifactError as exc:
        raise BacklogError(
            f"recorded {role} prompt replay lineage is unavailable ({exc.code}): {exc}"
        ) from exc


def _attempt_matches_workspace(profile: RepoProfile, *, workspace_root: Path, attempt: Any) -> bool:
    if attempt.worktree_path and Path(attempt.worktree_path).resolve() == workspace_root:
        return True
    if attempt.branch:
        existing = find_worktree_for_branch(profile, attempt.branch)
        if existing and Path(existing).resolve() == workspace_root:
            return True
    return False


def _attempt_sort_key(attempt: Any) -> float:
    ended = parse_iso(attempt.ended_at)
    if ended is not None:
        return ended.timestamp()
    started = parse_iso(attempt.started_at)
    if started is not None:
        return started.timestamp()
    return 0.0


def _resolve_task_command_target(
    profile: RepoProfile,
    *,
    workset_id: str | None = None,
    task_id: str | None = None,
    cwd: Path | None = None,
    allow_latest: bool = False,
    allow_landing_transaction: bool = False,
) -> tuple[str, str, Any | None]:
    resolved_workset = str(workset_id or "").strip() or None
    resolved_task = str(task_id or "").strip() or None
    runtime_state = load_runtime_state(profile.paths)
    if (resolved_workset is None) != (resolved_task is None):
        raise BacklogError("provide both --workset and --task, or neither when running inside a task worktree")
    if resolved_workset is not None and resolved_task is not None:
        attempt = active_task_attempt(runtime_state, resolved_workset, resolved_task)
        if attempt is None and allow_landing_transaction:
            latest = latest_task_attempt(runtime_state, resolved_workset, resolved_task)
            if latest is not None and load_landing_transaction(
                profile,
                workset_id=resolved_workset,
                task_id=resolved_task,
                attempt_id=latest.attempt_id,
            ) is not None:
                attempt = latest
        if attempt is None and allow_latest:
            attempt = latest_task_attempt(runtime_state, resolved_workset, resolved_task)
        if attempt is None and allow_latest:
            return resolved_workset, resolved_task, None
        if attempt is None and not allow_landing_transaction:
            raise BacklogError(f"No active WTAM attempt for task {resolved_task!r} in workset {resolved_workset!r}")
        if attempt is None:
            raise BacklogError(
                f"No eligible WTAM attempt for task {resolved_task!r} in workset {resolved_workset!r}"
            )
        return resolved_workset, resolved_task, attempt

    workspace_root = _repo_root((cwd or Path.cwd()).resolve())
    active_matches: list[tuple[str, Any]] = []
    latest_matches: list[tuple[str, Any]] = []
    for workset in runtime_state.worksets:
        for attempt in workset.attempts:
            if _attempt_matches_workspace(profile, workspace_root=workspace_root, attempt=attempt):
                latest_matches.append((workset.workset_id, attempt))
                if attempt.status == "in_progress":
                    active_matches.append((workset.workset_id, attempt))
    if len(active_matches) > 1:
        raise WorktreeError(f"multiple active task attempts are associated with {workspace_root}; specify --workset and --task")
    if active_matches:
        workset, attempt = active_matches[0]
        return workset, attempt.task_id, attempt
    if allow_latest and latest_matches:
        workset, attempt = max(latest_matches, key=lambda item: _attempt_sort_key(item[1]))
        return workset, attempt.task_id, attempt
    raise WorktreeError(
        f"could not infer a Blackdog task from {workspace_root}; run from a task worktree or provide both "
        "--workset and --task for an existing task. For new work, use `blackdog task begin` without "
        "--workset/--task."
    )


def _run_command(*args: str, cwd: Path | None = None) -> None:
    completed = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        rendered = " ".join(args)
        raise WorktreeError(f"{rendered} failed: {detail}")


def preview_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    prompt: str,
    prompt_source: str | None = None,
    prompt_mode: str = PROMPT_MODE_RAW,
    model: str | None = None,
    reasoning_effort: str | None = None,
    branch: str | None = None,
    from_ref: str | None = None,
    path: str | None = None,
    cwd: Path | None = None,
    note: str | None = None,
    include_prompt: bool = False,
    expand_contract: bool = False,
) -> WorktreePreview:
    workset, task = _require_workset_and_task(profile, workset_id=workset_id, task_id=task_id)
    current_root = _repo_root(command_workspace_root(profile, cwd=cwd))
    primary_root = find_primary_worktree(profile.paths.project_root)
    target_branch = str(workset.branch_intent.get("target_branch") or "").strip() or _current_branch(primary_root)
    base_ref = _resolve_from_ref(primary_root, from_ref, default_branch=target_branch)
    base_commit = _run_git(primary_root, "rev-parse", f"{base_ref}^{{commit}}")
    resolved_branch = branch or default_task_branch(workset_id, task)
    integration_branch = (
        str(workset.branch_intent.get("integration_branch") or resolved_branch).strip() or resolved_branch
    )
    worktree_path = Path(path).resolve() if path else default_task_worktree_path(profile, workset_id=workset_id, task=task).resolve()
    existing_worktree = _find_worktree_for_branch(primary_root, f"refs/heads/{resolved_branch}")
    prompt_receipt = create_prompt_receipt(prompt, source=prompt_source, mode=prompt_mode)
    conflicts: list[str] = []
    if existing_worktree is not None:
        conflicts.append(f"branch already has a worktree: {existing_worktree}")
    if _is_within(primary_root, worktree_path):
        conflicts.append(f"refusing worktree path inside the repository: {worktree_path}")
    elif worktree_path.exists():
        conflicts.append(f"worktree path already exists: {worktree_path}")
    handlers = plan_worktree_handlers(profile, worktree_path=worktree_path)
    if not handlers.ready:
        blocked = [action.message for action in handlers.actions if action.status == "blocked"]
        conflicts.extend(blocked)
        if handlers.remediation and handlers.remediation not in conflicts:
            conflicts.append(handlers.remediation)
    return WorktreePreview(
        workset_id=workset_id,
        task_id=task.task_id,
        task_title=task.title,
        task_slug=_task_slug(workset_id, task),
        actor=actor,
        execution_model="direct_wtam",
        workspace_identity=str(workset.workspace.get("identity") or "").strip() or None,
        branch=resolved_branch,
        base_ref=base_ref,
        base_commit=base_commit,
        target_branch=target_branch,
        integration_branch=integration_branch,
        worktree_path=str(worktree_path),
        primary_worktree=str(primary_root),
        current_worktree=str(current_root),
        model=model,
        reasoning_effort=reasoning_effort,
        note=note,
        prompt_hash=prompt_receipt.prompt_hash,
        prompt_source=prompt_receipt.source,
        prompt_mode=prompt_receipt.mode,
        prompt_text=prompt_receipt.text if include_prompt else None,
        task_paths=task.paths,
        task_docs=task.docs,
        task_checks=task.checks,
        validation_commands=profile.validation_commands,
        doc_routing_defaults=profile.doc_routing_defaults,
        contract_documents=contract_documents(
            profile,
            expand_skill_text=expand_contract,
            expand_doc_text=expand_contract,
        ),
        handlers=handlers,
        existing_branch_worktree=str(existing_worktree) if existing_worktree is not None else None,
        path_exists=worktree_path.exists(),
        start_ready=not conflicts and handlers.ready,
        conflicts=tuple(conflicts),
    )


def default_task_branch(workset_id: str, task: TaskSpec) -> str:
    return f"agent/{_task_slug(workset_id, task)}"


def default_task_worktree_path(profile: RepoProfile, *, workset_id: str, task: TaskSpec) -> Path:
    return profile.paths.worktrees_dir / f"wt-{_task_slug(workset_id, task)}"


def _resolve_from_ref(primary_root: Path, from_ref: str | None, *, default_branch: str) -> str:
    if not from_ref:
        return default_branch
    if _run_git_no_check(primary_root, "rev-parse", "--verify", f"{from_ref}^{{commit}}").returncode == 0:
        return from_ref
    remote_ref = f"origin/{from_ref}"
    if _run_git_no_check(primary_root, "rev-parse", "--verify", f"{remote_ref}^{{commit}}").returncode == 0:
        return remote_ref
    raise WorktreeError(f"could not resolve --from ref: {from_ref} (try: git fetch --all --prune)")


def _is_task_branch(profile: RepoProfile, branch: str | None) -> bool:
    resolved = str(branch or "").strip()
    if not resolved:
        return False
    planning_state = load_planning_state(profile.paths)
    for workset in planning_state.worksets:
        for task in workset.tasks:
            if resolved == default_task_branch(workset.workset_id, task):
                return True
    return False


def _target_branch_for_current_worktree(profile: RepoProfile, *, repo_root: Path | None = None) -> str:
    current_root = _repo_root(repo_root or profile.paths.project_root)
    primary_root = find_primary_worktree(profile.paths.project_root)
    primary_branch = _current_branch(primary_root)
    current_branch = _current_branch(current_root)
    if not _is_primary_worktree(current_root) and not _is_task_branch(profile, current_branch):
        return current_branch
    return primary_branch


def worktree_contract(
    profile: RepoProfile,
    *,
    workspace: Path | None = None,
    workspace_mode: str | None = None,
) -> dict[str, Any]:
    resolved_workspace = _repo_root(workspace or profile.paths.project_root)
    primary_root = find_primary_worktree(profile.paths.project_root)
    current_is_primary = _is_primary_worktree(resolved_workspace)
    workspace_blackdog = resolved_workspace / ".VE" / "bin" / "blackdog"
    workspace_has_local_blackdog = workspace_blackdog.is_file() and os.access(workspace_blackdog, os.X_OK)
    primary_branch = _current_branch(primary_root)
    current_branch = _run_git(resolved_workspace, "rev-parse", "--abbrev-ref", "HEAD")
    workspace_role = WORKTREE_ROLE_PRIMARY if current_is_primary else (
        WORKTREE_ROLE_TASK if _is_task_branch(profile, current_branch) else WORKTREE_ROLE_LINKED
    )
    target_branch = current_branch if workspace_role == WORKTREE_ROLE_LINKED else primary_branch
    return {
        "workspace_mode": workspace_mode or WORKSPACE_MODE_GIT_WORKTREE,
        "current_worktree": str(resolved_workspace),
        "current_branch": current_branch,
        "current_is_primary": current_is_primary,
        "workspace_role": workspace_role,
        "primary_worktree": str(primary_root),
        "primary_branch": primary_branch,
        "target_branch": target_branch,
        "primary_dirty": _managed_status_dirty(profile, primary_root),
        "primary_dirty_paths": _managed_dirty_paths(profile, primary_root),
        "workspace_ve": str(resolved_workspace / ".VE"),
        "workspace_blackdog_path": str(workspace_blackdog),
        "workspace_has_local_blackdog": workspace_has_local_blackdog,
        "ve_expectation": WTAM_WORKTREE_VE_NOTE,
    }


def worktree_preflight(profile: RepoProfile, *, cwd: Path | None = None) -> dict[str, Any]:
    resolved_cwd = command_workspace_root(profile, cwd=cwd).resolve()
    current_root = _repo_root(resolved_cwd)
    contract = worktree_contract(profile, workspace=current_root)
    primary_root = Path(contract["primary_worktree"]).resolve()
    configured_worktrees_dir = profile.paths.worktrees_dir.resolve()
    worktrees = []
    for row in _parse_worktree_list(profile.paths.project_root):
        path = Path(str(row.get("worktree") or "")).resolve()
        branch = str(row.get("branch") or "")
        worktrees.append(
            {
                "path": str(path),
                "branch": branch.removeprefix("refs/heads/") if branch else "",
                "is_primary": _is_primary_worktree(path),
            }
        )
    return {
        "project_root": str(profile.paths.project_root),
        "repo_root": str(current_root),
        "cwd": str(resolved_cwd),
        "current_worktree": contract["current_worktree"],
        "current_branch": contract["current_branch"],
        "current_is_primary": contract["current_is_primary"],
        "workspace_role": contract["workspace_role"],
        "primary_worktree": contract["primary_worktree"],
        "primary_branch": contract["primary_branch"],
        "dirty": _status_dirty(current_root),
        "implementation_dirty": _managed_status_dirty(profile, current_root),
        "workspace_mode": contract["workspace_mode"],
        "target_branch": contract["target_branch"],
        "primary_dirty": contract["primary_dirty"],
        "landing_state": "blocked" if contract["primary_dirty"] else "ready",
        "primary_dirty_paths": contract["primary_dirty_paths"],
        "current_worktree_ve": contract["workspace_ve"],
        "current_worktree_blackdog_path": contract["workspace_blackdog_path"],
        "current_worktree_has_local_blackdog": contract["workspace_has_local_blackdog"],
        "ve_expectation": contract["ve_expectation"],
        "workspace_contract": contract,
        "worktrees_dir": str(configured_worktrees_dir),
        "worktrees_dir_inside_repo": _is_within(primary_root, configured_worktrees_dir),
        "worktrees": worktrees,
    }


def primary_worktree_is_dirty(profile: RepoProfile, *, ignore_runtime: bool = True) -> bool:
    primary_root = find_primary_worktree(profile.paths.project_root)
    if ignore_runtime:
        return _managed_status_dirty(profile, primary_root)
    return _status_dirty(primary_root)


def primary_worktree_dirty_paths(profile: RepoProfile, *, ignore_runtime: bool = True) -> list[str]:
    primary_root = find_primary_worktree(profile.paths.project_root)
    if ignore_runtime:
        return _managed_dirty_paths(profile, primary_root)
    return dirty_paths(primary_root)


def dirty_primary_worktree_error(profile: RepoProfile, *, branch: str, target_branch: str | None = None) -> DirtyPrimaryWorktreeError:
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    return DirtyPrimaryWorktreeError(
        primary_worktree=primary_root,
        branch=branch,
        target_branch=resolved_target,
        dirty_paths=primary_worktree_dirty_paths(profile, ignore_runtime=True),
    )


def branch_changed_paths(
    profile: RepoProfile,
    *,
    branch: str,
    target_branch: str | None = None,
    primary_root: Path | None = None,
    changed_paths_cache: dict[tuple[str, str | None], list[str]] | None = None,
) -> list[str]:
    cache_key = (branch, target_branch)
    if changed_paths_cache is not None and cache_key in changed_paths_cache:
        return list(changed_paths_cache[cache_key])
    primary_root = primary_root or find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    completed = _run_git_no_check(primary_root, "diff", "--name-only", f"{resolved_target}..{branch}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git diff --name-only {resolved_target}..{branch} failed: {detail}")
    changed = sorted({line.strip() for line in completed.stdout.splitlines() if line.strip()})
    if changed_paths_cache is not None:
        changed_paths_cache[cache_key] = changed
    return changed


def branch_ahead_of_target(profile: RepoProfile, *, branch: str, target_branch: str | None = None) -> bool:
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    completed = _run_git_no_check(primary_root, "rev-list", "--count", f"{resolved_target}..{branch}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git rev-list --count {resolved_target}..{branch} failed: {detail}")
    return int(completed.stdout.strip() or "0") > 0


def _inspect_branch_ref(
    repo_root: Path,
    ref: str | None,
    *,
    role: str,
    ref_cache: dict[str, str | None] | None = None,
) -> GitReferenceInspection:
    if not ref:
        return GitReferenceInspection(
            role=role,
            ref=None,
            state="metadata_missing",
            detail=f"{role.replace('_', ' ')} metadata is missing",
        )
    cache_key = f"refs/heads/{ref}"
    if ref_cache is not None and cache_key in ref_cache:
        cached = ref_cache[cache_key]
        return GitReferenceInspection(
            role=role,
            ref=ref,
            state="exists" if cached is not None else "missing",
            command=("git", "show-ref", "--hash", cache_key),
            return_code=0 if cached is not None else 1,
            resolved_commit=cached,
        )
    # ``show-ref`` without ``--verify`` has the stable seam we need here:
    # 0 means a matching ref exists, 1 means no match, and every other code is
    # an inspection failure.  ``--verify`` collapses a missing ref into 128 on
    # supported Apple Git versions and would make absence indistinguishable
    # from malformed input or repository errors.
    command = ("git", "show-ref", "--hash", cache_key)
    completed = _run_git_no_check(repo_root, *command[1:])
    if completed.returncode == 0:
        resolved = completed.stdout.strip() or None
        if resolved is None:
            return GitReferenceInspection(
                role=role,
                ref=ref,
                state="error",
                command=command,
                return_code=completed.returncode,
                detail=f"git show-ref returned success without a commit for {cache_key}",
            )
        if ref_cache is not None:
            ref_cache[cache_key] = resolved
        return GitReferenceInspection(
            role=role,
            ref=ref,
            state="exists",
            command=command,
            return_code=completed.returncode,
            resolved_commit=resolved,
        )
    if completed.returncode == 1:
        if ref_cache is not None:
            ref_cache[cache_key] = None
        return GitReferenceInspection(
            role=role,
            ref=ref,
            state="missing",
            command=command,
            return_code=completed.returncode,
            detail=f"local branch ref {cache_key} is missing",
        )
    detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
    return GitReferenceInspection(
        role=role,
        ref=ref,
        state="error",
        command=command,
        return_code=completed.returncode,
        detail=f"git show-ref inspection failed for {cache_key}: {detail}",
    )


def _recovery_branch_state(
    profile: RepoProfile,
    *,
    branch: str | None,
    target_branch: str | None,
    primary_root: Path | None = None,
    ref_cache: dict[str, str | None] | None = None,
    branch_ahead_cache: dict[tuple[str, str], tuple[bool, str | None]] | None = None,
) -> tuple[
    bool,
    GitReferenceInspection,
    GitReferenceInspection,
    str | None,
    str | None,
]:
    primary_root = primary_root or find_primary_worktree(profile.paths.project_root)
    branch_inspection = _inspect_branch_ref(
        primary_root,
        branch,
        role="task_branch",
        ref_cache=ref_cache,
    )
    target_inspection = _inspect_branch_ref(
        primary_root,
        target_branch,
        role="target_branch",
        ref_cache=ref_cache,
    )
    if branch_inspection.state == "metadata_missing":
        return False, branch_inspection, target_inspection, "task_branch_metadata_missing", branch_inspection.detail
    if target_inspection.state == "metadata_missing":
        return False, branch_inspection, target_inspection, "target_branch_metadata_missing", target_inspection.detail
    if branch_inspection.state == "error":
        return False, branch_inspection, target_inspection, "task_branch_inspection_failed", branch_inspection.detail
    if target_inspection.state == "error":
        return False, branch_inspection, target_inspection, "target_branch_inspection_failed", target_inspection.detail
    if branch_inspection.state == "missing":
        return (
            False,
            branch_inspection,
            target_inspection,
            "task_branch_missing",
            f"task branch {branch!r} is missing",
        )
    if target_inspection.state == "missing":
        return (
            False,
            branch_inspection,
            target_inspection,
            "target_branch_missing",
            f"target branch {target_branch!r} is missing",
        )
    if branch is None or target_branch is None:
        raise AssertionError("existing branch inspections require branch metadata")
    cache_key = (target_branch, branch)
    if branch_ahead_cache is not None and cache_key in branch_ahead_cache:
        cached_ahead, cached_error = branch_ahead_cache[cache_key]
        return (
            cached_ahead,
            branch_inspection,
            target_inspection,
            "branch_relationship_inspection_failed" if cached_error else None,
            cached_error,
        )
    completed = _run_git_no_check(primary_root, "rev-list", "--count", f"{target_branch}..{branch}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        error = f"git rev-list --count {target_branch}..{branch} failed: {detail}"
        if branch_ahead_cache is not None:
            branch_ahead_cache[cache_key] = (False, error)
        return (
            False,
            branch_inspection,
            target_inspection,
            "branch_relationship_inspection_failed",
            error,
        )
    try:
        ahead = int(completed.stdout.strip() or "0") > 0
    except ValueError:
        error = f"git rev-list --count returned a non-integer result for {target_branch}..{branch}"
        if branch_ahead_cache is not None:
            branch_ahead_cache[cache_key] = (False, error)
        return (
            False,
            branch_inspection,
            target_inspection,
            "branch_relationship_inspection_failed",
            error,
        )
    if branch_ahead_cache is not None:
        branch_ahead_cache[cache_key] = (ahead, None)
    return ahead, branch_inspection, target_inspection, None, None


def _inspect_commit(repo_root: Path, ref: str | None, *, role: str) -> GitReferenceInspection:
    """Resolve a commit without collapsing Git failures into missing evidence."""
    if not ref:
        return GitReferenceInspection(
            role=role,
            ref=None,
            state="metadata_missing",
            detail=f"{role} metadata is missing",
        )
    # ``--quiet`` gives this proof seam the distinction Git documents for
    # verification: an absent object/ref returns 1 without diagnostics, while
    # malformed input and operational repository failures remain non-1 errors.
    command = ("git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    completed = _run_git_no_check(repo_root, *command[1:])
    if completed.returncode == 0:
        resolved = completed.stdout.strip() or None
        if resolved is None:
            return GitReferenceInspection(
                role=role,
                ref=ref,
                state="error",
                command=command,
                return_code=completed.returncode,
                detail=f"git rev-parse returned success without a commit for {ref!r}",
            )
        return GitReferenceInspection(
            role=role,
            ref=ref,
            state="exists",
            command=command,
            return_code=completed.returncode,
            resolved_commit=resolved,
        )
    detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
    if completed.returncode == 1:
        return GitReferenceInspection(
            role=role,
            ref=ref,
            state="missing",
            command=command,
            return_code=completed.returncode,
            detail=f"commit {ref!r} is missing: {detail}",
        )
    return GitReferenceInspection(
        role=role,
        ref=ref,
        state="error",
        command=command,
        return_code=completed.returncode,
        detail=f"git commit inspection failed for {ref!r}: {detail}",
    )


def _inspection_error(inspection: GitReferenceInspection) -> WorktreeError:
    evidence = inspection.to_dict()
    return WorktreeError(
        f"{inspection.role} proof is unavailable: {inspection.detail}; evidence={evidence!r}"
    )


def _can_force_delete_landed_task_branch(
    primary_root: Path,
    *,
    branch: str,
    branch_tip: str,
    latest_attempt: TaskRuntimeRecord | None,
    ref_cache: dict[str, str | None] | None = None,
) -> tuple[bool, str]:
    if latest_attempt is None:
        return False, "no runtime attempt metadata"
    if latest_attempt.branch != branch:
        return False, "latest attempt branch does not match the cleanup branch"
    if latest_attempt.status != ATTEMPT_STATUS_SUCCESS:
        return False, f"latest attempt status is {latest_attempt.status!r}, not success"
    if not latest_attempt.commit:
        return False, "latest attempt is missing the recorded task-branch commit"
    if not latest_attempt.landed_commit:
        return False, "latest attempt is missing the canonical landed commit"
    if not latest_attempt.target_branch:
        return False, "latest attempt is missing the target branch"

    recorded_inspection = _inspect_commit(
        primary_root,
        latest_attempt.commit,
        role="recorded_task_commit",
    )
    if recorded_inspection.state == "error":
        raise _inspection_error(recorded_inspection)
    recorded_commit = recorded_inspection.resolved_commit
    if recorded_commit is None:
        return False, f"recorded task-branch commit {latest_attempt.commit} is missing"
    if recorded_commit != branch_tip:
        return False, "branch tip changed after the recorded landed attempt"

    landed_inspection = _inspect_commit(
        primary_root,
        latest_attempt.landed_commit,
        role="landed_commit",
    )
    if landed_inspection.state == "error":
        raise _inspection_error(landed_inspection)
    landed_commit = landed_inspection.resolved_commit
    if landed_commit is None:
        return False, f"landed commit {latest_attempt.landed_commit} is missing"
    target_inspection = _inspect_branch_ref(
        primary_root,
        latest_attempt.target_branch,
        role="target_branch",
        ref_cache=ref_cache,
    )
    if target_inspection.state == "error":
        raise _inspection_error(target_inspection)
    if target_inspection.state != "exists":
        return False, f"target branch {latest_attempt.target_branch} is missing"

    landed_reachable = _run_git_no_check(
        primary_root,
        "merge-base",
        "--is-ancestor",
        landed_commit,
        latest_attempt.target_branch,
    )
    if landed_reachable.returncode == 1:
        return False, f"landed commit {landed_commit[:12]} is not reachable from {latest_attempt.target_branch}"
    if landed_reachable.returncode != 0:
        detail = landed_reachable.stderr.strip() or landed_reachable.stdout.strip() or f"exit code {landed_reachable.returncode}"
        raise WorktreeError(f"could not inspect landed-commit reachability: {detail}")

    same_tree = _run_git_no_check(primary_root, "diff", "--quiet", branch_tip, landed_commit)
    if same_tree.returncode == 1:
        return False, "recorded task-branch tree differs from the landed commit"
    if same_tree.returncode != 0:
        detail = same_tree.stderr.strip() or same_tree.stdout.strip() or f"exit code {same_tree.returncode}"
        raise WorktreeError(f"could not compare task and landed commit trees: {detail}")

    return True, "recorded task branch is patch-equivalent to the canonical landed commit"


def _can_force_delete_terminal_patch_equivalent_branch(
    primary_root: Path,
    *,
    branch: str,
    latest_attempt: TaskRuntimeRecord | None,
    ref_cache: dict[str, str | None] | None = None,
) -> tuple[bool, str]:
    if latest_attempt is None:
        return False, "no runtime attempt metadata"
    if latest_attempt.branch != branch:
        return False, "latest attempt branch does not match the cleanup branch"
    if latest_attempt.status not in {
        ATTEMPT_STATUS_SUCCESS,
        ATTEMPT_STATUS_BLOCKED,
        ATTEMPT_STATUS_FAILED,
        ATTEMPT_STATUS_ABANDONED,
    }:
        return False, f"latest attempt status {latest_attempt.status!r} is not terminal"
    if not latest_attempt.target_branch:
        return False, "latest attempt is missing the target branch"
    target_inspection = _inspect_branch_ref(
        primary_root,
        latest_attempt.target_branch,
        role="target_branch",
        ref_cache=ref_cache,
    )
    if target_inspection.state == "error":
        raise _inspection_error(target_inspection)
    if target_inspection.state != "exists":
        return False, f"target branch {latest_attempt.target_branch!r} is missing"

    merge_commits = _run_git_no_check(
        primary_root,
        "rev-list",
        "--merges",
        f"{latest_attempt.target_branch}..{branch}",
    )
    if merge_commits.returncode != 0:
        detail = merge_commits.stderr.strip() or merge_commits.stdout.strip() or f"exit code {merge_commits.returncode}"
        return False, f"could not inspect task-branch merge commits: {detail}"
    if merge_commits.stdout.strip():
        return False, "task branch contains merge commits that cannot be proven by patch equivalence"

    cherry = _run_git_no_check(primary_root, "cherry", latest_attempt.target_branch, branch)
    if cherry.returncode != 0:
        detail = cherry.stderr.strip() or cherry.stdout.strip() or f"exit code {cherry.returncode}"
        return False, f"git cherry patch-equivalence check failed: {detail}"
    patch_rows = [line.strip() for line in cherry.stdout.splitlines() if line.strip()]
    if not patch_rows:
        return False, "task branch has no independently verifiable patch-equivalence rows"
    unlanded_rows = [line for line in patch_rows if not line.startswith("- ")]
    if unlanded_rows:
        return False, f"{len(unlanded_rows)} task-branch patch(es) are not represented on {latest_attempt.target_branch}"
    return True, f"all terminal task-branch patches are equivalent to patches on {latest_attempt.target_branch}"


def _plan_task_branch_cleanup(
    primary_root: Path,
    *,
    branch: str,
    latest_attempt: TaskRuntimeRecord | None,
    ref_cache: dict[str, str | None] | None = None,
) -> _BranchCleanupPlan:
    branch_inspection = _inspect_branch_ref(
        primary_root,
        branch,
        role="task_branch",
        ref_cache=ref_cache,
    )
    if branch_inspection.state == "error":
        raise _inspection_error(branch_inspection)
    branch_tip = branch_inspection.resolved_commit
    if branch_inspection.state == "missing":
        return _BranchCleanupPlan(
            branch_exists=False,
            force_delete=False,
            branch_tip=None,
            reason="branch already absent",
            proof_state="branch_absent",
        )

    target_branch = latest_attempt.target_branch if latest_attempt and latest_attempt.target_branch else _current_branch(primary_root)
    if branch_tip is None:
        raise WorktreeError(f"task branch {branch!r} resolved without a commit")
    target_inspection = _inspect_branch_ref(
        primary_root,
        target_branch,
        role="target_branch",
        ref_cache=ref_cache,
    )
    if target_inspection.state == "error":
        raise _inspection_error(target_inspection)
    target_commit = target_inspection.resolved_commit
    if target_inspection.state == "exists" and target_commit is not None:
        if branch_tip == target_commit:
            return _BranchCleanupPlan(
                branch_exists=True,
                force_delete=False,
                branch_tip=branch_tip,
                reason=f"branch has no commits ahead of {target_branch}",
                proof_state="no_ahead",
            )
        merged = _run_git_no_check(primary_root, "merge-base", "--is-ancestor", branch_tip, target_branch)
        if merged.returncode == 0:
            return _BranchCleanupPlan(
                branch_exists=True,
                force_delete=False,
                branch_tip=branch_tip,
                reason=f"branch is already merged into {target_branch}",
                proof_state="contained",
            )
        if merged.returncode != 1:
            detail = merged.stderr.strip() or merged.stdout.strip() or f"exit code {merged.returncode}"
            raise WorktreeError(f"could not inspect whether task branch is contained in target: {detail}")

    can_force_delete, reason = _can_force_delete_landed_task_branch(
        primary_root,
        branch=branch,
        branch_tip=branch_tip,
        latest_attempt=latest_attempt,
        ref_cache=ref_cache,
    )
    if can_force_delete:
        return _BranchCleanupPlan(
            branch_exists=True,
            force_delete=True,
            branch_tip=branch_tip,
            reason=reason,
            proof_state="patch_equivalent",
        )
    terminal_patch_equivalent, patch_reason = _can_force_delete_terminal_patch_equivalent_branch(
        primary_root,
        branch=branch,
        latest_attempt=latest_attempt,
        ref_cache=ref_cache,
    )
    if terminal_patch_equivalent:
        return _BranchCleanupPlan(
            branch_exists=True,
            force_delete=True,
            branch_tip=branch_tip,
            reason=patch_reason,
            proof_state="patch_equivalent",
        )
    raise WorktreeError(
        f"refusing cleanup: branch {branch} has commits not proven landed on {target_branch} "
        f"({reason}; {patch_reason})"
    )


def _workspace_adoption_completion_branch_cleanup_plan(
    profile: RepoProfile,
    *,
    primary_root: Path,
    workset_id: str,
    task_id: str,
    branch: str,
    worktree_path: Path,
    latest_attempt: Any | None,
    runtime_state: Any,
) -> _BranchCleanupPlan | None:
    if latest_attempt is None or latest_attempt.status != ATTEMPT_STATUS_SUCCESS:
        return None
    receipt = _workspace_adoption_receipt(latest_attempt)
    if receipt is None or receipt["branch"] != branch:
        return None
    completion_intent = _load_workspace_adoption_completion_intent(
        profile,
        attempt=latest_attempt,
        receipt=receipt,
    )
    if completion_intent is None:
        return None
    predecessor = find_task_attempt(
        runtime_state,
        workset_id,
        str(receipt["predecessor_attempt_id"]),
    )
    if predecessor is None or predecessor.task_id != task_id:
        raise LandingTransactionError(
            "workspace adoption cleanup predecessor is missing"
        )
    predecessor_transaction = load_landing_transaction(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=predecessor.attempt_id,
    )
    if predecessor_transaction is None:
        raise LandingTransactionError(
            "workspace adoption cleanup predecessor transaction is missing"
        )
    native_transaction = (
        load_landing_transaction(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=latest_attempt.attempt_id,
        )
        if completion_intent["completion_route"] == "successor_landing"
        else None
    )
    _validate_workspace_adoption_completion_route(
        profile,
        attempt=latest_attempt,
        receipt=receipt,
        payload=completion_intent,
        predecessor_transaction=predecessor_transaction,
        native_transaction=native_transaction,
        require_native_land=native_transaction is not None,
    )
    _land_payload, complete_payload = _workspace_adoption_completion_payloads(
        attempt=latest_attempt,
        receipt=receipt,
        completion_intent=completion_intent,
    )
    if not _exact_workspace_adoption_event(
        profile,
        event_id=_workspace_adoption_complete_event_id(latest_attempt.attempt_id),
        event_type="worktree.adoption.complete",
        actor=latest_attempt.actor,
        payload=complete_payload,
    ):
        raise LandingTransactionError(
            "workspace adoption cleanup requires exact completion evidence"
        )
    branch_inspection = _inspect_branch_ref(
        primary_root,
        branch,
        role="task_branch",
    )
    if branch_inspection.state == "error":
        raise _inspection_error(branch_inspection)
    if branch_inspection.state == "missing":
        return _BranchCleanupPlan(
            branch_exists=False,
            force_delete=False,
            branch_tip=None,
            reason="adopted successor branch already absent",
            proof_state="workspace_adoption_completion",
        )
    branch_tip = branch_inspection.resolved_commit
    if branch_tip != completion_intent["source_commit"]:
        raise LandingTransactionError(
            "workspace adoption cleanup branch tip conflicts with completion intent"
        )
    if completion_intent["completion_route"] == "successor_landing":
        same_tree = _run_git_no_check(
            primary_root,
            "diff",
            "--quiet",
            branch_tip,
            str(completion_intent["landed_commit"]),
        )
        if same_tree.returncode != 0:
            if same_tree.returncode == 1:
                raise LandingTransactionError(
                    "workspace adoption cleanup source and landed trees differ"
                )
            detail = same_tree.stderr.strip() or same_tree.stdout.strip()
            raise WorktreeError(
                "could not compare workspace adoption cleanup trees: " + detail
            )
    if worktree_path.exists():
        registration = _registered_worktree_row(primary_root, worktree_path)
        if (
            registration is None
            or registration.get("branch") != f"refs/heads/{branch}"
            or str(registration.get("HEAD") or "").strip() != branch_tip
            or _run_git(worktree_path, "rev-parse", "HEAD") != branch_tip
        ):
            raise LandingTransactionError(
                "workspace adoption cleanup worktree registration conflicts"
            )
    return _BranchCleanupPlan(
        branch_exists=True,
        force_delete=True,
        branch_tip=branch_tip,
        reason="exact workspace adoption completion proves the retained source landed",
        proof_state="workspace_adoption_completion",
    )


def _resolve_attempt_worktree(
    profile: RepoProfile,
    *,
    branch: str | None,
    worktree_path: str | None,
    worktree_by_branch: dict[str, Path] | None = None,
) -> Path | None:
    if worktree_path:
        candidate = Path(worktree_path).resolve()
        if candidate.exists() and _is_git_worktree_path(candidate):
            return candidate
    if branch:
        branch_ref = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
        if worktree_by_branch is not None:
            candidate = worktree_by_branch.get(branch_ref)
            if candidate is not None and candidate.exists():
                return candidate.resolve()
            return None
        existing = find_worktree_for_branch(profile, branch)
        if existing:
            candidate = Path(existing).resolve()
            if candidate.exists():
                return candidate
    return None


def _worktree_changed_paths(profile: RepoProfile, worktree_path: Path) -> list[str]:
    return _managed_dirty_paths(profile, worktree_path)


def _attempt_changed_paths(
    profile: RepoProfile,
    *,
    branch: str | None,
    target_branch: str | None,
    worktree_path: Path | None,
    primary_root: Path | None = None,
    changed_paths_cache: dict[tuple[str, str | None], list[str]] | None = None,
) -> list[str]:
    changed: set[str] = set()
    if branch:
        try:
            changed.update(
                branch_changed_paths(
                    profile,
                    branch=branch,
                    target_branch=target_branch,
                    primary_root=primary_root,
                    changed_paths_cache=changed_paths_cache,
                )
            )
        except WorktreeError:
            pass
    if worktree_path is not None and worktree_path.exists():
        changed.update(_worktree_changed_paths(profile, worktree_path))
    return sorted(changed)


def _format_size(size_bytes: int | None) -> str | None:
    if size_bytes is None:
        return None
    units = ("B", "KiB", "MiB", "GiB")
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def _path_size_bytes(path: Path | None) -> int | None:
    if path is None or not path.exists():
        return None
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for filename in files:
            candidate = Path(root) / filename
            if candidate.is_symlink():
                continue
            total += candidate.stat().st_size
    return total


def _commit_metadata(repo_root: Path | None, ref: str = "HEAD") -> dict[str, str | None]:
    empty = {"commit": None, "date": None, "message": None}
    if repo_root is None or not repo_root.exists():
        return empty
    completed = _run_git_no_check(repo_root, "show", "-s", "--format=%H%x00%cI%x00%s", ref)
    if completed.returncode != 0:
        return empty
    parts = completed.stdout.rstrip("\n").split("\x00", 2)
    if len(parts) != 3:
        return empty
    return {"commit": parts[0] or None, "date": parts[1] or None, "message": parts[2] or None}


def _cleanup_classification(
    profile: RepoProfile,
    *,
    primary_root: Path,
    branch: str | None,
    worktree_exists: bool,
    worktree_dirty_paths: list[str],
    active_attempt: bool,
    latest_attempt: TaskRuntimeRecord | None,
    ref_cache: dict[str, str | None] | None = None,
) -> _CleanupClassification:
    if not worktree_exists:
        if active_attempt:
            return _CleanupClassification("missing_worktree", "active attempt workspace is missing", "missing_active")
        if not branch:
            return _CleanupClassification("absent", "no branch or worktree remains", "absent")
        try:
            plan = _plan_task_branch_cleanup(
                primary_root,
                branch=branch,
                latest_attempt=latest_attempt,
                ref_cache=ref_cache,
            )
        except WorktreeError as exc:
            return _CleanupClassification("blocked_unlanded", str(exc), "unproven")
        if plan.branch_exists:
            return _CleanupClassification("cleanup_ready", f"worktree already absent; {plan.reason}", plan.proof_state)
        return _CleanupClassification("absent", "worktree and branch are already absent", plan.proof_state)
    if worktree_dirty_paths:
        return _CleanupClassification("blocked_dirty", "worktree has uncommitted changes", "dirty")
    if active_attempt:
        return _CleanupClassification("active", "active attempts must be landed or closed before cleanup", "active")
    if not branch:
        return _CleanupClassification("cleanup_ready", "no branch recorded", "no_branch")
    try:
        plan = _plan_task_branch_cleanup(
            primary_root,
            branch=branch,
            latest_attempt=latest_attempt,
            ref_cache=ref_cache,
        )
    except (WorktreeError, OSError) as exc:
        return _CleanupClassification("blocked_unlanded", str(exc), "unproven")
    return _CleanupClassification("cleanup_ready", plan.reason, plan.proof_state)


def _worktree_table_row(
    profile: RepoProfile,
    *,
    workset_id: str,
    task: TaskSpec,
    runtime_state: Any | None = None,
    primary_root: Path | None = None,
    primary_dirty: bool | None = None,
    primary_dirty_paths: list[str] | None = None,
    worktree_by_branch: dict[str, Path] | None = None,
    ref_cache: dict[str, str | None] | None = None,
    branch_ahead_cache: dict[tuple[str, str], tuple[bool, str | None]] | None = None,
    changed_paths_cache: dict[tuple[str, str | None], list[str]] | None = None,
    events: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    runtime_state = runtime_state or load_runtime_state(profile.paths)
    primary_root = primary_root or find_primary_worktree(profile.paths.project_root)
    active_attempt = active_task_attempt(runtime_state, workset_id, task.task_id)
    latest_attempt = latest_task_attempt(runtime_state, workset_id, task.task_id)
    selected_attempt = active_attempt or latest_attempt
    if selected_attempt is None:
        return None

    payload = _task_recovery_payload(
        profile,
        workset_id=workset_id,
        task_id=task.task_id,
        runtime_state=runtime_state,
        primary_root=primary_root,
        primary_dirty=primary_dirty,
        primary_dirty_paths=primary_dirty_paths,
        worktree_by_branch=worktree_by_branch,
        ref_cache=ref_cache,
        branch_ahead_cache=branch_ahead_cache,
        changed_paths_cache=changed_paths_cache,
        events=events,
    )

    worktree_path = Path(payload["worktree_path"]).resolve() if payload["worktree_path"] else None
    worktree_dirty_paths = list(payload["worktree_dirty_paths"])
    resolved_branch = (
        payload["branch"]
        or (latest_attempt.branch if latest_attempt is not None else None)
        or default_task_branch(workset_id, task)
    )
    cleanup = _cleanup_classification(
        profile,
        primary_root=primary_root,
        branch=resolved_branch,
        worktree_exists=bool(payload["worktree_exists"]),
        worktree_dirty_paths=worktree_dirty_paths,
        active_attempt=bool(payload["active_attempt"]),
        latest_attempt=latest_attempt,
        ref_cache=ref_cache,
    )
    if (
        not payload["worktree_exists"]
        and not payload["active_attempt"]
        and not payload["stale_claim"]
        and cleanup.status == "absent"
    ):
        return None
    commit_root = worktree_path if worktree_path is not None and worktree_path.exists() else primary_root
    commit_ref = "HEAD" if worktree_path is not None and worktree_path.exists() else resolved_branch or "HEAD"
    commit = _commit_metadata(commit_root, commit_ref)
    size_bytes = _path_size_bytes(worktree_path)
    cleanup_command = (
        f"blackdog task cleanup --project-root . --workset {workset_id} --task {task.task_id}"
        if cleanup.status == "cleanup_ready"
        else None
    )
    recommended_action = cleanup_command
    if recommended_action is None and payload["recommended_actions"]:
        recommended_action = payload["recommended_actions"][0]
    if recommended_action is None:
        recommended_action = cleanup.reason
    return {
        "workset_id": workset_id,
        "task_id": task.task_id,
        "task_title": task.title,
        "state": payload["recovery_state"],
        "latest_attempt_status": payload["latest_attempt_status"],
        "started_at": payload["started_at"],
        "ended_at": payload["ended_at"],
        "last_commit_at": commit["date"],
        "last_commit": commit["commit"],
        "last_commit_message": commit["message"],
        "branch": resolved_branch,
        "target_branch": payload["target_branch"],
        "worktree_path": payload["worktree_path"],
        "worktree_dirty_count": len(worktree_dirty_paths),
        "branch_ahead_of_target": payload["branch_ahead_of_target"],
        "changed_paths_count": len(payload["changed_paths"]),
        "size_bytes": size_bytes,
        "size": _format_size(size_bytes),
        "cleanup_status": cleanup.status,
        "cleanup_reason": cleanup.reason,
        "cleanup_proof": cleanup.proof_state,
        "cleanup_command": cleanup_command,
        "recommended_action": recommended_action,
    }


def build_worktree_table(profile: RepoProfile) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    planning_state = load_planning_state(profile.paths)
    runtime_state = load_runtime_state(profile.paths)
    events = load_events(profile.paths.events_file)
    primary_root = find_primary_worktree(profile.paths.project_root)
    primary_dirty_paths = _managed_dirty_paths(profile, primary_root)
    primary_dirty = bool(primary_dirty_paths)
    worktree_by_branch = _worktree_branch_map(primary_root)
    ref_cache: dict[str, str | None] = {}
    branch_ahead_cache: dict[tuple[str, str], tuple[bool, str | None]] = {}
    changed_paths_cache: dict[tuple[str, str | None], list[str]] = {}
    for workset in planning_state.worksets:
        for task in workset.tasks:
            row = _worktree_table_row(
                profile,
                workset_id=workset.workset_id,
                task=task,
                runtime_state=runtime_state,
                primary_root=primary_root,
                primary_dirty=primary_dirty,
                primary_dirty_paths=primary_dirty_paths,
                worktree_by_branch=worktree_by_branch,
                ref_cache=ref_cache,
                branch_ahead_cache=branch_ahead_cache,
                changed_paths_cache=changed_paths_cache,
                events=events,
            )
            if row is not None:
                rows.append(row)
    rows.sort(
        key=lambda row: (
            str(row.get("started_at") or ""),
            str(row.get("workset_id") or ""),
            str(row.get("task_id") or ""),
        ),
        reverse=True,
    )
    return {
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "columns": list(WORKTREE_TABLE_COLUMNS),
        "rows": rows,
        "counts": {
            "rows": len(rows),
            "cleanup_ready": sum(1 for row in rows if row["cleanup_status"] == "cleanup_ready"),
            "blocked": sum(1 for row in rows if str(row["cleanup_status"]).startswith("blocked_")),
            "active": sum(1 for row in rows if row["state"] == "active_attempt"),
            "missing_worktree": sum(1 for row in rows if row["cleanup_status"] == "missing_worktree"),
        },
    }


def cleanup_worktree_table(profile: RepoProfile) -> dict[str, Any]:
    before = build_worktree_table(profile)
    cleaned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for row in before["rows"]:
        if row["cleanup_status"] != "cleanup_ready":
            skipped.append(
                {
                    "workset_id": row["workset_id"],
                    "task_id": row["task_id"],
                    "cleanup_status": row["cleanup_status"],
                    "reason": row["recommended_action"],
                }
            )
            continue
        try:
            cleanup = cleanup_task_worktree(
                profile,
                workset_id=row["workset_id"],
                task_id=row["task_id"],
                path=row["worktree_path"],
                branch=row["branch"],
            )
        except WorktreeError as exc:
            errors.append(
                {
                    "workset_id": row["workset_id"],
                    "task_id": row["task_id"],
                    "worktree_path": row["worktree_path"],
                    "error": str(exc),
                }
            )
        else:
            cleaned.append(
                {
                    "workset_id": row["workset_id"],
                    "task_id": row["task_id"],
                    **cleanup,
                }
            )
    return {
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "cleaned": cleaned,
        "skipped": skipped,
        "errors": errors,
        "before": before,
        "remaining": build_worktree_table(profile),
    }


def _append_prompt_lineage_trailers(
    lines: list[str],
    *,
    prompt_receipt: PromptReceiptRecord | None,
    user_prompt_receipt: PromptReceiptRecord | None,
) -> None:
    if prompt_receipt is None:
        return
    lines.append(f"Blackdog-Prompt-Hash: {prompt_receipt.prompt_hash}")
    if prompt_receipt.source:
        lines.append(f"Blackdog-Prompt-Source: {prompt_receipt.source}")
    if prompt_receipt.mode:
        lines.append(f"Blackdog-Prompt-Mode: {prompt_receipt.mode}")
    if user_prompt_receipt is None:
        return
    same_prompt_lineage = (
        user_prompt_receipt.prompt_hash == prompt_receipt.prompt_hash
        and user_prompt_receipt.source == prompt_receipt.source
        and user_prompt_receipt.mode == prompt_receipt.mode
    )
    if same_prompt_lineage:
        return
    lines.append(f"Blackdog-User-Prompt-Hash: {user_prompt_receipt.prompt_hash}")
    if user_prompt_receipt.source:
        lines.append(f"Blackdog-User-Prompt-Source: {user_prompt_receipt.source}")
    if user_prompt_receipt.mode:
        lines.append(f"Blackdog-User-Prompt-Mode: {user_prompt_receipt.mode}")


def _append_codex_session_trailers(
    lines: list[str],
    *,
    codex_session: CodexSessionRefRecord | None,
) -> None:
    if codex_session is None:
        return
    lines.append(f"Blackdog-Codex-Thread: {codex_session.thread_id}")
    if codex_session.session_path:
        lines.append(f"Blackdog-Codex-Session: {codex_session.session_path}")
    if codex_session.turn_id:
        lines.append(f"Blackdog-Codex-Turn: {codex_session.turn_id}")


def _canonical_commit_message(
    workset: Workset,
    task: TaskSpec,
    *,
    attempt_id: str,
    actor: str,
    changed_paths: tuple[str, ...],
    prompt_receipt: PromptReceiptRecord | None,
    user_prompt_receipt: PromptReceiptRecord | None,
    codex_session: CodexSessionRefRecord | None,
    execution_model: str | None,
    model: str | None,
    reasoning_effort: str | None,
    target_branch: str | None,
    status: str,
    summary: str,
    validations: tuple[ValidationRecord, ...],
    residuals: tuple[str, ...],
    followup_candidates: tuple[str, ...],
) -> str:
    summary_lines = tuple(
        " ".join(line.split())
        for line in str(summary).splitlines()
        if line.strip()
    )
    if not summary_lines:
        raise BacklogError("landing summary must contain a nonempty human-readable line")
    lines = [
        summary_lines[0],
        "",
    ]
    if len(summary_lines) > 1:
        lines.extend((*summary_lines[1:], ""))
    lines.extend(
        [
            f"Blackdog-Workset: {workset.workset_id}",
            f"Blackdog-Task: {task.task_id}",
            f"Blackdog-Attempt: {attempt_id}",
            f"Blackdog-Actor: {actor}",
            f"Blackdog-Status: {status}",
            f"Blackdog-Commit-Format: {CANONICAL_COMMIT_FORMAT_VERSION}",
        ]
    )
    if target_branch:
        lines.append(f"Blackdog-Target-Branch: {target_branch}")
    if execution_model:
        lines.append(f"Blackdog-Execution-Model: {execution_model}")
    if model:
        lines.append(f"Blackdog-Model: {model}")
    if reasoning_effort:
        lines.append(f"Blackdog-Reasoning-Effort: {reasoning_effort}")
    _append_prompt_lineage_trailers(
        lines,
        prompt_receipt=prompt_receipt,
        user_prompt_receipt=user_prompt_receipt,
    )
    _append_codex_session_trailers(lines, codex_session=codex_session)
    for path in changed_paths:
        lines.append(f"Blackdog-Changed-Path: {path}")
    for validation in validations:
        lines.append(f"Blackdog-Validation: {validation.name}={validation.status}")
    for residual in residuals:
        lines.append(f"Blackdog-Residual: {residual}")
    for followup in followup_candidates:
        lines.append(f"Blackdog-Followup: {followup}")
    return "\n".join(lines).rstrip() + "\n"


def _landing_prep_commit_message(
    workset: Workset,
    task: TaskSpec,
    *,
    attempt_id: str,
) -> str:
    return (
        f"blackdog-wip({workset.workset_id}/{task.task_id}): prepare land\n\n"
        "Auto-commit task worktree changes so `blackdog worktree land` can create\n"
        "one canonical landed commit for the attempt.\n\n"
        f"Blackdog-Workset: {workset.workset_id}\n"
        f"Blackdog-Task: {task.task_id}\n"
        f"Blackdog-Attempt: {attempt_id}\n"
        "Blackdog-Status: staged-for-land\n"
    )


def _tracked_index_modes(worktree_path: Path) -> dict[str, str]:
    raw = _run_git_bytes(worktree_path, "ls-files", "--stage", "-z")
    modes: dict[str, str] = {}
    for row in raw.split(b"\0"):
        if not row:
            continue
        metadata, separator, raw_path = row.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) < 3:
            raise WorktreeError("git ls-files --stage returned malformed landing source evidence")
        modes[os.fsdecode(raw_path)] = parts[0].decode("ascii")
    return modes


def _projected_source_tree_manifest(worktree_path: Path) -> tuple[bytes, str]:
    """Hash the exact tree `git add -A` would prepare without writing Git state."""
    tracked_modes = _tracked_index_modes(worktree_path)
    filemode_row = _run_git_no_check(worktree_path, "config", "--bool", "core.filemode")
    honor_filemode = filemode_row.returncode != 0 or filemode_row.stdout.strip() != "false"
    raw_paths = _run_git_bytes(
        worktree_path,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    rows: list[bytes] = []
    for raw_path in sorted({item for item in raw_paths.split(b"\0") if item}):
        relative_path = os.fsdecode(raw_path)
        absolute_path = worktree_path / relative_path
        try:
            metadata = absolute_path.lstat()
        except FileNotFoundError:
            continue
        tracked_mode = tracked_modes.get(relative_path)
        if tracked_mode == "160000":
            object_id = _run_git(absolute_path, "rev-parse", "HEAD")
            mode = "160000"
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(absolute_path)
            target_bytes = os.fsencode(target)
            object_id = _run_git_bytes(
                worktree_path,
                "hash-object",
                "--stdin",
                f"--path={relative_path}",
                input_bytes=target_bytes,
            ).decode("ascii").strip()
            mode = "120000"
        elif stat.S_ISREG(metadata.st_mode):
            object_id = _run_git_bytes(
                worktree_path,
                "hash-object",
                "--stdin",
                f"--path={relative_path}",
                input_bytes=absolute_path.read_bytes(),
            ).decode("ascii").strip()
            if tracked_mode in {"100644", "100755"} and not honor_filemode:
                mode = tracked_mode
            else:
                mode = "100755" if metadata.st_mode & 0o111 else "100644"
        else:
            raise WorktreeError(
                f"landing source path has unsupported filesystem type: {relative_path}"
            )
        rows.append(f"{mode} {object_id}\t".encode("ascii") + raw_path + b"\0")
    manifest = b"".join(rows)
    return manifest, hashlib.sha256(manifest).hexdigest()


def _committed_tree_manifest(repo_root: Path, commit: str) -> tuple[bytes, str]:
    raw = _run_git_bytes(repo_root, "ls-tree", "-r", "-z", commit)
    rows: list[bytes] = []
    for row in raw.split(b"\0"):
        if not row:
            continue
        metadata, separator, raw_path = row.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 3:
            raise WorktreeError(f"git ls-tree returned malformed evidence for {commit}")
        mode, _object_type, object_id = parts
        rows.append(mode + b" " + object_id + b"\t" + raw_path + b"\0")
    manifest = b"".join(rows)
    return manifest, hashlib.sha256(manifest).hexdigest()


def _tree_manifest_entries(manifest: bytes) -> dict[bytes, bytes]:
    entries: dict[bytes, bytes] = {}
    for row in manifest.split(b"\0"):
        if not row:
            continue
        metadata, separator, path = row.partition(b"\t")
        if not separator or not path or path in entries:
            raise WorktreeError("landing tree manifest is malformed or contains duplicate paths")
        entries[path] = metadata
    return entries


def _tree_manifest_changed_paths(base: bytes, projected: bytes) -> tuple[str, ...]:
    base_entries = _tree_manifest_entries(base)
    projected_entries = _tree_manifest_entries(projected)
    changed = sorted(
        path
        for path in set(base_entries) | set(projected_entries)
        if base_entries.get(path) != projected_entries.get(path)
    )
    return tuple(os.fsdecode(path) for path in changed)


def _landing_source_projection(worktree_path: Path, source_head: str) -> tuple[str, str]:
    _manifest, tree_hash = _projected_source_tree_manifest(worktree_path)
    fingerprint = hashlib.sha256(
        f"blackdog.landing.source/v1\0{source_head}\0{tree_hash}".encode("utf-8")
    ).hexdigest()
    return tree_hash, fingerprint


def _commit_dirty_attempt_worktree(
    profile: RepoProfile,
    *,
    workset: Workset,
    task: TaskSpec,
    branch: str | None,
    worktree_path: Path | None,
    attempt_id: str,
) -> str | None:
    if branch is None or worktree_path is None or not worktree_path.exists():
        return None
    if not _managed_status_dirty(profile, worktree_path):
        return None
    _run_git(worktree_path, "add", "-A")
    staged = _run_git_no_check(worktree_path, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        return None
    _run_git_with_input(
        worktree_path,
        "commit",
        "--quiet",
        "-F",
        "-",
        input_text=_landing_prep_commit_message(workset, task, attempt_id=attempt_id),
    )
    return _run_git(find_primary_worktree(profile.paths.project_root), "rev-parse", branch)


def _ordinary_resume_start_event_id(attempt_id: str) -> str:
    return hashlib.sha256(
        f"blackdog.worktree.start.resume/v1\0{attempt_id}".encode("utf-8")
    ).hexdigest()


def _initial_start_event_id(attempt_id: str) -> str:
    return hashlib.sha256(
        f"blackdog.worktree.start.initial/v1\0{attempt_id}".encode("utf-8")
    ).hexdigest()


def _worktree_start_receipt(*, preview: WorktreePreview) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "base_ref": preview.base_ref,
        "base_commit": preview.base_commit,
        "primary_worktree": preview.primary_worktree,
    }


def _durable_worktree_start_receipt(attempt: Any) -> dict[str, Any]:
    setup = attempt.setup_receipt
    receipt = setup.get("worktree_start") if isinstance(setup, Mapping) else None
    if (
        not isinstance(receipt, Mapping)
        or set(receipt) != _WORKTREE_START_RECEIPT_KEYS
        or receipt.get("schema_version") != 1
        or not isinstance(receipt.get("base_ref"), str)
        or not str(receipt["base_ref"]).strip()
        or receipt.get("base_commit") != attempt.start_commit
        or not isinstance(receipt.get("primary_worktree"), str)
        or not str(receipt["primary_worktree"]).strip()
    ):
        raise WorktreeError("reserved task start is missing its durable worktree-start receipt")
    recorded_primary = Path(str(receipt["primary_worktree"]))
    if (
        not recorded_primary.is_absolute()
        or recorded_primary != recorded_primary.resolve(strict=False)
    ):
        raise WorktreeError(
            "reserved task start primary worktree is no longer its recorded absolute path"
        )
    return dict(receipt)


def _worktree_start_event_payload(
    *,
    spec: WorktreeSpec,
    attempt: Any,
    handlers: HandlerPlanSummary,
) -> dict[str, Any]:
    user_receipt = attempt.user_prompt_receipt or attempt.prompt_receipt
    return {
        "workset_id": spec.workset_id,
        "task_id": spec.task_id,
        "attempt_id": spec.attempt_id,
        "branch": spec.branch,
        "target_branch": spec.target_branch,
        "base_ref": spec.base_ref,
        "base_commit": spec.base_commit,
        "worktree_path": spec.worktree_path,
        "prompt_hash": spec.prompt_hash,
        "prompt_source": spec.prompt_source,
        "prompt_mode": spec.prompt_mode,
        "prompt_replay_artifact_path": (
            attempt.prompt_receipt.replay_artifact_path
            if attempt.prompt_receipt is not None
            else None
        ),
        "user_prompt_hash": user_receipt.prompt_hash if user_receipt is not None else None,
        "user_prompt_source": user_receipt.source if user_receipt is not None else None,
        "user_prompt_mode": user_receipt.mode if user_receipt is not None else None,
        "user_prompt_replay_artifact_path": (
            user_receipt.replay_artifact_path if user_receipt is not None else None
        ),
        "workspace_blackdog_path": handlers.blackdog_path,
        "runtime_mode": handlers.runtime_mode,
        "source_mode": handlers.source_mode,
        "script_policy": handlers.script_policy,
        "setup_receipt": attempt.setup_receipt,
        "model": attempt.model,
        "reasoning_effort": attempt.reasoning_effort,
        "codex_thread_id": (
            attempt.codex_session.thread_id
            if attempt.codex_session is not None
            else None
        ),
        "codex_session_path": (
            attempt.codex_session.session_path
            if attempt.codex_session is not None
            else None
        ),
        "handler_actions": [action.to_dict() for action in handlers.actions],
    }


def _durable_start_handlers(
    profile: RepoProfile,
    attempt: Any,
) -> HandlerPlanSummary:
    """Reconstruct timing-stable handler evidence from the durable setup receipt."""

    setup = attempt.setup_receipt
    if (
        not isinstance(setup, Mapping)
        or setup.get("schema_version")
        not in {LEGACY_SETUP_RECEIPT_SCHEMA_VERSION, SETUP_RECEIPT_SCHEMA_VERSION}
        or setup.get("status") != "ok"
        or setup.get("blockers") != []
        or not isinstance(setup.get("probes"), list)
    ):
        raise WorktreeError(
            "reserved ordinary resume setup receipt cannot reconstruct handler evidence"
        )
    actions: list[HandlerAction] = []
    for probe in setup["probes"]:
        if (
            not isinstance(probe, Mapping)
            or not isinstance(probe.get("handler_id"), str)
            or not isinstance(probe.get("kind"), str)
            or not isinstance(probe.get("action"), str)
        ):
            continue
        actions.append(
            HandlerAction(
                handler_id=str(probe["handler_id"]),
                kind=str(probe["kind"]),
                action=str(probe["action"]),
                target_path=(
                    str(probe["target_path"])
                    if probe.get("target_path") is not None
                    else None
                ),
                status=(
                    HANDLER_STATUS_VALIDATED
                    if probe.get("status") == "ok"
                    else HANDLER_STATUS_BLOCKED
                ),
                message=str(probe.get("message") or ""),
                elapsed_ms=(
                    int(probe["elapsed_ms"])
                    if type(probe.get("elapsed_ms")) is int
                    else None
                ),
            )
        )
    if not actions and any(handler.enabled for handler in profile.handlers):
        raise WorktreeError(
            "reserved ordinary resume setup receipt has no handler evidence"
        )
    return HandlerPlanSummary(
        ready=True,
        actions=tuple(actions),
        remediation=None,
        worktree_ve_path=(
            str(setup["workspace_ve"])
            if setup.get("workspace_ve") is not None
            else None
        ),
        blackdog_path=(
            str(setup["workspace_blackdog_path"])
            if setup.get("workspace_blackdog_path") is not None
            else None
        ),
        runtime_mode=(
            str(setup["runtime_mode"])
            if setup.get("runtime_mode") is not None
            else None
        ),
        source_mode=(
            str(setup["source_mode"])
            if setup.get("source_mode") is not None
            else None
        ),
        script_policy=(
            str(setup["script_policy"])
            if setup.get("script_policy") is not None
            else None
        ),
    )


def _reserved_resume_attempt(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    resume_guard: _TaskResumeGuard,
) -> Any | None:
    attempt = find_task_attempt(
        load_runtime_state(profile.paths),
        workset_id,
        resume_guard.attempt_id,
    )
    if (
        attempt is None
        or attempt.task_id != task_id
        or attempt.status != ATTEMPT_STATUS_IN_PROGRESS
        or attempt.ended_at is not None
    ):
        return None
    return attempt


def _reserved_initial_attempt(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    branch: str,
    worktree_path: Path,
    start_commit: str,
    execution_receipt: PromptReceiptRecord,
    user_receipt: PromptReceiptRecord,
) -> Any | None:
    runtime_state = load_runtime_state(profile.paths)
    attempts = _task_attempts_in_append_order(
        runtime_state,
        workset_id=workset_id,
        task_id=task_id,
    )
    if len(attempts) != 1:
        return None
    attempt = attempts[0]
    if (
        attempt.status != ATTEMPT_STATUS_IN_PROGRESS
        or attempt.ended_at is not None
        or attempt.actor != actor
        or attempt.branch != branch
        or Path(str(attempt.worktree_path)).resolve(strict=False)
        != worktree_path.resolve(strict=False)
        or attempt.start_commit != start_commit
        or attempt.prompt_receipt is None
        or attempt.user_prompt_receipt is None
        or (
            attempt.prompt_receipt.prompt_hash,
            attempt.prompt_receipt.mode,
            attempt.user_prompt_receipt.prompt_hash,
            attempt.user_prompt_receipt.mode,
        )
        != (
            execution_receipt.prompt_hash,
            execution_receipt.mode,
            user_receipt.prompt_hash,
            user_receipt.mode,
        )
    ):
        return None
    return attempt


def _recorded_start_worktree_path(attempt: Any) -> Path:
    recorded = Path(str(attempt.worktree_path or ""))
    if (
        not recorded.is_absolute()
        or recorded != recorded.resolve(strict=False)
    ):
        raise WorktreeError(
            "reserved task start worktree path is no longer its recorded absolute path"
        )
    return recorded


def _handler_contract_action(action: HandlerAction) -> tuple[str, str, str, str | None, str]:
    """Project transient handler execution verbs onto their preview contract."""

    normalized_action = {
        "create-root-venv": "validate-root-venv",
        "ensure-root-venv": "validate-root-venv",
        "create-worktree-venv": "ensure-worktree-venv",
        "validate-worktree-venv": "ensure-worktree-venv",
    }.get(action.action, action.action)
    return (
        action.handler_id,
        action.kind,
        normalized_action,
        action.target_path,
        (
            "ok"
            if action.status
            in {
                "planned",
                "validated",
                "created",
                HANDLER_STATUS_UPDATED,
                "preserved",
                "skipped",
            }
            else "blocked"
        ),
    )


def _preflight_reserved_handler_contract(
    profile: RepoProfile,
    *,
    attempt: Any,
) -> None:
    """Prove the durable handler receipt still matches the live config, read-only."""

    worktree_path = _recorded_start_worktree_path(attempt)
    durable = _durable_start_handlers(profile, attempt)
    planned = plan_worktree_handlers(profile, worktree_path=worktree_path)
    if not planned.ready:
        raise WorktreeError(
            planned.remediation
            or "reserved task start handler contract is no longer ready"
        )
    summary_fields = (
        "worktree_ve_path",
        "blackdog_path",
        "runtime_mode",
        "source_mode",
        "script_policy",
    )
    mismatches = [
        field
        for field in summary_fields
        if getattr(durable, field) != getattr(planned, field)
    ]
    durable_actions = tuple(_handler_contract_action(action) for action in durable.actions)
    planned_actions = tuple(_handler_contract_action(action) for action in planned.actions)
    if Counter(durable_actions) != Counter(planned_actions):
        mismatches.append("handler_actions")
    if mismatches:
        raise WorktreeError(
            "reserved task start handler contract conflicts with durable setup: "
            + ", ".join(mismatches)
            + (
                f" (durable={durable_actions!r}, planned={planned_actions!r})"
                if "handler_actions" in mismatches
                else ""
            )
        )


def _preflight_reserved_resume_workspace(
    profile: RepoProfile,
    *,
    attempt: Any,
) -> None:
    """Reject conflicting workspace identity without mutating Git or handlers."""

    if not attempt.worktree_path or not attempt.branch or not attempt.start_commit:
        raise WorktreeError("reserved ordinary resume is missing workspace identity")
    receipt = _durable_worktree_start_receipt(attempt)
    primary_root = Path(str(receipt["primary_worktree"])).resolve()
    if primary_root != find_primary_worktree(profile.paths.project_root):
        raise WorktreeError("reserved ordinary resume primary worktree identity changed")
    worktree_path = _recorded_start_worktree_path(attempt)
    branch_ref = f"refs/heads/{attempt.branch}"
    registration = _registered_worktree_row(primary_root, worktree_path)
    if registration is not None and (
        registration.get("branch") != branch_ref
        or registration.get("HEAD") != attempt.start_commit
    ):
        raise WorktreeError("reserved ordinary resume has a conflicting Git registration")
    if worktree_path.exists():
        if (
            registration is None
            or not _is_git_worktree_path(worktree_path)
            or _run_git(worktree_path, "rev-parse", "HEAD") != attempt.start_commit
            or _run_git(primary_root, "rev-parse", attempt.branch) != attempt.start_commit
            or _managed_status_dirty(profile, worktree_path)
        ):
            raise WorktreeError(
                "reserved ordinary resume workspace is not clean at its recorded start commit"
            )
        operation = _in_progress_git_operation(worktree_path)
        if operation is not None:
            raise WorktreeError(
                f"reserved ordinary resume workspace has an in-progress Git operation ({operation})"
            )
        return
    registered_for_branch = _find_worktree_for_branch(primary_root, branch_ref)
    if registered_for_branch is not None and registered_for_branch != worktree_path:
        raise WorktreeError(
            "reserved ordinary resume branch is registered at a different workspace: "
            f"{registered_for_branch}"
        )
    branch = _run_git_no_check(
        primary_root,
        "show-ref",
        "--verify",
        "--quiet",
        branch_ref,
    )
    if branch.returncode not in {0, 1}:
        detail = branch.stderr.strip() or branch.stdout.strip() or str(branch.returncode)
        raise WorktreeError(f"could not inspect reserved resume branch: {detail}")
    if (
        branch.returncode == 0
        and _run_git(primary_root, "rev-parse", attempt.branch) != attempt.start_commit
    ):
        raise WorktreeError(
            "reserved ordinary resume branch moved away from its recorded start commit"
        )


def _ensure_reserved_resume_workspace(
    profile: RepoProfile,
    *,
    attempt: Any,
) -> tuple[Path, HandlerPlanSummary, bool]:
    """Preserve or recreate the exact workspace owned by a reserved successor."""

    if not attempt.worktree_path or not attempt.branch or not attempt.start_commit:
        raise WorktreeError("reserved ordinary resume is missing workspace identity")
    try:
        start_receipt = _durable_worktree_start_receipt(attempt)
    except WorktreeError as exc:
        raise _TaskStartProofConflict(str(exc)) from exc
    primary_root = Path(str(start_receipt["primary_worktree"])).resolve()
    if primary_root != find_primary_worktree(profile.paths.project_root):
        raise WorktreeError("reserved ordinary resume primary worktree identity changed")
    worktree_path = _recorded_start_worktree_path(attempt)
    branch_ref = f"refs/heads/{attempt.branch}"
    registration = _registered_worktree_row(primary_root, worktree_path)
    workspace_mutated = False
    if registration is not None and not worktree_path.exists():
        if (
            registration.get("branch") != branch_ref
            or registration.get("HEAD") != attempt.start_commit
        ):
            raise WorktreeError(
                "missing reserved workspace has a conflicting Git registration"
            )
        removed = _run_git_no_check(
            primary_root,
            "worktree",
            "remove",
            "--force",
            str(worktree_path),
        )
        if removed.returncode != 0:
            detail = removed.stderr.strip() or removed.stdout.strip() or str(removed.returncode)
            raise WorktreeError(
                f"could not remove stale reserved-workspace registration: {detail}"
            )
        registration = None
        workspace_mutated = True
    if registration is not None:
        if (
            registration.get("branch") != branch_ref
            or registration.get("HEAD") != attempt.start_commit
            or not _is_git_worktree_path(worktree_path)
        ):
            raise WorktreeError(
                "reserved ordinary resume workspace registration conflicts with its start identity"
            )
    else:
        registered_for_branch = _find_worktree_for_branch(primary_root, branch_ref)
        if registered_for_branch is not None:
            raise WorktreeError(
                "reserved ordinary resume branch is registered at a different workspace: "
                f"{registered_for_branch}"
            )
        if worktree_path.exists():
            raise WorktreeError(
                "reserved ordinary resume path exists but is not its registered Git worktree"
            )
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        branch = _run_git_no_check(
            primary_root,
            "show-ref",
            "--verify",
            "--quiet",
            branch_ref,
        )
        if branch.returncode not in {0, 1}:
            detail = branch.stderr.strip() or branch.stdout.strip() or str(branch.returncode)
            raise WorktreeError(f"could not inspect reserved resume branch: {detail}")
        if (
            branch.returncode == 0
            and _run_git(primary_root, "rev-parse", attempt.branch) != attempt.start_commit
        ):
            raise WorktreeError(
                "reserved ordinary resume branch moved away from its recorded start commit"
            )
        add_args = (
            ("worktree", "add", str(worktree_path), attempt.branch)
            if branch.returncode == 0
            else (
                "worktree",
                "add",
                str(worktree_path),
                "-b",
                attempt.branch,
                attempt.start_commit,
            )
        )
        added = _run_git_no_check(primary_root, *add_args)
        if added.returncode != 0:
            detail = added.stderr.strip() or added.stdout.strip() or str(added.returncode)
            raise WorktreeError(f"could not recreate reserved resume workspace: {detail}")
        workspace_mutated = True

    operation = _in_progress_git_operation(worktree_path)
    if operation is not None:
        raise WorktreeError(
            f"reserved ordinary resume workspace has an in-progress Git operation ({operation})"
        )
    observed_head = _run_git(worktree_path, "rev-parse", "HEAD")
    branch_tip = _run_git(primary_root, "rev-parse", attempt.branch)
    registration = _registered_worktree_row(primary_root, worktree_path)
    if (
        observed_head != attempt.start_commit
        or branch_tip != attempt.start_commit
        or registration is None
        or registration.get("branch") != branch_ref
        or registration.get("HEAD") != attempt.start_commit
        or _managed_status_dirty(profile, worktree_path)
    ):
        raise WorktreeError(
            "reserved ordinary resume workspace is not clean at its recorded start commit"
        )
    validation_handlers = validate_existing_worktree_handlers(
        profile,
        worktree_path=worktree_path,
    )
    if validation_handlers.ready:
        proof_handlers = plan_worktree_handlers(profile, worktree_path=worktree_path)
        live_handlers = validation_handlers
    else:
        proof_handlers = execute_worktree_handlers(profile, worktree_path=worktree_path)
        workspace_mutated = workspace_mutated or any(
            action.status in {"created", "updated"}
            for action in proof_handlers.actions
        )
        if not proof_handlers.ready:
            raise WorktreeError(
                proof_handlers.remediation
                or "reserved ordinary resume handlers are not ready"
            )
        live_handlers = validate_existing_worktree_handlers(
            profile,
            worktree_path=worktree_path,
        )
        if not live_handlers.ready:
            raise WorktreeError(
                live_handlers.remediation
                or "reserved ordinary resume handlers failed post-repair validation"
            )
    if (
        _run_git(worktree_path, "rev-parse", "HEAD") != attempt.start_commit
        or _run_git(primary_root, "rev-parse", attempt.branch) != attempt.start_commit
        or _managed_status_dirty(profile, worktree_path)
        or _in_progress_git_operation(worktree_path) is not None
    ):
        raise WorktreeError(
            "reserved ordinary resume workspace changed while validating handlers"
        )
    return primary_root, live_handlers, workspace_mutated


def _repair_reserved_resume_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    resume_guard: _TaskResumeGuard,
    cwd: Path | None,
    model: str | None,
    reasoning_effort: str | None,
    branch: str | None,
    from_ref: str | None,
    path: str | None,
    note: str | None,
    _attempt_lock_held: bool = False,
) -> WorktreeSpec:
    if not _attempt_lock_held:
        with attempt_lifecycle_lock(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=resume_guard.attempt_id,
        ):
            return _repair_reserved_resume_worktree(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                resume_guard=resume_guard,
                cwd=cwd,
                model=model,
                reasoning_effort=reasoning_effort,
                branch=branch,
                from_ref=from_ref,
                path=path,
                note=note,
                _attempt_lock_held=True,
            )
    attempt = _reserved_resume_attempt(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        resume_guard=resume_guard,
    )
    if attempt is None:
        raise BacklogError("reserved ordinary resume successor is no longer active")
    workset, task = _require_workset_and_task(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    try:
        start_receipt = _durable_worktree_start_receipt(attempt)
    except WorktreeError as exc:
        raise _TaskStartProofConflict(str(exc)) from exc
    primary_root = Path(str(start_receipt["primary_worktree"])).resolve()
    override_conflicts: list[str] = []
    if model is not None and model != attempt.model:
        override_conflicts.append("model")
    if reasoning_effort is not None and reasoning_effort != attempt.reasoning_effort:
        override_conflicts.append("reasoning_effort")
    if branch is not None and branch != attempt.branch:
        override_conflicts.append("branch")
    if path is not None and Path(path).expanduser().resolve() != Path(str(attempt.worktree_path)).resolve():
        override_conflicts.append("path")
    if note is not None and note != attempt.note:
        override_conflicts.append("note")
    if from_ref is not None:
        resolved_from = _run_git_no_check(primary_root, "rev-parse", from_ref)
        if (
            resolved_from.returncode != 0
            or resolved_from.stdout.strip() != start_receipt["base_commit"]
        ):
            override_conflicts.append("from_ref")
    if override_conflicts:
        raise _TaskStartProofConflict(
            "reserved ordinary resume retry overrides conflict with durable identity: "
            + ", ".join(override_conflicts)
        )
    runtime_state = load_runtime_state(profile.paths)
    evidence_state, evidence_issue = _task_start_repair_evidence(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        runtime_state=runtime_state,
        successor=attempt,
    )
    if evidence_state == "conflict" or evidence_state is None:
        raise _TaskStartProofConflict(
            evidence_issue or "reserved ordinary resume start evidence is not repairable"
        )
    try:
        _preflight_reserved_resume_workspace(profile, attempt=attempt)
        _preflight_reserved_handler_contract(profile, attempt=attempt)
    except WorktreeError as exc:
        raise _TaskStartProofConflict(str(exc)) from exc
    runtime_before = _read_bytes_if_present(profile.paths.runtime_file)
    events_before = _read_bytes_if_present(profile.paths.events_file)
    assert attempt.prompt_receipt is not None
    assert attempt.user_prompt_receipt is not None
    if resume_guard.start_kind == "resume":
        repaired_attempt = start_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor=attempt.actor,
            execution_model=attempt.execution_model,
            workspace_identity=attempt.workspace_identity,
            workspace_mode=attempt.workspace_mode,
            worktree_role=attempt.worktree_role,
            worktree_path=attempt.worktree_path,
            branch=attempt.branch,
            target_branch=attempt.target_branch,
            integration_branch=attempt.integration_branch,
            start_commit=attempt.start_commit,
            model=attempt.model,
            reasoning_effort=attempt.reasoning_effort,
            codex_session=attempt.codex_session,
            prompt_receipt=attempt.prompt_receipt,
            user_prompt_receipt=attempt.user_prompt_receipt,
            note=attempt.note,
            setup_receipt=attempt.setup_receipt,
            attempt_id=resume_guard.attempt_id,
            expected_predecessor_attempt_id=resume_guard.predecessor_attempt_id,
            atomic_start_kind="resume",
            expected_task_actor=resume_guard.task_actor,
            expected_execution_prompt_hash=resume_guard.execution_prompt_hash,
            expected_execution_prompt_mode=resume_guard.execution_prompt_mode,
            expected_request_prompt_hash=resume_guard.request_prompt_hash,
            expected_request_prompt_mode=resume_guard.request_prompt_mode,
            expected_task_updated_at=resume_guard.task_updated_at,
        )
    else:
        repaired_attempt = repair_task_start_events(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
        )
    primary_root, live_handlers, workspace_mutated = _ensure_reserved_resume_workspace(
        profile,
        attempt=repaired_attempt,
    )
    durable_handlers = _durable_start_handlers(profile, repaired_attempt)
    spec = WorktreeSpec(
        workset_id=workset_id,
        task_id=task_id,
        task_title=task.title,
        task_slug=_task_slug(workset_id, task),
        branch=str(repaired_attempt.branch),
        base_ref=str(start_receipt["base_ref"]),
        base_commit=str(start_receipt["base_commit"]),
        target_branch=str(repaired_attempt.target_branch),
        worktree_path=str(repaired_attempt.worktree_path),
        primary_worktree=str(primary_root),
        current_worktree=str(command_workspace_root(profile, cwd=cwd)),
        attempt_id=repaired_attempt.attempt_id,
        prompt_hash=repaired_attempt.prompt_receipt.prompt_hash,
        prompt_source=repaired_attempt.prompt_receipt.source,
        prompt_mode=repaired_attempt.prompt_receipt.mode,
        workspace_ve=live_handlers.worktree_ve_path,
        workspace_blackdog_path=live_handlers.blackdog_path,
        runtime_mode=live_handlers.runtime_mode,
        source_root=live_handlers.source_root,
        source_mode=live_handlers.source_mode,
        script_policy=live_handlers.script_policy,
        setup_receipt=dict(repaired_attempt.setup_receipt or {}),
        handlers=live_handlers,
        workspace_action="repaired",
        predecessor_attempt_id=resume_guard.predecessor_attempt_id or None,
    )
    append_event_once(
        profile.paths.events_file,
        event_id=(
            _ordinary_resume_start_event_id(repaired_attempt.attempt_id)
            if resume_guard.start_kind == "resume"
            else _initial_start_event_id(repaired_attempt.attempt_id)
        ),
        event_type="worktree.start",
        actor=repaired_attempt.actor,
        payload=_worktree_start_event_payload(
            spec=spec,
            attempt=repaired_attempt,
            handlers=durable_handlers,
        ),
    )
    if (
        not workspace_mutated
        and runtime_before == _read_bytes_if_present(profile.paths.runtime_file)
        and events_before == _read_bytes_if_present(profile.paths.events_file)
    ):
        spec = replace(spec, workspace_action="reused")
    return spec


def _unreserved_workspace_state(
    profile: RepoProfile,
    *,
    preview: WorktreePreview,
) -> tuple[str, str | None]:
    """Classify an exact pre-attempt workspace retained by a failed begin."""

    primary_root = Path(preview.primary_worktree).resolve()
    worktree_path = Path(preview.worktree_path).resolve()
    branch_ref = f"refs/heads/{preview.branch}"
    registration = _registered_worktree_row(primary_root, worktree_path)
    registered_for_branch = _find_worktree_for_branch(primary_root, branch_ref)
    branch = _run_git_no_check(
        primary_root,
        "show-ref",
        "--verify",
        "--quiet",
        branch_ref,
    )
    if branch.returncode not in {0, 1}:
        detail = branch.stderr.strip() or branch.stdout.strip() or str(branch.returncode)
        return "conflict", f"could not inspect retained task-begin branch: {detail}"
    branch_exists = branch.returncode == 0
    if registration is not None:
        exact = (
            registration.get("branch") == branch_ref
            and registration.get("HEAD") == preview.base_commit
            and registered_for_branch == worktree_path
            and worktree_path.exists()
            and _is_git_worktree_path(worktree_path)
            and branch_exists
            and _run_git(worktree_path, "rev-parse", "HEAD") == preview.base_commit
            and _run_git(primary_root, "rev-parse", preview.branch) == preview.base_commit
            and not _managed_status_dirty(profile, worktree_path)
            and _in_progress_git_operation(worktree_path) is None
        )
        return (
            ("workspace", None)
            if exact
            else (
                "conflict",
                "retained task-begin workspace conflicts with its exact branch/path/start commit",
            )
        )
    if registered_for_branch is not None:
        return (
            "conflict",
            "retained task-begin branch is registered at a different workspace",
        )
    if worktree_path.exists():
        return (
            "conflict",
            "retained task-begin path exists without its exact Git registration",
        )
    if branch_exists:
        if _run_git(primary_root, "rev-parse", preview.branch) != preview.base_commit:
            return (
                "conflict",
                "retained task-begin branch moved away from its exact start commit",
            )
        return "branch", None
    return "absent", None


def start_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    prompt: str,
    prompt_source: str | None = None,
    prompt_mode: str = PROMPT_MODE_RAW,
    execution_prompt_receipt: PromptReceiptRecord | None = None,
    user_prompt_receipt: PromptReceiptRecord | None = None,
    guard_receipt: dict[str, Any] | None = None,
    skill_provenance: dict[str, Any] | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    branch: str | None = None,
    from_ref: str | None = None,
    path: str | None = None,
    cwd: Path | None = None,
    note: str | None = None,
    resume_guard: _TaskResumeGuard | None = None,
) -> WorktreeSpec:
    _require_no_incomplete_close(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    codex_context = current_codex_runtime_context()
    resolved_model = model or codex_context.model
    resolved_reasoning_effort = reasoning_effort or codex_context.reasoning_effort
    candidate_execution_receipt = create_prompt_receipt(
        prompt,
        source=prompt_source,
        mode=prompt_mode,
    )
    if execution_prompt_receipt is not None and (
        execution_prompt_receipt.text != candidate_execution_receipt.text
        or execution_prompt_receipt.prompt_hash != candidate_execution_receipt.prompt_hash
        or execution_prompt_receipt.source != candidate_execution_receipt.source
        or execution_prompt_receipt.mode != candidate_execution_receipt.mode
    ):
        raise WorktreeError("task start execution prompt receipt does not match its prompt input")
    resolved_execution_receipt = execution_prompt_receipt or candidate_execution_receipt
    resolved_user_receipt = user_prompt_receipt or resolved_execution_receipt
    if resume_guard is not None and resume_guard.retry_reserved_successor:
        return _repair_reserved_resume_worktree(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            resume_guard=resume_guard,
            cwd=cwd,
            model=model,
            reasoning_effort=reasoning_effort,
            branch=branch,
            from_ref=from_ref,
            path=path,
            note=note,
        )
    resolved_guard_receipt = guard_receipt or _guard_task_start(
        profile,
        actor=actor,
        prompt_mode=prompt_mode,
        execution_receipt=resolved_execution_receipt,
        user_receipt=resolved_user_receipt,
    )
    preview = preview_task_worktree(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        actor=actor,
        prompt=prompt,
        prompt_source=prompt_source,
        prompt_mode=prompt_mode,
        model=resolved_model,
        reasoning_effort=resolved_reasoning_effort,
        branch=branch,
        from_ref=from_ref,
        path=path,
        cwd=cwd,
        note=note,
        include_prompt=False,
        expand_contract=False,
    )
    primary_root = Path(preview.primary_worktree).resolve()
    worktree_path = Path(preview.worktree_path).resolve()
    retained_workspace_state, retained_workspace_issue = _unreserved_workspace_state(
        profile,
        preview=preview,
    )
    retained_repair_authorized = (
        _reserved_auto_task_envelope_payload(
            profile,
            workset_id=workset_id,
            task_id=task_id,
        )
        is not None
    )
    if (
        retained_workspace_state in {"workspace", "branch"}
        and not retained_repair_authorized
    ):
        retained_workspace_state = "conflict"
    if retained_workspace_issue is not None:
        raise WorktreeError(retained_workspace_issue)
    if not preview.start_ready and retained_workspace_state != "workspace":
        raise WorktreeError("; ".join(preview.conflicts))
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if retained_workspace_state != "workspace":
        add_args = (
            ("worktree", "add", str(worktree_path), preview.branch)
            if retained_workspace_state == "branch"
            else (
                "worktree",
                "add",
                str(worktree_path),
                "-b",
                preview.branch,
                preview.base_ref,
            )
        )
        completed = _run_git_no_check(primary_root, *add_args)
        if completed.returncode != 0:
            after_state, after_issue = _unreserved_workspace_state(
                profile,
                preview=preview,
            )
            detail = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or f"exit code {completed.returncode}"
            )
            if after_state == "workspace" and after_issue is None:
                raise WorktreeError(
                    "git worktree add reported failure after retaining the exact task workspace: "
                    + detail
                )
            raise WorktreeError(f"git worktree add failed: {detail}")
    try:
        handlers = execute_worktree_handlers(profile, worktree_path=worktree_path)
        setup_receipt = _handler_setup_receipt(
            resolved_guard_receipt,
            handlers,
            skill_provenance=skill_provenance,
        )
        setup_receipt["worktree_start"] = _worktree_start_receipt(preview=preview)
        if not handlers.ready:
            blocked = [action.message for action in handlers.actions if action.status == "blocked"]
            detail = "; ".join(blocked)
            if handlers.remediation:
                detail = "; ".join(item for item in [detail, handlers.remediation] if item)
            raise WorktreeError(detail or "worktree handler execution did not produce a ready workspace")
        persisted_execution_receipt, persisted_user_receipt = persist_prompt_receipts(
            profile.paths.control_dir,
            (resolved_execution_receipt, resolved_user_receipt),
        )
        stored_execution_receipt = prompt_receipt_reference(persisted_execution_receipt)
        stored_user_receipt = prompt_receipt_reference(persisted_user_receipt)
        capture_user_prompt_hash = (
            stored_user_receipt.prompt_hash
            if stored_user_receipt is not None
            else stored_execution_receipt.prompt_hash
        )
        try:
            codex_session = current_codex_session_ref(
                user_prompt_hash=capture_user_prompt_hash,
                execution_prompt_hash=stored_execution_receipt.prompt_hash,
            )
        except Exception:
            # Codex invocation provenance is optional evidence. A capture
            # adapter failure must never roll back a ready task worktree.
            codex_session = (
                CodexSessionRefRecord(
                    thread_id=codex_context.thread_id,
                    session_path=codex_context.session_path,
                    user_prompt_hash=capture_user_prompt_hash,
                    execution_prompt_hash=stored_execution_receipt.prompt_hash,
                    capture_status=CODEX_CAPTURE_STATUS_MISSING,
                    capture_missing_reason=CODEX_CAPTURE_MISSING_REASON_CAPTURE_ERROR,
                )
                if codex_context.thread_id is not None
                else None
            )
        attempt = start_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor=actor,
            prompt_receipt=stored_execution_receipt,
            user_prompt_receipt=stored_user_receipt,
            workspace_identity=preview.workspace_identity,
            workspace_mode=WORKSPACE_MODE_GIT_WORKTREE,
            worktree_role=WORKTREE_ROLE_TASK,
            worktree_path=str(worktree_path),
            branch=preview.branch,
            target_branch=preview.target_branch,
            integration_branch=preview.integration_branch,
            start_commit=preview.base_commit,
            model=resolved_model,
            reasoning_effort=resolved_reasoning_effort,
            codex_session=codex_session,
            note=note,
            setup_receipt=setup_receipt,
            attempt_id=resume_guard.attempt_id if resume_guard is not None else None,
            expected_predecessor_attempt_id=(
                resume_guard.predecessor_attempt_id if resume_guard is not None else None
            ),
            atomic_start_kind="resume" if resume_guard is not None else None,
            expected_task_actor=(
                resume_guard.task_actor if resume_guard is not None else None
            ),
            expected_execution_prompt_hash=(
                resume_guard.execution_prompt_hash if resume_guard is not None else None
            ),
            expected_execution_prompt_mode=(
                resume_guard.execution_prompt_mode if resume_guard is not None else None
            ),
            expected_request_prompt_hash=(
                resume_guard.request_prompt_hash if resume_guard is not None else None
            ),
            expected_request_prompt_mode=(
                resume_guard.request_prompt_mode if resume_guard is not None else None
            ),
            expected_task_updated_at=(
                resume_guard.task_updated_at if resume_guard is not None else None
            ),
        )
    except Exception:
        reserved_successor = (
            _reserved_resume_attempt(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                resume_guard=resume_guard,
            )
            if resume_guard is not None
            else _reserved_initial_attempt(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                actor=actor,
                branch=preview.branch,
                worktree_path=worktree_path,
                start_commit=preview.base_commit,
                execution_receipt=resolved_execution_receipt,
                user_receipt=resolved_user_receipt,
            )
        )
        if reserved_successor is None:
            _run_git_no_check(primary_root, "worktree", "remove", "--force", str(worktree_path))
            _run_git_no_check(primary_root, "branch", "-D", preview.branch)
        raise
    durable_setup_receipt = attempt.setup_receipt or setup_receipt
    spec = WorktreeSpec(
        workset_id=preview.workset_id,
        task_id=preview.task_id,
        task_title=preview.task_title,
        task_slug=preview.task_slug,
        branch=preview.branch,
        base_ref=preview.base_ref,
        base_commit=preview.base_commit,
        target_branch=preview.target_branch,
        worktree_path=str(worktree_path),
        primary_worktree=preview.primary_worktree,
        current_worktree=preview.current_worktree,
        attempt_id=attempt.attempt_id,
        prompt_hash=preview.prompt_hash,
        prompt_source=preview.prompt_source,
        prompt_mode=preview.prompt_mode,
        workspace_ve=handlers.worktree_ve_path,
        workspace_blackdog_path=handlers.blackdog_path,
        runtime_mode=handlers.runtime_mode,
        source_root=handlers.source_root,
        source_mode=handlers.source_mode,
        script_policy=handlers.script_policy,
        setup_receipt=durable_setup_receipt,
        handlers=handlers,
    )
    if resume_guard is not None:
        append_event_once(
            profile.paths.events_file,
            event_id=_ordinary_resume_start_event_id(attempt.attempt_id),
            event_type="worktree.start",
            actor=actor,
            payload=_worktree_start_event_payload(
                spec=spec,
                attempt=attempt,
                handlers=_durable_start_handlers(profile, attempt),
            ),
        )
    else:
        append_event_once(
            profile.paths.events_file,
            event_id=_initial_start_event_id(attempt.attempt_id),
            event_type="worktree.start",
            actor=actor,
            payload=_worktree_start_event_payload(
                spec=spec,
                attempt=attempt,
                handlers=_durable_start_handlers(profile, attempt),
            ),
        )
    return spec


def _read_bytes_if_present(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _validate_workspace_adoption_lineage(
    *,
    predecessor: Any,
    actor: str,
    incoming_execution: PromptReceiptRecord,
    incoming_request: PromptReceiptRecord,
    expected_actor: str | None,
    expected_execution_prompt_hash: str | None,
    expected_execution_prompt_mode: str | None,
    expected_request_prompt_hash: str | None,
    expected_request_prompt_mode: str | None,
) -> None:
    expected_values = (
        expected_actor,
        expected_execution_prompt_hash,
        expected_execution_prompt_mode,
        expected_request_prompt_hash,
        expected_request_prompt_mode,
    )
    if any(not str(value or "").strip() for value in expected_values):
        raise BacklogError(
            "workspace adoption requires complete expected actor and prompt-lineage guards"
        )
    if predecessor.prompt_receipt is None or predecessor.user_prompt_receipt is None:
        raise BacklogError("workspace adoption predecessor is missing prompt lineage")
    for role, source in (
        ("execution", predecessor.prompt_receipt.source),
        ("request", predecessor.user_prompt_receipt.source),
    ):
        source_text = str(source or "").strip()
        if not source_text or source_text == "stdin" or source_text.startswith("inline:"):
            raise BacklogError(
                f"workspace adoption predecessor {role} prompt source is not replayable"
            )
    durable = (
        predecessor.actor,
        predecessor.prompt_receipt.prompt_hash,
        predecessor.prompt_receipt.source,
        predecessor.prompt_receipt.mode,
        predecessor.user_prompt_receipt.prompt_hash,
        predecessor.user_prompt_receipt.source,
        predecessor.user_prompt_receipt.mode,
    )
    supplied = (
        str(expected_actor),
        str(expected_execution_prompt_hash),
        predecessor.prompt_receipt.source,
        str(expected_execution_prompt_mode),
        str(expected_request_prompt_hash),
        predecessor.user_prompt_receipt.source,
        str(expected_request_prompt_mode),
    )
    incoming = (
        actor,
        incoming_execution.prompt_hash,
        incoming_execution.source,
        incoming_execution.mode,
        incoming_request.prompt_hash,
        incoming_request.source,
        incoming_request.mode,
    )
    if supplied != durable or incoming != durable:
        raise BacklogError(
            "workspace adoption actor or prompt lineage does not match the predecessor"
        )


def _workspace_adoption_start_event_payload(
    *,
    spec: WorktreeSpec,
    attempt: Any,
    proof: Mapping[str, Any],
    handlers: HandlerPlanSummary,
) -> dict[str, Any]:
    return {
        "workset_id": spec.workset_id,
        "task_id": spec.task_id,
        "attempt_id": spec.attempt_id,
        "branch": spec.branch,
        "target_branch": spec.target_branch,
        "base_ref": spec.base_ref,
        "base_commit": spec.base_commit,
        "worktree_path": spec.worktree_path,
        "prompt_hash": spec.prompt_hash,
        "prompt_source": spec.prompt_source,
        "prompt_mode": spec.prompt_mode,
        "user_prompt_hash": (
            attempt.user_prompt_receipt.prompt_hash
            if attempt.user_prompt_receipt is not None
            else spec.prompt_hash
        ),
        "user_prompt_source": (
            attempt.user_prompt_receipt.source
            if attempt.user_prompt_receipt is not None
            else spec.prompt_source
        ),
        "user_prompt_mode": (
            attempt.user_prompt_receipt.mode
            if attempt.user_prompt_receipt is not None
            else spec.prompt_mode
        ),
        "workspace_blackdog_path": handlers.blackdog_path,
        "runtime_mode": handlers.runtime_mode,
        "source_mode": handlers.source_mode,
        "script_policy": handlers.script_policy,
        "setup_receipt": attempt.setup_receipt,
        "model": attempt.model,
        "reasoning_effort": attempt.reasoning_effort,
        "codex_thread_id": attempt.codex_session.thread_id if attempt.codex_session is not None else None,
        "codex_session_path": attempt.codex_session.session_path if attempt.codex_session is not None else None,
        "handler_actions": [action.to_dict() for action in handlers.actions],
        "workspace_action": "adopted",
        "workspace_adoption": dict(proof),
    }


def _adopt_aborted_landing_source_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    incoming_execution: PromptReceiptRecord,
    incoming_request: PromptReceiptRecord,
    guard_receipt: dict[str, Any] | None = None,
    current_skill_provenance: dict[str, Any] | None,
    expected_actor: str | None,
    expected_execution_prompt_hash: str | None,
    expected_execution_prompt_mode: str | None,
    expected_request_prompt_hash: str | None,
    expected_request_prompt_mode: str | None,
    expected_predecessor_attempt: str,
    expected_landing_transaction: str,
    expected_source_commit: str,
    expected_source_tree: str,
    expected_branch: str,
    expected_path: str,
    expected_target_branch: str,
    expected_target_commit: str,
    cwd: Path | None,
    note: str | None,
) -> tuple[WorktreeSpec, bool]:
    workset, task = _require_workset_and_task(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    runtime_before = _read_bytes_if_present(profile.paths.runtime_file)
    events_before = _read_bytes_if_present(profile.paths.events_file)
    runtime_state = load_runtime_state(profile.paths)
    predecessor = find_task_attempt(runtime_state, workset_id, expected_predecessor_attempt)
    if predecessor is None or predecessor.task_id != task_id:
        raise BacklogError("workspace adoption predecessor does not exist in this task")
    transaction = load_landing_transaction(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=predecessor.attempt_id,
    )
    if transaction is None or transaction.transaction_id != expected_landing_transaction:
        raise LandingTransactionError(
            "workspace adoption transaction guard does not match the predecessor"
        )
    _validate_workspace_adoption_lineage(
        predecessor=predecessor,
        actor=actor,
        incoming_execution=incoming_execution,
        incoming_request=incoming_request,
        expected_actor=expected_actor,
        expected_execution_prompt_hash=expected_execution_prompt_hash,
        expected_execution_prompt_mode=expected_execution_prompt_mode,
        expected_request_prompt_hash=expected_request_prompt_hash,
        expected_request_prompt_mode=expected_request_prompt_mode,
    )
    if note is not None and note != predecessor.note:
        raise BacklogError("workspace adoption cannot replace the predecessor note")

    expected_proof = {
        "predecessor_attempt_id": expected_predecessor_attempt,
        "abort_transaction_id": expected_landing_transaction,
        "source_commit": expected_source_commit,
        "source_tree_hash": expected_source_tree,
        "branch": expected_branch,
        "worktree_path": str(Path(expected_path).resolve()),
        "target_branch": expected_target_branch,
        "target_commit_at_adoption": expected_target_commit,
    }
    adoption_id = _workspace_adoption_id(transaction.transaction_id)
    successor_attempt_id = _workspace_adoption_successor_id(
        task_id=task_id,
        adoption_id=adoption_id,
    )
    attempts = _task_attempts_in_append_order(
        runtime_state,
        workset_id=workset_id,
        task_id=task_id,
    )
    latest = attempts[-1] if attempts else None
    retry_successor = (
        latest
        if latest is not None and latest.attempt_id == successor_attempt_id
        else None
    )
    if retry_successor is not None:
        proof = _workspace_adoption_receipt(retry_successor)
        if proof is None:
            raise BacklogError("active workspace adoption successor has invalid durable proof")
        verified_source = _verify_landing_abort_chain(
            profile,
            transaction=transaction,
            require_source=False,
        )
        derived_proof = _derive_workspace_adoption_receipt(
            predecessor=predecessor,
            transaction=transaction,
            target_commit_at_adoption=str(retry_successor.start_commit or ""),
        )
        if (
            verified_source != derived_proof["source_commit"]
            or not strict_json_equal(proof, derived_proof)
        ):
            raise BacklogError(
                "active workspace adoption successor conflicts with immutable predecessor proof"
            )
        receipt_projection = {
            "predecessor_attempt_id": proof["predecessor_attempt_id"],
            "abort_transaction_id": proof["abort_transaction_id"],
            "source_commit": proof["source_commit"],
            "source_tree_hash": proof["source_tree_hash"],
            "branch": proof["branch"],
            "worktree_path": proof["worktree_path"],
            "target_branch": proof["target_branch"],
            "target_commit_at_adoption": proof["target_commit_at_adoption"],
        }
        if not strict_json_equal(receipt_projection, expected_proof):
            raise BacklogError("workspace adoption retry conflicts with its durable proof")
        source_path = Path(proof["worktree_path"])
        registration = _registered_worktree_row(Path(transaction.intent.primary_worktree), source_path)
        if (
            retry_successor.status != ATTEMPT_STATUS_IN_PROGRESS
            or retry_successor.ended_at is not None
            or registration is None
            or registration.get("branch") != f"refs/heads/{proof['branch']}"
            or str(registration.get("HEAD") or "").strip() != proof["source_commit"]
            or _run_git(source_path, "rev-parse", "HEAD") != proof["source_commit"]
            or _managed_status_dirty(profile, source_path)
            or _in_progress_git_operation(source_path) is not None
        ):
            raise BacklogError("workspace adoption retry conflicts with the active successor")
        setup_receipt = retry_successor.setup_receipt
        assert setup_receipt is not None
        start_state, start_issue = _workspace_adoption_start_evidence(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
            successor=retry_successor,
            predecessor=predecessor,
            receipt=proof,
            transaction=transaction,
        )
        if start_state == "conflict":
            raise BacklogError(
                start_issue or "workspace adoption retry has conflicting start evidence"
            )
        handlers = _workspace_adoption_handlers_from_setup(profile, retry_successor)
    else:
        if latest is None or latest.attempt_id != predecessor.attempt_id:
            raise BacklogError("workspace adoption predecessor is no longer latest")
        proof = _prove_aborted_landing_source_adoption(
            profile,
            predecessor=predecessor,
            transaction=transaction,
            runtime_state=runtime_state,
            expected=expected_proof,
            allow_canceled=False,
        )
        predecessor_skill = _bounded_skill_provenance(predecessor.setup_receipt)
        if predecessor.prompt_receipt is not None and predecessor.prompt_receipt.mode == PROMPT_MODE_SKILL:
            if predecessor_skill is None or current_skill_provenance != predecessor_skill:
                raise BacklogError(
                    "workspace adoption managed-skill provenance does not match the predecessor"
                )
        elif current_skill_provenance is not None:
            raise BacklogError("workspace adoption received unexpected managed-skill provenance")
        source_path = Path(proof["worktree_path"])
        handlers = validate_existing_worktree_handlers(profile, worktree_path=source_path)
        if not handlers.ready:
            raise WorktreeError(
                handlers.remediation or "retained workspace handler validation failed"
            )
        if guard_receipt is None:
            raise BacklogError(
                "workspace adoption requires repository guard evidence before prompt persistence"
            )
        setup_receipt = _handler_setup_receipt(
            guard_receipt,
            handlers,
            skill_provenance=predecessor_skill,
        )
        setup_receipt["workspace_adoption"] = dict(proof)

        # This is the cross-store ownership boundary.  Re-read target as the
        # final operation before reserving the deterministic successor.  A
        # candidate that arrived before this point still belongs to the
        # predecessor reconciliation path; containment observed after the
        # runtime reservation belongs to successor completion.
        current_target, contains_candidate = _landing_abort_target_state(
            intent=transaction.intent,
            landed_commit=str(proof["canonical_candidate"]),
        )
        if (
            current_target != proof["target_commit_at_adoption"]
            or contains_candidate
        ):
            raise _WorkspaceAdoptionTargetChanged(
                candidate_contained=contains_candidate
            )

    if not predecessor.ended_at:
        raise BacklogError("workspace adoption predecessor task state is missing its runtime generation")
    if retry_successor is not None:
        # The guarded proof above established semantic lineage and verified any
        # recorded artifacts.  Reuse the exact stored references so the core
        # deterministic retry remains byte-for-byte idempotent.
        successor_execution_receipt = retry_successor.prompt_receipt
        successor_request_receipt = retry_successor.user_prompt_receipt
    else:
        successor_execution_receipt = predecessor.prompt_receipt
        successor_request_receipt = predecessor.user_prompt_receipt
        successor_execution_receipt = replace(
            successor_execution_receipt,
            replay_artifact_path=(
                successor_execution_receipt.replay_artifact_path
                or incoming_execution.replay_artifact_path
            ),
        )
        successor_request_receipt = replace(
            successor_request_receipt,
            replay_artifact_path=(
                successor_request_receipt.replay_artifact_path
                or incoming_request.replay_artifact_path
            ),
        )
    attempt = start_task(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        actor=predecessor.actor,
        workspace_identity=predecessor.workspace_identity,
        workspace_mode=WORKSPACE_MODE_GIT_WORKTREE,
        worktree_role=WORKTREE_ROLE_TASK,
        worktree_path=str(proof["worktree_path"]),
        branch=str(proof["branch"]),
        target_branch=str(proof["target_branch"]),
        integration_branch=predecessor.integration_branch,
        start_commit=str(proof["target_commit_at_adoption"]),
        model=predecessor.model,
        reasoning_effort=predecessor.reasoning_effort,
        codex_session=predecessor.codex_session,
        prompt_receipt=successor_execution_receipt,
        user_prompt_receipt=successor_request_receipt,
        note=predecessor.note,
        setup_receipt=setup_receipt,
        attempt_id=successor_attempt_id,
        expected_predecessor_attempt_id=predecessor.attempt_id,
        atomic_start_kind="adoption",
        expected_task_actor=predecessor.actor,
        expected_execution_prompt_hash=predecessor.prompt_receipt.prompt_hash,
        expected_execution_prompt_mode=predecessor.prompt_receipt.mode,
        expected_request_prompt_hash=predecessor.user_prompt_receipt.prompt_hash,
        expected_request_prompt_mode=predecessor.user_prompt_receipt.mode,
        expected_task_updated_at=predecessor.ended_at,
    )
    source_path = Path(str(proof["worktree_path"]))
    spec = WorktreeSpec(
        workset_id=workset_id,
        task_id=task_id,
        task_title=task.title,
        task_slug=_task_slug(workset_id, task),
        branch=str(proof["branch"]),
        base_ref=str(proof["target_branch"]),
        base_commit=str(proof["target_commit_at_adoption"]),
        target_branch=str(proof["target_branch"]),
        worktree_path=str(source_path),
        primary_worktree=transaction.intent.primary_worktree,
        current_worktree=str(command_workspace_root(profile, cwd=cwd)),
        attempt_id=attempt.attempt_id,
        prompt_hash=attempt.prompt_receipt.prompt_hash if attempt.prompt_receipt is not None else "",
        prompt_source=attempt.prompt_receipt.source if attempt.prompt_receipt is not None else None,
        prompt_mode=attempt.prompt_receipt.mode if attempt.prompt_receipt is not None else None,
        workspace_ve=handlers.worktree_ve_path,
        workspace_blackdog_path=handlers.blackdog_path,
        runtime_mode=handlers.runtime_mode,
        source_root=handlers.source_root,
        source_mode=handlers.source_mode,
        script_policy=handlers.script_policy,
        setup_receipt=dict(attempt.setup_receipt or {}),
        handlers=handlers,
        workspace_action="adopted",
        predecessor_attempt_id=predecessor.attempt_id,
    )
    append_event_once(
        profile.paths.events_file,
        event_id=_workspace_adoption_start_event_id(attempt.attempt_id),
        event_type="worktree.start",
        actor=predecessor.actor,
        payload=_workspace_adoption_start_event_payload(
            spec=spec,
            attempt=attempt,
            proof=proof,
            handlers=handlers,
        ),
    )
    mutated = (
        runtime_before != _read_bytes_if_present(profile.paths.runtime_file)
        or events_before != _read_bytes_if_present(profile.paths.events_file)
    )
    return spec, mutated


def task_begin_preflight_result(
    error: TaskBeginPreflightError,
    *,
    actor: str,
    prompt_mode: str,
    workset_id: str | None = None,
    task_id: str | None = None,
) -> OperationResult:
    """Render a setup refusal without writing any Blackdog or Git state."""

    next_action = NextAction.terminal(
        action_id=error.action_id,
        kind="blocked",
        disposition="input_required",
        reason_code=error.reason_code,
        reason_detail=str(error),
        display=error.display,
        required_inputs=error.required_inputs,
    )
    return OperationResult(
        operation="task.begin",
        operation_status="blocked",
        task_status=None,
        attempt_status=None,
        disposition=next_action.disposition,
        mutation_started=False,
        mutation_completed=False,
        mutation_phase="none",
        failure_code=error.failure_code,
        next_action=next_action,
        legacy_payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "actor": actor,
            "created_workset": False,
            "prompt_mode": prompt_mode,
            "user_prompt_hash": None,
            "user_prompt_source": None,
            "execution_prompt_hash": None,
            "execution_prompt_source": None,
            "execution_prompt_text": None,
            "worktree": None,
            "error": str(error),
            "recommended_actions": [],
            "recommended_commands": [],
        },
    )


def _task_begin_start_proof_conflict_result(
    profile: RepoProfile,
    *,
    error: _TaskStartProofConflict,
    workset_id: str,
    task_id: str,
    actor: str,
    prompt_mode: str,
    user_receipt: PromptReceiptRecord,
    execution_receipt: PromptReceiptRecord,
) -> OperationResult:
    """Render a durable-lineage conflict without reserving or repairing anything."""

    runtime_state = load_runtime_state(profile.paths)
    attempt = latest_task_attempt(runtime_state, workset_id, task_id)
    task_state = task_state_index(runtime_state, workset_id).get(task_id)
    next_action = NextAction.terminal(
        action_id="task_start_proof_required",
        kind="blocked",
        disposition="repair_required",
        reason_code="task_start_evidence_conflict",
        reason_detail=str(error),
        display="Repair task-start proof",
        required_inputs=("canonical_resume_start_evidence",),
    )
    payload = {
        "workset_id": workset_id,
        "task_id": task_id,
        "actor": actor,
        "created_workset": False,
        "prompt_mode": prompt_mode,
        "user_prompt_hash": user_receipt.prompt_hash,
        "user_prompt_source": user_receipt.source,
        "user_prompt_replay_artifact_path": user_receipt.replay_artifact_path,
        "execution_prompt_hash": execution_receipt.prompt_hash,
        "execution_prompt_source": execution_receipt.source,
        "execution_prompt_replay_artifact_path": execution_receipt.replay_artifact_path,
        "execution_prompt_text": None,
        "worktree": None,
        "error": str(error),
        "error_type": type(error).__name__,
        "next_action": next_action.to_dict(),
        "recommended_commands": [],
        "recommended_actions": [next_action.display],
    }
    return OperationResult(
        operation="task.begin",
        operation_status="blocked",
        task_status=task_state.status if task_state is not None else None,
        attempt_status=attempt.status if attempt is not None else None,
        disposition=next_action.disposition,
        mutation_started=False,
        mutation_completed=False,
        mutation_phase="none",
        failure_code=None,
        next_action=next_action,
        legacy_payload=payload,
    )


def _task_begin_partial_after_reservation(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    created_workset: bool,
    retained_envelope: bool,
    prompt_mode: str,
    user_receipt: PromptReceiptRecord,
    execution_receipt: PromptReceiptRecord,
    prior_attempt_id: str | None,
    mutation_observed: bool,
    error: Exception,
    include_prompt: bool,
    cwd: Path | None,
    branch: str | None,
    from_ref: str | None,
    path: str | None,
    model: str | None,
    reasoning_effort: str | None,
    note: str | None,
) -> OperationResult | None:
    """Return a typed partial result only for an attempt reserved by this call."""

    attempt = latest_task_attempt(
        load_runtime_state(profile.paths),
        workset_id,
        task_id,
    )
    if attempt is None and retained_envelope:
        planning = load_planning_state(profile.paths)
        created = next(
            (
                task
                for workset in planning.worksets
                if workset.workset_id == workset_id
                for task in workset.tasks
                if task.task_id == task_id
            ),
            None,
        )
        if created is None:
            return None
        try:
            retained_preview = preview_task_worktree(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                actor=actor,
                prompt=execution_receipt.text,
                prompt_source=execution_receipt.source,
                prompt_mode=prompt_mode,
                model=model,
                reasoning_effort=reasoning_effort,
                branch=branch,
                from_ref=from_ref,
                path=path,
                cwd=cwd,
                note=note,
                include_prompt=False,
                expand_contract=False,
            )
            retained_workspace_state, retained_workspace_issue = (
                _unreserved_workspace_state(profile, preview=retained_preview)
            )
            if retained_workspace_state in {"absent", "branch"} and not retained_preview.start_ready:
                retained_workspace_state = "conflict"
                retained_workspace_issue = (
                    "retained task-begin identity has additional start blockers: "
                    + "; ".join(retained_preview.conflicts)
                )
            elif retained_workspace_state == "workspace":
                ignorable_conflicts = {
                    f"branch already has a worktree: {retained_preview.worktree_path}",
                    f"worktree path already exists: {retained_preview.worktree_path}",
                }
                unexpected_conflicts = tuple(
                    conflict
                    for conflict in retained_preview.conflicts
                    if conflict not in ignorable_conflicts
                )
                if not retained_preview.handlers.ready or unexpected_conflicts:
                    retained_workspace_state = "conflict"
                    retained_workspace_issue = (
                        "retained task-begin workspace has additional start blockers: "
                        + "; ".join(unexpected_conflicts or retained_preview.conflicts)
                    )
        except (BacklogError, WorktreeError) as exc:
            retained_preview = None
            retained_workspace_state = "conflict"
            retained_workspace_issue = str(exc)
        if retained_workspace_state not in {"absent", "branch", "workspace"}:
            next_action = NextAction.terminal(
                action_id="task_start_workspace_proof_required",
                kind="blocked",
                disposition="proof_required",
                reason_code="retained_task_workspace_identity_conflict",
                reason_detail=(
                    retained_workspace_issue
                    or "The retained task workspace no longer has one exact owned Git identity."
                ),
                display="Prove or clean up the retained task workspace",
                required_inputs=("exact_task_workspace_ownership",),
            )
            payload = {
                "workset_id": workset_id,
                "task_id": task_id,
                "actor": actor,
                "created_workset": created_workset,
                "prompt_mode": prompt_mode,
                "user_prompt_hash": user_receipt.prompt_hash,
                "user_prompt_source": user_receipt.source,
                "user_prompt_replay_artifact_path": user_receipt.replay_artifact_path,
                "execution_prompt_hash": execution_receipt.prompt_hash,
                "execution_prompt_source": execution_receipt.source,
                "execution_prompt_replay_artifact_path": execution_receipt.replay_artifact_path,
                "execution_prompt_text": execution_receipt.text if include_prompt else None,
                "worktree": None,
                "retained_workspace_state": retained_workspace_state,
                "retained_workspace_issue": retained_workspace_issue,
                "retained_workspace_identity": (
                    {
                        "branch": retained_preview.branch,
                        "base_commit": retained_preview.base_commit,
                        "worktree_path": retained_preview.worktree_path,
                    }
                    if retained_preview is not None
                    else None
                ),
                "error": str(error),
                "error_type": type(error).__name__,
                "next_action": next_action.to_dict(),
                "recommended_commands": [],
                "recommended_actions": [next_action.display],
            }
            return observe_operation_result(
                profile,
                OperationResult(
                    operation="task.begin",
                    operation_status="blocked",
                    task_status=None,
                    attempt_status=None,
                    disposition=next_action.disposition,
                    mutation_started=bool(created_workset or mutation_observed),
                    mutation_completed=False,
                    mutation_phase=(
                        "git_prepared"
                        if created_workset or mutation_observed
                        else "none"
                    ),
                    failure_code=None,
                    next_action=next_action,
                    legacy_payload=payload,
                ),
            )
        if (
            execution_receipt.replay_artifact_path is None
            or user_receipt.replay_artifact_path is None
        ):
            return None
        execution_prompt_path = (
            profile.paths.control_dir / execution_receipt.replay_artifact_path
        ).resolve(strict=False)
        request_prompt_path = (
            profile.paths.control_dir / user_receipt.replay_artifact_path
        ).resolve(strict=False)
        argv = [
            _lifecycle_blackdog_executable(profile, {}),
            "task",
            "begin",
            f"--project-root={profile.paths.project_root}",
            f"--workset={workset_id}",
            f"--task={task_id}",
            f"--actor={actor}",
            f"--execution-prompt-file={execution_prompt_path}",
            f"--prompt-mode={prompt_mode}",
        ]
        request_semantics = (
            user_receipt.prompt_hash,
            user_receipt.mode,
            user_receipt.source,
            user_receipt.replay_artifact_path,
        )
        execution_semantics = (
            execution_receipt.prompt_hash,
            execution_receipt.mode,
            execution_receipt.source,
            execution_receipt.replay_artifact_path,
        )
        if request_semantics != execution_semantics:
            argv.append(f"--request-file={request_prompt_path}")
        for flag, value in (
            ("--branch", branch),
            ("--from", from_ref),
            ("--path", path),
            ("--model", model),
            ("--reasoning-effort", reasoning_effort),
            ("--note", note),
        ):
            if value is not None:
                argv.append(f"{flag}={value}")
        action = LifecycleAction(
            action_id="retry_reserved_task_begin",
            disposition="retryable",
            reason_code="task_envelope_reserved_before_attempt",
            reason_detail=(
                "The task envelope and prompt artifacts were reserved, but no attempt was created."
            ),
            argv=tuple(argv),
            safety_class="validated_mutation",
            mutation_class="git_and_runtime",
            display="Retry the reserved task begin",
        )
        next_action = NextAction.command(action)
        payload = {
            "workset_id": workset_id,
            "task_id": task_id,
            "actor": actor,
            "created_workset": True,
            "prompt_mode": prompt_mode,
            "user_prompt_hash": user_receipt.prompt_hash,
            "user_prompt_source": user_receipt.source,
            "user_prompt_replay_artifact_path": user_receipt.replay_artifact_path,
            "execution_prompt_hash": execution_receipt.prompt_hash,
            "execution_prompt_source": execution_receipt.source,
            "execution_prompt_replay_artifact_path": execution_receipt.replay_artifact_path,
            "execution_prompt_text": execution_receipt.text if include_prompt else None,
            "worktree": None,
            "retained_workspace_state": retained_workspace_state,
            "retained_workspace_issue": retained_workspace_issue,
            "error": str(error),
            "error_type": type(error).__name__,
            "next_action": next_action.to_dict(),
            "recommended_commands": [action.to_dict()],
            "recommended_actions": [action.display],
        }
        return observe_operation_result(
            profile,
            OperationResult(
                operation="task.begin",
                operation_status="partial",
                task_status=None,
                attempt_status=None,
                disposition=next_action.disposition,
                mutation_started=True,
                mutation_completed=False,
                mutation_phase="preflight",
                failure_code=FAILURE_CLASS_UNKNOWN,
                next_action=next_action,
                legacy_payload=payload,
            ),
        )
    if (
        attempt is None
        or (attempt.attempt_id == prior_attempt_id and not mutation_observed)
        or attempt.status != ATTEMPT_STATUS_IN_PROGRESS
        or attempt.ended_at is not None
        or attempt.actor != actor
        or attempt.prompt_receipt is None
        or attempt.user_prompt_receipt is None
        or (
            attempt.prompt_receipt.prompt_hash,
            attempt.prompt_receipt.mode,
            attempt.user_prompt_receipt.prompt_hash,
            attempt.user_prompt_receipt.mode,
        )
        != (
            execution_receipt.prompt_hash,
            execution_receipt.mode,
            user_receipt.prompt_hash,
            user_receipt.mode,
        )
    ):
        return None
    state_payload = _task_recovery_payload(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    next_action = decide_next_action(_lifecycle_context(profile, state_payload))
    start_evidence_complete = not bool(state_payload.get("resume_start_incomplete")) and not bool(
        state_payload.get("resume_start_issue_code")
    )
    payload = {
        "workset_id": workset_id,
        "task_id": task_id,
        "actor": actor,
        "created_workset": created_workset,
        "prompt_mode": prompt_mode,
        "user_prompt_hash": user_receipt.prompt_hash,
        "user_prompt_source": user_receipt.source,
        "user_prompt_replay_artifact_path": user_receipt.replay_artifact_path,
        "execution_prompt_hash": execution_receipt.prompt_hash,
        "execution_prompt_source": execution_receipt.source,
        "execution_prompt_replay_artifact_path": execution_receipt.replay_artifact_path,
        "execution_prompt_text": execution_receipt.text if include_prompt else None,
        "worktree": {
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt.attempt_id,
            "branch": attempt.branch,
            "target_branch": attempt.target_branch,
            "base_commit": attempt.start_commit,
            "worktree_path": attempt.worktree_path,
            "workspace_action": "reserved",
        },
        "error": str(error),
        "error_type": type(error).__name__,
        "next_action": next_action.to_dict(),
        "recommended_commands": list(state_payload["recommended_commands"]),
        "recommended_actions": _task_surface_actions(
            list(state_payload["recommended_actions"])
        ),
    }
    return observe_operation_result(
        profile,
        OperationResult(
            operation="task.begin",
            operation_status="partial",
            task_status=state_payload.get("task_runtime_status"),
            attempt_status=state_payload.get("latest_attempt_status"),
            disposition=next_action.disposition,
            mutation_started=True,
            mutation_completed=start_evidence_complete,
            mutation_phase="workspace_started",
            failure_code=FAILURE_CLASS_UNKNOWN,
            next_action=next_action,
            legacy_payload=payload,
        ),
    )


def _task_begin_side_effect_fingerprint(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
) -> str:
    """Bounded runtime/event/Git/handler-output identity for begin error reporting."""

    attempt = latest_task_attempt(
        load_runtime_state(profile.paths),
        workset_id,
        task_id,
    )
    observed_paths: set[str] = set()
    if attempt is not None:
        if attempt.worktree_path:
            observed_paths.add(str(attempt.worktree_path))
        setup = attempt.setup_receipt
        probes = setup.get("probes") if isinstance(setup, Mapping) else None
        if isinstance(probes, list):
            for probe in probes:
                target = probe.get("target_path") if isinstance(probe, Mapping) else None
                if isinstance(target, str) and target:
                    observed_paths.add(target)

    def path_marker(value: str) -> tuple[Any, ...]:
        candidate = Path(value)
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            return (value, "missing", type(exc).__name__)
        link_target = None
        if stat.S_ISLNK(metadata.st_mode):
            try:
                link_target = os.readlink(candidate)
            except OSError:
                link_target = "<unreadable>"
        return (
            value,
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            link_target,
        )

    primary_root = find_primary_worktree(profile.paths.project_root)
    worktrees = _run_git_no_check(primary_root, "worktree", "list", "--porcelain")
    branches = _run_git_no_check(primary_root, "show-ref", "--heads")
    material = (
        _read_bytes_if_present(profile.paths.runtime_file),
        _read_bytes_if_present(profile.paths.events_file),
        worktrees.returncode,
        worktrees.stdout,
        worktrees.stderr,
        branches.returncode,
        branches.stdout,
        branches.stderr,
        tuple(path_marker(value) for value in sorted(observed_paths)),
    )
    return hashlib.sha256(repr(material).encode("utf-8")).hexdigest()


def begin_task_worktree(
    profile: RepoProfile,
    *,
    actor: str,
    prompt: str,
    prompt_source: str | None = None,
    user_prompt: str | None = None,
    user_prompt_source: str | None = None,
    prompt_mode: str = PROMPT_MODE_RAW,
    expected_actor: str | None = None,
    expected_execution_prompt_hash: str | None = None,
    expected_execution_prompt_mode: str | None = None,
    expected_request_prompt_hash: str | None = None,
    expected_request_prompt_mode: str | None = None,
    adopt_aborted_landing_source: bool = False,
    expected_predecessor_attempt: str | None = None,
    expected_landing_transaction: str | None = None,
    expected_source_commit: str | None = None,
    expected_source_tree: str | None = None,
    expected_branch: str | None = None,
    expected_path: str | None = None,
    expected_target_branch: str | None = None,
    expected_target_commit: str | None = None,
    workset_id: str | None = None,
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
    resolved_workset = str(workset_id or "").strip() or None
    resolved_task = str(task_id or "").strip() or None
    adoption_guards = {
        "expected_predecessor_attempt": str(expected_predecessor_attempt or "").strip(),
        "expected_landing_transaction": str(expected_landing_transaction or "").strip(),
        "expected_source_commit": str(expected_source_commit or "").strip(),
        "expected_source_tree": str(expected_source_tree or "").strip(),
        "expected_branch": str(expected_branch or "").strip(),
        "expected_path": str(expected_path or "").strip(),
        "expected_target_branch": str(expected_target_branch or "").strip(),
        "expected_target_commit": str(expected_target_commit or "").strip(),
    }
    if prompt_mode not in {PROMPT_MODE_RAW, PROMPT_MODE_SKILL, PROMPT_MODE_TUNED}:
        raise BacklogError(f"prompt mode must be one of {PROMPT_MODE_RAW}, {PROMPT_MODE_SKILL}, {PROMPT_MODE_TUNED}")
    if (resolved_workset is None) != (resolved_task is None):
        raise BacklogError(
            "task begin received only one of --workset/--task. For new work, omit both flags; "
            "to target existing planning state, provide both."
        )
    if adopt_aborted_landing_source:
        if resolved_workset is None or resolved_task is None:
            raise BacklogError(
                "workspace adoption requires the existing --workset and --task envelope"
            )
        missing_guards = [name for name, value in adoption_guards.items() if not value]
        if missing_guards:
            raise BacklogError(
                "workspace adoption requires complete proof guards: "
                + ", ".join(missing_guards)
            )
        if any(value is not None for value in (title, model, reasoning_effort, branch, from_ref, path)):
            raise BacklogError(
                "workspace adoption cannot override title, model, reasoning, branch, base, or path"
            )
    elif any(adoption_guards.values()):
        raise BacklogError(
            "workspace adoption proof guards require --adopt-aborted-landing-source"
        )

    if resolved_workset is not None and resolved_task is not None:
        close_gate = _incomplete_close_gate(
            profile,
            operation="task.begin",
            workset_id=resolved_workset,
            task_id=resolved_task,
            actor=actor,
            legacy_updates={
                "created_workset": False,
                "prompt_mode": prompt_mode,
                "worktree": None,
            },
        )
        if close_gate is not None:
            return close_gate
        stale_gate = _workset_stale_claim_release_gate(
            profile,
            operation="task.begin",
            workset_id=resolved_workset,
            task_id=resolved_task,
            actor=actor,
            legacy_updates={
                "created_workset": False,
                "prompt_mode": prompt_mode,
                "worktree": None,
            },
        )
        if stale_gate is not None:
            return stale_gate
        pending_transition, _pending_action = _pending_task_runtime_transition_action(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
        )
        if pending_transition is not None:
            return _recoverable_task_blocked_result(
                profile,
                operation="task.begin",
                workset_id=resolved_workset,
                task_id=resolved_task,
                actor=actor,
                legacy_updates={
                    "created_workset": False,
                    "prompt_mode": prompt_mode,
                    "worktree": None,
                },
            )

    skill_provenance = None
    if prompt_mode == PROMPT_MODE_SKILL:
        skill_provenance = _managed_skill_provenance(
            profile,
            workspace_root=command_workspace_root(profile, cwd=cwd),
        )

    user_receipt, execution_receipt = _resolve_task_begin_prompts(
        profile,
        prompt=prompt,
        prompt_source=prompt_source,
        user_prompt=user_prompt,
        user_prompt_source=user_prompt_source,
        prompt_mode=prompt_mode,
    )
    if adopt_aborted_landing_source:
        assert resolved_workset is not None
        assert resolved_task is not None
        predecessor_attempt_id = adoption_guards["expected_predecessor_attempt"]
        predecessor = find_task_attempt(
            load_runtime_state(profile.paths),
            resolved_workset,
            predecessor_attempt_id,
        )
        if predecessor is None or predecessor.task_id != resolved_task:
            raise BacklogError("workspace adoption predecessor does not exist in this task")
        predecessor_transaction = load_landing_transaction(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            attempt_id=predecessor.attempt_id,
        )
        if predecessor_transaction is None:
            raise BacklogError("workspace adoption predecessor has no landing transaction")
        expected_successor_id = _workspace_adoption_successor_id(
            task_id=resolved_task,
            adoption_id=_workspace_adoption_id(predecessor_transaction.transaction_id),
        )
        latest_before_adoption = latest_task_attempt(
            load_runtime_state(profile.paths),
            resolved_workset,
            resolved_task,
        )
        adoption_guard_receipt = None
        if (
            latest_before_adoption is None
            or latest_before_adoption.attempt_id != expected_successor_id
        ):
            adoption_guard_receipt = _guard_task_start(
                profile,
                actor=actor,
                prompt_mode=prompt_mode,
                execution_receipt=execution_receipt,
                user_receipt=user_receipt,
            )
        if predecessor.prompt_receipt is not None:
            _verify_recorded_prompt_artifact(
                profile,
                role="execution",
                prompt_hash=predecessor.prompt_receipt.prompt_hash,
                replay_artifact_path=predecessor.prompt_receipt.replay_artifact_path,
            )
        if predecessor.user_prompt_receipt is not None:
            _verify_recorded_prompt_artifact(
                profile,
                role="request",
                prompt_hash=predecessor.user_prompt_receipt.prompt_hash,
                replay_artifact_path=predecessor.user_prompt_receipt.replay_artifact_path,
            )
        user_receipt, execution_receipt = persist_prompt_receipts(
            profile.paths.control_dir,
            (user_receipt, execution_receipt),
        )
        with attempt_lifecycle_lock(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            attempt_id=predecessor_attempt_id,
        ):
            try:
                spec, mutated = _adopt_aborted_landing_source_worktree(
                    profile,
                    workset_id=resolved_workset,
                    task_id=resolved_task,
                    actor=actor,
                    incoming_execution=execution_receipt,
                    incoming_request=user_receipt,
                    guard_receipt=adoption_guard_receipt,
                    current_skill_provenance=skill_provenance,
                    expected_actor=expected_actor,
                    expected_execution_prompt_hash=expected_execution_prompt_hash,
                    expected_execution_prompt_mode=expected_execution_prompt_mode,
                    expected_request_prompt_hash=expected_request_prompt_hash,
                    expected_request_prompt_mode=expected_request_prompt_mode,
                    expected_predecessor_attempt=predecessor_attempt_id,
                    expected_landing_transaction=adoption_guards["expected_landing_transaction"],
                    expected_source_commit=adoption_guards["expected_source_commit"],
                    expected_source_tree=adoption_guards["expected_source_tree"],
                    expected_branch=adoption_guards["expected_branch"],
                    expected_path=adoption_guards["expected_path"],
                    expected_target_branch=adoption_guards["expected_target_branch"],
                    expected_target_commit=adoption_guards["expected_target_commit"],
                    cwd=cwd,
                    note=note,
                )
            except _WorkspaceAdoptionTargetChanged as race:
                state_payload = _task_recovery_payload(
                    profile,
                    workset_id=resolved_workset,
                    task_id=resolved_task,
                )
                next_action = decide_next_action(
                    _lifecycle_context(profile, state_payload)
                )
                payload = {
                    "workset_id": resolved_workset,
                    "task_id": resolved_task,
                    "actor": actor,
                    "created_workset": False,
                    "prompt_mode": prompt_mode,
                    "user_prompt_hash": user_receipt.prompt_hash,
                    "user_prompt_source": user_receipt.source,
                    "user_prompt_replay_artifact_path": user_receipt.replay_artifact_path,
                    "execution_prompt_hash": execution_receipt.prompt_hash,
                    "execution_prompt_source": execution_receipt.source,
                    "execution_prompt_replay_artifact_path": execution_receipt.replay_artifact_path,
                    "execution_prompt_text": (
                        execution_receipt.text if include_prompt else None
                    ),
                    "worktree": None,
                    "workspace_adoption_boundary": {
                        "outcome": "target_changed_before_successor_reservation",
                        "candidate_contained": race.candidate_contained,
                    },
                    "recommended_commands": list(
                        state_payload["recommended_commands"]
                    ),
                    "recommended_actions": _task_surface_actions(
                        list(state_payload["recommended_actions"])
                    ),
                    "next_action": next_action.to_dict(),
                }
                return observe_operation_result(profile, OperationResult(
                    operation="task.begin",
                    operation_status="blocked",
                    task_status=state_payload.get("task_runtime_status"),
                    attempt_status=state_payload.get("latest_attempt_status"),
                    disposition=next_action.disposition,
                    mutation_started=False,
                    mutation_completed=False,
                    mutation_phase="none",
                    failure_code=None,
                    next_action=next_action,
                    legacy_payload=payload,
                ))
            begin_spec = TaskBeginSpec(
                workset_id=resolved_workset,
                task_id=resolved_task,
                task_title=spec.task_title,
                actor=actor,
                created_workset=False,
                prompt_mode=prompt_mode,
                user_prompt_hash=user_receipt.prompt_hash,
                user_prompt_source=user_receipt.source,
                user_prompt_replay_artifact_path=user_receipt.replay_artifact_path,
                execution_prompt_hash=execution_receipt.prompt_hash,
                execution_prompt_source=execution_receipt.source,
                execution_prompt_replay_artifact_path=execution_receipt.replay_artifact_path,
                execution_prompt_text=execution_receipt.text if include_prompt else None,
                worktree=spec,
            )
            state_payload = _task_recovery_payload(
                profile,
                workset_id=resolved_workset,
                task_id=resolved_task,
            )
            next_action = decide_next_action(_lifecycle_context(profile, state_payload))
            payload = begin_spec.to_dict()
            payload["next_action"] = next_action.to_dict()
            payload["recommended_commands"] = list(state_payload["recommended_commands"])
            payload["recommended_actions"] = _task_surface_actions(
                list(state_payload["recommended_actions"])
            )
            return observe_operation_result(profile, OperationResult(
                operation="task.begin",
                operation_status="succeeded",
                task_status=state_payload.get("task_runtime_status"),
                attempt_status=state_payload.get("latest_attempt_status"),
                disposition=next_action.disposition,
                mutation_started=mutated,
                mutation_completed=mutated,
                mutation_phase="workspace_adopted" if mutated else "none",
                failure_code=None,
                next_action=next_action,
                legacy_payload=payload,
            ))
    resume_guard: _TaskResumeGuard | None = None
    if resolved_workset is not None and resolved_task is not None:
        latest = latest_task_attempt(
            load_runtime_state(profile.paths),
            resolved_workset,
            resolved_task,
        )
        if latest is not None:
            with attempt_lifecycle_lock(
                profile,
                workset_id=resolved_workset,
                task_id=resolved_task,
                attempt_id=latest.attempt_id,
            ):
                current_latest = latest_task_attempt(
                    load_runtime_state(profile.paths),
                    resolved_workset,
                    resolved_task,
                )
                transaction = (
                    load_landing_transaction(
                        profile,
                        workset_id=resolved_workset,
                        task_id=resolved_task,
                        attempt_id=current_latest.attempt_id,
                    )
                    if current_latest is not None
                    else None
                )
                if (
                    current_latest is not None
                    and current_latest.attempt_id != latest.attempt_id
                ):
                    raise BacklogError(
                        "task begin resume lineage changed while waiting for the predecessor attempt lock"
                    )
                if transaction is not None and not transaction.terminal:
                    return _recoverable_task_blocked_result(
                        profile,
                        operation="task.begin",
                        workset_id=resolved_workset,
                        task_id=resolved_task,
                        actor=actor,
                        legacy_updates={
                            "created_workset": False,
                            "prompt_mode": prompt_mode,
                            "user_prompt_hash": user_receipt.prompt_hash,
                            "user_prompt_source": user_receipt.source,
                            "user_prompt_replay_artifact_path": user_receipt.replay_artifact_path,
                            "execution_prompt_hash": execution_receipt.prompt_hash,
                            "execution_prompt_source": execution_receipt.source,
                            "execution_prompt_replay_artifact_path": execution_receipt.replay_artifact_path,
                            "execution_prompt_text": (
                                execution_receipt.text if include_prompt else None
                            ),
                            "worktree": None,
                        },
                    )
                try:
                    resume_guard = _validate_existing_task_resume_lineage(
                        profile,
                        workset_id=resolved_workset,
                        task_id=resolved_task,
                        actor=actor,
                        user_receipt=user_receipt,
                        execution_receipt=execution_receipt,
                        expected_actor=expected_actor,
                        expected_execution_prompt_hash=expected_execution_prompt_hash,
                        expected_execution_prompt_mode=expected_execution_prompt_mode,
                        expected_request_prompt_hash=expected_request_prompt_hash,
                        expected_request_prompt_mode=expected_request_prompt_mode,
                    )
                except _TaskStartProofConflict as exc:
                    return _task_begin_start_proof_conflict_result(
                        profile,
                        error=exc,
                        workset_id=resolved_workset,
                        task_id=resolved_task,
                        actor=actor,
                        prompt_mode=prompt_mode,
                        user_receipt=user_receipt,
                        execution_receipt=execution_receipt,
                    )
        else:
            try:
                resume_guard = _validate_existing_task_resume_lineage(
                    profile,
                    workset_id=resolved_workset,
                    task_id=resolved_task,
                    actor=actor,
                    user_receipt=user_receipt,
                    execution_receipt=execution_receipt,
                    expected_actor=expected_actor,
                    expected_execution_prompt_hash=expected_execution_prompt_hash,
                    expected_execution_prompt_mode=expected_execution_prompt_mode,
                    expected_request_prompt_hash=expected_request_prompt_hash,
                    expected_request_prompt_mode=expected_request_prompt_mode,
                )
            except _TaskStartProofConflict as exc:
                return _task_begin_start_proof_conflict_result(
                    profile,
                    error=exc,
                    workset_id=resolved_workset,
                    task_id=resolved_task,
                    actor=actor,
                    prompt_mode=prompt_mode,
                    user_receipt=user_receipt,
                    execution_receipt=execution_receipt,
                )
    guard_receipt = None
    if resume_guard is None or not resume_guard.retry_reserved_successor:
        guard_receipt = _guard_task_start(
            profile,
            actor=actor,
            prompt_mode=prompt_mode,
            execution_receipt=execution_receipt,
            user_receipt=user_receipt,
        )
    if resume_guard is not None:
        _verify_recorded_prompt_artifact(
            profile,
            role="execution",
            prompt_hash=resume_guard.execution_prompt_hash,
            replay_artifact_path=resume_guard.execution_prompt_replay_artifact_path,
        )
        _verify_recorded_prompt_artifact(
            profile,
            role="request",
            prompt_hash=resume_guard.request_prompt_hash,
            replay_artifact_path=resume_guard.request_prompt_replay_artifact_path,
        )
        user_receipt, execution_receipt = persist_prompt_receipts(
            profile.paths.control_dir,
            (user_receipt, execution_receipt),
        )
        execution_receipt = replace(
            execution_receipt,
            source=resume_guard.execution_prompt_source,
            replay_artifact_path=(
                resume_guard.execution_prompt_replay_artifact_path
                or execution_receipt.replay_artifact_path
            ),
        )
        user_receipt = replace(
            user_receipt,
            source=resume_guard.request_prompt_source,
            replay_artifact_path=(
                resume_guard.request_prompt_replay_artifact_path
                or user_receipt.replay_artifact_path
            ),
        )
    else:
        user_receipt, execution_receipt = persist_prompt_receipts(
            profile.paths.control_dir,
            (user_receipt, execution_receipt),
        )
    created_workset = False
    retained_envelope_payload: dict[str, Any] | None = None
    if resolved_workset is None:
        workspace_root = command_workspace_root(profile, cwd=cwd)
        retained_envelope_payload = _auto_task_workset_payload(
            profile,
            prompt=user_receipt.text,
            title=title,
            workspace_root=workspace_root,
        )
        retained_envelope_payload["tasks"][0]["metadata"]["prompt_mode"] = prompt_mode
        resolved_workset = str(retained_envelope_payload["id"])
        resolved_task = str(retained_envelope_payload["tasks"][0]["id"])
        created_workset = True
    else:
        retained_envelope_payload = _reserved_auto_task_envelope_payload(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
        )

    stale_gate = _workset_stale_claim_release_gate(
        profile,
        operation="task.begin",
        workset_id=resolved_workset,
        task_id=resolved_task,
        actor=actor,
        legacy_updates={
            "created_workset": created_workset,
            "prompt_mode": prompt_mode,
            "user_prompt_hash": user_receipt.prompt_hash,
            "user_prompt_source": user_receipt.source,
            "user_prompt_replay_artifact_path": user_receipt.replay_artifact_path,
            "execution_prompt_hash": execution_receipt.prompt_hash,
            "execution_prompt_source": execution_receipt.source,
            "execution_prompt_replay_artifact_path": execution_receipt.replay_artifact_path,
            "worktree": None,
        },
    )
    if stale_gate is not None:
        return stale_gate

    retained_envelope = retained_envelope_payload is not None
    if retained_envelope_payload is not None:
        try:
            _reserve_auto_task_envelope(profile, retained_envelope_payload)
        except Exception as exc:
            partial = _task_begin_partial_after_reservation(
                profile,
                workset_id=resolved_workset,
                task_id=resolved_task,
                actor=actor,
                created_workset=created_workset,
                retained_envelope=True,
                prompt_mode=prompt_mode,
                user_receipt=user_receipt,
                execution_receipt=execution_receipt,
                prior_attempt_id=None,
                mutation_observed=True,
                error=exc,
                include_prompt=include_prompt,
                cwd=cwd,
                branch=branch,
                from_ref=from_ref,
                path=path,
                model=model,
                reasoning_effort=reasoning_effort,
                note=note,
            )
            if partial is not None:
                return partial
            raise

    prior_attempt = latest_task_attempt(
        load_runtime_state(profile.paths),
        resolved_workset,
        resolved_task,
    )
    begin_side_effect_before = _task_begin_side_effect_fingerprint(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
    )
    try:
        spec = start_task_worktree(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            actor=actor,
            prompt=execution_receipt.text,
            prompt_source=execution_receipt.source,
            prompt_mode=prompt_mode,
            execution_prompt_receipt=execution_receipt,
            user_prompt_receipt=user_receipt,
            guard_receipt=guard_receipt,
            skill_provenance=skill_provenance,
            model=model,
            reasoning_effort=reasoning_effort,
            branch=branch,
            from_ref=from_ref,
            path=path,
            cwd=cwd,
            note=note,
            resume_guard=resume_guard,
        )
    except _TaskStartProofConflict as exc:
        return _task_begin_start_proof_conflict_result(
            profile,
            error=exc,
            workset_id=resolved_workset,
            task_id=resolved_task,
            actor=actor,
            prompt_mode=prompt_mode,
            user_receipt=user_receipt,
            execution_receipt=execution_receipt,
        )
    except Exception as exc:
        partial = _task_begin_partial_after_reservation(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            actor=actor,
            created_workset=created_workset,
            retained_envelope=retained_envelope,
            prompt_mode=prompt_mode,
            user_receipt=user_receipt,
            execution_receipt=execution_receipt,
            prior_attempt_id=(prior_attempt.attempt_id if prior_attempt is not None else None),
            mutation_observed=(
                begin_side_effect_before
                != _task_begin_side_effect_fingerprint(
                    profile,
                    workset_id=resolved_workset,
                    task_id=resolved_task,
                )
            ),
            error=exc,
            include_prompt=include_prompt,
            cwd=cwd,
            branch=branch,
            from_ref=from_ref,
            path=path,
            model=model,
            reasoning_effort=reasoning_effort,
            note=note,
        )
        if partial is not None:
            return partial
        raise
    begin_spec = TaskBeginSpec(
        workset_id=resolved_workset,
        task_id=resolved_task,
        task_title=spec.task_title,
        actor=actor,
        created_workset=created_workset,
        prompt_mode=prompt_mode,
        user_prompt_hash=user_receipt.prompt_hash,
        user_prompt_source=user_receipt.source,
        user_prompt_replay_artifact_path=user_receipt.replay_artifact_path,
        execution_prompt_hash=execution_receipt.prompt_hash,
        execution_prompt_source=execution_receipt.source,
        execution_prompt_replay_artifact_path=execution_receipt.replay_artifact_path,
        execution_prompt_text=execution_receipt.text if include_prompt else None,
        worktree=spec,
    )
    state_payload = _task_recovery_payload(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
    )
    next_action = decide_next_action(_lifecycle_context(profile, state_payload))
    payload = begin_spec.to_dict()
    payload["next_action"] = next_action.to_dict()
    payload["recommended_commands"] = list(state_payload["recommended_commands"])
    payload["recommended_actions"] = _task_surface_actions(
        list(state_payload["recommended_actions"])
    )
    begin_mutated = spec.workspace_action != "reused"
    return observe_operation_result(profile, OperationResult(
        operation="task.begin",
        operation_status="succeeded",
        task_status=state_payload.get("task_runtime_status"),
        attempt_status=state_payload.get("latest_attempt_status"),
        disposition=next_action.disposition,
        mutation_started=begin_mutated,
        mutation_completed=begin_mutated,
        mutation_phase="workspace_started" if begin_mutated else "none",
        failure_code=None,
        next_action=next_action,
        legacy_payload=payload,
    ))


def show_task(
    profile: RepoProfile,
    *,
    workset_id: str | None = None,
    task_id: str | None = None,
    cwd: Path | None = None,
) -> OperationResult:
    resolved_workset, resolved_task, _attempt = _resolve_task_command_target(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        cwd=cwd,
        allow_latest=True,
    )
    payload = inspect_task_worktree(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
        include_reconciliation_detection=True,
    )
    payload["recommended_actions"] = _task_surface_actions(
        list(payload["recommended_actions"])
    )
    _pending_stale, pending_stale_action = _pending_stale_claim_release_for_workset_action(
        profile,
        workset_id=resolved_workset,
    )
    _pending, pending_action = _pending_task_runtime_transition_action(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
    )
    _pending_close, pending_close_action = _pending_close_action(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
    )
    next_action = (
        pending_close_action
        if pending_close_action is not None
        else pending_stale_action
        if pending_stale_action is not None
        else pending_action
        if pending_action is not None
        else decide_next_action(_lifecycle_context(profile, payload))
    )
    return observe_operation_result(profile, OperationResult(
        operation="task.show",
        operation_status="observed",
        task_status=payload.get("task_runtime_status"),
        attempt_status=payload.get("latest_attempt_status"),
        disposition=next_action.disposition,
        mutation_started=False,
        mutation_completed=False,
        mutation_phase="none",
        failure_code=payload.get("failure_class"),
        next_action=next_action,
        legacy_payload=payload,
    ))


def _recoverable_task_blocked_result(
    profile: RepoProfile,
    *,
    operation: str,
    workset_id: str,
    task_id: str,
    actor: str | None = None,
    legacy_updates: Mapping[str, Any] | None = None,
) -> OperationResult:
    """Project one durable recovery blocker without recording a mutation."""

    state_payload = _task_recovery_payload(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    if actor is not None:
        state_payload["actor"] = actor
    _pending_stale, pending_stale_action = _pending_stale_claim_release_for_workset_action(
        profile,
        workset_id=workset_id,
    )
    _pending, pending_action = _pending_task_runtime_transition_action(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    _pending_close, pending_close_action = _pending_close_action(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    next_action = (
        pending_close_action
        if pending_close_action is not None
        else pending_stale_action
        if pending_stale_action is not None
        else pending_action
        if pending_action is not None
        else decide_next_action(_lifecycle_context(profile, state_payload))
    )
    if (
        pending_close_action is None
        and pending_stale_action is None
        and
        pending_action is None
        and
        state_payload.get("landing_transaction_incomplete")
        and not state_payload.get("resume_start_incomplete")
        and not state_payload.get("resume_start_issue_code")
    ):
        attempt_id = str(state_payload.get("attempt_id") or "").strip()
        transaction = (
            load_landing_transaction(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=attempt_id,
            )
            if attempt_id
            else None
        )
        if transaction is not None and not transaction.terminal:
            next_action = NextAction.command(
                _landing_abort_close_action(transaction)
                if transaction.aborted
                else _landing_resume_action(transaction.intent)
            )
    payload = dict(state_payload)
    payload.update(
        {
            "status": (
                state_payload.get("latest_attempt_status")
                or state_payload.get("task_runtime_status")
            ),
            "summary": next_action.reason_detail,
            "error": next_action.reason_detail,
            "attempt_active": bool(state_payload.get("active_attempt")),
            "next_action": next_action.to_dict(),
            "recommended_commands": next_action.legacy_command_rows(),
            "recommended_actions": [next_action.display],
        }
    )
    if legacy_updates is not None:
        payload.update(dict(legacy_updates))
    return observe_operation_result(
        profile,
        OperationResult(
            operation=operation,
            operation_status="blocked",
            task_status=state_payload.get("task_runtime_status"),
            attempt_status=state_payload.get("latest_attempt_status"),
            disposition=next_action.disposition,
            mutation_started=False,
            mutation_completed=False,
            mutation_phase="none",
            failure_code=None,
            next_action=next_action,
            legacy_payload=payload,
        ),
    )


def _workset_stale_claim_release_gate(
    profile: RepoProfile,
    *,
    operation: str,
    workset_id: str,
    task_id: str,
    actor: str | None = None,
    legacy_updates: Mapping[str, Any] | None = None,
) -> OperationResult | None:
    """Block claim-set mutations before workspace, Git, or runtime effects."""

    reservation, _action = _pending_stale_claim_release_for_workset_action(
        profile,
        workset_id=workset_id,
    )
    if reservation is None:
        return None
    updates = dict(legacy_updates or {})
    updates["stale_claim_release_owner_task_id"] = reservation.get(
        "owner_task_id"
    )
    return _recoverable_task_blocked_result(
        profile,
        operation=operation,
        workset_id=workset_id,
        task_id=task_id,
        actor=actor,
        legacy_updates=updates,
    )


def _incomplete_close_gate(
    profile: RepoProfile,
    *,
    operation: str,
    workset_id: str,
    task_id: str,
    actor: str | None = None,
    legacy_updates: Mapping[str, Any] | None = None,
) -> OperationResult | None:
    pending, _action = _pending_close_action(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    if pending is None:
        return None
    return _recoverable_task_blocked_result(
        profile,
        operation=operation,
        workset_id=workset_id,
        task_id=task_id,
        actor=actor,
        legacy_updates=legacy_updates,
    )


def _require_no_incomplete_close(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
) -> None:
    pending, _action = _pending_close_action(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    if pending is not None:
        raise CloseTransactionError(
            "same-task mutation is gated by its incomplete close transaction"
        )


def _workspace_adoption_completion_command_identity(
    argv: Sequence[str],
) -> dict[str, Any] | None:
    """Return the caller-controlled identity of one adoption completion command."""

    try:
        task_index = argv.index("task")
        command = argv[task_index + 1]
    except (ValueError, IndexError):
        return None
    options: dict[str, list[str | None]] = {}
    for token in argv[task_index + 2 :]:
        if not token.startswith("--"):
            return None
        name, separator, value = token[2:].partition("=")
        options.setdefault(name, []).append(value if separator else None)

    def one(name: str) -> str | None:
        values = options.get(name, [])
        if len(values) > 1:
            raise LandingTransactionError(
                f"workspace adoption completion command repeats --{name}"
            )
        value = values[0] if values else None
        return value if isinstance(value, str) else None

    common = {
        "operation": f"task.{command}",
        "workset_id": one("workset"),
        "task_id": one("task"),
        "actor": one("actor"),
    }
    if command == "land":
        return {
            **common,
            "summary": one("summary"),
            "validations": list(options.get("validation", ())),
            "residuals": list(options.get("residual", ())),
            "followup_candidates": list(options.get("followup", ())),
            "note": one("note"),
            "cleanup": "keep-worktree" not in options,
        }
    if command == "reconcile-landing":
        return {
            **common,
            "attempt_id": one("attempt"),
            "landed_commit": one("landed-commit"),
            "apply": options.get("apply") == [None],
        }
    return None


def _task_start_terminal_gate(
    profile: RepoProfile,
    *,
    operation: str,
    workset_id: str,
    task_id: str,
    attempt: Any,
    actor: str,
    completion_request_identity: Mapping[str, Any] | None = None,
) -> OperationResult | None:
    pending, _action = _pending_task_runtime_transition_action(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    if pending is not None:
        return _recoverable_task_blocked_result(
            profile,
            operation=operation,
            workset_id=workset_id,
            task_id=task_id,
            actor=actor,
        )
    if attempt.status != ATTEMPT_STATUS_IN_PROGRESS:
        return None
    runtime_state = load_runtime_state(profile.paths)
    current = find_task_attempt(runtime_state, workset_id, attempt.attempt_id)
    if current is None or current.task_id != task_id:
        raise BacklogError("task attempt changed before terminal start-evidence preflight")
    state, _issue = _task_start_repair_evidence(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        runtime_state=runtime_state,
        successor=current,
    )
    if state not in {None, "complete"}:
        return _recoverable_task_blocked_result(
            profile,
            operation=operation,
            workset_id=workset_id,
            task_id=task_id,
            actor=actor,
        )
    recovery = _task_recovery_payload(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        runtime_state=runtime_state,
    )
    if recovery.get("active_workspace_adoption"):
        if (
            recovery.get("workspace_adoption_eligible")
            or recovery.get("workspace_adoption_issue_code")
        ):
            return _recoverable_task_blocked_result(
                profile,
                operation=operation,
                workset_id=workset_id,
                task_id=task_id,
                actor=actor,
            )
        if recovery.get("workspace_adoption_completion_pending"):
            completion_argv = tuple(
                str(item)
                for item in recovery.get("workspace_adoption_completion_argv")
                or ()
            )
            expected_identity = _workspace_adoption_completion_command_identity(
                completion_argv
            )
            if (
                expected_identity is None
                or expected_identity.get("operation") != operation
                or completion_request_identity is None
                or not strict_json_equal(
                    expected_identity,
                    dict(completion_request_identity),
                )
            ):
                return _recoverable_task_blocked_result(
                    profile,
                    operation=operation,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor=actor,
                )
    return None


def land_task(
    profile: RepoProfile,
    *,
    summary: str | None,
    actor: str | None = None,
    workset_id: str | None = None,
    task_id: str | None = None,
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
    cleanup: bool = True,
    cwd: Path | None = None,
) -> OperationResult:
    explicit_workset = str(workset_id or "").strip() or None
    explicit_task = str(task_id or "").strip() or None
    if explicit_workset is not None and explicit_task is not None:
        close_gate = _incomplete_close_gate(
            profile,
            operation="task.land",
            workset_id=explicit_workset,
            task_id=explicit_task,
            actor=str(actor or "").strip() or None,
        )
        if close_gate is not None:
            return close_gate
        pending_transition, _pending_action = _pending_task_runtime_transition_action(
            profile,
            workset_id=explicit_workset,
            task_id=explicit_task,
        )
        if pending_transition is not None:
            return _recoverable_task_blocked_result(
                profile,
                operation="task.land",
                workset_id=explicit_workset,
                task_id=explicit_task,
                actor=str(actor or "").strip() or None,
            )
    resolved_workset, resolved_task, attempt = _resolve_task_command_target(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        cwd=cwd,
        allow_latest=False,
        allow_landing_transaction=True,
    )
    stale_gate = _workset_stale_claim_release_gate(
        profile,
        operation="task.land",
        workset_id=resolved_workset,
        task_id=resolved_task,
        actor=str(actor or "").strip() or None,
    )
    if stale_gate is not None:
        return stale_gate
    resolved_actor = str(actor or getattr(attempt, "actor", "")).strip() or None
    if resolved_actor is None:
        raise BacklogError("task land requires a persisted attempt actor")
    if resolved_actor != str(getattr(attempt, "actor", "") or "").strip():
        raise BacklogError(
            f"Attempt {attempt.attempt_id!r} is owned by {attempt.actor}, not {resolved_actor}"
        )
    primary_root_before = find_primary_worktree(profile.paths.project_root)
    primary_profile = load_profile(primary_root_before)
    existing_transaction = load_landing_transaction(
        primary_profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
        attempt_id=attempt.attempt_id,
    )
    if (
        existing_transaction is not None
        and not str(summary or "").strip()
        and not validations
    ):
        intent = existing_transaction.intent
        summary = intent.summary
        validations = tuple(
            ValidationRecord(name=name, status=status)
            for name, status in intent.validations
        )
        residuals = intent.residuals
        followup_candidates = intent.followup_candidates
        note = intent.note
        cleanup = intent.cleanup
    start_gate = _task_start_terminal_gate(
        primary_profile,
        operation="task.land",
        workset_id=resolved_workset,
        task_id=resolved_task,
        attempt=attempt,
        actor=resolved_actor,
        completion_request_identity={
            "operation": "task.land",
            "workset_id": resolved_workset,
            "task_id": resolved_task,
            "actor": resolved_actor,
            "summary": summary,
            "validations": [
                f"{validation.name}={validation.status}"
                for validation in validations
            ],
            "residuals": list(residuals),
            "followup_candidates": list(followup_candidates),
            "note": note,
            "cleanup": cleanup,
        },
    )
    if start_gate is not None:
        return start_gate
    if existing_transaction is None and not _landing_request_has_evidence(
        summary=summary,
        validations=validations,
    ):
        state_payload = _task_recovery_payload(
            primary_profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
        )
        next_action = landing_evidence_required_action()
        payload = dict(state_payload)
        payload.update(
            {
                "target_worktree": None,
                "landing_worktree": None,
                "landed_commit": None,
                "diff_file": None,
                "diffstat_file": None,
                "cleanup": cleanup,
                "cleaned_worktree": None,
                "deleted_branch": False,
                "removed_temporary_target": False,
                "status": ATTEMPT_STATUS_BLOCKED,
                "summary": next_action.reason_detail,
                "commit": None,
                "commit_message": None,
                "error": next_action.reason_detail,
                "attempt_active": True,
                "land_failure_disposition": "retryable",
                "transaction_id": None,
                "landing_transaction": None,
                "landing_transaction_incomplete": False,
                "landing_evidence_required": True,
                "mutation_observed": False,
            }
        )
        return observe_operation_result(primary_profile, OperationResult(
            operation="task.land",
            operation_status="blocked",
            task_status=state_payload.get("task_runtime_status"),
            attempt_status=state_payload.get("latest_attempt_status"),
            disposition=next_action.disposition,
            mutation_started=False,
            mutation_completed=False,
            mutation_phase="none",
            failure_code=None,
            next_action=next_action,
            legacy_payload=payload,
        ))
    payload = land_task_worktree(
        primary_profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
        actor=resolved_actor,
        summary=summary,
        validations=validations,
        residuals=residuals,
        followup_candidates=followup_candidates,
        note=note,
        cleanup=cleanup,
        _automatic_stale_recovery_enabled=(
            primary_profile.landing.automatic_stale_rebase
        ),
    )
    primary_text = str(payload.get("primary_worktree") or "").strip()
    operation_profile = (
        load_profile(Path(primary_text))
        if primary_text and Path(primary_text).exists()
        else primary_profile
    )
    after_transaction = load_landing_transaction(
        operation_profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
        attempt_id=attempt.attempt_id,
    )
    state_payload = _task_recovery_payload(
        operation_profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
    )
    if payload.get("landing_correction") is None and state_payload.get(
        "landing_correction"
    ) is not None:
        payload["landing_correction"] = state_payload["landing_correction"]
    if state_payload.get("landing_correction_state") is not None:
        payload["landing_correction_state"] = state_payload[
            "landing_correction_state"
        ]
    if payload.get("close_transaction_blocked"):
        close_request_value = str(payload.get("close_request_id") or "").strip()
        durable_close_request = (
            load_close_request_by_id(operation_profile, close_request_value)
            if close_request_value
            else None
        )
        if payload.get("next_action", {}).get("kind") == "blocked":
            next_action = _close_conflict_action(
                str(payload.get("evidence_error") or payload.get("error"))
            )
        elif durable_close_request is not None:
            next_action = NextAction.command(
                _close_retry_action(operation_profile, durable_close_request)
            )
        else:
            retry_request = _close_request_for_attempt(
                operation_profile,
                workset_id=resolved_workset,
                task_id=resolved_task,
                attempt=attempt,
                actor=resolved_actor,
                status=str(payload["status"]),
                summary=str(payload["summary"]),
                validations=tuple(
                    ValidationRecord(name=name, status=value)
                    for name, value in (
                        payload.get("validations")
                        or tuple(
                            (row.name, row.status) for row in validations
                        )
                    )
                ),
                residuals=tuple(payload.get("residuals") or residuals),
                followup_candidates=tuple(
                    payload.get("followup_candidates") or followup_candidates
                ),
                note=payload.get("note") or note or payload.get("landing_error"),
                cleanup=bool(payload.get("cleanup_requested")),
                failure_class=payload.get("failure_class"),
                recovery_action=payload.get("recovery_action"),
                prompt_issue=bool(payload.get("prompt_issue")),
                operator_issue=bool(payload.get("operator_issue")),
                trusted_failure_details=True,
            )
            next_action = NextAction.command(
                _close_retry_action(
                    operation_profile,
                    retry_request,
                    guarded=False,
                )
            )
        payload["next_action"] = next_action.to_dict()
        payload["recommended_commands"] = next_action.legacy_command_rows()
        payload["recommended_actions"] = [next_action.display]
        return observe_operation_result(operation_profile, OperationResult(
            operation="task.land",
            operation_status="blocked",
            task_status=state_payload.get("task_runtime_status"),
            attempt_status=state_payload.get("latest_attempt_status"),
            disposition=next_action.disposition,
            mutation_started=bool(payload.get("mutation_started")),
            mutation_completed=False,
            mutation_phase=str(payload.get("mutation_phase") or "preflight"),
            failure_code=payload.get("failure_class"),
            next_action=next_action,
            legacy_payload=payload,
        ))
    if payload.get("close_transaction_complete"):
        next_action = decide_next_action(
            _lifecycle_context(operation_profile, state_payload)
        )
        payload["next_action"] = next_action.to_dict()
        payload["recommended_commands"] = next_action.legacy_command_rows()
        payload["recommended_actions"] = (
            [] if next_action.kind == "complete" else [next_action.display]
        )
        return observe_operation_result(operation_profile, OperationResult(
            operation="task.land",
            operation_status=(
                "partial"
                if payload.get("operation_status") == "partial"
                else "closed"
            ),
            task_status=state_payload.get("task_runtime_status"),
            attempt_status=state_payload.get("latest_attempt_status"),
            disposition=next_action.disposition,
            mutation_started=bool(payload.get("mutation_started")),
            mutation_completed=bool(payload.get("mutation_completed")),
            mutation_phase=str(payload.get("mutation_phase") or "close_complete"),
            failure_code=payload.get("failure_class"),
            next_action=next_action,
            legacy_payload=payload,
        ))
    pretransaction_failure_action = _pretransaction_landing_failure_next_action(
        payload
    )
    if after_transaction is not None and after_transaction.outcome == "abort_in_progress":
        next_action = NextAction.command(_landing_abort_close_action(after_transaction))
    elif after_transaction is not None and after_transaction.outcome == "landing_in_progress":
        next_action = NextAction.command(_landing_resume_action(after_transaction.intent))
    elif pretransaction_failure_action is not None:
        next_action = pretransaction_failure_action
    else:
        next_action = decide_next_action(
            _lifecycle_context(operation_profile, state_payload)
        )
    payload["next_action"] = next_action.to_dict()
    payload["recommended_commands"] = next_action.legacy_command_rows()
    payload["recommended_actions"] = (
        [] if next_action.kind == "complete" else [next_action.display]
    )
    success = payload.get("status") == ATTEMPT_STATUS_SUCCESS
    closed = payload.get("land_failure_disposition") == "closed"
    mutation_started = bool(closed or payload.get("mutation_observed"))
    mutation_completed = bool(
        closed
        or (
            success
            and mutation_started
            and after_transaction is not None
            and after_transaction.terminal
        )
    )
    mutation_phase = (
        _landing_operation_phase(after_transaction)
        if after_transaction is not None
        else str(payload.get("mutation_phase") or "preflight")
    )
    operation_status = (
        "succeeded"
        if success
        else "closed"
        if closed
        else "partial"
        if after_transaction is not None
        else "blocked"
    )
    return observe_operation_result(operation_profile, OperationResult(
        operation="task.land",
        operation_status=operation_status,
        task_status=state_payload.get("task_runtime_status"),
        attempt_status=state_payload.get("latest_attempt_status"),
        disposition=next_action.disposition,
        mutation_started=mutation_started,
        mutation_completed=mutation_completed,
        mutation_phase=mutation_phase,
        failure_code=payload.get("failure_class"),
        next_action=next_action,
        legacy_payload=payload,
    ))


def _task_mutation_result(
    profile: RepoProfile,
    *,
    operation: str,
    workset_id: str,
    task_id: str,
    legacy_payload: dict[str, Any],
    mutation_phase: str,
    operation_status: str = "succeeded",
    actor: str | None = None,
    mutation_started: bool = True,
    mutation_completed: bool = True,
    next_action_override: NextAction | None = None,
) -> OperationResult:
    """Return one lifecycle result from the mutation payload and post-mutation state."""
    state_payload = _task_recovery_payload(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    if actor is not None:
        state_payload["actor"] = actor
    next_action = next_action_override or decide_next_action(_lifecycle_context(profile, state_payload))
    payload = dict(legacy_payload)
    payload["next_action"] = next_action.to_dict()
    if next_action_override is not None:
        payload["recommended_commands"] = next_action.legacy_command_rows()
        payload["recommended_actions"] = (
            [] if next_action.kind == "complete" else [next_action.display]
        )
    else:
        payload["recommended_commands"] = list(state_payload["recommended_commands"])
        payload["recommended_actions"] = _task_surface_actions(
            list(state_payload["recommended_actions"])
        )
    return observe_operation_result(profile, OperationResult(
        operation=operation,
        operation_status=operation_status,
        task_status=state_payload.get("task_runtime_status"),
        attempt_status=state_payload.get("latest_attempt_status"),
        disposition=next_action.disposition,
        mutation_started=mutation_started,
        mutation_completed=mutation_completed,
        mutation_phase=mutation_phase,
        failure_code=payload.get("failure_class") or state_payload.get("failure_class"),
        next_action=next_action,
        legacy_payload=payload,
    ))


def close_task(
    profile: RepoProfile,
    *,
    status: str | None = None,
    summary: str | None = None,
    actor: str | None = None,
    workset_id: str | None = None,
    task_id: str | None = None,
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
    cleanup: bool | None = None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool | None = None,
    operator_issue: bool | None = None,
    close_request_id: str | None = None,
    cwd: Path | None = None,
) -> OperationResult:
    guarded_request = None
    if close_request_id is not None:
        guarded_request = load_close_request_record_by_id(
            profile,
            close_request_id,
        )
        if guarded_request is None:
            raise CloseTransactionError(
                "guarded task close names an unknown close request"
            )
        if workset_id is not None and workset_id != guarded_request.workset_id:
            raise CloseTransactionError(
                "guarded task close workset conflicts with its durable request"
            )
        if task_id is not None and task_id != guarded_request.task_id:
            raise CloseTransactionError(
                "guarded task close task conflicts with its durable request"
            )
        workset_id = guarded_request.workset_id
        task_id = guarded_request.task_id
        actor = actor if actor is not None else guarded_request.actor
        status = status if status is not None else guarded_request.status
        summary = summary if summary is not None else guarded_request.summary
        validations = validations or tuple(
            ValidationRecord(name=name, status=value)
            for name, value in guarded_request.validations
        )
        residuals = residuals or guarded_request.residuals
        followup_candidates = (
            followup_candidates or guarded_request.followup_candidates
        )
        note = note if note is not None else guarded_request.note
        cleanup = (
            cleanup if cleanup is not None else guarded_request.cleanup_requested
        )
        failure_class = (
            failure_class
            if failure_class is not None
            else guarded_request.failure_class
        )
        recovery_action = (
            recovery_action
            if recovery_action is not None
            else guarded_request.recovery_action
        )
        prompt_issue = (
            prompt_issue
            if prompt_issue is not None
            else guarded_request.prompt_issue
        )
        operator_issue = (
            operator_issue
            if operator_issue is not None
            else guarded_request.operator_issue
        )
    if status is None or summary is None:
        raise BacklogError("task close requires status and summary")
    cleanup = bool(cleanup)
    prompt_issue = bool(prompt_issue)
    operator_issue = bool(operator_issue)
    explicit_workset = str(workset_id or "").strip() or None
    explicit_task = str(task_id or "").strip() or None
    completed_replay_request = None
    if (
        guarded_request is None
        and explicit_workset is not None
        and explicit_task is not None
    ):
        replay_state = load_runtime_state(profile.paths)
        replay_active = active_task_attempt(
            replay_state,
            explicit_workset,
            explicit_task,
        )
        replay_latest = latest_task_attempt(
            replay_state,
            explicit_workset,
            explicit_task,
        )
        if replay_active is None and replay_latest is not None:
            candidate_request = load_close_request(
                profile,
                workset_id=explicit_workset,
                task_id=explicit_task,
                attempt_id=replay_latest.attempt_id,
            )
            if candidate_request is not None and _close_transaction_state(
                profile,
                request=candidate_request,
            )["complete"]:
                completed_replay_request = candidate_request
    if explicit_workset is not None and explicit_task is not None:
        pending_transition, _pending_action = _pending_task_runtime_transition_action(
            profile,
            workset_id=explicit_workset,
            task_id=explicit_task,
        )
        if pending_transition is not None:
            return _recoverable_task_blocked_result(
                profile,
                operation="task.close",
                workset_id=explicit_workset,
                task_id=explicit_task,
                actor=str(actor or "").strip() or None,
            )
    resolved_close_request = guarded_request or completed_replay_request
    if resolved_close_request is not None:
        resolved_workset = resolved_close_request.workset_id
        resolved_task = resolved_close_request.task_id
        attempt = find_task_attempt(
            load_runtime_state(profile.paths),
            resolved_workset,
            resolved_close_request.attempt_id,
        )
        if attempt is None or attempt.task_id != resolved_task:
            raise CloseTransactionError(
                "guarded task close no longer names its exact attempt"
            )
    else:
        resolved_workset, resolved_task, attempt = _resolve_task_command_target(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            cwd=cwd,
            allow_latest=False,
            allow_landing_transaction=True,
        )
    stale_gate = None if resolved_close_request is not None else _workset_stale_claim_release_gate(
        profile,
        operation="task.close",
        workset_id=resolved_workset,
        task_id=resolved_task,
        actor=str(actor or "").strip() or None,
    )
    if stale_gate is not None:
        return stale_gate
    resolved_actor = str(actor or getattr(attempt, "actor", "")).strip() or None
    if resolved_actor is None:
        raise BacklogError("task close requires a persisted attempt actor")
    if (
        resolved_close_request is None
        and resolved_actor != str(getattr(attempt, "actor", "") or "").strip()
    ):
        raise BacklogError(
            f"Attempt {attempt.attempt_id!r} is owned by {attempt.actor}, not {resolved_actor}"
        )
    start_gate = None if resolved_close_request is not None else _task_start_terminal_gate(
        profile,
        operation="task.close",
        workset_id=resolved_workset,
        task_id=resolved_task,
        attempt=attempt,
        actor=resolved_actor,
    )
    if start_gate is not None:
        return start_gate
    payload = close_task_worktree(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
        actor=resolved_actor,
        status=status,
        summary=summary,
        validations=validations,
        residuals=residuals,
        followup_candidates=followup_candidates,
        note=note,
        cleanup=cleanup,
        failure_class=failure_class,
        recovery_action=recovery_action,
        prompt_issue=prompt_issue,
        operator_issue=operator_issue,
        close_request_id=close_request_id,
        _trusted_failure_details=bool(
            failure_class is not None
            or recovery_action is not None
            or prompt_issue
            or operator_issue
        ),
    )
    if payload.get("close_transaction_blocked"):
        pending_request = guarded_request or load_close_request_by_id(
            profile,
            str(payload.get("close_request_id") or ""),
        )
        if payload.get("next_action", {}).get("kind") == "blocked":
            next_action = _close_conflict_action(
                str(payload.get("evidence_error") or payload.get("error"))
            )
        elif pending_request is not None:
            next_action = NextAction.command(
                _close_retry_action(profile, pending_request)
            )
        else:
            retry_request = _close_request_for_attempt(
                profile,
                workset_id=resolved_workset,
                task_id=resolved_task,
                attempt=attempt,
                actor=resolved_actor,
                status=status,
                summary=str(summary).strip(),
                validations=validations,
                residuals=residuals,
                followup_candidates=followup_candidates,
                note=note,
                cleanup=cleanup,
                failure_class=failure_class,
                recovery_action=recovery_action,
                prompt_issue=prompt_issue,
                operator_issue=operator_issue,
                trusted_failure_details=close_request_id is not None,
            )
            next_action = NextAction.command(
                _close_retry_action(profile, retry_request, guarded=False)
            )
        return _task_mutation_result(
            profile,
            operation="task.close",
            workset_id=resolved_workset,
            task_id=resolved_task,
            legacy_payload=payload,
            mutation_phase=str(payload["mutation_phase"]),
            operation_status="blocked",
            actor=resolved_actor,
            mutation_started=bool(payload["mutation_started"]),
            mutation_completed=False,
            next_action_override=next_action,
        )
    if payload.get("close_transaction_complete"):
        return _task_mutation_result(
            profile,
            operation="task.close",
            workset_id=resolved_workset,
            task_id=resolved_task,
            legacy_payload=payload,
            mutation_phase=str(payload["mutation_phase"]),
            operation_status=str(payload.get("operation_status") or "succeeded"),
            actor=resolved_actor,
            mutation_started=bool(payload["mutation_started"]),
            mutation_completed=bool(payload["mutation_completed"]),
        )
    if payload.get("closure_refused"):
        transaction = load_landing_transaction(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            attempt_id=attempt.attempt_id,
        )
        if transaction is None or transaction.terminal:
            raise LandingTransactionError(
                "close refusal lost its incomplete landing transaction"
            )
        if transaction.aborted:
            refusal_action = _landing_abort_close_action(transaction)
        else:
            refusal_action = _landing_safe_abort_action(
                transaction.intent,
                detail=str(payload.get("error") or "landing target changed"),
            )
        mutation_observed = bool(payload.get("mutation_observed"))
        return _task_mutation_result(
            profile,
            operation="task.close",
            workset_id=resolved_workset,
            task_id=resolved_task,
            legacy_payload=payload,
            mutation_phase=_landing_operation_phase(transaction),
            operation_status="blocked",
            actor=resolved_actor,
            mutation_started=mutation_observed,
            mutation_completed=False,
            next_action_override=NextAction.command(
                refusal_action
            ),
        )
    transaction = load_landing_transaction(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
        attempt_id=attempt.attempt_id,
    )
    if transaction is not None and transaction.abort_requested:
        mutation_observed = bool(payload.get("mutation_observed"))
        return _task_mutation_result(
            profile,
            operation="task.close",
            workset_id=resolved_workset,
            task_id=resolved_task,
            legacy_payload=payload,
            mutation_phase=_landing_operation_phase(transaction),
            operation_status="succeeded" if transaction.terminal else "partial",
            actor=resolved_actor,
            mutation_started=mutation_observed,
            mutation_completed=bool(mutation_observed and transaction.terminal),
        )
    cleanup_incomplete = bool(cleanup and payload.get("cleanup_reason"))
    next_action_override = None
    cleanup_payload = payload.get("cleanup")
    if (
        cleanup_incomplete
        and isinstance(cleanup_payload, dict)
        and cleanup_payload.get("event_finalized") is False
    ):
        next_action_override = NextAction.command(
            _cleanup_event_retry_action(
                profile,
                workset_id=resolved_workset,
                task_id=resolved_task,
                cleanup_payload=cleanup_payload,
            )
        )
    if cleanup_incomplete:
        mutation_phase = "runtime_finalized_cleanup_pending"
    elif cleanup and payload.get("cleanup_performed"):
        mutation_phase = "runtime_and_cleanup_finalized"
    else:
        mutation_phase = "runtime_finalized"
    return _task_mutation_result(
        profile,
        operation="task.close",
        workset_id=resolved_workset,
        task_id=resolved_task,
        legacy_payload=payload,
        mutation_phase=mutation_phase,
        operation_status="partial" if cleanup_incomplete else "succeeded",
        actor=resolved_actor,
        mutation_completed=not cleanup_incomplete,
        next_action_override=next_action_override,
    )


def _task_state_payload(
    *,
    profile: RepoProfile,
    workset_id: str,
    task_id: str,
    actor: str,
    status: str,
    summary: str | None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
    expected_transition_request_event_id: str | None = None,
    expected_transition_decision_event_id: str | None = None,
) -> dict[str, Any]:
    transition = set_task_runtime_status(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        actor=actor,
        status=status,
        summary=summary,
        failure_class=failure_class,
        recovery_action=recovery_action,
        prompt_issue=prompt_issue,
        operator_issue=operator_issue,
        return_transition_result=True,
        expected_transition_request_event_id=expected_transition_request_event_id,
        expected_transition_decision_event_id=expected_transition_decision_event_id,
    )
    if not isinstance(transition, TaskRuntimeTransitionResult):
        raise BacklogError("task runtime transition did not return mutation metadata")
    record = transition.record
    return {
        "workset_id": workset_id,
        "task_id": task_id,
        "actor": actor,
        "status": record.status,
        "updated_at": record.updated_at,
        "summary": record.note,
        "failure_class": record.failure_class,
        "recovery_action": record.recovery_action,
        "prompt_issue": record.prompt_issue,
        "operator_issue": record.operator_issue,
        "transition_runtime_changed": transition.runtime_changed,
        "transition_events_changed": transition.events_changed,
        "transition_request_event_appended": transition.request_event_appended,
        "transition_decision_event_appended": transition.decision_event_appended,
        "transition_owned_event_appended": transition.owned_event_appended,
    }


def _task_runtime_transition_retry_action(
    profile: RepoProfile,
    *,
    operation: str,
    workset_id: str,
    task_id: str,
    actor: str,
    summary: str | None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
    transition_request_event_id: str | None = None,
    transition_decision_event_id: str | None = None,
) -> LifecycleAction:
    argv = [
        _lifecycle_blackdog_executable(profile, {}),
        "task",
        operation,
        f"--project-root={profile.paths.project_root}",
        f"--workset={workset_id}",
        f"--task={task_id}",
        f"--actor={actor}",
    ]
    if transition_request_event_id is not None:
        argv.append(f"--transition-request={transition_request_event_id}")
    if transition_decision_event_id is not None:
        argv.append(f"--transition-decision={transition_decision_event_id}")
    if summary is not None:
        argv.append(f"--summary={summary}")
    if operation == "cancel":
        if failure_class is not None:
            argv.append(f"--failure-class={failure_class}")
        if recovery_action is not None:
            argv.append(f"--recovery-action={recovery_action}")
        if prompt_issue:
            argv.append("--prompt-issue")
        if operator_issue:
            argv.append("--operator-issue")
    return LifecycleAction(
        action_id=f"retry_task_{operation}_finalization",
        disposition="retryable",
        reason_code="task_runtime_transition_finalization_pending",
        reason_detail=(
            "The durable task-state transition must repair its exact runtime and event finalization."
        ),
        argv=tuple(argv),
        safety_class="validated_mutation",
        mutation_class="runtime",
        display=f"Retry exact task {operation} finalization",
    )


def _task_runtime_transition_guard_conflict_action(detail: str) -> NextAction:
    return NextAction.terminal(
        action_id="inspect_task_runtime_transition_guard_conflict",
        kind="blocked",
        disposition="proof_required",
        reason_code="task_runtime_transition_retry_superseded",
        reason_detail=detail,
        display="Inspect superseded task-state transition retry",
        required_inputs=("canonical_task_runtime_transition_generation",),
    )


def _durable_task_runtime_transition_event_id(
    profile: RepoProfile,
    event_id: str | None,
) -> str | None:
    if event_id is None:
        return None
    return (
        event_id
        if any(
            event.get("event_id") == event_id
            for event in load_events(profile.paths.events_file)
        )
        else None
    )


def _task_runtime_transition_guard_conflict_result(
    profile: RepoProfile,
    *,
    operation: str,
    workset_id: str,
    task_id: str,
    actor: str,
    request_event_id: str | None,
    decision_event_id: str | None,
    detail: str,
) -> OperationResult:
    return _task_mutation_result(
        profile,
        operation=f"task.{operation}",
        workset_id=workset_id,
        task_id=task_id,
        actor=actor,
        legacy_payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "actor": actor,
            "transition_request_event_id": request_event_id,
            "transition_decision_event_id": decision_event_id,
            "error": detail,
        },
        mutation_phase="none",
        operation_status="blocked",
        mutation_started=False,
        mutation_completed=False,
        next_action_override=_task_runtime_transition_guard_conflict_action(detail),
    )


def _pending_task_runtime_transition_action(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
) -> tuple[dict[str, Any] | None, NextAction | None]:
    pending = pending_task_runtime_transition(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    if pending is None:
        return None, None
    if pending["stage"] in {"runtime_conflict", "ledger_conflict"}:
        ledger_conflict = pending["stage"] == "ledger_conflict"
        return pending, NextAction.terminal(
            action_id=(
                "inspect_task_runtime_transition_ledger_conflict"
                if ledger_conflict
                else "inspect_task_runtime_transition_conflict"
            ),
            kind="blocked",
            disposition="proof_required",
            reason_code=(
                "task_runtime_transition_ledger_conflict"
                if ledger_conflict
                else "task_runtime_transition_state_conflict"
            ),
            reason_detail=(
                "The durable task-state transition ledger contains a hidden, duplicate, or out-of-order incomplete generation."
                if ledger_conflict
                else "The durable task-state transition matches neither its recorded pre-runtime nor post-runtime state."
            ),
            display=(
                "Inspect conflicting task-state transition ledger"
                if ledger_conflict
                else "Inspect conflicting task-state transition evidence"
            ),
            required_inputs=(
                "canonical_task_runtime_transition_ledger"
                if ledger_conflict
                else "canonical_task_runtime_transition_state",
            ),
        )
    request = pending["request"]
    operation = (
        "cancel"
        if request["status"] == TASK_STATUS_CANCELED
        else "reopen"
    )
    return pending, NextAction.command(
        _task_runtime_transition_retry_action(
            profile,
            operation=operation,
            workset_id=workset_id,
            task_id=task_id,
            actor=str(request["actor"]),
            summary=request.get("summary"),
            failure_class=request.get("failure_class"),
            recovery_action=request.get("recovery_action"),
            prompt_issue=bool(request.get("prompt_issue")),
            operator_issue=bool(request.get("operator_issue")),
            transition_request_event_id=str(pending["request_event_id"]),
            transition_decision_event_id=(
                str(pending["decision_event_id"])
                if pending["decision_event_id"] is not None
                else None
            ),
        )
    )


def _stale_claim_release_retry_action(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    status: str,
    summary: str,
    note: str | None,
    request_event_id: str,
    decision_event_id: str | None,
) -> LifecycleAction:
    argv = [
        _lifecycle_blackdog_executable(profile, {}),
        "task",
        "recover",
        f"--project-root={profile.paths.project_root}",
        f"--workset={workset_id}",
        f"--task={task_id}",
        "--release-stale-claim",
        f"--status={status}",
        f"--summary={summary}",
        f"--stale-claim-release-request={request_event_id}",
    ]
    if decision_event_id is not None:
        argv.append(f"--stale-claim-release-decision={decision_event_id}")
    if note is not None:
        argv.append(f"--note={note}")
    return LifecycleAction(
        action_id="retry_stale_claim_release_finalization",
        disposition="retryable",
        reason_code="stale_claim_release_finalization_pending",
        reason_detail=(
            "The durable stale-claim release must repair its exact runtime and owned event finalization."
        ),
        argv=tuple(argv),
        safety_class="validated_mutation",
        mutation_class="runtime",
        display="Retry exact stale-claim release finalization",
    )


def _stale_claim_release_conflict_action(detail: str) -> NextAction:
    return NextAction.terminal(
        action_id="inspect_stale_claim_release_conflict",
        kind="blocked",
        disposition="proof_required",
        reason_code="stale_claim_release_evidence_conflict",
        reason_detail=detail,
        display="Inspect conflicting stale-claim release evidence",
        required_inputs=("canonical_stale_claim_release_generation",),
    )


def _pending_stale_claim_release_action(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
) -> tuple[dict[str, Any] | None, NextAction | None]:
    try:
        pending = pending_stale_claim_release(
            profile,
            workset_id=workset_id,
            task_id=task_id,
        )
    except StaleClaimReleaseConflictError as exc:
        conflict = {
            "stage": "ledger_conflict",
            "mutation_phase": "preflight",
            "request_event_id": None,
            "decision_event_id": None,
            "task_release_event_id": None,
            "workset_release_event_id": None,
            "error": str(exc),
        }
        return conflict, _stale_claim_release_conflict_action(str(exc))
    if pending is None:
        return None, None
    return pending, _stale_claim_release_action_from_pending(
        profile,
        workset_id=workset_id,
        owner_task_id=task_id,
        pending=pending,
    )


def _stale_claim_release_action_from_pending(
    profile: RepoProfile,
    *,
    workset_id: str,
    owner_task_id: str,
    pending: Mapping[str, Any],
) -> NextAction:
    if pending["stage"] in {"runtime_conflict", "ledger_conflict"}:
        detail = (
            "The durable stale-claim release ledger is conflicting or out of order."
            if pending["stage"] == "ledger_conflict"
            else "The stale-claim release matches neither its recorded pre-runtime nor post-runtime state."
        )
        return _stale_claim_release_conflict_action(detail)
    request = pending["request"]
    return NextAction.command(
        _stale_claim_release_retry_action(
            profile,
            workset_id=workset_id,
            task_id=owner_task_id,
            status=str(request["status"]),
            summary=str(request["summary"]),
            note=request.get("note"),
            request_event_id=str(pending["request_event_id"]),
            decision_event_id=(
                str(pending["decision_event_id"])
                if pending["decision_event_id"] is not None
                else None
            ),
        )
    )


def _pending_stale_claim_release_for_workset_action(
    profile: RepoProfile,
    *,
    workset_id: str,
) -> tuple[dict[str, Any] | None, NextAction | None]:
    """Project the task that owns the workset claim-set reservation."""

    try:
        reservation = pending_stale_claim_release_for_workset(
            profile,
            workset_id=workset_id,
        )
    except StaleClaimReleaseConflictError as exc:
        conflict = {
            "owner_task_id": None,
            "release": {
                "stage": "ledger_conflict",
                "mutation_phase": "preflight",
                "request_event_id": None,
                "decision_event_id": None,
                "task_release_event_id": None,
                "workset_release_event_id": None,
                "error": str(exc),
            },
        }
        return conflict, _stale_claim_release_conflict_action(str(exc))
    if reservation is None:
        return None, None
    owner_task_id = str(reservation["owner_task_id"])
    pending = reservation["release"]
    return reservation, _stale_claim_release_action_from_pending(
        profile,
        workset_id=workset_id,
        owner_task_id=owner_task_id,
        pending=pending,
    )


def _pending_task_runtime_transition_matches(
    pending: Mapping[str, Any],
    *,
    operation: str,
    actor: str,
    summary: str | None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
) -> bool:
    request = pending["request"]
    expected_status = (
        TASK_STATUS_CANCELED if operation == "cancel" else TASK_STATUS_PLANNED
    )
    normalized_summary = str(summary or "").strip() or None
    normalized_failure_class = failure_class
    if operation == "cancel" and normalized_failure_class is None:
        normalized_failure_class = FAILURE_CLASS_UNKNOWN
    return (
        request.get("status") == expected_status
        and request.get("actor") == str(actor or "").strip()
        and request.get("summary") == normalized_summary
        and request.get("failure_class")
        == (normalized_failure_class if operation == "cancel" else None)
        and request.get("recovery_action")
        == (
            str(recovery_action or "").strip() or None
            if operation == "cancel"
            else None
        )
        and bool(request.get("prompt_issue"))
        == (bool(prompt_issue) if operation == "cancel" else False)
        and bool(request.get("operator_issue"))
        == (bool(operator_issue) if operation == "cancel" else False)
    )


def _require_task_transition_actor(actor: str, *, operation: str) -> str:
    """Normalize task-state ownership before any runtime or event mutation."""
    resolved_actor = str(actor or "").strip()
    if not resolved_actor:
        raise BacklogError(f"task {operation} requires a nonempty actor")
    return resolved_actor


def cancel_task(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    summary: str | None = None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
    transition_request_event_id: str | None = None,
    transition_decision_event_id: str | None = None,
    _attempt_lock_held: bool = False,
) -> OperationResult:
    resolved_actor = _require_task_transition_actor(actor, operation="cancel")
    close_gate = _incomplete_close_gate(
        profile,
        operation="task.cancel",
        workset_id=workset_id,
        task_id=task_id,
        actor=resolved_actor,
    )
    if close_gate is not None:
        return close_gate
    resolved_summary = str(summary or "").strip() or None
    resolved_failure_class = str(failure_class or "").strip() or None
    resolved_recovery_action = str(recovery_action or "").strip() or None
    pending_stale_release, _pending_stale_action = (
        _pending_stale_claim_release_action(
            profile,
            workset_id=workset_id,
            task_id=task_id,
        )
    )
    if pending_stale_release is not None:
        return _recoverable_task_blocked_result(
            profile,
            operation="task.cancel",
            workset_id=workset_id,
            task_id=task_id,
            actor=resolved_actor,
        )
    pending_transition, _pending_action = _pending_task_runtime_transition_action(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    if transition_request_event_id is not None and pending_transition is not None and (
        pending_transition["stage"] in {"runtime_conflict", "ledger_conflict"}
        or pending_transition["request_event_id"] != transition_request_event_id
        or transition_decision_event_id is not None
        and pending_transition["decision_event_id"] != transition_decision_event_id
    ):
        return _task_runtime_transition_guard_conflict_result(
            profile,
            operation="cancel",
            workset_id=workset_id,
            task_id=task_id,
            actor=resolved_actor,
            request_event_id=transition_request_event_id,
            decision_event_id=transition_decision_event_id,
            detail="The identity-bound cancel retry no longer names the repairable transition.",
        )
    if pending_transition is not None and (
        pending_transition["stage"] in {"runtime_conflict", "ledger_conflict"}
        or not _pending_task_runtime_transition_matches(
            pending_transition,
            operation="cancel",
            actor=resolved_actor,
            summary=resolved_summary,
            failure_class=resolved_failure_class,
            recovery_action=resolved_recovery_action,
            prompt_issue=prompt_issue,
            operator_issue=operator_issue,
        )
    ):
        return _recoverable_task_blocked_result(
            profile,
            operation="task.cancel",
            workset_id=workset_id,
            task_id=task_id,
            actor=resolved_actor,
        )
    latest = latest_task_attempt(load_runtime_state(profile.paths), workset_id, task_id)
    if latest is not None and not _attempt_lock_held:
        with attempt_lifecycle_lock(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=latest.attempt_id,
        ):
            return cancel_task(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                actor=resolved_actor,
                summary=resolved_summary,
                failure_class=resolved_failure_class,
                recovery_action=resolved_recovery_action,
                prompt_issue=prompt_issue,
                operator_issue=operator_issue,
                transition_request_event_id=transition_request_event_id,
                transition_decision_event_id=transition_decision_event_id,
                _attempt_lock_held=True,
            )
    if latest is not None:
        locked_runtime_state = load_runtime_state(profile.paths)
        locked_latest = latest_task_attempt(
            locked_runtime_state,
            workset_id,
            task_id,
        )
        if locked_latest is None or locked_latest.attempt_id != latest.attempt_id:
            raise BacklogError(
                "task attempt changed while waiting for the cancel operation lock"
            )
        if locked_latest.status == ATTEMPT_STATUS_IN_PROGRESS:
            start_gate = _task_start_terminal_gate(
                profile,
                operation="task.cancel",
                workset_id=workset_id,
                task_id=task_id,
                attempt=locked_latest,
                actor=resolved_actor,
            )
            if start_gate is not None:
                return start_gate
            _require_workspace_adoption_start_evidence(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                runtime_state=locked_runtime_state,
                attempt=locked_latest,
            )
        transaction = load_landing_transaction(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=latest.attempt_id,
        )
        if transaction is not None and not transaction.terminal:
            return _recoverable_task_blocked_result(
                profile,
                operation="task.cancel",
                workset_id=workset_id,
                task_id=task_id,
                actor=resolved_actor,
            )
    try:
        payload = _task_state_payload(
            profile=profile,
            workset_id=workset_id,
            task_id=task_id,
            actor=resolved_actor,
            status=TASK_STATUS_CANCELED,
            summary=resolved_summary,
            failure_class=resolved_failure_class,
            recovery_action=resolved_recovery_action,
            prompt_issue=prompt_issue,
            operator_issue=operator_issue,
            expected_transition_request_event_id=transition_request_event_id,
            expected_transition_decision_event_id=transition_decision_event_id,
        )
    except TaskRuntimeTransitionGuardConflictError as exc:
        return _task_runtime_transition_guard_conflict_result(
            profile,
            operation="cancel",
            workset_id=workset_id,
            task_id=task_id,
            actor=resolved_actor,
            request_event_id=transition_request_event_id,
            decision_event_id=transition_decision_event_id,
            detail=str(exc),
        )
    except TaskRuntimeTransitionFinalizationError as exc:
        payload = {
            "workset_id": workset_id,
            "task_id": task_id,
            "actor": resolved_actor,
            "status": TASK_STATUS_CANCELED,
            "summary": resolved_summary,
            "failure_class": resolved_failure_class,
            "recovery_action": resolved_recovery_action,
            "prompt_issue": prompt_issue,
            "operator_issue": operator_issue,
            "error": str(exc),
            "transition_request_event_id": exc.request_event_id,
            "transition_decision_event_id": exc.decision_event_id,
            "transition_owned_event_id": exc.owned_event_id,
        }
        return _task_mutation_result(
            profile,
            operation="task.cancel",
            workset_id=workset_id,
            task_id=task_id,
            legacy_payload=payload,
            mutation_phase=exc.mutation_phase,
            operation_status="partial",
            actor=resolved_actor,
            mutation_started=exc.mutation_started,
            mutation_completed=exc.mutation_phase == "event_finalized",
            next_action_override=NextAction.command(
                _task_runtime_transition_retry_action(
                    profile,
                    operation="cancel",
                    workset_id=workset_id,
                    task_id=task_id,
                    actor=resolved_actor,
                    summary=resolved_summary,
                    failure_class=resolved_failure_class,
                    recovery_action=resolved_recovery_action,
                    prompt_issue=prompt_issue,
                    operator_issue=operator_issue,
                    transition_request_event_id=exc.request_event_id,
                    transition_decision_event_id=(
                        _durable_task_runtime_transition_event_id(
                            profile,
                            exc.decision_event_id,
                        )
                    ),
                )
            ),
        )
    runtime_changed = bool(payload["transition_runtime_changed"])
    events_changed = bool(payload["transition_events_changed"])
    mutation_started = runtime_changed or events_changed
    mutation_phase = (
        "runtime_and_event_finalized"
        if runtime_changed and events_changed
        else "runtime_finalized"
        if runtime_changed
        else "event_finalized"
        if events_changed
        else "none"
    )
    return _task_mutation_result(
        profile,
        operation="task.cancel",
        workset_id=workset_id,
        task_id=task_id,
        legacy_payload=payload,
        mutation_phase=mutation_phase,
        actor=resolved_actor,
        mutation_started=mutation_started,
        mutation_completed=mutation_started,
    )


def reopen_task(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    summary: str | None = None,
    transition_request_event_id: str | None = None,
    transition_decision_event_id: str | None = None,
    _attempt_lock_held: bool = False,
) -> OperationResult:
    resolved_actor = _require_task_transition_actor(actor, operation="reopen")
    close_gate = _incomplete_close_gate(
        profile,
        operation="task.reopen",
        workset_id=workset_id,
        task_id=task_id,
        actor=resolved_actor,
    )
    if close_gate is not None:
        return close_gate
    resolved_summary = str(summary or "").strip() or None
    pending_stale_release, _pending_stale_action = (
        _pending_stale_claim_release_action(
            profile,
            workset_id=workset_id,
            task_id=task_id,
        )
    )
    if pending_stale_release is not None:
        return _recoverable_task_blocked_result(
            profile,
            operation="task.reopen",
            workset_id=workset_id,
            task_id=task_id,
            actor=resolved_actor,
        )
    pending_transition, _pending_action = _pending_task_runtime_transition_action(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    if transition_request_event_id is not None and pending_transition is not None and (
        pending_transition["stage"] in {"runtime_conflict", "ledger_conflict"}
        or pending_transition["request_event_id"] != transition_request_event_id
        or transition_decision_event_id is not None
        and pending_transition["decision_event_id"] != transition_decision_event_id
    ):
        return _task_runtime_transition_guard_conflict_result(
            profile,
            operation="reopen",
            workset_id=workset_id,
            task_id=task_id,
            actor=resolved_actor,
            request_event_id=transition_request_event_id,
            decision_event_id=transition_decision_event_id,
            detail="The identity-bound reopen retry no longer names the repairable transition.",
        )
    if pending_transition is not None and (
        pending_transition["stage"] in {"runtime_conflict", "ledger_conflict"}
        or not _pending_task_runtime_transition_matches(
            pending_transition,
            operation="reopen",
            actor=resolved_actor,
            summary=resolved_summary,
        )
    ):
        return _recoverable_task_blocked_result(
            profile,
            operation="task.reopen",
            workset_id=workset_id,
            task_id=task_id,
            actor=resolved_actor,
        )
    latest = latest_task_attempt(load_runtime_state(profile.paths), workset_id, task_id)
    if latest is not None and not _attempt_lock_held:
        with attempt_lifecycle_lock(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=latest.attempt_id,
        ):
            return reopen_task(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                actor=resolved_actor,
                summary=resolved_summary,
                transition_request_event_id=transition_request_event_id,
                transition_decision_event_id=transition_decision_event_id,
                _attempt_lock_held=True,
            )
    if latest is not None:
        locked_latest = latest_task_attempt(
            load_runtime_state(profile.paths),
            workset_id,
            task_id,
        )
        if locked_latest is None or locked_latest.attempt_id != latest.attempt_id:
            raise BacklogError(
                "task attempt changed while waiting for the reopen operation lock"
            )
        transaction = load_landing_transaction(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=latest.attempt_id,
        )
        if transaction is not None and not transaction.terminal:
            return _recoverable_task_blocked_result(
                profile,
                operation="task.reopen",
                workset_id=workset_id,
                task_id=task_id,
                actor=resolved_actor,
            )
    try:
        payload = _task_state_payload(
            profile=profile,
            workset_id=workset_id,
            task_id=task_id,
            actor=resolved_actor,
            status=TASK_STATUS_PLANNED,
            summary=resolved_summary,
            expected_transition_request_event_id=transition_request_event_id,
            expected_transition_decision_event_id=transition_decision_event_id,
        )
    except TaskRuntimeTransitionGuardConflictError as exc:
        return _task_runtime_transition_guard_conflict_result(
            profile,
            operation="reopen",
            workset_id=workset_id,
            task_id=task_id,
            actor=resolved_actor,
            request_event_id=transition_request_event_id,
            decision_event_id=transition_decision_event_id,
            detail=str(exc),
        )
    except TaskRuntimeTransitionFinalizationError as exc:
        payload = {
            "workset_id": workset_id,
            "task_id": task_id,
            "actor": resolved_actor,
            "status": TASK_STATUS_PLANNED,
            "summary": resolved_summary,
            "error": str(exc),
            "transition_request_event_id": exc.request_event_id,
            "transition_decision_event_id": exc.decision_event_id,
            "transition_owned_event_id": exc.owned_event_id,
        }
        return _task_mutation_result(
            profile,
            operation="task.reopen",
            workset_id=workset_id,
            task_id=task_id,
            legacy_payload=payload,
            mutation_phase=exc.mutation_phase,
            operation_status="partial",
            actor=resolved_actor,
            mutation_started=exc.mutation_started,
            mutation_completed=exc.mutation_phase == "event_finalized",
            next_action_override=NextAction.command(
                _task_runtime_transition_retry_action(
                    profile,
                    operation="reopen",
                    workset_id=workset_id,
                    task_id=task_id,
                    actor=resolved_actor,
                    summary=resolved_summary,
                    transition_request_event_id=exc.request_event_id,
                    transition_decision_event_id=(
                        _durable_task_runtime_transition_event_id(
                            profile,
                            exc.decision_event_id,
                        )
                    ),
                )
            ),
        )
    runtime_changed = bool(payload["transition_runtime_changed"])
    events_changed = bool(payload["transition_events_changed"])
    mutation_started = runtime_changed or events_changed
    mutation_phase = (
        "runtime_and_event_finalized"
        if runtime_changed and events_changed
        else "runtime_finalized"
        if runtime_changed
        else "event_finalized"
        if events_changed
        else "none"
    )
    return _task_mutation_result(
        profile,
        operation="task.reopen",
        workset_id=workset_id,
        task_id=task_id,
        legacy_payload=payload,
        mutation_phase=mutation_phase,
        actor=resolved_actor,
        mutation_started=mutation_started,
        mutation_completed=mutation_started,
    )


def _cleanup_event_retry_action(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    cleanup_payload: dict[str, Any],
) -> LifecycleAction:
    argv = [
        _lifecycle_blackdog_executable(profile, cleanup_payload),
        "task",
        "cleanup",
        f"--project-root={profile.paths.project_root}",
        f"--workset={workset_id}",
        f"--task={task_id}",
    ]
    worktree_path = str(cleanup_payload.get("worktree_path") or "").strip()
    branch = str(cleanup_payload.get("branch") or "").strip()
    if worktree_path:
        argv.append(f"--path={worktree_path}")
    if branch:
        argv.append(f"--branch={branch}")
    return LifecycleAction(
        action_id="finalize_cleanup_event",
        disposition="retryable",
        reason_code="cleanup_event_finalization_unconfirmed",
        reason_detail="Filesystem cleanup completed, but its deterministic event write was not confirmed.",
        argv=tuple(argv),
        safety_class="validated_mutation",
        mutation_class="event",
        display="Finalize deterministic cleanup evidence",
    )


def cleanup_task(
    profile: RepoProfile,
    *,
    workset_id: str | None = None,
    task_id: str | None = None,
    path: str | None = None,
    branch: str | None = None,
    cwd: Path | None = None,
) -> OperationResult:
    resolved_workset, resolved_task, _attempt = _resolve_task_command_target(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        cwd=cwd,
        allow_latest=True,
    )
    close_gate = _incomplete_close_gate(
        profile,
        operation="task.cleanup",
        workset_id=resolved_workset,
        task_id=resolved_task,
    )
    if close_gate is not None:
        return close_gate
    try:
        payload = cleanup_task_worktree(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            path=path,
            branch=branch,
        )
    except CleanupOwnershipError as exc:
        primary_root = find_primary_worktree(profile.paths.project_root)
        inspect_action = LifecycleAction(
            action_id="inspect_cleanup_ownership",
            disposition="proof_required",
            reason_code="cleanup_workspace_identity_mismatch",
            reason_detail=str(exc),
            argv=(
                "git",
                "-C",
                str(primary_root),
                "worktree",
                "list",
                "--porcelain",
            ),
            safety_class="read_only",
            mutation_class="none",
            display="Inspect task worktree ownership before cleanup",
        )
        return _task_mutation_result(
            profile,
            operation="task.cleanup",
            workset_id=resolved_workset,
            task_id=resolved_task,
            legacy_payload=exc.refusal_payload(),
            mutation_phase="none",
            operation_status="blocked",
            mutation_started=False,
            mutation_completed=False,
            next_action_override=NextAction.command(inspect_action),
        )
    except CleanupPostMutationError as exc:
        return _task_mutation_result(
            profile,
            operation="task.cleanup",
            workset_id=resolved_workset,
            task_id=resolved_task,
            legacy_payload=exc.partial_payload(),
            mutation_phase="worktree_removed_branch_cleanup_pending",
            operation_status="partial",
            mutation_started=True,
            mutation_completed=False,
        )
    except CleanupEventFinalizationError as exc:
        partial_payload = exc.partial_payload()
        retry_action = _cleanup_event_retry_action(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            cleanup_payload=partial_payload,
        )
        return _task_mutation_result(
            profile,
            operation="task.cleanup",
            workset_id=resolved_workset,
            task_id=resolved_task,
            legacy_payload=partial_payload,
            mutation_phase="cleanup_event_finalization_pending",
            operation_status="partial",
            mutation_started=True,
            mutation_completed=False,
            next_action_override=NextAction.command(retry_action),
        )
    if payload.get("cleanup_refused"):
        runtime_state = load_runtime_state(profile.paths)
        latest = latest_task_attempt(runtime_state, resolved_workset, resolved_task)
        transaction = (
            load_landing_transaction(
                profile,
                workset_id=resolved_workset,
                task_id=resolved_task,
                attempt_id=latest.attempt_id,
            )
            if latest is not None
            else None
        )
        if transaction is None or transaction.complete:
            raise LandingTransactionError(
                "cleanup refusal lost its incomplete landing transaction"
            )
        return _task_mutation_result(
            profile,
            operation="task.cleanup",
            workset_id=resolved_workset,
            task_id=resolved_task,
            legacy_payload=payload,
            mutation_phase=_landing_operation_phase(transaction),
            operation_status="blocked",
            mutation_started=False,
            mutation_completed=False,
            next_action_override=NextAction.command(
                _landing_abort_close_action(transaction)
                if transaction.aborted
                else _landing_resume_action(transaction.intent)
            ),
        )
    changed_git_state = bool(payload.get("worktree_existed") or payload.get("deleted_branch"))
    event_appended = bool(payload.get("event_appended"))
    mutation_started = changed_git_state or event_appended
    if changed_git_state and event_appended:
        mutation_phase = "git_and_filesystem_and_event_finalized"
    elif changed_git_state:
        mutation_phase = "git_and_filesystem_finalized"
    elif event_appended:
        mutation_phase = "event_finalized"
    else:
        mutation_phase = "none"
    return _task_mutation_result(
        profile,
        operation="task.cleanup",
        workset_id=resolved_workset,
        task_id=resolved_task,
        legacy_payload=payload,
        mutation_phase=mutation_phase,
        mutation_started=mutation_started,
        mutation_completed=mutation_started,
    )


def _task_recovery_state(
    *,
    active_attempt: bool,
    stale_claim: bool,
    worktree_exists: bool,
    worktree_dirty: bool,
    branch_ahead_of_target: bool,
    reference_issue: bool,
) -> str:
    if stale_claim:
        return "stale_claim"
    if active_attempt:
        return "active_attempt"
    if reference_issue:
        return "stale_reference"
    if worktree_exists and (worktree_dirty or branch_ahead_of_target):
        return "retained_dirty_worktree"
    if worktree_exists:
        return "cleanup_ready"
    return "idle"


def _lifecycle_blackdog_executable(profile: RepoProfile, payload: dict[str, Any]) -> str:
    candidates: list[Path] = []
    worktree_path = str(payload.get("worktree_path") or "").strip()
    if worktree_path:
        candidates.append(Path(worktree_path) / ".VE" / "bin" / "blackdog")
    candidates.append(profile.paths.project_root / ".VE" / "bin" / "blackdog")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return "blackdog"


def _latest_task_event_actor(
    events: Sequence[dict[str, Any]],
    *,
    workset_id: str,
    task_id: str,
) -> str | None:
    """Recover the last explicitly persisted task-state actor without schema growth."""
    for event in reversed(events):
        if event.get("type") not in {"task.cancel", "task.reopen"}:
            continue
        event_payload = event.get("payload")
        if not isinstance(event_payload, dict):
            continue
        if event_payload.get("workset_id") != workset_id or event_payload.get("task_id") != task_id:
            continue
        actor = str(event.get("actor") or "").strip()
        if actor:
            return actor
    return None


def _latest_task_cleanup_evidence(
    events: Sequence[dict[str, Any]],
    *,
    workset_id: str,
    task_id: str,
    branch: str | None,
    attempt_id: str | None,
    attempt_ended_at: str | None,
) -> dict[str, Any] | None:
    """Return bounded proof that Blackdog itself removed the recorded task branch."""
    for event in reversed(events):
        if event.get("type") != "worktree.cleanup":
            continue
        event_payload = event.get("payload")
        if not isinstance(event_payload, dict):
            continue
        if event_payload.get("workset_id") != workset_id or event_payload.get("task_id") != task_id:
            continue
        if branch is not None and event_payload.get("branch") != branch:
            continue
        event_attempt_id = str(event_payload.get("attempt_id") or "").strip() or None
        if event_attempt_id is not None and event_attempt_id != attempt_id:
            continue
        if event_attempt_id is None and attempt_ended_at is not None:
            event_at = parse_iso(str(event.get("at") or "").strip() or None)
            ended_at = parse_iso(attempt_ended_at)
            if event_at is None or ended_at is None or event_at < ended_at:
                continue
        if (
            event_payload.get("cleanup_complete") is not True
            and event_payload.get("deleted_branch") is not True
        ):
            continue
        return {
            "event_id": event.get("event_id"),
            "event_type": event.get("type"),
            "attempt_id": event_attempt_id,
            "branch": event_payload.get("branch"),
            "worktree_path": event_payload.get("worktree_path"),
            "cleanup_complete": bool(event_payload.get("cleanup_complete", True)),
            "deleted_branch": event_payload.get("deleted_branch"),
            "branch_cleanup_proof": event_payload.get("branch_cleanup_proof"),
        }
    return None


def _verify_resume_prompt_file(
    profile: RepoProfile,
    *,
    role: str,
    expected_hash: str | None,
    source: str | None,
    mode: str | None,
    replay_artifact_path: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Verify durable replay content, with source-file fallback for legacy rows."""
    if not expected_hash:
        return None, f"{role}_prompt_hash_missing", f"The recorded {role} prompt hash is missing."
    if not mode:
        return None, f"{role}_prompt_mode_missing", f"The recorded {role} prompt mode is missing."
    artifact_path = str(replay_artifact_path or "").strip() or None
    if artifact_path is not None:
        try:
            verified = verify_prompt_artifact(
                profile.paths.control_dir,
                prompt_hash=expected_hash,
                replay_artifact_path=artifact_path,
            )
        except PromptArtifactError as exc:
            return None, f"{role}_{exc.code}", str(exc)
        return str(verified), None, None
    source_text = str(source or "").strip()
    if not source_text:
        return None, f"{role}_prompt_source_missing", f"The recorded {role} prompt source is missing."
    if source_text == "stdin" or source_text.startswith("inline:"):
        return (
            None,
            f"{role}_prompt_source_not_replayable",
            f"The recorded {role} prompt source {source_text!r} is not a replayable file.",
        )
    candidate = Path(source_text).expanduser()
    if not candidate.is_absolute():
        candidate = profile.paths.project_root / candidate
    try:
        prompt_text = candidate.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, f"{role}_prompt_file_missing", f"The recorded {role} prompt file is missing: {candidate}"
    except (OSError, UnicodeError) as exc:
        return None, f"{role}_prompt_file_unreadable", f"The recorded {role} prompt file cannot be read: {exc}"
    try:
        receipt = create_prompt_receipt(prompt_text, source=source_text, mode=mode)
    except ValueError as exc:
        return None, f"{role}_prompt_receipt_invalid", str(exc)
    if receipt.prompt_hash != expected_hash:
        return (
            None,
            f"{role}_prompt_hash_mismatch",
            f"The recorded {role} prompt file changed after the attempt was created.",
        )
    return str(candidate.resolve()), None, None


def _resume_lineage(profile: RepoProfile, payload: dict[str, Any]) -> dict[str, Any]:
    execution_hash = str(payload.get("execution_prompt_hash") or "").strip() or None
    execution_source = str(payload.get("execution_prompt_source") or "").strip() or None
    execution_mode = str(payload.get("execution_prompt_mode") or "").strip() or None
    execution_artifact = (
        str(payload.get("execution_prompt_replay_artifact_path") or "").strip()
        or None
    )
    request_hash = str(payload.get("user_prompt_hash") or "").strip() or None
    request_source = str(payload.get("user_prompt_source") or "").strip() or None
    request_mode = str(payload.get("user_prompt_mode") or "").strip() or None
    request_artifact = (
        str(payload.get("user_prompt_replay_artifact_path") or "").strip()
        or None
    )
    request_distinct = (request_hash, request_source, request_mode) != (
        execution_hash,
        execution_source,
        execution_mode,
    )
    execution_file, issue_code, issue_detail = _verify_resume_prompt_file(
        profile,
        role="execution",
        expected_hash=execution_hash,
        source=execution_source,
        mode=execution_mode,
        replay_artifact_path=execution_artifact,
    )
    request_file: str | None = None
    if issue_code is None and request_distinct:
        if request_mode != PROMPT_MODE_RAW:
            issue_code = "request_prompt_mode_not_replayable"
            issue_detail = (
                f"The recorded request prompt mode {request_mode!r} cannot be preserved by task begin."
            )
        else:
            request_file, issue_code, issue_detail = _verify_resume_prompt_file(
                profile,
                role="request",
                expected_hash=request_hash,
                source=request_source,
                mode=request_mode,
                replay_artifact_path=request_artifact,
            )
    elif issue_code is None and request_hash is None:
        issue_code = "request_prompt_hash_missing"
        issue_detail = "The recorded request prompt hash is missing; exact request lineage is unknown."
    return {
        "status": "blocked" if issue_code else "verified",
        "execution_prompt_file": execution_file,
        "request_file": request_file,
        "request_distinct": request_distinct,
        "issue_code": issue_code,
        "issue_detail": issue_detail,
    }


def _task_surface_actions(actions: list[str]) -> list[str]:
    """Preserve the legacy task-surface prose while typed actions own execution."""
    rewritten: list[str] = []
    for action in actions:
        text = action.replace("blackdog worktree land", "blackdog task land")
        text = text.replace(
            "blackdog worktree close --status blocked|failed|abandoned",
            "blackdog task close --status blocked|failed|abandoned",
        )
        text = text.replace("blackdog worktree cleanup", "blackdog task cleanup")
        rewritten.append(text)
    return rewritten


def _legacy_command_template(
    command: str,
    *,
    reason: str,
    disposition: str,
) -> dict[str, Any]:
    """Retain the pre-typed schema without representing a template as executable."""
    return {
        "command": command,
        "reason": reason,
        "disposition": disposition,
        "argv": None,
        "executable": False,
        "template": True,
        "deprecated": True,
    }


def _legacy_recovery_guidance(
    *,
    branch: str | None,
    target_branch: str | None,
    branch_exists: bool | None,
    target_branch_exists: bool | None,
    branch_ahead_error: str | None,
    landed_cleanup_complete: bool,
    stale_claim: bool,
    active_attempt: bool,
    selected_attempt: bool,
    task_worktree: Path | None,
    worktree_exists: bool,
    worktree_dirty_paths: list[str],
    branch_ahead: bool,
    primary_dirty: bool,
    reference_issue: bool,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return the additive compatibility view used before typed ``next_action``."""
    recommended_actions: list[str] = []
    if branch_exists is False and branch and not landed_cleanup_complete:
        recommended_actions.append(
            f"restore task branch `{branch}` before landing or close/cancel this stale task"
        )
    if target_branch_exists is False and target_branch:
        recommended_actions.append(
            f"restore target branch `{target_branch}` or close/cancel this stale task if it is obsolete"
        )
    if branch_ahead_error and branch_exists is not False and target_branch_exists is not False:
        recommended_actions.append("inspect the recorded task branch and target branch before landing")
    if stale_claim:
        if task_worktree is not None and (worktree_dirty_paths or branch_ahead):
            recommended_actions.append(
                "inspect the retained task workspace before releasing the stale claim"
            )
        recommended_actions.append(
            "run `blackdog task recover --release-stale-claim --status blocked|failed|abandoned "
            "--summary \"...\"` to release the stale claim"
        )
    elif active_attempt:
        if primary_dirty:
            recommended_actions.append(
                "clean or land the primary worktree changes before `blackdog task land`"
            )
        if not worktree_exists:
            recommended_actions.append(
                "restore the task workspace or close the attempt before starting new work"
            )
        if worktree_dirty_paths or branch_ahead:
            recommended_actions.append(
                "run `blackdog worktree land` to create the canonical landed commit"
            )
        recommended_actions.append(
            "run `blackdog worktree close --status blocked|failed|abandoned` to close without landing"
        )
    elif selected_attempt and reference_issue:
        recommended_actions.append(
            "use `blackdog task cancel` if this stale task should stay out of normal ready work"
        )
    elif task_worktree is None and not landed_cleanup_complete:
        recommended_actions.append("start a new WTAM attempt for this task")
    elif worktree_dirty_paths or branch_ahead:
        recommended_actions.append(
            "inspect the retained task workspace and clean or discard its changes before cleanup"
        )
    if task_worktree is not None and not worktree_dirty_paths:
        recommended_actions.append(
            "run `blackdog task cleanup` if the task workspace is no longer needed"
        )

    recommended_commands: list[dict[str, Any]] = []
    if stale_claim:
        recommended_commands.append(
            _legacy_command_template(
                'blackdog task recover --release-stale-claim --status blocked|failed|abandoned --summary "..."',
                reason="release a stale task claim without deleting retained work",
                disposition="retryable_after_operator_choice",
            )
        )
    elif active_attempt:
        if worktree_dirty_paths or branch_ahead:
            recommended_commands.append(
                _legacy_command_template(
                    'blackdog task land --summary "..."',
                    reason="land the active task attempt through the canonical success path",
                    disposition="auto_safe_after_validation",
                )
            )
        recommended_commands.append(
            _legacy_command_template(
                'blackdog task close --status blocked|failed|abandoned --summary "..."',
                reason="close the active attempt without landing code",
                disposition="operator_choice",
            )
        )
    elif selected_attempt and reference_issue:
        recommended_commands.append(
            _legacy_command_template(
                'blackdog task cancel --summary "..."',
                reason="keep stale task state out of normal ready work",
                disposition="operator_choice",
            )
        )
    elif task_worktree is None and not landed_cleanup_complete:
        recommended_commands.append(
            _legacy_command_template(
                'blackdog task begin --prompt "..."',
                reason="start a new WTAM attempt for this task",
                disposition="auto_safe",
            )
        )
    if task_worktree is not None and not worktree_dirty_paths:
        recommended_commands.append(
            _legacy_command_template(
                "blackdog task cleanup",
                reason="remove retained task workspace after proving it is disposable",
                disposition="auto_safe_if_cleanup_ready",
            )
        )
    elif task_worktree is not None:
        recommended_commands.append(
            _legacy_command_template(
                "git status --short",
                reason="inspect retained task workspace before cleanup",
                disposition="read_only",
            )
        )
    return recommended_actions, recommended_commands


def _lifecycle_context(profile: RepoProfile, payload: dict[str, Any]) -> LifecycleContext:
    resume_lineage = payload.get("resume_lineage")
    if not isinstance(resume_lineage, dict):
        resume_lineage = {}
    return LifecycleContext(
        project_root=str(profile.paths.project_root),
        workset_id=str(payload["workset_id"]),
        task_id=str(payload["task_id"]),
        actor=str(payload.get("actor") or "").strip() or None,
        task_status=str(payload.get("task_runtime_status") or "").strip() or None,
        attempt_status=str(payload.get("latest_attempt_status") or "").strip() or None,
        attempt_id=str(payload.get("attempt_id") or "").strip() or None,
        active_attempt=bool(payload.get("active_attempt")),
        worktree_path=str(payload.get("worktree_path") or "").strip() or None,
        worktree_exists=bool(payload.get("worktree_exists")),
        worktree_dirty=bool(payload.get("worktree_dirty")),
        branch_ahead_of_target=bool(payload.get("branch_ahead_of_target")),
        primary_worktree=str(payload.get("primary_worktree") or "").strip() or None,
        primary_dirty=bool(payload.get("primary_dirty")),
        branch_exists=payload.get("branch_exists"),
        target_branch_exists=payload.get("target_branch_exists"),
        stale_claim=bool(payload.get("stale_claim")),
        reference_issue=bool(payload.get("reference_issue")),
        reference_issue_code=str(payload.get("reference_issue_code") or "").strip() or None,
        reference_issue_detail=str(payload.get("reference_issue_detail") or "").strip() or None,
        blackdog_executable=_lifecycle_blackdog_executable(profile, payload),
        execution_prompt_hash=str(payload.get("execution_prompt_hash") or "").strip() or None,
        execution_prompt_source=str(payload.get("execution_prompt_source") or "").strip() or None,
        execution_prompt_mode=str(payload.get("execution_prompt_mode") or "").strip() or None,
        request_prompt_hash=str(payload.get("user_prompt_hash") or "").strip() or None,
        request_prompt_source=str(payload.get("user_prompt_source") or "").strip() or None,
        request_prompt_mode=str(payload.get("user_prompt_mode") or "").strip() or None,
        resume_execution_prompt_file=(
            str(resume_lineage.get("execution_prompt_file") or "").strip() or None
        ),
        resume_request_file=str(resume_lineage.get("request_file") or "").strip() or None,
        resume_request_distinct=bool(resume_lineage.get("request_distinct")),
        resume_lineage_issue_code=str(resume_lineage.get("issue_code") or "").strip() or None,
        resume_lineage_issue_detail=str(resume_lineage.get("issue_detail") or "").strip() or None,
        resume_start_incomplete=bool(payload.get("resume_start_incomplete")),
        resume_start_issue_code=(
            str(payload.get("resume_start_issue_code") or "").strip() or None
        ),
        resume_start_issue_detail=(
            str(payload.get("resume_start_issue_detail") or "").strip() or None
        ),
        reconciliation_candidate=bool(payload.get("reconciliation_candidate")),
        landing_reconcile_argv=tuple(payload.get("landing_reconcile_argv") or ()),
        reconciliation_action_id=(
            str(payload.get("reconciliation_action_id") or "").strip() or None
        ),
        reconciliation_reason_code=(
            str(payload.get("reconciliation_reason_code") or "").strip() or None
        ),
        reconciliation_reason_detail=(
            str(payload.get("reconciliation_reason_detail") or "").strip() or None
        ),
        landing_transaction_incomplete=bool(
            payload.get("landing_transaction_incomplete")
        ),
        landing_last_phase=(
            str(payload.get("landing_last_phase") or "").strip() or None
        ),
        landing_resume_argv=tuple(payload.get("landing_resume_argv") or ()),
        workspace_adoption_eligible=bool(payload.get("workspace_adoption_eligible")),
        workspace_adoption_argv=tuple(payload.get("workspace_adoption_argv") or ()),
        workspace_adoption_issue_code=(
            str(payload.get("workspace_adoption_issue_code") or "").strip() or None
        ),
        workspace_adoption_issue_detail=(
            str(payload.get("workspace_adoption_issue_detail") or "").strip() or None
        ),
        active_workspace_adoption=bool(payload.get("active_workspace_adoption")),
        workspace_adoption_relation=(
            str(payload.get("workspace_adoption_relation") or "").strip() or None
        ),
        workspace_adoption_operation=(
            str(payload.get("workspace_adoption_operation") or "").strip() or None
        ),
        workspace_adoption_rebase_argv=tuple(
            payload.get("workspace_adoption_rebase_argv") or ()
        ),
        workspace_adoption_candidate_arrived=bool(
            payload.get("workspace_adoption_candidate_arrived")
        ),
        workspace_adoption_completion_pending=bool(
            payload.get("workspace_adoption_completion_pending")
        ),
        workspace_adoption_completion_argv=tuple(
            payload.get("workspace_adoption_completion_argv") or ()
        ),
        source_git_operation=(
            str(payload.get("source_git_operation") or "").strip() or None
        ),
        source_git_operation_detail=(
            str(payload.get("source_git_operation_detail") or "").strip() or None
        ),
        landing_correction_state=(
            str(payload.get("landing_correction_state") or "").strip() or None
        ),
        landing_correction_resume_argv=tuple(
            payload.get("landing_correction_resume_argv") or ()
        ),
        landing_correction_worktree_path=(
            str(payload.get("landing_correction_worktree_path") or "").strip()
            or None
        ),
        landing_correction_branch=(
            str(payload.get("landing_correction_branch") or "").strip() or None
        ),
        landing_correction_target_branch=(
            str(payload.get("landing_correction_target_branch") or "").strip()
            or None
        ),
    )


def _attach_next_action(profile: RepoProfile, payload: dict[str, Any]) -> NextAction:
    next_action = decide_next_action(_lifecycle_context(profile, payload))
    payload["next_action"] = next_action.to_dict()
    return next_action


def _task_recovery_payload(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: Any | None = None,
    primary_root: Path | None = None,
    primary_dirty: bool | None = None,
    primary_dirty_paths: list[str] | None = None,
    worktree_by_branch: dict[str, Path] | None = None,
    ref_cache: dict[str, str | None] | None = None,
    branch_ahead_cache: dict[tuple[str, str], tuple[bool, str | None]] | None = None,
    changed_paths_cache: dict[tuple[str, str | None], list[str]] | None = None,
    events: Sequence[dict[str, Any]] | None = None,
    include_reconciliation_detection: bool = False,
) -> dict[str, Any]:
    _workset, task = _require_workset_and_task(profile, workset_id=workset_id, task_id=task_id)
    runtime_state = runtime_state or load_runtime_state(profile.paths)
    runtime_task_claims = task_claim_index(runtime_state, workset_id)
    current_task_claim = runtime_task_claims.get(task_id)
    current_workset_claim = workset_claim(runtime_state, workset_id)
    runtime_task_state = task_state_index(runtime_state, workset_id).get(
        task_id,
        TaskRuntimeRecord(task_id=task_id, status=TASK_STATUS_PLANNED),
    )
    active_attempt = active_task_attempt(runtime_state, workset_id, task_id)
    latest_attempt = latest_task_attempt(runtime_state, workset_id, task_id)
    selected_attempt = active_attempt or latest_attempt
    landing_transaction = (
        load_landing_transaction(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=latest_attempt.attempt_id,
        )
        if latest_attempt is not None
        else None
    )
    landing_resume_action = None
    if landing_transaction is not None:
        if landing_transaction.outcome == "abort_in_progress":
            landing_resume_action = _landing_abort_close_action(landing_transaction)
        elif landing_transaction.outcome == "landing_in_progress":
            landing_resume_action = _landing_resume_action(landing_transaction.intent)
    events = load_events(profile.paths.events_file) if events is None else events
    branch = selected_attempt.branch if selected_attempt is not None else None
    target_branch = selected_attempt.target_branch if selected_attempt is not None else None
    recorded_worktree_path = selected_attempt.worktree_path if selected_attempt is not None else None
    task_worktree = (
        _resolve_attempt_worktree(
            profile,
            branch=branch,
            worktree_path=recorded_worktree_path,
            worktree_by_branch=worktree_by_branch,
        )
        if selected_attempt is not None
        else None
    )
    worktree_exists = task_worktree is not None and task_worktree.exists()
    worktree_dirty_paths = _worktree_changed_paths(profile, task_worktree) if task_worktree is not None else []
    source_git_operation: str | None = None
    source_git_operation_detail: str | None = None
    if active_attempt is not None and worktree_exists and task_worktree is not None:
        try:
            source_git_operation = _in_progress_git_operation(task_worktree)
        except WorktreeError:
            source_git_operation = "inspection_error"
            source_git_operation_detail = (
                "Blackdog could not prove that the retained task workspace is free "
                "of an in-progress Git operation. The current landing agent must "
                "inspect it without resetting or discarding unique work."
            )
        else:
            if source_git_operation is not None:
                source_git_operation_detail = (
                    f"The retained task workspace reports {source_git_operation!r}. "
                    "Blackdog will not infer ownership, abort it, or continue landing; "
                    "the current landing agent must preserve unique work and restore a "
                    "coherent Git state."
                )
    if selected_attempt is None:
        branch_ahead = False
        branch_inspection = GitReferenceInspection(
            role="task_branch",
            ref=None,
            state="metadata_missing",
            detail="no attempt has recorded task branch metadata",
        )
        target_inspection = GitReferenceInspection(
            role="target_branch",
            ref=None,
            state="metadata_missing",
            detail="no attempt has recorded target branch metadata",
        )
        reference_issue_code = None
        reference_issue_detail = None
    else:
        (
            branch_ahead,
            branch_inspection,
            target_inspection,
            reference_issue_code,
            reference_issue_detail,
        ) = _recovery_branch_state(
            profile,
            branch=branch,
            target_branch=target_branch,
            primary_root=primary_root,
            ref_cache=ref_cache,
            branch_ahead_cache=branch_ahead_cache,
        )
    branch_exists = branch_inspection.exists
    target_branch_exists = target_inspection.exists
    cleanup_evidence = _latest_task_cleanup_evidence(
        events,
        workset_id=workset_id,
        task_id=task_id,
        branch=branch,
        attempt_id=selected_attempt.attempt_id if selected_attempt is not None else None,
        attempt_ended_at=selected_attempt.ended_at if selected_attempt is not None else None,
    )
    landed_cleanup_complete = bool(
        active_attempt is None
        and selected_attempt is not None
        and selected_attempt.status == ATTEMPT_STATUS_SUCCESS
        and selected_attempt.landed_commit
        and not worktree_exists
        and branch_exists is False
        and target_branch_exists is True
    )
    terminal_cleanup_complete = bool(
        active_attempt is None
        and selected_attempt is not None
        and selected_attempt.status
        in {ATTEMPT_STATUS_BLOCKED, ATTEMPT_STATUS_FAILED, ATTEMPT_STATUS_ABANDONED}
        and not worktree_exists
        and branch_exists is False
        and target_branch_exists is True
        and cleanup_evidence is not None
    )
    if (landed_cleanup_complete or terminal_cleanup_complete) and reference_issue_code == "task_branch_missing":
        reference_issue_code = None
        reference_issue_detail = None
    reference_issue = reference_issue_code is not None
    branch_ahead_error = reference_issue_detail
    primary_root = primary_root or find_primary_worktree(profile.paths.project_root)
    if primary_dirty_paths is None:
        primary_dirty_paths = _managed_dirty_paths(profile, primary_root)
    if primary_dirty is None:
        primary_dirty = bool(primary_dirty_paths)
    stale_claim = current_task_claim is not None and active_attempt is None
    recovery_state = _task_recovery_state(
        active_attempt=active_attempt is not None,
        stale_claim=stale_claim,
        worktree_exists=worktree_exists,
        worktree_dirty=bool(worktree_dirty_paths),
        branch_ahead_of_target=branch_ahead,
        reference_issue=reference_issue,
    )
    failure_class = runtime_task_state.failure_class
    recovery_action = runtime_task_state.recovery_action
    prompt_issue = runtime_task_state.prompt_issue
    operator_issue = runtime_task_state.operator_issue
    if failure_class is None and active_attempt is not None and not worktree_exists:
        failure_class = FAILURE_CLASS_MISSING_WORKTREE
        recovery_action = "restore_or_cleanup_worktree"
        operator_issue = True
    elif failure_class is None and reference_issue:
        failure_class = FAILURE_CLASS_STALE_BRANCH
        recovery_action = "restore_ref_or_cancel_task"
        operator_issue = True
    recommended_actions, recommended_commands = _legacy_recovery_guidance(
        branch=branch,
        target_branch=target_branch,
        branch_exists=branch_exists,
        target_branch_exists=target_branch_exists,
        branch_ahead_error=branch_ahead_error,
        landed_cleanup_complete=landed_cleanup_complete,
        stale_claim=stale_claim,
        active_attempt=active_attempt is not None,
        selected_attempt=selected_attempt is not None,
        task_worktree=task_worktree,
        worktree_exists=worktree_exists,
        worktree_dirty_paths=worktree_dirty_paths,
        branch_ahead=branch_ahead,
        primary_dirty=primary_dirty,
        reference_issue=reference_issue,
    )
    transaction_outcome = (
        landing_transaction.outcome if landing_transaction is not None else None
    )
    reconciliation_candidate = False
    landing_reconcile_argv: tuple[str, ...] = ()
    reconciliation_action_id: str | None = None
    reconciliation_reason_code: str | None = None
    reconciliation_reason_detail: str | None = None
    if (
        landing_transaction is not None
        and landing_transaction.outcome == "abort_complete"
        and landing_transaction.abort_data is not None
    ):
        abort_candidate = landing_transaction.abort_data.get("landed_commit")
        if (
            isinstance(abort_candidate, str)
            and latest_attempt is not None
            and latest_attempt.status == ATTEMPT_STATUS_SUCCESS
            and str(latest_attempt.landed_commit or "").lower()
            == abort_candidate.lower()
        ):
            transaction_outcome = "abort_reconciled"
        elif isinstance(abort_candidate, str):
            try:
                _current_target, contains_abort_candidate = _landing_abort_target_state(
                    intent=landing_transaction.intent,
                    landed_commit=abort_candidate,
                )
            except WorktreeError:
                contains_abort_candidate = False
            if contains_abort_candidate:
                reconciliation_candidate = True
                reconciliation_action_id = "verify_late_landing_reconciliation"
                reconciliation_reason_code = "abort_complete_target_contains_candidate"
                reconciliation_reason_detail = (
                    "The abort is terminal, but its exact canonical candidate is now reachable "
                    "from target; verify reconciliation proof."
                )
                executable = _lifecycle_blackdog_executable(
                    profile,
                    {"worktree_path": landing_transaction.intent.worktree_path},
                )
                landing_reconcile_argv = (
                    executable,
                    "task",
                    "reconcile-landing",
                    f"--project-root={landing_transaction.intent.primary_worktree}",
                    f"--workset={workset_id}",
                    f"--task={task_id}",
                    f"--attempt={landing_transaction.intent.attempt_id}",
                    f"--landed-commit={abort_candidate}",
                    f"--actor={landing_transaction.intent.actor}",
                    "--reason=Late target containment after durable landing abort",
                )
    legacy_reconciliation_detection: dict[str, Any] | None = None
    if include_reconciliation_detection:
        (
            legacy_reconciliation_detection,
            legacy_reconcile_argv,
        ) = _detect_legacy_landing_reconciliation(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            active_attempt=active_attempt,
            latest_attempt=latest_attempt,
            current_task_claim=current_task_claim,
            landing_transaction=landing_transaction,
        )
        if (
            not reconciliation_candidate
            and legacy_reconciliation_detection["state"] == "ready"
        ):
            reconciliation_candidate = True
            landing_reconcile_argv = legacy_reconcile_argv
            reconciliation_action_id = "verify_legacy_landing_reconciliation"
            reconciliation_reason_code = "canonical_legacy_landing_detected"
            reconciliation_reason_detail = str(
                legacy_reconciliation_detection["reason_detail"]
            )
    payload = {
        "workset_id": workset_id,
        "task_id": task_id,
        "task_title": task.title,
        "task_runtime_status": runtime_task_state.status,
        "task_runtime_note": runtime_task_state.note,
        "task_runtime_updated_at": runtime_task_state.updated_at,
        "active_attempt": active_attempt is not None,
        "attempt_id": selected_attempt.attempt_id if selected_attempt is not None else None,
        "latest_attempt_id": latest_attempt.attempt_id if latest_attempt is not None else None,
        "latest_attempt_status": latest_attempt.status if latest_attempt is not None else None,
        "latest_attempt_summary": latest_attempt.summary if latest_attempt is not None else None,
        "landed_commit": selected_attempt.landed_commit if selected_attempt is not None else None,
        "actor": (
            active_attempt.actor
            if active_attempt is not None and str(active_attempt.actor or "").strip()
            else _latest_task_event_actor(
                events,
                workset_id=workset_id,
                task_id=task_id,
            )
            or (
                selected_attempt.actor
                if selected_attempt is not None and str(selected_attempt.actor or "").strip()
                else None
            )
        ),
        "branch": branch,
        "target_branch": target_branch,
        "worktree_path": str(task_worktree) if task_worktree is not None else recorded_worktree_path,
        "worktree_exists": worktree_exists,
        "worktree_dirty": bool(worktree_dirty_paths),
        "worktree_dirty_paths": worktree_dirty_paths,
        "source_git_operation": source_git_operation,
        "source_git_operation_detail": source_git_operation_detail,
        "branch_ahead_of_target": branch_ahead,
        "branch_exists": branch_exists,
        "target_branch_exists": target_branch_exists,
        "branch_ahead_error": branch_ahead_error,
        "branch_reference": branch_inspection.to_dict(),
        "target_branch_reference": target_inspection.to_dict(),
        "reference_issue": reference_issue,
        "reference_issue_code": reference_issue_code,
        "reference_issue_detail": reference_issue_detail,
        "changed_paths": _attempt_changed_paths(
            profile,
            branch=branch,
            target_branch=target_branch,
            worktree_path=task_worktree,
            primary_root=primary_root,
            changed_paths_cache=changed_paths_cache,
        ),
        "task_claim": (
            {
                "actor": current_task_claim.actor,
                "execution_model": current_task_claim.execution_model,
                "claimed_at": current_task_claim.claimed_at,
                "attempt_id": current_task_claim.attempt_id,
                "note": current_task_claim.note,
            }
            if current_task_claim is not None
            else None
        ),
        "workset_claim": (
            {
                "actor": current_workset_claim.actor,
                "execution_model": current_workset_claim.execution_model,
                "claimed_at": current_workset_claim.claimed_at,
                "note": current_workset_claim.note,
            }
            if current_workset_claim is not None
            else None
        ),
        "stale_claim": stale_claim,
        "recovery_state": recovery_state,
        "failure_class": failure_class,
        "recovery_action": recovery_action,
        "prompt_issue": prompt_issue,
        "operator_issue": operator_issue,
        "setup_receipt": selected_attempt.setup_receipt if selected_attempt is not None else None,
        "execution_prompt_hash": (
            selected_attempt.prompt_receipt.prompt_hash
            if selected_attempt is not None and selected_attempt.prompt_receipt is not None
            else None
        ),
        "execution_prompt_source": (
            selected_attempt.prompt_receipt.source
            if selected_attempt is not None and selected_attempt.prompt_receipt is not None
            else None
        ),
        "execution_prompt_mode": (
            selected_attempt.prompt_receipt.mode
            if selected_attempt is not None and selected_attempt.prompt_receipt is not None
            else None
        ),
        "execution_prompt_replay_artifact_path": (
            selected_attempt.prompt_receipt.replay_artifact_path
            if selected_attempt is not None and selected_attempt.prompt_receipt is not None
            else None
        ),
        "user_prompt_hash": (
            selected_attempt.user_prompt_receipt.prompt_hash
            if selected_attempt is not None and selected_attempt.user_prompt_receipt is not None
            else None
        ),
        "user_prompt_source": (
            selected_attempt.user_prompt_receipt.source
            if selected_attempt is not None and selected_attempt.user_prompt_receipt is not None
            else None
        ),
        "user_prompt_mode": (
            selected_attempt.user_prompt_receipt.mode
            if selected_attempt is not None and selected_attempt.user_prompt_receipt is not None
            else None
        ),
        "user_prompt_replay_artifact_path": (
            selected_attempt.user_prompt_receipt.replay_artifact_path
            if selected_attempt is not None and selected_attempt.user_prompt_receipt is not None
            else None
        ),
        "prompt_hash": (
            selected_attempt.prompt_receipt.prompt_hash
            if selected_attempt is not None and selected_attempt.prompt_receipt is not None
            else None
        ),
        "prompt_source": (
            selected_attempt.prompt_receipt.source
            if selected_attempt is not None and selected_attempt.prompt_receipt is not None
            else None
        ),
        "started_at": selected_attempt.started_at if selected_attempt is not None else None,
        "ended_at": selected_attempt.ended_at if selected_attempt is not None else None,
        "primary_worktree": str(primary_root),
        "primary_dirty": primary_dirty,
        "primary_dirty_paths": primary_dirty_paths,
        "recommended_actions": recommended_actions,
        "recommended_commands": recommended_commands,
        "landed_cleanup_complete": landed_cleanup_complete,
        "terminal_cleanup_complete": terminal_cleanup_complete,
        "cleanup_evidence": cleanup_evidence,
        "reconciliation_candidate": reconciliation_candidate,
        "landing_reconcile_argv": list(landing_reconcile_argv),
        "reconciliation_action_id": reconciliation_action_id,
        "reconciliation_reason_code": reconciliation_reason_code,
        "reconciliation_reason_detail": reconciliation_reason_detail,
        "landing_transaction": (
            landing_transaction.to_dict() if landing_transaction is not None else None
        ),
        "landing_transaction_incomplete": bool(
            landing_transaction is not None and not landing_transaction.terminal
        ),
        "landing_transaction_outcome": (
            transaction_outcome
        ),
        "landing_last_phase": (
            landing_transaction.last_phase if landing_transaction is not None else None
        ),
        "landing_resume_argv": (
            list(landing_resume_action.argv) if landing_resume_action is not None else []
        ),
    }
    if legacy_reconciliation_detection is not None:
        payload["legacy_reconciliation_detection"] = legacy_reconciliation_detection
    skill_provenance = _bounded_skill_provenance(
        selected_attempt.setup_receipt if selected_attempt is not None else None
    )
    if skill_provenance is not None:
        payload["skill_provenance"] = skill_provenance
    payload["resume_lineage"] = _resume_lineage(profile, payload)
    payload.update(
        _ordinary_resume_recovery_fields(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
            active_attempt=active_attempt,
        )
    )
    payload.update(
        _workspace_adoption_recovery_fields(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
            task_status=runtime_task_state.status,
            active_attempt=active_attempt,
            latest_attempt=latest_attempt,
            landing_transaction=landing_transaction,
            resume_lineage=payload["resume_lineage"],
            predecessor_reconciliation_candidate=reconciliation_candidate,
        )
    )
    pending_transition, pending_action = _pending_task_runtime_transition_action(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    pending_stale_reservation, pending_stale_action = (
        _pending_stale_claim_release_for_workset_action(
            profile,
            workset_id=workset_id,
        )
    )
    pending_close, pending_close_action = _pending_close_action(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    pending_stale_release = (
        pending_stale_reservation["release"]
        if pending_stale_reservation is not None
        else None
    )
    pending_stale_owner = (
        pending_stale_reservation.get("owner_task_id")
        if pending_stale_reservation is not None
        else None
    )
    payload["runtime_transition_pending"] = pending_transition is not None
    payload["runtime_transition"] = pending_transition
    payload["stale_claim_release_pending"] = (
        pending_stale_reservation is not None
    )
    payload["stale_claim_release"] = pending_stale_release
    payload["stale_claim_release_owner_task_id"] = pending_stale_owner
    payload["target_stale_claim_release_pending"] = (
        pending_stale_owner == task_id
    )
    payload["close_transaction_pending"] = pending_close is not None
    correction_selection = (
        load_landing_correction_selection(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=selected_attempt.attempt_id,
        )
        if selected_attempt is not None
        else None
    )
    payload["landing_correction"] = (
        correction_selection.to_dict()
        if correction_selection is not None
        and correction_selection.corrections
        else None
    )
    correction_action_state: str | None = None
    correction_resume_argv: tuple[str, ...] = ()
    correction_action_receipt = None
    if correction_selection is not None:
        if correction_selection.active is not None:
            correction_action_receipt = correction_selection.active
            correction_action_state = "active"
            correction_resume_argv = correction_action_receipt.intent.resume_argv
        elif (
            correction_selection.latest_terminal is not None
            and correction_selection.latest_terminal.blocked
        ):
            correction_action_receipt = correction_selection.latest_terminal
            correction_reason = str(
                correction_action_receipt.phase_data["blocked"]["reason_code"]
            )
            correction_action_state = {
                "automatic_rebase_conflict": "conflict",
                "post_rebase_validation_failed": "validation_failed",
                "automatic_rebase_safety_unproven": "unsafe",
                "automatic_stale_recovery_retry_exhausted": "retry_exhausted",
            }.get(correction_reason, "unsafe")
    payload["landing_correction_state"] = correction_action_state
    payload["landing_correction_resume_argv"] = list(correction_resume_argv)
    payload["landing_correction_worktree_path"] = (
        correction_action_receipt.intent.worktree_path
        if correction_action_receipt is not None
        else None
    )
    payload["landing_correction_branch"] = (
        correction_action_receipt.intent.branch
        if correction_action_receipt is not None
        else None
    )
    payload["landing_correction_target_branch"] = (
        correction_action_receipt.intent.target_branch
        if correction_action_receipt is not None
        else None
    )
    if pending_close is None:
        payload["close_transaction"] = None
    elif pending_close.get("stage") == "conflict":
        payload["close_transaction"] = dict(pending_close)
    else:
        close_request = pending_close["request"]
        payload["close_transaction"] = {
            "stage": pending_close["stage"],
            "complete": False,
            "close_request_id": close_request.request_event_id,
            "finalization_id": close_request.finalization_id,
            "close_event_id": close_request.close_event_id,
            "cleanup_event_id": close_request.cleanup_event_id,
            "attempt_id": close_request.attempt_id,
            "core": _close_core_evidence_payload(pending_close["core"]),
            "cleanup_event_recorded": pending_close["cleanup_event"] is not None,
        }
    if pending_close is not None and pending_close_action is not None:
        next_action = pending_close_action
        payload["next_action"] = next_action.to_dict()
        payload["recommended_commands"] = next_action.legacy_command_rows()
        payload["recommended_actions"] = [next_action.display]
    elif pending_stale_reservation is not None and pending_stale_action is not None:
        next_action = pending_stale_action
        payload["next_action"] = next_action.to_dict()
        payload["recommended_commands"] = next_action.legacy_command_rows()
        payload["recommended_actions"] = [next_action.display]
    elif pending_transition is not None and pending_action is not None:
        next_action = pending_action
        payload["next_action"] = next_action.to_dict()
        payload["recommended_commands"] = next_action.legacy_command_rows()
        payload["recommended_actions"] = [next_action.display]
    else:
        next_action = _attach_next_action(profile, payload)
    if (
        legacy_reconciliation_detection is not None
        and legacy_reconciliation_detection["state"] == "ready"
        and next_action.action_id == "verify_legacy_landing_reconciliation"
    ):
        payload["recommended_commands"] = next_action.legacy_command_rows()
        payload["recommended_actions"] = [next_action.display]
    return payload


def _release_stale_task_claim(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    status: str,
    summary: str,
    note: str | None = None,
    expected_request_event_id: str | None = None,
    expected_decision_event_id: str | None = None,
) -> tuple[dict[str, Any], StaleClaimReleaseResult]:
    result = release_stale_task_claim(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        status=status,
        summary=summary,
        note=note,
        expected_request_event_id=expected_request_event_id,
        expected_decision_event_id=expected_decision_event_id,
    )
    payload = _task_recovery_payload(profile, workset_id=workset_id, task_id=task_id)
    payload["released_stale_claim"] = True
    payload["stale_claim_release_runtime_finalized"] = True
    payload["stale_claim_release_event_finalized"] = True
    payload["stale_claim_release_finalization_pending"] = False
    payload["released_attempt_id"] = result.stale_claim.attempt_id
    payload["release_status"] = result.status
    payload["release_summary"] = result.summary
    payload["release_note"] = result.note
    payload["released_workset_claim"] = result.release_workset_claim
    payload["repaired_runtime_status"] = result.repaired_runtime_status
    payload["stale_claim_release_request_event_id"] = result.request_event_id
    payload["stale_claim_release_decision_event_id"] = result.decision_event_id
    payload["stale_claim_release_task_event_id"] = result.task_release_event_id
    payload["stale_claim_release_workset_event_id"] = result.workset_release_event_id
    payload["stale_claim_release_runtime_changed"] = result.runtime_changed
    payload["stale_claim_release_request_event_appended"] = (
        result.request_event_appended
    )
    payload["stale_claim_release_decision_event_appended"] = (
        result.decision_event_appended
    )
    payload["stale_claim_release_task_event_appended"] = (
        result.task_release_event_appended
    )
    payload["stale_claim_release_workset_event_appended"] = (
        result.workset_release_event_appended
    )
    payload.update(
        {
            "failure_class": result.failure_class,
            "recovery_action": result.recovery_action,
            "prompt_issue": result.prompt_issue,
            "operator_issue": result.operator_issue,
        }
    )
    return payload, result


def recover_task(
    profile: RepoProfile,
    *,
    workset_id: str | None = None,
    task_id: str | None = None,
    release_stale_claim: bool = False,
    status: str | None = None,
    summary: str | None = None,
    note: str | None = None,
    stale_claim_release_request_event_id: str | None = None,
    stale_claim_release_decision_event_id: str | None = None,
    cwd: Path | None = None,
) -> OperationResult:
    if not release_stale_claim and any(
        item is not None
        for item in (
            status,
            summary,
            note,
            stale_claim_release_request_event_id,
            stale_claim_release_decision_event_id,
        )
    ):
        raise BacklogError(
            "task recover only accepts stale-claim release inputs with --release-stale-claim"
        )
    if (
        stale_claim_release_decision_event_id is not None
        and stale_claim_release_request_event_id is None
    ):
        raise BacklogError(
            "stale-claim release decision guard requires its request guard"
        )
    resolved_workset, resolved_task, _attempt = _resolve_task_command_target(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        cwd=cwd,
        allow_latest=True,
    )
    pending_transition, pending_action = _pending_task_runtime_transition_action(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
    )
    pending_close, pending_close_action = _pending_close_action(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
    )
    if pending_close is not None and release_stale_claim:
        return _recoverable_task_blocked_result(
            profile,
            operation="task.recover",
            workset_id=resolved_workset,
            task_id=resolved_task,
        )
    if pending_transition is not None and release_stale_claim:
        return _recoverable_task_blocked_result(
            profile,
            operation="task.recover",
            workset_id=resolved_workset,
            task_id=resolved_task,
        )
    pending_stale_reservation, pending_stale_action = (
        _pending_stale_claim_release_for_workset_action(
            profile,
            workset_id=resolved_workset,
        )
    )
    pending_stale_owner = (
        pending_stale_reservation.get("owner_task_id")
        if pending_stale_reservation is not None
        else None
    )
    if (
        release_stale_claim
        and pending_stale_reservation is not None
        and pending_stale_owner != resolved_task
    ):
        return _recoverable_task_blocked_result(
            profile,
            operation="task.recover",
            workset_id=resolved_workset,
            task_id=resolved_task,
        )
    if not release_stale_claim:
        payload = _task_recovery_payload(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            include_reconciliation_detection=True,
        )
        payload["released_stale_claim"] = False
        next_action = (
            pending_close_action
            if pending_close_action is not None
            else pending_stale_action
            if pending_stale_action is not None
            else pending_action
            if pending_action is not None
            else decide_next_action(_lifecycle_context(profile, payload))
        )
        payload["recommended_actions"] = _task_surface_actions(
            list(payload["recommended_actions"])
        )
        return observe_operation_result(profile, OperationResult(
            operation="task.recover",
            operation_status="observed",
            task_status=payload.get("task_runtime_status"),
            attempt_status=payload.get("latest_attempt_status"),
            disposition=next_action.disposition,
            mutation_started=False,
            mutation_completed=False,
            mutation_phase="none",
            failure_code=payload.get("failure_class"),
            next_action=next_action,
            legacy_payload=payload,
        ))

    resolved_status = str(status or "").strip()
    resolved_summary = str(summary or "").strip()
    resolved_note = str(note or "").strip() or None
    try:
        payload, release_result = _release_stale_task_claim(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            status=resolved_status,
            summary=resolved_summary,
            note=resolved_note,
            expected_request_event_id=stale_claim_release_request_event_id,
            expected_decision_event_id=stale_claim_release_decision_event_id,
        )
    except StaleClaimReleaseConflictError as exc:
        return _task_mutation_result(
            profile,
            operation="task.recover",
            workset_id=resolved_workset,
            task_id=resolved_task,
            legacy_payload={
                "workset_id": resolved_workset,
                "task_id": resolved_task,
                "release_status": resolved_status,
                "release_summary": resolved_summary,
                "release_note": resolved_note,
                "stale_claim_release_request_event_id": (
                    stale_claim_release_request_event_id
                ),
                "stale_claim_release_decision_event_id": (
                    stale_claim_release_decision_event_id
                ),
                "error": str(exc),
            },
            mutation_phase="none",
            operation_status="blocked",
            mutation_started=False,
            mutation_completed=False,
            next_action_override=_stale_claim_release_conflict_action(str(exc)),
        )
    except StaleClaimReleaseFinalizationError as exc:
        durable_decision_id = _durable_task_runtime_transition_event_id(
            profile,
            exc.decision_event_id,
        )
        retry_action = _stale_claim_release_retry_action(
            profile,
            workset_id=resolved_workset,
            task_id=resolved_task,
            status=resolved_status,
            summary=resolved_summary,
            note=resolved_note,
            request_event_id=str(exc.request_event_id),
            decision_event_id=durable_decision_id,
        )
        runtime_finalized = exc.mutation_phase in {
            "runtime_finalized",
            "event_finalization_partial",
            "event_finalized",
        }
        event_finalized = exc.mutation_phase == "event_finalized"
        return _task_mutation_result(
            profile,
            operation="task.recover",
            workset_id=resolved_workset,
            task_id=resolved_task,
            legacy_payload={
                "workset_id": resolved_workset,
                "task_id": resolved_task,
                "released_stale_claim": runtime_finalized,
                "stale_claim_release_runtime_finalized": runtime_finalized,
                "stale_claim_release_event_finalized": event_finalized,
                "stale_claim_release_finalization_pending": not event_finalized,
                "release_status": resolved_status,
                "release_summary": resolved_summary,
                "release_note": resolved_note,
                "stale_claim_release_request_event_id": exc.request_event_id,
                "stale_claim_release_decision_event_id": exc.decision_event_id,
                "stale_claim_release_task_event_id": exc.task_release_event_id,
                "stale_claim_release_workset_event_id": (
                    exc.workset_release_event_id
                ),
                "error": str(exc),
            },
            mutation_phase=exc.mutation_phase,
            operation_status="partial",
            mutation_started=exc.mutation_started,
            mutation_completed=exc.mutation_phase == "event_finalized",
            next_action_override=NextAction.command(retry_action),
        )
    else:
        runtime_changed = release_result.runtime_changed
        event_changed = (
            release_result.task_release_event_appended
            or release_result.workset_release_event_appended
        )
        ledger_changed = (
            release_result.request_event_appended
            or release_result.decision_event_appended
        )
        mutation_started = runtime_changed or event_changed or ledger_changed
        mutation_phase = (
            "runtime_and_event_finalized"
            if runtime_changed and event_changed
            else "runtime_finalized"
            if runtime_changed
            else "event_finalized"
            if event_changed or ledger_changed
            else "none"
        )
    payload["recommended_actions"] = _task_surface_actions(
        list(payload["recommended_actions"])
    )
    next_action = decide_next_action(_lifecycle_context(profile, payload))
    return observe_operation_result(profile, OperationResult(
        operation="task.recover",
        operation_status="succeeded",
        task_status=payload.get("task_runtime_status"),
        attempt_status=payload.get("latest_attempt_status"),
        disposition=next_action.disposition,
        mutation_started=mutation_started,
        mutation_completed=mutation_started,
        mutation_phase=mutation_phase,
        failure_code=payload.get("failure_class"),
        next_action=next_action,
        legacy_payload=payload,
    ))


def _landing_operation_phase(transaction: LandingTransaction | None) -> str:
    if transaction is None:
        return "preflight"
    if transaction.abort_complete:
        return "landing_abort_complete"
    if transaction.abort_close_event_recorded:
        return "landing_abort_close_event_recorded"
    if transaction.abort_runtime_finalized:
        return "landing_abort_runtime_finalized"
    if transaction.abort_superseded and not transaction.target_updated:
        return "landing_abort_superseded"
    if transaction.abort_cleanup_complete and transaction.aborted:
        return "landing_abort_temporary_cleanup_complete"
    if transaction.abort_requested and transaction.aborted:
        return "landing_abort_intent_recorded"
    return f"landing_{transaction.last_phase}"


def _in_progress_git_operation(worktree_path: Path) -> str | None:
    unmerged = _run_git_bytes(worktree_path, "ls-files", "--unmerged", "-z")
    if unmerged:
        return "unmerged-index"
    for marker in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "REBASE_HEAD",
        "rebase-apply",
        "rebase-merge",
        "sequencer",
    ):
        marker_text = _run_git(worktree_path, "rev-parse", "--git-path", marker)
        marker_path = Path(marker_text)
        if not marker_path.is_absolute():
            marker_path = worktree_path / marker_path
        if marker_path.exists():
            return marker
    return None


def _require_no_in_progress_source_operation(worktree_path: Path) -> None:
    operation = _in_progress_git_operation(worktree_path)
    if operation == "unmerged-index":
        raise WorktreeError(
            "cannot record landing intent while the source index has unresolved entries"
        )
    if operation is not None:
        raise WorktreeError(
            f"cannot record landing intent during an in-progress Git operation ({operation})"
        )


def _rebase_metadata_present(worktree_path: Path) -> bool:
    for marker in ("REBASE_HEAD", "rebase-apply", "rebase-merge"):
        marker_text = _run_git(worktree_path, "rev-parse", "--git-path", marker)
        marker_path = Path(marker_text)
        if not marker_path.is_absolute():
            marker_path = worktree_path / marker_path
        if marker_path.exists():
            return True
    return False


def _stash_identity(worktree_path: Path) -> tuple[str, ...]:
    completed = _run_git_no_check(
        worktree_path,
        "stash",
        "list",
        "--format=%H%x00%gd",
    )
    if completed.returncode != 0:
        raise WorktreeError("could not inspect the task worktree stash identity")
    return tuple(completed.stdout.splitlines())


def _automatic_stale_recovery_payload(
    *,
    state: str,
    correction_id: str | None,
    attempt_count: int,
    worktree_path: Path,
    branch: str,
    target_branch: str,
    original_source_head: str,
    rebased_source_head: str | None,
    target_commit: str,
    rebase_started: bool,
    rebase_aborted: bool,
    validation: ValidationRunResult | None = None,
    validated_tree_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "state": state,
        "correction_id": correction_id,
        "attempt_count": attempt_count,
        "max_attempts": AUTOMATIC_STALE_REBASE_MAX_ATTEMPTS,
        "worktree_path": str(worktree_path),
        "branch": branch,
        "target_branch": target_branch,
        "original_source_head": original_source_head,
        "rebased_source_head": rebased_source_head,
        "target_commit": target_commit,
        "validated_tree_hash": validated_tree_hash,
        "rebase_started": rebase_started,
        "rebase_aborted": rebase_aborted,
        "worktree_preserved": True,
        "target_updated_by_blackdog": False,
        "landing_agent_handoff_required": state != "completed",
        "validation": validation.to_dict() if validation is not None else None,
    }


def _run_automatic_stale_rebase(
    profile: RepoProfile,
    *,
    attempt: Any,
    exc: StaleTaskBranchError,
    correction_id: str | None,
) -> tuple[Path, str, str, dict[str, Any]]:
    primary_root = find_primary_worktree(profile.paths.project_root)
    worktree_path = Path(str(exc.branch_worktree or attempt.worktree_path or "")).resolve(
        strict=False
    )
    if (
        not worktree_path.exists()
        or not _is_git_worktree_path(worktree_path)
        or _registered_worktree_row(primary_root, worktree_path) is None
    ):
        evidence = _automatic_stale_recovery_payload(
            state="unsafe",
            correction_id=correction_id,
            attempt_count=0,
            worktree_path=worktree_path,
            branch=str(attempt.branch),
            target_branch=str(attempt.target_branch),
            original_source_head="unknown",
            rebased_source_head=None,
            target_commit="unknown",
            rebase_started=False,
            rebase_aborted=False,
        )
        raise AutomaticStaleRecoveryError(
            state="unsafe",
            detail="automatic stale recovery could not prove the exact task worktree registration",
            evidence=evidence,
        )
    operation = _in_progress_git_operation(worktree_path)
    if operation is not None:
        source_head = _run_git(worktree_path, "rev-parse", "HEAD")
        target_commit = _run_git(primary_root, "rev-parse", str(attempt.target_branch))
        evidence = _automatic_stale_recovery_payload(
            state="unsafe",
            correction_id=correction_id,
            attempt_count=0,
            worktree_path=worktree_path,
            branch=str(attempt.branch),
            target_branch=str(attempt.target_branch),
            original_source_head=source_head,
            rebased_source_head=None,
            target_commit=target_commit,
            rebase_started=False,
            rebase_aborted=False,
        )
        raise AutomaticStaleRecoveryError(
            state="unsafe",
            detail=(
                "automatic stale recovery found a pre-existing Git operation "
                f"({operation}); the landing agent must inspect it"
            ),
            evidence=evidence,
        )
    branch_head = _run_git(primary_root, "rev-parse", f"refs/heads/{attempt.branch}")
    if _run_git(worktree_path, "rev-parse", "HEAD") != branch_head:
        raise WorktreeError("automatic stale recovery found incoherent task branch HEAD")
    target_commit = _run_git(
        primary_root,
        "rev-parse",
        f"refs/heads/{attempt.target_branch}",
    )
    _manifest, original_tree_hash = _projected_source_tree_manifest(worktree_path)
    original_stash = _stash_identity(worktree_path)
    action = _stale_branch_rebase_action(exc)
    completed = subprocess.run(
        list(action.argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    operation_after = _in_progress_git_operation(worktree_path)
    if completed.returncode != 0 or operation_after is not None:
        rebase_aborted = False
        restoration_proven = False
        if _rebase_metadata_present(worktree_path):
            aborted = _run_git_no_check(worktree_path, "rebase", "--abort")
            rebase_aborted = aborted.returncode == 0
            if rebase_aborted and _in_progress_git_operation(worktree_path) is None:
                _restored_manifest, restored_tree_hash = _projected_source_tree_manifest(
                    worktree_path
                )
                restoration_proven = (
                    _run_git(worktree_path, "rev-parse", "HEAD") == branch_head
                    and restored_tree_hash == original_tree_hash
                    and _stash_identity(worktree_path) == original_stash
                )
        state = "conflict" if restoration_proven else "unsafe"
        current_head = _run_git(worktree_path, "rev-parse", "HEAD")
        evidence = _automatic_stale_recovery_payload(
            state=state,
            correction_id=correction_id,
            attempt_count=1,
            worktree_path=worktree_path,
            branch=str(attempt.branch),
            target_branch=str(attempt.target_branch),
            original_source_head=branch_head,
            rebased_source_head=current_head if current_head != branch_head else None,
            target_commit=target_commit,
            rebase_started=True,
            rebase_aborted=rebase_aborted,
        )
        detail = (
            "automatic stale recovery encountered a content conflict and restored "
            "the original task workspace"
            if restoration_proven
            else "automatic stale recovery could not prove exact restoration; the "
            "task workspace and any Git/autostash state were preserved"
        )
        raise AutomaticStaleRecoveryError(
            state=state,
            detail=detail,
            evidence=evidence,
        )
    if _in_progress_git_operation(worktree_path) is not None:
        raise WorktreeError("automatic stale recovery left an in-progress Git operation")
    rebased_head = _run_git(worktree_path, "rev-parse", "HEAD")
    current_target_commit = _run_git(
        primary_root,
        "rev-parse",
        f"refs/heads/{attempt.target_branch}",
    )
    if current_target_commit != target_commit:
        evidence = _automatic_stale_recovery_payload(
            state="retry_exhausted",
            correction_id=correction_id,
            attempt_count=AUTOMATIC_STALE_REBASE_MAX_ATTEMPTS,
            worktree_path=worktree_path,
            branch=str(attempt.branch),
            target_branch=str(attempt.target_branch),
            original_source_head=branch_head,
            rebased_source_head=rebased_head,
            target_commit=current_target_commit,
            rebase_started=True,
            rebase_aborted=False,
        )
        raise AutomaticStaleRecoveryError(
            state="retry_exhausted",
            detail=(
                "the landing target moved while the one allowed automatic rebase "
                "was running"
            ),
            evidence=evidence,
        )
    if (
        _run_git(primary_root, "rev-parse", f"refs/heads/{attempt.branch}")
        != rebased_head
        or _stash_identity(worktree_path) != original_stash
    ):
        evidence = _automatic_stale_recovery_payload(
            state="unsafe",
            correction_id=correction_id,
            attempt_count=1,
            worktree_path=worktree_path,
            branch=str(attempt.branch),
            target_branch=str(attempt.target_branch),
            original_source_head=branch_head,
            rebased_source_head=rebased_head,
            target_commit=target_commit,
            rebase_started=True,
            rebase_aborted=False,
        )
        raise AutomaticStaleRecoveryError(
            state="unsafe",
            detail="automatic stale recovery could not prove branch or stash coherence",
            evidence=evidence,
        )
    ancestor = _run_git_no_check(
        primary_root,
        "merge-base",
        "--is-ancestor",
        target_commit,
        rebased_head,
    )
    if ancestor.returncode != 0:
        evidence = _automatic_stale_recovery_payload(
            state="unsafe",
            correction_id=correction_id,
            attempt_count=1,
            worktree_path=worktree_path,
            branch=str(attempt.branch),
            target_branch=str(attempt.target_branch),
            original_source_head=branch_head,
            rebased_source_head=rebased_head,
            target_commit=target_commit,
            rebase_started=True,
            rebase_aborted=False,
        )
        raise AutomaticStaleRecoveryError(
            state="unsafe",
            detail="automatic stale recovery did not place the task on its recorded target",
            evidence=evidence,
        )
    evidence = _automatic_stale_recovery_payload(
        state="rebased",
        correction_id=correction_id,
        attempt_count=1,
        worktree_path=worktree_path,
        branch=str(attempt.branch),
        target_branch=str(attempt.target_branch),
        original_source_head=branch_head,
        rebased_source_head=rebased_head,
        target_commit=target_commit,
        rebase_started=True,
        rebase_aborted=False,
    )
    return worktree_path, rebased_head, target_commit, evidence


def _run_post_rebase_validation(
    profile: RepoProfile,
    *,
    attempt: Any,
    worktree_path: Path,
    original_source_head: str,
    rebased_source_head: str,
    target_commit: str,
    correction_id: str | None,
    attempt_count: int,
    rebase_started: bool,
) -> tuple[ValidationRunResult, str, dict[str, Any]]:
    _before_manifest, before_tree_hash = _projected_source_tree_manifest(worktree_path)
    validation = run_validation_commands(
        profile.validation_commands,
        cwd=worktree_path,
        timeout_seconds=profile.landing.validation_timeout_seconds,
    )
    _after_manifest, after_tree_hash = _projected_source_tree_manifest(worktree_path)
    if not validation.all_passed or after_tree_hash != before_tree_hash:
        evidence = _automatic_stale_recovery_payload(
            state="validation_failed",
            correction_id=correction_id,
            attempt_count=attempt_count,
            worktree_path=worktree_path,
            branch=str(attempt.branch),
            target_branch=str(attempt.target_branch),
            original_source_head=original_source_head,
            rebased_source_head=rebased_source_head,
            target_commit=target_commit,
            rebase_started=rebase_started,
            rebase_aborted=False,
            validation=validation,
            validated_tree_hash=(
                after_tree_hash if after_tree_hash == before_tree_hash else None
            ),
        )
        reason = (
            "configured validation changed managed task content"
            if after_tree_hash != before_tree_hash
            else "configured validation did not pass on the rebased task tree"
        )
        raise AutomaticStaleRecoveryError(
            state="validation_failed",
            detail=reason,
            evidence=evidence,
        )
    evidence = _automatic_stale_recovery_payload(
        state="completed",
        correction_id=correction_id,
        attempt_count=attempt_count,
        worktree_path=worktree_path,
        branch=str(attempt.branch),
        target_branch=str(attempt.target_branch),
        original_source_head=original_source_head,
        rebased_source_head=rebased_source_head,
        target_commit=target_commit,
        rebase_started=rebase_started,
        rebase_aborted=False,
        validation=validation,
        validated_tree_hash=after_tree_hash,
    )
    return validation, after_tree_hash, evidence


def _landing_correction_evidence_hash(value: Any) -> str:
    rendered = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _landing_correction_intent(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt: Any,
    request: Mapping[str, Any],
    generation: int,
) -> LandingCorrectionIntent:
    primary_root = find_primary_worktree(profile.paths.project_root)
    worktree_path = _resolve_attempt_worktree(
        profile,
        branch=str(attempt.branch or ""),
        worktree_path=attempt.worktree_path,
    )
    if (
        worktree_path is None
        or not worktree_path.exists()
        or not _is_git_worktree_path(worktree_path)
    ):
        raise MissingTaskWorktreeError(worktree_path or attempt.worktree_path)
    registration = _registered_worktree_row(primary_root, worktree_path)
    if (
        registration is None
        or registration.get("branch") != f"refs/heads/{attempt.branch}"
    ):
        raise WorktreeError(
            "automatic stale recovery could not prove the task worktree registration"
        )
    source_head = _run_git(
        primary_root,
        "rev-parse",
        f"refs/heads/{attempt.branch}",
    )
    if _run_git(worktree_path, "rev-parse", "HEAD") != source_head:
        raise WorktreeError(
            "automatic stale recovery found an incoherent task branch HEAD"
        )
    target_commit = _run_git(
        primary_root,
        "rev-parse",
        f"refs/heads/{attempt.target_branch}",
    )
    _manifest, source_tree_hash = _projected_source_tree_manifest(worktree_path)
    policy_identity = {
        "schema_version": profile.landing.schema_version,
        "validation_timeout_seconds": profile.landing.validation_timeout_seconds,
        "validation_command_hashes": [
            hashlib.sha256(command.encode("utf-8")).hexdigest()
            for command in profile.validation_commands
        ],
    }
    executable = _lifecycle_blackdog_executable(
        profile,
        {"worktree_path": str(worktree_path)},
    )
    resume_argv = [
        executable,
        "task",
        "land",
        f"--project-root={primary_root}",
        f"--workset={workset_id}",
        f"--task={task_id}",
        f"--actor={request['actor']}",
        f"--summary={request['summary']}",
    ]
    resume_argv.extend(
        f"--validation={name}={status}"
        for name, status in request["validations"]
    )
    resume_argv.extend(
        f"--residual={value}" for value in request["residuals"]
    )
    resume_argv.extend(
        f"--followup={value}" for value in request["followup_candidates"]
    )
    if request["note"] is not None:
        resume_argv.append(f"--note={request['note']}")
    if not request["cleanup"]:
        resume_argv.append("--keep-worktree")
    return LandingCorrectionIntent(
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=str(attempt.attempt_id),
        actor=str(request["actor"]),
        branch=str(attempt.branch),
        target_branch=str(attempt.target_branch),
        worktree_path=str(worktree_path.resolve(strict=False)),
        source_head_commit=source_head,
        target_commit=target_commit,
        source_tree_hash=source_tree_hash,
        request_identity_hash=_landing_correction_evidence_hash(dict(request)),
        validation_policy_hash=_landing_correction_evidence_hash(policy_identity),
        generation=generation,
        resume_argv=tuple(resume_argv),
    )


def _automatic_stale_reason_code(state: str) -> str:
    return {
        "conflict": "automatic_rebase_conflict",
        "validation_failed": "post_rebase_validation_failed",
        "unsafe": "automatic_rebase_safety_unproven",
        "retry_exhausted": "automatic_stale_recovery_retry_exhausted",
    }[state]


def _correction_source_state(
    correction: LandingCorrection,
) -> tuple[str, str, str]:
    if CORRECTION_PHASE_VALIDATION_COMPLETED in correction.phases:
        row = correction.phase_data[CORRECTION_PHASE_VALIDATION_COMPLETED]
    elif CORRECTION_PHASE_REBASE_COMPLETED in correction.phases:
        row = correction.phase_data[CORRECTION_PHASE_REBASE_COMPLETED]
    else:
        row = correction.phase_data[CORRECTION_PHASE_INTENT_RECORDED]
    return (
        str(row["source_head_commit"]),
        str(row["target_commit"]),
        str(row["source_tree_hash"]),
    )


def _raise_recorded_correction_blocker(
    correction: LandingCorrection,
    *,
    current: LandingCorrectionIntent,
) -> None:
    blocked = correction.phase_data.get("blocked")
    reason_code = str(blocked.get("reason_code") if blocked is not None else "")
    state = {
        "automatic_rebase_conflict": "conflict",
        "post_rebase_validation_failed": "validation_failed",
        "automatic_rebase_safety_unproven": "unsafe",
        "automatic_stale_recovery_retry_exhausted": "retry_exhausted",
    }.get(reason_code)
    if state is None:
        raise WorktreeError(
            "automatic stale recovery has unsupported terminal correction evidence"
        )
    source_head, recorded_target, source_tree = _correction_source_state(correction)
    if (
        current.source_head_commit != source_head
        or current.source_tree_hash != source_tree
    ):
        return
    if state not in {"conflict", "unsafe", "retry_exhausted"} and (
        current.target_commit != recorded_target
    ):
        return
    evidence = _automatic_stale_recovery_payload(
        state=state,
        correction_id=correction.correction_id,
        attempt_count=(
            1
            if CORRECTION_PHASE_REBASE_COMPLETED in correction.phases
            else 0
        ),
        worktree_path=Path(current.worktree_path),
        branch=current.branch,
        target_branch=current.target_branch,
        original_source_head=correction.intent.source_head_commit,
        rebased_source_head=(
            source_head
            if source_head != correction.intent.source_head_commit
            else None
        ),
        target_commit=current.target_commit,
        rebase_started=CORRECTION_PHASE_REBASE_COMPLETED in correction.phases,
        rebase_aborted=state == "conflict",
        validated_tree_hash=(
            source_tree
            if CORRECTION_PHASE_VALIDATION_COMPLETED in correction.phases
            else None
        ),
    )
    evidence["receipt"] = correction.to_dict()
    raise AutomaticStaleRecoveryError(
        state=state,
        detail=(
            "the retained task state still matches the durable automatic stale "
            "recovery blocker; the current landing agent must satisfy its typed "
            "required inputs before retrying"
        ),
        evidence=evidence,
    )


def _record_automatic_correction_blocker(
    profile: RepoProfile,
    *,
    correction_intent: LandingCorrectionIntent,
    exc: AutomaticStaleRecoveryError,
) -> None:
    validation_payload = exc.automatic_stale_recovery.get("validation")
    validation = None
    if validation_payload is not None:
        observed_validation = ValidationRunResult.from_dict(validation_payload)
        if exc.state == "validation_failed":
            validation = observed_validation
    record_landing_correction_blocked(
        profile,
        intent=correction_intent,
        reason_code=_automatic_stale_reason_code(exc.state),
        validation=validation,
    )
    correction = load_landing_correction(
        profile,
        workset_id=correction_intent.workset_id,
        task_id=correction_intent.task_id,
        attempt_id=correction_intent.attempt_id,
        correction_id=correction_intent.correction_id,
    )
    evidence = dict(exc.automatic_stale_recovery)
    evidence["correction_id"] = correction_intent.correction_id
    if correction is not None:
        evidence["receipt"] = correction.to_dict()
    exc.automatic_stale_recovery = evidence


def _automatic_stale_correction(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt: Any,
    request: Mapping[str, Any],
    stale_error: StaleTaskBranchError | None,
) -> tuple[dict[str, Any], dict[str, Any], LandingCorrectionIntent]:
    selection = load_landing_correction_selection(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=str(attempt.attempt_id),
    )
    generation = (
        selection.active.intent.generation
        if selection.active is not None
        else len(selection.corrections) + 1
    )
    current_intent = _landing_correction_intent(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt=attempt,
        request=request,
        generation=generation,
    )
    if (
        selection.latest_terminal is not None
        and selection.latest_terminal.blocked
        and str(
            selection.latest_terminal.phase_data["blocked"]["reason_code"]
        )
        == "automatic_stale_recovery_retry_exhausted"
    ):
        _raise_recorded_correction_blocker(
            selection.latest_terminal,
            current=current_intent,
        )

    correction = selection.active
    created_new_correction = correction is None
    if created_new_correction:
        correction_intent = current_intent
        record_landing_correction_intent(profile, intent=correction_intent)
        correction = load_landing_correction(
            profile,
            workset_id=correction_intent.workset_id,
            task_id=correction_intent.task_id,
            attempt_id=correction_intent.attempt_id,
            correction_id=correction_intent.correction_id,
        )
        if correction is None:
            raise WorktreeError(
                "automatic stale recovery intent was not durably observable"
            )
    else:
        correction_intent = correction.intent
        if (
            current_intent.actor != correction_intent.actor
            or current_intent.branch != correction_intent.branch
            or current_intent.target_branch != correction_intent.target_branch
            or current_intent.worktree_path != correction_intent.worktree_path
            or current_intent.request_identity_hash
            != correction_intent.request_identity_hash
            or current_intent.validation_policy_hash
            != correction_intent.validation_policy_hash
        ):
            evidence = _automatic_stale_recovery_payload(
                state="unsafe",
                correction_id=correction_intent.correction_id,
                attempt_count=0,
                worktree_path=Path(correction_intent.worktree_path),
                branch=correction_intent.branch,
                target_branch=correction_intent.target_branch,
                original_source_head=correction_intent.source_head_commit,
                rebased_source_head=None,
                target_commit=current_intent.target_commit,
                rebase_started=False,
                rebase_aborted=False,
            )
            exc = AutomaticStaleRecoveryError(
                state="unsafe",
                detail=(
                    "automatic stale recovery retry conflicts with its durable "
                    "request, policy, or workspace identity"
                ),
                evidence=evidence,
            )
            _record_automatic_correction_blocker(
                profile,
                correction_intent=correction_intent,
                exc=exc,
            )
            raise exc

    try:
        correction = load_landing_correction(
            profile,
            workset_id=correction_intent.workset_id,
            task_id=correction_intent.task_id,
            attempt_id=correction_intent.attempt_id,
            correction_id=correction_intent.correction_id,
        )
        assert correction is not None
        rebase_started = CORRECTION_PHASE_REBASE_COMPLETED in correction.phases
        original_source_head = correction_intent.source_head_commit
        worktree_path = Path(correction_intent.worktree_path)
        if not rebase_started:
            current_intent = _landing_correction_intent(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt=attempt,
                request=request,
                generation=correction_intent.generation,
            )
            target_is_ancestor = (
                _run_git_no_check(
                    find_primary_worktree(profile.paths.project_root),
                    "merge-base",
                    "--is-ancestor",
                    current_intent.target_commit,
                    current_intent.source_head_commit,
                ).returncode
                == 0
            )
            if (
                not created_new_correction
                and current_intent.target_commit
                != correction_intent.target_commit
            ):
                evidence = _automatic_stale_recovery_payload(
                    state="retry_exhausted",
                    correction_id=correction_intent.correction_id,
                    attempt_count=AUTOMATIC_STALE_REBASE_MAX_ATTEMPTS,
                    worktree_path=worktree_path,
                    branch=correction_intent.branch,
                    target_branch=correction_intent.target_branch,
                    original_source_head=original_source_head,
                    rebased_source_head=(
                        current_intent.source_head_commit
                        if current_intent.source_head_commit
                        != original_source_head
                        else None
                    ),
                    target_commit=current_intent.target_commit,
                    rebase_started=target_is_ancestor,
                    rebase_aborted=False,
                )
                raise AutomaticStaleRecoveryError(
                    state="retry_exhausted",
                    detail=(
                        "the target advanced after the automatic correction "
                        "intent but before its rebase evidence was recorded"
                    ),
                    evidence=evidence,
                )
            if not created_new_correction and target_is_ancestor:
                source_head = current_intent.source_head_commit
                target_commit = current_intent.target_commit
                source_tree_hash = current_intent.source_tree_hash
                recovery_evidence = _automatic_stale_recovery_payload(
                    state="rebased",
                    correction_id=correction_intent.correction_id,
                    attempt_count=1,
                    worktree_path=worktree_path,
                    branch=correction_intent.branch,
                    target_branch=correction_intent.target_branch,
                    original_source_head=original_source_head,
                    rebased_source_head=source_head,
                    target_commit=target_commit,
                    rebase_started=True,
                    rebase_aborted=False,
                )
                record_landing_correction_rebase_completed(
                    profile,
                    intent=correction_intent,
                    source_head_commit=source_head,
                    target_commit=target_commit,
                    source_tree_hash=source_tree_hash,
                )
                rebase_started = True
            elif stale_error is not None or not created_new_correction:
                rebase_error = stale_error or StaleTaskBranchError(
                    branch=correction_intent.branch,
                    target_branch=correction_intent.target_branch,
                    branch_worktree=worktree_path,
                )
                (
                    worktree_path,
                    source_head,
                    target_commit,
                    recovery_evidence,
                ) = _run_automatic_stale_rebase(
                    profile,
                    attempt=attempt,
                    exc=rebase_error,
                    correction_id=correction_intent.correction_id,
                )
                _manifest, source_tree_hash = _projected_source_tree_manifest(
                    worktree_path
                )
                record_landing_correction_rebase_completed(
                    profile,
                    intent=correction_intent,
                    source_head_commit=source_head,
                    target_commit=target_commit,
                    source_tree_hash=source_tree_hash,
                )
                rebase_started = True
            elif target_is_ancestor:
                source_head = current_intent.source_head_commit
                target_commit = current_intent.target_commit
                source_tree_hash = current_intent.source_tree_hash
                recovery_evidence = _automatic_stale_recovery_payload(
                    state="validating",
                    correction_id=correction_intent.correction_id,
                    attempt_count=0,
                    worktree_path=worktree_path,
                    branch=correction_intent.branch,
                    target_branch=correction_intent.target_branch,
                    original_source_head=original_source_head,
                    rebased_source_head=source_head,
                    target_commit=target_commit,
                    rebase_started=False,
                    rebase_aborted=False,
                )
            else:
                raise WorktreeError(
                    "automatic stale recovery could not prove the corrected source ancestry"
                )
        else:
            source_head, target_commit, source_tree_hash = _correction_source_state(
                correction
            )
            current_intent = _landing_correction_intent(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt=attempt,
                request=request,
                generation=correction_intent.generation,
            )
            if (
                current_intent.source_head_commit != source_head
                or current_intent.source_tree_hash != source_tree_hash
                or current_intent.target_commit != target_commit
            ):
                evidence = _automatic_stale_recovery_payload(
                    state="retry_exhausted",
                    correction_id=correction_intent.correction_id,
                    attempt_count=AUTOMATIC_STALE_REBASE_MAX_ATTEMPTS,
                    worktree_path=worktree_path,
                    branch=correction_intent.branch,
                    target_branch=correction_intent.target_branch,
                    original_source_head=original_source_head,
                    rebased_source_head=source_head,
                    target_commit=current_intent.target_commit,
                    rebase_started=True,
                    rebase_aborted=False,
                )
                raise AutomaticStaleRecoveryError(
                    state="retry_exhausted",
                    detail=(
                        "the target or corrected task tree changed after the one "
                        "allowed automatic stale correction"
                    ),
                    evidence=evidence,
                )
            recovery_evidence = _automatic_stale_recovery_payload(
                state="rebased",
                correction_id=correction_intent.correction_id,
                attempt_count=1,
                worktree_path=worktree_path,
                branch=correction_intent.branch,
                target_branch=correction_intent.target_branch,
                original_source_head=original_source_head,
                rebased_source_head=source_head,
                target_commit=target_commit,
                rebase_started=True,
                rebase_aborted=False,
            )

        correction = load_landing_correction(
            profile,
            workset_id=correction_intent.workset_id,
            task_id=correction_intent.task_id,
            attempt_id=correction_intent.attempt_id,
            correction_id=correction_intent.correction_id,
        )
        assert correction is not None
        if CORRECTION_PHASE_VALIDATION_COMPLETED not in correction.phases:
            validation, source_tree_hash, recovery_evidence = (
                _run_post_rebase_validation(
                    profile,
                    attempt=attempt,
                    worktree_path=worktree_path,
                    original_source_head=original_source_head,
                    rebased_source_head=source_head,
                    target_commit=target_commit,
                    correction_id=correction_intent.correction_id,
                    attempt_count=1 if rebase_started else 0,
                    rebase_started=rebase_started,
                )
            )
            record_landing_correction_validation_completed(
                profile,
                intent=correction_intent,
                source_head_commit=source_head,
                target_commit=target_commit,
                source_tree_hash=source_tree_hash,
                validation_evidence_hash=_landing_correction_evidence_hash(
                    validation.to_dict()
                ),
                validation=validation,
            )
        else:
            validation_row = correction.phase_data[
                CORRECTION_PHASE_VALIDATION_COMPLETED
            ]
            source_tree_hash = str(validation_row["source_tree_hash"])
            current_intent = _landing_correction_intent(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt=attempt,
                request=request,
                generation=correction_intent.generation,
            )
            if (
                current_intent.source_head_commit != source_head
                or current_intent.target_commit != target_commit
                or current_intent.source_tree_hash != source_tree_hash
            ):
                raise AutomaticStaleRecoveryError(
                    state="retry_exhausted",
                    detail=(
                        "validated automatic stale recovery evidence no longer "
                        "matches the exact source and target"
                    ),
                    evidence=recovery_evidence,
                )

        corrected_request = _landing_request_with_automatic_validation(request)
        correction = load_landing_correction(
            profile,
            workset_id=correction_intent.workset_id,
            task_id=correction_intent.task_id,
            attempt_id=correction_intent.attempt_id,
            correction_id=correction_intent.correction_id,
        )
        assert correction is not None
        recovery_evidence = dict(recovery_evidence)
        recovery_evidence["receipt"] = correction.to_dict()
        return corrected_request, recovery_evidence, correction_intent
    except AutomaticStaleRecoveryError as exc:
        _record_automatic_correction_blocker(
            profile,
            correction_intent=correction_intent,
            exc=exc,
        )
        raise
    except WorktreeError as cause:
        evidence = _automatic_stale_recovery_payload(
            state="unsafe",
            correction_id=correction_intent.correction_id,
            attempt_count=0,
            worktree_path=Path(correction_intent.worktree_path),
            branch=correction_intent.branch,
            target_branch=correction_intent.target_branch,
            original_source_head=correction_intent.source_head_commit,
            rebased_source_head=None,
            target_commit=correction_intent.target_commit,
            rebase_started=False,
            rebase_aborted=False,
        )
        exc = AutomaticStaleRecoveryError(
            state="unsafe",
            detail=(
                "automatic stale recovery could not prove a safe correction or "
                "validation state"
            ),
            evidence=evidence,
        )
        _record_automatic_correction_blocker(
            profile,
            correction_intent=correction_intent,
            exc=exc,
        )
        raise exc from cause


def _normalized_landing_request(
    *,
    actor: str,
    summary: str,
    validations: tuple[ValidationRecord, ...],
    residuals: tuple[str, ...],
    followup_candidates: tuple[str, ...],
    note: str | None,
    cleanup: bool,
) -> dict[str, Any]:
    resolved_actor = str(actor or "").strip()
    resolved_summary = str(summary or "").strip()
    if not resolved_actor or not resolved_summary:
        raise BacklogError("landing actor and summary must be nonempty")
    resolved_validations: list[tuple[str, str]] = []
    for validation in validations:
        name = str(validation.name or "").strip()
        status = str(validation.status or "").strip()
        if not name or not status:
            raise BacklogError("landing validation name and status must be nonempty")
        if status not in VALIDATION_STATUSES:
            raise BacklogError(
                "landing validation status must be one of "
                + ", ".join(sorted(VALIDATION_STATUSES))
            )
        resolved_validations.append((name, status))

    def rows(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
        result = tuple(str(value or "").strip() for value in values)
        if any(not value for value in result):
            raise BacklogError(f"landing {label} values must be nonempty")
        return result

    return {
        "actor": resolved_actor,
        "summary": resolved_summary,
        "validations": tuple(resolved_validations),
        "residuals": rows(residuals, label="residual"),
        "followup_candidates": rows(followup_candidates, label="follow-up"),
        "note": str(note).strip() if note is not None and str(note).strip() else None,
        "cleanup": bool(cleanup),
    }


def _landing_request_with_automatic_validation(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    corrected_request = dict(request)
    corrected_request["validations"] = (
        *tuple(request["validations"]),
        (AUTOMATIC_STALE_REBASE_VALIDATION_NAME, "passed"),
    )
    return corrected_request


def _landing_request_has_evidence(
    *,
    summary: str | None,
    validations: tuple[ValidationRecord, ...],
) -> bool:
    if not str(summary or "").strip() or not validations:
        return False
    return all(
        str(validation.name or "").strip()
        and str(validation.status or "").strip() in VALIDATION_STATUSES
        for validation in validations
    )


def _build_landing_intent(
    profile: RepoProfile,
    *,
    workset: Workset,
    task: TaskSpec,
    attempt: Any,
    request: Mapping[str, Any],
) -> LandingIntent:
    if attempt.branch is None:
        raise WorktreeError(f"active attempt {attempt.attempt_id} is missing its branch")
    if attempt.target_branch is None:
        raise WorktreeError(f"active attempt {attempt.attempt_id} is missing its target_branch")
    if attempt.branch == attempt.target_branch:
        raise WorktreeError(f"refusing to land into the same branch: {attempt.target_branch}")
    if attempt.branch == "main":
        raise WorktreeError("refusing to land branch=main")
    primary_root = find_primary_worktree(profile.paths.project_root)
    task_worktree = _resolve_attempt_worktree(
        profile,
        branch=attempt.branch,
        worktree_path=attempt.worktree_path,
    )
    if task_worktree is None or not task_worktree.exists() or not _is_git_worktree_path(task_worktree):
        raise MissingTaskWorktreeError(task_worktree or attempt.worktree_path)
    source_registration = _registered_worktree_row(primary_root, task_worktree)
    if (
        source_registration is None
        or source_registration.get("branch") != f"refs/heads/{attempt.branch}"
    ):
        raise WorktreeError(
            "recorded task worktree is not registered to the primary repository and task branch"
        )
    branch_inspection = _inspect_branch_ref(primary_root, attempt.branch, role="task_branch")
    if branch_inspection.state == "error":
        raise _inspection_error(branch_inspection)
    source_head = branch_inspection.resolved_commit
    if source_head is None:
        raise WorktreeError(f"landing task branch {attempt.branch!r} is missing")
    if _run_git(task_worktree, "rev-parse", "HEAD") != source_head:
        raise WorktreeError("task worktree HEAD conflicts with its recorded branch ref")
    _require_no_in_progress_source_operation(task_worktree)

    target_inspection = _inspect_branch_ref(
        primary_root,
        attempt.target_branch,
        role="target_branch",
    )
    if target_inspection.state == "error":
        raise _inspection_error(target_inspection)
    target_base = target_inspection.resolved_commit
    if target_base is None:
        raise WorktreeError(f"landing target branch {attempt.target_branch!r} is missing")
    target_worktree = _find_worktree_for_branch(
        primary_root,
        f"refs/heads/{attempt.target_branch}",
    )
    if target_worktree is not None:
        if _managed_status_dirty(profile, target_worktree):
            if target_worktree == primary_root:
                raise dirty_primary_worktree_error(
                    profile,
                    branch=attempt.branch,
                    target_branch=attempt.target_branch,
                )
            raise DirtyTargetWorktreeError(target_worktree)
        if _run_git(target_worktree, "rev-parse", "HEAD") != target_base:
            raise WorktreeError("checked-out landing target HEAD conflicts with its branch ref")
    ancestor = _run_git_no_check(
        primary_root,
        "merge-base",
        "--is-ancestor",
        target_base,
        source_head,
    )
    if ancestor.returncode == 1:
        raise StaleTaskBranchError(
            branch=attempt.branch,
            target_branch=attempt.target_branch,
            branch_worktree=task_worktree,
        )
    if ancestor.returncode != 0:
        detail = ancestor.stderr.strip() or ancestor.stdout.strip() or f"exit code {ancestor.returncode}"
        raise WorktreeError(f"could not prove landing source ancestry: {detail}")

    source_manifest, expected_tree_hash = _projected_source_tree_manifest(task_worktree)
    source_fingerprint = hashlib.sha256(
        f"blackdog.landing.source/v1\0{source_head}\0{expected_tree_hash}".encode("utf-8")
    ).hexdigest()
    target_manifest, target_tree_hash = _committed_tree_manifest(primary_root, target_base)
    changed_paths = _tree_manifest_changed_paths(target_manifest, source_manifest)
    if not changed_paths or expected_tree_hash == target_tree_hash:
        raise NoChangesToLandError(
            branch=attempt.branch,
            target_branch=attempt.target_branch,
        )

    transaction_id = landing_transaction_id(
        workset_id=workset.workset_id,
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
    )
    temporary_path = (
        profile.paths.worktrees_dir / f"wt-land-{transaction_id[:24]}"
    ).resolve(strict=False)
    if temporary_path.exists() or _registered_worktree_row(primary_root, temporary_path) is not None:
        raise WorktreeError(
            f"deterministic landing path is already occupied before intent: {temporary_path}"
        )
    intent = LandingIntent(
        workset_id=workset.workset_id,
        task_id=task.task_id,
        attempt_id=attempt.attempt_id,
        actor=str(request["actor"]),
        branch=attempt.branch,
        target_branch=attempt.target_branch,
        worktree_path=str(task_worktree),
        primary_worktree=str(primary_root),
        target_base_commit=target_base,
        source_head_commit=source_head,
        source_fingerprint=source_fingerprint,
        expected_source_tree_hash=expected_tree_hash,
        source_dirty=_managed_status_dirty(profile, task_worktree),
        summary=str(request["summary"]),
        note=request["note"],
        validations=tuple(request["validations"]),
        residuals=tuple(request["residuals"]),
        followup_candidates=tuple(request["followup_candidates"]),
        changed_paths=changed_paths,
        cleanup=bool(request["cleanup"]),
        commit_message=_canonical_commit_message(
            workset,
            task,
            attempt_id=attempt.attempt_id,
            actor=str(request["actor"]),
            changed_paths=changed_paths,
            prompt_receipt=attempt.prompt_receipt,
            user_prompt_receipt=attempt.user_prompt_receipt,
            codex_session=attempt.codex_session,
            execution_model=attempt.execution_model,
            model=attempt.model,
            reasoning_effort=attempt.reasoning_effort,
            target_branch=attempt.target_branch,
            status=ATTEMPT_STATUS_SUCCESS,
            summary=str(request["summary"]),
            validations=tuple(
                ValidationRecord(name=name, status=status)
                for name, status in request["validations"]
            ),
            residuals=tuple(request["residuals"]),
            followup_candidates=tuple(request["followup_candidates"]),
        ),
        temporary_worktree_path=str(temporary_path),
    )
    # Validate that the exact value we will append can be decoded without
    # normalization before reserving the immutable event identity.
    if LandingIntent.from_dict(intent.to_dict()) != intent:
        raise LandingTransactionError("constructed landing intent does not round-trip canonically")
    return intent


def _landing_commit_parent(repo_root: Path, commit: str) -> str:
    row = _run_git(repo_root, "rev-list", "--parents", "-n", "1", commit)
    parts = row.split()
    if len(parts) != 2:
        raise WorktreeError(
            f"landing commit {commit[:12]} must have exactly one parent; got {max(0, len(parts) - 1)}"
        )
    return parts[1]


def _landing_commit_message(repo_root: Path, commit: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "show", "-s", "--format=%B", commit],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"could not read landing commit message for {commit}: {detail}")
    return completed.stdout.rstrip("\n") + "\n"


def _landing_commit_tree(repo_root: Path, commit: str) -> str:
    return _run_git(repo_root, "show", "-s", "--format=%T", commit)


def _require_landing_tree_hash(repo_root: Path, commit: str, expected: str, *, role: str) -> str:
    _manifest, actual = _committed_tree_manifest(repo_root, commit)
    if actual != expected:
        raise WorktreeError(
            f"{role} {commit[:12]} tree manifest does not match immutable landing intent"
        )
    return _landing_commit_tree(repo_root, commit)


def _landing_source_phase_data(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    workset: Workset,
    task: TaskSpec,
) -> dict[str, Any]:
    primary_root = Path(intent.primary_worktree)
    worktree_path = Path(intent.worktree_path)
    branch_inspection = _inspect_branch_ref(primary_root, intent.branch, role="task_branch")
    if branch_inspection.state == "error":
        raise _inspection_error(branch_inspection)
    if branch_inspection.state != "exists" or branch_inspection.resolved_commit is None:
        raise WorktreeError(f"landing task branch {intent.branch!r} is missing")
    current_head = branch_inspection.resolved_commit
    if not worktree_path.exists() or not _is_git_worktree_path(worktree_path):
        raise MissingTaskWorktreeError(worktree_path)
    source_registration = _registered_worktree_row(primary_root, worktree_path)
    if (
        source_registration is None
        or source_registration.get("branch") != f"refs/heads/{intent.branch}"
        or str(source_registration.get("HEAD") or "").strip() != current_head
        or _run_git(worktree_path, "rev-parse", "HEAD") != current_head
    ):
        raise WorktreeError(
            "landing source worktree is not coherently registered to its task branch"
        )

    prep_message = _landing_prep_commit_message(
        workset,
        task,
        attempt_id=intent.attempt_id,
    )
    prep_created = False
    if current_head == intent.source_head_commit:
        current_tree_hash, current_fingerprint = _landing_source_projection(
            worktree_path,
            intent.source_head_commit,
        )
        if (
            current_tree_hash != intent.expected_source_tree_hash
            or current_fingerprint != intent.source_fingerprint
        ):
            raise WorktreeError("landing source content changed after immutable intent was recorded")
        prepared = _commit_dirty_attempt_worktree(
            profile,
            workset=workset,
            task=task,
            branch=intent.branch,
            worktree_path=worktree_path,
            attempt_id=intent.attempt_id,
        )
        source_commit = prepared or current_head
        prep_created = prepared is not None
    else:
        if not intent.source_dirty:
            raise WorktreeError("landing task branch changed after immutable intent was recorded")
        source_commit = current_head
        if _landing_commit_parent(primary_root, source_commit) != intent.source_head_commit:
            raise WorktreeError("landing prep commit does not descend directly from the intended source head")
        if _landing_commit_message(primary_root, source_commit) != prep_message:
            raise WorktreeError("landing prep commit message does not match the canonical prep message")
        prep_created = True

    if _managed_status_dirty(profile, worktree_path):
        raise WorktreeError("landing source worktree changed while preparing the immutable source commit")
    source_tree = _require_landing_tree_hash(
        primary_root,
        source_commit,
        intent.expected_source_tree_hash,
        role="landing source commit",
    )
    target_manifest, _target_hash = _committed_tree_manifest(
        primary_root,
        intent.target_base_commit,
    )
    source_manifest, _source_hash = _committed_tree_manifest(primary_root, source_commit)
    actual_changed_paths = _tree_manifest_changed_paths(
        target_manifest,
        source_manifest,
    )
    if actual_changed_paths != tuple(sorted(intent.changed_paths)):
        raise WorktreeError(
            "prepared landing source changed paths do not match immutable landing intent"
        )
    if prep_created:
        if _landing_commit_parent(primary_root, source_commit) != intent.source_head_commit:
            raise WorktreeError("landing prep commit parent changed during source preparation")
        if _landing_commit_message(primary_root, source_commit) != prep_message:
            raise WorktreeError("landing prep commit message changed during source preparation")
    elif source_commit != intent.source_head_commit:
        raise WorktreeError("clean landing source no longer matches the intended source head")
    return {
        "source_commit": source_commit,
        "source_parent_commit": intent.source_head_commit if prep_created else None,
        "source_tree": source_tree,
        "source_tree_hash": intent.expected_source_tree_hash,
        "prep_commit_created": prep_created,
        "changed_paths": list(actual_changed_paths),
    }


def _verify_landing_source_phase(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    transaction: LandingTransaction,
    require_branch: bool,
) -> str:
    data = transaction.data_for("source_prepared")
    source_commit_value = data.get("source_commit")
    if not isinstance(source_commit_value, str) or not source_commit_value.strip():
        raise LandingTransactionError("source_prepared phase is missing source_commit")
    source_commit = source_commit_value
    primary_root = Path(intent.primary_worktree)
    inspection = _inspect_commit(primary_root, source_commit, role="landing_source_commit")
    if inspection.state == "error":
        raise _inspection_error(inspection)
    if inspection.state != "exists" or inspection.resolved_commit != source_commit:
        raise WorktreeError(f"landing source commit {source_commit!r} is missing")
    tree = _require_landing_tree_hash(
        primary_root,
        source_commit,
        intent.expected_source_tree_hash,
        role="landing source commit",
    )
    if data.get("source_tree_hash") != intent.expected_source_tree_hash or data.get("source_tree") != tree:
        raise LandingTransactionError("source_prepared phase tree evidence conflicts with Git")
    target_manifest, _target_hash = _committed_tree_manifest(
        primary_root,
        intent.target_base_commit,
    )
    source_manifest, _source_hash = _committed_tree_manifest(primary_root, source_commit)
    actual_changed_paths = _tree_manifest_changed_paths(
        target_manifest,
        source_manifest,
    )
    if (
        tuple(data.get("changed_paths") or ()) != actual_changed_paths
        or actual_changed_paths != tuple(sorted(intent.changed_paths))
    ):
        raise LandingTransactionError("source_prepared changed-path evidence conflicts with Git")
    prep_created_value = data.get("prep_commit_created")
    if type(prep_created_value) is not bool:
        raise LandingTransactionError("source_prepared prep_commit_created must be a boolean")
    prep_created = prep_created_value
    expected_parent = intent.source_head_commit if prep_created else None
    expected_data = {
        "source_commit": source_commit,
        "source_parent_commit": expected_parent,
        "source_tree": tree,
        "source_tree_hash": intent.expected_source_tree_hash,
        "prep_commit_created": prep_created,
        "changed_paths": list(actual_changed_paths),
    }
    if not strict_json_equal(dict(data), expected_data):
        raise LandingTransactionError("source_prepared phase evidence is not canonical")
    if prep_created:
        if _landing_commit_parent(primary_root, source_commit) != intent.source_head_commit:
            raise WorktreeError("recorded landing prep commit parent no longer matches intent")
        workset, task = _require_workset_and_task(
            profile,
            workset_id=intent.workset_id,
            task_id=intent.task_id,
        )
        if _landing_commit_message(primary_root, source_commit) != _landing_prep_commit_message(
            workset,
            task,
            attempt_id=intent.attempt_id,
        ):
            raise WorktreeError("recorded landing prep commit message no longer matches intent")
    elif source_commit != intent.source_head_commit:
        raise LandingTransactionError("recorded clean landing source conflicts with intent source head")
    if require_branch:
        branch_inspection = _inspect_branch_ref(primary_root, intent.branch, role="task_branch")
        if branch_inspection.state == "error":
            raise _inspection_error(branch_inspection)
        if branch_inspection.resolved_commit != source_commit:
            raise WorktreeError("landing task branch no longer points to the prepared source commit")
        worktree_path = Path(intent.worktree_path)
        registration = _registered_worktree_row(primary_root, worktree_path)
        if (
            not worktree_path.exists()
            or registration is None
            or registration.get("branch") != f"refs/heads/{intent.branch}"
            or str(registration.get("HEAD") or "").strip() != source_commit
            or _run_git(worktree_path, "rev-parse", "HEAD") != source_commit
            or _managed_status_dirty(profile, worktree_path)
        ):
            raise WorktreeError("landing source worktree is missing or changed after source preparation")
    return source_commit


def _registered_worktree_row(
    primary_root: Path,
    worktree_path: Path,
) -> dict[str, str] | None:
    expected = worktree_path.resolve(strict=False)
    matches = [
        row
        for row in _parse_worktree_list(primary_root)
        if row.get("worktree")
        and Path(row["worktree"]).resolve(strict=False) == expected
    ]
    if len(matches) > 1:
        raise WorktreeError(f"Git registered landing path more than once: {expected}")
    return matches[0] if matches else None


def _require_transaction_temporary_worktree(
    primary_root: Path,
    temporary_path: Path,
    *,
    expected_commits: tuple[str, ...],
) -> str:
    row = _registered_worktree_row(primary_root, temporary_path)
    if row is None:
        raise WorktreeError(
            f"deterministic landing path is not registered to the primary repository: {temporary_path}"
        )
    if "detached" not in row or row.get("branch"):
        raise WorktreeError(
            f"deterministic landing worktree is not detached: {temporary_path}"
        )
    registered_head = str(row.get("HEAD") or "").strip()
    if registered_head not in expected_commits:
        raise WorktreeError(
            f"deterministic landing worktree has unexpected registered HEAD {registered_head!r}"
        )
    if not temporary_path.exists() or not _is_git_worktree_path(temporary_path):
        raise WorktreeError(
            f"registered deterministic landing worktree is missing or invalid: {temporary_path}"
        )
    actual_head = _run_git(temporary_path, "rev-parse", "HEAD")
    if actual_head != registered_head:
        raise WorktreeError(
            f"deterministic landing worktree HEAD conflicts with its registration: {temporary_path}"
        )
    return actual_head


def _canonical_landing_phase_data(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    source_commit: str,
) -> dict[str, Any]:
    primary_root = Path(intent.primary_worktree)
    temporary_path = Path(intent.temporary_worktree_path)
    registered = _registered_worktree_row(primary_root, temporary_path)
    if temporary_path.exists() and registered is None:
        raise WorktreeError(
            f"deterministic landing path exists but is not registered to the primary repository: {temporary_path}"
        )
    if registered is not None and not temporary_path.exists():
        raise WorktreeError(
            f"deterministic landing worktree registration exists without its path: {temporary_path}"
        )
    if registered is None:
        temporary_path.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            primary_root,
            "worktree",
            "add",
            "--detach",
            str(temporary_path),
            intent.target_base_commit,
        )
    registered_head = str((registered or {}).get("HEAD") or "").strip()
    allowed_heads = (
        (intent.target_base_commit, registered_head)
        if registered_head and registered_head != intent.target_base_commit
        else (intent.target_base_commit,)
    )
    current_head = _require_transaction_temporary_worktree(
        primary_root,
        temporary_path,
        expected_commits=allowed_heads,
    )
    if current_head == intent.target_base_commit:
        if _status_dirty(temporary_path):
            for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD"):
                marker_path = Path(_run_git(temporary_path, "rev-parse", "--git-path", marker))
                if not marker_path.is_absolute():
                    marker_path = temporary_path / marker_path
                if marker_path.exists():
                    raise WorktreeError(
                        f"deterministic landing worktree has conflicting Git state {marker}: {temporary_path}"
                    )
            unmerged = _run_git_bytes(temporary_path, "ls-files", "--unmerged", "-z")
            if unmerged:
                raise WorktreeError(
                    f"deterministic landing worktree has unresolved merge entries: {temporary_path}"
                )
            _manifest, projected_hash = _projected_source_tree_manifest(temporary_path)
            if projected_hash != intent.expected_source_tree_hash:
                raise WorktreeError(
                    "interrupted canonical landing content does not match immutable landing intent"
                )
            # A process may stop after the successful squash and before the
            # commit. Re-stage the already-proven projection so recovery also
            # repairs a partially-written index without accepting new content.
            _run_git(temporary_path, "add", "-A")
        else:
            completed = _run_git_no_check(
                temporary_path,
                "merge",
                "--squash",
                "--no-commit",
                source_commit,
            )
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
                raise WorktreeError(f"git merge --squash --no-commit {source_commit} failed: {detail}")
        _manifest, staged_tree_hash = _committed_tree_manifest(
            temporary_path,
            _run_git(temporary_path, "write-tree"),
        )
        if staged_tree_hash != intent.expected_source_tree_hash:
            raise WorktreeError(
                "canonical landing index does not match immutable landing intent"
            )
        _run_git_with_input(
            temporary_path,
            "commit",
            "--quiet",
            "-F",
            "-",
            input_text=intent.commit_message,
        )
        landed_commit = _run_git(temporary_path, "rev-parse", "HEAD")
    else:
        landed_commit = current_head
    parent = _landing_commit_parent(primary_root, landed_commit)
    if parent != intent.target_base_commit:
        raise WorktreeError("canonical landing commit parent does not match immutable target base")
    if _landing_commit_message(primary_root, landed_commit) != intent.commit_message:
        raise WorktreeError("canonical landing commit message does not match immutable intent")
    landed_tree = _require_landing_tree_hash(
        primary_root,
        landed_commit,
        intent.expected_source_tree_hash,
        role="canonical landing commit",
    )
    if _status_dirty(temporary_path):
        raise WorktreeError("deterministic landing worktree is dirty after canonical commit creation")
    _require_transaction_temporary_worktree(
        primary_root,
        temporary_path,
        expected_commits=(landed_commit,),
    )
    return {
        "landed_commit": landed_commit,
        "landed_parent_commit": parent,
        "landed_tree": landed_tree,
        "landed_tree_hash": intent.expected_source_tree_hash,
        "temporary_worktree_path": str(temporary_path),
    }


def _verify_canonical_landing_phase(
    *,
    intent: LandingIntent,
    transaction: LandingTransaction,
) -> str:
    data = transaction.data_for("canonical_commit_created")
    landed_commit_value = data.get("landed_commit")
    if not isinstance(landed_commit_value, str) or not landed_commit_value.strip():
        raise LandingTransactionError("canonical_commit_created phase is missing landed_commit")
    landed_commit = landed_commit_value
    primary_root = Path(intent.primary_worktree)
    inspection = _inspect_commit(primary_root, landed_commit, role="canonical_landing_commit")
    if inspection.state == "error":
        raise _inspection_error(inspection)
    if inspection.state != "exists" or inspection.resolved_commit != landed_commit:
        raise WorktreeError(f"canonical landing commit {landed_commit!r} is missing")
    parent = _landing_commit_parent(primary_root, landed_commit)
    tree = _require_landing_tree_hash(
        primary_root,
        landed_commit,
        intent.expected_source_tree_hash,
        role="canonical landing commit",
    )
    expected_data = {
        "landed_commit": landed_commit,
        "landed_parent_commit": parent,
        "landed_tree": tree,
        "landed_tree_hash": intent.expected_source_tree_hash,
        "temporary_worktree_path": intent.temporary_worktree_path,
    }
    if (
        parent != intent.target_base_commit
        or not strict_json_equal(dict(data), expected_data)
        or _landing_commit_message(primary_root, landed_commit) != intent.commit_message
    ):
        raise LandingTransactionError("canonical_commit_created evidence conflicts with Git or intent")
    base_manifest, _base_hash = _committed_tree_manifest(
        primary_root,
        intent.target_base_commit,
    )
    landed_manifest, _landed_hash = _committed_tree_manifest(primary_root, landed_commit)
    if _tree_manifest_changed_paths(base_manifest, landed_manifest) != intent.changed_paths:
        raise LandingTransactionError(
            "canonical landing changed paths conflict with immutable intent"
        )
    return landed_commit


def _target_contains_landed_commit(
    primary_root: Path,
    *,
    target_commit: str,
    landed_commit: str,
) -> bool:
    relationship = _run_git_no_check(
        primary_root,
        "merge-base",
        "--is-ancestor",
        landed_commit,
        target_commit,
    )
    if relationship.returncode == 0:
        return True
    if relationship.returncode == 1:
        return False
    detail = relationship.stderr.strip() or relationship.stdout.strip() or f"exit code {relationship.returncode}"
    raise WorktreeError(f"could not inspect target landing reachability: {detail}")


def _update_landing_target(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    landed_commit: str,
) -> dict[str, Any]:
    primary_root = Path(intent.primary_worktree)
    target_inspection = _inspect_branch_ref(
        primary_root,
        intent.target_branch,
        role="target_branch",
    )
    if target_inspection.state == "error":
        raise _inspection_error(target_inspection)
    current_target = target_inspection.resolved_commit
    if current_target is None:
        raise WorktreeError(f"landing target branch {intent.target_branch!r} is missing")
    target_worktree = _find_worktree_for_branch(
        primary_root,
        f"refs/heads/{intent.target_branch}",
    )
    if target_worktree is not None:
        if _managed_status_dirty(profile, target_worktree):
            if target_worktree == primary_root:
                raise dirty_primary_worktree_error(
                    profile,
                    branch=intent.branch,
                    target_branch=intent.target_branch,
                )
            raise DirtyTargetWorktreeError(target_worktree)
        if _run_git(target_worktree, "rev-parse", "HEAD") != current_target:
            raise WorktreeError(
                "checked-out landing target HEAD conflicts with its branch ref"
            )
    update_mode = "already_contains"
    if current_target == intent.target_base_commit:
        if target_worktree is not None:
            _run_git(target_worktree, "merge", "--ff-only", landed_commit)
            update_mode = "checked_out_fast_forward"
        else:
            _run_git(
                primary_root,
                "update-ref",
                f"refs/heads/{intent.target_branch}",
                landed_commit,
                intent.target_base_commit,
            )
            update_mode = "atomic_update_ref"
        current_target = landed_commit
    elif not _target_contains_landed_commit(
        primary_root,
        target_commit=current_target,
        landed_commit=landed_commit,
    ):
        raise StaleTaskBranchError(
            branch=intent.branch,
            target_branch=intent.target_branch,
            branch_worktree=Path(intent.worktree_path),
        )
    final_inspection = _inspect_branch_ref(
        primary_root,
        intent.target_branch,
        role="target_branch",
    )
    if final_inspection.state == "error":
        raise _inspection_error(final_inspection)
    final_target = final_inspection.resolved_commit
    if final_target is None or not _target_contains_landed_commit(
        primary_root,
        target_commit=final_target,
        landed_commit=landed_commit,
    ):
        raise WorktreeError("landing target update did not retain the canonical commit")
    if target_worktree is not None:
        if _run_git(target_worktree, "rev-parse", "HEAD") != final_target:
            raise WorktreeError("checked-out landing target HEAD is incoherent after update")
        if _managed_status_dirty(profile, target_worktree):
            raise WorktreeError("checked-out landing target is dirty after update")
    current_target = final_target
    return {
        "target_base_commit": intent.target_base_commit,
        "landed_commit": landed_commit,
        "target_commit": current_target,
        "update_mode": update_mode,
    }


def _verify_target_updated_phase(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    transaction: LandingTransaction,
    landed_commit: str,
    require_live_target: bool = True,
) -> str:
    data = transaction.data_for("target_updated")
    primary_root = Path(intent.primary_worktree)
    expected_keys = {
        "target_base_commit",
        "landed_commit",
        "target_commit",
        "update_mode",
    }
    if set(data) != expected_keys:
        raise LandingTransactionError("target_updated phase has conflicting fields")
    recorded_target = data.get("target_commit")
    update_mode = data.get("update_mode")
    if not isinstance(recorded_target, str) or not recorded_target.strip():
        raise LandingTransactionError("target_updated target_commit must be a nonempty string")
    if update_mode not in {
        "already_contains",
        "checked_out_fast_forward",
        "atomic_update_ref",
    }:
        raise LandingTransactionError("target_updated update_mode is invalid")
    expected_data = {
        "target_base_commit": intent.target_base_commit,
        "landed_commit": landed_commit,
        "target_commit": recorded_target,
        "update_mode": update_mode,
    }
    if not strict_json_equal(dict(data), expected_data):
        raise LandingTransactionError("target_updated phase evidence is not canonical")
    recorded_inspection = _inspect_commit(
        primary_root,
        recorded_target,
        role="recorded_target_commit",
    )
    if recorded_inspection.state == "error":
        raise _inspection_error(recorded_inspection)
    if (
        recorded_inspection.resolved_commit != recorded_target
        or not _target_contains_landed_commit(
            primary_root,
            target_commit=recorded_target,
            landed_commit=landed_commit,
        )
    ):
        raise WorktreeError("recorded landing target proof is missing or incoherent")
    if not require_live_target:
        return recorded_target
    target_inspection = _inspect_branch_ref(
        primary_root,
        intent.target_branch,
        role="target_branch",
    )
    if target_inspection.state == "error":
        raise _inspection_error(target_inspection)
    target_commit = target_inspection.resolved_commit
    if target_commit is None or not _target_contains_landed_commit(
        primary_root,
        target_commit=target_commit,
        landed_commit=landed_commit,
    ):
        raise WorktreeError("recorded canonical landing is no longer reachable from its target branch")
    target_worktree = _find_worktree_for_branch(
        primary_root,
        f"refs/heads/{intent.target_branch}",
    )
    if target_worktree is not None:
        if _run_git(target_worktree, "rev-parse", "HEAD") != target_commit:
            raise WorktreeError(
                "checked-out landing target HEAD conflicts with its recorded branch ref"
            )
        if _managed_status_dirty(profile, target_worktree):
            raise WorktreeError(
                "checked-out landing target is dirty after recorded update"
            )
    if (
        data.get("landed_commit") != landed_commit
        or data.get("target_base_commit") != intent.target_base_commit
    ):
        raise LandingTransactionError("target_updated phase evidence conflicts with immutable landing intent")
    return target_commit


def _temporary_landing_cleanup_phase_data(
    *,
    intent: LandingIntent,
    landed_commit: str,
) -> dict[str, Any]:
    primary_root = Path(intent.primary_worktree)
    temporary_path = Path(intent.temporary_worktree_path)
    registered = _registered_worktree_row(primary_root, temporary_path)
    if temporary_path.exists():
        _require_transaction_temporary_worktree(
            primary_root,
            temporary_path,
            expected_commits=(landed_commit,),
        )
        if _status_dirty(temporary_path):
            raise WorktreeError(
                f"refusing to remove dirty deterministic landing worktree: {temporary_path}"
            )
        _run_git(primary_root, "worktree", "remove", str(temporary_path))
    elif registered is not None:
        if "detached" not in registered or registered.get("branch"):
            raise WorktreeError(
                f"stale deterministic landing registration is not detached: {temporary_path}"
            )
        if str(registered.get("HEAD") or "").strip() != landed_commit:
            raise WorktreeError(
                "stale deterministic landing registration has unexpected commit"
            )
        _run_git(primary_root, "worktree", "remove", "--force", str(temporary_path))
    if temporary_path.exists() or _registered_worktree_row(primary_root, temporary_path) is not None:
        raise WorktreeError(
            f"deterministic landing worktree cleanup is incomplete: {temporary_path}"
        )
    return {
        "temporary_worktree_path": str(temporary_path),
        "landed_commit": landed_commit,
        "worktree_absent": True,
        "registration_absent": True,
    }


def _verify_temporary_landing_cleanup_phase(
    *,
    intent: LandingIntent,
    transaction: LandingTransaction,
    landed_commit: str,
) -> None:
    data = transaction.data_for("temporary_cleanup_complete")
    temporary_path = Path(intent.temporary_worktree_path)
    expected_data = {
        "temporary_worktree_path": str(temporary_path),
        "landed_commit": landed_commit,
        "worktree_absent": True,
        "registration_absent": True,
    }
    if not strict_json_equal(dict(data), expected_data):
        raise LandingTransactionError(
            "temporary_cleanup_complete phase evidence conflicts with immutable landing intent"
        )
    if temporary_path.exists() or _registered_worktree_row(
        Path(intent.primary_worktree), temporary_path
    ) is not None:
        raise WorktreeError(
            "recorded deterministic landing worktree cleanup no longer holds"
        )


def _abort_canonical_candidate(
    profile: RepoProfile,
    *,
    transaction: LandingTransaction,
) -> str | None:
    intent = transaction.intent
    if "canonical_commit_created" in transaction.phases:
        return _verify_canonical_landing_phase(intent=intent, transaction=transaction)
    primary_root = Path(intent.primary_worktree)
    temporary_path = Path(intent.temporary_worktree_path)
    registration = _registered_worktree_row(primary_root, temporary_path)
    if not temporary_path.exists() and registration is None:
        return None
    if not temporary_path.exists() or registration is None:
        raise WorktreeError(
            "cannot safely abort: deterministic landing path and registration disagree"
        )
    if "detached" not in registration or registration.get("branch"):
        raise WorktreeError("cannot safely abort a non-detached landing worktree")
    head = _run_git(temporary_path, "rev-parse", "HEAD")
    if str(registration.get("HEAD") or "").strip() != head:
        raise WorktreeError("cannot safely abort an incoherent landing worktree")
    if head == intent.target_base_commit:
        if _status_dirty(temporary_path):
            if _run_git_bytes(temporary_path, "ls-files", "--unmerged", "-z"):
                raise WorktreeError(
                    "cannot safely abort while the landing worktree has unresolved entries"
                )
            _manifest, projected_hash = _projected_source_tree_manifest(temporary_path)
            if projected_hash != intent.expected_source_tree_hash:
                raise WorktreeError(
                    "cannot safely abort: landing worktree content is not transaction-owned"
                )
        return None
    if _status_dirty(temporary_path):
        raise WorktreeError("cannot safely abort a modified canonical landing candidate")
    if (
        _landing_commit_parent(primary_root, head) != intent.target_base_commit
        or _landing_commit_message(primary_root, head) != intent.commit_message
    ):
        raise WorktreeError("cannot safely abort an unrecognized landing candidate")
    _require_landing_tree_hash(
        primary_root,
        head,
        intent.expected_source_tree_hash,
        role="abort landing candidate",
    )
    return head


def _existing_canonical_landing_phase_data(
    *,
    intent: LandingIntent,
    landed_commit: str,
) -> dict[str, Any]:
    primary_root = Path(intent.primary_worktree)
    parent = _landing_commit_parent(primary_root, landed_commit)
    tree = _require_landing_tree_hash(
        primary_root,
        landed_commit,
        intent.expected_source_tree_hash,
        role="abort landing candidate",
    )
    if (
        parent != intent.target_base_commit
        or _landing_commit_message(primary_root, landed_commit) != intent.commit_message
    ):
        raise WorktreeError(
            "abort landing candidate conflicts with immutable landing intent"
        )
    base_manifest, _base_hash = _committed_tree_manifest(
        primary_root,
        intent.target_base_commit,
    )
    landed_manifest, _landed_hash = _committed_tree_manifest(
        primary_root,
        landed_commit,
    )
    if _tree_manifest_changed_paths(base_manifest, landed_manifest) != intent.changed_paths:
        raise WorktreeError(
            "abort landing candidate changed paths conflict with immutable landing intent"
        )
    return {
        "landed_commit": landed_commit,
        "landed_parent_commit": parent,
        "landed_tree": tree,
        "landed_tree_hash": intent.expected_source_tree_hash,
        "temporary_worktree_path": intent.temporary_worktree_path,
    }


def _landing_abort_target_state(
    *,
    intent: LandingIntent,
    landed_commit: str | None,
) -> tuple[str | None, bool]:
    primary_root = Path(intent.primary_worktree)
    target = _inspect_branch_ref(
        primary_root,
        intent.target_branch,
        role="target_branch",
    )
    if target.state == "error":
        raise _inspection_error(target)
    target_commit = target.resolved_commit
    contains = bool(
        landed_commit is not None
        and target_commit is not None
        and _target_contains_landed_commit(
            primary_root,
            target_commit=target_commit,
            landed_commit=landed_commit,
        )
    )
    return target_commit, contains


def _landing_abort_finalization_id(transaction_id: str) -> str:
    return hashlib.sha256(
        f"blackdog.worktree.landing.abort-finalization/v1\0{transaction_id}".encode(
            "utf-8"
        )
    ).hexdigest()


def _landing_abort_close_event_id(transaction_id: str) -> str:
    return hashlib.sha256(
        f"blackdog.worktree.landing.abort-close/v1\0{transaction_id}".encode(
            "utf-8"
        )
    ).hexdigest()


def _landing_abort_finalization_started(
    profile: RepoProfile,
    *,
    transaction: LandingTransaction,
) -> bool:
    if transaction.abort_data is None:
        return False
    request = transaction.abort_data.get("close_request")
    if not isinstance(request, Mapping):
        raise LandingTransactionError("landing abort close request is missing")
    expected_id = request.get("finalization_id")
    with exclusive_file_lock(profile.paths.events_file):
        events = load_events(profile.paths.events_file)
    for event in events:
        if event.get("type") != "task.finalization.request":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if (
            payload.get("workset_id") == transaction.intent.workset_id
            and payload.get("task_id") == transaction.intent.task_id
            and payload.get("attempt_id") == transaction.intent.attempt_id
        ):
            if payload.get("finalization_id") != expected_id:
                raise LandingTransactionError(
                    "landing abort attempt has a conflicting durable finalization request"
                )
            return True
    return False


def _landing_abort_close_request(
    *,
    intent: LandingIntent,
    actor: str,
    status: str,
    summary: str,
    validations: tuple[ValidationRecord, ...],
    residuals: tuple[str, ...],
    followup_candidates: tuple[str, ...],
    note: str | None,
    cleanup: bool,
    failure_class: str | None,
    recovery_action: str | None,
    prompt_issue: bool,
    operator_issue: bool,
) -> dict[str, Any]:
    if actor != intent.actor:
        raise WorktreeError(
            f"landing transaction is owned by {intent.actor!r}, not {actor!r}"
        )
    if status not in {
        ATTEMPT_STATUS_BLOCKED,
        ATTEMPT_STATUS_FAILED,
        ATTEMPT_STATUS_ABANDONED,
    }:
        raise WorktreeError("landing abort status must be blocked, failed, or abandoned")
    failure_details = _failure_details_for_status(
        status,
        recovery_action=recovery_action,
    )
    if failure_class is not None:
        failure_details["failure_class"] = failure_class
    failure_details["prompt_issue"] = bool(
        prompt_issue or failure_details["prompt_issue"]
    )
    failure_details["operator_issue"] = bool(
        operator_issue or failure_details["operator_issue"]
    )
    return {
        "actor": actor,
        "status": status,
        "summary": summary,
        "validations": [
            {"name": row.name, "status": row.status} for row in validations
        ],
        "residuals": list(residuals),
        "followup_candidates": list(followup_candidates),
        "note": note,
        "cleanup_requested": cleanup,
        "failure_class": failure_details["failure_class"],
        "recovery_action": failure_details["recovery_action"],
        "prompt_issue": failure_details["prompt_issue"],
        "operator_issue": failure_details["operator_issue"],
        "finalization_id": _landing_abort_finalization_id(intent.transaction_id),
    }


def _landing_abort_data(
    profile: RepoProfile,
    *,
    transaction: LandingTransaction,
    source_commit: str,
    landed_commit: str | None,
    close_request: Mapping[str, Any],
) -> dict[str, Any]:
    intent = transaction.intent
    primary_root = Path(intent.primary_worktree)
    target = _inspect_branch_ref(primary_root, intent.target_branch, role="target_branch")
    if target.state == "error":
        raise _inspection_error(target)
    target_commit = target.resolved_commit
    if landed_commit is not None and target_commit is not None and _target_contains_landed_commit(
        primary_root,
        target_commit=target_commit,
        landed_commit=landed_commit,
    ):
        raise WorktreeError(
            "refusing close: target already contains the canonical landing commit"
        )
    return {
        "target_branch": intent.target_branch,
        "observed_target_commit": target_commit,
        "landed_commit": landed_commit,
        "target_contains_landed": False,
        "source_commit": source_commit,
        "source_tree_hash": intent.expected_source_tree_hash,
        "source_worktree_path": intent.worktree_path,
        "source_branch": intent.branch,
        "temporary_worktree_path": intent.temporary_worktree_path,
        "close_request": dict(close_request),
    }


def _verify_landing_abort(
    profile: RepoProfile,
    *,
    transaction: LandingTransaction,
    close_request: Mapping[str, Any] | None = None,
    require_source: bool = True,
) -> str:
    if not transaction.abort_requested or transaction.abort_data is None:
        raise LandingTransactionError("landing transaction is not durably aborted")
    intent = transaction.intent
    data = transaction.abort_data
    source_commit = data.get("source_commit")
    landed_commit = data.get("landed_commit")
    observed_target = data.get("observed_target_commit")
    if not isinstance(source_commit, str) or not source_commit.strip():
        raise LandingTransactionError("landing abort source_commit is invalid")
    if landed_commit is not None and (
        not isinstance(landed_commit, str) or not landed_commit.strip()
    ):
        raise LandingTransactionError("landing abort landed_commit is invalid")
    if observed_target is not None and (
        not isinstance(observed_target, str) or not observed_target.strip()
    ):
        raise LandingTransactionError("landing abort target commit is invalid")
    stored_request = data.get("close_request")
    if not isinstance(stored_request, Mapping):
        raise LandingTransactionError("landing abort close request is invalid")
    expected_request_keys = {
        "actor",
        "status",
        "summary",
        "validations",
        "residuals",
        "followup_candidates",
        "note",
        "cleanup_requested",
        "failure_class",
        "recovery_action",
        "prompt_issue",
        "operator_issue",
        "finalization_id",
    }
    if set(stored_request) != expected_request_keys:
        raise LandingTransactionError("landing abort close request has conflicting fields")
    if (
        stored_request.get("actor") != intent.actor
        or stored_request.get("status")
        not in {
            ATTEMPT_STATUS_BLOCKED,
            ATTEMPT_STATUS_FAILED,
            ATTEMPT_STATUS_ABANDONED,
        }
        or not isinstance(stored_request.get("summary"), str)
        or not str(stored_request.get("summary") or "").strip()
        or not isinstance(stored_request.get("validations"), list)
        or not isinstance(stored_request.get("residuals"), list)
        or not isinstance(stored_request.get("followup_candidates"), list)
        or stored_request.get("note") is not None
        and not isinstance(stored_request.get("note"), str)
        or type(stored_request.get("cleanup_requested")) is not bool
        or not isinstance(stored_request.get("failure_class"), str)
        or not isinstance(stored_request.get("recovery_action"), str)
        or type(stored_request.get("prompt_issue")) is not bool
        or type(stored_request.get("operator_issue")) is not bool
        or stored_request.get("finalization_id")
        != _landing_abort_finalization_id(intent.transaction_id)
    ):
        raise LandingTransactionError("landing abort close request is not canonical")
    try:
        stored_validations = tuple(
            ValidationRecord(name=row["name"], status=row["status"])
            for row in stored_request["validations"]
            if isinstance(row, Mapping) and set(row) == {"name", "status"}
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LandingTransactionError("landing abort validations are invalid") from exc
    if len(stored_validations) != len(stored_request["validations"]):
        raise LandingTransactionError("landing abort validations are invalid")
    if any(not isinstance(value, str) for value in stored_request["residuals"]):
        raise LandingTransactionError("landing abort residuals are invalid")
    if any(
        not isinstance(value, str)
        for value in stored_request["followup_candidates"]
    ):
        raise LandingTransactionError("landing abort followups are invalid")
    if close_request is not None and not strict_json_equal(
        dict(stored_request), dict(close_request)
    ):
        raise LandingTransactionError(
            "landing abort retry conflicts with the durable close request"
        )
    expected = {
        "target_branch": intent.target_branch,
        "observed_target_commit": observed_target,
        "landed_commit": landed_commit,
        "target_contains_landed": False,
        "source_commit": source_commit,
        "source_tree_hash": intent.expected_source_tree_hash,
        "source_worktree_path": intent.worktree_path,
        "source_branch": intent.branch,
        "temporary_worktree_path": intent.temporary_worktree_path,
        "close_request": dict(stored_request),
    }
    if not strict_json_equal(dict(data), expected):
        raise LandingTransactionError("landing abort evidence is not canonical")
    _require_landing_tree_hash(
        Path(intent.primary_worktree),
        source_commit,
        intent.expected_source_tree_hash,
        role="aborted landing source",
    )
    if landed_commit is not None:
        if "canonical_commit_created" not in transaction.phases:
            raise LandingTransactionError(
                "landing abort candidate is missing canonical phase evidence"
            )
        if _verify_canonical_landing_phase(
            intent=intent,
            transaction=transaction,
        ) != landed_commit:
            raise LandingTransactionError(
                "landing abort candidate conflicts with canonical phase evidence"
            )
    if require_source:
        branch = _inspect_branch_ref(
            Path(intent.primary_worktree), intent.branch, role="task_branch"
        )
        registration = _registered_worktree_row(
            Path(intent.primary_worktree), Path(intent.worktree_path)
        )
        if (
            branch.resolved_commit != source_commit
            or registration is None
            or registration.get("branch") != f"refs/heads/{intent.branch}"
            or str(registration.get("HEAD") or "").strip() != source_commit
            or not Path(intent.worktree_path).exists()
            or _run_git(Path(intent.worktree_path), "rev-parse", "HEAD") != source_commit
            or _managed_status_dirty(profile, Path(intent.worktree_path))
        ):
            raise WorktreeError(
                "aborted landing source carry is no longer clean and coherent"
            )
    return source_commit


def _cleanup_aborted_landing_temporary(
    profile: RepoProfile,
    *,
    transaction: LandingTransaction,
) -> dict[str, Any]:
    intent = transaction.intent
    primary_root = Path(intent.primary_worktree)
    temporary_path = Path(intent.temporary_worktree_path)
    candidate = transaction.abort_data.get("landed_commit") if transaction.abort_data else None
    registration = _registered_worktree_row(primary_root, temporary_path)
    if temporary_path.exists():
        allowed = tuple(
            value
            for value in (intent.target_base_commit, candidate)
            if isinstance(value, str)
        )
        head = _require_transaction_temporary_worktree(
            primary_root,
            temporary_path,
            expected_commits=allowed,
        )
        force = False
        if _status_dirty(temporary_path):
            if head != intent.target_base_commit:
                raise WorktreeError("cannot remove a modified aborted landing candidate")
            if _run_git_bytes(temporary_path, "ls-files", "--unmerged", "-z"):
                raise WorktreeError("cannot remove unresolved aborted landing worktree")
            _manifest, projected_hash = _projected_source_tree_manifest(temporary_path)
            if projected_hash != intent.expected_source_tree_hash:
                raise WorktreeError("aborted landing worktree content is not transaction-owned")
            force = True
        args = ("worktree", "remove", "--force", str(temporary_path)) if force else (
            "worktree",
            "remove",
            str(temporary_path),
        )
        _run_git(primary_root, *args)
    elif registration is not None:
        raise WorktreeError("aborted landing registration exists without its worktree path")
    if temporary_path.exists() or _registered_worktree_row(primary_root, temporary_path) is not None:
        raise WorktreeError("aborted landing temporary cleanup is incomplete")
    return {
        "temporary_worktree_path": str(temporary_path),
        "worktree_absent": True,
        "registration_absent": True,
    }


def _verify_landing_abort_chain(
    profile: RepoProfile,
    *,
    transaction: LandingTransaction,
    close_request: Mapping[str, Any] | None = None,
    require_source: bool = True,
) -> str:
    source_commit = _verify_landing_abort(
        profile,
        transaction=transaction,
        close_request=close_request,
        require_source=require_source,
    )
    intent = transaction.intent
    data = transaction.abort_data
    assert data is not None
    landed_commit = data.get("landed_commit")
    stored_request = data.get("close_request")
    assert isinstance(stored_request, Mapping)
    expected_cleanup = {
        "temporary_worktree_path": intent.temporary_worktree_path,
        "worktree_absent": True,
        "registration_absent": True,
    }
    if transaction.abort_cleanup_complete:
        if not strict_json_equal(transaction.abort_cleanup_data, expected_cleanup):
            raise LandingTransactionError(
                "landing abort cleanup evidence is not canonical"
            )
        if Path(intent.temporary_worktree_path).exists() or _registered_worktree_row(
            Path(intent.primary_worktree), Path(intent.temporary_worktree_path)
        ) is not None:
            raise WorktreeError("landing abort temporary cleanup proof no longer holds")
    if transaction.abort_superseded:
        if _landing_abort_finalization_started(profile, transaction=transaction):
            raise LandingTransactionError(
                "landing abort supersession conflicts with a durable abort finalization request"
            )
        if not transaction.abort_cleanup_complete or not isinstance(landed_commit, str):
            raise LandingTransactionError(
                "landing abort supersession requires a cleaned canonical candidate"
            )
        supersession = transaction.abort_superseded_data
        assert supersession is not None
        recorded_target = supersession.get("target_commit")
        expected_supersession = {
            "landed_commit": landed_commit,
            "target_branch": intent.target_branch,
            "target_commit": recorded_target,
            "reason": "target_contains_recorded_candidate",
        }
        if (
            not isinstance(recorded_target, str)
            or not recorded_target.strip()
            or not strict_json_equal(supersession, expected_supersession)
            or not _target_contains_landed_commit(
                Path(intent.primary_worktree),
                target_commit=recorded_target,
                landed_commit=landed_commit,
            )
        ):
            raise LandingTransactionError(
                "landing abort supersession evidence is not canonical"
            )
        current_target, current_contains = _landing_abort_target_state(
            intent=intent,
            landed_commit=landed_commit,
        )
        if current_target is None or not current_contains:
            raise WorktreeError(
                "superseded landing abort candidate is no longer reachable from target"
            )
    finalization_id = stored_request["finalization_id"]
    if transaction.abort_runtime_finalized:
        expected_runtime = {
            "finalization_id": finalization_id,
            "status": stored_request["status"],
            "source_commit": source_commit,
            "changed_paths": list(intent.changed_paths),
        }
        if not strict_json_equal(transaction.abort_runtime_data, expected_runtime):
            raise LandingTransactionError(
                "landing abort runtime evidence is not canonical"
            )
    if transaction.abort_close_event_recorded:
        expected_close = {
            "event_id": _landing_abort_close_event_id(intent.transaction_id),
            "status": stored_request["status"],
        }
        if not strict_json_equal(transaction.abort_close_data, expected_close):
            raise LandingTransactionError(
                "landing abort close-event evidence is not canonical"
            )
    if transaction.abort_complete:
        expected_complete = {
            "finalization_id": finalization_id,
            "status": stored_request["status"],
            "source_retained": True,
        }
        if not strict_json_equal(transaction.abort_complete_data, expected_complete):
            raise LandingTransactionError(
                "landing abort completion evidence is not canonical"
            )
    return source_commit


def _canonical_evidence_hash(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LandingTransactionError("workspace adoption evidence is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _workspace_adoption_id(transaction_id: str) -> str:
    return hashlib.sha256(
        f"blackdog.workspace.adoption/v1\0{transaction_id}".encode("utf-8")
    ).hexdigest()


def _workspace_adoption_successor_id(*, task_id: str, adoption_id: str) -> str:
    return f"{task_id}-adopt-{adoption_id[:12]}"


def _workspace_adoption_start_event_id(successor_attempt_id: str) -> str:
    return hashlib.sha256(
        f"blackdog.worktree.start.adoption/v1\0{successor_attempt_id}".encode("utf-8")
    ).hexdigest()


def _workspace_adoption_complete_event_id(successor_attempt_id: str) -> str:
    return hashlib.sha256(
        f"blackdog.worktree.adoption.complete/v1\0{successor_attempt_id}".encode("utf-8")
    ).hexdigest()


def _workspace_adoption_land_event_id(successor_attempt_id: str) -> str:
    return hashlib.sha256(
        f"blackdog.worktree.land.adoption/v1\0{successor_attempt_id}".encode("utf-8")
    ).hexdigest()


def _workspace_adoption_completion_intent_event_id(
    successor_attempt_id: str,
) -> str:
    return hashlib.sha256(
        (
            "blackdog.worktree.adoption.completion.intent/v1\0"
            + successor_attempt_id
        ).encode("utf-8")
    ).hexdigest()


def _git_commit_relation(
    primary_root: Path,
    *,
    source_commit: str,
    target_commit: str,
) -> str:
    if source_commit == target_commit:
        return "equal"

    def ancestor(left: str, right: str) -> bool:
        completed = _run_git_no_check(
            primary_root,
            "merge-base",
            "--is-ancestor",
            left,
            right,
        )
        if completed.returncode == 0:
            return True
        if completed.returncode == 1:
            return False
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"could not inspect adopted workspace ancestry: {detail}")

    if ancestor(target_commit, source_commit):
        return "ahead"
    if ancestor(source_commit, target_commit):
        return "behind"
    return "diverged"


_WORKSPACE_ADOPTION_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "adoption_id",
        "successor_attempt_id",
        "predecessor_attempt_id",
        "predecessor_status",
        "abort_transaction_id",
        "canonical_candidate",
        "source_commit",
        "source_tree_hash",
        "branch",
        "worktree_path",
        "target_branch",
        "target_commit_at_adoption",
        "relation_at_adoption",
        "actor",
        "execution_prompt_hash",
        "execution_prompt_source",
        "execution_prompt_mode",
        "request_prompt_hash",
        "request_prompt_source",
        "request_prompt_mode",
        "predecessor_setup_receipt_hash",
    }
)
_WORKSPACE_ADOPTION_ATOMIC_START_KEYS = _ATOMIC_START_RECEIPT_KEYS


def _workspace_adoption_receipt(attempt: Any) -> dict[str, Any] | None:
    setup = attempt.setup_receipt
    value = setup.get("workspace_adoption") if isinstance(setup, Mapping) else None
    if not isinstance(value, Mapping) or set(value) != _WORKSPACE_ADOPTION_RECEIPT_KEYS:
        return None
    receipt = dict(value)
    if (
        receipt.get("schema_version") != WORKSPACE_ADOPTION_SCHEMA_VERSION
        or receipt.get("successor_attempt_id") != attempt.attempt_id
        or receipt.get("actor") != attempt.actor
        or receipt.get("branch") != attempt.branch
        or receipt.get("worktree_path") != attempt.worktree_path
        or receipt.get("target_branch") != attempt.target_branch
        or receipt.get("target_commit_at_adoption") != attempt.start_commit
        or receipt.get("relation_at_adoption") not in {"equal", "ahead", "behind", "diverged"}
    ):
        return None
    atomic_start = setup.get("atomic_start") if isinstance(setup, Mapping) else None
    if (
        not isinstance(atomic_start, Mapping)
        or set(atomic_start) != _WORKSPACE_ADOPTION_ATOMIC_START_KEYS
        or atomic_start.get("schema_version") != 2
        or atomic_start.get("attempt_id") != attempt.attempt_id
        or atomic_start.get("expected_predecessor_attempt_id")
        != receipt.get("predecessor_attempt_id")
        or atomic_start.get("start_kind") != "adoption"
        or atomic_start.get("expected_task_actor") != receipt.get("actor")
        or atomic_start.get("expected_execution_prompt_hash")
        != receipt.get("execution_prompt_hash")
        or atomic_start.get("expected_execution_prompt_mode")
        != receipt.get("execution_prompt_mode")
        or atomic_start.get("expected_request_prompt_hash")
        != receipt.get("request_prompt_hash")
        or atomic_start.get("expected_request_prompt_mode")
        != receipt.get("request_prompt_mode")
        or not isinstance(atomic_start.get("expected_task_updated_at"), str)
        or not str(atomic_start["expected_task_updated_at"]).strip()
        or type(atomic_start.get("workset_claim_created")) is not bool
    ):
        return None
    for key in (
        "adoption_id",
        "predecessor_attempt_id",
        "abort_transaction_id",
        "canonical_candidate",
        "source_commit",
        "source_tree_hash",
        "branch",
        "worktree_path",
        "target_branch",
        "target_commit_at_adoption",
        "actor",
        "execution_prompt_hash",
        "execution_prompt_source",
        "execution_prompt_mode",
        "request_prompt_hash",
        "request_prompt_source",
        "request_prompt_mode",
        "predecessor_setup_receipt_hash",
    ):
        if not isinstance(receipt.get(key), str) or not str(receipt[key]).strip():
            return None
    return receipt


def _derive_workspace_adoption_receipt(
    *,
    predecessor: Any,
    transaction: LandingTransaction,
    target_commit_at_adoption: str,
) -> dict[str, Any]:
    if (
        transaction.outcome != "abort_complete"
        or transaction.abort_data is None
        or predecessor.attempt_id != transaction.intent.attempt_id
        or predecessor.task_id != transaction.intent.task_id
        or predecessor.prompt_receipt is None
        or predecessor.user_prompt_receipt is None
        or predecessor.setup_receipt is None
    ):
        raise LandingTransactionError(
            "workspace adoption receipt cannot be derived from incomplete predecessor evidence"
        )
    source_commit = transaction.abort_data.get("source_commit")
    candidate = transaction.abort_data.get("landed_commit")
    if (
        not isinstance(source_commit, str)
        or not source_commit.strip()
        or not isinstance(candidate, str)
        or not candidate.strip()
    ):
        raise LandingTransactionError(
            "workspace adoption receipt requires exact source and candidate commits"
        )
    adoption_id = _workspace_adoption_id(transaction.transaction_id)
    receipt = {
        "schema_version": WORKSPACE_ADOPTION_SCHEMA_VERSION,
        "adoption_id": adoption_id,
        "successor_attempt_id": _workspace_adoption_successor_id(
            task_id=transaction.intent.task_id,
            adoption_id=adoption_id,
        ),
        "predecessor_attempt_id": predecessor.attempt_id,
        "predecessor_status": predecessor.status,
        "abort_transaction_id": transaction.transaction_id,
        "canonical_candidate": candidate,
        "source_commit": source_commit,
        "source_tree_hash": transaction.intent.expected_source_tree_hash,
        "branch": transaction.intent.branch,
        "worktree_path": transaction.intent.worktree_path,
        "target_branch": transaction.intent.target_branch,
        "target_commit_at_adoption": target_commit_at_adoption,
        "relation_at_adoption": _git_commit_relation(
            Path(transaction.intent.primary_worktree),
            source_commit=source_commit,
            target_commit=target_commit_at_adoption,
        ),
        "actor": predecessor.actor,
        "execution_prompt_hash": predecessor.prompt_receipt.prompt_hash,
        "execution_prompt_source": predecessor.prompt_receipt.source,
        "execution_prompt_mode": predecessor.prompt_receipt.mode,
        "request_prompt_hash": predecessor.user_prompt_receipt.prompt_hash,
        "request_prompt_source": predecessor.user_prompt_receipt.source,
        "request_prompt_mode": predecessor.user_prompt_receipt.mode,
        "predecessor_setup_receipt_hash": _canonical_evidence_hash(
            predecessor.setup_receipt
        ),
    }
    if (
        set(receipt) != _WORKSPACE_ADOPTION_RECEIPT_KEYS
        or receipt["relation_at_adoption"] not in {"equal", "ahead", "behind", "diverged"}
        or any(
            not isinstance(receipt.get(key), str) or not str(receipt[key]).strip()
            for key in _WORKSPACE_ADOPTION_RECEIPT_KEYS - {"schema_version"}
        )
    ):
        raise LandingTransactionError(
            "workspace adoption receipt contains incomplete immutable evidence"
        )
    return receipt


def _task_attempts_in_append_order(
    runtime_state: Any,
    *,
    workset_id: str,
    task_id: str,
) -> tuple[Any, ...]:
    return tuple(
        attempt
        for runtime_workset in runtime_state.worksets
        if runtime_workset.workset_id == workset_id
        for attempt in runtime_workset.attempts
        if attempt.task_id == task_id
    )


def _prove_aborted_landing_source_adoption(
    profile: RepoProfile,
    *,
    predecessor: Any,
    transaction: LandingTransaction,
    runtime_state: Any,
    expected: Mapping[str, str] | None = None,
    allow_canceled: bool = False,
) -> dict[str, Any]:
    intent = transaction.intent
    if transaction.outcome != "abort_complete" or not transaction.abort_complete:
        raise LandingTransactionError(
            "workspace adoption requires an abort_complete native landing transaction"
        )
    if predecessor.attempt_id != intent.attempt_id or predecessor.task_id != intent.task_id:
        raise LandingTransactionError("workspace adoption predecessor conflicts with landing intent")
    attempts = _task_attempts_in_append_order(
        runtime_state,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
    )
    if not attempts or attempts[-1].attempt_id != predecessor.attempt_id:
        raise BacklogError("workspace adoption predecessor is not the latest appended same-task attempt")
    if (
        predecessor.status
        not in {ATTEMPT_STATUS_BLOCKED, ATTEMPT_STATUS_FAILED, ATTEMPT_STATUS_ABANDONED}
        or predecessor.ended_at is None
    ):
        raise BacklogError("workspace adoption predecessor is not terminal failed, blocked, or abandoned")
    if active_task_attempt(runtime_state, intent.workset_id, intent.task_id) is not None:
        raise BacklogError("workspace adoption cannot start while another attempt is active")
    if task_claim_index(runtime_state, intent.workset_id).get(intent.task_id) is not None:
        raise BacklogError("workspace adoption cannot start while the task has a claim")
    task_record = task_state_index(runtime_state, intent.workset_id).get(intent.task_id)
    task_status = task_record.status if task_record is not None else TASK_STATUS_PLANNED
    allowed_statuses = {TASK_STATUS_BLOCKED, TASK_STATUS_PLANNED}
    if allow_canceled:
        allowed_statuses.add(TASK_STATUS_CANCELED)
    if task_status not in allowed_statuses:
        raise BacklogError(f"workspace adoption task status {task_status!r} is not restartable")
    if predecessor.prompt_receipt is None or predecessor.user_prompt_receipt is None:
        raise BacklogError("workspace adoption predecessor is missing exact prompt lineage")
    if predecessor.setup_receipt is None:
        raise BacklogError("workspace adoption predecessor is missing handler setup evidence")

    source_commit = _verify_landing_abort_chain(
        profile,
        transaction=transaction,
        require_source=True,
    )
    abort_data = transaction.abort_data
    assert abort_data is not None
    candidate = abort_data.get("landed_commit")
    if not isinstance(candidate, str) or not candidate.strip():
        raise LandingTransactionError("workspace adoption requires a recorded canonical candidate")
    primary_root = Path(intent.primary_worktree)
    worktree_path = Path(intent.worktree_path)
    branch = _inspect_branch_ref(primary_root, intent.branch, role="task_branch")
    target = _inspect_branch_ref(primary_root, intent.target_branch, role="target_branch")
    if branch.state == "error":
        raise _inspection_error(branch)
    if target.state == "error":
        raise _inspection_error(target)
    target_commit = target.resolved_commit
    if branch.resolved_commit != source_commit or target_commit is None:
        raise WorktreeError("workspace adoption branch or target proof is missing")
    registration = _registered_worktree_row(primary_root, worktree_path)
    if (
        registration is None
        or registration.get("branch") != f"refs/heads/{intent.branch}"
        or str(registration.get("HEAD") or "").strip() != source_commit
        or not worktree_path.exists()
        or not _is_git_worktree_path(worktree_path)
        or _run_git(worktree_path, "rev-parse", "HEAD") != source_commit
    ):
        raise WorktreeError("workspace adoption source path, ref, registration, and HEAD are not exact")
    if _managed_status_dirty(profile, worktree_path):
        raise WorktreeError("workspace adoption source is not managed-clean")
    operation = _in_progress_git_operation(worktree_path)
    if operation is not None:
        raise WorktreeError(f"workspace adoption source has an in-progress Git operation ({operation})")
    _committed_manifest, committed_tree_hash = _committed_tree_manifest(primary_root, source_commit)
    _projected_manifest, projected_tree_hash = _projected_source_tree_manifest(worktree_path)
    if (
        committed_tree_hash != intent.expected_source_tree_hash
        or projected_tree_hash != intent.expected_source_tree_hash
    ):
        raise WorktreeError("workspace adoption source tree does not match durable abort evidence")
    if Path(intent.temporary_worktree_path).exists() or _registered_worktree_row(
        primary_root,
        Path(intent.temporary_worktree_path),
    ) is not None:
        raise WorktreeError("workspace adoption temporary landing state is not fully cleaned")
    if _target_contains_landed_commit(
        primary_root,
        target_commit=target_commit,
        landed_commit=candidate,
    ):
        raise _WorkspaceAdoptionTargetChanged(candidate_contained=True)
    if any(
        event.get("type") == "task.landing.reconciled"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("workset_id") == intent.workset_id
        and event["payload"].get("task_id") == intent.task_id
        and event["payload"].get("attempt_id") == predecessor.attempt_id
        for event in load_events(profile.paths.events_file)
    ):
        raise LandingTransactionError("workspace adoption predecessor is already reconciled")

    proof = _derive_workspace_adoption_receipt(
        predecessor=predecessor,
        transaction=transaction,
        target_commit_at_adoption=target_commit,
    )
    if expected is not None:
        expected_projection = {
            "predecessor_attempt_id": proof["predecessor_attempt_id"],
            "abort_transaction_id": proof["abort_transaction_id"],
            "source_commit": proof["source_commit"],
            "source_tree_hash": proof["source_tree_hash"],
            "branch": proof["branch"],
            "worktree_path": proof["worktree_path"],
            "target_branch": proof["target_branch"],
            "target_commit_at_adoption": proof["target_commit_at_adoption"],
        }
        if not strict_json_equal(dict(expected), expected_projection):
            expected_without_target = dict(expected)
            observed_without_target = dict(expected_projection)
            expected_without_target.pop("target_commit_at_adoption", None)
            observed_without_target.pop("target_commit_at_adoption", None)
            if strict_json_equal(expected_without_target, observed_without_target):
                raise _WorkspaceAdoptionTargetChanged(candidate_contained=False)
            raise BacklogError(
                "workspace adoption expected proof is stale or conflicts with durable state"
            )
    return proof


def _workspace_adoption_begin_argv(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    primary_worktree: str,
    proof: Mapping[str, Any],
    resume_lineage: Mapping[str, Any],
) -> list[str]:
    if resume_lineage.get("status") != "verified":
        raise BacklogError("workspace adoption prompt lineage is not replayable")
    execution_file = str(resume_lineage.get("execution_prompt_file") or "").strip()
    if not execution_file:
        raise BacklogError("workspace adoption execution prompt file is unavailable")
    argv = [
        _lifecycle_blackdog_executable(
            profile,
            {"worktree_path": str(proof["worktree_path"])},
        ),
        "task",
        "begin",
        f"--project-root={primary_worktree}",
        f"--workset={workset_id}",
        f"--task={task_id}",
        f"--actor={proof['actor']}",
        f"--execution-prompt-file={execution_file}",
        f"--prompt-mode={proof['execution_prompt_mode']}",
    ]
    if resume_lineage.get("request_distinct"):
        request_file = str(resume_lineage.get("request_file") or "").strip()
        if not request_file:
            raise BacklogError("workspace adoption request prompt file is unavailable")
        argv.append(f"--request-file={request_file}")
    argv.extend(
        [
            f"--expected-actor={proof['actor']}",
            f"--expected-execution-prompt-hash={proof['execution_prompt_hash']}",
            f"--expected-execution-prompt-mode={proof['execution_prompt_mode']}",
            f"--expected-request-prompt-hash={proof['request_prompt_hash']}",
            f"--expected-request-prompt-mode={proof['request_prompt_mode']}",
            "--adopt-aborted-landing-source",
            f"--expected-predecessor-attempt={proof['predecessor_attempt_id']}",
            f"--expected-landing-transaction={proof['abort_transaction_id']}",
            f"--expected-source-commit={proof['source_commit']}",
            f"--expected-source-tree={proof['source_tree_hash']}",
            f"--expected-branch={proof['branch']}",
            f"--expected-path={proof['worktree_path']}",
            f"--expected-target-branch={proof['target_branch']}",
            f"--expected-target-commit={proof['target_commit_at_adoption']}",
        ]
    )
    return argv


def _prompt_receipt_identity(receipt: Any) -> tuple[Any, Any, Any] | None:
    """Return the durable prompt-lineage identity, excluding replay storage."""
    if receipt is None:
        return None
    return (receipt.prompt_hash, receipt.source, receipt.mode)


def _recorded_prompt_artifact_issue(
    profile: RepoProfile,
    *,
    owner: str,
    role: str,
    receipt: Any,
) -> str | None:
    """Verify an additive artifact reference without requiring one on legacy rows."""
    if receipt is None:
        return None
    replay_artifact_path = str(receipt.replay_artifact_path or "").strip()
    if not replay_artifact_path:
        return None
    try:
        verify_prompt_artifact(
            profile.paths.control_dir,
            prompt_hash=receipt.prompt_hash,
            replay_artifact_path=replay_artifact_path,
        )
    except PromptArtifactError as exc:
        return (
            f"{owner} {role} prompt replay artifact cannot be verified "
            f"({exc.code}): {exc}"
        )
    return None


def _workspace_adoption_successor_contract_issue(
    profile: RepoProfile,
    *,
    successor: Any,
    predecessor: Any,
    receipt: Mapping[str, Any],
) -> str | None:
    expected = {
        "attempt_id": receipt["successor_attempt_id"],
        "actor": predecessor.actor,
        "workspace_identity": predecessor.workspace_identity,
        "workspace_mode": WORKSPACE_MODE_GIT_WORKTREE,
        "worktree_role": WORKTREE_ROLE_TASK,
        "worktree_path": receipt["worktree_path"],
        "branch": receipt["branch"],
        "target_branch": receipt["target_branch"],
        "integration_branch": predecessor.integration_branch,
        "start_commit": receipt["target_commit_at_adoption"],
        "execution_model": predecessor.execution_model,
        "model": predecessor.model,
        "reasoning_effort": predecessor.reasoning_effort,
        "codex_session": predecessor.codex_session,
        "note": predecessor.note,
    }
    mismatches = [
        field
        for field, expected_value in expected.items()
        if getattr(successor, field) != expected_value
    ]
    if _prompt_receipt_identity(successor.prompt_receipt) != _prompt_receipt_identity(
        predecessor.prompt_receipt
    ):
        mismatches.append("prompt_receipt")
    if _prompt_receipt_identity(
        successor.user_prompt_receipt
    ) != _prompt_receipt_identity(predecessor.user_prompt_receipt):
        mismatches.append("user_prompt_receipt")
    atomic = (
        successor.setup_receipt.get("atomic_start")
        if isinstance(successor.setup_receipt, Mapping)
        else None
    )
    if (
        not isinstance(atomic, Mapping)
        or atomic.get("expected_task_updated_at") != predecessor.ended_at
    ):
        mismatches.append("atomic_start.expected_task_updated_at")
    if mismatches:
        return (
            "adopted successor fields conflict with predecessor lineage: "
            + ", ".join(mismatches)
        )
    for owner, role, prompt_receipt in (
        ("predecessor", "execution", predecessor.prompt_receipt),
        ("predecessor", "request", predecessor.user_prompt_receipt),
        ("successor", "execution", successor.prompt_receipt),
        ("successor", "request", successor.user_prompt_receipt),
    ):
        issue = _recorded_prompt_artifact_issue(
            profile,
            owner=owner,
            role=role,
            receipt=prompt_receipt,
        )
        if issue is not None:
            return issue
    return None


def _workspace_adoption_handler_issue(
    *,
    successor: Any,
    handlers: HandlerPlanSummary,
) -> str | None:
    setup = successor.setup_receipt
    if not isinstance(setup, Mapping):
        return "adopted successor is missing its setup receipt"
    expected_summary = {
        "workspace_ve": handlers.worktree_ve_path,
        "workspace_blackdog_path": handlers.blackdog_path,
        "runtime_mode": handlers.runtime_mode,
        "source_mode": handlers.source_mode,
        "script_policy": handlers.script_policy,
    }
    mismatches = [
        key for key, value in expected_summary.items() if setup.get(key) != value
    ]
    probes = setup.get("probes")
    if not isinstance(probes, list):
        mismatches.append("probes")
    else:
        probe_index = {
            row.get("name"): row
            for row in probes
            if isinstance(row, Mapping) and isinstance(row.get("name"), str)
        }
        for action in handlers.actions:
            name = f"{action.handler_id}.{action.action}"
            expected_probe = {
                "name": name,
                "status": (
                    "ok"
                    if action.status
                    in {"validated", "created", "preserved", "skipped"}
                    else "blocked"
                ),
                "handler_id": action.handler_id,
                "kind": action.kind,
                "action": action.action,
                "target_path": action.target_path,
                "required": True,
                "message": action.message,
                "elapsed_ms": action.elapsed_ms,
            }
            if not strict_json_equal(probe_index.get(name), expected_probe):
                mismatches.append(name)
    return (
        "retained handler proof conflicts with durable successor setup: "
        + ", ".join(mismatches)
        if mismatches
        else None
    )


def _workspace_adoption_handlers_from_setup(
    profile: RepoProfile,
    successor: Any,
) -> HandlerPlanSummary:
    setup = successor.setup_receipt
    if (
        not isinstance(setup, Mapping)
        or setup.get("schema_version")
        not in {LEGACY_SETUP_RECEIPT_SCHEMA_VERSION, SETUP_RECEIPT_SCHEMA_VERSION}
        or setup.get("status") != "ok"
        or setup.get("blockers") != []
        or not isinstance(setup.get("probes"), list)
    ):
        raise LandingTransactionError(
            "adopted successor setup receipt cannot reconstruct handler evidence"
        )
    actions = []
    for probe in setup["probes"]:
        if (
            not isinstance(probe, Mapping)
            or not isinstance(probe.get("handler_id"), str)
            or not isinstance(probe.get("kind"), str)
            or not isinstance(probe.get("action"), str)
        ):
            continue
        actions.append(
            HandlerAction(
                handler_id=str(probe["handler_id"]),
                kind=str(probe["kind"]),
                action=str(probe["action"]),
                target_path=(
                    str(probe["target_path"])
                    if probe.get("target_path") is not None
                    else None
                ),
                status=(
                    HANDLER_STATUS_VALIDATED
                    if probe.get("status") == "ok"
                    else HANDLER_STATUS_BLOCKED
                ),
                message=str(probe.get("message") or ""),
                elapsed_ms=(
                    int(probe["elapsed_ms"])
                    if type(probe.get("elapsed_ms")) is int
                    else None
                ),
            )
        )
    if not actions and any(handler.enabled for handler in profile.handlers):
        raise LandingTransactionError(
            "adopted successor setup receipt has no handler evidence"
        )
    return HandlerPlanSummary(
        ready=setup.get("status") == "ok",
        actions=tuple(actions),
        remediation=None,
        worktree_ve_path=(
            str(setup["workspace_ve"])
            if setup.get("workspace_ve") is not None
            else None
        ),
        blackdog_path=(
            str(setup["workspace_blackdog_path"])
            if setup.get("workspace_blackdog_path") is not None
            else None
        ),
        runtime_mode=(
            str(setup["runtime_mode"])
            if setup.get("runtime_mode") is not None
            else None
        ),
        source_mode=(
            str(setup["source_mode"])
            if setup.get("source_mode") is not None
            else None
        ),
        script_policy=(
            str(setup["script_policy"])
            if setup.get("script_policy") is not None
            else None
        ),
    )


def _initial_start_evidence(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: Any,
    successor: Any,
) -> tuple[str | None, str | None]:
    setup = successor.setup_receipt
    if not isinstance(setup, Mapping) or "worktree_start" not in setup:
        return None, None
    if isinstance(setup.get("atomic_start"), Mapping):
        return None, None
    attempts = _task_attempts_in_append_order(
        runtime_state,
        workset_id=workset_id,
        task_id=task_id,
    )
    if (
        len(attempts) != 1
        or attempts[0].attempt_id != successor.attempt_id
        or successor.status != ATTEMPT_STATUS_IN_PROGRESS
        or successor.ended_at is not None
        or successor.prompt_receipt is None
        or successor.user_prompt_receipt is None
    ):
        return "conflict", "initial task start durable identity is malformed"
    task_record = task_state_index(runtime_state, workset_id).get(task_id)
    task_claim = task_claim_index(runtime_state, workset_id).get(task_id)
    workset_claim_record = workset_claim(runtime_state, workset_id)
    if (
        task_record is None
        or task_record.status != TASK_STATUS_IN_PROGRESS
        or task_record.updated_at != successor.started_at
        or task_record.actor != successor.actor
        or task_record.note != successor.note
        or task_claim is None
        or task_claim.attempt_id != successor.attempt_id
        or task_claim.actor != successor.actor
        or task_claim.execution_model != successor.execution_model
        or task_claim.claimed_at != successor.started_at
        or task_claim.note != successor.note
        or workset_claim_record is None
        or workset_claim_record.actor != successor.actor
        or workset_claim_record.execution_model != successor.execution_model
    ):
        return "conflict", "initial task start runtime claims are not canonical"
    try:
        handlers = _durable_start_handlers(profile, successor)
        start_receipt = _durable_worktree_start_receipt(successor)
    except WorktreeError as exc:
        return "conflict", str(exc)
    spec = WorktreeSpec(
        workset_id=workset_id,
        task_id=task_id,
        task_title="",
        task_slug="",
        branch=str(successor.branch or ""),
        base_ref=str(start_receipt["base_ref"]),
        base_commit=str(start_receipt["base_commit"]),
        target_branch=str(successor.target_branch or ""),
        worktree_path=str(successor.worktree_path or ""),
        primary_worktree=str(start_receipt["primary_worktree"]),
        current_worktree=str(start_receipt["primary_worktree"]),
        attempt_id=successor.attempt_id,
        prompt_hash=successor.prompt_receipt.prompt_hash,
        prompt_source=successor.prompt_receipt.source,
        prompt_mode=successor.prompt_receipt.mode,
        workspace_ve=handlers.worktree_ve_path,
        workspace_blackdog_path=handlers.blackdog_path,
        runtime_mode=handlers.runtime_mode,
        source_root=handlers.source_root,
        source_mode=handlers.source_mode,
        script_policy=handlers.script_policy,
        setup_receipt=dict(successor.setup_receipt or {}),
        handlers=handlers,
    )
    workset_claim_created = (
        workset_claim_record.claimed_at == successor.started_at
        and workset_claim_record.note == successor.note
    )
    expected_events: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for contract in task_start_event_contracts(
        workset_id=workset_id,
        task_id=task_id,
        attempt=successor,
        workset_claim_record=workset_claim_record,
        workset_claim_created=workset_claim_created,
        deterministic=True,
    ):
        expected_events[str(contract["event_id"])] = (
            str(contract["event_type"]),
            str(contract["actor"]),
            dict(contract["payload"]),
        )
    expected_events[_initial_start_event_id(successor.attempt_id)] = (
        "worktree.start",
        successor.actor,
        _worktree_start_event_payload(
            spec=spec,
            attempt=successor,
            handlers=handlers,
        ),
    )
    events = load_events(profile.paths.events_file)
    missing = False
    for event_id, (event_type, event_actor, event_payload) in expected_events.items():
        matches = [event for event in events if event.get("event_id") == event_id]
        if not matches:
            missing = True
            continue
        if len(matches) != 1 or (
            matches[0].get("type") != event_type
            or matches[0].get("actor") != event_actor
            or not strict_json_equal(matches[0].get("payload"), event_payload)
        ):
            return "conflict", f"initial task start event {event_type} is conflicting"
    return (
        ("missing", "deterministic initial task-start evidence is incomplete")
        if missing
        else ("complete", None)
    )


def _ordinary_resume_start_evidence(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: Any,
    successor: Any,
) -> tuple[str | None, str | None]:
    """Classify deterministic ordinary-resume evidence as complete/missing/conflict."""

    setup = successor.setup_receipt
    atomic = setup.get("atomic_start") if isinstance(setup, Mapping) else None
    if not isinstance(atomic, Mapping) or atomic.get("start_kind") != "resume":
        return None, None
    if (
        set(atomic) != _ATOMIC_START_RECEIPT_KEYS
        or atomic.get("schema_version") != 2
        or atomic.get("attempt_id") != successor.attempt_id
        or type(atomic.get("workset_claim_created")) is not bool
        or successor.status != ATTEMPT_STATUS_IN_PROGRESS
        or successor.ended_at is not None
        or successor.prompt_receipt is None
        or successor.user_prompt_receipt is None
    ):
        return "conflict", "ordinary resume atomic-start receipt is malformed"
    predecessor_id = str(atomic.get("expected_predecessor_attempt_id") or "").strip()
    attempts = _task_attempts_in_append_order(
        runtime_state,
        workset_id=workset_id,
        task_id=task_id,
    )
    if (
        len(attempts) < 2
        or attempts[-1].attempt_id != successor.attempt_id
        or attempts[-2].attempt_id != predecessor_id
        or attempts[-2].status == ATTEMPT_STATUS_IN_PROGRESS
        or attempts[-2].ended_at is None
    ):
        return "conflict", "ordinary resume successor is not after its terminal predecessor"
    predecessor = attempts[-2]
    if predecessor.prompt_receipt is None or predecessor.user_prompt_receipt is None:
        return "conflict", "ordinary resume predecessor is missing prompt lineage"
    try:
        expected_actor, expected_generation = _expected_resume_task_identity(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            predecessor=predecessor,
        )
    except BacklogError as exc:
        return "conflict", str(exc)
    expected_identity = task_resume_attempt_id(
        workset_id=workset_id,
        task_id=task_id,
        predecessor_attempt_id=predecessor_id,
        actor=expected_actor,
        execution_prompt_hash=predecessor.prompt_receipt.prompt_hash,
        execution_prompt_mode=str(predecessor.prompt_receipt.mode),
        request_prompt_hash=predecessor.user_prompt_receipt.prompt_hash,
        request_prompt_mode=str(predecessor.user_prompt_receipt.mode),
    )
    guarded = (
        atomic.get("expected_task_actor"),
        atomic.get("expected_execution_prompt_hash"),
        atomic.get("expected_execution_prompt_mode"),
        atomic.get("expected_request_prompt_hash"),
        atomic.get("expected_request_prompt_mode"),
    )
    durable = (
        expected_actor,
        predecessor.prompt_receipt.prompt_hash,
        predecessor.prompt_receipt.mode,
        predecessor.user_prompt_receipt.prompt_hash,
        predecessor.user_prompt_receipt.mode,
    )
    successor_lineage = (
        successor.actor,
        successor.prompt_receipt.prompt_hash,
        successor.prompt_receipt.mode,
        successor.user_prompt_receipt.prompt_hash,
        successor.user_prompt_receipt.mode,
    )
    if (
        successor.attempt_id != expected_identity
        or guarded != durable
        or successor_lineage != durable
        or atomic.get("expected_task_updated_at") != expected_generation
    ):
        return "conflict", "ordinary resume deterministic identity or prompt lineage conflicts"
    task_record = task_state_index(runtime_state, workset_id).get(task_id)
    task_claim = task_claim_index(runtime_state, workset_id).get(task_id)
    workset_claim_record = workset_claim(runtime_state, workset_id)
    if (
        task_record is None
        or task_record.status != TASK_STATUS_IN_PROGRESS
        or task_record.updated_at != successor.started_at
        or task_record.actor != successor.actor
        or task_record.note != successor.note
        or task_claim is None
        or task_claim.attempt_id != successor.attempt_id
        or task_claim.actor != successor.actor
        or task_claim.execution_model != successor.execution_model
        or task_claim.claimed_at != successor.started_at
        or task_claim.note != successor.note
        or workset_claim_record is None
        or workset_claim_record.actor != successor.actor
        or workset_claim_record.execution_model != successor.execution_model
        or (
            atomic.get("workset_claim_created") is True
            and (
                workset_claim_record.claimed_at != successor.started_at
                or workset_claim_record.note != successor.note
            )
        )
    ):
        return "conflict", "ordinary resume runtime claims are not canonical"
    workset_claim_created = (
        workset_claim_record.claimed_at == successor.started_at
        and workset_claim_record.note == successor.note
    )
    if atomic.get("workset_claim_created") is not workset_claim_created:
        return "conflict", "ordinary resume workset-claim ownership conflicts"
    try:
        handlers = _durable_start_handlers(profile, successor)
        start_receipt = _durable_worktree_start_receipt(successor)
    except WorktreeError as exc:
        return "conflict", str(exc)
    spec = WorktreeSpec(
        workset_id=workset_id,
        task_id=task_id,
        task_title="",
        task_slug="",
        branch=str(successor.branch or ""),
        base_ref=str(start_receipt["base_ref"]),
        base_commit=str(start_receipt["base_commit"]),
        target_branch=str(successor.target_branch or ""),
        worktree_path=str(successor.worktree_path or ""),
        primary_worktree=str(start_receipt["primary_worktree"]),
        current_worktree=str(start_receipt["primary_worktree"]),
        attempt_id=successor.attempt_id,
        prompt_hash=successor.prompt_receipt.prompt_hash,
        prompt_source=successor.prompt_receipt.source,
        prompt_mode=successor.prompt_receipt.mode,
        workspace_ve=handlers.worktree_ve_path,
        workspace_blackdog_path=handlers.blackdog_path,
        runtime_mode=handlers.runtime_mode,
        source_root=handlers.source_root,
        source_mode=handlers.source_mode,
        script_policy=handlers.script_policy,
        setup_receipt=dict(successor.setup_receipt or {}),
        handlers=handlers,
        workspace_action="repaired",
        predecessor_attempt_id=predecessor_id,
    )
    expected_events: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for contract in task_start_event_contracts(
        workset_id=workset_id,
        task_id=task_id,
        attempt=successor,
        workset_claim_record=workset_claim_record,
        workset_claim_created=workset_claim_created,
        deterministic=True,
    ):
        expected_events[str(contract["event_id"])] = (
            str(contract["event_type"]),
            str(contract["actor"]),
            dict(contract["payload"]),
        )
    expected_events[_ordinary_resume_start_event_id(successor.attempt_id)] = (
        "worktree.start",
        successor.actor,
        _worktree_start_event_payload(
            spec=spec,
            attempt=successor,
            handlers=handlers,
        ),
    )
    events = load_events(profile.paths.events_file)
    if not workset_claim_created:
        unexpected_id = task_start_event_id(
            attempt_id=successor.attempt_id,
            event_type="workset.claim",
        )
        if any(event.get("event_id") == unexpected_id for event in events):
            return "conflict", "ordinary resume has unexpected workset.claim evidence"
    missing = False
    for event_id, (event_type, event_actor, event_payload) in expected_events.items():
        matches = [event for event in events if event.get("event_id") == event_id]
        if not matches:
            missing = True
            continue
        if len(matches) != 1 or (
            matches[0].get("type") != event_type
            or matches[0].get("actor") != event_actor
            or not strict_json_equal(matches[0].get("payload"), event_payload)
        ):
            return "conflict", f"ordinary resume start event {event_type} is conflicting"
    return (
        ("missing", "deterministic ordinary-resume start evidence is incomplete")
        if missing
        else ("complete", None)
    )


def _task_start_repair_evidence(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: Any,
    successor: Any,
) -> tuple[str | None, str | None]:
    state, issue = _ordinary_resume_start_evidence(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        runtime_state=runtime_state,
        successor=successor,
    )
    if state is not None:
        return state, issue
    return _initial_start_evidence(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        runtime_state=runtime_state,
        successor=successor,
    )


def _require_ordinary_resume_start_evidence(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: Any,
    attempt: Any,
) -> None:
    state, issue = _task_start_repair_evidence(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        runtime_state=runtime_state,
        successor=attempt,
    )
    if state in {None, "complete"}:
        return
    raise BacklogError(
        "task start evidence requires exact task begin repair before terminal mutation: "
        + (issue or "deterministic start evidence is incomplete")
    )


def _workspace_adoption_start_evidence(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: Any,
    successor: Any,
    predecessor: Any,
    receipt: Mapping[str, Any],
    transaction: LandingTransaction,
) -> tuple[str, str | None]:
    contract_issue = _workspace_adoption_successor_contract_issue(
        profile,
        successor=successor,
        predecessor=predecessor,
        receipt=receipt,
    )
    if contract_issue is not None:
        return "conflict", contract_issue
    setup = successor.setup_receipt
    assert isinstance(setup, Mapping)
    atomic = setup.get("atomic_start")
    assert isinstance(atomic, Mapping)
    task_claim = task_claim_index(runtime_state, workset_id).get(task_id)
    workset_claim_record = workset_claim(runtime_state, workset_id)
    task_record = task_state_index(runtime_state, workset_id).get(task_id)
    if (
        task_record is None
        or task_record.status != TASK_STATUS_IN_PROGRESS
        or task_record.updated_at != successor.started_at
        or task_record.actor != successor.actor
        or task_record.note != successor.note
        or task_claim is None
        or task_claim.attempt_id != successor.attempt_id
        or task_claim.actor != successor.actor
        or task_claim.execution_model != successor.execution_model
        or task_claim.claimed_at != successor.started_at
        or task_claim.note != successor.note
        or workset_claim_record is None
        or workset_claim_record.actor != successor.actor
        or workset_claim_record.execution_model != successor.execution_model
        or (
            atomic.get("workset_claim_created") is True
            and (
                workset_claim_record.claimed_at != successor.started_at
                or workset_claim_record.note != successor.note
            )
        )
    ):
        return "conflict", "adopted successor runtime claims are not canonical"
    workset_claim_created = (
        workset_claim_record.claimed_at == successor.started_at
        and workset_claim_record.note == successor.note
    )
    if atomic.get("workset_claim_created") is not workset_claim_created:
        return "conflict", "adopted successor workset-claim ownership conflicts"
    handlers = _workspace_adoption_handlers_from_setup(profile, successor)
    expected_events: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for contract in task_start_event_contracts(
        workset_id=workset_id,
        task_id=task_id,
        attempt=successor,
        workset_claim_record=workset_claim_record,
        workset_claim_created=workset_claim_created,
        deterministic=True,
    ):
        expected_events[str(contract["event_id"])] = (
            str(contract["event_type"]),
            str(contract["actor"]),
            dict(contract["payload"]),
        )
    spec = WorktreeSpec(
        workset_id=workset_id,
        task_id=task_id,
        task_title="",
        task_slug="",
        branch=str(receipt["branch"]),
        base_ref=str(receipt["target_branch"]),
        base_commit=str(receipt["target_commit_at_adoption"]),
        target_branch=str(receipt["target_branch"]),
        worktree_path=str(receipt["worktree_path"]),
        primary_worktree=transaction.intent.primary_worktree,
        current_worktree=transaction.intent.primary_worktree,
        attempt_id=successor.attempt_id,
        prompt_hash=successor.prompt_receipt.prompt_hash,
        prompt_source=successor.prompt_receipt.source,
        prompt_mode=successor.prompt_receipt.mode,
        workspace_ve=handlers.worktree_ve_path,
        workspace_blackdog_path=handlers.blackdog_path,
        runtime_mode=handlers.runtime_mode,
        source_root=handlers.source_root,
        source_mode=handlers.source_mode,
        script_policy=handlers.script_policy,
        setup_receipt=dict(successor.setup_receipt),
        handlers=handlers,
        workspace_action="adopted",
        predecessor_attempt_id=predecessor.attempt_id,
    )
    expected_events[_workspace_adoption_start_event_id(successor.attempt_id)] = (
        "worktree.start",
        successor.actor,
        _workspace_adoption_start_event_payload(
            spec=spec,
            attempt=successor,
            proof=receipt,
            handlers=handlers,
        ),
    )
    events = load_events(profile.paths.events_file)
    product_event_id = _workspace_adoption_start_event_id(successor.attempt_id)
    product_matches = [
        event for event in events if event.get("event_id") == product_event_id
    ]
    if not product_matches:
        live_handlers = validate_existing_worktree_handlers(
            profile,
            worktree_path=Path(str(receipt["worktree_path"])),
        )
        if not live_handlers.ready:
            return (
                "conflict",
                live_handlers.remediation or "retained handlers are not ready",
            )
        handler_issue = _workspace_adoption_handler_issue(
            successor=successor,
            handlers=live_handlers,
        )
        if handler_issue is not None:
            return "conflict", handler_issue
    if not workset_claim_created:
        unexpected_workset_event_id = task_start_event_id(
            attempt_id=successor.attempt_id,
            event_type="workset.claim",
        )
        if any(
            event.get("event_id") == unexpected_workset_event_id
            for event in events
        ):
            return "conflict", "unexpected deterministic workset.claim evidence"
    missing = False
    for event_id, (event_type, event_actor, event_payload) in expected_events.items():
        matches = [event for event in events if event.get("event_id") == event_id]
        if not matches:
            missing = True
            continue
        if len(matches) != 1 or (
            matches[0].get("type") != event_type
            or matches[0].get("actor") != event_actor
            or not strict_json_equal(matches[0].get("payload"), event_payload)
        ):
            return "conflict", f"adoption start event {event_type} is conflicting"
    return ("missing", "deterministic adoption start evidence is incomplete") if missing else (
        "complete",
        None,
    )


def _require_workspace_adoption_start_evidence(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: Any,
    attempt: Any,
) -> tuple[Mapping[str, Any], Any, LandingTransaction] | None:
    receipt = _workspace_adoption_receipt(attempt)
    has_marker = bool(
        isinstance(attempt.setup_receipt, Mapping)
        and "workspace_adoption" in attempt.setup_receipt
    )
    if receipt is None:
        if has_marker:
            raise LandingTransactionError(
                "workspace adoption start evidence cannot be verified because its receipt is malformed"
            )
        return None
    predecessor = find_task_attempt(
        runtime_state,
        workset_id,
        str(receipt["predecessor_attempt_id"]),
    )
    if predecessor is None or predecessor.task_id != task_id:
        raise LandingTransactionError(
            "workspace adoption start evidence has no exact predecessor"
        )
    transaction = load_landing_transaction(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=predecessor.attempt_id,
    )
    if (
        transaction is None
        or transaction.transaction_id != receipt["abort_transaction_id"]
        or transaction.outcome != "abort_complete"
    ):
        raise LandingTransactionError(
            "workspace adoption start evidence has no exact abort-complete transaction"
        )
    verified_source = _verify_landing_abort_chain(
        profile,
        transaction=transaction,
        require_source=False,
    )
    derived_receipt = _derive_workspace_adoption_receipt(
        predecessor=predecessor,
        transaction=transaction,
        target_commit_at_adoption=str(receipt["target_commit_at_adoption"]),
    )
    if (
        verified_source != derived_receipt["source_commit"]
        or not strict_json_equal(receipt, derived_receipt)
    ):
        raise LandingTransactionError(
            "workspace adoption start evidence conflicts with immutable predecessor proof"
        )
    start_state, start_issue = _workspace_adoption_start_evidence(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        runtime_state=runtime_state,
        successor=attempt,
        predecessor=predecessor,
        receipt=receipt,
        transaction=transaction,
    )
    if start_state != "complete":
        detail = start_issue or "deterministic adoption start evidence is incomplete"
        raise LandingTransactionError(
            "workspace adoption start evidence requires exact guarded task begin repair before terminal mutation: "
            + detail
        )
    return receipt, predecessor, transaction


def _workspace_adoption_recovery_fields(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: Any,
    task_status: str,
    active_attempt: Any | None,
    latest_attempt: Any | None,
    landing_transaction: LandingTransaction | None,
    resume_lineage: Mapping[str, Any],
    predecessor_reconciliation_candidate: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "workspace_adoption_eligible": False,
        "workspace_adoption_argv": [],
        "workspace_adoption_issue_code": None,
        "workspace_adoption_issue_detail": None,
        "active_workspace_adoption": False,
        "workspace_adoption_relation": None,
        "workspace_adoption_operation": None,
        "workspace_adoption_rebase_argv": [],
        "workspace_adoption_candidate_arrived": False,
        "workspace_adoption_completion_pending": False,
        "workspace_adoption_completion_argv": [],
    }

    if active_attempt is not None:
        receipt = _workspace_adoption_receipt(active_attempt)
        has_adoption_marker = bool(
            isinstance(active_attempt.setup_receipt, Mapping)
            and "workspace_adoption" in active_attempt.setup_receipt
        )
        if receipt is None:
            if has_adoption_marker:
                result.update(
                    workspace_adoption_issue_code="workspace_adoption_receipt_invalid",
                    workspace_adoption_issue_detail=(
                        "The active successor has malformed workspace-adoption evidence."
                    ),
                )
            return result
        result["active_workspace_adoption"] = True
        try:
            predecessor = find_task_attempt(
                runtime_state,
                workset_id,
                str(receipt["predecessor_attempt_id"]),
            )
            if predecessor is None or predecessor.task_id != task_id:
                raise BacklogError("workspace adoption predecessor is missing")
            transaction = load_landing_transaction(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=predecessor.attempt_id,
            )
            if (
                transaction is None
                or transaction.transaction_id != receipt["abort_transaction_id"]
                or transaction.outcome != "abort_complete"
            ):
                raise LandingTransactionError(
                    "active workspace adoption has no exact abort-complete predecessor"
                )
            source_commit = _verify_landing_abort_chain(
                profile,
                transaction=transaction,
                require_source=False,
            )
            derived_receipt = _derive_workspace_adoption_receipt(
                predecessor=predecessor,
                transaction=transaction,
                target_commit_at_adoption=str(receipt["target_commit_at_adoption"]),
            )
            if (
                source_commit != derived_receipt["source_commit"]
                or not strict_json_equal(receipt, derived_receipt)
            ):
                raise LandingTransactionError(
                    "active workspace adoption conflicts with predecessor evidence"
                )
            start_state, start_issue = _workspace_adoption_start_evidence(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                runtime_state=runtime_state,
                successor=active_attempt,
                predecessor=predecessor,
                receipt=receipt,
                transaction=transaction,
            )
            if start_state == "conflict":
                raise LandingTransactionError(
                    start_issue or "workspace adoption start evidence conflicts"
                )
            if start_state == "missing":
                result["workspace_adoption_eligible"] = True
                result["workspace_adoption_argv"] = _workspace_adoption_begin_argv(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    primary_worktree=transaction.intent.primary_worktree,
                    proof=receipt,
                    resume_lineage=resume_lineage,
                )
                return result
            completion_intent = _load_workspace_adoption_completion_intent(
                profile,
                attempt=active_attempt,
                receipt=receipt,
            )
            if completion_intent is not None:
                native_transaction = (
                    load_landing_transaction(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        attempt_id=active_attempt.attempt_id,
                    )
                    if completion_intent["completion_route"] == "successor_landing"
                    else None
                )
                _validate_workspace_adoption_completion_route(
                    profile,
                    attempt=active_attempt,
                    receipt=receipt,
                    payload=completion_intent,
                    predecessor_transaction=transaction,
                    native_transaction=native_transaction,
                )
                executable = _lifecycle_blackdog_executable(
                    profile,
                    {"worktree_path": str(receipt["worktree_path"])},
                )
                if completion_intent["completion_route"] == "successor_landing":
                    assert native_transaction is not None
                    completion_argv = list(
                        native_transaction.intent.task_land_argv(
                            executable=executable,
                            project_root=Path(native_transaction.intent.primary_worktree),
                        )
                    )
                else:
                    completion_argv = [
                        executable,
                        "task",
                        "reconcile-landing",
                        f"--project-root={transaction.intent.primary_worktree}",
                        f"--workset={workset_id}",
                        f"--task={task_id}",
                        f"--attempt={active_attempt.attempt_id}",
                        f"--landed-commit={completion_intent['landed_commit']}",
                        f"--actor={active_attempt.actor}",
                        "--apply",
                    ]
                result.update(
                    workspace_adoption_completion_pending=True,
                    workspace_adoption_completion_argv=completion_argv,
                )
                return result
            source_path = Path(str(receipt["worktree_path"]))
            primary_root = Path(transaction.intent.primary_worktree)
            branch = _inspect_branch_ref(
                primary_root,
                str(receipt["branch"]),
                role="task_branch",
            )
            target = _inspect_branch_ref(
                primary_root,
                str(receipt["target_branch"]),
                role="target_branch",
            )
            if branch.state == "error":
                raise _inspection_error(branch)
            if target.state == "error":
                raise _inspection_error(target)
            registration = _registered_worktree_row(primary_root, source_path)
            source_head = branch.resolved_commit
            target_commit = target.resolved_commit
            if (
                source_head is None
                or target_commit is None
                or registration is None
                or registration.get("branch")
                != f"refs/heads/{receipt['branch']}"
                or str(registration.get("HEAD") or "").strip() != source_head
                or not source_path.exists()
                or not _is_git_worktree_path(source_path)
                or _run_git(source_path, "rev-parse", "HEAD") != source_head
            ):
                raise WorktreeError(
                    "active workspace adoption source registration is not coherent"
                )
            if Path(transaction.intent.temporary_worktree_path).exists() or _registered_worktree_row(
                primary_root,
                Path(transaction.intent.temporary_worktree_path),
            ) is not None:
                raise WorktreeError(
                    "active workspace adoption has unexpected temporary landing state"
                )
            operation = _in_progress_git_operation(source_path)
            relation = _git_commit_relation(
                primary_root,
                source_commit=source_head,
                target_commit=target_commit,
            )
            result["workspace_adoption_relation"] = relation
            result["workspace_adoption_operation"] = operation
            if relation in {"behind", "diverged"}:
                result["workspace_adoption_rebase_argv"] = [
                    "git",
                    "-C",
                    str(source_path),
                    "rebase",
                    str(receipt["target_branch"]),
                ]

            candidate = str(receipt["canonical_candidate"])
            candidate_arrived = _target_contains_landed_commit(
                primary_root,
                target_commit=target_commit,
                landed_commit=candidate,
            )
            if candidate_arrived and operation is None and not _managed_status_dirty(
                profile,
                source_path,
            ):
                _committed_manifest, committed_tree_hash = _committed_tree_manifest(
                    primary_root,
                    source_head,
                )
                _projected_manifest, projected_tree_hash = _projected_source_tree_manifest(
                    source_path
                )
                if (
                    committed_tree_hash == receipt["source_tree_hash"]
                    and projected_tree_hash == receipt["source_tree_hash"]
                ):
                    result["workspace_adoption_candidate_arrived"] = True
                    executable = _lifecycle_blackdog_executable(
                        profile,
                        {"worktree_path": str(source_path)},
                    )
                    result["landing_reconcile_argv"] = [
                        executable,
                        "task",
                        "reconcile-landing",
                        f"--project-root={transaction.intent.primary_worktree}",
                        f"--workset={workset_id}",
                        f"--task={task_id}",
                        f"--attempt={active_attempt.attempt_id}",
                        f"--landed-commit={candidate}",
                        f"--actor={active_attempt.actor}",
                        "--reason=Complete adopted successor after target containment",
                    ]
        except (BacklogError, LandingTransactionError, WorktreeError) as exc:
            result.update(
                workspace_adoption_issue_code="active_workspace_adoption_proof_failed",
                workspace_adoption_issue_detail=str(exc),
            )
        return result

    if latest_attempt is not None and latest_attempt.status == ATTEMPT_STATUS_SUCCESS:
        receipt = _workspace_adoption_receipt(latest_attempt)
        has_adoption_marker = bool(
            isinstance(latest_attempt.setup_receipt, Mapping)
            and "workspace_adoption" in latest_attempt.setup_receipt
        )
        if receipt is None:
            if has_adoption_marker:
                result.update(
                    workspace_adoption_issue_code="workspace_adoption_receipt_invalid",
                    workspace_adoption_issue_detail=(
                        "The terminal successor has malformed workspace-adoption evidence."
                    ),
                )
            return result
        try:
            predecessor = find_task_attempt(
                runtime_state,
                workset_id,
                str(receipt["predecessor_attempt_id"]),
            )
            if predecessor is None or predecessor.task_id != task_id:
                raise LandingTransactionError(
                    "terminal adopted successor predecessor is missing"
                )
            predecessor_transaction = load_landing_transaction(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=predecessor.attempt_id,
            )
            if predecessor_transaction is None:
                raise LandingTransactionError(
                    "terminal adopted successor predecessor transaction is missing"
                )
            verified_source = _verify_landing_abort_chain(
                profile,
                transaction=predecessor_transaction,
                require_source=False,
            )
            derived = _derive_workspace_adoption_receipt(
                predecessor=predecessor,
                transaction=predecessor_transaction,
                target_commit_at_adoption=str(receipt["target_commit_at_adoption"]),
            )
            if (
                verified_source != derived["source_commit"]
                or not strict_json_equal(receipt, derived)
            ):
                raise LandingTransactionError(
                    "terminal adopted successor conflicts with predecessor evidence"
                )
            completion_intent = _load_workspace_adoption_completion_intent(
                profile,
                attempt=latest_attempt,
                receipt=receipt,
            )
            if completion_intent is None:
                raise LandingTransactionError(
                    "terminal adopted successor is missing its completion intent"
                )
            native_transaction = (
                load_landing_transaction(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    attempt_id=latest_attempt.attempt_id,
                )
                if completion_intent["completion_route"] == "successor_landing"
                else None
            )
            _validate_workspace_adoption_completion_route(
                profile,
                attempt=latest_attempt,
                receipt=receipt,
                payload=completion_intent,
                predecessor_transaction=predecessor_transaction,
                native_transaction=native_transaction,
                require_native_land=native_transaction is not None,
            )
            _land_payload, complete_payload = _workspace_adoption_completion_payloads(
                attempt=latest_attempt,
                receipt=receipt,
                completion_intent=completion_intent,
            )
            complete_event_id = _workspace_adoption_complete_event_id(
                latest_attempt.attempt_id
            )
            matches = [
                event
                for event in load_events(profile.paths.events_file)
                if event.get("event_id") == complete_event_id
            ]
            if matches and not _exact_workspace_adoption_event(
                profile,
                event_id=complete_event_id,
                event_type="worktree.adoption.complete",
                actor=latest_attempt.actor,
                payload=complete_payload,
            ):
                raise LandingTransactionError(
                    "terminal adopted successor completion marker conflicts"
                )
            if not matches:
                executable = _lifecycle_blackdog_executable(
                    profile,
                    {"worktree_path": str(receipt["worktree_path"])},
                )
                if native_transaction is not None:
                    completion_argv = list(
                        native_transaction.intent.task_land_argv(
                            executable=executable,
                            project_root=Path(native_transaction.intent.primary_worktree),
                        )
                    )
                else:
                    completion_argv = [
                        executable,
                        "task",
                        "reconcile-landing",
                        f"--project-root={predecessor_transaction.intent.primary_worktree}",
                        f"--workset={workset_id}",
                        f"--task={task_id}",
                        f"--attempt={latest_attempt.attempt_id}",
                        f"--landed-commit={completion_intent['landed_commit']}",
                        f"--actor={latest_attempt.actor}",
                        "--apply",
                    ]
                result.update(
                    workspace_adoption_completion_pending=True,
                    workspace_adoption_completion_argv=completion_argv,
                )
        except (BacklogError, LandingTransactionError, WorktreeError) as exc:
            result.update(
                workspace_adoption_issue_code="terminal_workspace_adoption_proof_failed",
                workspace_adoption_issue_detail=str(exc),
            )
        return result

    if (
        predecessor_reconciliation_candidate
        or latest_attempt is None
        or landing_transaction is None
        or landing_transaction.outcome != "abort_complete"
        or latest_attempt.attempt_id != landing_transaction.intent.attempt_id
    ):
        return result
    try:
        proof = _prove_aborted_landing_source_adoption(
            profile,
            predecessor=latest_attempt,
            transaction=landing_transaction,
            runtime_state=runtime_state,
            allow_canceled=True,
        )
        handlers = validate_existing_worktree_handlers(
            profile,
            worktree_path=Path(str(proof["worktree_path"])),
        )
        if not handlers.ready:
            raise WorktreeError(
                handlers.remediation or "retained workspace handler validation failed"
            )
        if resume_lineage.get("status") != "verified":
            result.update(
                workspace_adoption_issue_code=(
                    str(resume_lineage.get("issue_code") or "").strip()
                    or "workspace_adoption_lineage_unavailable"
                ),
                workspace_adoption_issue_detail=(
                    str(resume_lineage.get("issue_detail") or "").strip()
                    or "Exact predecessor prompt files are not replayable."
                ),
            )
            return result
        result["workspace_adoption_eligible"] = True
        result["workspace_adoption_argv"] = _workspace_adoption_begin_argv(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            primary_worktree=landing_transaction.intent.primary_worktree,
            proof=proof,
            resume_lineage=resume_lineage,
        )
    except (BacklogError, LandingTransactionError, WorktreeError) as exc:
        result.update(
            workspace_adoption_issue_code="workspace_adoption_proof_failed",
            workspace_adoption_issue_detail=str(exc),
        )
    return result


def _ordinary_resume_recovery_fields(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: Any,
    active_attempt: Any | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "resume_start_incomplete": False,
        "resume_start_issue_code": None,
        "resume_start_issue_detail": None,
    }
    if active_attempt is None:
        return result
    try:
        state, issue = _task_start_repair_evidence(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
            successor=active_attempt,
        )
    except (BacklogError, WorktreeError) as exc:
        state, issue = "conflict", str(exc)
    if state == "missing":
        result.update(
            resume_start_incomplete=True,
            resume_start_issue_detail=issue,
        )
    elif state == "conflict":
        result.update(
            resume_start_issue_code="task_start_evidence_conflict",
            resume_start_issue_detail=issue,
        )
    return result


def _aborted_source_requires_live_proof(
    profile: RepoProfile,
    *,
    transaction: LandingTransaction,
) -> bool:
    intent = transaction.intent
    source_path = Path(intent.worktree_path)
    if source_path.exists():
        return True
    if not transaction.abort_complete or not _exact_task_cleanup_event(
        profile,
        intent=intent,
    ):
        raise WorktreeError(
            "aborted landing source is absent without exact task cleanup evidence"
        )
    branch = _inspect_branch_ref(
        Path(intent.primary_worktree),
        intent.branch,
        role="task_branch",
    )
    if branch.state == "error":
        raise _inspection_error(branch)
    if branch.resolved_commit is not None or _registered_worktree_row(
        Path(intent.primary_worktree), source_path
    ) is not None:
        raise WorktreeError(
            "aborted landing cleanup evidence conflicts with retained branch state"
        )
    return False


def _abort_landing_for_close(
    profile: RepoProfile,
    *,
    workset: Workset,
    task: TaskSpec,
    transaction: LandingTransaction,
    close_request: Mapping[str, Any],
) -> LandingTransaction:
    intent = transaction.intent
    if transaction.target_updated or transaction.complete:
        raise WorktreeError("refusing close after the landing target was updated")
    if not transaction.abort_requested:
        if "source_prepared" not in transaction.phases:
            source_data = _landing_source_phase_data(
                profile,
                intent=intent,
                workset=workset,
                task=task,
            )
            record_landing_phase(
                profile,
                intent=intent,
                phase="source_prepared",
                data=source_data,
            )
            transaction = _reload_landing_transaction(profile, intent=intent)
        source_commit = _verify_landing_source_phase(
            profile,
            intent=intent,
            transaction=transaction,
            require_branch=True,
        )
        landed_commit = _abort_canonical_candidate(profile, transaction=transaction)
        if landed_commit is not None and "canonical_commit_created" not in transaction.phases:
            record_landing_phase(
                profile,
                intent=intent,
                phase="canonical_commit_created",
                data=_existing_canonical_landing_phase_data(
                    intent=intent,
                    landed_commit=landed_commit,
                ),
            )
            transaction = _reload_landing_transaction(profile, intent=intent)
            if _verify_canonical_landing_phase(
                intent=intent,
                transaction=transaction,
            ) != landed_commit:
                raise LandingTransactionError(
                    "recorded abort candidate changed during canonical phase append"
                )
        _target_commit, target_contains = _landing_abort_target_state(
            intent=intent,
            landed_commit=landed_commit,
        )
        if target_contains:
            # No abort intent is needed: the exact canonical candidate is
            # already reachable and the normal landing transaction can safely
            # converge from its durable canonical phase.
            return transaction
        abort_data = _landing_abort_data(
            profile,
            transaction=transaction,
            source_commit=source_commit,
            landed_commit=landed_commit,
            close_request=close_request,
        )
        record_landing_abort(profile, intent=intent, data=abort_data)
        transaction = _reload_landing_transaction(profile, intent=intent)
    _verify_landing_abort_chain(
        profile,
        transaction=transaction,
        close_request=close_request,
    )
    if not transaction.abort_cleanup_complete:
        cleanup_data = _cleanup_aborted_landing_temporary(
            profile,
            transaction=transaction,
        )
        record_landing_abort_cleanup(profile, intent=intent, data=cleanup_data)
        transaction = _reload_landing_transaction(profile, intent=intent)
    _verify_landing_abort_chain(
        profile,
        transaction=transaction,
        close_request=close_request,
    )
    landed_commit = (
        transaction.abort_data.get("landed_commit")
        if transaction.abort_data is not None
        else None
    )
    _target_commit, target_contains = _landing_abort_target_state(
        intent=intent,
        landed_commit=landed_commit if isinstance(landed_commit, str) else None,
    )
    if (
        target_contains
        and not transaction.abort_runtime_finalized
        and not _landing_abort_finalization_started(
            profile,
            transaction=transaction,
        )
    ):
        assert isinstance(landed_commit, str)
        assert isinstance(_target_commit, str)
        record_landing_abort_superseded(
            profile,
            intent=intent,
            data={
                "landed_commit": landed_commit,
                "target_branch": intent.target_branch,
                "target_commit": _target_commit,
                "reason": "target_contains_recorded_candidate",
            },
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
        _verify_landing_abort_chain(
            profile,
            transaction=transaction,
            close_request=close_request,
        )
    return transaction


def _landing_abort_validation_records(
    request: Mapping[str, Any],
) -> tuple[ValidationRecord, ...]:
    rows = request.get("validations")
    if not isinstance(rows, list):
        raise LandingTransactionError("landing abort validations are invalid")
    try:
        return tuple(
            ValidationRecord(name=row["name"], status=row["status"])
            for row in rows
            if isinstance(row, Mapping) and set(row) == {"name", "status"}
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LandingTransactionError("landing abort validations are invalid") from exc


def _landing_abort_close_event_payload(
    *,
    transaction: LandingTransaction,
) -> dict[str, Any]:
    intent = transaction.intent
    if transaction.abort_data is None:
        raise LandingTransactionError("landing abort intent is missing")
    request = transaction.abort_data.get("close_request")
    if not isinstance(request, Mapping):
        raise LandingTransactionError("landing abort close request is missing")
    return {
        "workset_id": intent.workset_id,
        "task_id": intent.task_id,
        "attempt_id": intent.attempt_id,
        "status": request["status"],
        "summary": request["summary"],
        "branch": intent.branch,
        "target_branch": intent.target_branch,
        "worktree_path": intent.worktree_path,
        "changed_paths": list(intent.changed_paths),
        "commit": transaction.abort_data["source_commit"],
        "cleanup_requested": request["cleanup_requested"],
        "cleanup_performed": False,
        "cleanup_reason": "source retained for exact successor adoption",
        "failure_class": request["failure_class"],
        "recovery_action": request["recovery_action"],
        "prompt_issue": request["prompt_issue"],
        "operator_issue": request["operator_issue"],
        "transaction_id": intent.transaction_id,
        "abort_finalization_id": request["finalization_id"],
    }


def _append_landing_abort_close_event_once(
    profile: RepoProfile,
    *,
    transaction: LandingTransaction,
) -> bool:
    intent = transaction.intent
    event_id = _landing_abort_close_event_id(intent.transaction_id)
    payload = _landing_abort_close_event_payload(transaction=transaction)
    try:
        changed = append_event_once(
            profile.paths.events_file,
            event_id=event_id,
            event_type="worktree.close",
            actor=intent.actor,
            payload=payload,
        )
    except StoreError as exc:
        raise LandingTransactionError(
            "landing abort close event conflicts with durable close request"
        ) from exc
    with exclusive_file_lock(profile.paths.events_file):
        matches = [
            event
            for event in load_events(profile.paths.events_file)
            if event.get("event_id") == event_id
        ]
    if not (
        len(matches) == 1
        and matches[0].get("type") == "worktree.close"
        and matches[0].get("actor") == intent.actor
        and strict_json_equal(matches[0].get("payload"), payload)
    ):
        raise LandingTransactionError(
            "landing abort close event conflicts with durable close request"
        )
    return changed


def _finalize_aborted_landing(
    profile: RepoProfile,
    *,
    transaction: LandingTransaction,
    close_request: Mapping[str, Any],
    require_source: bool = True,
) -> tuple[LandingTransaction, Any | None]:
    intent = transaction.intent
    source_commit = _verify_landing_abort_chain(
        profile,
        transaction=transaction,
        close_request=close_request,
        require_source=require_source,
    )
    if transaction.abort_superseded:
        return transaction, None
    landed_commit = (
        transaction.abort_data.get("landed_commit")
        if transaction.abort_data is not None
        else None
    )
    target_commit, target_contains = _landing_abort_target_state(
        intent=intent,
        landed_commit=landed_commit if isinstance(landed_commit, str) else None,
    )
    if (
        target_contains
        and not transaction.abort_runtime_finalized
        and not _landing_abort_finalization_started(
            profile,
            transaction=transaction,
        )
    ):
        assert isinstance(landed_commit, str)
        assert isinstance(target_commit, str)
        record_landing_abort_superseded(
            profile,
            intent=intent,
            data={
                "landed_commit": landed_commit,
                "target_branch": intent.target_branch,
                "target_commit": target_commit,
                "reason": "target_contains_recorded_candidate",
            },
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
        _verify_landing_abort_chain(
            profile,
            transaction=transaction,
            close_request=close_request,
            require_source=require_source,
        )
        return transaction, None

    assert transaction.abort_data is not None
    request = transaction.abort_data["close_request"]
    assert isinstance(request, Mapping)
    finished = finish_task(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
        actor=intent.actor,
        status=request["status"],
        summary=request["summary"],
        changed_paths=intent.changed_paths,
        validations=_landing_abort_validation_records(request),
        residuals=tuple(request["residuals"]),
        followup_candidates=tuple(request["followup_candidates"]),
        commit=source_commit,
        failure_class=request["failure_class"],
        recovery_action=request["recovery_action"],
        prompt_issue=request["prompt_issue"],
        operator_issue=request["operator_issue"],
        note=request["note"],
        finalization_id=request["finalization_id"],
    )
    if (
        finished.status != request["status"]
        or finished.commit != source_commit
        or finished.changed_paths != intent.changed_paths
        or finished.validations != _landing_abort_validation_records(request)
        or finished.residuals != tuple(request["residuals"])
        or finished.followup_candidates != tuple(request["followup_candidates"])
    ):
        raise BacklogError(
            "landing abort runtime finalization conflicts with durable close request"
        )
    runtime_data = {
        "finalization_id": request["finalization_id"],
        "status": request["status"],
        "source_commit": source_commit,
        "changed_paths": list(intent.changed_paths),
    }
    if not transaction.abort_runtime_finalized:
        record_landing_abort_runtime(
            profile,
            intent=intent,
            data=runtime_data,
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
    _verify_landing_abort_chain(
        profile,
        transaction=transaction,
        close_request=close_request,
        require_source=require_source,
    )

    _append_landing_abort_close_event_once(profile, transaction=transaction)
    close_data = {
        "event_id": _landing_abort_close_event_id(intent.transaction_id),
        "status": request["status"],
    }
    if not transaction.abort_close_event_recorded:
        record_landing_abort_close_event(
            profile,
            intent=intent,
            data=close_data,
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
    _verify_landing_abort_chain(
        profile,
        transaction=transaction,
        close_request=close_request,
        require_source=require_source,
    )

    complete_data = {
        "finalization_id": request["finalization_id"],
        "status": request["status"],
        "source_retained": True,
    }
    if not transaction.abort_complete:
        record_landing_abort_complete(
            profile,
            intent=intent,
            data=complete_data,
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
    _verify_landing_abort_chain(
        profile,
        transaction=transaction,
        close_request=close_request,
        require_source=require_source,
    )
    return transaction, finished


def _landing_validation_records(intent: LandingIntent) -> tuple[ValidationRecord, ...]:
    return tuple(
        ValidationRecord(name=name, status=status)
        for name, status in intent.validations
    )


def _finalize_landing_runtime(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    source_commit: str,
    landed_commit: str,
) -> tuple[Any, dict[str, Any]]:
    finished = finish_task(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
        actor=intent.actor,
        status=ATTEMPT_STATUS_SUCCESS,
        summary=intent.summary,
        changed_paths=intent.changed_paths,
        validations=_landing_validation_records(intent),
        residuals=intent.residuals,
        followup_candidates=intent.followup_candidates,
        commit=source_commit,
        landed_commit=landed_commit,
        note=intent.note,
        finalization_id=intent.transaction_id,
    )
    if (
        finished.status != ATTEMPT_STATUS_SUCCESS
        or finished.commit != source_commit
        or finished.landed_commit != landed_commit
        or finished.changed_paths != intent.changed_paths
        or finished.validations != _landing_validation_records(intent)
        or finished.residuals != intent.residuals
        or finished.followup_candidates != intent.followup_candidates
    ):
        raise BacklogError("landing runtime finalization conflicts with immutable intent")
    data = {
        "finalization_id": intent.transaction_id,
        "attempt_id": intent.attempt_id,
        "status": ATTEMPT_STATUS_SUCCESS,
        "source_commit": source_commit,
        "landed_commit": landed_commit,
        "ended_at": finished.ended_at,
    }
    return finished, data


def _verify_landing_runtime_phase(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    transaction: LandingTransaction,
    source_commit: str,
    landed_commit: str,
) -> Any:
    finished, expected_data = _finalize_landing_runtime(
        profile,
        intent=intent,
        source_commit=source_commit,
        landed_commit=landed_commit,
    )
    if not strict_json_equal(
        dict(transaction.data_for("runtime_finalized")), expected_data
    ):
        raise LandingTransactionError("runtime_finalized phase evidence conflicts with runtime")
    return finished


def _landing_event_payload(
    *,
    intent: LandingIntent,
    landed_commit: str,
) -> dict[str, Any]:
    return {
        "workset_id": intent.workset_id,
        "task_id": intent.task_id,
        "attempt_id": intent.attempt_id,
        "branch": intent.branch,
        "target_branch": intent.target_branch,
        "landed_commit": landed_commit,
        "changed_paths": list(intent.changed_paths),
        "commit_message": intent.commit_message,
        "cleanup": intent.cleanup,
    }


def _record_landing_event_phase_data(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    landed_commit: str,
) -> dict[str, Any]:
    payload = _landing_event_payload(intent=intent, landed_commit=landed_commit)
    append_worktree_land_once(profile, intent=intent, payload=payload)
    if not exact_worktree_land_event(profile, intent=intent, payload=payload):
        raise LandingTransactionError("append-once worktree.land evidence is missing or conflicting")
    return {
        "event_id": worktree_land_event_id(intent.transaction_id),
        "transaction_id": intent.transaction_id,
        "event_recorded": True,
    }


def _verify_landing_event_phase(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    transaction: LandingTransaction,
    landed_commit: str,
) -> None:
    expected_data = {
        "event_id": worktree_land_event_id(intent.transaction_id),
        "transaction_id": intent.transaction_id,
        "event_recorded": True,
    }
    if not strict_json_equal(
        dict(transaction.data_for("land_event_recorded")), expected_data
    ):
        raise LandingTransactionError("land_event_recorded phase evidence is not canonical")
    if not exact_worktree_land_event(
        profile,
        intent=intent,
        payload=_landing_event_payload(intent=intent, landed_commit=landed_commit),
    ):
        raise LandingTransactionError("recorded worktree.land event is missing or conflicting")


def _task_cleanup_event_id(
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str | None,
    branch: str | None,
    worktree_path: str,
) -> str:
    return worktree_cleanup_event_id(
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
        branch=branch,
        worktree_path=worktree_path,
    )


def _expected_task_cleanup_event(
    *,
    intent: LandingIntent,
) -> tuple[str, dict[str, Any]]:
    event_id = _task_cleanup_event_id(
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
        branch=intent.branch,
        worktree_path=intent.worktree_path,
    )
    return event_id, {
        "workset_id": intent.workset_id,
        "task_id": intent.task_id,
        "attempt_id": intent.attempt_id,
        "branch": intent.branch,
        "worktree_path": intent.worktree_path,
        "cleanup_complete": True,
        "worktree_absent": True,
        "branch_absent": True,
    }


def _exact_task_cleanup_event(profile: RepoProfile, *, intent: LandingIntent) -> bool:
    event_id, expected_payload = _expected_task_cleanup_event(intent=intent)
    matches = [
        event
        for event in load_events(profile.paths.events_file)
        if event.get("event_id") == event_id
    ]
    return len(matches) == 1 and (
        matches[0].get("type") == "worktree.cleanup"
        and matches[0].get("actor") == "blackdog"
        and strict_json_equal(matches[0].get("payload"), expected_payload)
    )


def _task_landing_cleanup_phase_data(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    source_commit: str,
) -> dict[str, Any]:
    primary_root = Path(intent.primary_worktree)
    worktree_path = Path(intent.worktree_path)
    if not intent.cleanup:
        branch = _inspect_branch_ref(primary_root, intent.branch, role="task_branch")
        registration = _registered_worktree_row(primary_root, worktree_path)
        if (
            branch.state != "exists"
            or branch.resolved_commit != source_commit
            or not worktree_path.exists()
            or registration is None
            or registration.get("branch") != f"refs/heads/{intent.branch}"
            or str(registration.get("HEAD") or "").strip() != source_commit
            or _run_git(worktree_path, "rev-parse", "HEAD") != source_commit
            or _managed_status_dirty(profile, worktree_path)
        ):
            raise WorktreeError("retained landing source is not clean and coherently registered")
        return {
            "cleanup_requested": False,
            "retained": True,
            "worktree_path": intent.worktree_path,
            "branch": intent.branch,
            "worktree_absent": False,
            "branch_absent": False,
            "cleanup_event_id": None,
            "source_commit": source_commit,
        }
    cleanup_task_worktree(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        path=intent.worktree_path,
        branch=intent.branch,
        _attempt_lock_held=True,
        _landing_transaction_id=intent.transaction_id,
    )
    branch = _inspect_branch_ref(primary_root, intent.branch, role="task_branch")
    if branch.state == "error":
        raise _inspection_error(branch)
    if (
        worktree_path.exists()
        or _registered_worktree_row(primary_root, worktree_path) is not None
        or branch.state != "missing"
        or not _exact_task_cleanup_event(profile, intent=intent)
    ):
        raise WorktreeError("task landing cleanup did not reach its exact final state")
    event_id, _payload = _expected_task_cleanup_event(intent=intent)
    return {
        "cleanup_requested": True,
        "retained": False,
        "worktree_path": intent.worktree_path,
        "branch": intent.branch,
        "worktree_absent": True,
        "branch_absent": True,
        "cleanup_event_id": event_id,
        "source_commit": source_commit,
    }


def _verify_task_landing_cleanup_phase(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    transaction: LandingTransaction,
) -> None:
    source_commit = transaction.data_for("source_prepared").get("source_commit")
    if not isinstance(source_commit, str) or not source_commit.strip():
        raise LandingTransactionError("task cleanup proof requires a canonical source commit")
    event_id, _payload = _expected_task_cleanup_event(intent=intent)
    expected_data = {
        "cleanup_requested": intent.cleanup,
        "retained": not intent.cleanup,
        "worktree_path": intent.worktree_path,
        "branch": intent.branch,
        "worktree_absent": intent.cleanup,
        "branch_absent": intent.cleanup,
        "cleanup_event_id": event_id if intent.cleanup else None,
        "source_commit": source_commit,
    }
    if not strict_json_equal(
        dict(transaction.data_for("task_cleanup_complete")), expected_data
    ):
        raise LandingTransactionError("task_cleanup_complete phase evidence is not canonical")
    if not intent.cleanup:
        primary_root = Path(intent.primary_worktree)
        worktree_path = Path(intent.worktree_path)
        branch = _inspect_branch_ref(primary_root, intent.branch, role="task_branch")
        if branch.state == "error":
            raise _inspection_error(branch)
        registration = _registered_worktree_row(primary_root, worktree_path)
        if (
            branch.state != "exists"
            or branch.resolved_commit != source_commit
            or not worktree_path.exists()
            or registration is None
            or registration.get("branch") != f"refs/heads/{intent.branch}"
            or str(registration.get("HEAD") or "").strip() != source_commit
            or _run_git(worktree_path, "rev-parse", "HEAD") != source_commit
            or _managed_status_dirty(profile, worktree_path)
        ):
            raise WorktreeError("recorded retained landing source no longer holds")
        return
    primary_root = Path(intent.primary_worktree)
    branch = _inspect_branch_ref(primary_root, intent.branch, role="task_branch")
    if branch.state == "error":
        raise _inspection_error(branch)
    if (
        Path(intent.worktree_path).exists()
        or _registered_worktree_row(primary_root, Path(intent.worktree_path)) is not None
        or branch.state != "missing"
        or not _exact_task_cleanup_event(profile, intent=intent)
    ):
        raise WorktreeError("recorded task landing cleanup no longer holds")


def _complete_landing_phase_data(
    *,
    intent: LandingIntent,
    landed_commit: str,
) -> dict[str, Any]:
    return {
        "transaction_id": intent.transaction_id,
        "landed_commit": landed_commit,
        "target_branch": intent.target_branch,
        "complete": True,
    }


def _failure_details_for_status(status: str, *, recovery_action: str | None = None) -> dict[str, Any]:
    if status == ATTEMPT_STATUS_ABANDONED:
        return {
            "failure_class": FAILURE_CLASS_ABANDONED,
            "recovery_action": recovery_action or "reopen_if_needed",
            "prompt_issue": False,
            "operator_issue": True,
        }
    return {
        "failure_class": FAILURE_CLASS_UNKNOWN,
        "recovery_action": recovery_action or "inspect",
        "prompt_issue": False,
        "operator_issue": False,
    }


def _failure_details_for_land_error(exc: Exception) -> dict[str, Any]:
    return classify_lifecycle_exception(exc).to_legacy_dict()


def _terminal_land_failure_status(exc: Exception) -> str | None:
    return classify_lifecycle_exception(exc).terminal_attempt_status


def _canonical_trailer_values(repo_root: Path, commit: str) -> dict[str, list[str]]:
    message = _run_git(repo_root, "show", "-s", "--format=%B", commit)
    parsed = _run_git_with_input(repo_root, "interpret-trailers", "--parse", input_text=message)
    trailers: dict[str, list[str]] = {}
    for raw_line in parsed.splitlines():
        key, separator, value = raw_line.partition(":")
        if not separator:
            continue
        trailers.setdefault(key.strip(), []).append(value.strip())
    return trailers


def _require_exact_canonical_trailer(
    trailers: dict[str, list[str]],
    *,
    key: str,
    expected: str,
) -> None:
    values = trailers.get(key, [])
    if values != [expected]:
        rendered = values if values else "missing"
        raise WorktreeError(
            f"landed commit canonical trailer {key} must be exactly {expected!r}; got {rendered!r}"
        )


def _require_supported_canonical_commit_format(
    trailers: dict[str, list[str]],
) -> int:
    values = trailers.get("Blackdog-Commit-Format", [])
    if not values:
        return 1
    if values == [CANONICAL_COMMIT_FORMAT_VERSION]:
        return int(CANONICAL_COMMIT_FORMAT_VERSION)
    raise WorktreeError(
        "landed commit canonical trailer Blackdog-Commit-Format must be absent "
        f"for legacy format 1 or exactly {CANONICAL_COMMIT_FORMAT_VERSION!r}; got {values!r}"
    )


def _stable_diff_patch_id(repo_root: Path, base_commit: str, head_commit: str) -> str:
    diff = _run_git(repo_root, "diff", "--binary", base_commit, head_commit)
    if not diff:
        raise WorktreeError(f"commit range {base_commit[:12]}..{head_commit[:12]} has no patch")
    patch_id_row = _run_git_with_input(repo_root, "patch-id", "--stable", input_text=diff)
    patch_id = patch_id_row.split(maxsplit=1)[0] if patch_id_row else ""
    if not patch_id:
        raise WorktreeError(
            f"could not compute a stable patch id for {base_commit[:12]}..{head_commit[:12]}"
        )
    return patch_id


def _actor_mismatch_finalization_evidence(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    attempt_actor: str,
) -> dict[str, Any] | None:
    for event in reversed(load_events(profile.paths.events_file)):
        payload = event.get("payload")
        if event.get("type") not in {"task.finish", "worktree.close"} or not isinstance(payload, dict):
            continue
        if (
            payload.get("workset_id") != workset_id
            or payload.get("task_id") != task_id
            or payload.get("attempt_id") != attempt_id
            or payload.get("status")
            not in {
                ATTEMPT_STATUS_BLOCKED,
                ATTEMPT_STATUS_FAILED,
                ATTEMPT_STATUS_ABANDONED,
            }
            or event.get("actor") != attempt_actor
        ):
            continue
        summary = str(payload.get("summary") or "").strip()
        normalized = " ".join(summary.lower().split())
        landing_succeeded = re.search(r"\bgit landing (?:completed|succeeded)\b", normalized) is not None
        finalization_failed = (
            re.search(r"\bruntime finalization (?:rejected|failed)\b", normalized) is not None
        )
        deliberate_actor_mismatch = (
            re.search(r"\bdeliberately mismatched actor\b", normalized) is not None
        )
        after_mutation = re.search(r"\bafter mutation\b", normalized) is not None
        contradiction = any(
            marker in normalized
            for marker in (
                "actor mismatch did not occur",
                "no actor mismatch",
                "without actor mismatch",
                "before git landing",
                "git landing did not complete",
                "git landing failed",
            )
        )
        if (
            landing_succeeded
            and finalization_failed
            and deliberate_actor_mismatch
            and after_mutation
            and not contradiction
        ):
            return {
                "event_id": event.get("event_id"),
                "event_type": event.get("type"),
                "event_actor": event.get("actor"),
                "status": payload.get("status"),
                "summary": _bounded_reconciliation_text(summary),
            }
    return None


def _bounded_reconciliation_text(value: object, *, limit: int = 500) -> str:
    rendered = " ".join(str(value or "").split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _reconciliation_trailers(repo_root: Path, commit: str) -> dict[str, list[str]]:
    try:
        return _canonical_trailer_values(repo_root, commit)
    except (WorktreeError, OSError) as exc:
        raise LandingReconciliationInspectionError(str(exc)) from exc


def _reconciliation_run_git_no_check(
    repo_root: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return _run_git_no_check(repo_root, *args)
    except OSError as exc:
        raise LandingReconciliationInspectionError(
            f"could not execute git {' '.join(args)}: {exc}"
        ) from exc


def _reconciliation_inspect_commit(
    repo_root: Path,
    ref: str | None,
    *,
    role: str,
) -> GitReferenceInspection:
    try:
        return _inspect_commit(repo_root, ref, role=role)
    except OSError as exc:
        raise LandingReconciliationInspectionError(
            f"could not inspect {role.replace('_', ' ')}: {exc}"
        ) from exc


def _reconciliation_inspect_branch(
    repo_root: Path,
    ref: str | None,
    *,
    role: str,
) -> GitReferenceInspection:
    try:
        return _inspect_branch_ref(repo_root, ref, role=role)
    except OSError as exc:
        raise LandingReconciliationInspectionError(
            f"could not inspect {role.replace('_', ' ')}: {exc}"
        ) from exc


def _reconciliation_patch_id(repo_root: Path, base_commit: str, head_commit: str) -> str:
    try:
        diff = _run_git(repo_root, "diff", "--binary", base_commit, head_commit)
    except (WorktreeError, OSError) as exc:
        raise LandingReconciliationInspectionError(str(exc)) from exc
    if not diff:
        raise LandingReconciliationProofError(
            f"commit range {base_commit[:12]}..{head_commit[:12]} has no patch"
        )
    try:
        patch_id_row = _run_git_with_input(
            repo_root,
            "patch-id",
            "--stable",
            input_text=diff,
        )
    except (WorktreeError, OSError) as exc:
        raise LandingReconciliationInspectionError(str(exc)) from exc
    patch_id = patch_id_row.split(maxsplit=1)[0] if patch_id_row else ""
    if not patch_id:
        raise LandingReconciliationInspectionError(
            f"could not compute a stable patch id for {base_commit[:12]}..{head_commit[:12]}"
        )
    return patch_id


def _prove_landing_reconciliation_candidate(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt: Any,
    landed_commit: str,
) -> dict[str, Any]:
    """Return the one canonical, read-only proof shared by detection and repair."""
    try:
        primary_root = find_primary_worktree(profile.paths.project_root)
    except (WorktreeError, OSError) as exc:
        raise LandingReconciliationInspectionError(str(exc)) from exc

    landed_inspection = _reconciliation_inspect_commit(
        primary_root,
        landed_commit,
        role="landed_commit",
    )
    if landed_inspection.state == "error":
        raise LandingReconciliationInspectionError(str(_inspection_error(landed_inspection)))
    resolved_landed_commit = landed_inspection.resolved_commit
    if resolved_landed_commit is None:
        raise LandingReconciliationProofError(
            f"landed commit {landed_commit!r} does not resolve to a commit"
        )
    target_inspection = _reconciliation_inspect_branch(
        primary_root,
        attempt.target_branch,
        role="target_branch",
    )
    if target_inspection.state == "error":
        raise LandingReconciliationInspectionError(str(_inspection_error(target_inspection)))
    resolved_target_commit = target_inspection.resolved_commit
    if resolved_target_commit is None:
        raise LandingReconciliationProofError(
            f"target branch {attempt.target_branch!r} does not resolve to a commit"
        )
    reachable = _reconciliation_run_git_no_check(
        primary_root,
        "merge-base",
        "--is-ancestor",
        resolved_landed_commit,
        resolved_target_commit,
    )
    if reachable.returncode == 1:
        raise LandingReconciliationProofError(
            f"landed commit {resolved_landed_commit[:12]} is not reachable from {attempt.target_branch}"
        )
    if reachable.returncode != 0:
        detail = reachable.stderr.strip() or reachable.stdout.strip() or f"exit code {reachable.returncode}"
        raise LandingReconciliationInspectionError(
            f"could not inspect landed-commit reachability: {detail}"
        )

    parents = _reconciliation_run_git_no_check(
        primary_root,
        "rev-list",
        "--parents",
        "-n",
        "1",
        resolved_landed_commit,
    )
    if parents.returncode != 0:
        detail = parents.stderr.strip() or parents.stdout.strip() or f"exit code {parents.returncode}"
        raise LandingReconciliationInspectionError(
            f"could not inspect landed-commit parents: {detail}"
        )
    landed_parts = parents.stdout.strip().split()
    if len(landed_parts) != 2:
        raise LandingReconciliationProofError(
            f"landed commit {resolved_landed_commit[:12]} must have exactly one parent for reconciliation"
        )
    landed_parent_commit = landed_parts[1]

    trailers = _reconciliation_trailers(primary_root, resolved_landed_commit)
    expected_trailers = {
        "Blackdog-Workset": workset_id,
        "Blackdog-Task": task_id,
        "Blackdog-Attempt": attempt.attempt_id,
        "Blackdog-Status": ATTEMPT_STATUS_SUCCESS,
        "Blackdog-Target-Branch": attempt.target_branch,
    }
    for key, expected in expected_trailers.items():
        try:
            _require_exact_canonical_trailer(trailers, key=key, expected=expected)
        except WorktreeError as exc:
            raise LandingReconciliationProofError(str(exc)) from exc
    try:
        commit_format = _require_supported_canonical_commit_format(trailers)
    except WorktreeError as exc:
        raise LandingReconciliationProofError(str(exc)) from exc
    commit_actor_values = trailers.get("Blackdog-Actor", [])
    if len(commit_actor_values) != 1 or not commit_actor_values[0]:
        raise LandingReconciliationProofError(
            "landed commit canonical trailer Blackdog-Actor must occur exactly once with a nonempty value"
        )
    commit_actor = commit_actor_values[0]
    actor_matches_attempt = commit_actor == attempt.actor
    actor_mismatch_evidence = None
    if not actor_matches_attempt:
        try:
            actor_mismatch_evidence = _actor_mismatch_finalization_evidence(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=attempt.attempt_id,
                attempt_actor=attempt.actor,
            )
        except (StoreError, OSError) as exc:
            raise LandingReconciliationInspectionError(
                f"could not inspect actor-mismatch finalization evidence: {exc}"
            ) from exc
        if actor_mismatch_evidence is None:
            raise LandingReconciliationProofError(
                f"landed commit actor {commit_actor!r} does not match attempt actor {attempt.actor!r}, "
                "and the terminal attempt history does not prove a post-Git actor-ownership finalization failure"
            )

    changed = _reconciliation_run_git_no_check(
        primary_root,
        "diff",
        "--name-only",
        landed_parent_commit,
        resolved_landed_commit,
    )
    if changed.returncode != 0:
        detail = changed.stderr.strip() or changed.stdout.strip() or f"exit code {changed.returncode}"
        raise LandingReconciliationInspectionError(
            f"could not inspect landed-commit changed paths: {detail}"
        )
    changed_paths = tuple(
        sorted(line.strip() for line in changed.stdout.splitlines() if line.strip())
    )
    if not changed_paths:
        raise LandingReconciliationProofError(
            f"landed commit {resolved_landed_commit[:12]} has no changed paths"
        )
    trailer_changed_paths = tuple(sorted(trailers.get("Blackdog-Changed-Path", [])))
    if trailer_changed_paths != changed_paths:
        raise LandingReconciliationProofError(
            "landed commit Blackdog-Changed-Path trailers do not match the commit diff: "
            f"trailers={list(trailer_changed_paths)!r}, diff={list(changed_paths)!r}"
        )

    source_inspection = _reconciliation_inspect_commit(
        primary_root,
        attempt.commit,
        role="source_commit",
    )
    if source_inspection.state == "error":
        raise LandingReconciliationInspectionError(str(_inspection_error(source_inspection)))
    resolved_source_commit = source_inspection.resolved_commit
    source_patch_equivalent: bool | None = None
    resolved_source_base: str | None = None
    if resolved_source_commit is not None:
        source_base_inspection = _reconciliation_inspect_commit(
            primary_root,
            attempt.start_commit,
            role="source_start_commit",
        )
        if source_base_inspection.state == "error":
            raise LandingReconciliationInspectionError(
                str(_inspection_error(source_base_inspection))
            )
        resolved_source_base = source_base_inspection.resolved_commit
        if resolved_source_base is None:
            raise LandingReconciliationProofError(
                f"recorded source commit {resolved_source_commit[:12]} resolves, but attempt start commit "
                f"{attempt.start_commit!r} does not; patch equivalence cannot be proven"
            )
        source_patch_id = _reconciliation_patch_id(
            primary_root,
            resolved_source_base,
            resolved_source_commit,
        )
        landed_patch_id = _reconciliation_patch_id(
            primary_root,
            landed_parent_commit,
            resolved_landed_commit,
        )
        if source_patch_id != landed_patch_id:
            raise LandingReconciliationProofError(
                f"recorded source commit {resolved_source_commit[:12]} is not patch-equivalent to "
                f"landed commit {resolved_landed_commit[:12]}"
            )
        source_patch_equivalent = True

    reconciliation_id = landing_reconciliation_id(
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt.attempt_id,
        landed_commit=resolved_landed_commit,
    )
    proof = {
        "target_branch": attempt.target_branch,
        "target_commit": resolved_target_commit,
        "landed_parent_commit": landed_parent_commit,
        "reachable_from_target": True,
        "canonical_trailers": {**expected_trailers, "Blackdog-Actor": commit_actor},
        "commit_format": commit_format,
        "commit_actor": commit_actor,
        "attempt_actor": attempt.actor,
        "actor_matches_attempt": actor_matches_attempt,
        "actor_mismatch_evidence": actor_mismatch_evidence,
        "changed_paths": list(changed_paths),
        "changed_paths_match": True,
        "source_commit": resolved_source_commit,
        "source_start_commit": resolved_source_base,
        "source_patch_equivalent": source_patch_equivalent,
    }
    return {
        "reconciliation_id": reconciliation_id,
        "resolved_landed_commit": resolved_landed_commit,
        "commit_actor": commit_actor,
        "proof": proof,
    }


def _legacy_reconciliation_detection_result(
    *,
    state: str,
    reason_code: str,
    reason_detail: str,
    candidate_count: int = 0,
    candidate_commit: str | None = None,
    candidate_commits: Sequence[str] = (),
    sentinel_commit: str | None = None,
    inspected_commit_count: int = 0,
    proof: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if state not in {
        "ready",
        "none",
        "unproven",
        "ambiguous",
        "inconclusive",
        "error",
    }:
        raise ValueError(f"invalid legacy reconciliation detection state: {state}")
    result: dict[str, Any] = {
        "state": state,
        "reason_code": reason_code,
        "reason_detail": _bounded_reconciliation_text(reason_detail),
        "candidate_count": min(max(0, int(candidate_count)), LEGACY_RECONCILIATION_SCAN_LIMIT),
        "candidate_commit": candidate_commit,
        "candidate_commits": list(candidate_commits)[:LEGACY_RECONCILIATION_SCAN_LIMIT],
        "scan_limit": LEGACY_RECONCILIATION_SCAN_LIMIT,
        "inspected_commit_count": min(
            max(0, int(inspected_commit_count)),
            LEGACY_RECONCILIATION_SCAN_LIMIT,
        ),
        "sentinel_commit": sentinel_commit,
    }
    if proof is not None:
        result["proof"] = dict(proof)
    return result


def _attempt_has_workspace_adoption_marker(
    attempt: Any,
) -> bool:
    setup_receipt = attempt.setup_receipt
    return isinstance(setup_receipt, Mapping) and "workspace_adoption" in setup_receipt


def _detect_legacy_landing_reconciliation(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    active_attempt: Any | None,
    latest_attempt: Any | None,
    current_task_claim: Any | None,
    landing_transaction: LandingTransaction | None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Boundedly detect one legacy candidate without mutating any durable state."""
    no_action: tuple[str, ...] = ()
    if active_attempt is not None:
        return (
            _legacy_reconciliation_detection_result(
                state="none",
                reason_code="active_attempt_present",
                reason_detail="An active attempt owns this task; legacy reconciliation is ineligible.",
            ),
            no_action,
        )
    if latest_attempt is None:
        return (
            _legacy_reconciliation_detection_result(
                state="none",
                reason_code="no_attempt",
                reason_detail="The task has no attempt to inspect for legacy reconciliation.",
            ),
            no_action,
        )
    if current_task_claim is not None:
        return (
            _legacy_reconciliation_detection_result(
                state="none",
                reason_code="task_claim_present",
                reason_detail="A task claim is still present; legacy reconciliation is ineligible.",
            ),
            no_action,
        )
    if latest_attempt.status not in {ATTEMPT_STATUS_FAILED, ATTEMPT_STATUS_BLOCKED}:
        return (
            _legacy_reconciliation_detection_result(
                state="none",
                reason_code="attempt_status_ineligible",
                reason_detail=(
                    f"Latest attempt status {latest_attempt.status!r} is not failed or blocked."
                ),
            ),
            no_action,
        )
    if latest_attempt.ended_at is None:
        return (
            _legacy_reconciliation_detection_result(
                state="none",
                reason_code="attempt_not_terminal",
                reason_detail="The latest attempt is not terminal.",
            ),
            no_action,
        )
    if latest_attempt.landed_commit:
        return (
            _legacy_reconciliation_detection_result(
                state="none",
                reason_code="landed_commit_recorded",
                reason_detail="The latest attempt already records a landed commit.",
            ),
            no_action,
        )
    if landing_transaction is not None:
        return (
            _legacy_reconciliation_detection_result(
                state="none",
                reason_code="native_landing_transaction_present",
                reason_detail="A native landing transaction owns recovery for this attempt.",
            ),
            no_action,
        )
    if _attempt_has_workspace_adoption_marker(latest_attempt):
        return (
            _legacy_reconciliation_detection_result(
                state="none",
                reason_code="workspace_adoption_present",
                reason_detail="Workspace-adoption evidence owns recovery for this attempt.",
            ),
            no_action,
        )
    if not str(latest_attempt.target_branch or "").strip():
        return (
            _legacy_reconciliation_detection_result(
                state="inconclusive",
                reason_code="target_branch_metadata_missing",
                reason_detail="The attempt is missing target-branch metadata.",
            ),
            no_action,
        )
    if not str(latest_attempt.start_commit or "").strip():
        return (
            _legacy_reconciliation_detection_result(
                state="inconclusive",
                reason_code="start_commit_metadata_missing",
                reason_detail="The attempt is missing its exact start-commit sentinel.",
            ),
            no_action,
        )
    if not str(latest_attempt.actor or "").strip():
        return (
            _legacy_reconciliation_detection_result(
                state="inconclusive",
                reason_code="attempt_actor_missing",
                reason_detail="The attempt is missing actor attribution required by the dry-run command.",
            ),
            no_action,
        )

    try:
        primary_root = find_primary_worktree(profile.paths.project_root)
    except (WorktreeError, OSError) as exc:
        return (
            _legacy_reconciliation_detection_result(
                state="error",
                reason_code="primary_worktree_inspection_failed",
                reason_detail=str(exc),
            ),
            no_action,
        )
    try:
        target = _reconciliation_inspect_branch(
            primary_root,
            latest_attempt.target_branch,
            role="target_branch",
        )
    except LandingReconciliationInspectionError as exc:
        return (
            _legacy_reconciliation_detection_result(
                state="error",
                reason_code="target_branch_inspection_failed",
                reason_detail=str(exc),
            ),
            no_action,
        )
    if target.state in {"missing", "metadata_missing"}:
        return (
            _legacy_reconciliation_detection_result(
                state="inconclusive",
                reason_code="target_branch_unresolved",
                reason_detail=target.detail or "The recorded target branch does not resolve.",
            ),
            no_action,
        )
    if target.state == "error" or target.resolved_commit is None:
        return (
            _legacy_reconciliation_detection_result(
                state="error",
                reason_code="target_branch_inspection_failed",
                reason_detail=target.detail or "The recorded target branch could not be inspected.",
            ),
            no_action,
        )
    try:
        sentinel = _reconciliation_inspect_commit(
            primary_root,
            latest_attempt.start_commit,
            role="attempt_start_commit",
        )
    except LandingReconciliationInspectionError as exc:
        return (
            _legacy_reconciliation_detection_result(
                state="error",
                reason_code="start_commit_inspection_failed",
                reason_detail=str(exc),
            ),
            no_action,
        )
    if sentinel.state in {"missing", "metadata_missing"}:
        return (
            _legacy_reconciliation_detection_result(
                state="inconclusive",
                reason_code="start_commit_unresolved",
                reason_detail=sentinel.detail or "The attempt start-commit sentinel does not resolve.",
            ),
            no_action,
        )
    if sentinel.state == "error" or sentinel.resolved_commit is None:
        return (
            _legacy_reconciliation_detection_result(
                state="error",
                reason_code="start_commit_inspection_failed",
                reason_detail=sentinel.detail or "The attempt start commit could not be inspected.",
            ),
            no_action,
        )

    try:
        history_result = _reconciliation_run_git_no_check(
            primary_root,
            "rev-list",
            "--first-parent",
            f"--max-count={LEGACY_RECONCILIATION_SCAN_LIMIT + 1}",
            target.resolved_commit,
        )
    except LandingReconciliationInspectionError as exc:
        return (
            _legacy_reconciliation_detection_result(
                state="error",
                reason_code="target_history_inspection_failed",
                reason_detail=str(exc),
                sentinel_commit=sentinel.resolved_commit,
            ),
            no_action,
        )
    if history_result.returncode != 0:
        detail = (
            history_result.stderr.strip()
            or history_result.stdout.strip()
            or f"exit code {history_result.returncode}"
        )
        return (
            _legacy_reconciliation_detection_result(
                state="error",
                reason_code="target_history_inspection_failed",
                reason_detail=f"Could not inspect bounded target first-parent history: {detail}",
                sentinel_commit=sentinel.resolved_commit,
            ),
            no_action,
        )
    history = [line.strip() for line in history_result.stdout.splitlines() if line.strip()]
    try:
        sentinel_index = history.index(sentinel.resolved_commit)
    except ValueError:
        return (
            _legacy_reconciliation_detection_result(
                state="inconclusive",
                reason_code="start_commit_outside_scan_bound",
                reason_detail=(
                    "The exact start-commit sentinel was not observed within 64 first-parent "
                    "commits after target."
                ),
                sentinel_commit=sentinel.resolved_commit,
                inspected_commit_count=min(len(history), LEGACY_RECONCILIATION_SCAN_LIMIT),
            ),
            no_action,
        )
    commits_after_sentinel = history[:sentinel_index]
    plausible_candidates: list[str] = []
    for commit in commits_after_sentinel:
        try:
            trailers = _reconciliation_trailers(primary_root, commit)
        except LandingReconciliationInspectionError as exc:
            return (
                _legacy_reconciliation_detection_result(
                    state="error",
                    reason_code="candidate_trailer_inspection_failed",
                    reason_detail=str(exc),
                    sentinel_commit=sentinel.resolved_commit,
                    inspected_commit_count=len(commits_after_sentinel),
                ),
                no_action,
            )
        plausible_identity = {
            "Blackdog-Workset": workset_id,
            "Blackdog-Task": task_id,
            "Blackdog-Attempt": latest_attempt.attempt_id,
        }
        if all(expected in trailers.get(key, []) for key, expected in plausible_identity.items()):
            plausible_candidates.append(commit)

    candidate_count = len(plausible_candidates)
    common = {
        "candidate_count": candidate_count,
        "candidate_commits": plausible_candidates,
        "sentinel_commit": sentinel.resolved_commit,
        "inspected_commit_count": len(commits_after_sentinel),
    }
    if candidate_count == 0:
        return (
            _legacy_reconciliation_detection_result(
                state="none",
                reason_code="no_plausible_candidate",
                reason_detail="No commit in the bounded history referenced the exact task attempt identity.",
                **common,
            ),
            no_action,
        )
    if candidate_count > 1:
        return (
            _legacy_reconciliation_detection_result(
                state="ambiguous",
                reason_code="multiple_plausible_candidates",
                reason_detail=(
                    f"Found {candidate_count} commits referencing the exact task attempt identity; "
                    "Blackdog will not select one."
                ),
                **common,
            ),
            no_action,
        )

    candidate = plausible_candidates[0]
    try:
        candidate_proof = _prove_landing_reconciliation_candidate(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt=latest_attempt,
            landed_commit=candidate,
        )
    except LandingReconciliationProofError as exc:
        return (
            _legacy_reconciliation_detection_result(
                state="unproven",
                reason_code="canonical_proof_failed",
                reason_detail=str(exc),
                candidate_commit=candidate,
                **common,
            ),
            no_action,
        )
    except (LandingReconciliationInspectionError, StoreError, OSError) as exc:
        return (
            _legacy_reconciliation_detection_result(
                state="error",
                reason_code="canonical_proof_inspection_failed",
                reason_detail=str(exc),
                candidate_commit=candidate,
                **common,
            ),
            no_action,
        )

    resolved_candidate = str(candidate_proof["resolved_landed_commit"])
    executable = _lifecycle_blackdog_executable(
        profile,
        {"worktree_path": latest_attempt.worktree_path},
    )
    argv = (
        executable,
        "task",
        "reconcile-landing",
        f"--project-root={primary_root}",
        f"--workset={workset_id}",
        f"--task={task_id}",
        f"--attempt={latest_attempt.attempt_id}",
        f"--landed-commit={resolved_candidate}",
        f"--actor={latest_attempt.actor}",
        f"--reason={LEGACY_RECONCILIATION_REASON}",
    )
    return (
        _legacy_reconciliation_detection_result(
            state="ready",
            reason_code="canonical_legacy_landing_detected",
            reason_detail=(
                "One bounded legacy candidate passed the canonical read-only landing proof."
            ),
            candidate_commit=resolved_candidate,
            proof=candidate_proof["proof"],
            **common,
        ),
        argv,
    )


def _workspace_adoption_completion_intent_payload(
    *,
    attempt: Any,
    receipt: Mapping[str, Any],
    completion_route: str,
    source_commit: str,
    source_tree_hash: str,
    target_commit: str,
    landed_commit: str,
    changed_paths: Sequence[str],
    cleanup_requested: bool,
    source_attribution: Mapping[str, Any],
    native_target_updated_commit: str | None = None,
    native_landing_transaction_id: str | None = None,
    native_land_event_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": WORKSPACE_ADOPTION_SCHEMA_VERSION,
        "adoption_id": receipt["adoption_id"],
        "successor_attempt_id": attempt.attempt_id,
        "predecessor_attempt_id": receipt["predecessor_attempt_id"],
        "abort_transaction_id": receipt["abort_transaction_id"],
        "canonical_candidate": receipt["canonical_candidate"],
        "source_commit": source_commit,
        "source_tree_hash": source_tree_hash,
        "landed_commit": landed_commit,
        "branch": receipt["branch"],
        "worktree_path": receipt["worktree_path"],
        "target_branch": receipt["target_branch"],
        "target_commit_at_completion": target_commit,
        "native_target_updated_commit": native_target_updated_commit,
        "changed_paths": list(changed_paths),
        "completion_route": completion_route,
        "cleanup_requested": cleanup_requested,
        "source_attribution": dict(source_attribution),
        "native_landing_transaction_id": native_landing_transaction_id,
        "native_land_event_id": native_land_event_id,
    }


def _append_workspace_adoption_completion_intent(
    profile: RepoProfile,
    *,
    attempt: Any,
    payload: Mapping[str, Any],
) -> bool:
    event_id = _workspace_adoption_completion_intent_event_id(attempt.attempt_id)
    existing = [
        event
        for event in load_events(profile.paths.events_file)
        if event.get("event_id") == event_id
    ]
    if not existing:
        primary_root = find_primary_worktree(profile.paths.project_root)
        target = _inspect_branch_ref(
            primary_root,
            str(payload.get("target_branch") or ""),
            role="target_branch",
        )
        if target.state == "error":
            raise _inspection_error(target)
        target_commit = target.resolved_commit
        landed_commit = str(payload.get("landed_commit") or "")
        if (
            target_commit != payload.get("target_commit_at_completion")
            or not landed_commit
            or not _target_contains_landed_commit(
                primary_root,
                target_commit=target_commit or "",
                landed_commit=landed_commit,
            )
        ):
            raise LandingTransactionError(
                "workspace adoption target changed before durable completion intent append"
            )
    appended = append_event_once(
        profile.paths.events_file,
        event_id=event_id,
        event_type="worktree.adoption.completion.intent",
        actor=attempt.actor,
        payload=payload,
    )
    if not _exact_workspace_adoption_event(
        profile,
        event_id=event_id,
        event_type="worktree.adoption.completion.intent",
        actor=attempt.actor,
        payload=payload,
    ):
        raise LandingTransactionError(
            "workspace adoption completion intent is missing or conflicting"
        )
    return appended


def _load_workspace_adoption_completion_intent(
    profile: RepoProfile,
    *,
    attempt: Any,
    receipt: Mapping[str, Any],
    expected_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    event_id = _workspace_adoption_completion_intent_event_id(attempt.attempt_id)
    matches = [
        event
        for event in load_events(profile.paths.events_file)
        if event.get("event_id") == event_id
    ]
    if not matches:
        return None
    if len(matches) != 1 or (
        matches[0].get("type") != "worktree.adoption.completion.intent"
        or matches[0].get("actor") != attempt.actor
        or not isinstance(matches[0].get("payload"), Mapping)
    ):
        raise LandingTransactionError(
            "workspace adoption completion intent is conflicting"
        )
    payload = dict(matches[0]["payload"])
    _validate_workspace_adoption_completion_intent(
        attempt=attempt,
        receipt=receipt,
        payload=payload,
        expected_payload=expected_payload,
    )
    return payload


_WORKSPACE_ADOPTION_COMPLETION_INTENT_KEYS = frozenset(
    {
        "schema_version",
        "adoption_id",
        "successor_attempt_id",
        "predecessor_attempt_id",
        "abort_transaction_id",
        "canonical_candidate",
        "source_commit",
        "source_tree_hash",
        "landed_commit",
        "branch",
        "worktree_path",
        "target_branch",
        "target_commit_at_completion",
        "native_target_updated_commit",
        "changed_paths",
        "completion_route",
        "cleanup_requested",
        "source_attribution",
        "native_landing_transaction_id",
        "native_land_event_id",
    }
)


def _validate_workspace_adoption_completion_intent(
    *,
    attempt: Any,
    receipt: Mapping[str, Any],
    payload: Mapping[str, Any],
    expected_payload: Mapping[str, Any] | None = None,
) -> None:
    if set(payload) != _WORKSPACE_ADOPTION_COMPLETION_INTENT_KEYS:
        raise LandingTransactionError(
            "workspace adoption completion intent has an invalid field set"
        )
    required_text = (
        "adoption_id",
        "successor_attempt_id",
        "predecessor_attempt_id",
        "abort_transaction_id",
        "canonical_candidate",
        "source_commit",
        "source_tree_hash",
        "landed_commit",
        "branch",
        "worktree_path",
        "target_branch",
        "target_commit_at_completion",
        "completion_route",
    )
    if payload.get("schema_version") != WORKSPACE_ADOPTION_SCHEMA_VERSION or any(
        not isinstance(payload.get(key), str) or not str(payload[key]).strip()
        for key in required_text
    ):
        raise LandingTransactionError(
            "workspace adoption completion intent has invalid typed identity"
        )
    if type(payload.get("cleanup_requested")) is not bool:
        raise LandingTransactionError(
            "workspace adoption completion intent cleanup_requested must be boolean"
        )
    changed_paths = payload.get("changed_paths")
    if (
        not isinstance(changed_paths, list)
        or any(not isinstance(path, str) or not path.strip() for path in changed_paths)
        or len(changed_paths) != len(set(changed_paths))
    ):
        raise LandingTransactionError(
            "workspace adoption completion intent changed_paths must be unique nonempty strings"
        )
    if not isinstance(payload.get("source_attribution"), Mapping):
        raise LandingTransactionError(
            "workspace adoption completion intent source_attribution must be an object"
        )
    route = payload["completion_route"]
    native_transaction = payload.get("native_landing_transaction_id")
    native_land = payload.get("native_land_event_id")
    if route == "predecessor_candidate_containment":
        if (
            native_transaction is not None
            or native_land is not None
            or payload.get("native_target_updated_commit") is not None
        ):
            raise LandingTransactionError(
                "predecessor-candidate completion cannot reference native successor landing evidence"
            )
    elif route == "successor_landing":
        if (
            not isinstance(native_transaction, str)
            or not native_transaction.strip()
            or not isinstance(native_land, str)
            or not native_land.strip()
            or native_land != worktree_land_event_id(native_transaction)
            or not isinstance(payload.get("native_target_updated_commit"), str)
            or not str(payload["native_target_updated_commit"]).strip()
        ):
            raise LandingTransactionError(
                "successor landing completion requires exact native landing identity"
            )
    else:
        raise LandingTransactionError(
            "workspace adoption completion intent has an unknown completion route"
        )
    expected_identity = {
        "adoption_id": receipt["adoption_id"],
        "successor_attempt_id": attempt.attempt_id,
        "predecessor_attempt_id": receipt["predecessor_attempt_id"],
        "abort_transaction_id": receipt["abort_transaction_id"],
        "canonical_candidate": receipt["canonical_candidate"],
        "branch": receipt["branch"],
        "worktree_path": receipt["worktree_path"],
        "target_branch": receipt["target_branch"],
    }
    mismatches = [
        key for key, expected in expected_identity.items()
        if payload.get(key) != expected
    ]
    if mismatches:
        raise LandingTransactionError(
            "workspace adoption completion intent conflicts with adoption identity on: "
            + ", ".join(sorted(mismatches))
        )
    if expected_payload is not None and not strict_json_equal(payload, expected_payload):
        raise LandingTransactionError(
            "workspace adoption completion intent conflicts with the proven completion route"
        )


def _workspace_adoption_completion_payloads(
    *,
    attempt: Any,
    receipt: Mapping[str, Any],
    completion_intent: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_commit = str(attempt.commit or "").strip()
    landed_commit = str(attempt.landed_commit or "").strip()
    if (
        not source_commit
        or not landed_commit
        or attempt.status != ATTEMPT_STATUS_SUCCESS
        or source_commit != completion_intent.get("source_commit")
        or landed_commit != completion_intent.get("landed_commit")
        or list(attempt.changed_paths) != completion_intent.get("changed_paths")
    ):
        raise BacklogError(
            "workspace adoption completion conflicts with its durable intent"
        )
    common = {
        key: completion_intent[key]
        for key in (
            "schema_version",
            "adoption_id",
            "successor_attempt_id",
            "predecessor_attempt_id",
            "abort_transaction_id",
            "canonical_candidate",
            "source_commit",
            "source_tree_hash",
            "landed_commit",
            "branch",
            "worktree_path",
            "target_branch",
            "target_commit_at_completion",
            "native_target_updated_commit",
            "changed_paths",
            "completion_route",
            "cleanup_requested",
            "source_attribution",
            "native_landing_transaction_id",
            "native_land_event_id",
        )
    }
    land_payload = {
        **common,
        "transaction_id": receipt["adoption_id"],
        "workspace_adoption": True,
    }
    complete_payload = {
        **common,
        "land_event_id": (
            completion_intent.get("native_land_event_id")
            or _workspace_adoption_land_event_id(attempt.attempt_id)
        ),
        "complete": True,
    }
    return land_payload, complete_payload


def _exact_workspace_adoption_event(
    profile: RepoProfile,
    *,
    event_id: str,
    event_type: str,
    actor: str,
    payload: Mapping[str, Any],
) -> bool:
    matches = [
        event
        for event in load_events(profile.paths.events_file)
        if event.get("event_id") == event_id
    ]
    return len(matches) == 1 and (
        matches[0].get("type") == event_type
        and matches[0].get("actor") == actor
        and strict_json_equal(matches[0].get("payload"), dict(payload))
    )


def _append_workspace_adoption_completion(
    profile: RepoProfile,
    *,
    attempt: Any,
    receipt: Mapping[str, Any],
    completion_intent: Mapping[str, Any],
    native_transaction: LandingTransaction | None = None,
) -> tuple[bool, bool]:
    land_payload, complete_payload = _workspace_adoption_completion_payloads(
        attempt=attempt,
        receipt=receipt,
        completion_intent=completion_intent,
    )
    native_land_id = completion_intent.get("native_land_event_id")
    land_id = (
        str(native_land_id)
        if isinstance(native_land_id, str) and native_land_id
        else _workspace_adoption_land_event_id(attempt.attempt_id)
    )
    complete_id = _workspace_adoption_complete_event_id(attempt.attempt_id)
    if native_land_id:
        if (
            native_transaction is None
            or native_transaction.transaction_id
            != completion_intent.get("native_landing_transaction_id")
            or not exact_worktree_land_event(
                profile,
                intent=native_transaction.intent,
                payload=_landing_event_payload(
                    intent=native_transaction.intent,
                    landed_commit=str(completion_intent["landed_commit"]),
                ),
            )
        ):
            raise LandingTransactionError(
                "native successor worktree.land evidence conflicts with adoption completion"
            )
        land_appended = False
    else:
        land_appended = append_event_once(
            profile.paths.events_file,
            event_id=land_id,
            event_type="worktree.land",
            actor=attempt.actor,
            payload=land_payload,
        )
        if not _exact_workspace_adoption_event(
            profile,
            event_id=land_id,
            event_type="worktree.land",
            actor=attempt.actor,
            payload=land_payload,
        ):
            raise LandingTransactionError(
                "workspace adoption completion has missing or conflicting worktree.land evidence"
            )
    complete_appended = append_event_once(
        profile.paths.events_file,
        event_id=complete_id,
        event_type="worktree.adoption.complete",
        actor=attempt.actor,
        payload=complete_payload,
    )
    if not _exact_workspace_adoption_event(
        profile,
        event_id=complete_id,
        event_type="worktree.adoption.complete",
        actor=attempt.actor,
        payload=complete_payload,
    ):
        raise LandingTransactionError(
            "workspace adoption completion evidence is missing or conflicting"
        )
    return land_appended, complete_appended


def _finalize_workspace_adoption_from_intent(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt: Any,
    receipt: Mapping[str, Any],
    completion_intent: Mapping[str, Any],
    completion_transaction: LandingTransaction,
    finalize_runtime: bool,
) -> tuple[Any, bool, bool, dict[str, Any] | None, str | None]:
    runtime_state = load_runtime_state(profile.paths)
    predecessor = find_task_attempt(
        runtime_state,
        workset_id,
        str(receipt["predecessor_attempt_id"]),
    )
    if predecessor is None or predecessor.task_id != task_id:
        raise LandingTransactionError(
            "workspace adoption completion predecessor is missing"
        )
    verified_source = _verify_landing_abort_chain(
        profile,
        transaction=completion_transaction,
        require_source=False,
    )
    derived_receipt = _derive_workspace_adoption_receipt(
        predecessor=predecessor,
        transaction=completion_transaction,
        target_commit_at_adoption=str(receipt["target_commit_at_adoption"]),
    )
    if (
        verified_source != derived_receipt["source_commit"]
        or not strict_json_equal(receipt, derived_receipt)
    ):
        raise LandingTransactionError(
            "workspace adoption completion conflicts with predecessor proof"
        )
    _validate_workspace_adoption_completion_route(
        profile,
        attempt=attempt,
        receipt=receipt,
        payload=completion_intent,
        predecessor_transaction=completion_transaction,
    )
    if finalize_runtime:
        if completion_intent["completion_route"] != "predecessor_candidate_containment":
            raise LandingTransactionError(
                "only predecessor-candidate completion can finalize an active successor"
            )
        finished = finish_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
            actor=attempt.actor,
            status=ATTEMPT_STATUS_SUCCESS,
            summary=completion_transaction.intent.summary,
            changed_paths=tuple(completion_intent["changed_paths"]),
            validations=_landing_validation_records(completion_transaction.intent),
            residuals=completion_transaction.intent.residuals,
            followup_candidates=completion_transaction.intent.followup_candidates,
            commit=str(completion_intent["source_commit"]),
            landed_commit=str(completion_intent["landed_commit"]),
            note=attempt.note,
            finalization_id=str(receipt["adoption_id"]),
        )
    else:
        finished = attempt
    if (
        finished.status != ATTEMPT_STATUS_SUCCESS
        or finished.commit != completion_intent["source_commit"]
        or finished.landed_commit != completion_intent["landed_commit"]
        or list(finished.changed_paths) != completion_intent["changed_paths"]
    ):
        raise BacklogError(
            "adopted successor runtime finalization conflicts with completion intent"
        )
    land_appended, complete_appended = _append_workspace_adoption_completion(
        profile,
        attempt=finished,
        receipt=receipt,
        completion_intent=completion_intent,
    )
    cleanup_payload: dict[str, Any] | None = None
    cleanup_error: str | None = None
    if completion_intent["cleanup_requested"]:
        try:
            cleanup_payload = cleanup_task_worktree(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                path=str(completion_intent["worktree_path"]),
                branch=str(completion_intent["branch"]),
                _attempt_lock_held=True,
            )
        except (CleanupEventFinalizationError, WorktreeError) as exc:
            cleanup_error = str(exc)
            if isinstance(exc, CleanupEventFinalizationError):
                cleanup_payload = exc.partial_payload()
    return finished, land_appended, complete_appended, cleanup_payload, cleanup_error


def _normal_workspace_adoption_source_attribution(
    *,
    predecessor_transaction: LandingTransaction,
    native_transaction: LandingTransaction,
    source_commit: str,
    source_tree_hash: str,
) -> dict[str, Any]:
    return {
        "mode": "successor_landing",
        "successor_only_work_allowed": True,
        "native_landing_transaction_id": native_transaction.transaction_id,
        "native_land_event_id": worktree_land_event_id(
            native_transaction.transaction_id
        ),
        "predecessor_abort_transaction_id": predecessor_transaction.transaction_id,
        "source_commit": source_commit,
        "source_tree_hash": source_tree_hash,
        "changed_paths": list(native_transaction.intent.changed_paths),
    }


def _ensure_normal_workspace_adoption_completion_intent(
    profile: RepoProfile,
    *,
    attempt: Any,
    receipt: Mapping[str, Any],
    predecessor_transaction: LandingTransaction,
    native_transaction: LandingTransaction,
) -> tuple[dict[str, Any], bool]:
    if (
        native_transaction.intent.attempt_id != attempt.attempt_id
        or native_transaction.intent.branch != receipt["branch"]
        or native_transaction.intent.worktree_path != receipt["worktree_path"]
        or native_transaction.intent.target_branch != receipt["target_branch"]
        or predecessor_transaction.transaction_id != receipt["abort_transaction_id"]
        or predecessor_transaction.outcome != "abort_complete"
        or not {
            "source_prepared",
            "canonical_commit_created",
            "target_updated",
            "temporary_cleanup_complete",
        }.issubset(native_transaction.phases)
    ):
        raise LandingTransactionError(
            "native successor landing does not match workspace adoption identity"
        )
    source_commit = str(
        native_transaction.data_for("source_prepared").get("source_commit") or ""
    )
    landed_commit = str(
        native_transaction.data_for("canonical_commit_created").get("landed_commit")
        or ""
    )
    native_target_commit = str(
        native_transaction.data_for("target_updated").get("target_commit") or ""
    )
    if (
        not source_commit
        or not landed_commit
        or not native_target_commit
    ):
        raise LandingTransactionError(
            "native successor landing source phases are incomplete"
        )
    _manifest, source_tree_hash = _committed_tree_manifest(
        Path(native_transaction.intent.primary_worktree),
        source_commit,
    )
    stored = _load_workspace_adoption_completion_intent(
        profile,
        attempt=attempt,
        receipt=receipt,
    )
    if stored is None:
        target_commit, contains_landed = _landing_abort_target_state(
            intent=native_transaction.intent,
            landed_commit=landed_commit,
        )
        if not contains_landed:
            raise LandingTransactionError(
                "native successor target no longer contains its landed commit before completion intent"
            )
    else:
        target_commit = str(stored["target_commit_at_completion"])
    native_land_id = worktree_land_event_id(native_transaction.transaction_id)
    source_attribution = _normal_workspace_adoption_source_attribution(
        predecessor_transaction=predecessor_transaction,
        native_transaction=native_transaction,
        source_commit=source_commit,
        source_tree_hash=source_tree_hash,
    )
    expected = _workspace_adoption_completion_intent_payload(
        attempt=attempt,
        receipt=receipt,
        completion_route="successor_landing",
        source_commit=source_commit,
        source_tree_hash=source_tree_hash,
        target_commit=target_commit,
        landed_commit=landed_commit,
        changed_paths=native_transaction.intent.changed_paths,
        cleanup_requested=native_transaction.intent.cleanup,
        source_attribution=source_attribution,
        native_target_updated_commit=native_target_commit,
        native_landing_transaction_id=native_transaction.transaction_id,
        native_land_event_id=native_land_id,
    )
    _validate_workspace_adoption_completion_route(
        profile,
        attempt=attempt,
        receipt=receipt,
        payload=expected,
        predecessor_transaction=predecessor_transaction,
        native_transaction=native_transaction,
    )
    appended = False
    if stored is None:
        final_target, final_contains_landed = _landing_abort_target_state(
            intent=native_transaction.intent,
            landed_commit=landed_commit,
        )
        if final_target != target_commit or not final_contains_landed:
            raise LandingTransactionError(
                "native successor target changed before durable completion intent"
            )
        appended = _append_workspace_adoption_completion_intent(
            profile,
            attempt=attempt,
            payload=expected,
        )
        stored = _load_workspace_adoption_completion_intent(
            profile,
            attempt=attempt,
            receipt=receipt,
            expected_payload=expected,
        )
    else:
        _validate_workspace_adoption_completion_route(
            profile,
            attempt=attempt,
            receipt=receipt,
            payload=stored,
            predecessor_transaction=predecessor_transaction,
            native_transaction=native_transaction,
        )
    assert stored is not None
    return stored, appended


def _ensure_normal_workspace_adoption_complete(
    profile: RepoProfile,
    *,
    attempt: Any,
    receipt: Mapping[str, Any],
    predecessor_transaction: LandingTransaction,
    native_transaction: LandingTransaction,
) -> tuple[dict[str, Any], bool, bool]:
    stored, intent_appended = _ensure_normal_workspace_adoption_completion_intent(
        profile,
        attempt=attempt,
        receipt=receipt,
        predecessor_transaction=predecessor_transaction,
        native_transaction=native_transaction,
    )
    if attempt.status != ATTEMPT_STATUS_SUCCESS:
        raise LandingTransactionError(
            "native adoption completion marker requires terminal successor runtime"
        )
    _validate_workspace_adoption_completion_route(
        profile,
        attempt=attempt,
        receipt=receipt,
        payload=stored,
        predecessor_transaction=predecessor_transaction,
        native_transaction=native_transaction,
        require_native_land=True,
    )
    land_appended, complete_appended = _append_workspace_adoption_completion(
        profile,
        attempt=attempt,
        receipt=receipt,
        completion_intent=stored,
        native_transaction=native_transaction,
    )
    return stored, intent_appended or land_appended, complete_appended


def _special_workspace_adoption_completion_result(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt: Any,
    receipt: Mapping[str, Any],
    completion_intent: Mapping[str, Any],
    predecessor_transaction: LandingTransaction,
    proof: Mapping[str, Any],
) -> OperationResult:
    runtime_before = _read_bytes_if_present(profile.paths.runtime_file)
    events_before = _read_bytes_if_present(profile.paths.events_file)
    finished, land_appended, complete_appended, cleanup_payload, cleanup_error = (
        _finalize_workspace_adoption_from_intent(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt=attempt,
            receipt=receipt,
            completion_intent=completion_intent,
            completion_transaction=predecessor_transaction,
            finalize_runtime=attempt.status == ATTEMPT_STATUS_IN_PROGRESS,
        )
    )
    state_payload = _task_recovery_payload(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    next_action = decide_next_action(_lifecycle_context(profile, state_payload))
    mutated = (
        runtime_before != _read_bytes_if_present(profile.paths.runtime_file)
        or events_before != _read_bytes_if_present(profile.paths.events_file)
        or bool(
            isinstance(cleanup_payload, Mapping)
            and (
                cleanup_payload.get("worktree_removed")
                or cleanup_payload.get("deleted_branch")
                or cleanup_payload.get("event_appended")
            )
        )
    )
    payload: dict[str, Any] = {
        "reconciliation_id": receipt["adoption_id"],
        "workset_id": workset_id,
        "task_id": task_id,
        "attempt_id": finished.attempt_id,
        "attempt_actor": finished.actor,
        "operator_actor": finished.actor,
        "previous_status": attempt.status,
        "status": ATTEMPT_STATUS_SUCCESS,
        "landed_commit": completion_intent["landed_commit"],
        "apply": True,
        "would_change_runtime": False,
        "runtime_changed": runtime_before
        != _read_bytes_if_present(profile.paths.runtime_file),
        "land_event_appended": land_appended,
        "adoption_complete_event_appended": complete_appended,
        "completion_intent": dict(completion_intent),
        "cleanup": cleanup_payload,
        "cleanup_error": cleanup_error,
        "proof": dict(proof),
        "workspace_adoption_completion": True,
        "next_action": next_action.to_dict(),
        "recommended_commands": list(state_payload["recommended_commands"]),
        "recommended_actions": _task_surface_actions(
            list(state_payload["recommended_actions"])
        ),
    }
    return observe_operation_result(profile, OperationResult(
        operation="task.reconcile-landing",
        operation_status="partial" if cleanup_error else "succeeded",
        task_status=state_payload.get("task_runtime_status"),
        attempt_status=state_payload.get("latest_attempt_status"),
        disposition=next_action.disposition,
        mutation_started=mutated,
        mutation_completed=mutated and not cleanup_error,
        mutation_phase=(
            "runtime_finalized_cleanup_pending"
            if cleanup_error
            else "runtime_and_event_finalized"
            if mutated
            else "none"
        ),
        failure_code=None,
        next_action=next_action,
        legacy_payload=payload,
    ))


def _workspace_adoption_no_successor_work_proof(
    *,
    primary_root: Path,
    source_head: str,
    source_tree_hash: str,
    target_commit: str,
    receipt: Mapping[str, Any],
    transaction: LandingTransaction,
) -> dict[str, Any]:
    original_base = transaction.intent.target_base_commit
    original_head = str(receipt["source_commit"])
    predecessor_ancestor = _run_git_no_check(
        primary_root,
        "merge-base",
        "--is-ancestor",
        original_base,
        original_head,
    )
    if predecessor_ancestor.returncode != 0:
        if predecessor_ancestor.returncode == 1:
            raise WorktreeError(
                "adopted predecessor source does not descend from its landing target base"
            )
        detail = (
            predecessor_ancestor.stderr.strip()
            or predecessor_ancestor.stdout.strip()
        )
        raise WorktreeError(
            "could not inspect adopted predecessor range: " + detail
        )
    if (
        source_head == receipt["source_commit"]
        and source_tree_hash == receipt["source_tree_hash"]
    ):
        return {
            "mode": "exact_original_source",
            "source_ancestor_of_target": None,
            "first_parent_commit_count": None,
            "original_range_base": original_base,
            "current_range_base": original_base,
            "original_patch_id": None,
            "current_patch_id": None,
            "no_merges": None,
            "changed_paths": list(transaction.intent.changed_paths),
            "patch_equivalent": True,
            "changed_paths_match": True,
        }
    source_ancestor = _run_git_no_check(
        primary_root,
        "merge-base",
        "--is-ancestor",
        source_head,
        target_commit,
    )
    if source_ancestor.returncode not in {0, 1}:
        detail = source_ancestor.stderr.strip() or source_ancestor.stdout.strip()
        raise WorktreeError(
            "could not inspect rebased adopted source ancestry: " + detail
        )
    _target_manifest, target_tree_hash = _committed_tree_manifest(
        primary_root,
        target_commit,
    )
    source_tree_equals_target = source_tree_hash == target_tree_hash
    if source_ancestor.returncode == 1 and not source_tree_equals_target:
        raise WorktreeError(
            "rebased adopted successor source is not proven present on target"
        )
    count_text = _run_git(
        primary_root,
        "rev-list",
        "--first-parent",
        "--count",
        f"{original_base}..{original_head}",
    )
    try:
        commit_count = int(count_text)
    except ValueError as exc:
        raise WorktreeError("adopted predecessor commit count is invalid") from exc
    if commit_count < 1 or commit_count > 256:
        raise WorktreeError("adopted predecessor commit range is not bounded")
    if _run_git(
        primary_root,
        "rev-list",
        "--merges",
        f"{original_base}..{original_head}",
    ):
        raise WorktreeError("adopted predecessor range contains merge commits")
    rebased_base = _run_git(
        primary_root,
        "rev-parse",
        f"{source_head}~{commit_count}",
    )
    if _run_git(
        primary_root,
        "rev-list",
        "--merges",
        f"{rebased_base}..{source_head}",
    ):
        raise WorktreeError("rebased adopted range contains merge commits")
    rebased_count = int(
        _run_git(
            primary_root,
            "rev-list",
            "--first-parent",
            "--count",
            f"{rebased_base}..{source_head}",
        )
    )
    if rebased_count != commit_count:
        raise WorktreeError("rebased adopted range commit count changed")
    original_patch = _stable_diff_patch_id(
        primary_root,
        original_base,
        original_head,
    )
    rebased_patch = _stable_diff_patch_id(
        primary_root,
        rebased_base,
        source_head,
    )
    if original_patch != rebased_patch:
        raise WorktreeError(
            "rebased adopted range is not patch-equivalent to the predecessor"
        )
    rebased_paths = tuple(
        sorted(
            line.strip()
            for line in _run_git(
                primary_root,
                "diff",
                "--name-only",
                rebased_base,
                source_head,
            ).splitlines()
            if line.strip()
        )
    )
    if rebased_paths != tuple(sorted(transaction.intent.changed_paths)):
        raise WorktreeError(
            "rebased adopted range changed paths differ from predecessor intent"
        )
    return {
        "mode": "patch_equivalent_rebase",
        "source_ancestor_of_target": source_ancestor.returncode == 0,
        "source_tree_equals_target": source_tree_equals_target,
        "source_presence_mode": (
            "ancestor" if source_ancestor.returncode == 0 else "exact_target_tree"
        ),
        "first_parent_commit_count": commit_count,
        "original_range_base": original_base,
        "current_range_base": rebased_base,
        "original_patch_id": original_patch,
        "current_patch_id": rebased_patch,
        "no_merges": True,
        "changed_paths": list(rebased_paths),
        "patch_equivalent": True,
        "changed_paths_match": True,
    }


def _validate_workspace_adoption_completion_route(
    profile: RepoProfile,
    *,
    attempt: Any,
    receipt: Mapping[str, Any],
    payload: Mapping[str, Any],
    predecessor_transaction: LandingTransaction,
    native_transaction: LandingTransaction | None = None,
    require_native_land: bool = False,
) -> None:
    _validate_workspace_adoption_completion_intent(
        attempt=attempt,
        receipt=receipt,
        payload=payload,
    )
    if (
        predecessor_transaction.transaction_id != receipt["abort_transaction_id"]
        or predecessor_transaction.outcome != "abort_complete"
        or predecessor_transaction.abort_data is None
    ):
        raise LandingTransactionError(
            "workspace adoption completion predecessor transaction is not canonical"
        )
    runtime_state = load_runtime_state(profile.paths)
    predecessor = find_task_attempt(
        runtime_state,
        predecessor_transaction.intent.workset_id,
        str(receipt["predecessor_attempt_id"]),
    )
    if (
        predecessor is None
        or predecessor.task_id != predecessor_transaction.intent.task_id
    ):
        raise LandingTransactionError(
            "workspace adoption completion predecessor runtime is missing"
        )
    verified_source = _verify_landing_abort_chain(
        profile,
        transaction=predecessor_transaction,
        require_source=False,
    )
    derived_receipt = _derive_workspace_adoption_receipt(
        predecessor=predecessor,
        transaction=predecessor_transaction,
        target_commit_at_adoption=str(receipt["target_commit_at_adoption"]),
    )
    if (
        verified_source != derived_receipt["source_commit"]
        or not strict_json_equal(receipt, derived_receipt)
    ):
        raise LandingTransactionError(
            "workspace adoption completion receipt conflicts with predecessor evidence"
        )
    primary_root = Path(predecessor_transaction.intent.primary_worktree)
    route = payload["completion_route"]
    if route == "predecessor_candidate_containment":
        source_commit = str(payload["source_commit"])
        target_commit = str(payload["target_commit_at_completion"])
        _manifest, source_tree_hash = _committed_tree_manifest(
            primary_root,
            source_commit,
        )
        if not _target_contains_landed_commit(
            primary_root,
            target_commit=target_commit,
            landed_commit=str(receipt["canonical_candidate"]),
        ):
            raise LandingTransactionError(
                "special adoption completion target does not contain the predecessor candidate"
            )
        source_attribution = _workspace_adoption_no_successor_work_proof(
            primary_root=primary_root,
            source_head=source_commit,
            source_tree_hash=source_tree_hash,
            target_commit=target_commit,
            receipt=receipt,
            transaction=predecessor_transaction,
        )
        expected = _workspace_adoption_completion_intent_payload(
            attempt=attempt,
            receipt=receipt,
            completion_route="predecessor_candidate_containment",
            source_commit=source_commit,
            source_tree_hash=source_tree_hash,
            target_commit=target_commit,
            landed_commit=str(receipt["canonical_candidate"]),
            changed_paths=predecessor_transaction.intent.changed_paths,
            cleanup_requested=predecessor_transaction.intent.cleanup,
            source_attribution=source_attribution,
        )
    else:
        if (
            native_transaction is None
            or native_transaction.transaction_id
            != payload["native_landing_transaction_id"]
            or native_transaction.intent.attempt_id != attempt.attempt_id
            or native_transaction.intent.actor != attempt.actor
            or native_transaction.intent.branch != receipt["branch"]
            or native_transaction.intent.worktree_path != receipt["worktree_path"]
            or native_transaction.intent.target_branch != receipt["target_branch"]
            or not {
                "source_prepared",
                "canonical_commit_created",
                "target_updated",
                "temporary_cleanup_complete",
            }.issubset(native_transaction.phases)
        ):
            raise LandingTransactionError(
                "normal adoption completion has no exact native landing transaction"
            )
        source_commit = _verify_landing_source_phase(
            profile,
            intent=native_transaction.intent,
            transaction=native_transaction,
            require_branch=False,
        )
        landed_commit = _verify_canonical_landing_phase(
            intent=native_transaction.intent,
            transaction=native_transaction,
        )
        native_target_commit = _verify_target_updated_phase(
            profile,
            intent=native_transaction.intent,
            transaction=native_transaction,
            landed_commit=landed_commit,
            require_live_target=False,
        )
        _verify_temporary_landing_cleanup_phase(
            intent=native_transaction.intent,
            transaction=native_transaction,
            landed_commit=landed_commit,
        )
        target_commit = str(payload["target_commit_at_completion"])
        if not _target_contains_landed_commit(
            Path(native_transaction.intent.primary_worktree),
            target_commit=target_commit,
            landed_commit=landed_commit,
        ):
            raise LandingTransactionError(
                "normal adoption completion final target proof does not contain its landed commit"
            )
        _manifest, source_tree_hash = _committed_tree_manifest(
            Path(native_transaction.intent.primary_worktree),
            source_commit,
        )
        source_attribution = _normal_workspace_adoption_source_attribution(
            predecessor_transaction=predecessor_transaction,
            native_transaction=native_transaction,
            source_commit=source_commit,
            source_tree_hash=source_tree_hash,
        )
        expected = _workspace_adoption_completion_intent_payload(
            attempt=attempt,
            receipt=receipt,
            completion_route="successor_landing",
            source_commit=source_commit,
            source_tree_hash=source_tree_hash,
            target_commit=target_commit,
            landed_commit=landed_commit,
            changed_paths=native_transaction.intent.changed_paths,
            cleanup_requested=native_transaction.intent.cleanup,
            source_attribution=source_attribution,
            native_target_updated_commit=native_target_commit,
            native_landing_transaction_id=native_transaction.transaction_id,
            native_land_event_id=worktree_land_event_id(
                native_transaction.transaction_id
            ),
        )
        if require_native_land and not exact_worktree_land_event(
            profile,
            intent=native_transaction.intent,
            payload=_landing_event_payload(
                intent=native_transaction.intent,
                landed_commit=landed_commit,
            ),
        ):
            raise LandingTransactionError(
                "normal adoption completion native worktree.land event conflicts"
            )
        if require_native_land:
            runtime_data = native_transaction.data_for("runtime_finalized")
            expected_runtime_data = {
                "finalization_id": native_transaction.transaction_id,
                "attempt_id": attempt.attempt_id,
                "status": ATTEMPT_STATUS_SUCCESS,
                "source_commit": source_commit,
                "landed_commit": landed_commit,
                "ended_at": attempt.ended_at,
            }
            if (
                attempt.status != ATTEMPT_STATUS_SUCCESS
                or not strict_json_equal(runtime_data, expected_runtime_data)
            ):
                raise LandingTransactionError(
                    "normal adoption completion runtime phase conflicts with successor"
                )
            _verify_landing_event_phase(
                profile,
                intent=native_transaction.intent,
                transaction=native_transaction,
                landed_commit=landed_commit,
            )
    if not strict_json_equal(payload, expected):
        raise LandingTransactionError(
            "workspace adoption completion intent conflicts with immutable route evidence"
        )


def _reconcile_adopted_successor_landing(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt: Any,
    runtime_state: Any,
    receipt: Mapping[str, Any],
    landed_commit: str,
    actor: str,
    apply: bool,
    reason: str | None,
) -> OperationResult:
    if actor != attempt.actor:
        raise BacklogError(
            f"Adopted successor {attempt.attempt_id!r} is owned by {attempt.actor}, not {actor}"
        )
    active = active_task_attempt(runtime_state, workset_id, task_id)
    claim = task_claim_index(runtime_state, workset_id).get(task_id)
    if (
        active is None
        or active.attempt_id != attempt.attempt_id
        or claim is None
        or claim.attempt_id != attempt.attempt_id
        or claim.actor != attempt.actor
        or claim.execution_model != attempt.execution_model
    ):
        raise BacklogError("adopted successor does not own the exact active task claim")
    predecessor = find_task_attempt(
        runtime_state,
        workset_id,
        str(receipt["predecessor_attempt_id"]),
    )
    if predecessor is None or predecessor.task_id != task_id:
        raise BacklogError("adopted successor predecessor is missing")
    transaction = load_landing_transaction(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=predecessor.attempt_id,
    )
    if (
        transaction is None
        or transaction.transaction_id != receipt["abort_transaction_id"]
        or transaction.outcome != "abort_complete"
    ):
        raise LandingTransactionError(
            "adopted successor completion requires its exact abort-complete transaction"
        )
    verified_source = _verify_landing_abort_chain(
        profile,
        transaction=transaction,
        require_source=False,
    )
    derived = _derive_workspace_adoption_receipt(
        predecessor=predecessor,
        transaction=transaction,
        target_commit_at_adoption=str(receipt["target_commit_at_adoption"]),
    )
    if verified_source != derived["source_commit"] or not strict_json_equal(
        receipt,
        derived,
    ):
        raise LandingTransactionError(
            "adopted successor completion conflicts with immutable adoption evidence"
        )
    start_state, start_issue = _workspace_adoption_start_evidence(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        runtime_state=runtime_state,
        successor=attempt,
        predecessor=predecessor,
        receipt=receipt,
        transaction=transaction,
    )
    if start_state != "complete":
        raise BacklogError(
            start_issue or "adopted successor start evidence requires repair"
        )
    candidate = str(receipt["canonical_candidate"])
    resolved_requested = _inspect_commit(
        Path(transaction.intent.primary_worktree),
        landed_commit,
        role="landed_commit",
    ).resolved_commit
    if resolved_requested != candidate:
        raise LandingTransactionError(
            "adopted successor completion must use the predecessor canonical candidate"
        )
    existing_completion_intent = _load_workspace_adoption_completion_intent(
        profile,
        attempt=attempt,
        receipt=receipt,
    )
    if existing_completion_intent is not None:
        _validate_workspace_adoption_completion_route(
            profile,
            attempt=attempt,
            receipt=receipt,
            payload=existing_completion_intent,
            predecessor_transaction=transaction,
        )
        if (
            existing_completion_intent["completion_route"]
            != "predecessor_candidate_containment"
            or existing_completion_intent["landed_commit"] != candidate
        ):
            raise LandingTransactionError(
                "adopted successor reconciliation conflicts with durable completion intent"
            )
        if not apply:
            proof = {
                "workspace_adoption": dict(receipt),
                "target_commit": existing_completion_intent[
                    "target_commit_at_completion"
                ],
                "target_contains_candidate": True,
                "source_head": existing_completion_intent["source_commit"],
                "source_tree_hash": existing_completion_intent["source_tree_hash"],
                "source_attribution": dict(
                    existing_completion_intent["source_attribution"]
                ),
                "changed_paths": list(existing_completion_intent["changed_paths"]),
                "successor_only_work_absent": True,
                "durable_completion_intent": True,
            }
            action = LifecycleAction(
                action_id="apply_adopted_successor_completion",
                disposition="proof_verified",
                reason_code="adopted_completion_intent_recorded",
                reason_detail="The durable adopted-successor completion intent is ready to finalize.",
                argv=(
                    _lifecycle_blackdog_executable(
                        profile,
                        {"worktree_path": str(receipt["worktree_path"])},
                    ),
                    "task",
                    "reconcile-landing",
                    f"--project-root={transaction.intent.primary_worktree}",
                    f"--workset={workset_id}",
                    f"--task={task_id}",
                    f"--attempt={attempt.attempt_id}",
                    f"--landed-commit={candidate}",
                    f"--actor={attempt.actor}",
                    "--apply",
                ),
                safety_class="proof_guarded_mutation",
                mutation_class="git_and_runtime",
                display="Finalize the adopted successor",
            )
            next_action = NextAction.command(action)
            task_record = task_state_index(runtime_state, workset_id).get(task_id)
            return observe_operation_result(profile, OperationResult(
                operation="task.reconcile-landing",
                operation_status="observed",
                task_status=task_record.status if task_record is not None else None,
                attempt_status=attempt.status,
                disposition=next_action.disposition,
                mutation_started=False,
                mutation_completed=False,
                mutation_phase="proof_verified",
                failure_code=None,
                next_action=next_action,
                legacy_payload={
                    "workset_id": workset_id,
                    "task_id": task_id,
                    "attempt_id": attempt.attempt_id,
                    "status": "ready",
                    "apply": False,
                    "landed_commit": candidate,
                    "proof": proof,
                    "workspace_adoption_completion": True,
                    "next_action": next_action.to_dict(),
                    "recommended_commands": next_action.legacy_command_rows(),
                    "recommended_actions": [next_action.display],
                },
            ))
        return _special_workspace_adoption_completion_result(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt=attempt,
            receipt=receipt,
            completion_intent=existing_completion_intent,
            predecessor_transaction=transaction,
            proof={
                "workspace_adoption": dict(receipt),
                "target_commit": existing_completion_intent[
                    "target_commit_at_completion"
                ],
                "target_contains_candidate": True,
                "source_head": existing_completion_intent["source_commit"],
                "source_tree_hash": existing_completion_intent["source_tree_hash"],
                "source_attribution": dict(
                    existing_completion_intent["source_attribution"]
                ),
                "changed_paths": list(existing_completion_intent["changed_paths"]),
                "successor_only_work_absent": True,
                "durable_completion_intent": True,
            },
        )
    primary_root = Path(transaction.intent.primary_worktree)
    target = _inspect_branch_ref(
        primary_root,
        str(receipt["target_branch"]),
        role="target_branch",
    )
    if target.state == "error":
        raise _inspection_error(target)
    target_commit = target.resolved_commit
    if target_commit is None or not _target_contains_landed_commit(
        primary_root,
        target_commit=target_commit,
        landed_commit=candidate,
    ):
        raise WorktreeError("target does not contain the adopted canonical candidate")
    source_path = Path(str(receipt["worktree_path"]))
    branch = _inspect_branch_ref(
        primary_root,
        str(receipt["branch"]),
        role="task_branch",
    )
    if branch.state == "error":
        raise _inspection_error(branch)
    registration = _registered_worktree_row(primary_root, source_path)
    source_head = branch.resolved_commit
    if (
        source_head is None
        or registration is None
        or registration.get("branch") != f"refs/heads/{receipt['branch']}"
        or str(registration.get("HEAD") or "").strip() != source_head
        or not source_path.exists()
        or _run_git(source_path, "rev-parse", "HEAD") != source_head
        or _managed_status_dirty(profile, source_path)
        or _in_progress_git_operation(source_path) is not None
    ):
        raise WorktreeError(
            "adopted successor workspace is not clean and coherently registered"
        )
    _source_manifest, source_tree_hash = _committed_tree_manifest(
        primary_root,
        source_head,
    )
    _projected_manifest, projected_tree_hash = _projected_source_tree_manifest(
        source_path
    )
    if projected_tree_hash != source_tree_hash:
        raise WorktreeError(
            "adopted successor projected source differs from its committed tree"
        )
    source_attribution = _workspace_adoption_no_successor_work_proof(
        primary_root=primary_root,
        source_head=source_head,
        source_tree_hash=source_tree_hash,
        target_commit=target_commit,
        receipt=receipt,
        transaction=transaction,
    )
    trailers = _canonical_trailer_values(primary_root, candidate)
    expected_trailers = {
        "Blackdog-Workset": workset_id,
        "Blackdog-Task": task_id,
        "Blackdog-Attempt": predecessor.attempt_id,
        "Blackdog-Actor": predecessor.actor,
        "Blackdog-Status": ATTEMPT_STATUS_SUCCESS,
        "Blackdog-Target-Branch": transaction.intent.target_branch,
    }
    for key, expected in expected_trailers.items():
        _require_exact_canonical_trailer(trailers, key=key, expected=expected)
    commit_format = _require_supported_canonical_commit_format(trailers)
    trailer_paths = tuple(sorted(trailers.get("Blackdog-Changed-Path", ())))
    if trailer_paths != tuple(sorted(transaction.intent.changed_paths)):
        raise WorktreeError(
            "adopted canonical candidate changed-path trailers conflict with its predecessor intent"
        )
    proof = {
        "workspace_adoption": dict(receipt),
        "target_commit": target_commit,
        "target_contains_candidate": True,
        "source_head": source_head,
        "source_tree_hash": source_tree_hash,
        "source_attribution": source_attribution,
        "canonical_trailers": expected_trailers,
        "commit_format": commit_format,
        "changed_paths": list(transaction.intent.changed_paths),
        "successor_only_work_absent": True,
    }
    executable = _lifecycle_blackdog_executable(
        profile,
        {"worktree_path": str(source_path)},
    )
    apply_argv = (
        executable,
        "task",
        "reconcile-landing",
        f"--project-root={transaction.intent.primary_worktree}",
        f"--workset={workset_id}",
        f"--task={task_id}",
        f"--attempt={attempt.attempt_id}",
        f"--landed-commit={candidate}",
        f"--actor={attempt.actor}",
        *((f"--reason={reason}",) if reason else ()),
        "--apply",
    )
    payload: dict[str, Any] = {
        "reconciliation_id": receipt["adoption_id"],
        "workset_id": workset_id,
        "task_id": task_id,
        "attempt_id": attempt.attempt_id,
        "attempt_actor": attempt.actor,
        "operator_actor": actor,
        "previous_status": attempt.status,
        "status": "ready" if not apply else ATTEMPT_STATUS_SUCCESS,
        "landed_commit": candidate,
        "apply": apply,
        "would_change_runtime": not apply,
        "proof": proof,
        "workspace_adoption_completion": True,
    }
    if not apply:
        action = LifecycleAction(
            action_id="apply_adopted_successor_completion",
            disposition="proof_verified",
            reason_code="adopted_candidate_completion_proven",
            reason_detail="The predecessor candidate and absence of successor-only work are proven.",
            argv=apply_argv,
            safety_class="proof_guarded_mutation",
            mutation_class="git_and_runtime",
            display="Finalize the adopted successor",
        )
        next_action = NextAction.command(action)
        payload["next_action"] = next_action.to_dict()
        payload["recommended_commands"] = next_action.legacy_command_rows()
        payload["recommended_actions"] = [next_action.display]
        task_record = task_state_index(runtime_state, workset_id).get(task_id)
        return observe_operation_result(profile, OperationResult(
            operation="task.reconcile-landing",
            operation_status="observed",
            task_status=task_record.status if task_record is not None else None,
            attempt_status=attempt.status,
            disposition=next_action.disposition,
            mutation_started=False,
            mutation_completed=False,
            mutation_phase="proof_verified",
            failure_code=None,
            next_action=next_action,
            legacy_payload=payload,
        ))

    # This is the special completion transaction boundary.  Re-read target and
    # source immediately before persisting the intent; after the intent exists,
    # retries use its frozen proof even if target moves again.
    final_target_commit, final_contains_candidate = _landing_abort_target_state(
        intent=transaction.intent,
        landed_commit=candidate,
    )
    if not final_contains_candidate:
        raise WorktreeError("target does not contain the adopted canonical candidate")
    final_source_head = _run_git(source_path, "rev-parse", "HEAD")
    if (
        final_source_head != _run_git(primary_root, "rev-parse", str(receipt["branch"]))
        or _managed_status_dirty(profile, source_path)
        or _in_progress_git_operation(source_path) is not None
    ):
        raise WorktreeError(
            "adopted successor workspace changed before completion intent"
        )
    _final_manifest, final_source_tree_hash = _committed_tree_manifest(
        primary_root,
        final_source_head,
    )
    _final_projected, final_projected_tree_hash = _projected_source_tree_manifest(
        source_path
    )
    if final_projected_tree_hash != final_source_tree_hash:
        raise WorktreeError(
            "adopted successor projected source changed before completion intent"
        )
    final_source_attribution = _workspace_adoption_no_successor_work_proof(
        primary_root=primary_root,
        source_head=final_source_head,
        source_tree_hash=final_source_tree_hash,
        target_commit=final_target_commit,
        receipt=receipt,
        transaction=transaction,
    )
    completion_intent = _workspace_adoption_completion_intent_payload(
        attempt=attempt,
        receipt=receipt,
        completion_route="predecessor_candidate_containment",
        source_commit=final_source_head,
        source_tree_hash=final_source_tree_hash,
        target_commit=final_target_commit,
        landed_commit=candidate,
        changed_paths=transaction.intent.changed_paths,
        cleanup_requested=transaction.intent.cleanup,
        source_attribution=final_source_attribution,
    )
    _validate_workspace_adoption_completion_route(
        profile,
        attempt=attempt,
        receipt=receipt,
        payload=completion_intent,
        predecessor_transaction=transaction,
    )
    intent_target_commit, intent_contains_candidate = _landing_abort_target_state(
        intent=transaction.intent,
        landed_commit=candidate,
    )
    if (
        intent_target_commit != final_target_commit
        or not intent_contains_candidate
    ):
        raise LandingTransactionError(
            "adopted successor target changed before durable completion intent"
        )
    _append_workspace_adoption_completion_intent(
        profile,
        attempt=attempt,
        payload=completion_intent,
    )
    completion_intent = _load_workspace_adoption_completion_intent(
        profile,
        attempt=attempt,
        receipt=receipt,
        expected_payload=completion_intent,
    )
    assert completion_intent is not None
    proof.update(
        target_commit=final_target_commit,
        source_head=final_source_head,
        source_tree_hash=final_source_tree_hash,
        source_attribution=final_source_attribution,
        durable_completion_intent=True,
    )
    return _special_workspace_adoption_completion_result(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt=attempt,
        receipt=receipt,
        completion_intent=completion_intent,
        predecessor_transaction=transaction,
        proof=proof,
    )


def reconcile_task_landing(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    landed_commit: str,
    actor: str,
    apply: bool = False,
    reason: str | None = None,
    _attempt_lock_held: bool = False,
) -> OperationResult:
    """Prove and optionally reconcile a canonical Git landing into runtime state."""
    close_gate = _incomplete_close_gate(
        profile,
        operation="task.reconcile-landing",
        workset_id=workset_id,
        task_id=task_id,
        actor=actor,
    )
    if close_gate is not None:
        return close_gate
    if apply and not _attempt_lock_held:
        with attempt_lifecycle_lock(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
        ):
            return reconcile_task_landing(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=attempt_id,
                landed_commit=landed_commit,
                actor=actor,
                apply=apply,
                reason=reason,
                _attempt_lock_held=True,
            )
    _require_workset_and_task(profile, workset_id=workset_id, task_id=task_id)
    runtime_state = load_runtime_state(profile.paths)
    attempt = find_task_attempt(runtime_state, workset_id, attempt_id)
    if attempt is None:
        raise BacklogError(f"Unknown attempt {attempt_id!r} in workset {workset_id!r}")
    if attempt.task_id != task_id:
        raise BacklogError(f"Attempt {attempt_id!r} does not belong to task {task_id!r}")
    latest_attempt = latest_task_attempt(runtime_state, workset_id, task_id)
    if latest_attempt is None or latest_attempt.attempt_id != attempt_id:
        latest_id = latest_attempt.attempt_id if latest_attempt is not None else "unknown"
        raise BacklogError(
            f"Attempt {attempt_id!r} is not the latest attempt for task {task_id!r}; latest is {latest_id!r}"
        )
    start_gate = _task_start_terminal_gate(
        profile,
        operation="task.reconcile-landing",
        workset_id=workset_id,
        task_id=task_id,
        attempt=attempt,
        actor=actor,
        completion_request_identity={
            "operation": "task.reconcile-landing",
            "workset_id": workset_id,
            "task_id": task_id,
            "actor": actor,
            "attempt_id": attempt_id,
            "landed_commit": landed_commit,
            "apply": apply,
        },
    )
    if start_gate is not None:
        return start_gate
    adoption_receipt = _workspace_adoption_receipt(attempt)
    if attempt.status == ATTEMPT_STATUS_SUCCESS and adoption_receipt is not None:
        completion_intent = _load_workspace_adoption_completion_intent(
            profile,
            attempt=attempt,
            receipt=adoption_receipt,
        )
        if completion_intent is None:
            raise LandingTransactionError(
                "terminal adopted successor is missing durable completion intent"
            )
        resolved_requested = _inspect_commit(
            profile.paths.project_root,
            landed_commit,
            role="landed_commit",
        ).resolved_commit
        if (
            actor != attempt.actor
            or resolved_requested != completion_intent["landed_commit"]
        ):
            raise LandingTransactionError(
                "terminal adoption completion retry conflicts with durable intent"
            )
        if (
            completion_intent["completion_route"]
            == "predecessor_candidate_containment"
        ):
            predecessor = find_task_attempt(
                runtime_state,
                workset_id,
                str(adoption_receipt["predecessor_attempt_id"]),
            )
            if predecessor is None or predecessor.task_id != task_id:
                raise LandingTransactionError(
                    "terminal adoption completion predecessor is missing"
                )
            predecessor_transaction = load_landing_transaction(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=predecessor.attempt_id,
            )
            if (
                predecessor_transaction is None
                or predecessor_transaction.transaction_id
                != adoption_receipt["abort_transaction_id"]
                or predecessor_transaction.outcome != "abort_complete"
            ):
                raise LandingTransactionError(
                    "terminal adoption completion predecessor transaction conflicts"
                )
            return _special_workspace_adoption_completion_result(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt=attempt,
                receipt=adoption_receipt,
                completion_intent=completion_intent,
                predecessor_transaction=predecessor_transaction,
                proof={
                    "workspace_adoption": dict(adoption_receipt),
                    "target_commit": completion_intent[
                        "target_commit_at_completion"
                    ],
                    "target_contains_candidate": True,
                    "source_head": completion_intent["source_commit"],
                    "source_tree_hash": completion_intent["source_tree_hash"],
                    "source_attribution": dict(
                        completion_intent["source_attribution"]
                    ),
                    "changed_paths": list(completion_intent["changed_paths"]),
                    "successor_only_work_absent": True,
                    "durable_completion_intent": True,
                },
            )
    if (
        attempt.status == ATTEMPT_STATUS_IN_PROGRESS
        and adoption_receipt is not None
    ):
        return _reconcile_adopted_successor_landing(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt=attempt,
            runtime_state=runtime_state,
            receipt=adoption_receipt,
            landed_commit=landed_commit,
            actor=actor,
            apply=apply,
            reason=reason,
        )
    if (
        attempt.status == ATTEMPT_STATUS_IN_PROGRESS
        and isinstance(attempt.setup_receipt, Mapping)
        and "workspace_adoption" in attempt.setup_receipt
    ):
        raise LandingTransactionError(
            "active adopted successor has malformed durable adoption evidence"
        )
    native_transaction = load_landing_transaction(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    if attempt.status == ATTEMPT_STATUS_ABANDONED and (
        native_transaction is None
        or native_transaction.outcome != "abort_complete"
    ):
        raise LandingTransactionError(
            "abandoned landing reconciliation requires the exact terminal native abort-complete transaction"
        )
    if native_transaction is not None and not native_transaction.terminal:
        action = (
            _landing_abort_close_action(native_transaction)
            if native_transaction.aborted
            else _landing_resume_action(native_transaction.intent)
        )
        next_action = NextAction.command(action)
        payload = {
            "reconciliation_id": None,
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "attempt_actor": attempt.actor,
            "operator_actor": actor,
            "commit_actor": None,
            "previous_status": attempt.status,
            "status": "native_landing_transaction_incomplete",
            "landed_commit": landed_commit,
            "apply": apply,
            "would_change_runtime": False,
            "proof": {"native_landing_transaction": native_transaction.to_dict()},
            "landing_transaction": native_transaction.to_dict(),
            "next_action": next_action.to_dict(),
            "recommended_commands": next_action.legacy_command_rows(),
            "recommended_actions": [action.display],
        }
        task_record = task_state_index(runtime_state, workset_id).get(task_id)
        return observe_operation_result(profile, OperationResult(
            operation="task.reconcile-landing",
            operation_status="blocked" if apply else "observed",
            task_status=task_record.status if task_record is not None else None,
            attempt_status=attempt.status,
            disposition=next_action.disposition,
            mutation_started=False,
            mutation_completed=False,
            mutation_phase=_landing_operation_phase(native_transaction),
            failure_code=None,
            next_action=next_action,
            legacy_payload=payload,
        ))
    abandoned_eligibility: AbandonedLandingEligibility | None = None
    if (
        native_transaction is not None
        and native_transaction.outcome == "abort_complete"
    ):
        _verify_landing_abort_chain(
            profile,
            transaction=native_transaction,
            require_source=_aborted_source_requires_live_proof(
                profile,
                transaction=native_transaction,
            ),
        )
        recorded_candidate = (
            native_transaction.abort_data.get("landed_commit")
            if native_transaction.abort_data is not None
            else None
        )
        if (
            not isinstance(recorded_candidate, str)
            or recorded_candidate.lower() != str(landed_commit).strip().lower()
        ):
            raise LandingTransactionError(
                "abort-complete reconciliation must use its exact recorded canonical candidate"
            )
        abandoned_eligibility = AbandonedLandingEligibility(
            attempt_id=attempt_id,
            transaction_id=native_transaction.transaction_id,
            canonical_candidate=recorded_candidate,
        )
    if task_id in task_claim_index(runtime_state, workset_id):
        raise BacklogError(f"Task {task_id!r} has an active claim and cannot be reconciled")
    if attempt.ended_at is None:
        raise BacklogError(f"Attempt {attempt_id!r} is not terminal")
    if attempt.status == ATTEMPT_STATUS_SUCCESS:
        if str(attempt.landed_commit or "").strip().lower() != str(landed_commit).strip().lower():
            raise BacklogError(f"Attempt {attempt_id!r} is already successful with a different landed commit")
    elif attempt.status not in {
        ATTEMPT_STATUS_BLOCKED,
        ATTEMPT_STATUS_FAILED,
        ATTEMPT_STATUS_ABANDONED,
    }:
        raise BacklogError(
            f"Attempt {attempt_id!r} status {attempt.status!r} is not failed, blocked, or abandoned"
        )
    elif attempt.landed_commit:
        raise BacklogError(
            f"Attempt {attempt_id!r} already records landed commit {attempt.landed_commit!r}"
        )
    if not attempt.target_branch:
        raise BacklogError(f"Attempt {attempt_id!r} is missing its target branch")

    candidate_proof = _prove_landing_reconciliation_candidate(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt=attempt,
        landed_commit=landed_commit,
    )
    reconciliation_id = str(candidate_proof["reconciliation_id"])
    resolved_landed_commit = str(candidate_proof["resolved_landed_commit"])
    commit_actor = str(candidate_proof["commit_actor"])
    proof = dict(candidate_proof["proof"])
    changed_paths = tuple(proof["changed_paths"])
    payload: dict[str, Any] = {
        "reconciliation_id": reconciliation_id,
        "workset_id": workset_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "attempt_actor": attempt.actor,
        "operator_actor": actor,
        "commit_actor": commit_actor,
        "previous_status": attempt.status,
        "status": "ready" if attempt.status != ATTEMPT_STATUS_SUCCESS else "already_reconciled",
        "landed_commit": resolved_landed_commit,
        "apply": apply,
        "would_change_runtime": attempt.status != ATTEMPT_STATUS_SUCCESS,
        "proof": proof,
    }
    would_change_runtime = bool(payload["would_change_runtime"])
    if not apply:
        if would_change_runtime:
            executable = _lifecycle_blackdog_executable(
                profile,
                {"worktree_path": attempt.worktree_path},
            )
            apply_argv = (
                executable,
                "task",
                "reconcile-landing",
                f"--project-root={profile.paths.project_root}",
                f"--workset={workset_id}",
                f"--task={task_id}",
                f"--attempt={attempt_id}",
                f"--landed-commit={resolved_landed_commit}",
                f"--actor={actor}",
                *((f"--reason={reason}",) if reason else ()),
                "--apply",
            )
            apply_action = LifecycleAction(
                action_id="apply_landing_reconciliation",
                disposition="proof_verified",
                reason_code="canonical_landing_proven",
                reason_detail="Canonical commit proof passed; runtime reconciliation is ready to apply.",
                argv=apply_argv,
                safety_class="proof_guarded_mutation",
                mutation_class="runtime",
                display="Apply the proven landing reconciliation",
            )
            next_action = NextAction.command(apply_action)
        else:
            next_action = NextAction.terminal(
                action_id="landing_reconciliation_complete",
                kind="complete",
                disposition="complete",
                reason_code="runtime_already_reconciled",
                reason_detail="Runtime already records the proven canonical landing.",
                display="No reconciliation mutation is required",
            )
        payload["next_action"] = next_action.to_dict()
        payload["recommended_commands"] = next_action.legacy_command_rows()
        payload["recommended_actions"] = (
            [] if next_action.kind == "complete" else [next_action.display]
        )
        task_record = task_state_index(runtime_state, workset_id).get(task_id)
        return observe_operation_result(profile, OperationResult(
            operation="task.reconcile-landing",
            operation_status="observed",
            task_status=task_record.status if task_record is not None else None,
            attempt_status=attempt.status,
            disposition=next_action.disposition,
            mutation_started=False,
            mutation_completed=False,
            mutation_phase="proof_verified",
            failure_code=None,
            next_action=next_action,
            legacy_payload=payload,
        ))

    correction = reconcile_landed_attempt(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
        landed_commit=resolved_landed_commit,
        actor=actor,
        changed_paths=changed_paths,
        reason=reason,
        proof=proof,
        abandoned_eligibility=abandoned_eligibility,
    )
    payload.update(correction)
    payload["apply"] = True
    payload["status"] = ATTEMPT_STATUS_SUCCESS
    payload["would_change_runtime"] = False
    runtime_changed = bool(correction.get("runtime_changed"))
    event_appended = bool(correction.get("event_appended"))
    native_land_event_appended = False
    native_cleanup_payload: dict[str, Any] | None = None
    native_cleanup_error: str | None = None
    if (
        native_transaction is not None
        and native_transaction.outcome == "abort_complete"
    ):
        intent = native_transaction.intent
        land_payload = _landing_event_payload(
            intent=intent,
            landed_commit=resolved_landed_commit,
        )
        native_land_event_appended = append_worktree_land_once(
            profile,
            intent=intent,
            payload=land_payload,
        )
        if not exact_worktree_land_event(
            profile,
            intent=intent,
            payload=land_payload,
        ):
            raise LandingTransactionError(
                "late landing reconciliation did not retain exact worktree.land evidence"
            )
        if intent.cleanup:
            try:
                native_cleanup_payload = cleanup_task_worktree(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    path=intent.worktree_path,
                    branch=intent.branch,
                    _attempt_lock_held=True,
                )
            except (CleanupEventFinalizationError, WorktreeError) as exc:
                native_cleanup_error = str(exc)
                if isinstance(exc, CleanupEventFinalizationError):
                    native_cleanup_payload = exc.partial_payload()
        payload["native_abort_reconciled"] = True
        payload["native_land_event_appended"] = native_land_event_appended
        payload["native_cleanup"] = native_cleanup_payload
        payload["native_cleanup_error"] = native_cleanup_error
    native_cleanup_mutated = bool(
        native_cleanup_payload is not None
        and (
            native_cleanup_payload.get("worktree_removed")
            or native_cleanup_payload.get("deleted_branch")
            or native_cleanup_payload.get("event_appended")
        )
    )
    mutation_happened = (
        runtime_changed
        or event_appended
        or native_land_event_appended
        or native_cleanup_mutated
    )
    if native_cleanup_error:
        mutation_phase = "runtime_finalized_cleanup_pending"
    elif runtime_changed and event_appended:
        mutation_phase = "runtime_and_event_finalized"
    elif runtime_changed:
        mutation_phase = "runtime_finalized"
    elif event_appended:
        mutation_phase = "event_finalized"
    else:
        mutation_phase = "none"
    state_payload = _task_recovery_payload(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    next_action = decide_next_action(_lifecycle_context(profile, state_payload))
    payload["next_action"] = next_action.to_dict()
    payload["recommended_commands"] = list(state_payload["recommended_commands"])
    payload["recommended_actions"] = _task_surface_actions(
        list(state_payload["recommended_actions"])
    )
    return observe_operation_result(profile, OperationResult(
        operation="task.reconcile-landing",
        operation_status="partial" if native_cleanup_error else "succeeded",
        task_status=state_payload.get("task_runtime_status"),
        attempt_status=state_payload.get("latest_attempt_status"),
        disposition=next_action.disposition,
        mutation_started=mutation_happened,
        mutation_completed=bool(mutation_happened and not native_cleanup_error),
        mutation_phase=mutation_phase,
        failure_code=None,
        next_action=next_action,
        legacy_payload=payload,
    ))


def render_landing_reconciliation_text(payload: Any) -> str:
    proof = payload["proof"]
    action = "applied" if payload.get("apply") else "dry-run ready"
    lines: list[str] = []
    _append_operation_contract(lines, prefix="[blackdog-task]", payload=payload)
    lines.extend(
        [
            f"Landing reconciliation: {action}",
            f"Task: {payload['workset_id']}/{payload['task_id']} attempt={payload['attempt_id']}",
            f"Commit: {payload['landed_commit']} target={proof['target_branch']}",
            f"Changed paths: {len(proof['changed_paths'])}",
        ]
    )
    if payload.get("apply"):
        lines.append(
            f"Runtime changed: {'yes' if payload.get('runtime_changed') else 'no'} | "
            f"Event appended: {'yes' if payload.get('event_appended') else 'no'}"
        )
    else:
        lines.append("No runtime, event, worktree, branch, or commit state was changed.")
    return "\n".join(lines) + "\n"


def _reload_landing_transaction(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
) -> LandingTransaction:
    transaction = load_landing_transaction(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
    )
    if transaction is None:
        raise LandingTransactionError(
            f"landing transaction {intent.transaction_id} disappeared after durable append"
        )
    return transaction


def _run_landing_transaction(
    profile: RepoProfile,
    *,
    workset: Workset,
    task: TaskSpec,
    transaction: LandingTransaction,
    workspace_adoption: tuple[Mapping[str, Any], Any, LandingTransaction]
    | None = None,
) -> tuple[LandingTransaction, Any]:
    intent = transaction.intent
    if transaction.aborted:
        raise WorktreeError(
            "landing transaction has a durable abort intent; resume task close instead"
        )
    # Once append-once landing evidence is durable, source cleanup is the
    # authorized next side effect. A retry after cleanup but before its phase
    # append must therefore prove the source commit/tree without requiring the
    # intentionally removed live branch/worktree.
    require_source = not (
        intent.cleanup and "land_event_recorded" in transaction.phases
    )
    if transaction.abort_superseded:
        _verify_landing_abort_chain(
            profile,
            transaction=transaction,
            require_source=require_source,
        )
    if "source_prepared" in transaction.phases:
        source_commit = _verify_landing_source_phase(
            profile,
            intent=intent,
            transaction=transaction,
            require_branch=require_source,
        )
    else:
        source_data = _landing_source_phase_data(
            profile,
            intent=intent,
            workset=workset,
            task=task,
        )
        record_landing_phase(
            profile,
            intent=intent,
            phase="source_prepared",
            data=source_data,
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
        source_commit = _verify_landing_source_phase(
            profile,
            intent=intent,
            transaction=transaction,
            require_branch=True,
        )

    if "canonical_commit_created" in transaction.phases:
        landed_commit = _verify_canonical_landing_phase(
            intent=intent,
            transaction=transaction,
        )
    else:
        canonical_data = _canonical_landing_phase_data(
            profile,
            intent=intent,
            source_commit=source_commit,
        )
        record_landing_phase(
            profile,
            intent=intent,
            phase="canonical_commit_created",
            data=canonical_data,
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
        landed_commit = _verify_canonical_landing_phase(
            intent=intent,
            transaction=transaction,
        )

    historical_adoption_target_proof = False
    if workspace_adoption is not None:
        adoption_receipt, _predecessor, _predecessor_transaction = (
            workspace_adoption
        )
        adoption_attempt = find_task_attempt(
            load_runtime_state(profile.paths),
            intent.workset_id,
            intent.attempt_id,
        )
        if adoption_attempt is None:
            raise LandingTransactionError(
                "native landing lost its adopted successor runtime"
            )
        historical_adoption_target_proof = (
            _load_workspace_adoption_completion_intent(
                profile,
                attempt=adoption_attempt,
                receipt=adoption_receipt,
            )
            is not None
        )

    if "target_updated" in transaction.phases:
        _verify_target_updated_phase(
            profile,
            intent=intent,
            transaction=transaction,
            landed_commit=landed_commit,
            require_live_target=not historical_adoption_target_proof,
        )
    else:
        target_data = _update_landing_target(
            profile,
            intent=intent,
            landed_commit=landed_commit,
        )
        record_landing_phase(
            profile,
            intent=intent,
            phase="target_updated",
            data=target_data,
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
        _verify_target_updated_phase(
            profile,
            intent=intent,
            transaction=transaction,
            landed_commit=landed_commit,
            require_live_target=True,
        )

    if "temporary_cleanup_complete" in transaction.phases:
        _verify_temporary_landing_cleanup_phase(
            intent=intent,
            transaction=transaction,
            landed_commit=landed_commit,
        )
    else:
        temporary_cleanup = _temporary_landing_cleanup_phase_data(
            intent=intent,
            landed_commit=landed_commit,
        )
        record_landing_phase(
            profile,
            intent=intent,
            phase="temporary_cleanup_complete",
            data=temporary_cleanup,
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
        _verify_temporary_landing_cleanup_phase(
            intent=intent,
            transaction=transaction,
            landed_commit=landed_commit,
        )

    if workspace_adoption is not None:
        adoption_receipt, _predecessor, predecessor_transaction = (
            workspace_adoption
        )
        active_or_latest = find_task_attempt(
            load_runtime_state(profile.paths),
            intent.workset_id,
            intent.attempt_id,
        )
        if active_or_latest is None:
            raise LandingTransactionError(
                "native landing lost its adopted successor runtime"
            )
        _ensure_normal_workspace_adoption_completion_intent(
            profile,
            attempt=active_or_latest,
            receipt=adoption_receipt,
            predecessor_transaction=predecessor_transaction,
            native_transaction=transaction,
        )

    if "runtime_finalized" in transaction.phases:
        finished = _verify_landing_runtime_phase(
            profile,
            intent=intent,
            transaction=transaction,
            source_commit=source_commit,
            landed_commit=landed_commit,
        )
    else:
        finished, runtime_data = _finalize_landing_runtime(
            profile,
            intent=intent,
            source_commit=source_commit,
            landed_commit=landed_commit,
        )
        record_landing_phase(
            profile,
            intent=intent,
            phase="runtime_finalized",
            data=runtime_data,
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
        finished = _verify_landing_runtime_phase(
            profile,
            intent=intent,
            transaction=transaction,
            source_commit=source_commit,
            landed_commit=landed_commit,
        )

    if "land_event_recorded" in transaction.phases:
        _verify_landing_event_phase(
            profile,
            intent=intent,
            transaction=transaction,
            landed_commit=landed_commit,
        )
    else:
        event_data = _record_landing_event_phase_data(
            profile,
            intent=intent,
            landed_commit=landed_commit,
        )
        record_landing_phase(
            profile,
            intent=intent,
            phase="land_event_recorded",
            data=event_data,
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
        _verify_landing_event_phase(
            profile,
            intent=intent,
            transaction=transaction,
            landed_commit=landed_commit,
        )

    if workspace_adoption is not None:
        adoption_receipt, _predecessor, predecessor_transaction = (
            workspace_adoption
        )
        _ensure_normal_workspace_adoption_complete(
            profile,
            attempt=finished,
            receipt=adoption_receipt,
            predecessor_transaction=predecessor_transaction,
            native_transaction=transaction,
        )

    if "task_cleanup_complete" in transaction.phases:
        _verify_task_landing_cleanup_phase(
            profile,
            intent=intent,
            transaction=transaction,
        )
    else:
        cleanup_data = _task_landing_cleanup_phase_data(
            profile,
            intent=intent,
            source_commit=source_commit,
        )
        record_landing_phase(
            profile,
            intent=intent,
            phase="task_cleanup_complete",
            data=cleanup_data,
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
        _verify_task_landing_cleanup_phase(
            profile,
            intent=intent,
            transaction=transaction,
        )

    complete_data = _complete_landing_phase_data(
        intent=intent,
        landed_commit=landed_commit,
    )
    if "complete" in transaction.phases:
        if not strict_json_equal(
            dict(transaction.data_for("complete")), complete_data
        ):
            raise LandingTransactionError("complete landing phase evidence is not canonical")
    else:
        record_landing_phase(
            profile,
            intent=intent,
            phase="complete",
            data=complete_data,
        )
        transaction = _reload_landing_transaction(profile, intent=intent)
        if not strict_json_equal(
            dict(transaction.data_for("complete")), complete_data
        ):
            raise LandingTransactionError("complete landing phase evidence is not canonical")
    return transaction, finished


def _landing_result_payload(
    profile: RepoProfile,
    *,
    transaction: LandingTransaction,
) -> dict[str, Any]:
    intent = transaction.intent
    source_data = transaction.data_for("source_prepared")
    canonical_data = transaction.data_for("canonical_commit_created")
    target_data = transaction.data_for("target_updated")
    target_worktree = _find_worktree_for_branch(
        Path(intent.primary_worktree),
        f"refs/heads/{intent.target_branch}",
    )
    return {
        "branch": intent.branch,
        "target_branch": intent.target_branch,
        "primary_worktree": intent.primary_worktree,
        "target_worktree": str(target_worktree) if target_worktree is not None else None,
        "landing_worktree": intent.temporary_worktree_path,
        "landed_commit": canonical_data["landed_commit"],
        "target_commit": target_data["target_commit"],
        "diff_file": None,
        "diffstat_file": None,
        "changed_paths": list(intent.changed_paths),
        "cleanup": intent.cleanup,
        "cleaned_worktree": intent.worktree_path if intent.cleanup else None,
        "deleted_branch": intent.cleanup,
        "removed_temporary_target": True,
        "attempt_id": intent.attempt_id,
        "task_id": intent.task_id,
        "status": ATTEMPT_STATUS_SUCCESS,
        "summary": intent.summary,
        "commit": source_data["source_commit"],
        "commit_message": intent.commit_message,
        "worktree_path": intent.worktree_path,
        "attempt_active": False,
        "transaction_id": intent.transaction_id,
        "landing_transaction": transaction.to_dict(),
    }


def _landing_resume_action(intent: LandingIntent) -> LifecycleAction:
    primary_root = Path(intent.primary_worktree)
    primary_executable = primary_root / ".VE" / "bin" / "blackdog"
    executable = (
        str(primary_executable.resolve())
        if primary_executable.is_file() and os.access(primary_executable, os.X_OK)
        else "blackdog"
    )
    return LifecycleAction(
        action_id="resume_landing_transaction",
        disposition="retryable",
        reason_code="landing_transaction_incomplete",
        reason_detail="The durable landing transaction must resume from its verified phase.",
        argv=intent.task_land_argv(
            executable=executable,
            project_root=primary_root,
        ),
        safety_class="validated_mutation",
        mutation_class="git_and_runtime",
        display="Resume the durable landing transaction",
    )


def _landing_abort_close_action(transaction: LandingTransaction) -> LifecycleAction:
    if transaction.abort_data is None:
        raise LandingTransactionError("landing abort close action requires abort intent")
    request = transaction.abort_data.get("close_request")
    if not isinstance(request, Mapping):
        raise LandingTransactionError("landing abort close action is missing close request")
    intent = transaction.intent
    executable = _lifecycle_blackdog_executable(
        load_profile(Path(intent.primary_worktree)),
        {"worktree_path": intent.worktree_path},
    )
    argv = [
        executable,
        "task",
        "close",
        f"--project-root={intent.primary_worktree}",
        f"--workset={intent.workset_id}",
        f"--task={intent.task_id}",
        f"--actor={intent.actor}",
        f"--status={request['status']}",
        f"--summary={request['summary']}",
    ]
    argv.extend(
        f"--validation={row['name']}={row['status']}"
        for row in request["validations"]
    )
    argv.extend(f"--residual={value}" for value in request["residuals"])
    argv.extend(f"--followup={value}" for value in request["followup_candidates"])
    if request["note"] is not None:
        argv.append(f"--note={request['note']}")
    if request["cleanup_requested"]:
        argv.append("--cleanup")
    return LifecycleAction(
        action_id="resume_landing_abort",
        disposition="retryable",
        reason_code="landing_abort_incomplete",
        reason_detail=(
            "The durable landing abort must finish its exact runtime and event finalization."
        ),
        argv=tuple(argv),
        safety_class="validated_mutation",
        mutation_class="runtime",
        display="Resume the durable landing abort",
    )


def _landing_safe_abort_action(
    intent: LandingIntent,
    *,
    detail: str,
) -> LifecycleAction:
    executable = _lifecycle_blackdog_executable(
        load_profile(Path(intent.primary_worktree)),
        {"worktree_path": intent.worktree_path},
    )
    return LifecycleAction(
        action_id="abort_stale_landing",
        disposition="operator_choice",
        reason_code="landing_target_advanced_after_intent",
        reason_detail=(
            "The immutable landing target moved; close now records a safe abort and retains the source workspace."
        ),
        argv=(
            executable,
            "task",
            "close",
            f"--project-root={intent.primary_worktree}",
            f"--workset={intent.workset_id}",
            f"--task={intent.task_id}",
            f"--actor={intent.actor}",
            "--status=blocked",
            f"--summary=Landing aborted safely: {detail}",
        ),
        safety_class="proof_guarded_mutation",
        mutation_class="git_and_runtime",
        display="Safely abort the stale landing and retain its source workspace",
    )


def _stale_branch_rebase_action(exc: StaleTaskBranchError) -> LifecycleAction:
    argv = ["git"]
    if exc.branch_worktree:
        argv.extend(("-C", exc.branch_worktree))
    argv.extend(("rebase", "--autostash", exc.target_branch))
    return LifecycleAction(
        action_id="rebase_task_branch",
        disposition="operator_action_required",
        reason_code="stale_task_branch",
        reason_detail=str(exc),
        argv=tuple(argv),
        safety_class="operator_confirmation",
        mutation_class="git_and_filesystem",
        display=f"Rebase {exc.branch} onto {exc.target_branch}",
    )


def _pretransaction_landing_failure_next_action(
    payload: Mapping[str, Any],
) -> NextAction | None:
    """Project an exact action from a typed blocker found before landing intent."""
    automatic_action = str(payload.get("recovery_action") or "").strip()
    automatic_actions = {
        "automatic_stale_recovery_conflict": (
            "automatic_rebase_conflict",
            "The automatic rebase encountered a real content conflict. Blackdog "
            "preserved the task workspace and did not move the target.",
            (
                "task_worktree_conflict_resolution",
                "unique_work_preservation_proof",
                "fresh_validation_evidence",
            ),
        ),
        "automatic_stale_recovery_validation_failed": (
            "post_rebase_validation_failed",
            "Configured validation did not prove the rebased task tree. Blackdog "
            "preserved the task workspace and did not move the target.",
            ("task_worktree_repair", "fresh_validation_evidence"),
        ),
        "automatic_stale_recovery_unsafe": (
            "automatic_rebase_safety_unproven",
            "Blackdog could not prove a safe automatic rebase or restoration. The "
            "task workspace is retained for the current landing agent.",
            ("git_operation_proof", "unique_work_preservation_proof"),
        ),
    }
    automatic = automatic_actions.get(automatic_action)
    if payload.get("landing_transaction") is None and automatic is not None:
        reason_code, reason_detail, required_inputs = automatic
        return NextAction.terminal(
            action_id=automatic_action,
            kind="blocked",
            disposition="repair_required",
            reason_code=reason_code,
            reason_detail=reason_detail,
            display="Return automatic stale recovery to the current landing agent",
            required_inputs=required_inputs,
        )
    if (
        payload.get("landing_transaction") is not None
        or payload.get("failure_class") != FAILURE_CLASS_STALE_BRANCH
        or payload.get("recovery_action") != "rebase_task_branch"
    ):
        return None
    branch = str(payload["branch"])
    target_branch = str(payload["target_branch"])
    worktree_path = str(payload.get("worktree_path") or "").strip()
    return NextAction.command(
        _stale_branch_rebase_action(
            StaleTaskBranchError(
                branch=branch,
                target_branch=target_branch,
                branch_worktree=Path(worktree_path) if worktree_path else None,
            )
        )
    )


def _landing_side_effect_fingerprint(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
) -> str:
    primary_root = Path(intent.primary_worktree)

    def ref(name: str) -> str | None:
        row = _run_git_no_check(primary_root, "rev-parse", "--verify", f"refs/heads/{name}")
        return row.stdout.strip() if row.returncode == 0 else None

    temporary_path = Path(intent.temporary_worktree_path)
    temporary_row = _registered_worktree_row(primary_root, temporary_path)
    temporary_head = None
    temporary_status = None
    if temporary_path.exists() and _is_git_worktree_path(temporary_path):
        head = _run_git_no_check(temporary_path, "rev-parse", "HEAD")
        status = _run_git_no_check(temporary_path, "status", "--porcelain=v1", "-z")
        temporary_head = head.stdout.strip() if head.returncode == 0 else None
        temporary_status = status.stdout if status.returncode == 0 else None
    runtime_state = load_runtime_state(profile.paths)
    attempt = find_task_attempt(runtime_state, intent.workset_id, intent.attempt_id)
    transaction = load_landing_transaction(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
    )
    with exclusive_file_lock(profile.paths.events_file):
        events = load_events(profile.paths.events_file)
    owned_events = []
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if (
            payload.get("transaction_id") == intent.transaction_id
            or (
                payload.get("workset_id") == intent.workset_id
                and payload.get("task_id") == intent.task_id
                and payload.get("attempt_id") == intent.attempt_id
            )
        ):
            owned_events.append(
                {
                    "event_id": event.get("event_id"),
                    "type": event.get("type"),
                    "actor": event.get("actor"),
                    "payload": dict(payload),
                }
            )
    material = repr(
        {
            "source_ref": ref(intent.branch),
            "target_ref": ref(intent.target_branch),
            "source_exists": Path(intent.worktree_path).exists(),
            "source_registration": _registered_worktree_row(
                primary_root,
                Path(intent.worktree_path),
            ),
            "temporary_exists": temporary_path.exists(),
            "temporary_registration": temporary_row,
            "temporary_head": temporary_head,
            "temporary_status": temporary_status,
            "attempt": (
                (
                    attempt.status,
                    attempt.ended_at,
                    attempt.commit,
                    attempt.landed_commit,
                )
                if attempt is not None
                else None
            ),
            "landing_transaction": (
                transaction.to_dict() if transaction is not None else None
            ),
            "owned_events": owned_events,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _landing_blocked_payload(
    profile: RepoProfile,
    *,
    attempt: Any,
    exc: Exception,
    transaction: LandingTransaction | None,
    cleanup: bool,
) -> dict[str, Any]:
    intent = transaction.intent if transaction is not None else None
    mapping = _failure_details_for_land_error(exc)
    runtime_state = load_runtime_state(profile.paths)
    active = active_task_attempt(
        runtime_state,
        intent.workset_id if intent is not None else "",
        attempt.task_id,
    ) if intent is not None else None
    branch = intent.branch if intent is not None else attempt.branch
    target_branch = intent.target_branch if intent is not None else attempt.target_branch
    worktree_path = intent.worktree_path if intent is not None else attempt.worktree_path
    changed_paths = list(intent.changed_paths) if intent is not None else []
    source_commit = None
    landed_commit = None
    recommended_actions: list[str] = []
    recommended_commands: list[dict[str, Any]] = []
    primary_root = (
        Path(intent.primary_worktree)
        if intent is not None
        else find_primary_worktree(profile.paths.project_root)
    )
    if transaction is not None:
        if "source_prepared" in transaction.phases:
            source_commit = transaction.data_for("source_prepared").get("source_commit")
        if "canonical_commit_created" in transaction.phases:
            landed_commit = transaction.data_for("canonical_commit_created").get("landed_commit")
        if transaction.aborted:
            action = _landing_abort_close_action(transaction)
        elif isinstance(exc, StaleTaskBranchError):
            action = _landing_safe_abort_action(intent, detail=str(exc))
        else:
            action = _landing_resume_action(intent)
        recommended_actions.append(action.command)
        recommended_commands.append(
            {
                "action_id": action.action_id,
                "command": action.command,
                "argv": list(action.argv),
                "reason": action.reason_detail,
                "disposition": action.disposition,
            }
        )
    elif isinstance(exc, StaleTaskBranchError):
        action = _stale_branch_rebase_action(exc)
        recommended_actions.append(action.command)
        recommended_commands.append(
            {
                "action_id": action.action_id,
                "command": action.command,
                "argv": list(action.argv),
                "reason": action.reason_detail,
                "disposition": action.disposition,
            }
        )
    else:
        recommended_actions.append(
            "fix the landing blocker, then rerun `blackdog task land` with closure evidence"
        )
    payload = {
        "branch": branch,
        "target_branch": target_branch,
        "primary_worktree": str(primary_root),
        "target_worktree": None,
        "landing_worktree": intent.temporary_worktree_path if intent is not None else None,
        "landed_commit": landed_commit,
        "diff_file": None,
        "diffstat_file": None,
        "changed_paths": changed_paths,
        "cleanup": intent.cleanup if intent is not None else cleanup,
        "cleaned_worktree": None,
        "deleted_branch": False,
        "removed_temporary_target": False,
        "attempt_id": attempt.attempt_id,
        "task_id": attempt.task_id,
        "status": ATTEMPT_STATUS_BLOCKED,
        "summary": f"Landing blocked: {exc}",
        "commit": source_commit,
        "commit_message": intent.commit_message if intent is not None else None,
        "worktree_path": worktree_path,
        "error": str(exc),
        "attempt_active": active is not None if intent is not None else True,
        "land_failure_disposition": "retryable",
        "transaction_id": intent.transaction_id if intent is not None else None,
        "landing_transaction": transaction.to_dict() if transaction is not None else None,
        "mutation_phase": _landing_operation_phase(transaction),
        **mapping,
        "recommended_actions": recommended_actions,
        "recommended_commands": recommended_commands,
    }
    automatic_stale_recovery = getattr(exc, "automatic_stale_recovery", None)
    if isinstance(automatic_stale_recovery, Mapping):
        payload["automatic_stale_recovery"] = dict(automatic_stale_recovery)
        payload["landing_correction"] = automatic_stale_recovery.get("receipt")
        payload["mutation_phase"] = "git_prepared"
    return payload


def land_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    summary: str | None = None,
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
    cleanup: bool = True,
    _automatic_stale_recovery_enabled: bool = False,
) -> dict[str, Any]:
    workset, task = _require_workset_and_task(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    pending_close, pending_close_action = _pending_close_action(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    if pending_close is not None:
        if "request" in pending_close:
            payload = _close_blocked_payload(
                profile,
                request=pending_close["request"],
                exc=CloseTransactionError(
                    "landing is gated by the incomplete close transaction"
                ),
                durable_request=True,
                mutation_started=False,
            )
        else:
            assert pending_close_action is not None
            payload = {
                "workset_id": workset_id,
                "task_id": task_id,
                "status": ATTEMPT_STATUS_BLOCKED,
                "error": pending_close.get("error"),
                "close_transaction_blocked": True,
                "mutation_started": False,
                "mutation_completed": False,
                "mutation_phase": "event_finalization_partial",
                "next_action": pending_close_action.to_dict(),
                "recommended_actions": [pending_close_action.display],
                "recommended_commands": pending_close_action.legacy_command_rows(),
            }
        payload["attempt_active"] = bool(
            active_task_attempt(
                load_runtime_state(profile.paths),
                workset_id,
                task_id,
            )
        )
        payload["land_failure_disposition"] = "close_pending"
        return payload
    request = _normalized_landing_request(
        actor=actor,
        summary=str(summary or "").strip(),
        validations=validations,
        residuals=residuals,
        followup_candidates=followup_candidates,
        note=note,
        cleanup=cleanup,
    )
    initial_state = load_runtime_state(profile.paths)
    initial_active = active_task_attempt(initial_state, workset_id, task_id)
    initial_latest = latest_task_attempt(initial_state, workset_id, task_id)
    candidate = initial_active
    if candidate is None and initial_latest is not None:
        existing = load_landing_transaction(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=initial_latest.attempt_id,
        )
        if existing is not None:
            candidate = initial_latest
    if candidate is None:
        raise BacklogError(
            f"No active or transaction-owned WTAM attempt for task {task_id!r} "
            f"in workset {workset_id!r}"
        )

    with attempt_lifecycle_lock(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=candidate.attempt_id,
    ):
        runtime_state = load_runtime_state(profile.paths)
        active = active_task_attempt(runtime_state, workset_id, task_id)
        latest = latest_task_attempt(runtime_state, workset_id, task_id)
        transaction: LandingTransaction | None = None
        if active is not None:
            attempt = active
            transaction = load_landing_transaction(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=attempt.attempt_id,
            )
        elif latest is not None:
            transaction = load_landing_transaction(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=latest.attempt_id,
            )
            if transaction is None:
                raise BacklogError(
                    f"No active or transaction-owned WTAM attempt for task {task_id!r}"
                )
            attempt = latest
        else:
            raise BacklogError(f"Task {task_id!r} has no WTAM attempt")
        if attempt.attempt_id != candidate.attempt_id:
            raise BacklogError(
                "task attempt changed while waiting for the landing operation lock"
            )
        if attempt.actor != request["actor"]:
            raise BacklogError(
                f"Attempt {attempt.attempt_id!r} is owned by {attempt.actor}, "
                f"not {request['actor']}"
            )
        adoption_receipt = _workspace_adoption_receipt(attempt)
        if attempt.status == ATTEMPT_STATUS_IN_PROGRESS:
            _require_ordinary_resume_start_evidence(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                runtime_state=runtime_state,
                attempt=attempt,
            )
            adoption_contract = _require_workspace_adoption_start_evidence(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                runtime_state=runtime_state,
                attempt=attempt,
            )
        elif attempt.status == ATTEMPT_STATUS_SUCCESS and adoption_receipt is not None:
            predecessor = find_task_attempt(
                runtime_state,
                workset_id,
                str(adoption_receipt["predecessor_attempt_id"]),
            )
            predecessor_transaction = (
                load_landing_transaction(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    attempt_id=predecessor.attempt_id,
                )
                if predecessor is not None and predecessor.task_id == task_id
                else None
            )
            if (
                predecessor is None
                or predecessor_transaction is None
                or predecessor_transaction.transaction_id
                != adoption_receipt["abort_transaction_id"]
                or predecessor_transaction.outcome != "abort_complete"
            ):
                raise LandingTransactionError(
                    "terminal workspace adoption has no exact predecessor transaction"
                )
            verified_source = _verify_landing_abort_chain(
                profile,
                transaction=predecessor_transaction,
                require_source=False,
            )
            derived_receipt = _derive_workspace_adoption_receipt(
                predecessor=predecessor,
                transaction=predecessor_transaction,
                target_commit_at_adoption=str(
                    adoption_receipt["target_commit_at_adoption"]
                ),
            )
            if (
                verified_source != derived_receipt["source_commit"]
                or not strict_json_equal(adoption_receipt, derived_receipt)
            ):
                raise LandingTransactionError(
                    "terminal workspace adoption conflicts with immutable predecessor proof"
                )
            adoption_contract = (
                adoption_receipt,
                predecessor,
                predecessor_transaction,
            )
        else:
            adoption_contract = None

        side_effect_before: str | None = None
        intent_recorded_by_call = False
        correction_mutation_observed = False
        automatic_recovery_evidence: dict[str, Any] | None = None
        correction_intent: LandingCorrectionIntent | None = None
        try:
            if transaction is not None:
                correction_selection = load_landing_correction_selection(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    attempt_id=attempt.attempt_id,
                )
                active_correction = correction_selection.active
                if (
                    active_correction is not None
                    and CORRECTION_PHASE_VALIDATION_COMPLETED
                    in active_correction.phases
                ):
                    record_landing_correction_handed_to_landing(
                        profile,
                        intent=active_correction.intent,
                        landing_transaction_id=transaction.intent.transaction_id,
                        landing_intent_event_id=landing_phase_event_id(
                            transaction.intent.transaction_id,
                            "intent_recorded",
                        ),
                    )
                    correction_mutation_observed = True
            if transaction is None:
                if (
                    _automatic_stale_recovery_enabled
                    and any(
                        name == AUTOMATIC_STALE_REBASE_VALIDATION_NAME
                        for name, _status in request["validations"]
                    )
                ):
                    raise BacklogError(
                        f"{AUTOMATIC_STALE_REBASE_VALIDATION_NAME!r} is reserved "
                        "for Blackdog-owned post-rebase evidence"
                    )
                correction_selection = (
                    load_landing_correction_selection(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        attempt_id=attempt.attempt_id,
                    )
                    if _automatic_stale_recovery_enabled
                    else None
                )
                try:
                    intent = _build_landing_intent(
                        profile,
                        workset=workset,
                        task=task,
                        attempt=attempt,
                        request=request,
                    )
                except StaleTaskBranchError as stale_error:
                    if not _automatic_stale_recovery_enabled:
                        raise
                    correction_mutation_observed = True
                    (
                        request,
                        automatic_recovery_evidence,
                        correction_intent,
                    ) = _automatic_stale_correction(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        attempt=attempt,
                        request=request,
                        stale_error=stale_error,
                    )
                    try:
                        intent = _build_landing_intent(
                            profile,
                            workset=workset,
                            task=task,
                            attempt=attempt,
                            request=request,
                        )
                    except StaleTaskBranchError as second_stale:
                        latest_target = _run_git(
                            find_primary_worktree(profile.paths.project_root),
                            "rev-parse",
                            f"refs/heads/{attempt.target_branch}",
                        )
                        exhausted_evidence = dict(
                            automatic_recovery_evidence or {}
                        )
                        exhausted_evidence.update(
                            {
                                "state": "retry_exhausted",
                                "target_commit": latest_target,
                                "landing_agent_handoff_required": True,
                            }
                        )
                        exhausted = AutomaticStaleRecoveryError(
                            state="retry_exhausted",
                            detail=(
                                "the landing target moved again after the one "
                                "allowed automatic stale correction"
                            ),
                            evidence=exhausted_evidence,
                        )
                        assert correction_intent is not None
                        _record_automatic_correction_blocker(
                            profile,
                            correction_intent=correction_intent,
                            exc=exhausted,
                        )
                        raise exhausted from second_stale
                else:
                    if (
                        correction_selection is not None
                        and (
                            correction_selection.active is not None
                            or (
                                correction_selection.latest_terminal is not None
                                and correction_selection.latest_terminal.blocked
                            )
                        )
                    ):
                        correction_mutation_observed = True
                        (
                            request,
                            automatic_recovery_evidence,
                            correction_intent,
                        ) = _automatic_stale_correction(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            attempt=attempt,
                            request=request,
                            stale_error=None,
                        )
                        intent = _build_landing_intent(
                            profile,
                            workset=workset,
                            task=task,
                            attempt=attempt,
                            request=request,
                        )
                intent_recorded_by_call = record_landing_phase(
                    profile,
                    intent=intent,
                    phase="intent_recorded",
                    data=intent.to_dict(),
                )
                transaction = _reload_landing_transaction(profile, intent=intent)
                if correction_intent is not None:
                    record_landing_correction_handed_to_landing(
                        profile,
                        intent=correction_intent,
                        landing_transaction_id=intent.transaction_id,
                        landing_intent_event_id=landing_phase_event_id(
                            intent.transaction_id,
                            "intent_recorded",
                        ),
                    )
                    correction_mutation_observed = True
            elif transaction.intent.request_identity() != request:
                corrected_retry = _landing_request_with_automatic_validation(
                    request
                )
                retry_correction_selection = (
                    load_landing_correction_selection(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        attempt_id=attempt.attempt_id,
                    )
                )
                handed_correction = retry_correction_selection.latest_terminal
                if (
                    handed_correction is not None
                    and handed_correction.handed_to_landing
                    and transaction.intent.request_identity() == corrected_retry
                ):
                    request = corrected_retry
                else:
                    mismatches = sorted(
                        key
                        for key in request
                        if transaction.intent.request_identity().get(key)
                        != request.get(key)
                    )
                    raise LandingTransactionError(
                        "landing retry conflicts with immutable intent on: "
                        + ", ".join(mismatches)
                    )
            side_effect_before = _landing_side_effect_fingerprint(
                profile,
                intent=transaction.intent,
            )
            transaction, _finished = _run_landing_transaction(
                profile,
                workset=workset,
                task=task,
                transaction=transaction,
                workspace_adoption=adoption_contract,
            )
            if adoption_contract is not None:
                adoption_receipt, _predecessor, _predecessor_transaction = adoption_contract
                completion_intent = _load_workspace_adoption_completion_intent(
                    profile,
                    attempt=_finished,
                    receipt=adoption_receipt,
                )
                if completion_intent is None:
                    raise LandingTransactionError(
                        "native successor landing completed without adoption completion intent"
                    )
            result = _landing_result_payload(profile, transaction=transaction)
            if adoption_contract is not None:
                result.update(
                    workspace_adoption_completion=True,
                    workspace_adoption_completion_intent=completion_intent,
                    adoption_completion_recorded=True,
                    native_land_event_reused=True,
                )
            correction_selection = load_landing_correction_selection(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=attempt.attempt_id,
            )
            completed_correction = correction_selection.latest_terminal
            if (
                completed_correction is not None
                and completed_correction.handed_to_landing
            ):
                result["landing_correction"] = completed_correction.to_dict()
                evidence = dict(automatic_recovery_evidence or {})
                evidence.update(
                    {
                        "schema_version": 1,
                        "state": "landed",
                        "correction_id": completed_correction.correction_id,
                        "target_updated_by_blackdog": True,
                        "landing_agent_handoff_required": False,
                        "receipt": completed_correction.to_dict(),
                    }
                )
                result["automatic_stale_recovery"] = evidence
            result["mutation_observed"] = (
                intent_recorded_by_call
                or correction_mutation_observed
                or side_effect_before
                != _landing_side_effect_fingerprint(
                    profile,
                    intent=transaction.intent,
                )
            )
            return result
        except Exception as exc:
            current = load_landing_transaction(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=attempt.attempt_id,
            )
            mutation_observed = bool(
                intent_recorded_by_call
                or correction_mutation_observed
                or isinstance(exc, AutomaticStaleRecoveryError)
                or (
                    side_effect_before is not None
                    and current is not None
                    and side_effect_before
                    != _landing_side_effect_fingerprint(
                        profile,
                        intent=current.intent,
                    )
                )
            )
            if current is not None and current.outcome == "landed_complete":
                try:
                    current, _finished = _run_landing_transaction(
                        profile,
                        workset=workset,
                        task=task,
                        transaction=current,
                        workspace_adoption=adoption_contract,
                    )
                except Exception as verification_error:
                    exc = verification_error
                else:
                    if adoption_contract is not None:
                        adoption_receipt, _predecessor, _predecessor_transaction = adoption_contract
                        completion_intent = _load_workspace_adoption_completion_intent(
                            profile,
                            attempt=_finished,
                            receipt=adoption_receipt,
                        )
                        if completion_intent is None:
                            raise LandingTransactionError(
                                "native successor landing completed without adoption completion intent"
                            )
                    result = _landing_result_payload(
                        profile,
                        transaction=current,
                    )
                    if adoption_contract is not None:
                        result.update(
                            workspace_adoption_completion=True,
                            workspace_adoption_completion_intent=completion_intent,
                            adoption_completion_recorded=True,
                            native_land_event_reused=True,
                        )
                    correction_selection = load_landing_correction_selection(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        attempt_id=attempt.attempt_id,
                    )
                    completed_correction = correction_selection.latest_terminal
                    if (
                        completed_correction is not None
                        and completed_correction.handed_to_landing
                    ):
                        result["landing_correction"] = (
                            completed_correction.to_dict()
                        )
                        result["automatic_stale_recovery"] = {
                            "schema_version": 1,
                            "state": "landed",
                            "correction_id": completed_correction.correction_id,
                            "target_updated_by_blackdog": True,
                            "landing_agent_handoff_required": False,
                            "receipt": completed_correction.to_dict(),
                        }
                    result["mutation_observed"] = mutation_observed
                    return result
            terminal_status = _terminal_land_failure_status(exc)
            if current is None and terminal_status is not None:
                failure_details = _failure_details_for_land_error(exc)
                payload = close_task_worktree(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor=str(request["actor"]),
                    status=terminal_status,
                    summary=f"Landing closed: {exc}",
                    validations=tuple(
                        ValidationRecord(name=name, status=status)
                        for name, status in request["validations"]
                    ),
                    residuals=tuple(request["residuals"]),
                    followup_candidates=tuple(request["followup_candidates"]),
                    note=request["note"] or str(exc),
                    cleanup=bool(request["cleanup"]),
                    _attempt_lock_held=True,
                    _trusted_failure_details=True,
                    **failure_details,
                )
                if payload.get("close_transaction_blocked"):
                    payload.update(
                        {
                            "landing_error": str(exc),
                            "attempt_active": bool(
                                active_task_attempt(
                                    load_runtime_state(profile.paths),
                                    workset_id,
                                    task_id,
                                )
                            ),
                            "land_failure_disposition": "close_pending",
                        }
                    )
                else:
                    payload.update(
                        {
                            "error": str(exc),
                            "attempt_active": False,
                            "land_failure_disposition": "closed",
                            **failure_details,
                        }
                    )
                return payload
            blocked = _landing_blocked_payload(
                profile,
                attempt=attempt,
                exc=exc,
                transaction=current,
                cleanup=bool(request["cleanup"]),
            )
            blocked["mutation_observed"] = mutation_observed
            return blocked


class _CloseGuardConflict(CloseTransactionError):
    """A guarded close can no longer mutate the state named by its request."""


def _close_failure_details(
    *,
    status: str,
    failure_class: str | None,
    recovery_action: str | None,
    prompt_issue: bool,
    operator_issue: bool,
    trusted_classification: bool,
) -> dict[str, Any]:
    derived = _failure_details_for_status(status, recovery_action=recovery_action)
    if not trusted_classification:
        supplied = {
            "failure_class": failure_class,
            "recovery_action": str(recovery_action or "").strip() or None,
            "prompt_issue": bool(prompt_issue),
            "operator_issue": bool(operator_issue),
        }
        expected = {
            "failure_class": derived["failure_class"],
            "recovery_action": derived["recovery_action"],
            "prompt_issue": derived["prompt_issue"],
            "operator_issue": derived["operator_issue"],
        }
        if any(
            supplied[key] not in {None, False, expected[key]}
            for key in supplied
        ):
            raise CloseTransactionError(
                "initial close failure fields must be derived from terminal status"
            )
        return expected
    resolved = {
        "failure_class": str(failure_class or derived["failure_class"]).strip(),
        "recovery_action": str(recovery_action or derived["recovery_action"]).strip(),
        "prompt_issue": bool(prompt_issue),
        "operator_issue": bool(operator_issue or status == ATTEMPT_STATUS_ABANDONED),
    }
    if status == ATTEMPT_STATUS_ABANDONED:
        resolved["failure_class"] = FAILURE_CLASS_ABANDONED
        resolved["operator_issue"] = True
    return resolved


def _close_source_projection(
    profile: RepoProfile,
    *,
    attempt: Any,
    cleanup: bool,
) -> dict[str, Any]:
    primary_root = find_primary_worktree(profile.paths.project_root)
    recorded_path = (
        Path(attempt.worktree_path).resolve(strict=False)
        if str(attempt.worktree_path or "").strip()
        else None
    )
    resolved_path = _resolve_attempt_worktree(
        profile,
        branch=attempt.branch,
        worktree_path=attempt.worktree_path,
    )
    source_path = resolved_path or recorded_path
    path_exists = bool(source_path is not None and source_path.exists())
    source_is_worktree = bool(
        source_path is not None and path_exists and _is_git_worktree_path(source_path)
    )
    registration = (
        _registered_worktree_row(primary_root, source_path)
        if source_path is not None
        else None
    )
    registration_payload = {
        "registered": registration is not None,
        "path": (
            str(Path(str(registration["worktree"])).resolve(strict=False))
            if registration is not None and registration.get("worktree")
            else None
        ),
        "branch": (
            str(registration.get("branch") or "").strip() or None
            if registration is not None
            else None
        ),
        "head": (
            str(registration.get("HEAD") or "").strip() or None
            if registration is not None
            else None
        ),
        "detached": bool(registration is not None and "detached" in registration),
    }
    branch_inspection = _inspect_branch_ref(
        primary_root,
        str(attempt.branch or "").strip() or None,
        role="task_branch",
    )
    if branch_inspection.state == "error":
        raise _inspection_error(branch_inspection)
    if branch_inspection.state == "metadata_missing":
        raise CloseTransactionError("close attempt is missing its durable task branch")
    branch_state = branch_inspection.state
    branch_commit = branch_inspection.resolved_commit
    actual_head: str | None = None
    if source_is_worktree and source_path is not None:
        actual_head = _run_git(source_path, "rev-parse", "HEAD")
    source_head = actual_head or branch_commit
    changed = (
        tuple(
            _attempt_changed_paths(
                profile,
                branch=attempt.branch,
                target_branch=attempt.target_branch,
                worktree_path=source_path,
            )
        )
        if branch_state == "exists"
        else tuple(attempt.changed_paths)
    )
    dirty = bool(
        source_is_worktree
        and source_path is not None
        and _managed_status_dirty(profile, source_path)
    )
    expected_branch_ref = (
        f"refs/heads/{attempt.branch}" if str(attempt.branch or "").strip() else None
    )
    exact_registration = bool(
        source_path is not None
        and recorded_path is not None
        and source_path.resolve(strict=False) != primary_root.resolve(strict=False)
        and source_path.resolve(strict=False) == recorded_path
        and registration_payload["registered"]
        and registration_payload["path"] == str(recorded_path)
        and registration_payload["branch"] == expected_branch_ref
        and not registration_payload["detached"]
        and registration_payload["head"] == actual_head
        and actual_head == branch_commit
    )
    exact_absence = bool(
        recorded_path is not None
        and not recorded_path.exists()
        and registration is None
        and branch_state == "missing"
    )
    cleanup_eligible = False
    cleanup_disposition = "retain_not_requested"
    cleanup_proof = "not_requested"
    cleanup_reason = "cleanup was not requested"
    if cleanup:
        if exact_absence:
            cleanup_eligible = True
            cleanup_disposition = "already_absent"
            cleanup_proof = "exact_source_absent"
            cleanup_reason = "recorded source worktree and branch are already absent"
        elif not exact_registration:
            cleanup_disposition = "retain_unproven"
            cleanup_proof = "source_identity_unproven"
            cleanup_reason = "recorded source path, registration, HEAD, and branch do not form one exact identity"
        elif dirty:
            cleanup_disposition = "retain_dirty"
            cleanup_proof = "dirty"
            cleanup_reason = "recorded source worktree is dirty"
        else:
            try:
                cleanup_plan = _plan_task_branch_cleanup(
                    primary_root,
                    branch=str(attempt.branch),
                    latest_attempt=attempt,
                )
            except (WorktreeError, OSError) as exc:
                cleanup_disposition = "retain_unlanded"
                cleanup_proof = "unproven"
                cleanup_reason = str(exc)
            else:
                cleanup_eligible = True
                cleanup_disposition = "remove"
                cleanup_proof = cleanup_plan.proof_state
                cleanup_reason = cleanup_plan.reason
    projection = {
        "recorded_branch": str(attempt.branch or "").strip() or None,
        "recorded_target_branch": str(attempt.target_branch or "").strip() or None,
        "recorded_worktree_path": str(recorded_path) if recorded_path is not None else None,
        "resolved_source_path": str(source_path) if source_path is not None else None,
        "source_path_exists": path_exists,
        "source_is_worktree": source_is_worktree,
        "source_registration": registration_payload,
        "source_head_commit": source_head,
        "branch_state": branch_state,
        "branch_commit": branch_commit,
        "changed_paths": list(changed),
        "worktree_dirty": dirty,
        "cleanup_eligible": cleanup_eligible,
        "cleanup_disposition": cleanup_disposition,
        "cleanup_proof": cleanup_proof,
        "cleanup_reason": cleanup_reason,
    }
    return projection


def _close_request_for_attempt(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt: Any,
    actor: str,
    status: str,
    summary: str,
    validations: tuple[ValidationRecord, ...],
    residuals: tuple[str, ...],
    followup_candidates: tuple[str, ...],
    note: str | None,
    cleanup: bool,
    failure_class: str | None,
    recovery_action: str | None,
    prompt_issue: bool,
    operator_issue: bool,
    trusted_failure_details: bool,
) -> CloseRequest:
    projection = _close_source_projection(
        profile,
        attempt=attempt,
        cleanup=cleanup,
    )
    cleanup_event_id = (
        _task_cleanup_event_id(
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
            branch=attempt.branch,
            worktree_path=str(projection["resolved_source_path"] or ""),
        )
        if cleanup and projection["cleanup_eligible"]
        else None
    )
    failure_details = _close_failure_details(
        status=status,
        failure_class=failure_class,
        recovery_action=recovery_action,
        prompt_issue=prompt_issue,
        operator_issue=operator_issue,
        trusted_classification=trusted_failure_details,
    )
    request = CloseRequest(
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt.attempt_id,
        actor=actor,
        status=status,
        summary=summary,
        validations=tuple((row.name, row.status) for row in validations),
        residuals=tuple(residuals),
        followup_candidates=tuple(followup_candidates),
        note=note,
        failure_class=str(failure_details["failure_class"]),
        recovery_action=str(failure_details["recovery_action"]),
        prompt_issue=bool(failure_details["prompt_issue"]),
        operator_issue=bool(failure_details["operator_issue"]),
        cleanup_requested=cleanup,
        cleanup_event_id=cleanup_event_id,
        pre_close_projection=projection,
    )
    # Reject noncanonical terminal evidence before the append-once request can
    # reserve its deterministic identity.  The same strict parser owns both
    # initial construction and every later ledger read.
    return CloseRequest.from_dict(request.to_dict())


def _verify_frozen_close_projection(
    profile: RepoProfile,
    *,
    request: CloseRequest,
    allow_owned_cleanup_progress: bool,
) -> None:
    runtime_state = load_runtime_state(profile.paths)
    attempt = find_task_attempt(runtime_state, request.workset_id, request.attempt_id)
    if (
        attempt is None
        or attempt.task_id != request.task_id
        or attempt.actor != request.actor
        or attempt.branch != request.pre_close_projection["recorded_branch"]
        or attempt.target_branch
        != request.pre_close_projection["recorded_target_branch"]
        or (
            str(Path(attempt.worktree_path).resolve(strict=False))
            if attempt.worktree_path
            else None
        )
        != request.pre_close_projection["recorded_worktree_path"]
    ):
        raise _CloseGuardConflict(
            "close attempt identity changed after its durable request"
        )
    current_projection = _close_source_projection(
        profile,
        attempt=attempt,
        cleanup=request.cleanup_requested,
    )
    if strict_json_equal(current_projection, request.pre_close_projection):
        return
    if not (
        allow_owned_cleanup_progress
        and request.cleanup_event_id is not None
        and request.pre_close_projection["cleanup_disposition"] == "remove"
    ):
        raise _CloseGuardConflict(
            "close source projection changed after its durable request"
        )
    source_path_value = request.pre_close_projection["resolved_source_path"]
    if not isinstance(source_path_value, str):
        raise _CloseGuardConflict("close cleanup source path is missing")
    source_path = Path(source_path_value).resolve(strict=False)
    primary_root = find_primary_worktree(profile.paths.project_root)
    registration = _registered_worktree_row(primary_root, source_path)
    branch = request.pre_close_projection["recorded_branch"]
    branch_inspection = _inspect_branch_ref(
        primary_root,
        str(branch) if branch is not None else None,
        role="task_branch",
    )
    if branch_inspection.state == "error":
        raise _inspection_error(branch_inspection)
    expected_commit = request.pre_close_projection["source_head_commit"]
    branch_is_owned_or_absent = bool(
        branch_inspection.state == "missing"
        or (
            branch_inspection.state == "exists"
            and branch_inspection.resolved_commit == expected_commit
        )
    )
    if source_path.exists() or registration is not None or not branch_is_owned_or_absent:
        raise _CloseGuardConflict(
            "close cleanup progress no longer has exact source ownership"
        )


def _close_request_matches_call(
    request: CloseRequest,
    *,
    actor: str,
    status: str,
    summary: str,
    validations: tuple[ValidationRecord, ...],
    residuals: tuple[str, ...],
    followup_candidates: tuple[str, ...],
    note: str | None,
    cleanup: bool,
    failure_class: str | None,
    recovery_action: str | None,
    prompt_issue: bool,
    operator_issue: bool,
    trusted_failure_details: bool,
) -> bool:
    failure_details = _close_failure_details(
        status=status,
        failure_class=failure_class,
        recovery_action=recovery_action,
        prompt_issue=prompt_issue,
        operator_issue=operator_issue,
        trusted_classification=trusted_failure_details,
    )
    return (
        request.actor == actor
        and request.status == status
        and request.summary == summary
        and request.validations
        == tuple((row.name, row.status) for row in validations)
        and request.residuals == tuple(residuals)
        and request.followup_candidates == tuple(followup_candidates)
        and request.note == note
        and request.cleanup_requested == cleanup
        and request.failure_class == failure_details["failure_class"]
        and request.recovery_action == failure_details["recovery_action"]
        and request.prompt_issue == failure_details["prompt_issue"]
        and request.operator_issue == failure_details["operator_issue"]
    )


def _inspect_core_close_finalization(
    profile: RepoProfile,
    *,
    request: CloseRequest,
) -> TaskFinalizationEvidence:
    return inspect_task_finalization(
        profile,
        finalization_id=request.finalization_id,
        workset_id=request.workset_id,
        task_id=request.task_id,
        attempt_id=request.attempt_id,
        actor=request.actor,
        status=request.status,
        summary=request.summary,
        changed_paths=tuple(request.pre_close_projection["changed_paths"]),
        validations=tuple(
            ValidationRecord(name=name, status=status)
            for name, status in request.validations
        ),
        residuals=request.residuals,
        followup_candidates=request.followup_candidates,
        commit=request.pre_close_projection["source_head_commit"],
        landed_commit=None,
        elapsed_seconds=None,
        failure_class=request.failure_class,
        recovery_action=request.recovery_action,
        prompt_issue=request.prompt_issue,
        operator_issue=request.operator_issue,
        note=request.note,
    )


def _exact_close_cleanup_event(
    profile: RepoProfile,
    *,
    request: CloseRequest,
) -> dict[str, Any] | None:
    event_id = request.cleanup_event_id
    if event_id is None:
        return None
    expected_payload = {
        "workset_id": request.workset_id,
        "task_id": request.task_id,
        "attempt_id": request.attempt_id,
        "branch": request.pre_close_projection["recorded_branch"],
        "worktree_path": request.pre_close_projection["resolved_source_path"],
        "cleanup_complete": True,
        "worktree_absent": True,
        "branch_absent": True,
    }
    with exclusive_file_lock(profile.paths.events_file):
        events = load_events(profile.paths.events_file)
    rows = []
    for event in events:
        payload = event.get("payload")
        same_target = (
            isinstance(payload, Mapping)
            and payload.get("workset_id") == request.workset_id
            and payload.get("task_id") == request.task_id
            and payload.get("attempt_id") == request.attempt_id
            and event.get("type") == "worktree.cleanup"
        )
        if event.get("event_id") == event_id or same_target:
            rows.append(event)
    if not rows:
        return None
    if len(rows) != 1:
        raise CloseTransactionError("close-owned cleanup event occurs more than once")
    row = rows[0]
    if (
        row.get("event_id") != event_id
        or row.get("type") != "worktree.cleanup"
        or row.get("actor") != "blackdog"
        or not strict_json_equal(row.get("payload"), expected_payload)
    ):
        raise CloseTransactionError("close-owned cleanup event conflicts with close intent")
    return dict(row)


def _close_transaction_state(
    profile: RepoProfile,
    *,
    request: CloseRequest,
) -> dict[str, Any]:
    close_event = load_close_event(profile, request)
    core_evidence = _inspect_core_close_finalization(profile, request=request)
    core = core_evidence.to_dict()
    cleanup_event = _exact_close_cleanup_event(profile, request=request)
    if close_event is not None:
        if not core["complete"]:
            raise CloseTransactionError("worktree.close exists without complete core finalization")
        recorded_core = close_event["core_finalization"]
        expected_core = {
            key: core[key]
            for key in (
                "request_event_id",
                "decision_event_id",
                "task_release_event_id",
                "workset_release_event_id",
                "task_finish_event_id",
                "runtime_finalized",
            )
        }
        if not strict_json_equal(recorded_core, expected_core):
            raise CloseTransactionError("worktree.close core evidence is not durable")
        cleanup = close_event["cleanup"]
        if cleanup["event_id"] is not None:
            if cleanup_event is None or cleanup["event_id"] != request.cleanup_event_id:
                raise CloseTransactionError("worktree.close cleanup evidence is not durable")
        elif request.cleanup_event_id is not None and not cleanup["retained"]:
            raise CloseTransactionError("worktree.close lacks cleanup proof or retention")
        return {
            "stage": "complete",
            "complete": True,
            "request": request,
            "core": core,
            "cleanup_event": cleanup_event,
            "close_event": close_event,
        }
    if core["successor_present"]:
        raise CloseTransactionError(
            "incomplete close transaction conflicts with a later task attempt"
        )
    if not core["complete"]:
        return {
            "stage": core["stage"],
            "complete": False,
            "request": request,
            "core": core,
            "cleanup_event": cleanup_event,
            "close_event": None,
        }
    if request.cleanup_event_id is not None and cleanup_event is None:
        stage = "cleanup_pending"
    elif cleanup_event is not None:
        stage = "cleanup_finalized"
    else:
        stage = "close_event_pending"
    return {
        "stage": stage,
        "complete": False,
        "request": request,
        "core": core,
        "cleanup_event": cleanup_event,
        "close_event": None,
    }


def _close_stage_mutation_phase(stage: str) -> str:
    return {
        "not_started": "close_request_recorded",
        "request_recorded": "close_core_request_recorded",
        "decision_recorded": "close_core_decision_recorded",
        "runtime_finalized": "close_runtime_finalized",
        "task_release_recorded": "close_task_release_recorded",
        "workset_release_recorded": "close_workset_release_recorded",
        "task_finish_recorded": "close_task_finish_recorded",
        "owned_events_complete": "close_task_finish_recorded",
        "cleanup_pending": "close_cleanup_pending",
        "cleanup_finalized": "close_cleanup_finalized",
        "close_event_pending": "close_event_pending",
        "complete": "close_complete",
        "conflict": "event_finalization_partial",
    }.get(stage, "event_finalization_partial")


def _close_core_evidence_payload(core: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: core.get(key)
        for key in (
            "request_event_id",
            "decision_event_id",
            "task_release_event_id",
            "workset_release_event_id",
            "task_finish_event_id",
            "runtime_finalized",
        )
    }


def _close_cleanup_evidence(
    request: CloseRequest,
    *,
    cleanup_event: Mapping[str, Any] | None,
) -> dict[str, Any]:
    projection = request.pre_close_projection
    event_verified = cleanup_event is not None
    performed = bool(
        event_verified and projection["cleanup_disposition"] == "remove"
    )
    return {
        "requested": request.cleanup_requested,
        "eligible": bool(projection["cleanup_eligible"]),
        "event_id": request.cleanup_event_id if event_verified else None,
        "performed": performed,
        "worktree_removed": bool(performed and projection["source_path_exists"]),
        "branch_deleted": bool(
            performed and projection["branch_state"] == "exists"
        ),
        "retained": not event_verified,
        "reason": str(projection["cleanup_reason"]),
        "proof": str(projection["cleanup_proof"]),
    }


def _close_event_payload(
    request: CloseRequest,
    *,
    core: Mapping[str, Any],
    cleanup_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "close_request_id": request.request_event_id,
        "finalization_id": request.finalization_id,
        "close_event_id": request.close_event_id,
        "workset_id": request.workset_id,
        "task_id": request.task_id,
        "attempt_id": request.attempt_id,
        "actor": request.actor,
        "status": request.status,
        "summary": request.summary,
        "branch": request.pre_close_projection["recorded_branch"],
        "target_branch": request.pre_close_projection["recorded_target_branch"],
        "worktree_path": request.pre_close_projection["resolved_source_path"],
        "changed_paths": list(request.pre_close_projection["changed_paths"]),
        "commit": request.pre_close_projection["source_head_commit"],
        "cleanup_requested": request.cleanup_requested,
        "cleanup_performed": bool(cleanup_evidence["performed"]),
        "cleanup_reason": cleanup_evidence["reason"],
        "failure_class": request.failure_class,
        "recovery_action": request.recovery_action,
        "prompt_issue": request.prompt_issue,
        "operator_issue": request.operator_issue,
        "core_finalization": _close_core_evidence_payload(core),
        "cleanup": dict(cleanup_evidence),
    }


def _close_result_payload(
    request: CloseRequest,
    *,
    state: Mapping[str, Any],
    mutation_started: bool,
    mutation_completed: bool,
) -> dict[str, Any]:
    close_event = state.get("close_event")
    cleanup = (
        close_event.get("cleanup")
        if isinstance(close_event, Mapping)
        else _close_cleanup_evidence(
            request,
            cleanup_event=state.get("cleanup_event"),
        )
    )
    cleanup_pending = bool(
        request.cleanup_requested and cleanup.get("retained")
    )
    return {
        "workset_id": request.workset_id,
        "task_id": request.task_id,
        "attempt_id": request.attempt_id,
        "status": request.status,
        "summary": request.summary,
        "branch": request.pre_close_projection["recorded_branch"],
        "target_branch": request.pre_close_projection["recorded_target_branch"],
        "worktree_path": request.pre_close_projection["resolved_source_path"],
        "changed_paths": list(request.pre_close_projection["changed_paths"]),
        "commit": request.pre_close_projection["source_head_commit"],
        "cleanup_requested": request.cleanup_requested,
        "cleanup_performed": bool(cleanup["performed"]),
        "cleanup_reason": cleanup["reason"],
        "cleanup": dict(cleanup),
        "failure_class": request.failure_class,
        "recovery_action": request.recovery_action,
        "prompt_issue": request.prompt_issue,
        "operator_issue": request.operator_issue,
        "close_request_id": request.request_event_id,
        "finalization_id": request.finalization_id,
        "close_event_id": request.close_event_id,
        "cleanup_event_id": request.cleanup_event_id,
        "close_transaction_stage": state["stage"],
        "close_transaction_complete": bool(state["complete"]),
        "core_finalization": _close_core_evidence_payload(state["core"]),
        "operation_status": "partial" if cleanup_pending else "succeeded",
        "mutation_started": mutation_started,
        "mutation_completed": bool(mutation_completed and not cleanup_pending),
        "mutation_phase": (
            "runtime_finalized_cleanup_pending"
            if cleanup_pending
            else _close_stage_mutation_phase(str(state["stage"]))
        ),
    }


def _run_close_transaction(
    profile: RepoProfile,
    *,
    request: CloseRequest,
) -> dict[str, Any]:
    state = _close_transaction_state(profile, request=request)
    if state["complete"]:
        return _close_result_payload(
            request,
            state=state,
            mutation_started=False,
            mutation_completed=False,
        )
    initial_stage = str(state["stage"])
    if not state["core"]["complete"]:
        _verify_frozen_close_projection(
            profile,
            request=request,
            allow_owned_cleanup_progress=False,
        )
        finish_task(
            profile,
            workset_id=request.workset_id,
            task_id=request.task_id,
            attempt_id=request.attempt_id,
            actor=request.actor,
            status=request.status,
            summary=request.summary,
            changed_paths=tuple(request.pre_close_projection["changed_paths"]),
            validations=tuple(
                ValidationRecord(name=name, status=status)
                for name, status in request.validations
            ),
            residuals=request.residuals,
            followup_candidates=request.followup_candidates,
            commit=request.pre_close_projection["source_head_commit"],
            landed_commit=None,
            elapsed_seconds=None,
            failure_class=request.failure_class,
            recovery_action=request.recovery_action,
            prompt_issue=request.prompt_issue,
            operator_issue=request.operator_issue,
            note=request.note,
            finalization_id=request.finalization_id,
        )
        state = _close_transaction_state(profile, request=request)
        if not state["core"]["complete"]:
            raise CloseTransactionError(
                "core task finalization did not produce complete durable proof"
            )
    # A retained source was already frozen and re-proved before the
    # irreversible core finalization.  Once core is terminal, later source
    # movement cannot become cleanup authority and must not prevent the
    # deterministic retention receipt from converging.  Re-prove live source
    # ownership here only when this transaction is actually about to mutate
    # that source through its owned cleanup event.
    if request.cleanup_event_id is not None and state["cleanup_event"] is None:
        _verify_frozen_close_projection(
            profile,
            request=request,
            allow_owned_cleanup_progress=True,
        )
    if request.cleanup_event_id is not None and state["cleanup_event"] is None:
        cleanup_task_worktree(
            profile,
            workset_id=request.workset_id,
            task_id=request.task_id,
            path=request.pre_close_projection["resolved_source_path"],
            branch=request.pre_close_projection["recorded_branch"],
            _attempt_lock_held=True,
            _expected_attempt_id=request.attempt_id,
            _close_request_id=request.request_event_id,
            _expected_source_head=request.pre_close_projection["source_head_commit"],
            _expected_cleanup_event_id=request.cleanup_event_id,
        )
        state = _close_transaction_state(profile, request=request)
        if state["cleanup_event"] is None:
            raise CloseTransactionError(
                "close-owned cleanup did not produce its deterministic event"
            )
    if state["close_event"] is None:
        cleanup_evidence = _close_cleanup_evidence(
            request,
            cleanup_event=state["cleanup_event"],
        )
        record_close_event(
            profile,
            request=request,
            payload=_close_event_payload(
                request,
                core=state["core"],
                cleanup_evidence=cleanup_evidence,
            ),
        )
    final_state = _close_transaction_state(profile, request=request)
    if not final_state["complete"]:
        raise CloseTransactionError("task close transaction did not complete")
    return _close_result_payload(
        request,
        state=final_state,
        mutation_started=initial_stage != "complete",
        mutation_completed=True,
    )


def _pending_close_transaction(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
) -> dict[str, Any] | None:
    requests = close_requests_for_task(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    incomplete = [
        state
        for request in requests
        if not (state := _close_transaction_state(profile, request=request))["complete"]
    ]
    if len(incomplete) > 1:
        raise CloseTransactionError("task has multiple incomplete close transactions")
    return incomplete[0] if incomplete else None


def _close_retry_action(
    profile: RepoProfile,
    request: CloseRequest,
    *,
    guarded: bool = True,
) -> LifecycleAction:
    argv = [
        _lifecycle_blackdog_executable(profile, {}),
        "task",
        "close",
        f"--project-root={profile.paths.project_root}",
        f"--workset={request.workset_id}",
        f"--task={request.task_id}",
        f"--actor={request.actor}",
        f"--status={request.status}",
        f"--summary={request.summary}",
        f"--failure-class={request.failure_class}",
        f"--recovery-action={request.recovery_action}",
    ]
    if guarded:
        argv.append(f"--close-request={request.request_event_id}")
    argv.extend(
        f"--validation={name}={status}" for name, status in request.validations
    )
    argv.extend(f"--residual={item}" for item in request.residuals)
    argv.extend(f"--followup={item}" for item in request.followup_candidates)
    if request.note is not None:
        argv.append(f"--note={request.note}")
    if request.cleanup_requested:
        argv.append("--cleanup")
    if request.prompt_issue:
        argv.append("--prompt-issue")
    if request.operator_issue:
        argv.append("--operator-issue")
    return LifecycleAction(
        action_id="retry_task_close_finalization",
        disposition="retryable",
        reason_code=(
            "task_close_finalization_pending"
            if guarded
            else "task_close_request_not_recorded"
        ),
        reason_detail=(
            "The durable close transaction must finish its exact core, cleanup, and receipt stages."
            if guarded
            else "The close request was not durably recorded; retry the full close invocation."
        ),
        argv=tuple(argv),
        safety_class="validated_mutation",
        mutation_class="git_and_runtime",
        display="Retry exact task close finalization",
    )


def _close_blocked_payload(
    profile: RepoProfile,
    *,
    request: CloseRequest,
    exc: Exception,
    durable_request: bool,
    mutation_started: bool,
) -> dict[str, Any]:
    state: dict[str, Any] | None = None
    state_error: str | None = None
    if durable_request:
        try:
            state = _close_transaction_state(profile, request=request)
        except (BacklogError, CloseTransactionError, OSError, StoreError) as state_exc:
            state_error = str(state_exc)
    stage = str(state.get("stage") if state is not None else "conflict")
    conflict_detail = (
        state_error
        or (
            str(exc)
            if isinstance(exc, (_CloseGuardConflict, CleanupOwnershipError))
            else None
        )
    )
    action = (
        _close_conflict_action(conflict_detail)
        if conflict_detail is not None
        else NextAction.command(
            _close_retry_action(profile, request, guarded=durable_request)
        )
    )
    payload = {
        "workset_id": request.workset_id,
        "task_id": request.task_id,
        "attempt_id": request.attempt_id,
        "status": request.status,
        "summary": request.summary,
        "branch": request.pre_close_projection["recorded_branch"],
        "target_branch": request.pre_close_projection["recorded_target_branch"],
        "worktree_path": request.pre_close_projection["resolved_source_path"],
        "changed_paths": list(request.pre_close_projection["changed_paths"]),
        "commit": request.pre_close_projection["source_head_commit"],
        "cleanup_requested": request.cleanup_requested,
        "cleanup_performed": False,
        "cleanup_reason": request.pre_close_projection["cleanup_reason"],
        "cleanup": None,
        "failure_class": request.failure_class,
        "recovery_action": request.recovery_action,
        "prompt_issue": request.prompt_issue,
        "operator_issue": request.operator_issue,
        "close_request_id": request.request_event_id if durable_request else None,
        "finalization_id": request.finalization_id,
        "close_event_id": request.close_event_id,
        "cleanup_event_id": request.cleanup_event_id,
        "close_transaction_stage": stage,
        "close_transaction_complete": False,
        "close_transaction_blocked": True,
        "error": str(exc),
        "evidence_error": state_error,
        "mutation_started": mutation_started,
        "mutation_completed": False,
        "mutation_phase": (
            _close_stage_mutation_phase(stage)
            if durable_request
            else "preflight"
        ),
        "next_action": action.to_dict(),
        "recommended_actions": [action.display],
        "recommended_commands": action.legacy_command_rows(),
    }
    if state is not None:
        payload["core_finalization"] = _close_core_evidence_payload(state["core"])
    return payload


def _close_conflict_action(detail: str) -> NextAction:
    return NextAction.terminal(
        action_id="inspect_task_close_conflict",
        kind="blocked",
        disposition="proof_required",
        reason_code="task_close_evidence_conflict",
        reason_detail=detail,
        display="Inspect conflicting close transaction evidence",
        required_inputs=("canonical_task_close_transaction",),
    )


def _pending_close_action(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
) -> tuple[dict[str, Any] | None, NextAction | None]:
    try:
        pending = _pending_close_transaction(
            profile,
            workset_id=workset_id,
            task_id=task_id,
        )
    except (BacklogError, CloseTransactionError, OSError, StoreError) as exc:
        return {
            "stage": "conflict",
            "complete": False,
            "error": str(exc),
        }, _close_conflict_action(str(exc))
    if pending is None:
        return None, None
    request = pending["request"]
    return pending, NextAction.command(_close_retry_action(profile, request))


def inspect_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    include_reconciliation_detection: bool = False,
) -> dict[str, Any]:
    return _task_recovery_payload(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        include_reconciliation_detection=include_reconciliation_detection,
    )


def close_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str | None = None,
    task_id: str | None = None,
    actor: str | None = None,
    status: str | None = None,
    summary: str | None = None,
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
    cleanup: bool | None = None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool | None = None,
    operator_issue: bool | None = None,
    close_request_id: str | None = None,
    _attempt_lock_held: bool = False,
    _trusted_failure_details: bool = False,
) -> dict[str, Any]:
    guarded_request = None
    if close_request_id is not None:
        try:
            guarded_request = load_close_request_by_id(
                profile,
                close_request_id,
            )
        except (BacklogError, CloseTransactionError, OSError, StoreError) as exc:
            request_record = load_close_request_record_by_id(
                profile,
                close_request_id,
            )
            if request_record is None:
                raise
            return _close_blocked_payload(
                profile,
                request=request_record,
                exc=_CloseGuardConflict(str(exc)),
                durable_request=True,
                mutation_started=False,
            )
        if guarded_request is None:
            raise CloseTransactionError(
                "guarded task close names an unknown close request"
            )
        if workset_id is not None and guarded_request.workset_id != workset_id:
            raise CloseTransactionError(
                "guarded task close target conflicts with its durable request"
            )
        if task_id is not None and guarded_request.task_id != task_id:
            raise CloseTransactionError(
                "guarded task close target conflicts with its durable request"
            )
        workset_id = guarded_request.workset_id
        task_id = guarded_request.task_id
        actor = actor if actor is not None else guarded_request.actor
        status = status if status is not None else guarded_request.status
        summary = summary if summary is not None else guarded_request.summary
        validations = validations or tuple(
            ValidationRecord(name=name, status=value)
            for name, value in guarded_request.validations
        )
        residuals = residuals or guarded_request.residuals
        followup_candidates = (
            followup_candidates or guarded_request.followup_candidates
        )
        note = note if note is not None else guarded_request.note
        cleanup = (
            cleanup if cleanup is not None else guarded_request.cleanup_requested
        )
        failure_class = (
            failure_class
            if failure_class is not None
            else guarded_request.failure_class
        )
        recovery_action = (
            recovery_action
            if recovery_action is not None
            else guarded_request.recovery_action
        )
        prompt_issue = (
            prompt_issue
            if prompt_issue is not None
            else guarded_request.prompt_issue
        )
        operator_issue = (
            operator_issue
            if operator_issue is not None
            else guarded_request.operator_issue
        )
    if (
        workset_id is None
        or task_id is None
        or actor is None
        or status is None
        or summary is None
    ):
        raise BacklogError(
            "task close requires workset, task, actor, status, and summary"
        )
    cleanup = bool(cleanup)
    prompt_issue = bool(prompt_issue)
    operator_issue = bool(operator_issue)
    workset, task = _require_workset_and_task(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    runtime_state = load_runtime_state(profile.paths)
    attempt = (
        find_task_attempt(runtime_state, workset_id, guarded_request.attempt_id)
        if guarded_request is not None
        else active_task_attempt(runtime_state, workset_id, task_id)
    )
    if guarded_request is not None and (
        attempt is None
        or attempt.task_id != task_id
        or attempt.actor != guarded_request.actor
    ):
        raise CloseTransactionError(
            "guarded task close request no longer names its exact attempt"
        )
    if attempt is None:
        latest = latest_task_attempt(runtime_state, workset_id, task_id)
        completed_requests = close_requests_for_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
        )
        completed_request = next(
            (
                request
                for request in reversed(completed_requests)
                if latest is not None and request.attempt_id == latest.attempt_id
            ),
            None,
        )
        if completed_request is not None:
            attempt = latest
            guarded_request = completed_request
        latest_transaction = (
            load_landing_transaction(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=latest.attempt_id,
            )
            if latest is not None
            else None
        )
        if attempt is None and (
            latest is not None
            and latest_transaction is not None
            and (
                latest_transaction.aborted
                or (
                    latest_transaction.complete
                    and latest_transaction.abort_requested
                )
            )
        ):
            attempt = latest
    if attempt is None:
        raise BacklogError(f"No active WTAM attempt for task {task_id!r} in workset {workset_id!r}")
    if (
        guarded_request is None
        and actor != str(getattr(attempt, "actor", "") or "").strip()
    ):
        raise BacklogError(
            f"Attempt {attempt.attempt_id!r} is owned by {attempt.actor}, not {actor}"
        )
    if not _attempt_lock_held:
        with attempt_lifecycle_lock(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
        ):
            return close_task_worktree(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                actor=actor,
                status=status,
                summary=summary,
                validations=validations,
                residuals=residuals,
                followup_candidates=followup_candidates,
                note=note,
                cleanup=cleanup,
                failure_class=failure_class,
                recovery_action=recovery_action,
                prompt_issue=prompt_issue,
                operator_issue=operator_issue,
                close_request_id=close_request_id,
                _attempt_lock_held=True,
                _trusted_failure_details=_trusted_failure_details,
            )
    runtime_state = load_runtime_state(profile.paths)
    if attempt.status == ATTEMPT_STATUS_IN_PROGRESS:
        locked_attempt = active_task_attempt(runtime_state, workset_id, task_id)
        if locked_attempt is None or locked_attempt.attempt_id != attempt.attempt_id:
            raise BacklogError(
                "task attempt changed while waiting for the close operation lock"
            )
        _require_workspace_adoption_start_evidence(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
            attempt=locked_attempt,
        )
        _require_ordinary_resume_start_evidence(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            runtime_state=runtime_state,
            attempt=locked_attempt,
        )
        attempt = locked_attempt
    transaction = load_landing_transaction(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt.attempt_id,
    )
    resolved_summary = str(summary or "").strip()
    if not resolved_summary:
        raise BacklogError("task close summary must be nonblank")
    close_request = (
        _landing_abort_close_request(
            intent=transaction.intent,
            actor=actor,
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
        if transaction is not None
        and (not transaction.complete or transaction.abort_requested)
        else None
    )
    if (
        transaction is not None
        and transaction.outcome == "abort_complete"
        and attempt.status == ATTEMPT_STATUS_SUCCESS
    ):
        assert close_request is not None
        before_fingerprint = _landing_side_effect_fingerprint(
            profile,
            intent=transaction.intent,
        )
        _verify_landing_abort_chain(
            profile,
            transaction=transaction,
            close_request=close_request,
            require_source=_aborted_source_requires_live_proof(
                profile,
                transaction=transaction,
            ),
        )
        candidate = (
            transaction.abort_data.get("landed_commit")
            if transaction.abort_data is not None
            else None
        )
        if (
            not isinstance(candidate, str)
            or attempt.landed_commit != candidate
            or not _landing_abort_target_state(
                intent=transaction.intent,
                landed_commit=candidate,
            )[1]
        ):
            raise LandingTransactionError(
                "reconciled landing abort no longer matches its canonical target proof"
            )
        _append_landing_abort_close_event_once(
            profile,
            transaction=transaction,
        )
        payload = _landing_abort_close_event_payload(transaction=transaction)
        payload.update(
            {
                "status": ATTEMPT_STATUS_SUCCESS,
                "summary": attempt.summary,
                "landed_commit": candidate,
                "cleanup": None,
                "attempt_active": False,
                "closure_refused": False,
                "abort_reconciled": True,
                "landing_transaction": transaction.to_dict(),
                "mutation_phase": _landing_operation_phase(transaction),
                "mutation_observed": before_fingerprint
                != _landing_side_effect_fingerprint(
                    profile,
                    intent=transaction.intent,
                ),
            }
        )
        return payload
    if transaction is not None and transaction.outcome == "abort_complete":
        assert close_request is not None
        before_fingerprint = _landing_side_effect_fingerprint(
            profile,
            intent=transaction.intent,
        )
        transaction, finished = _finalize_aborted_landing(
            profile,
            transaction=transaction,
            close_request=close_request,
            require_source=_aborted_source_requires_live_proof(
                profile,
                transaction=transaction,
            ),
        )
        payload = {
            **_landing_abort_close_event_payload(transaction=transaction),
            "cleanup": None,
            "attempt_active": False,
            "closure_refused": False,
            "abort_complete": True,
            "landing_transaction": transaction.to_dict(),
            "mutation_phase": _landing_operation_phase(transaction),
            "mutation_observed": before_fingerprint
            != _landing_side_effect_fingerprint(
                profile,
                intent=transaction.intent,
            ),
        }
        if finished is not None:
            payload["attempt_id"] = finished.attempt_id
        return payload
    if (
        transaction is not None
        and transaction.complete
        and transaction.abort_requested
    ):
        assert close_request is not None
        _verify_landing_abort_chain(
            profile,
            transaction=transaction,
            close_request=close_request,
            require_source=not transaction.intent.cleanup,
        )
        payload = _landing_result_payload(profile, transaction=transaction)
        payload["close_superseded_by_landing"] = True
        payload["closure_refused"] = False
        payload["mutation_observed"] = False
        return payload
    if transaction is not None and not transaction.terminal:
        before_fingerprint = _landing_side_effect_fingerprint(
            profile,
            intent=transaction.intent,
        )
        try:
            transaction = _abort_landing_for_close(
                profile,
                workset=workset,
                task=task,
                transaction=transaction,
                close_request=close_request or {},
            )
            if not transaction.abort_requested or transaction.abort_superseded:
                transaction, _finished = _run_landing_transaction(
                    profile,
                    workset=workset,
                    task=task,
                    transaction=transaction,
                )
                payload = _landing_result_payload(profile, transaction=transaction)
                payload["close_superseded_by_landing"] = True
                payload["closure_refused"] = False
                payload["mutation_observed"] = before_fingerprint != _landing_side_effect_fingerprint(
                    profile,
                    intent=transaction.intent,
                )
                return payload
            transaction, finished = _finalize_aborted_landing(
                profile,
                transaction=transaction,
                close_request=close_request or {},
            )
            if transaction.abort_superseded:
                transaction, _finished = _run_landing_transaction(
                    profile,
                    workset=workset,
                    task=task,
                    transaction=transaction,
                )
                payload = _landing_result_payload(profile, transaction=transaction)
                payload["close_superseded_by_landing"] = True
                payload["closure_refused"] = False
                payload["mutation_observed"] = before_fingerprint != _landing_side_effect_fingerprint(
                    profile,
                    intent=transaction.intent,
                )
                return payload
            assert transaction.abort_data is not None
            stored_request = transaction.abort_data["close_request"]
            assert isinstance(stored_request, Mapping)
            payload = {
                **_landing_abort_close_event_payload(transaction=transaction),
                "status": stored_request["status"],
                "summary": stored_request["summary"],
                "cleanup": None,
                "attempt_active": False,
                "closure_refused": False,
                "abort_complete": transaction.abort_complete,
                "landing_transaction": transaction.to_dict(),
                "mutation_phase": _landing_operation_phase(transaction),
                "mutation_observed": before_fingerprint != _landing_side_effect_fingerprint(
                    profile,
                    intent=transaction.intent,
                ),
            }
            if finished is not None:
                payload["attempt_id"] = finished.attempt_id
            return payload
        except Exception as exc:
            current = load_landing_transaction(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=attempt.attempt_id,
            )
            payload = _landing_blocked_payload(
                profile,
                attempt=attempt,
                exc=exc if isinstance(exc, Exception) else WorktreeError(str(exc)),
                transaction=current,
                cleanup=cleanup,
            )
            payload["closure_refused"] = True
            payload["mutation_observed"] = bool(
                current is not None
                and before_fingerprint
                != _landing_side_effect_fingerprint(
                    profile,
                    intent=current.intent,
                )
            )
            return payload
    close_side_effect_before = _task_begin_side_effect_fingerprint(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    request = guarded_request or load_close_request(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt.attempt_id,
    )
    if request is None:
        request = _close_request_for_attempt(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt=attempt,
            actor=actor,
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
            trusted_failure_details=_trusted_failure_details,
        )
        try:
            record_close_request(profile, request)
        except Exception as exc:
            durable = False
            conflict: Exception | None = None
            try:
                occupied_request = load_close_request_by_id(
                    profile,
                    request.request_event_id,
                )
            except Exception as evidence_exc:
                # The deterministic identity is occupied but cannot be proved
                # as this canonical request.  Recovery must stop commandless;
                # an unguarded retry cannot repair corrupt ledger evidence.
                durable = True
                conflict = _CloseGuardConflict(str(evidence_exc))
            else:
                durable = occupied_request == request
                if occupied_request is not None and not durable:
                    request = occupied_request
                    durable = True
                    conflict = _CloseGuardConflict(
                        "concurrent task close semantics conflict with the durable request"
                    )
            return _close_blocked_payload(
                profile,
                request=request,
                exc=(
                    conflict
                    or (exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
                ),
                durable_request=durable,
                mutation_started=(
                    False
                    if conflict is not None
                    else close_side_effect_before
                    != _task_begin_side_effect_fingerprint(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                    )
                ),
            )
    try:
        if not _close_request_matches_call(
            request,
            actor=actor,
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
            trusted_failure_details=bool(
                _trusted_failure_details or close_request_id is not None
            ),
        ):
            raise _CloseGuardConflict(
                "task close retry semantics conflict with its durable request"
            )
        return _run_close_transaction(profile, request=request)
    except Exception as exc:
        return _close_blocked_payload(
            profile,
            request=request,
            exc=exc if isinstance(exc, Exception) else RuntimeError(str(exc)),
            durable_request=True,
            mutation_started=(
                close_side_effect_before
                != _task_begin_side_effect_fingerprint(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                )
            ),
        )


def cleanup_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    path: str | None = None,
    branch: str | None = None,
    _attempt_lock_held: bool = False,
    _landing_transaction_id: str | None = None,
    _expected_attempt_id: str | None = None,
    _close_request_id: str | None = None,
    _expected_source_head: str | None = None,
    _expected_cleanup_event_id: str | None = None,
) -> dict[str, Any]:
    _workset, task = _require_workset_and_task(profile, workset_id=workset_id, task_id=task_id)
    runtime_state = load_runtime_state(profile.paths)
    latest_attempt = latest_task_attempt(runtime_state, workset_id, task_id)
    if _expected_attempt_id is not None and (
        latest_attempt is None or latest_attempt.attempt_id != _expected_attempt_id
    ):
        raise CleanupOwnershipError(
            worktree_path=Path(path or profile.paths.project_root).resolve(strict=False),
            branch=branch,
            expected_worktree_path=(
                Path(latest_attempt.worktree_path).resolve(strict=False)
                if latest_attempt is not None and latest_attempt.worktree_path
                else None
            ),
            expected_branch=latest_attempt.branch if latest_attempt is not None else None,
            detail="the exact cleanup attempt is no longer the latest task attempt",
        )
    pending_close = _pending_close_transaction(
        profile,
        workset_id=workset_id,
        task_id=task_id,
    )
    if pending_close is not None:
        pending_request = pending_close["request"]
        if _close_request_id != pending_request.request_event_id:
            raise CloseTransactionError(
                "task cleanup is gated by its incomplete close transaction"
            )
        if (
            _expected_attempt_id != pending_request.attempt_id
            or path != pending_request.pre_close_projection["resolved_source_path"]
            or branch != pending_request.pre_close_projection["recorded_branch"]
        ):
            raise CloseTransactionError(
                "close-owned cleanup invocation conflicts with its durable request"
            )
    if latest_attempt is not None and not _attempt_lock_held:
        with attempt_lifecycle_lock(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=latest_attempt.attempt_id,
        ):
            return cleanup_task_worktree(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                path=path,
                branch=branch,
                _attempt_lock_held=True,
                _landing_transaction_id=_landing_transaction_id,
                _expected_attempt_id=_expected_attempt_id,
                _close_request_id=_close_request_id,
                _expected_source_head=_expected_source_head,
                _expected_cleanup_event_id=_expected_cleanup_event_id,
            )
    runtime_state = load_runtime_state(profile.paths)
    latest_attempt = latest_task_attempt(runtime_state, workset_id, task_id)
    if _expected_attempt_id is not None and (
        latest_attempt is None or latest_attempt.attempt_id != _expected_attempt_id
    ):
        raise CloseTransactionError(
            "exact cleanup attempt changed while waiting for its lifecycle lock"
        )
    primary_root = find_primary_worktree(profile.paths.project_root)
    expected_branch = (
        (latest_attempt.branch if latest_attempt is not None else None)
        or default_task_branch(workset_id, task)
    )
    expected_path = (
        Path(latest_attempt.worktree_path).resolve()
        if latest_attempt is not None and latest_attempt.worktree_path
        else default_task_worktree_path(
            profile,
            workset_id=workset_id,
            task=task,
        ).resolve()
    )
    resolved_branch = (
        branch
        or expected_branch
    )
    resolved_path: Path
    if path is not None:
        resolved_path = Path(path).resolve()
    elif latest_attempt is not None and latest_attempt.worktree_path:
        resolved_path = Path(latest_attempt.worktree_path).resolve()
    else:
        resolved_path = expected_path
    derived_cleanup_event_id = _task_cleanup_event_id(
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=latest_attempt.attempt_id if latest_attempt is not None else None,
        branch=resolved_branch,
        worktree_path=str(resolved_path),
    )
    if (
        _expected_cleanup_event_id is not None
        and derived_cleanup_event_id != _expected_cleanup_event_id
    ):
        raise CloseTransactionError(
            "close-owned cleanup identity conflicts with its durable request"
        )
    if branch is not None and resolved_branch != expected_branch:
        raise CleanupOwnershipError(
            worktree_path=resolved_path,
            branch=resolved_branch,
            expected_worktree_path=expected_path,
            expected_branch=expected_branch,
            detail=(
                f"requested branch {resolved_branch!r} does not match the durable task branch "
                f"{expected_branch!r}"
            ),
        )
    if path is not None and resolved_path != expected_path:
        raise CleanupOwnershipError(
            worktree_path=resolved_path,
            branch=resolved_branch,
            expected_worktree_path=expected_path,
            expected_branch=expected_branch,
            detail=(
                f"requested path {resolved_path} does not match the durable task worktree "
                f"{expected_path}"
            ),
        )
    path_exists = resolved_path.exists()
    worktree_exists = path_exists and _is_git_worktree_path(resolved_path)
    worktree_registration = (
        _registered_worktree_row(primary_root, resolved_path)
        if worktree_exists
        else None
    )
    if worktree_exists:
        expected_branch_ref = f"refs/heads/{resolved_branch}"
        registration_branch = (
            str(worktree_registration.get("branch") or "")
            if worktree_registration is not None
            else ""
        )
        if resolved_path == primary_root.resolve():
            detail = "the primary worktree is never a task-cleanup target"
        elif worktree_registration is None:
            detail = "the path is not registered to the task repository"
        elif "detached" in worktree_registration or not registration_branch:
            detail = "the registered worktree is detached"
        elif registration_branch != expected_branch_ref:
            detail = (
                f"the registered worktree belongs to {registration_branch!r}, "
                f"not {expected_branch_ref!r}"
            )
        else:
            detail = ""
        if detail:
            raise CleanupOwnershipError(
                worktree_path=resolved_path,
                branch=resolved_branch,
                expected_worktree_path=expected_path,
                expected_branch=expected_branch,
                detail=detail,
            )
    transaction = (
        load_landing_transaction(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=latest_attempt.attempt_id,
        )
        if latest_attempt is not None
        else None
    )
    if transaction is not None and not transaction.terminal:
        authorized = _landing_transaction_id == transaction.transaction_id
        if authorized and "land_event_recorded" not in transaction.phases:
            raise LandingTransactionError(
                "landing driver cannot clean source before worktree.land evidence is durable"
            )
        if not authorized:
            action = (
                _landing_abort_close_action(transaction)
                if transaction.aborted
                else _landing_resume_action(transaction.intent)
            )
            return {
                "worktree_path": str(resolved_path),
                "worktree_existed": worktree_exists,
                "worktree_removed": False,
                "branch": resolved_branch,
                "deleted_branch": False,
                "branch_cleanup_reason": "landing transaction owns this attempt",
                "branch_cleanup_proof": "landing_transaction_incomplete",
                "force_deleted_branch": False,
                "cleanup_complete": False,
                "cleanup_refused": True,
                "error": "refusing cleanup: resume the incomplete landing transaction",
                "transaction_id": transaction.transaction_id,
                "landing_transaction": transaction.to_dict(),
                "mutation_phase": _landing_operation_phase(transaction),
                "recommended_actions": [action.display],
                "recommended_commands": NextAction.command(action).legacy_command_rows(),
            }
    active_attempt = active_task_attempt(runtime_state, workset_id, task_id)
    if active_attempt is not None:
        if not worktree_exists:
            raise MissingTaskWorktreeError(resolved_path)
        raise WorktreeError("refusing cleanup: active attempts must be landed or closed before cleanup")
    if worktree_exists and _managed_status_dirty(profile, resolved_path):
        raise WorktreeError(f"refusing cleanup: worktree has uncommitted changes: {resolved_path}")
    adoption_branch_cleanup = (
        _workspace_adoption_completion_branch_cleanup_plan(
            profile,
            primary_root=primary_root,
            workset_id=workset_id,
            task_id=task_id,
            branch=resolved_branch,
            worktree_path=resolved_path,
            latest_attempt=latest_attempt,
            runtime_state=runtime_state,
        )
        if resolved_branch
        else None
    )
    branch_cleanup = adoption_branch_cleanup or (
        _plan_task_branch_cleanup(
            primary_root,
            branch=resolved_branch,
            latest_attempt=latest_attempt,
        )
        if resolved_branch
        else _BranchCleanupPlan(
            branch_exists=False,
            force_delete=False,
            branch_tip=None,
            reason="no branch recorded",
            proof_state="no_branch",
        )
    )
    expected_head_conflict = bool(
        _expected_source_head is not None
        and (
            (
                branch_cleanup.branch_exists
                and branch_cleanup.branch_tip != _expected_source_head
            )
            or (not branch_cleanup.branch_exists and resolved_path.exists())
        )
    )
    if expected_head_conflict:
        raise CleanupOwnershipError(
            worktree_path=resolved_path,
            branch=resolved_branch,
            expected_worktree_path=expected_path,
            expected_branch=expected_branch,
            detail="the task branch moved after the close cleanup projection was recorded",
        )
    if worktree_exists:
        assert worktree_registration is not None
        registered_head = str(worktree_registration.get("HEAD") or "").strip()
        actual_head = _run_git(resolved_path, "rev-parse", "HEAD")
        if (
            not branch_cleanup.branch_exists
            or branch_cleanup.branch_tip is None
            or registered_head != branch_cleanup.branch_tip
            or actual_head != branch_cleanup.branch_tip
        ):
            raise CleanupOwnershipError(
                worktree_path=resolved_path,
                branch=resolved_branch,
                expected_worktree_path=expected_path,
                expected_branch=expected_branch,
                detail=(
                    "the registered worktree HEAD does not match the inspected task-branch tip"
                ),
            )
    if worktree_exists:
        _run_git(primary_root, "worktree", "remove", str(resolved_path))
    deleted_branch = False
    if resolved_branch and branch_cleanup.branch_exists:
        current_inspection = _inspect_branch_ref(
            primary_root,
            resolved_branch,
            role="task_branch",
        )
        if current_inspection.state == "error":
            detail = str(_inspection_error(current_inspection))
            if worktree_exists:
                raise CleanupPostMutationError(
                    worktree_path=resolved_path,
                    branch=resolved_branch,
                    branch_cleanup_reason=branch_cleanup.reason,
                    branch_cleanup_proof=branch_cleanup.proof_state,
                    force_delete=branch_cleanup.force_delete,
                    detail=detail,
                )
            raise _inspection_error(current_inspection)
        if current_inspection.state == "exists":
            current_tip = current_inspection.resolved_commit
            if current_tip != branch_cleanup.branch_tip:
                detail = f"refusing cleanup: branch {resolved_branch} changed during cleanup"
                if worktree_exists:
                    raise CleanupPostMutationError(
                        worktree_path=resolved_path,
                        branch=resolved_branch,
                        branch_cleanup_reason=branch_cleanup.reason,
                        branch_cleanup_proof=branch_cleanup.proof_state,
                        force_delete=branch_cleanup.force_delete,
                        detail=detail,
                    )
                raise WorktreeError(detail)
            delete_flag = "-D" if branch_cleanup.force_delete else "-d"
            delete = _run_git_no_check(primary_root, "branch", delete_flag, resolved_branch)
            if delete.returncode == 0:
                deleted_branch = True
            else:
                detail = delete.stderr.strip() or delete.stdout.strip() or f"exit code {delete.returncode}"
                failure_detail = f"git branch {delete_flag} {resolved_branch} failed: {detail}"
                if worktree_exists:
                    raise CleanupPostMutationError(
                        worktree_path=resolved_path,
                        branch=resolved_branch,
                        branch_cleanup_reason=branch_cleanup.reason,
                        branch_cleanup_proof=branch_cleanup.proof_state,
                        force_delete=branch_cleanup.force_delete,
                        detail=failure_detail,
                    )
                raise WorktreeError(failure_detail)
    attempt_id = latest_attempt.attempt_id if latest_attempt is not None else None
    event_payload = {
        "workset_id": workset_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "branch": resolved_branch,
        "worktree_path": str(resolved_path),
        "cleanup_complete": True,
        "worktree_absent": True,
        "branch_absent": True,
    }
    cleanup_event_id = derived_cleanup_event_id
    result_payload = {
        "worktree_path": str(resolved_path),
        "worktree_existed": worktree_exists,
        "worktree_removed": worktree_exists,
        "branch": resolved_branch,
        "deleted_branch": deleted_branch,
        "branch_cleanup_reason": branch_cleanup.reason,
        "branch_cleanup_proof": branch_cleanup.proof_state,
        "force_deleted_branch": bool(deleted_branch and branch_cleanup.force_delete),
        "cleanup_complete": True,
        "cleanup_event_id": cleanup_event_id,
    }
    try:
        event_appended = append_event_once(
            profile.paths.events_file,
            event_id=cleanup_event_id,
            event_type="worktree.cleanup",
            actor="blackdog",
            payload=event_payload,
        )
    except Exception as exc:
        raise CleanupEventFinalizationError(
            cleanup_payload=result_payload,
            detail=str(exc),
        ) from exc
    result_payload["event_appended"] = event_appended
    result_payload["event_finalized"] = True
    return result_payload


def render_preflight_text(payload: dict[str, Any]) -> str:
    dirty = "yes" if payload["dirty"] else "no"
    implementation_dirty = "yes" if payload["implementation_dirty"] else "no"
    primary_clean = "yes" if not payload["primary_dirty"] else "no"
    primary = "yes" if payload["current_is_primary"] else f"no (hint: {payload['primary_worktree']})"
    location = "inside repo" if payload["worktrees_dir_inside_repo"] else "outside repo"
    workspace_blackdog = (
        payload["current_worktree_blackdog_path"] if payload["current_worktree_has_local_blackdog"] else "blackdog"
    )
    lines = [
        f"[blackdog-worktree] preflight: {payload['repo_root']} (branch: {payload['current_branch']}, dirty: {dirty})",
        f"[blackdog-worktree] project root: {payload['project_root']}",
        f"[blackdog-worktree] cwd: {payload['cwd']}",
        f"[blackdog-worktree] current worktree: {payload['current_worktree']}",
        f"[blackdog-worktree] workspace role: {payload['workspace_role']}",
        f"[blackdog-worktree] primary worktree: {primary}",
        f"[blackdog-worktree] workspace mode: {payload['workspace_mode']}",
        f"[blackdog-worktree] target branch: {payload['target_branch']}",
        f"[blackdog-worktree] landing state: {payload['landing_state']}",
        f"[blackdog-worktree] primary clean for landing: {primary_clean}",
        f"[blackdog-worktree] implementation dirty: {implementation_dirty}",
        f"[blackdog-worktree] worktrees dir: {payload['worktrees_dir']} ({location})",
        f"[blackdog-worktree] current worktree CLI: {workspace_blackdog}",
        f"[blackdog-worktree] .VE rule: {payload['ve_expectation']}",
    ]
    if payload["primary_dirty_paths"]:
        lines.append(f"[blackdog-worktree] primary dirty paths: {', '.join(payload['primary_dirty_paths'])}")
    for row in payload["worktrees"]:
        label = "primary" if row["is_primary"] else row["branch"] or "(detached)"
        lines.append(f"[blackdog-worktree] known: {row['path']} [{label}]")
    return "\n".join(lines) + "\n"


def render_preview_text(
    preview: WorktreePreview,
    *,
    show_prompt: bool = False,
    expand_contract: bool = False,
) -> str:
    lines = [
        f"[blackdog-worktree] preview: {preview.task_id} {preview.task_title}",
        f"[blackdog-worktree] actor: {preview.actor} exec={preview.execution_model}",
        f"[blackdog-worktree] branch: {preview.branch}",
        f"[blackdog-worktree] base: {preview.base_ref} ({preview.base_commit})",
        f"[blackdog-worktree] target branch: {preview.target_branch}",
        f"[blackdog-worktree] integration branch: {preview.integration_branch}",
        f"[blackdog-worktree] worktree: {preview.worktree_path}",
        f"[blackdog-worktree] workspace identity: {preview.workspace_identity or 'unset'}",
        f"[blackdog-worktree] prompt hash: {preview.prompt_hash}",
        f"[blackdog-worktree] prompt source: {preview.prompt_source or 'unspecified'}",
        f"[blackdog-worktree] prompt mode: {preview.prompt_mode or 'unset'}",
        f"[blackdog-worktree] runtime mode: {preview.handlers.runtime_mode or 'unset'}",
        f"[blackdog-worktree] workspace CLI: {preview.handlers.blackdog_path or 'missing'}",
        f"[blackdog-worktree] start ready: {'yes' if preview.start_ready else 'no'}",
    ]
    if preview.handlers.script_policy:
        lines.append(f"[blackdog-worktree] script policy: {preview.handlers.script_policy}")
    if preview.handlers.source_mode:
        lines.append(f"[blackdog-worktree] source mode: {preview.handlers.source_mode}")
    if preview.handlers.source_root:
        lines.append(f"[blackdog-worktree] source root: {preview.handlers.source_root}")
    if preview.model:
        lines.append(f"[blackdog-worktree] model: {preview.model}")
    if preview.reasoning_effort:
        lines.append(f"[blackdog-worktree] reasoning effort: {preview.reasoning_effort}")
    if preview.task_paths:
        lines.append(f"[blackdog-worktree] task paths: {', '.join(preview.task_paths)}")
    if preview.task_docs:
        lines.append(f"[blackdog-worktree] task docs: {', '.join(preview.task_docs)}")
    if preview.task_checks:
        lines.append(f"[blackdog-worktree] task checks: {', '.join(preview.task_checks)}")
    if preview.validation_commands:
        lines.append(f"[blackdog-worktree] default validations: {', '.join(preview.validation_commands)}")
    if preview.contract_documents:
        lines.append("[blackdog-worktree] repo contract inputs:")
        for document in preview.contract_documents:
            lines.append(f"  - {document.kind}: {document.path}")
    if preview.handlers.actions:
        lines.append("[blackdog-worktree] handler plan:")
        for action in preview.handlers.actions:
            target = f" -> {action.target_path}" if action.target_path else ""
            lines.append(f"  - {action.handler_id}: {action.action} {action.status}{target} ({action.message})")
    if preview.conflicts:
        lines.append(f"[blackdog-worktree] conflicts: {'; '.join(preview.conflicts)}")
    if show_prompt and preview.prompt_text is not None:
        lines.append("[blackdog-worktree] prompt text:")
        lines.extend(f"  {line}" for line in preview.prompt_text.splitlines())
    if expand_contract:
        for document in preview.contract_documents:
            if document.text is None:
                continue
            lines.append(f"[blackdog-worktree] contract text: {document.path}")
            lines.extend(f"  {line}" for line in document.text.splitlines())
    return "\n".join(lines) + "\n"


def render_start_text(spec: WorktreeSpec, *, surface: str = "worktree") -> str:
    prefix = f"[blackdog-{surface}]"
    lines = [
        f"{prefix} created: {spec.worktree_path}",
        f"{prefix} branch: {spec.branch}",
        f"{prefix} base: {spec.base_ref} ({spec.base_commit})",
        f"{prefix} target branch: {spec.target_branch}",
        f"{prefix} task: {spec.task_id} {spec.task_title}",
        f"{prefix} attempt: {spec.attempt_id}",
        f"{prefix} prompt hash: {spec.prompt_hash}",
        f"{prefix} prompt source: {spec.prompt_source or 'unspecified'}",
        f"{prefix} prompt mode: {spec.prompt_mode or 'unset'}",
        f"{prefix} workspace CLI: {spec.workspace_blackdog_path or 'missing'}",
        f"{prefix} runtime mode: {spec.runtime_mode or 'unset'}",
        (
            f"{prefix} setup: {spec.setup_receipt.get('status', 'unknown')} "
            f"repository_guards={len(spec.setup_receipt.get('guard_receipts') or ())}"
        ),
    ]
    if spec.script_policy:
        lines.append(f"{prefix} script policy: {spec.script_policy}")
    if spec.source_mode:
        lines.append(f"{prefix} source mode: {spec.source_mode}")
    if spec.source_root:
        lines.append(f"{prefix} source root: {spec.source_root}")
    skill_provenance = _bounded_skill_provenance(spec.setup_receipt)
    if skill_provenance is not None:
        lines.append(
            f"{prefix} repo skill: {skill_provenance['path']} "
            f"sha256={skill_provenance['sha256']} source={skill_provenance['source']}"
        )
    if spec.handlers.actions:
        lines.append(f"{prefix} handler results:")
        for action in spec.handlers.actions:
            target = f" -> {action.target_path}" if action.target_path else ""
            timing = "" if action.elapsed_ms is None else f" [{action.elapsed_ms}ms]"
            lines.append(
                f"  - {action.handler_id}: {action.action} {action.status}{target}{timing} ({action.message})"
            )
    return "\n".join(lines) + "\n"


def _append_operation_contract(lines: list[str], *, prefix: str, payload: Any) -> None:
    data = payload.to_dict() if isinstance(payload, OperationResult) else payload
    if data.get("operation"):
        lines.append(f"{prefix} operation: {data['operation']}")
    if data.get("operation_status"):
        lines.append(f"{prefix} operation status: {data['operation_status']}")
        if data.get("disposition"):
            lines.append(f"{prefix} disposition: {data['disposition']}")
        if all(
            key in data
            for key in ("mutation_started", "mutation_completed", "mutation_phase")
        ):
            lines.append(
                f"{prefix} mutation: started={'yes' if data['mutation_started'] else 'no'} "
                f"completed={'yes' if data['mutation_completed'] else 'no'} phase={data['mutation_phase']}"
            )
    next_action = data.get("next_action")
    if not isinstance(next_action, dict):
        return
    lines.append(f"{prefix} next action: {next_action['action_id']}")
    lines.append(f"{prefix} next action kind: {next_action['kind']}")
    lines.append(f"{prefix} next action disposition: {next_action['disposition']}")
    lines.append(f"{prefix} next action display: {next_action['display']}")
    if next_action.get("command"):
        lines.append(f"{prefix} next command: {next_action['command']}")
    for required_input in next_action.get("required_inputs") or ():
        lines.append(f"{prefix} required input: {required_input}")
    for choice in next_action.get("choices") or ():
        lines.append(
            f"{prefix} choice: {choice['action_id']} disposition={choice['disposition']} "
            f"display={choice['display']} command={choice['command']}"
        )
    for alternative in next_action.get("alternatives") or ():
        lines.append(
            f"{prefix} alternative: {alternative['action_id']} disposition={alternative['disposition']} "
            f"display={alternative['display']} command={alternative['command']}"
        )


def render_land_text(payload: Any, *, surface: str = "worktree") -> str:
    prefix = f"[blackdog-{surface}]"
    workspace_label = "task workspace" if surface == "task" else "worktree"
    target_label = "checkout" if surface == "task" else "worktree"
    if payload.get("status") and payload["status"] != "success":
        action = "closed" if payload.get("land_failure_disposition") == "closed" else "blocked"
        lines: list[str] = []
        _append_operation_contract(lines, prefix=prefix, payload=payload)
        lines.extend(
            [
                f"{prefix} land {action}: {payload['branch']} -> {payload['target_branch']}",
                f"{prefix} attempt: {payload['attempt_id']}",
                f"{prefix} attempt remains active: {'yes' if payload.get('attempt_active') else 'no'}",
            ]
        )
        if payload.get("summary"):
            lines.append(f"{prefix} summary: {payload['summary']}")
        if payload.get("failure_class"):
            lines.append(f"{prefix} failure class: {payload['failure_class']}")
        if payload.get("recovery_action"):
            lines.append(f"{prefix} recovery action: {payload['recovery_action']}")
        if payload.get("worktree_path"):
            lines.append(f"{prefix} {workspace_label}: {payload['worktree_path']}")
        if payload.get("commit"):
            lines.append(f"{prefix} branch commit: {payload['commit']}")
        if payload.get("changed_paths"):
            lines.append(f"{prefix} changed paths: {', '.join(payload['changed_paths'])}")
        if payload.get("cleanup_performed") and payload.get("cleanup"):
            lines.append(f"{prefix} removed {workspace_label}: {payload['cleanup']['worktree_path']}")
        elif payload.get("cleanup_reason"):
            lines.append(f"{prefix} cleanup: {payload['cleanup_reason']}")
        if payload.get("error"):
            lines.append(f"{prefix} error: {payload['error']}")
        return "\n".join(lines) + "\n"
    lines = []
    _append_operation_contract(lines, prefix=prefix, payload=payload)
    lines.extend(
        [
            f"{prefix} landed: {payload['branch']} -> {payload['target_branch']}",
            f"{prefix} target {target_label}: {payload['target_worktree']}",
            f"{prefix} landed commit: {payload['landed_commit']}",
        ]
    )
    if payload["changed_paths"]:
        lines.append(f"{prefix} changed paths: {', '.join(payload['changed_paths'])}")
    if payload.get("cleaned_worktree"):
        lines.append(f"{prefix} removed {workspace_label}: {payload['cleaned_worktree']}")
    if payload.get("deleted_branch"):
        lines.append(f"{prefix} deleted branch: {payload['branch']}")
    return "\n".join(lines) + "\n"


def render_task_state_text(payload: Any) -> str:
    lines: list[str] = []
    _append_operation_contract(lines, prefix="[blackdog-task]", payload=payload)
    lines.extend(
        [
            f"[blackdog-task] state: {payload['workset_id']}/{payload['task_id']} {payload['status']}",
            f"[blackdog-task] actor: {payload['actor']}",
        ]
    )
    if payload.get("updated_at"):
        lines.append(f"[blackdog-task] updated at: {payload['updated_at']}")
    if payload.get("summary"):
        lines.append(f"[blackdog-task] summary: {payload['summary']}")
    if payload.get("failure_class"):
        lines.append(f"[blackdog-task] failure class: {payload['failure_class']}")
    if payload.get("recovery_action"):
        lines.append(f"[blackdog-task] recovery action: {payload['recovery_action']}")
    return "\n".join(lines) + "\n"


def render_task_begin_text(spec: Any, *, show_prompt: bool = False) -> str:
    payload = spec.to_dict() if isinstance(spec, OperationResult) else spec.to_dict()
    worktree = payload.get("worktree")
    if payload.get("operation_status") == "blocked" and not isinstance(worktree, Mapping):
        lines: list[str] = []
        _append_operation_contract(lines, prefix="[blackdog-task]", payload=spec)
        lines.append(
            f"[blackdog-task] begin blocked: task={payload.get('task_id') or '-'} "
            f"actor={payload.get('actor') or '-'}"
        )
        if payload.get("error"):
            lines.append(f"[blackdog-task] error: {payload['error']}")
        lines.append("[blackdog-task] workspace: not started")
        return "\n".join(lines) + "\n"
    lines = []
    _append_operation_contract(lines, prefix="[blackdog-task]", payload=spec)
    lines.extend(
        [
            f"[blackdog-task] begin: {payload['task_id']} actor={payload['actor']}",
            f"[blackdog-task] prompt mode: {payload['prompt_mode']}",
            f"[blackdog-task] user prompt hash: {payload['user_prompt_hash']}",
            f"[blackdog-task] execution prompt hash: {payload['execution_prompt_hash']}",
        ]
    )
    if show_prompt and payload.get("execution_prompt_text") is not None:
        lines.append("[blackdog-task] execution prompt:")
        lines.extend(f"  {line}" for line in payload["execution_prompt_text"].splitlines())
    if not isinstance(worktree, Mapping):
        lines.append("[blackdog-task] workspace: not started")
        return "\n".join(lines) + "\n"
    lines.extend(
        [
            f"[blackdog-task] created: {worktree['worktree_path']}",
            f"[blackdog-task] branch: {worktree['branch']}",
            f"[blackdog-task] base: {worktree['base_ref']} ({worktree['base_commit']})",
            f"[blackdog-task] target branch: {worktree['target_branch']}",
            f"[blackdog-task] task: {worktree['task_id']} {worktree['task_title']}",
            f"[blackdog-task] attempt: {worktree['attempt_id']}",
            f"[blackdog-task] prompt hash: {worktree['prompt_hash']}",
            f"[blackdog-task] prompt source: {worktree.get('prompt_source') or 'unspecified'}",
            f"[blackdog-task] prompt mode: {worktree.get('prompt_mode') or 'unset'}",
            f"[blackdog-task] workspace CLI: {worktree.get('workspace_blackdog_path') or 'missing'}",
            f"[blackdog-task] runtime mode: {worktree.get('runtime_mode') or 'unset'}",
            (
                f"[blackdog-task] setup: {worktree.get('setup_receipt', {}).get('status', 'unknown')} "
                "repository_guards="
                f"{len(worktree.get('setup_receipt', {}).get('guard_receipts') or ())}"
            ),
        ]
    )
    if worktree.get("script_policy"):
        lines.append(f"[blackdog-task] script policy: {worktree['script_policy']}")
    if worktree.get("source_mode"):
        lines.append(f"[blackdog-task] source mode: {worktree['source_mode']}")
    if worktree.get("source_root"):
        lines.append(f"[blackdog-task] source root: {worktree['source_root']}")
    skill_provenance = payload.get("skill_provenance")
    if isinstance(skill_provenance, dict):
        lines.append(
            f"[blackdog-task] repo skill: {skill_provenance['path']} "
            f"sha256={skill_provenance['sha256']} source={skill_provenance['source']}"
        )
    handler_actions = worktree.get("handlers", {}).get("actions") or ()
    if handler_actions:
        lines.append("[blackdog-task] handler results:")
        for action in handler_actions:
            target = f" -> {action['target_path']}" if action.get("target_path") else ""
            timing = "" if action.get("elapsed_ms") is None else f" [{action['elapsed_ms']}ms]"
            lines.append(
                f"  - {action['id']}: {action['action']} {action['status']}{target}{timing} "
                f"({action['message']})"
            )
    return "\n".join(lines) + "\n"


def _append_legacy_reconciliation_detection_text(
    lines: list[str],
    *,
    prefix: str,
    payload: Mapping[str, Any],
) -> None:
    detection = payload.get("legacy_reconciliation_detection")
    if not isinstance(detection, Mapping):
        return
    lines.append(
        f"{prefix} Legacy reconciliation detection: {detection.get('state')} "
        f"({detection.get('reason_code')})"
    )
    if detection.get("candidate_commit"):
        lines.append(
            f"{prefix} legacy reconciliation candidate: {detection['candidate_commit']}"
        )
    if detection.get("reason_detail"):
        lines.append(
            f"{prefix} legacy reconciliation detail: {detection['reason_detail']}"
        )


def render_show_text(payload: Any, *, surface: str = "worktree") -> str:
    prefix = f"[blackdog-{surface}]"
    workspace_label = "task workspace" if surface == "task" else "worktree"
    lines: list[str] = []
    _append_operation_contract(lines, prefix=prefix, payload=payload)
    lines.extend(
        [
            f"{prefix} show: {payload['task_id']} {payload['task_title']}",
            f"{prefix} active attempt: {'yes' if payload['active_attempt'] else 'no'}",
        ]
    )
    if payload["attempt_id"]:
        lines.append(f"{prefix} attempt: {payload['attempt_id']}")
    if payload["latest_attempt_status"]:
        lines.append(f"{prefix} latest attempt: {payload['latest_attempt_status']} {payload['latest_attempt_id']}")
    if payload["latest_attempt_summary"]:
        lines.append(f"{prefix} latest summary: {payload['latest_attempt_summary']}")
    if payload["branch"]:
        lines.append(f"{prefix} branch: {payload['branch']}")
    if payload["target_branch"]:
        lines.append(f"{prefix} target branch: {payload['target_branch']}")
    if payload["worktree_path"]:
        lines.append(f"{prefix} {workspace_label}: {payload['worktree_path']}")
    if payload.get("branch_exists") is False:
        lines.append(f"{prefix} branch exists: no")
    if payload.get("target_branch_exists") is False:
        lines.append(f"{prefix} target branch exists: no")
    if payload.get("branch_ahead_error"):
        lines.append(f"{prefix} branch ahead check: {payload['branch_ahead_error']}")
    lines.append(f"{prefix} {workspace_label} exists: {'yes' if payload['worktree_exists'] else 'no'}")
    lines.append(f"{prefix} {workspace_label} dirty: {'yes' if payload['worktree_dirty'] else 'no'}")
    lines.append(f"{prefix} branch ahead of target: {'yes' if payload['branch_ahead_of_target'] else 'no'}")
    lines.append(f"{prefix} primary dirty: {'yes' if payload['primary_dirty'] else 'no'}")
    if payload["worktree_dirty_paths"]:
        lines.append(f"{prefix} {workspace_label} dirty paths: {', '.join(payload['worktree_dirty_paths'])}")
    if payload["changed_paths"]:
        lines.append(f"{prefix} attempt paths: {', '.join(payload['changed_paths'])}")
    if payload["user_prompt_hash"]:
        lines.append(f"{prefix} user prompt hash: {payload['user_prompt_hash']}")
    if payload["user_prompt_source"]:
        lines.append(f"{prefix} user prompt source: {payload['user_prompt_source']}")
    if payload["user_prompt_mode"]:
        lines.append(f"{prefix} user prompt mode: {payload['user_prompt_mode']}")
    if payload["execution_prompt_hash"]:
        lines.append(f"{prefix} execution prompt hash: {payload['execution_prompt_hash']}")
    if payload["execution_prompt_source"]:
        lines.append(f"{prefix} execution prompt source: {payload['execution_prompt_source']}")
    if payload["execution_prompt_mode"]:
        lines.append(f"{prefix} execution prompt mode: {payload['execution_prompt_mode']}")
    skill_provenance = payload.get("skill_provenance")
    if isinstance(skill_provenance, dict):
        lines.append(
            f"{prefix} repo skill: {skill_provenance['path']} "
            f"sha256={skill_provenance['sha256']} source={skill_provenance['source']}"
        )
    if payload.get("failure_class"):
        lines.append(f"{prefix} failure class: {payload['failure_class']}")
    if payload.get("recovery_action"):
        lines.append(f"{prefix} recovery action: {payload['recovery_action']}")
    _append_legacy_reconciliation_detection_text(lines, prefix=prefix, payload=payload)
    return "\n".join(lines) + "\n"


def render_recover_text(payload: Any) -> str:
    prefix = "[blackdog-task]"
    workspace_label = "task workspace"
    lines: list[str] = []
    _append_operation_contract(lines, prefix=prefix, payload=payload)
    lines.extend(
        [
            f"{prefix} recover: {payload['task_id']} {payload['task_title']}",
            f"{prefix} recovery state: {payload['recovery_state']}",
            f"{prefix} task runtime: {payload['task_runtime_status']}",
            f"{prefix} active attempt: {'yes' if payload['active_attempt'] else 'no'}",
            f"{prefix} stale claim: {'yes' if payload['stale_claim'] else 'no'}",
        ]
    )
    if payload.get("released_stale_claim"):
        lines.append(f"{prefix} released stale claim: yes")
        lines.append(f"{prefix} release status: {payload['release_status']}")
        lines.append(f"{prefix} release summary: {payload['release_summary']}")
        if payload.get("repaired_runtime_status"):
            lines.append(f"{prefix} repaired task runtime: {payload['repaired_runtime_status']}")
    task_claim = payload.get("task_claim")
    if task_claim is not None:
        lines.append(
            f"{prefix} task claim: {task_claim['actor']}/{task_claim['execution_model']} claimed_at={task_claim['claimed_at']}"
        )
        if task_claim.get("attempt_id"):
            lines.append(f"{prefix} claimed attempt: {task_claim['attempt_id']}")
    workset_claim = payload.get("workset_claim")
    if workset_claim is not None:
        lines.append(
            f"{prefix} workset claim: {workset_claim['actor']}/{workset_claim['execution_model']} claimed_at={workset_claim['claimed_at']}"
        )
    if payload["latest_attempt_status"]:
        lines.append(f"{prefix} latest attempt: {payload['latest_attempt_status']} {payload['latest_attempt_id']}")
    if payload["latest_attempt_summary"]:
        lines.append(f"{prefix} latest summary: {payload['latest_attempt_summary']}")
    if payload["branch"]:
        lines.append(f"{prefix} branch: {payload['branch']}")
    if payload["target_branch"]:
        lines.append(f"{prefix} target branch: {payload['target_branch']}")
    if payload["worktree_path"]:
        lines.append(f"{prefix} {workspace_label}: {payload['worktree_path']}")
    if payload.get("branch_exists") is False:
        lines.append(f"{prefix} branch exists: no")
    if payload.get("target_branch_exists") is False:
        lines.append(f"{prefix} target branch exists: no")
    if payload.get("branch_ahead_error"):
        lines.append(f"{prefix} branch ahead check: {payload['branch_ahead_error']}")
    lines.append(f"{prefix} {workspace_label} exists: {'yes' if payload['worktree_exists'] else 'no'}")
    lines.append(f"{prefix} {workspace_label} dirty: {'yes' if payload['worktree_dirty'] else 'no'}")
    lines.append(f"{prefix} branch ahead of target: {'yes' if payload['branch_ahead_of_target'] else 'no'}")
    lines.append(f"{prefix} primary dirty: {'yes' if payload['primary_dirty'] else 'no'}")
    if payload["worktree_dirty_paths"]:
        lines.append(f"{prefix} {workspace_label} dirty paths: {', '.join(payload['worktree_dirty_paths'])}")
    if payload["changed_paths"]:
        lines.append(f"{prefix} attempt paths: {', '.join(payload['changed_paths'])}")
    if payload.get("task_runtime_note"):
        lines.append(f"{prefix} task note: {payload['task_runtime_note']}")
    if payload.get("failure_class"):
        lines.append(f"{prefix} failure class: {payload['failure_class']}")
    if payload.get("recovery_action"):
        lines.append(f"{prefix} recovery action: {payload['recovery_action']}")
    _append_legacy_reconciliation_detection_text(lines, prefix=prefix, payload=payload)
    return "\n".join(lines) + "\n"


def render_close_text(payload: Any, *, surface: str = "worktree") -> str:
    prefix = f"[blackdog-{surface}]"
    workspace_label = "task workspace" if surface == "task" else "worktree"
    lines: list[str] = []
    _append_operation_contract(lines, prefix=prefix, payload=payload)
    lines.extend(
        [
            f"{prefix} closed: {payload['task_id']} attempt={payload['attempt_id']} status={payload['status']}",
            f"{prefix} summary: {payload['summary']}",
        ]
    )
    if payload.get("branch"):
        lines.append(f"{prefix} branch: {payload['branch']}")
    if payload.get("target_branch"):
        lines.append(f"{prefix} target branch: {payload['target_branch']}")
    if payload.get("worktree_path"):
        lines.append(f"{prefix} {workspace_label}: {payload['worktree_path']}")
    if payload.get("changed_paths"):
        lines.append(f"{prefix} changed paths: {', '.join(payload['changed_paths'])}")
    if payload.get("failure_class"):
        lines.append(f"{prefix} failure class: {payload['failure_class']}")
    if payload.get("recovery_action"):
        lines.append(f"{prefix} recovery action: {payload['recovery_action']}")
    if payload.get("cleanup_performed") and payload.get("cleanup"):
        cleanup_path = payload["cleanup"].get("worktree_path") or payload.get(
            "worktree_path"
        )
        lines.append(
            f"{prefix} removed: {cleanup_path}"
            if cleanup_path
            else f"{prefix} cleanup: performed"
        )
    elif payload.get("cleanup_reason"):
        lines.append(f"{prefix} cleanup: {payload['cleanup_reason']}")
    if payload.get("error"):
        lines.append(f"{prefix} error: {payload['error']}")
    return "\n".join(lines) + "\n"


def render_cleanup_text(payload: Any, *, surface: str = "worktree") -> str:
    prefix = f"[blackdog-{surface}]"
    lines: list[str] = []
    _append_operation_contract(lines, prefix=prefix, payload=payload)
    if payload.get("worktree_removed"):
        lines.append(f"{prefix} removed: {payload['worktree_path']}")
    elif payload.get("worktree_existed"):
        lines.append(f"{prefix} retained: {payload['worktree_path']}")
    else:
        lines.append(f"{prefix} already clean: {payload['worktree_path']}")
    if payload["branch"]:
        action = "deleted" if payload["deleted_branch"] else "kept"
        lines.append(f"{prefix} branch: {payload['branch']} ({action})")
    if payload.get("error"):
        lines.append(f"{prefix} error: {payload['error']}")
    return "\n".join(lines) + "\n"


def render_worktree_table_text(payload: dict[str, Any]) -> str:
    rows = payload["rows"]
    columns = payload["columns"]
    if not rows:
        return "\t".join(columns) + "\n"
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join("" if row.get(column) is None else str(row.get(column)) for column in columns))
    return "\n".join(lines) + "\n"


def render_worktree_cleanup_all_text(payload: dict[str, Any]) -> str:
    lines = [
        (
            f"[blackdog-worktree] cleanup all: cleaned={len(payload['cleaned'])} "
            f"skipped={len(payload['skipped'])} errors={len(payload['errors'])}"
        ),
        f"[blackdog-worktree] remaining rows: {payload['remaining']['counts']['rows']}",
    ]
    for row in payload["cleaned"]:
        branch = f" branch={row['branch']}" if row.get("branch") else ""
        lines.append(f"[blackdog-worktree] removed: {row['worktree_path']}{branch}")
    for row in payload["errors"]:
        lines.append(f"[blackdog-worktree] error: {row['workset_id']}/{row['task_id']} {row['error']}")
    return "\n".join(lines) + "\n"


__all__ = [
    "DirtyPrimaryWorktreeError",
    "TaskBeginSpec",
    "TaskBeginPreflightError",
    "WORKTREE_TABLE_COLUMNS",
    "WORKSPACE_MODE_GIT_WORKTREE",
    "WORKTREE_ROLE_LINKED",
    "WORKTREE_ROLE_PRIMARY",
    "WORKTREE_ROLE_TASK",
    "WTAM_WORKTREE_VE_NOTE",
    "WorktreeError",
    "WorktreeSpec",
    "branch_ahead_of_target",
    "branch_changed_paths",
    "begin_task_worktree",
    "cancel_task",
    "build_worktree_table",
    "cleanup_task",
    "cleanup_task_worktree",
    "cleanup_worktree_table",
    "close_task",
    "close_task_worktree",
    "command_workspace_root",
    "default_task_branch",
    "default_task_worktree_path",
    "dirty_paths",
    "dirty_primary_worktree_error",
    "find_primary_worktree",
    "find_worktree_for_branch",
    "inspect_task_worktree",
    "land_task",
    "land_task_worktree",
    "primary_worktree_dirty_paths",
    "primary_worktree_is_dirty",
    "preview_task_worktree",
    "render_cleanup_text",
    "render_close_text",
    "render_land_text",
    "render_preflight_text",
    "render_preview_text",
    "render_recover_text",
    "render_show_text",
    "render_start_text",
    "render_task_begin_text",
    "render_task_state_text",
    "render_worktree_cleanup_all_text",
    "render_worktree_table_text",
    "recover_task",
    "reopen_task",
    "show_task",
    "start_task_worktree",
    "task_begin_preflight_result",
    "worktree_contract",
    "worktree_preflight",
]
