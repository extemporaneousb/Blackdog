from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
import shlex
from typing import Any

from blackdog_core.state import (
    ATTEMPT_STATUS_ABANDONED,
    ATTEMPT_STATUS_BLOCKED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_IN_PROGRESS,
    ATTEMPT_STATUS_SUCCESS,
    ATTEMPT_STATUSES,
    FAILURE_CLASSES,
    FAILURE_CLASS_DIRTY_PRIMARY,
    FAILURE_CLASS_MISSING_WORKTREE,
    FAILURE_CLASS_NO_CHANGES,
    FAILURE_CLASS_STALE_BRANCH,
    FAILURE_CLASS_UNKNOWN,
    TASK_STATUS_CANCELED,
    TASK_STATUSES,
    VALIDATION_STATUSES,
)


NEXT_ACTION_KINDS = frozenset({"command", "choice", "complete", "blocked"})
ACTION_SAFETY_CLASSES = frozenset(
    {
        "operator_confirmation",
        "proof_guarded_mutation",
        "read_only",
        "requires_validation",
        "validated_mutation",
    }
)
ACTION_MUTATION_CLASSES = frozenset(
    {"none", "event", "runtime", "git_and_runtime", "git_and_filesystem"}
)
NORMAL_TASK_OPERATIONS = frozenset(
    {
        "task.begin",
        "task.show",
        "task.recover",
        "task.land",
        "task.reconcile-landing",
        "task.close",
        "task.cancel",
        "task.reopen",
        "task.cleanup",
    }
)
OPERATION_STATUSES = frozenset({"observed", "succeeded", "blocked", "closed", "partial"})
PRE_ATTEMPT_FAILURE_CODES = frozenset({"managed_skill_missing", "setup_guard"})
OPERATION_FAILURE_CODES = frozenset({*FAILURE_CLASSES, *PRE_ATTEMPT_FAILURE_CODES})
MUTATION_PHASES = frozenset(
    {
        "none",
        "preflight",
        "git_prepared",
        "workspace_started",
        "workspace_adopted",
        "proof_verified",
        "runtime_finalized",
        "runtime_and_cleanup_finalized",
        "runtime_finalized_cleanup_pending",
        "git_and_filesystem_finalized",
        "git_and_filesystem_and_event_finalized",
        "worktree_removed_branch_cleanup_pending",
        "cleanup_event_finalization_pending",
        "close_request_recorded",
        "close_core_request_recorded",
        "close_core_decision_recorded",
        "close_runtime_finalized",
        "close_task_release_recorded",
        "close_workset_release_recorded",
        "close_task_finish_recorded",
        "close_cleanup_pending",
        "close_cleanup_finalized",
        "close_event_pending",
        "close_complete",
        "runtime_and_event_finalized",
        "event_finalized",
        "event_finalization_partial",
        "landing_intent_recorded",
        "landing_source_prepared",
        "landing_canonical_commit_created",
        "landing_target_updated",
        "landing_temporary_cleanup_complete",
        "landing_runtime_finalized",
        "landing_land_event_recorded",
        "landing_task_cleanup_complete",
        "landing_complete",
        "landing_abort_intent_recorded",
        "landing_abort_temporary_cleanup_complete",
        "landing_abort_runtime_finalized",
        "landing_abort_close_event_recorded",
        "landing_abort_complete",
        "landing_abort_superseded",
    }
)
REFERENCE_INSPECTION_STATES = frozenset({"exists", "missing", "metadata_missing", "error"})


class WorktreeError(RuntimeError):
    """Base product-layer Git/worktree error."""


class LifecycleGitError(WorktreeError):
    failure_code = FAILURE_CLASS_UNKNOWN
    recovery_action = "inspect"
    operator_issue = False
    terminal_attempt_status: str | None = None


class DirtyPrimaryWorktreeError(LifecycleGitError):
    failure_code = FAILURE_CLASS_DIRTY_PRIMARY
    recovery_action = "clean_primary_worktree"
    operator_issue = True

    def __init__(
        self,
        *,
        primary_worktree: Path,
        branch: str,
        target_branch: str,
        dirty_paths: list[str],
    ) -> None:
        self.primary_worktree = str(primary_worktree)
        self.branch = branch
        self.target_branch = target_branch
        self.dirty_paths = tuple(dirty_paths)
        dirty_text = ", ".join(self.dirty_paths) or "none detected"
        super().__init__(
            "dirty primary worktree contract violation: "
            f"{self.primary_worktree} has uncommitted changes blocking landing {branch} into {target_branch}; "
            f"dirty paths: {dirty_text}; clean up or land the primary worktree changes and retry "
            "without using git stash"
        )


class DirtyTargetWorktreeError(LifecycleGitError):
    operator_issue = True

    def __init__(self, path: Path) -> None:
        self.path = str(path)
        super().__init__(f"target worktree has uncommitted changes: {path}")


class StaleTaskBranchError(LifecycleGitError):
    failure_code = FAILURE_CLASS_STALE_BRANCH
    recovery_action = "rebase_task_branch"
    operator_issue = True

    def __init__(self, *, branch: str, target_branch: str, branch_worktree: Path | None) -> None:
        self.branch = branch
        self.target_branch = target_branch
        self.branch_worktree = str(branch_worktree) if branch_worktree is not None else None
        rebase_location = f" -C {branch_worktree}" if branch_worktree is not None else ""
        super().__init__(
            f"cannot land: {branch} is not based on the current {target_branch}; "
            f"rebase it first with `git{rebase_location} rebase --autostash {target_branch}`"
        )


class MissingTaskWorktreeError(LifecycleGitError):
    failure_code = FAILURE_CLASS_MISSING_WORKTREE
    recovery_action = "restore_or_cleanup_worktree"
    operator_issue = True

    def __init__(self, path: Path | str | None) -> None:
        self.path = str(path) if path is not None else None
        suffix = f": {self.path}" if self.path else ""
        super().__init__(f"active task worktree is missing or invalid{suffix}")


class CleanupOwnershipError(LifecycleGitError):
    """Cleanup inputs do not identify the durable task workspace."""

    recovery_action = "inspect_task_cleanup_identity"
    operator_issue = True

    def __init__(
        self,
        *,
        worktree_path: Path,
        branch: str,
        expected_worktree_path: Path,
        expected_branch: str,
        detail: str,
    ) -> None:
        self.worktree_path = str(worktree_path)
        self.worktree_existed = worktree_path.exists()
        self.branch = branch
        self.expected_worktree_path = str(expected_worktree_path)
        self.expected_branch = expected_branch
        self.detail = detail
        super().__init__(f"refusing cleanup: task workspace ownership is unproven: {detail}")

    def refusal_payload(self) -> dict[str, Any]:
        return {
            "worktree_path": self.worktree_path,
            "worktree_existed": self.worktree_existed,
            "worktree_removed": False,
            "branch": self.branch,
            "deleted_branch": False,
            "expected_worktree_path": self.expected_worktree_path,
            "expected_branch": self.expected_branch,
            "branch_cleanup_reason": self.detail,
            "branch_cleanup_proof": "workspace_ownership_unproven",
            "force_deleted_branch": False,
            "cleanup_complete": False,
            "cleanup_refused": True,
            "error": str(self),
            "failure_class": self.failure_code,
            "recovery_action": self.recovery_action,
            "prompt_issue": False,
            "operator_issue": self.operator_issue,
        }


class NoChangesToLandError(LifecycleGitError):
    failure_code = FAILURE_CLASS_NO_CHANGES
    recovery_action = "close_no_change_attempt"
    terminal_attempt_status = ATTEMPT_STATUS_BLOCKED

    def __init__(self, *, branch: str, target_branch: str) -> None:
        self.branch = branch
        self.target_branch = target_branch
        super().__init__(f"cannot land: {branch} has no changes relative to {target_branch}")


class CleanupPostMutationError(LifecycleGitError):
    """Cleanup removed a workspace but could not finish branch deletion."""

    recovery_action = "retry_task_cleanup"
    operator_issue = True

    def __init__(
        self,
        *,
        worktree_path: Path,
        branch: str,
        branch_cleanup_reason: str,
        branch_cleanup_proof: str,
        force_delete: bool,
        detail: str,
    ) -> None:
        self.worktree_path = str(worktree_path)
        self.branch = branch
        self.branch_cleanup_reason = branch_cleanup_reason
        self.branch_cleanup_proof = branch_cleanup_proof
        self.force_delete = force_delete
        self.detail = detail
        super().__init__(
            f"task workspace was removed but branch cleanup for {branch!r} failed: {detail}"
        )

    def partial_payload(self) -> dict[str, Any]:
        return {
            "worktree_path": self.worktree_path,
            "worktree_existed": True,
            "worktree_removed": True,
            "branch": self.branch,
            "deleted_branch": False,
            "branch_cleanup_reason": self.branch_cleanup_reason,
            "branch_cleanup_proof": self.branch_cleanup_proof,
            "force_deleted_branch": False,
            "error": str(self),
            "failure_class": self.failure_code,
            "recovery_action": self.recovery_action,
            "prompt_issue": False,
            "operator_issue": self.operator_issue,
        }


class CleanupEventFinalizationError(WorktreeError):
    """Filesystem cleanup completed but its deterministic evidence write was unconfirmed."""

    def __init__(self, *, cleanup_payload: Mapping[str, Any], detail: str) -> None:
        self.cleanup_payload = dict(cleanup_payload)
        self.detail = detail
        super().__init__(f"cleanup completed but event finalization was not confirmed: {detail}")

    def partial_payload(self) -> dict[str, Any]:
        return {
            **self.cleanup_payload,
            "event_finalized": False,
            "error": str(self),
            "failure_class": FAILURE_CLASS_UNKNOWN,
            "recovery_action": "retry_cleanup_event_finalization",
            "prompt_issue": False,
            "operator_issue": True,
        }


@dataclass(frozen=True, slots=True)
class GitReferenceInspection:
    """Typed evidence that distinguishes missing refs from Git inspection errors."""

    role: str
    ref: str | None
    state: str
    command: tuple[str, ...] = ()
    return_code: int | None = None
    resolved_commit: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.state not in REFERENCE_INSPECTION_STATES:
            raise ValueError(f"invalid Git reference inspection state: {self.state}")

    @property
    def exists(self) -> bool | None:
        if self.state == "exists":
            return True
        if self.state == "missing":
            return False
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "ref": self.ref,
            "state": self.state,
            "command": list(self.command),
            "return_code": self.return_code,
            "resolved_commit": self.resolved_commit,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class FailureMapping:
    failure_code: str
    recovery_action: str
    prompt_issue: bool
    operator_issue: bool
    terminal_attempt_status: str | None = None

    def to_legacy_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_code,
            "recovery_action": self.recovery_action,
            "prompt_issue": self.prompt_issue,
            "operator_issue": self.operator_issue,
        }


def classify_lifecycle_exception(exc: BaseException) -> FailureMapping:
    if isinstance(exc, LifecycleGitError):
        return FailureMapping(
            failure_code=exc.failure_code,
            recovery_action=exc.recovery_action,
            prompt_issue=False,
            operator_issue=exc.operator_issue,
            terminal_attempt_status=exc.terminal_attempt_status,
        )
    return FailureMapping(
        failure_code=FAILURE_CLASS_UNKNOWN,
        recovery_action="inspect",
        prompt_issue=False,
        operator_issue=False,
    )


def _render_argv(argv: tuple[str, ...]) -> str:
    return shlex.join(argv)


def _argv_has_validation_evidence(argv: tuple[str, ...]) -> bool:
    for index, argument in enumerate(argv):
        if argument == "--validation":
            value = argv[index + 1] if index + 1 < len(argv) else ""
        elif argument.startswith("--validation="):
            value = argument.removeprefix("--validation=")
        else:
            continue
        if "=" not in value:
            continue
        name, status = value.split("=", 1)
        if name.strip() and status.strip() in VALIDATION_STATUSES:
            return True
    return False


@dataclass(frozen=True, slots=True)
class LifecycleAction:
    action_id: str
    disposition: str
    reason_code: str
    reason_detail: str
    argv: tuple[str, ...]
    safety_class: str
    mutation_class: str
    display: str

    def __post_init__(self) -> None:
        if not self.action_id.strip():
            raise ValueError("lifecycle action_id is required")
        if not self.argv or any(not isinstance(argument, str) or not argument for argument in self.argv):
            raise ValueError("lifecycle action requires complete nonempty argv")
        if self.safety_class not in ACTION_SAFETY_CLASSES:
            raise ValueError(f"invalid lifecycle action safety class: {self.safety_class}")
        if self.mutation_class not in ACTION_MUTATION_CLASSES:
            raise ValueError(f"invalid lifecycle action mutation class: {self.mutation_class}")
        if self.safety_class == "requires_validation" and not _argv_has_validation_evidence(
            self.argv
        ):
            raise ValueError(
                "requires_validation lifecycle actions must carry validation evidence in argv"
            )
        if not self.reason_code.strip() or not self.reason_detail.strip() or not self.display.strip():
            raise ValueError("lifecycle action reason and display fields are required")

    @property
    def command(self) -> str:
        return _render_argv(self.argv)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "disposition": self.disposition,
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
            "argv": list(self.argv),
            "command": self.command,
            "safety_class": self.safety_class,
            "mutation_class": self.mutation_class,
            "display": self.display,
        }


@dataclass(frozen=True, slots=True)
class NextAction:
    action_id: str
    kind: str
    disposition: str
    reason_code: str
    reason_detail: str
    display: str
    action: LifecycleAction | None = None
    choices: tuple[LifecycleAction, ...] = ()
    alternatives: tuple[LifecycleAction, ...] = ()
    required_inputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in NEXT_ACTION_KINDS:
            raise ValueError(f"invalid next action kind: {self.kind}")
        if self.kind == "command" and (self.action is None or self.choices):
            raise ValueError("command next action requires one primary command and no choices")
        if self.kind == "choice" and (self.action is not None or not self.choices or self.alternatives):
            raise ValueError("choice next action requires bounded choices and no primary command")
        if self.kind in {"complete", "blocked"} and (
            self.action is not None or self.choices or self.alternatives
        ):
            raise ValueError(f"{self.kind} next action cannot carry executable argv")
        if self.required_inputs and self.kind != "blocked":
            raise ValueError("only blocked next actions may declare required inputs")

    @classmethod
    def command(
        cls,
        action: LifecycleAction,
        *,
        alternatives: tuple[LifecycleAction, ...] = (),
    ) -> "NextAction":
        return cls(
            action_id=action.action_id,
            kind="command",
            disposition=action.disposition,
            reason_code=action.reason_code,
            reason_detail=action.reason_detail,
            display=action.display,
            action=action,
            alternatives=alternatives,
        )

    @classmethod
    def choice(
        cls,
        *,
        action_id: str,
        disposition: str,
        reason_code: str,
        reason_detail: str,
        display: str,
        choices: tuple[LifecycleAction, ...],
    ) -> "NextAction":
        return cls(
            action_id=action_id,
            kind="choice",
            disposition=disposition,
            reason_code=reason_code,
            reason_detail=reason_detail,
            display=display,
            choices=choices,
        )

    @classmethod
    def terminal(
        cls,
        *,
        action_id: str,
        kind: str,
        disposition: str,
        reason_code: str,
        reason_detail: str,
        display: str,
        required_inputs: tuple[str, ...] = (),
    ) -> "NextAction":
        return cls(
            action_id=action_id,
            kind=kind,
            disposition=disposition,
            reason_code=reason_code,
            reason_detail=reason_detail,
            display=display,
            required_inputs=required_inputs,
        )

    @property
    def argv(self) -> tuple[str, ...]:
        return self.action.argv if self.action is not None else ()

    @property
    def rendered_command(self) -> str | None:
        return self.action.command if self.action is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "disposition": self.disposition,
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
            "argv": list(self.argv),
            "command": self.rendered_command,
            "safety_class": self.action.safety_class if self.action is not None else "none",
            "mutation_class": self.action.mutation_class if self.action is not None else "none",
            "display": self.display,
            "choices": [choice.to_dict() for choice in self.choices],
            "alternatives": [alternative.to_dict() for alternative in self.alternatives],
            "required_inputs": list(self.required_inputs),
        }

    def legacy_command_rows(self) -> list[dict[str, Any]]:
        actions = (
            ((self.action,) if self.action is not None else self.choices)
            + self.alternatives
        )
        return [
            {
                "action_id": action.action_id,
                "command": action.command,
                "argv": list(action.argv),
                "reason": action.reason_detail,
                "disposition": action.disposition,
            }
            for action in actions
            if action is not None
        ]


@dataclass(frozen=True, slots=True)
class LifecycleContext:
    project_root: str
    workset_id: str
    task_id: str
    actor: str | None
    task_status: str | None
    attempt_status: str | None
    attempt_id: str | None
    active_attempt: bool
    worktree_path: str | None
    worktree_exists: bool
    worktree_dirty: bool
    branch_ahead_of_target: bool
    primary_worktree: str | None
    primary_dirty: bool
    branch_exists: bool | None
    target_branch_exists: bool | None
    stale_claim: bool
    reference_issue: bool = False
    reference_issue_code: str | None = None
    reference_issue_detail: str | None = None
    blackdog_executable: str = "blackdog"
    execution_prompt_hash: str | None = None
    execution_prompt_source: str | None = None
    execution_prompt_mode: str | None = None
    request_prompt_hash: str | None = None
    request_prompt_source: str | None = None
    request_prompt_mode: str | None = None
    resume_execution_prompt_file: str | None = None
    resume_request_file: str | None = None
    resume_request_distinct: bool = False
    resume_lineage_issue_code: str | None = None
    resume_lineage_issue_detail: str | None = None
    resume_start_incomplete: bool = False
    resume_start_issue_code: str | None = None
    resume_start_issue_detail: str | None = None
    reconciliation_candidate: bool = False
    landing_reconcile_argv: tuple[str, ...] = ()
    reconciliation_action_id: str | None = None
    reconciliation_reason_code: str | None = None
    reconciliation_reason_detail: str | None = None
    landing_transaction_incomplete: bool = False
    landing_last_phase: str | None = None
    landing_resume_argv: tuple[str, ...] = ()
    workspace_adoption_eligible: bool = False
    workspace_adoption_argv: tuple[str, ...] = ()
    workspace_adoption_issue_code: str | None = None
    workspace_adoption_issue_detail: str | None = None
    active_workspace_adoption: bool = False
    workspace_adoption_relation: str | None = None
    workspace_adoption_operation: str | None = None
    workspace_adoption_rebase_argv: tuple[str, ...] = ()
    workspace_adoption_candidate_arrived: bool = False
    workspace_adoption_completion_pending: bool = False
    workspace_adoption_completion_argv: tuple[str, ...] = ()
    source_git_operation: str | None = None
    source_git_operation_detail: str | None = None
    landing_correction_state: str | None = None
    landing_correction_resume_argv: tuple[str, ...] = ()
    landing_correction_worktree_path: str | None = None
    landing_correction_branch: str | None = None
    landing_correction_target_branch: str | None = None


def _flag(name: str, value: str) -> str:
    return f"--{name}={value}"


def _task_argv(context: LifecycleContext, command: str, *extra: str) -> tuple[str, ...]:
    return (
        context.blackdog_executable,
        "task",
        command,
        _flag("project-root", context.project_root),
        _flag("workset", context.workset_id),
        _flag("task", context.task_id),
        *extra,
    )


def _command_action(
    *,
    action_id: str,
    disposition: str,
    reason_code: str,
    reason_detail: str,
    argv: tuple[str, ...],
    safety_class: str,
    mutation_class: str,
    display: str,
) -> LifecycleAction:
    return LifecycleAction(
        action_id=action_id,
        disposition=disposition,
        reason_code=reason_code,
        reason_detail=reason_detail,
        argv=argv,
        safety_class=safety_class,
        mutation_class=mutation_class,
        display=display,
    )


def _terminal_close_actions(
    context: LifecycleContext,
    *,
    reason_code: str,
    reason_detail: str,
) -> tuple[LifecycleAction, ...]:
    if context.actor is None:
        raise ValueError("terminal close actions require a persisted actor")
    actor = context.actor
    return tuple(
        _command_action(
            action_id=f"close_{status}",
            disposition="operator_choice",
            reason_code=reason_code,
            reason_detail=reason_detail,
            argv=_task_argv(
                context,
                "close",
                _flag("actor", actor),
                _flag("status", status),
                _flag("summary", f"Close interrupted task as {status}"),
            ),
            safety_class="operator_confirmation",
            mutation_class="runtime",
            display=f"Close the active attempt as {status}",
        )
        for status in (ATTEMPT_STATUS_BLOCKED, ATTEMPT_STATUS_FAILED, ATTEMPT_STATUS_ABANDONED)
    )


def _close_choices(context: LifecycleContext, *, reason_code: str, reason_detail: str) -> NextAction:
    choices = _terminal_close_actions(
        context,
        reason_code=reason_code,
        reason_detail=reason_detail,
    )
    return NextAction.choice(
        action_id="choose_terminal_close",
        disposition="operator_choice",
        reason_code=reason_code,
        reason_detail=reason_detail,
        display="Choose how to close the active attempt",
        choices=choices,
    )


def _resume_action(context: LifecycleContext) -> LifecycleAction:
    if context.actor is None:
        raise ValueError("resume action requires a persisted actor")
    if context.resume_execution_prompt_file is None or context.execution_prompt_mode is None:
        raise ValueError("resume action requires verified execution-prompt lineage")
    if (
        context.execution_prompt_hash is None
        or context.request_prompt_hash is None
        or context.request_prompt_mode is None
    ):
        raise ValueError("resume action requires complete durable prompt lineage")
    prompt_args = (
        _flag("execution-prompt-file", context.resume_execution_prompt_file),
        _flag("prompt-mode", context.execution_prompt_mode),
        _flag("expected-actor", context.actor),
        _flag("expected-execution-prompt-hash", context.execution_prompt_hash),
        _flag("expected-execution-prompt-mode", context.execution_prompt_mode),
        _flag("expected-request-prompt-hash", context.request_prompt_hash),
        _flag("expected-request-prompt-mode", context.request_prompt_mode),
    )
    if context.resume_request_distinct:
        if context.resume_request_file is None:
            raise ValueError("distinct resume request lineage requires a verified request file")
        prompt_args = (*prompt_args, _flag("request-file", context.resume_request_file))
    return _command_action(
        action_id="resume_existing_task",
        disposition="retryable",
        reason_code="terminal_attempt_without_workspace",
        reason_detail="The terminal attempt has no retained workspace; start a new attempt in the same task envelope.",
        argv=_task_argv(context, "begin", _flag("actor", context.actor), *prompt_args),
        safety_class="validated_mutation",
        mutation_class="git_and_runtime",
        display="Resume the existing task in a new attempt",
    )


def _actor_required(context: LifecycleContext, *, operation: str) -> NextAction:
    return NextAction.terminal(
        action_id="actor_identity_required",
        kind="blocked",
        disposition="required_input",
        reason_code="actor_identity_missing",
        reason_detail=f"A persisted actor is required before Blackdog can emit {operation}.",
        display="Provide the invoking actor identity",
        required_inputs=("actor",),
    )


def _reference_repair_required(context: LifecycleContext) -> NextAction:
    return NextAction.terminal(
        action_id="inspect_reference_failure",
        kind="blocked",
        disposition="repair_required",
        reason_code=context.reference_issue_code or "git_reference_inspection_failed",
        reason_detail=context.reference_issue_detail or "Git reference inspection failed.",
        display="Repair the task and target branch relationship",
        required_inputs=("valid_task_branch_metadata", "valid_target_branch_metadata", "git_reference_proof"),
    )


def _resume_next_action(context: LifecycleContext) -> NextAction:
    if context.actor is None:
        return _actor_required(context, operation="same-envelope resume")
    if context.resume_lineage_issue_code is not None:
        required_inputs = ["execution_prompt_file_matching_recorded_hash", "recorded_prompt_mode"]
        if context.resume_request_distinct or context.request_prompt_hash is None:
            required_inputs.append("request_file_matching_recorded_hash")
        return NextAction.terminal(
            action_id="resume_lineage_required",
            kind="blocked",
            disposition="required_input",
            reason_code=context.resume_lineage_issue_code,
            reason_detail=context.resume_lineage_issue_detail
            or "Exact execution and request lineage could not be reconstructed.",
            display="Provide prompt files matching the recorded lineage",
            required_inputs=tuple(required_inputs),
        )
    return NextAction.command(_resume_action(context))


def _reference_proof_unknown(context: LifecycleContext) -> bool:
    return context.reference_issue or context.reference_issue_code is not None


def landing_evidence_required_action() -> NextAction:
    return NextAction.terminal(
        action_id="landing_evidence_required",
        kind="blocked",
        disposition="required_input",
        reason_code="landing_evidence_required",
        reason_detail=(
            "Canonical landing requires a nonblank completion summary and at least one "
            "validation evidence row before any mutation begins."
        ),
        display="Provide completion summary and validation evidence",
        required_inputs=("completion_summary", "validation_evidence"),
    )


def decide_next_action(context: LifecycleContext) -> NextAction:
    if context.workspace_adoption_completion_pending:
        if not context.workspace_adoption_completion_argv:
            return NextAction.terminal(
                action_id="adoption_completion_repair_identity_required",
                kind="blocked",
                disposition="repair_required",
                reason_code="adoption_completion_command_missing",
                reason_detail="Durable adoption completion evidence exists but its exact repair command is unavailable.",
                display="Repair adopted-successor completion identity",
                required_inputs=("completion_intent", "exact_repair_command"),
            )
        return NextAction.command(
            _command_action(
                action_id=(
                    "apply_adopted_successor_completion"
                    if context.active_attempt
                    else "repair_adoption_completion"
                ),
                disposition="retryable",
                reason_code="adoption_completion_pending",
                reason_detail="The durable adoption completion transaction must finish before cleanup.",
                argv=context.workspace_adoption_completion_argv,
                safety_class="proof_guarded_mutation",
                mutation_class="git_and_runtime",
                display="Repair adopted-successor completion",
            )
        )
    if context.workspace_adoption_issue_code is not None:
        return NextAction.terminal(
            action_id="workspace_adoption_proof_required",
            kind="blocked",
            disposition="proof_required",
            reason_code=context.workspace_adoption_issue_code,
            reason_detail=context.workspace_adoption_issue_detail
            or "The retained source does not satisfy the workspace-adoption contract.",
            display="Repair retained-workspace adoption proof",
            required_inputs=("exact_retained_source_proof",),
        )
    if context.resume_start_issue_code is not None:
        return NextAction.terminal(
            action_id="task_start_proof_required",
            kind="blocked",
            disposition="repair_required",
            reason_code=context.resume_start_issue_code,
            reason_detail=context.resume_start_issue_detail
            or "The reserved ordinary resume has conflicting deterministic start evidence.",
            display="Repair task-start proof",
            required_inputs=("canonical_resume_start_evidence",),
        )
    if context.resume_start_incomplete:
        lineage_action = _resume_next_action(context)
        if lineage_action.kind != "command":
            return lineage_action
        assert lineage_action.action is not None
        return NextAction.command(
            _command_action(
                action_id="repair_task_start_evidence",
                disposition="retryable",
                reason_code="task_start_incomplete",
                reason_detail=(
                    context.resume_start_issue_detail
                    or "The deterministic task attempt is reserved but its start evidence is incomplete."
                ),
                argv=lineage_action.action.argv,
                safety_class="validated_mutation",
                mutation_class="git_and_runtime",
                display="Repair the reserved task start",
            )
        )
    if context.landing_transaction_incomplete:
        if not context.landing_resume_argv:
            return NextAction.terminal(
                action_id="landing_resume_identity_required",
                kind="blocked",
                disposition="repair_required",
                reason_code="landing_resume_identity_missing",
                reason_detail="The landing transaction is incomplete but its exact resume command is unavailable.",
                display="Repair the landing transaction resume identity",
                required_inputs=("canonical_landing_intent",),
            )
        return NextAction.command(
            _command_action(
                action_id="resume_landing_transaction",
                disposition="retryable",
                reason_code="landing_transaction_incomplete",
                reason_detail=(
                    "The durable landing transaction must resume from phase "
                    f"{context.landing_last_phase or 'unknown'}."
                ),
                argv=context.landing_resume_argv,
                safety_class="validated_mutation",
                mutation_class="git_and_runtime",
                display="Resume the durable landing transaction",
            )
        )
    if context.source_git_operation is not None:
        return NextAction.terminal(
            action_id="resolve_task_source_git_operation",
            kind="blocked",
            disposition="repair_required",
            reason_code="task_source_git_operation_in_progress",
            reason_detail=(
                context.source_git_operation_detail
                or "The retained task workspace has an in-progress Git operation that "
                "requires the current landing agent to inspect and resolve it safely."
            ),
            display="Return the retained task workspace to a coherent Git state",
            required_inputs=(
                "task_worktree_git_operation_resolution",
                "unique_work_preservation_proof",
                "fresh_validation_evidence",
            ),
        )
    if context.landing_correction_state == "active":
        if not context.landing_correction_resume_argv:
            return NextAction.terminal(
                action_id="automatic_stale_recovery_proof_required",
                kind="blocked",
                disposition="repair_required",
                reason_code="automatic_stale_recovery_resume_missing",
                reason_detail=(
                    "The durable stale-correction receipt is active but has no "
                    "canonical resume command."
                ),
                display="Repair automatic stale-recovery evidence",
                required_inputs=("exact_task_land_resume_argv",),
            )
        return NextAction.command(
            LifecycleAction(
                action_id="resume_automatic_stale_recovery",
                disposition="retryable",
                reason_code="automatic_stale_recovery_incomplete",
                reason_detail=(
                    "Resume the append-once stale correction and canonical landing "
                    "from its durable receipt."
                ),
                argv=context.landing_correction_resume_argv,
                safety_class="validated_mutation",
                mutation_class="git_and_filesystem",
                display="Resume automatic stale recovery",
            )
        )
    if context.landing_correction_state == "retry_exhausted":
        if (
            context.landing_correction_worktree_path
            and context.landing_correction_target_branch
        ):
            return NextAction.command(
                LifecycleAction(
                    action_id="rebase_task_branch",
                    disposition="operator_action_required",
                    reason_code="stale_task_branch",
                    reason_detail=(
                        "The target moved after the one automatic correction; "
                        "perform the existing exact worktree-local rebase."
                    ),
                    argv=(
                        "git",
                        "-C",
                        context.landing_correction_worktree_path,
                        "rebase",
                        "--autostash",
                        context.landing_correction_target_branch,
                    ),
                    safety_class="operator_confirmation",
                    mutation_class="git_and_filesystem",
                    display=(
                        f"Rebase {context.landing_correction_branch or 'the task branch'} "
                        f"onto {context.landing_correction_target_branch}"
                    ),
                )
            )
        return NextAction.terminal(
            action_id="automatic_stale_recovery_proof_required",
            kind="blocked",
            disposition="repair_required",
            reason_code="automatic_stale_recovery_target_missing",
            reason_detail=(
                "Retry-exhausted correction evidence is missing its exact "
                "worktree or target identity."
            ),
            display="Repair automatic stale-recovery evidence",
            required_inputs=("task_worktree", "target_branch"),
        )
    automatic_blockers = {
        "conflict": (
            "automatic_stale_recovery_conflict",
            "automatic_rebase_conflict",
            "The automatic rebase encountered a real content conflict.",
            (
                "task_worktree_conflict_resolution",
                "unique_work_preservation_proof",
                "fresh_validation_evidence",
            ),
        ),
        "validation_failed": (
            "automatic_stale_recovery_validation_failed",
            "post_rebase_validation_failed",
            "Configured validation did not prove the corrected task tree.",
            ("task_worktree_repair", "fresh_validation_evidence"),
        ),
        "unsafe": (
            "automatic_stale_recovery_unsafe",
            "automatic_rebase_safety_unproven",
            "Blackdog could not prove a safe automatic correction state.",
            ("git_operation_proof", "unique_work_preservation_proof"),
        ),
    }
    automatic_blocker = automatic_blockers.get(
        str(context.landing_correction_state or "")
    )
    if automatic_blocker is not None:
        action_id, reason_code, detail, required_inputs = automatic_blocker
        return NextAction.terminal(
            action_id=action_id,
            kind="blocked",
            disposition="repair_required",
            reason_code=reason_code,
            reason_detail=(
                f"{detail} The retained task workspace remains authoritative."
            ),
            display="Return automatic stale recovery to the current landing agent",
            required_inputs=required_inputs,
        )
    if context.reconciliation_candidate:
        if context.landing_reconcile_argv:
            return NextAction.command(
                _command_action(
                    action_id=(
                        context.reconciliation_action_id
                        or "verify_late_landing_reconciliation"
                    ),
                    disposition="proof_required",
                    reason_code=(
                        context.reconciliation_reason_code
                        or "abort_complete_target_contains_candidate"
                    ),
                    reason_detail=(
                        context.reconciliation_reason_detail
                        or "The abort is terminal, but its exact canonical candidate is now reachable from target; verify reconciliation proof."
                    ),
                    argv=context.landing_reconcile_argv,
                    safety_class="read_only",
                    mutation_class="none",
                    display=(
                        "Verify the detected legacy landing reconciliation"
                        if context.reconciliation_action_id
                        == "verify_legacy_landing_reconciliation"
                        else "Verify the late landing reconciliation"
                    ),
                )
            )
        return NextAction.terminal(
            action_id="reconciliation_proof_pending",
            kind="blocked",
            disposition="proof_required",
            reason_code="reconciliation_candidate_unproven",
            reason_detail="A possible post-Git finalization gap requires canonical commit proof before reconciliation.",
            display="Await reconciliation proof",
        )

    if context.workspace_adoption_candidate_arrived:
        if context.landing_reconcile_argv:
            return NextAction.command(
                _command_action(
                    action_id="verify_adopted_successor_completion",
                    disposition="proof_required",
                    reason_code="adoption_candidate_reached_target",
                    reason_detail=(
                        "The adopted predecessor candidate reached target; verify and finalize the active successor."
                    ),
                    argv=context.landing_reconcile_argv,
                    safety_class="read_only",
                    mutation_class="none",
                    display="Verify adopted successor completion",
                )
            )
        return NextAction.terminal(
            action_id="adoption_completion_proof_pending",
            kind="blocked",
            disposition="proof_required",
            reason_code="adoption_candidate_arrived_without_proof_command",
            reason_detail="The adopted candidate reached target but exact successor completion proof is unavailable.",
            display="Repair adopted successor completion proof",
            required_inputs=("workspace_adoption_receipt", "canonical_candidate_proof"),
        )

    if context.stale_claim:
        choices = tuple(
            _command_action(
                action_id=f"release_stale_claim_{status}",
                disposition="operator_choice",
                reason_code="stale_claim",
                reason_detail="No active attempt owns the retained task claim.",
                argv=_task_argv(
                    context,
                    "recover",
                    "--release-stale-claim",
                    _flag("status", status),
                    _flag("summary", f"Release stale claim as {status}"),
                ),
                safety_class="operator_confirmation",
                mutation_class="runtime",
                display=f"Release the stale claim as {status}",
            )
            for status in (ATTEMPT_STATUS_BLOCKED, ATTEMPT_STATUS_FAILED, ATTEMPT_STATUS_ABANDONED)
        )
        return NextAction.choice(
            action_id="choose_stale_claim_release",
            disposition="operator_choice",
            reason_code="stale_claim",
            reason_detail="No active attempt owns the retained task claim.",
            display="Choose how to release the stale claim",
            choices=choices,
        )

    if _reference_proof_unknown(context):
        return _reference_repair_required(context)

    if context.task_status == TASK_STATUS_CANCELED:
        if context.actor is None:
            return _actor_required(context, operation="task reopen")
        return NextAction.command(
            _command_action(
                action_id="reopen_canceled_task",
                disposition="operator_choice",
                reason_code="task_canceled",
                reason_detail="Canceled tasks must be reopened before another attempt can start.",
                argv=_task_argv(
                    context,
                    "reopen",
                    _flag("actor", context.actor),
                    _flag("summary", "Reopen canceled task for continued execution"),
                ),
                safety_class="operator_confirmation",
                mutation_class="runtime",
                display="Reopen the canceled task",
            )
        )

    if context.workspace_adoption_eligible:
        if not context.workspace_adoption_argv:
            return NextAction.terminal(
                action_id="workspace_adoption_identity_required",
                kind="blocked",
                disposition="repair_required",
                reason_code="workspace_adoption_command_missing",
                reason_detail="The retained workspace is eligible but its exact guarded adoption command is unavailable.",
                display="Repair retained-workspace adoption identity",
                required_inputs=("workspace_adoption_proof",),
            )
        return NextAction.command(
            _command_action(
                action_id="adopt_aborted_landing_source",
                disposition="retryable",
                reason_code="abort_complete_source_retained",
                reason_detail="The exact abort-complete source workspace can become a deterministic successor attempt.",
                argv=context.workspace_adoption_argv,
                safety_class="validated_mutation",
                mutation_class="runtime",
                display="Adopt the retained landing source workspace",
            )
        )

    if context.active_workspace_adoption:
        if context.workspace_adoption_operation is not None:
            return NextAction.terminal(
                action_id="resolve_adopted_workspace_git_operation",
                kind="blocked",
                disposition="repair_required",
                reason_code="adopted_workspace_git_operation_in_progress",
                reason_detail=(
                    "The adopted workspace has an in-progress Git operation "
                    f"({context.workspace_adoption_operation}); resolve or abort it before continuing."
                ),
                display="Resolve the adopted workspace Git operation",
                required_inputs=("clean_git_operation_state",),
            )
        if context.workspace_adoption_relation in {"behind", "diverged"}:
            if context.worktree_dirty:
                return NextAction.terminal(
                    action_id="clean_adopted_workspace_before_rebase",
                    kind="blocked",
                    disposition="repair_required",
                    reason_code="adopted_workspace_dirty_before_rebase",
                    reason_detail="The adopted workspace must be managed-clean before the exact target rebase.",
                    display="Clean the adopted workspace before rebasing",
                    required_inputs=("managed_clean_workspace",),
                )
            if not context.workspace_adoption_rebase_argv:
                return NextAction.terminal(
                    action_id="adopted_workspace_rebase_identity_required",
                    kind="blocked",
                    disposition="repair_required",
                    reason_code="adopted_workspace_rebase_command_missing",
                    reason_detail="Target drift requires rebase but the exact command is unavailable.",
                    display="Repair adopted-workspace rebase identity",
                    required_inputs=("target_branch", "worktree_path"),
                )
            return NextAction.command(
                _command_action(
                    action_id="rebase_adopted_workspace",
                    disposition="retryable",
                    reason_code=f"adopted_workspace_{context.workspace_adoption_relation}",
                    reason_detail="The adopted workspace must rebase onto the current target before landing.",
                    argv=context.workspace_adoption_rebase_argv,
                    safety_class="validated_mutation",
                    mutation_class="git_and_filesystem",
                    display="Rebase the adopted workspace onto target",
                )
            )

    if context.active_attempt:
        if context.actor is None:
            return _actor_required(context, operation="active-attempt close or landing")
        if context.branch_exists is False or context.target_branch_exists is False or not context.worktree_exists:
            reason_code = (
                "missing_task_branch"
                if context.branch_exists is False
                else "missing_target_branch"
                if context.target_branch_exists is False
                else "missing_active_worktree"
            )
            return _close_choices(
                context,
                reason_code=reason_code,
                reason_detail="The active attempt cannot use the normal landing path until its missing Git state is repaired.",
            )
        if context.primary_dirty:
            argv = (
                "git",
                "-C",
                context.primary_worktree or context.project_root,
                "status",
                "--short",
            )
            return NextAction.command(
                _command_action(
                    action_id="inspect_dirty_primary",
                    disposition="retryable_after_cleanup",
                    reason_code="dirty_primary_worktree",
                    reason_detail="Primary-worktree changes block canonical landing.",
                    argv=argv,
                    safety_class="read_only",
                    mutation_class="none",
                    display="Inspect the dirty primary worktree",
                )
            )
        if context.worktree_dirty or context.branch_ahead_of_target:
            return landing_evidence_required_action()
        return NextAction.command(
            _command_action(
                action_id="inspect_pristine_active_task",
                disposition="continue_work",
                reason_code="active_attempt_pristine",
                reason_detail="The active attempt has no detected changes yet.",
                argv=("git", "-C", context.worktree_path or context.project_root, "status", "--short"),
                safety_class="read_only",
                mutation_class="none",
                display="Inspect the active task workspace",
            ),
            alternatives=_terminal_close_actions(
                context,
                reason_code="close_pristine_attempt",
                reason_detail="Close the active attempt if no implementation work should continue.",
            ),
        )

    if context.attempt_status == ATTEMPT_STATUS_SUCCESS:
        if context.worktree_exists:
            if context.worktree_dirty:
                return NextAction.command(
                    _command_action(
                        action_id="inspect_retained_success_workspace",
                        disposition="cleanup_blocked",
                        reason_code="retained_success_workspace_dirty",
                        reason_detail="A successful retained workspace has new uncommitted changes.",
                        argv=("git", "-C", context.worktree_path or context.project_root, "status", "--short"),
                        safety_class="read_only",
                        mutation_class="none",
                        display="Inspect the retained successful workspace",
                    )
                )
            return NextAction.command(
                _command_action(
                    action_id="cleanup_landed_task",
                    disposition="cleanup_ready",
                    reason_code="successful_workspace_retained",
                    reason_detail="The successful task workspace is retained and proven disposable by cleanup.",
                    argv=_task_argv(context, "cleanup"),
                    safety_class="validated_mutation",
                    mutation_class="git_and_filesystem",
                    display="Clean up the landed task workspace",
                )
            )
        if context.branch_exists is True:
            return NextAction.command(
                _command_action(
                    action_id="cleanup_landed_branch",
                    disposition="cleanup_ready",
                    reason_code="successful_branch_retained",
                    reason_detail="The successful workspace is absent but its proven task branch remains.",
                    argv=_task_argv(context, "cleanup"),
                    safety_class="validated_mutation",
                    mutation_class="git_and_filesystem",
                    display="Clean up the landed task branch",
                )
            )
        return NextAction.terminal(
            action_id="task_complete",
            kind="complete",
            disposition="complete",
            reason_code="successful_task_fully_cleaned",
            reason_detail="The task landed successfully and no retained workspace remains.",
            display="No further lifecycle action is required",
        )

    if context.attempt_status in {
        ATTEMPT_STATUS_BLOCKED,
        ATTEMPT_STATUS_FAILED,
        ATTEMPT_STATUS_ABANDONED,
    }:
        if context.target_branch_exists is False or (
            context.worktree_exists and context.branch_exists is False
        ):
            if context.actor is None:
                return _actor_required(context, operation="stale task cancellation")
            return NextAction.command(
                _command_action(
                    action_id="cancel_stale_task",
                    disposition="operator_choice",
                    reason_code=(
                        "missing_target_branch"
                        if context.target_branch_exists is False
                        else "missing_task_branch"
                    ),
                    reason_detail="Terminal task state references Git state that no longer exists.",
                    argv=_task_argv(
                        context,
                        "cancel",
                        _flag("actor", context.actor),
                        _flag("summary", "Cancel task with stale Git references"),
                        _flag("failure-class", FAILURE_CLASS_STALE_BRANCH),
                        _flag("recovery-action", "restore_ref_or_reopen_task"),
                        "--operator-issue",
                    ),
                    safety_class="operator_confirmation",
                    mutation_class="runtime",
                    display="Cancel the task with stale Git references",
                )
            )
        if context.worktree_exists:
            if context.worktree_dirty or context.branch_ahead_of_target:
                return NextAction.command(
                    _command_action(
                        action_id="inspect_terminal_workspace",
                        disposition="operator_review",
                        reason_code="terminal_workspace_has_work",
                        reason_detail="The terminal attempt retained work that must be reviewed before cleanup or restart.",
                        argv=("git", "-C", context.worktree_path or context.project_root, "status", "--short"),
                        safety_class="read_only",
                        mutation_class="none",
                        display="Inspect the retained terminal workspace",
                    )
                )
            return NextAction.command(
                _command_action(
                    action_id="cleanup_terminal_workspace",
                    disposition="cleanup_ready",
                    reason_code="terminal_workspace_clean",
                    reason_detail="The terminal attempt retained a clean workspace that must be removed before restart.",
                    argv=_task_argv(context, "cleanup"),
                    safety_class="validated_mutation",
                    mutation_class="git_and_filesystem",
                    display="Clean up the terminal task workspace",
                )
            )
        if context.branch_exists is True:
            return NextAction.command(
                _command_action(
                    action_id="cleanup_terminal_branch",
                    disposition="cleanup_ready",
                    reason_code="terminal_branch_retained",
                    reason_detail="The terminal workspace is absent but its proven task branch remains.",
                    argv=_task_argv(context, "cleanup"),
                    safety_class="validated_mutation",
                    mutation_class="git_and_filesystem",
                    display="Clean up the retained terminal task branch",
                )
            )
        return _resume_next_action(context)

    if context.branch_exists is False or context.target_branch_exists is False:
        if context.actor is None:
            return _actor_required(context, operation="stale task cancellation")
        return NextAction.command(
            _command_action(
                action_id="cancel_stale_task",
                disposition="operator_choice",
                reason_code=(
                    "missing_task_branch" if context.branch_exists is False else "missing_target_branch"
                ),
                reason_detail="Task state references Git state that no longer exists.",
                argv=_task_argv(
                    context,
                    "cancel",
                    _flag("actor", context.actor),
                    _flag("summary", "Cancel task with stale Git references"),
                    _flag("failure-class", FAILURE_CLASS_STALE_BRANCH),
                    _flag("recovery-action", "restore_ref_or_reopen_task"),
                    "--operator-issue",
                ),
                safety_class="operator_confirmation",
                mutation_class="runtime",
                display="Cancel the task with stale Git references",
            )
        )

    if context.reference_issue:
        return _reference_repair_required(context)

    if context.attempt_status == ATTEMPT_STATUS_IN_PROGRESS:
        return NextAction.terminal(
            action_id="active_attempt_state_inconsistent",
            kind="blocked",
            disposition="blocked",
            reason_code="attempt_marked_active_without_claim",
            reason_detail="Runtime state marks an attempt active but no active claim was resolved.",
            display="Repair inconsistent active-attempt state",
        )

    return _resume_next_action(context)


@dataclass(frozen=True, slots=True)
class OperationResult(Mapping[str, Any]):
    operation: str
    operation_status: str
    task_status: str | None
    attempt_status: str | None
    disposition: str
    mutation_started: bool
    mutation_completed: bool
    mutation_phase: str
    failure_code: str | None
    next_action: NextAction
    legacy_payload: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        if self.operation not in NORMAL_TASK_OPERATIONS:
            raise ValueError(f"invalid normal task operation: {self.operation}")
        if self.operation_status not in OPERATION_STATUSES:
            raise ValueError(f"invalid operation status: {self.operation_status}")
        if self.task_status is not None and self.task_status not in TASK_STATUSES:
            raise ValueError(f"invalid post-operation task status: {self.task_status}")
        if self.attempt_status is not None and self.attempt_status not in ATTEMPT_STATUSES:
            raise ValueError(f"invalid post-operation attempt status: {self.attempt_status}")
        if self.mutation_phase not in MUTATION_PHASES:
            raise ValueError(f"invalid mutation phase: {self.mutation_phase}")
        if self.mutation_completed and not self.mutation_started:
            raise ValueError("completed mutation must also be marked started")
        if self.failure_code is not None and self.failure_code not in OPERATION_FAILURE_CODES:
            raise ValueError(f"invalid bounded failure code: {self.failure_code}")
        if self.disposition != self.next_action.disposition:
            raise ValueError("operation disposition must match its post-operation next action")

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.legacy_payload)
        payload.update(
            {
                "operation": self.operation,
                "operation_status": self.operation_status,
                "task_status": self.task_status,
                "attempt_status": self.attempt_status,
                "disposition": self.disposition,
                "mutation_started": self.mutation_started,
                "mutation_completed": self.mutation_completed,
                "mutation_phase": self.mutation_phase,
                "failure_code": self.failure_code,
                "next_action": self.next_action.to_dict(),
            }
        )
        return payload

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


__all__ = [
    "ACTION_MUTATION_CLASSES",
    "ACTION_SAFETY_CLASSES",
    "CleanupPostMutationError",
    "CleanupEventFinalizationError",
    "CleanupOwnershipError",
    "DirtyPrimaryWorktreeError",
    "DirtyTargetWorktreeError",
    "FailureMapping",
    "GitReferenceInspection",
    "LifecycleAction",
    "LifecycleContext",
    "LifecycleGitError",
    "MissingTaskWorktreeError",
    "MUTATION_PHASES",
    "NEXT_ACTION_KINDS",
    "NORMAL_TASK_OPERATIONS",
    "NextAction",
    "NoChangesToLandError",
    "OperationResult",
    "OPERATION_FAILURE_CODES",
    "OPERATION_STATUSES",
    "PRE_ATTEMPT_FAILURE_CODES",
    "REFERENCE_INSPECTION_STATES",
    "StaleTaskBranchError",
    "WorktreeError",
    "classify_lifecycle_exception",
    "decide_next_action",
    "landing_evidence_required_action",
]
