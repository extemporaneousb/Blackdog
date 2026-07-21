from __future__ import annotations

from contextlib import chdir, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
from unittest.mock import patch

import blackdog.wtam as wtam
import blackdog_core.backlog as backlog
from blackdog.contract import managed_skill_relative_path
from blackdog.landing import load_landing_transaction
from blackdog.lifecycle import (
    DirtyPrimaryWorktreeError,
    DirtyTargetWorktreeError,
    LifecycleAction,
    LifecycleContext,
    MissingTaskWorktreeError,
    NoChangesToLandError,
    OperationResult,
    StaleTaskBranchError,
    WorktreeError,
    classify_lifecycle_exception,
    decide_next_action,
)
from blackdog.wtam import (
    render_cleanup_text,
    render_close_text,
    render_land_text,
    render_landing_reconciliation_text,
    render_recover_text,
    render_show_text,
    render_task_begin_text,
    render_task_state_text,
)
from blackdog_core.backlog import BacklogError, load_planning_state, upsert_workset
from blackdog_core.profile import load_profile
from blackdog_core.state import (
    ATTEMPT_STATUS_BLOCKED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_IN_PROGRESS,
    ATTEMPT_STATUS_SUCCESS,
    FAILURE_CLASS_DIRTY_PRIMARY,
    FAILURE_CLASS_MISSING_WORKTREE,
    FAILURE_CLASS_NO_CHANGES,
    FAILURE_CLASS_STALE_BRANCH,
    FAILURE_CLASS_UNKNOWN,
    TASK_STATUS_BLOCKED,
    TASK_STATUS_CANCELED,
    TaskRuntimeRecord,
    append_event,
    load_events,
    load_runtime_state,
    merge_workset_runtime,
    now_iso,
    save_runtime_state,
    task_state_index,
)
from blackdog_cli.main import _build_parser, main as blackdog_main
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


def _context(**changes: object) -> LifecycleContext:
    baseline = LifecycleContext(
        project_root="/tmp/repo with spaces",
        workset_id="workset-1",
        task_id="TASK-1",
        actor="codex",
        task_status="in_progress",
        attempt_status=ATTEMPT_STATUS_IN_PROGRESS,
        attempt_id="attempt-1",
        active_attempt=True,
        worktree_path="/tmp/task workspace",
        worktree_exists=True,
        worktree_dirty=False,
        branch_ahead_of_target=False,
        primary_worktree="/tmp/primary workspace",
        primary_dirty=False,
        branch_exists=True,
        target_branch_exists=True,
        stale_claim=False,
        execution_prompt_hash="execution-hash",
        execution_prompt_source="/tmp/execution.md",
        execution_prompt_mode="raw",
        request_prompt_hash="execution-hash",
        request_prompt_source="/tmp/execution.md",
        request_prompt_mode="raw",
        resume_execution_prompt_file="/tmp/execution.md",
    )
    return replace(baseline, **changes)


def _actions(next_action):
    if next_action.action is not None:
        yield next_action.action
    yield from next_action.choices
    yield from next_action.alternatives


class LifecycleDecisionTests(CoreAuditTestCase):
    def test_state_matrix_has_one_bounded_primary_next_action(self) -> None:
        matrix = {
            "pristine active": (_context(), "command", "inspect_pristine_active_task"),
            "dirty active": (
                _context(worktree_dirty=True),
                "blocked",
                "landing_evidence_required",
            ),
            "ahead active": (
                _context(branch_ahead_of_target=True),
                "blocked",
                "landing_evidence_required",
            ),
            "primary dirty": (
                _context(primary_dirty=True),
                "command",
                "inspect_dirty_primary",
            ),
            "missing active worktree": (
                _context(worktree_exists=False),
                "choice",
                "choose_terminal_close",
            ),
            "missing active task branch": (
                _context(branch_exists=False),
                "choice",
                "choose_terminal_close",
            ),
            "missing active target": (
                _context(target_branch_exists=False),
                "choice",
                "choose_terminal_close",
            ),
            "active ancestry error": (
                _context(
                    reference_issue=True,
                    reference_issue_code="branch_relationship_inspection_failed",
                    reference_issue_detail="merge-base inspection failed",
                ),
                "blocked",
                "inspect_reference_failure",
            ),
            "task branch metadata missing": (
                _context(
                    reference_issue=True,
                    reference_issue_code="task_branch_metadata_missing",
                    reference_issue_detail="attempt has no task branch metadata",
                    branch_exists=None,
                ),
                "blocked",
                "inspect_reference_failure",
            ),
            "target branch metadata missing": (
                _context(
                    reference_issue=True,
                    reference_issue_code="target_branch_metadata_missing",
                    reference_issue_detail="attempt has no target branch metadata",
                    target_branch_exists=None,
                ),
                "blocked",
                "inspect_reference_failure",
            ),
            "stale claim": (
                _context(active_attempt=False, stale_claim=True),
                "choice",
                "choose_stale_claim_release",
            ),
            "canceled": (
                _context(
                    active_attempt=False,
                    task_status=TASK_STATUS_CANCELED,
                    attempt_status=ATTEMPT_STATUS_FAILED,
                ),
                "command",
                "reopen_canceled_task",
            ),
            "failed retained dirty": (
                _context(
                    active_attempt=False,
                    attempt_status=ATTEMPT_STATUS_FAILED,
                    worktree_dirty=True,
                ),
                "command",
                "inspect_terminal_workspace",
            ),
            "blocked retained clean": (
                _context(
                    active_attempt=False,
                    attempt_status=ATTEMPT_STATUS_BLOCKED,
                ),
                "command",
                "cleanup_terminal_workspace",
            ),
            "failed fully cleaned": (
                _context(
                    active_attempt=False,
                    attempt_status=ATTEMPT_STATUS_FAILED,
                    worktree_exists=False,
                    branch_exists=False,
                ),
                "command",
                "resume_existing_task",
            ),
            "failed missing target": (
                _context(
                    active_attempt=False,
                    attempt_status=ATTEMPT_STATUS_FAILED,
                    worktree_exists=False,
                    branch_exists=False,
                    target_branch_exists=False,
                ),
                "command",
                "cancel_stale_task",
            ),
            "success retained dirty": (
                _context(
                    active_attempt=False,
                    task_status="done",
                    attempt_status=ATTEMPT_STATUS_SUCCESS,
                    worktree_dirty=True,
                ),
                "command",
                "inspect_retained_success_workspace",
            ),
            "success cleanup ready": (
                _context(
                    active_attempt=False,
                    task_status="done",
                    attempt_status=ATTEMPT_STATUS_SUCCESS,
                ),
                "command",
                "cleanup_landed_task",
            ),
            "success fully clean": (
                _context(
                    active_attempt=False,
                    task_status="done",
                    attempt_status=ATTEMPT_STATUS_SUCCESS,
                    worktree_exists=False,
                    branch_exists=False,
                ),
                "complete",
                "task_complete",
            ),
            "reconciliation placeholder": (
                _context(reconciliation_candidate=True),
                "blocked",
                "reconciliation_proof_pending",
            ),
        }

        for label, (context, expected_kind, expected_action_id) in matrix.items():
            with self.subTest(label=label):
                next_action = decide_next_action(context)
                self.assertEqual(next_action.kind, expected_kind)
                self.assertEqual(next_action.action_id, expected_action_id)
                if next_action.kind == "command":
                    self.assertIsNotNone(next_action.action)
                    self.assertFalse(next_action.choices)
                elif next_action.kind == "choice":
                    self.assertIsNone(next_action.action)
                    self.assertGreaterEqual(len(next_action.choices), 2)
                else:
                    self.assertFalse(tuple(_actions(next_action)))

        evidence_action = decide_next_action(_context(worktree_dirty=True))
        self.assertEqual(evidence_action.argv, ())
        self.assertEqual(
            evidence_action.required_inputs,
            ("completion_summary", "validation_evidence"),
        )

    def test_pretransaction_stale_landing_projects_exact_rebase_action(self) -> None:
        workspace = "/tmp/task workspace with spaces"
        next_action = wtam._pretransaction_landing_failure_next_action(
            {
                "branch": "agent/task-1",
                "target_branch": "release/next",
                "worktree_path": workspace,
                "failure_class": FAILURE_CLASS_STALE_BRANCH,
                "recovery_action": "rebase_task_branch",
                "landing_transaction": None,
            }
        )

        self.assertIsNotNone(next_action)
        assert next_action is not None
        self.assertEqual(next_action.kind, "command")
        self.assertEqual(next_action.action_id, "rebase_task_branch")
        self.assertEqual(next_action.reason_code, "stale_task_branch")
        self.assertEqual(
            next_action.argv,
            ("git", "-C", workspace, "rebase", "--autostash", "release/next"),
        )
        self.assertIsNone(
            wtam._pretransaction_landing_failure_next_action(
                {
                    "failure_class": FAILURE_CLASS_STALE_BRANCH,
                    "recovery_action": "rebase_task_branch",
                    "landing_transaction": {"transaction_id": "durable"},
                }
            )
        )

    def test_requires_validation_actions_must_carry_validation_evidence(self) -> None:
        invalid_argvs = (
            ("blackdog", "task", "land", "--summary=fixture"),
            ("blackdog", "task", "land", "--summary=fixture", "--validation"),
            ("blackdog", "task", "land", "--validation=missing-status"),
            ("blackdog", "task", "land", "--validation==passed"),
            ("blackdog", "task", "land", "--validation=unit=unknown"),
            ("blackdog", "task", "land", "--validation", "unit=unknown"),
        )
        for argv in invalid_argvs:
            with self.subTest(argv=argv), self.assertRaisesRegex(
                ValueError,
                "requires_validation lifecycle actions must carry validation evidence",
            ):
                LifecycleAction(
                    action_id="unsafe_land",
                    disposition="ready",
                    reason_code="fixture",
                    reason_detail="The fixture omitted valid validation evidence.",
                    argv=argv,
                    safety_class="requires_validation",
                    mutation_class="git_and_runtime",
                    display="Unsafe fixture",
                )

        action = LifecycleAction(
            action_id="validated_land",
            disposition="ready",
            reason_code="fixture",
            reason_detail="The fixture carries explicit skipped evidence.",
            argv=(
                "blackdog",
                "task",
                "land",
                "--summary=fixture",
                "--validation=not-run=skipped",
            ),
            safety_class="requires_validation",
            mutation_class="git_and_runtime",
            display="Validated fixture",
        )
        self.assertIn("--validation=not-run=skipped", action.argv)
        split_action = LifecycleAction(
            action_id="validated_land_split",
            disposition="ready",
            reason_code="fixture",
            reason_detail="The fixture carries split-form validation evidence.",
            argv=("blackdog", "task", "land", "--validation", "unit=passed"),
            safety_class="requires_validation",
            mutation_class="git_and_runtime",
            display="Validated split fixture",
        )
        self.assertEqual(split_action.argv[-1], "unit=passed")

    def test_all_emitted_commands_are_complete_parseable_and_shell_round_trip(self) -> None:
        prompt_file = Path(self.tmp.name) / "prompt -- with 'quotes'.md"
        prompt_file.write_text("resume this task\n", encoding="utf-8")
        weird = _context(
            project_root=str(Path(self.tmp.name) / "repo -- with spaces"),
            workset_id="-work set's id",
            task_id='-task "quoted" id',
            actor="-agent's quoted name",
            active_attempt=False,
            task_status="blocked",
            attempt_status=ATTEMPT_STATUS_FAILED,
            worktree_exists=False,
            branch_exists=False,
            execution_prompt_source=str(prompt_file),
            request_prompt_source=str(prompt_file),
            resume_execution_prompt_file=str(prompt_file),
        )
        contexts = [
            weird,
            replace(weird, active_attempt=True, attempt_status=ATTEMPT_STATUS_IN_PROGRESS, worktree_exists=True, branch_exists=True, worktree_dirty=True),
            replace(weird, active_attempt=True, attempt_status=ATTEMPT_STATUS_IN_PROGRESS, worktree_exists=True, branch_exists=True, worktree_dirty=False),
            replace(weird, active_attempt=False, stale_claim=True, branch_exists=True),
            replace(weird, active_attempt=False, task_status=TASK_STATUS_CANCELED, branch_exists=True),
        ]
        parser = _build_parser()
        for context in contexts:
            next_action = decide_next_action(context)
            for action in _actions(next_action):
                with self.subTest(action=action.action_id):
                    self.assertTrue(action.argv)
                    self.assertEqual(shlex.split(action.command), list(action.argv))
                    self.assertNotIn("...", action.command)
                    self.assertNotIn("blocked|failed", action.command)
                    if action.argv[0].endswith("blackdog") or action.argv[0] == "blackdog":
                        parsed = parser.parse_args(list(action.argv[1:]))
                        self.assertEqual(parsed.project_root, context.project_root)
                        self.assertEqual(parsed.workset, context.workset_id)
                        self.assertEqual(parsed.task, context.task_id)

        missing_prompt_action = decide_next_action(
            replace(
                weird,
                resume_execution_prompt_file=None,
                resume_lineage_issue_code="execution_prompt_file_missing",
                resume_lineage_issue_detail="recorded prompt file is missing",
            )
        )
        self.assertEqual(missing_prompt_action.kind, "blocked")
        self.assertEqual(missing_prompt_action.action_id, "resume_lineage_required")
        self.assertEqual(missing_prompt_action.argv, ())
        self.assertIn("execution_prompt_file_matching_recorded_hash", missing_prompt_action.required_inputs)

    def test_adoption_proof_conflict_blocks_before_generic_landing_resume(self) -> None:
        next_action = decide_next_action(
            _context(
                landing_transaction_incomplete=True,
                landing_last_phase="temporary_cleanup_complete",
                landing_resume_argv=(
                    "blackdog",
                    "task",
                    "land",
                    "--project-root=/tmp/repo with spaces",
                    "--workset=workset-1",
                    "--task=TASK-1",
                ),
                active_workspace_adoption=True,
                workspace_adoption_issue_code="active_workspace_adoption_proof_failed",
                workspace_adoption_issue_detail="completion intent conflicts with native landing proof",
            )
        )

        self.assertEqual(next_action.action_id, "workspace_adoption_proof_required")
        self.assertEqual(next_action.kind, "blocked")
        self.assertEqual(
            next_action.reason_code,
            "active_workspace_adoption_proof_failed",
        )
        self.assertEqual(next_action.argv, ())

    def test_typed_landing_failures_do_not_classify_legacy_message_phrases(self) -> None:
        primary = classify_lifecycle_exception(
            DirtyPrimaryWorktreeError(
                primary_worktree=Path("/tmp/primary"),
                branch="task",
                target_branch="main",
                dirty_paths=["owned.txt"],
            )
        )
        self.assertEqual(primary.failure_code, FAILURE_CLASS_DIRTY_PRIMARY)
        self.assertTrue(primary.operator_issue)

        stale = classify_lifecycle_exception(
            StaleTaskBranchError(branch="task", target_branch="main", branch_worktree=None)
        )
        self.assertEqual(stale.failure_code, FAILURE_CLASS_STALE_BRANCH)
        altered_stale = StaleTaskBranchError(
            branch="task",
            target_branch="main",
            branch_worktree=None,
        )
        altered_stale.args = ("prose with no branch, rebase, or stale markers",)
        self.assertEqual(
            classify_lifecycle_exception(altered_stale).failure_code,
            FAILURE_CLASS_STALE_BRANCH,
        )
        missing = MissingTaskWorktreeError("/tmp/missing task workspace")
        missing.args = ("an entirely different diagnostic",)
        missing_mapping = classify_lifecycle_exception(missing)
        self.assertEqual(missing_mapping.failure_code, FAILURE_CLASS_MISSING_WORKTREE)
        self.assertEqual(missing_mapping.recovery_action, "restore_or_cleanup_worktree")
        self.assertTrue(missing_mapping.operator_issue)
        no_changes = classify_lifecycle_exception(
            NoChangesToLandError(branch="task", target_branch="main")
        )
        self.assertEqual(no_changes.failure_code, FAILURE_CLASS_NO_CHANGES)
        self.assertEqual(no_changes.terminal_attempt_status, ATTEMPT_STATUS_BLOCKED)
        target = classify_lifecycle_exception(DirtyTargetWorktreeError(Path("/tmp/target")))
        self.assertEqual(target.failure_code, FAILURE_CLASS_UNKNOWN)
        self.assertTrue(target.operator_issue)

        legacy = classify_lifecycle_exception(
            WorktreeError("dirty primary worktree; task branch has no changes and is stale")
        )
        self.assertEqual(legacy.failure_code, FAILURE_CLASS_UNKNOWN)
        self.assertEqual(legacy.recovery_action, "inspect")
        self.assertFalse(legacy.operator_issue)

    def test_operation_text_and_json_share_the_exact_next_action(self) -> None:
        action = LifecycleAction(
            action_id="inspect_exact_state",
            disposition="operator_review",
            reason_code="fixture",
            reason_detail="Inspect the exact fixture state.",
            argv=("git", "-C", "/tmp/path with spaces", "status", "--short"),
            safety_class="read_only",
            mutation_class="none",
            display="Inspect exact state",
        )
        next_action = decide_next_action(_context(primary_dirty=True))
        next_action = next_action.command(action)
        result = OperationResult(
            operation="task.cancel",
            operation_status="succeeded",
            task_status="canceled",
            attempt_status=ATTEMPT_STATUS_FAILED,
            disposition=next_action.disposition,
            mutation_started=True,
            mutation_completed=True,
            mutation_phase="runtime_finalized",
            failure_code=None,
            next_action=next_action,
            legacy_payload={
                "workset_id": "workset-1",
                "task_id": "TASK-1",
                "actor": "codex",
                "status": "canceled",
                "summary": "fixture",
                "failure_class": None,
                "recovery_action": None,
            },
        )
        payload = json.loads(json.dumps(result.to_dict()))
        rendered = render_task_state_text(result)
        self.assertIn(f"next action: {payload['next_action']['action_id']}", rendered)
        self.assertIn(f"next action disposition: {payload['next_action']['disposition']}", rendered)
        self.assertIn(f"next action display: {payload['next_action']['display']}", rendered)
        self.assertIn(f"next command: {payload['next_action']['command']}", rendered)

    def test_all_normal_task_text_puts_authority_before_diagnostics(self) -> None:
        next_action = decide_next_action(_context(worktree_dirty=True))

        def result(operation: str, legacy_payload: dict[str, object]) -> OperationResult:
            legacy_payload.update(
                recommended_actions=["LEGACY RECOMMENDATION MUST NOT RENDER"],
                recommended_commands=[
                    {
                        "command": "LEGACY COMMAND MUST NOT RENDER",
                        "argv": None,
                        "executable": False,
                        "template": True,
                        "deprecated": True,
                    }
                ],
            )
            return OperationResult(
                operation=operation,
                operation_status="blocked",
                task_status="in_progress",
                attempt_status=ATTEMPT_STATUS_IN_PROGRESS,
                disposition=next_action.disposition,
                mutation_started=False,
                mutation_completed=False,
                mutation_phase="none",
                failure_code=None,
                next_action=next_action,
                legacy_payload=legacy_payload,
            )

        show_fields = {
            "task_id": "TASK-1",
            "task_title": "Fixture",
            "active_attempt": True,
            "attempt_id": "attempt-1",
            "latest_attempt_status": ATTEMPT_STATUS_IN_PROGRESS,
            "latest_attempt_id": "attempt-1",
            "latest_attempt_summary": None,
            "branch": "task-branch",
            "target_branch": "main",
            "worktree_path": "/tmp/task",
            "branch_exists": True,
            "target_branch_exists": True,
            "branch_ahead_error": None,
            "worktree_exists": True,
            "worktree_dirty": True,
            "branch_ahead_of_target": False,
            "primary_dirty": False,
            "worktree_dirty_paths": ["changed.txt"],
            "changed_paths": ["changed.txt"],
            "user_prompt_hash": None,
            "user_prompt_source": None,
            "user_prompt_mode": None,
            "execution_prompt_hash": None,
            "execution_prompt_source": None,
            "execution_prompt_mode": None,
            "failure_class": None,
            "recovery_action": None,
        }
        rendered_cases = (
            (
                render_task_begin_text(
                    result(
                        "task.begin",
                        {
                            "task_id": "TASK-1",
                            "actor": "codex",
                            "worktree": None,
                            "error": "blocked fixture",
                        },
                    )
                ),
                "task.begin",
                "begin blocked:",
            ),
            (
                render_show_text(result("task.show", dict(show_fields)), surface="task"),
                "task.show",
                "show: TASK-1",
            ),
            (
                render_recover_text(
                    result(
                        "task.recover",
                        {
                            **show_fields,
                            "recovery_state": "active_attempt",
                            "task_runtime_status": "in_progress",
                            "stale_claim": False,
                            "released_stale_claim": False,
                            "task_claim": None,
                            "workset_claim": None,
                            "task_runtime_note": None,
                        },
                    )
                ),
                "task.recover",
                "recover: TASK-1",
            ),
            (
                render_land_text(
                    result(
                        "task.land",
                        {
                            "branch": "task-branch",
                            "target_branch": "main",
                            "attempt_id": "attempt-1",
                            "attempt_active": True,
                            "status": ATTEMPT_STATUS_BLOCKED,
                            "summary": "blocked fixture",
                            "worktree_path": "/tmp/task",
                            "changed_paths": ["changed.txt"],
                            "land_failure_disposition": "retryable",
                        },
                    ),
                    surface="task",
                ),
                "task.land",
                "land blocked:",
            ),
            (
                render_landing_reconciliation_text(
                    result(
                        "task.reconcile-landing",
                        {
                            "proof": {"target_branch": "main", "changed_paths": ["changed.txt"]},
                            "apply": False,
                            "workset_id": "workset-1",
                            "task_id": "TASK-1",
                            "attempt_id": "attempt-1",
                            "landed_commit": "a" * 40,
                        },
                    )
                ),
                "task.reconcile-landing",
                "Landing reconciliation:",
            ),
            (
                render_close_text(
                    result(
                        "task.close",
                        {
                            "task_id": "TASK-1",
                            "attempt_id": "attempt-1",
                            "status": ATTEMPT_STATUS_BLOCKED,
                            "summary": "closed fixture",
                            "branch": "task-branch",
                            "target_branch": "main",
                            "worktree_path": "/tmp/task",
                            "changed_paths": ["changed.txt"],
                        },
                    ),
                    surface="task",
                ),
                "task.close",
                "closed: TASK-1",
            ),
            (
                render_task_state_text(
                    result(
                        "task.cancel",
                        {
                            "workset_id": "workset-1",
                            "task_id": "TASK-1",
                            "status": "canceled",
                            "actor": "codex",
                        },
                    )
                ),
                "task.cancel",
                "state: workset-1/TASK-1",
            ),
            (
                render_cleanup_text(
                    result(
                        "task.cleanup",
                        {
                            "worktree_removed": False,
                            "worktree_existed": True,
                            "worktree_path": "/tmp/task",
                            "branch": "task-branch",
                            "deleted_branch": False,
                        },
                    ),
                    surface="task",
                ),
                "task.cleanup",
                "retained: /tmp/task",
            ),
        )
        for rendered, operation, diagnostic in rendered_cases:
            with self.subTest(operation=operation):
                self.assertNotIn("LEGACY RECOMMENDATION", rendered)
                self.assertNotIn("LEGACY COMMAND", rendered)
                self.assertLess(rendered.index(f"operation: {operation}"), rendered.index(diagnostic))
                self.assertLess(
                    rendered.index("next action: landing_evidence_required"),
                    rendered.index(diagnostic),
                )


class LifecycleResumeIntegrationTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile("Lifecycle Resume")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "blackdog.toml"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Add Blackdog profile"],
            check=True,
            capture_output=True,
            text=True,
        )

    def run_cli(self, *args: str, cwd: Path | None = None) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with chdir(cwd or Path.cwd()), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = blackdog_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def run_subprocess_cli(self, *args: str, cwd: Path | None = None) -> tuple[int, str, str]:
        env = dict(os.environ)
        python_path = str(REPO_ROOT / "src")
        env["PYTHONPATH"] = os.pathsep.join(
            item for item in (python_path, env.get("PYTHONPATH", "")) if item
        )
        completed = subprocess.run(
            [sys.executable, "-m", "blackdog_cli", *args],
            cwd=str(cwd or self.root),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.returncode, completed.stdout, completed.stderr

    def install_repo_runtime(self) -> None:
        exit_code, _, stderr = self.run_cli(
            "repo",
            "install",
            "--project-root",
            str(self.root),
            "--source-root",
            str(REPO_ROOT),
        )
        self.assertEqual(exit_code, 0, stderr)
        profile = load_profile(self.root)
        tracked_paths = [
            "blackdog.toml",
            "AGENTS.md",
            str(managed_skill_relative_path(profile).parent),
        ]
        subprocess.run(
            ["git", "-C", str(self.root), "add", *tracked_paths],
            check=True,
            capture_output=True,
            text=True,
        )
        if self.git_output("status", "--short"):
            subprocess.run(
                ["git", "-C", str(self.root), "commit", "-m", "Install Blackdog runtime"],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_resume_command_executes_in_the_same_task_envelope(self) -> None:
        self.install_repo_runtime()
        prompt_file = self.root / "execution prompt.md"
        prompt_file.write_text("Implement and verify the lifecycle resume fixture.\n", encoding="utf-8")
        request_file = self.root / "request.md"
        request_file.write_text("Please repair the lifecycle resume fixture.\n", encoding="utf-8")
        actor = "agent with spaces"
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            actor,
            "--execution-prompt-file",
            str(prompt_file),
            "--request-file",
            str(request_file),
            "--prompt-mode",
            "skill",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        started = json.loads(stdout)["task"]
        workset_id = started["workset_id"]
        task_id = started["task_id"]
        first_attempt = started["worktree"]["attempt_id"]
        expected_lineage = {
            "actor": actor,
            "execution_prompt_hash": started["execution_prompt_hash"],
            "execution_prompt_source": started["execution_prompt_source"],
            "execution_prompt_mode": "skill",
            "execution_prompt_replay_artifact_path": started[
                "execution_prompt_replay_artifact_path"
            ],
            "user_prompt_hash": started["user_prompt_hash"],
            "user_prompt_source": started["user_prompt_source"],
            "user_prompt_mode": "raw",
            "user_prompt_replay_artifact_path": started[
                "user_prompt_replay_artifact_path"
            ],
            "skill_provenance": started["worktree"]["setup_receipt"]["skill_provenance"],
        }
        workspace = Path(started["worktree"]["worktree_path"])
        profile = load_profile(self.root)
        workset_count = len(load_planning_state(profile.paths).worksets)
        execution_artifact = (
            profile.paths.control_dir / started["execution_prompt_replay_artifact_path"]
        ).resolve()
        request_artifact = (
            profile.paths.control_dir / started["user_prompt_replay_artifact_path"]
        ).resolve()
        self.assertTrue(execution_artifact.is_file())
        self.assertTrue(request_artifact.is_file())
        self.assertEqual(execution_artifact.read_text(encoding="utf-8"), "Implement and verify the lifecycle resume fixture.")
        self.assertEqual(request_artifact.read_text(encoding="utf-8"), "Please repair the lifecycle resume fixture.")
        self.assertEqual(execution_artifact.stat().st_mode & 0o777, 0o600)
        self.assertEqual(request_artifact.stat().st_mode & 0o777, 0o600)
        runtime_text = profile.paths.runtime_file.read_text(encoding="utf-8")
        self.assertNotIn("Implement and verify the lifecycle resume fixture.", runtime_text)
        self.assertNotIn("Please repair the lifecycle resume fixture.", runtime_text)
        events_text = profile.paths.events_file.read_text(encoding="utf-8")
        self.assertNotIn("Implement and verify the lifecycle resume fixture.", events_text)
        self.assertNotIn("Please repair the lifecycle resume fixture.", events_text)
        prompt_file.unlink()
        request_file.unlink()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "failed",
            "--summary",
            "Close the fixture before an exact-envelope resume",
            "--cleanup",
            "--json",
            cwd=workspace,
        )
        self.assertEqual(exit_code, 0, stderr)
        closure = json.loads(stdout)["closure"]
        next_action = closure["next_action"]
        self.assertEqual(next_action["action_id"], "resume_existing_task")
        self.assertEqual(next_action["kind"], "command")
        self.assertFalse(workspace.exists())
        self.assertTrue(execution_artifact.is_file())
        self.assertTrue(request_artifact.is_file())
        argv = next_action["argv"]
        self.assertEqual(shlex.split(next_action["command"]), argv)
        self.assertIn(f"--execution-prompt-file={execution_artifact}", argv)
        self.assertIn(f"--request-file={request_artifact}", argv)
        self.assertIn("--prompt-mode=skill", argv)
        self.assertIn(f"--actor={actor}", argv)
        self.assertIn(f"--expected-actor={actor}", argv)
        self.assertIn(
            f"--expected-execution-prompt-hash={expected_lineage['execution_prompt_hash']}",
            argv,
        )
        self.assertIn("--expected-execution-prompt-mode=skill", argv)
        self.assertIn(
            f"--expected-request-prompt-hash={expected_lineage['user_prompt_hash']}",
            argv,
        )
        self.assertIn("--expected-request-prompt-mode=raw", argv)

        for command, wrapper in (("show", "task_show"), ("recover", "recovery")):
            exit_code, surface_stdout, surface_stderr = self.run_cli(
                "task",
                command,
                "--project-root",
                str(self.root),
                "--workset",
                workset_id,
                "--task",
                task_id,
                "--json",
            )
            self.assertEqual(exit_code, 0, surface_stderr)
            surface = json.loads(surface_stdout)[wrapper]
            self.assertEqual(surface["resume_lineage"]["status"], "verified")
            self.assertEqual(surface["next_action"]["argv"], argv)

        exit_code, _, stderr = self.run_cli(*argv[1:], cwd=self.root)
        self.assertEqual(exit_code, 0, stderr)
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        resumed = json.loads(stdout)["task_show"]
        self.assertEqual(resumed["workset_id"], workset_id)
        self.assertEqual(resumed["task_id"], task_id)
        self.assertEqual(resumed["actor"], actor)
        self.assertNotEqual(resumed["attempt_id"], first_attempt)
        self.assertTrue(resumed["active_attempt"])
        for key, expected in expected_lineage.items():
            self.assertEqual(resumed[key], expected, key)
        self.assertEqual(
            len(load_planning_state(load_profile(self.root).paths).worksets),
            workset_count,
        )

    def test_cancel_and_reopen_preserve_actor_without_any_attempt(self) -> None:
        profile = load_profile(self.root)
        upsert_workset(
            profile,
            {
                "id": "planned-only",
                "title": "Planned only",
                "tasks": [
                    {
                        "id": "PLAN-1",
                        "title": "Preserve actor",
                        "intent": "prove no-attempt action attribution",
                    }
                ],
            },
        )
        actor = "invoking agent with spaces"
        exit_code, stdout, stderr = self.run_subprocess_cli(
            "task",
            "cancel",
            "--project-root",
            str(self.root),
            "--workset",
            "planned-only",
            "--task",
            "PLAN-1",
            "--actor",
            actor,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        canceled = json.loads(stdout)["task_state"]
        self.assertIn(f"--actor={actor}", canceled["next_action"]["argv"])
        legacy_begin = next(
            row
            for row in canceled["recommended_commands"]
            if row["command"] == 'blackdog task begin --prompt "..."'
        )
        self.assertIsNone(legacy_begin["argv"])
        self.assertFalse(legacy_begin["executable"])
        self.assertTrue(legacy_begin["template"])
        self.assertTrue(legacy_begin["deprecated"])
        self.assertNotIn("...", canceled["next_action"]["command"])

        exit_code, stdout, stderr = self.run_subprocess_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--workset",
            "planned-only",
            "--task",
            "PLAN-1",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        shown_canceled = json.loads(stdout)["task_show"]
        self.assertEqual(shown_canceled["actor"], actor)
        self.assertNotIn("--actor=codex", shown_canceled["next_action"]["argv"])

        exit_code, stdout, stderr = self.run_subprocess_cli(
            "task",
            "reopen",
            "--project-root",
            str(self.root),
            "--workset",
            "planned-only",
            "--task",
            "PLAN-1",
            "--actor",
            actor,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        reopened = json.loads(stdout)["task_state"]
        self.assertEqual(reopened["actor"], actor)
        self.assertEqual(reopened["next_action"]["action_id"], "resume_lineage_required")
        self.assertEqual(reopened["next_action"]["argv"], [])

        exit_code, stdout, stderr = self.run_subprocess_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--workset",
            "planned-only",
            "--task",
            "PLAN-1",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        shown_reopened = json.loads(stdout)["task_show"]
        self.assertEqual(shown_reopened["actor"], actor)
        self.assertEqual(shown_reopened["next_action"]["action_id"], "resume_lineage_required")

    def test_resume_revalidates_lineage_at_mutation_boundary_with_or_without_expected_flags(self) -> None:
        self.install_repo_runtime()
        execution_file = self.root / "toctou-execution.md"
        execution_text = "Exercise the resume mutation-boundary contract.\n"
        execution_file.write_text(execution_text, encoding="utf-8")
        request_file = self.root / "toctou-request.md"
        request_text = "Preserve this distinct request lineage.\n"
        request_file.write_text(request_text, encoding="utf-8")
        actor = "lineage-boundary-agent"
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            actor,
            "--execution-prompt-file",
            str(execution_file),
            "--request-file",
            str(request_file),
            "--prompt-mode",
            "skill",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        started = json.loads(stdout)["task"]
        workspace = Path(started["worktree"]["worktree_path"])
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "failed",
            "--summary",
            "Close before mutation-boundary replay",
            "--cleanup",
            "--json",
            cwd=workspace,
        )
        self.assertEqual(exit_code, 0, stderr)
        argv = json.loads(stdout)["closure"]["next_action"]["argv"]
        self.assertTrue(any(value.startswith("--expected-actor=") for value in argv))
        profile = load_profile(self.root)
        runtime_before = profile.paths.runtime_file.read_bytes()
        attempts_before = tuple(
            attempt.attempt_id
            for runtime_workset in load_runtime_state(profile.paths).worksets
            for attempt in runtime_workset.attempts
        )
        worktrees_before = self.git_output("worktree", "list", "--porcelain")
        branches_before = self.git_output("branch", "--format=%(refname)")
        execution_artifact = Path(
            next(
                value.split("=", 1)[1]
                for value in argv
                if value.startswith("--execution-prompt-file=")
            )
        )
        request_artifact = Path(
            next(
                value.split("=", 1)[1]
                for value in argv
                if value.startswith("--request-file=")
            )
        )
        execution_artifact_text = execution_artifact.read_text(encoding="utf-8")
        request_artifact_text = request_artifact.read_text(encoding="utf-8")

        def assert_rejected_without_mutation(command: list[str]) -> None:
            exit_code, stdout, stderr = self.run_cli(*command[1:], cwd=self.root)
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("prompt lineage does not match", stderr)
            self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(
                tuple(
                    attempt.attempt_id
                    for runtime_workset in load_runtime_state(profile.paths).worksets
                    for attempt in runtime_workset.attempts
                ),
                attempts_before,
            )
            self.assertEqual(self.git_output("worktree", "list", "--porcelain"), worktrees_before)
            self.assertEqual(self.git_output("branch", "--format=%(refname)"), branches_before)
            self.assertFalse(workspace.exists())

        execution_artifact.write_text("Changed after next-action emission.", encoding="utf-8")
        assert_rejected_without_mutation(argv)
        execution_artifact.write_text(execution_artifact_text, encoding="utf-8")
        request_artifact.write_text("Changed request after next-action emission.", encoding="utf-8")
        assert_rejected_without_mutation(argv)

        argv_without_expected = [
            value for value in argv if not value.startswith("--expected-")
        ]
        assert_rejected_without_mutation(argv_without_expected)

        request_artifact.write_text(request_artifact_text, encoding="utf-8")

    def test_resume_runtime_race_rejects_stale_owner_and_cleans_created_workspace(self) -> None:
        self.install_repo_runtime()
        execution_file = self.root / "atomic-resume-execution.md"
        execution_file.write_text("Exercise atomic resume ownership.\n", encoding="utf-8")
        request_file = self.root / "atomic-resume-request.md"
        request_file.write_text("Keep the original request lineage.\n", encoding="utf-8")
        original_actor = "original-resume-owner"
        begin_args = (
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            original_actor,
            "--execution-prompt-file",
            str(execution_file),
            "--request-file",
            str(request_file),
            "--prompt-mode",
            "skill",
            "--json",
        )
        exit_code, stdout, stderr = self.run_cli(*begin_args)
        self.assertEqual(exit_code, 0, stderr)
        started = json.loads(stdout)["task"]
        workset_id = started["workset_id"]
        task_id = started["task_id"]
        predecessor_id = started["worktree"]["attempt_id"]
        task_path = Path(started["worktree"]["worktree_path"])
        task_branch = started["worktree"]["branch"]
        exit_code, _, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "failed",
            "--summary",
            "Prepare the terminal predecessor for a forced race.",
            "--cleanup",
            "--json",
            cwd=task_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertFalse(task_path.exists())
        resume_args = (
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--actor",
            original_actor,
            "--execution-prompt-file",
            str(execution_file),
            "--request-file",
            str(request_file),
            "--prompt-mode",
            "skill",
            "--json",
        )

        original_execute = wtam.execute_worktree_handlers
        injected = False

        def mutate_owner_after_preflight(profile, *, worktree_path):
            nonlocal injected
            if not injected:
                injected = True
                wtam.cancel_task(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor="racing-resume-owner",
                    summary="Race after product validation.",
                )
                wtam.reopen_task(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor="racing-resume-owner",
                    summary="Reopen under a different durable owner.",
                )
            return original_execute(profile, worktree_path=worktree_path)

        with patch.object(
            wtam,
            "execute_worktree_handlers",
            side_effect=mutate_owner_after_preflight,
        ):
            exit_code, stdout, stderr = self.run_cli(*resume_args)

        self.assertTrue(injected)
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("task actor or runtime generation no longer matches", stderr)
        runtime = load_runtime_state(load_profile(self.root).paths)
        runtime_workset = next(
            row for row in runtime.worksets if row.workset_id == workset_id
        )
        self.assertEqual(
            [row.attempt_id for row in runtime_workset.attempts if row.task_id == task_id],
            [predecessor_id],
        )
        self.assertEqual(runtime_workset.task_claims, ())
        self.assertIsNone(runtime_workset.workset_claim)
        task_state = next(row for row in runtime_workset.task_states if row.task_id == task_id)
        self.assertEqual(task_state.actor, "racing-resume-owner")
        self.assertFalse(task_path.exists())
        self.assertNotIn(
            f"refs/heads/{task_branch}",
            self.git_output("branch", "--format=%(refname)").splitlines(),
        )
        successor_events = [
            event
            for event in load_events(load_profile(self.root).paths.events_file)
            if event.get("payload", {}).get("attempt_id") not in {None, predecessor_id}
        ]
        self.assertEqual(successor_events, [])

    def test_prior_attempt_actor_transitions_are_durable_across_processes(self) -> None:
        self.install_repo_runtime()
        prompt_file = self.root / "actor-transition.md"
        prompt_file.write_text("Exercise durable actor transitions.\n", encoding="utf-8")
        exit_code, stdout, stderr = self.run_subprocess_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "attempt-owner",
            "--execution-prompt-file",
            str(prompt_file),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        started = json.loads(stdout)["task"]
        workset_id = started["workset_id"]
        task_id = started["task_id"]
        workspace = Path(started["worktree"]["worktree_path"])

        for command in ("land", "close"):
            args = [
                "task",
                command,
                "--project-root",
                str(self.root),
                "--actor",
                "not-the-attempt-owner",
            ]
            if command == "land":
                args.extend(("--summary", "Reject foreign land actor"))
            else:
                args.extend(("--status", "failed", "--summary", "Reject foreign close actor"))
            exit_code, stdout, stderr = self.run_subprocess_cli(*args, cwd=workspace)
            self.assertEqual(exit_code, 1)
            self.assertEqual(stdout, "")
            self.assertIn("owned by attempt-owner", stderr)
            shown = load_runtime_state(load_profile(self.root).paths).worksets[0].attempts[-1]
            self.assertEqual(shown.status, ATTEMPT_STATUS_IN_PROGRESS)

        exit_code, _, stderr = self.run_subprocess_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "failed",
            "--summary",
            "Close as the persisted attempt owner",
            "--cleanup",
            cwd=workspace,
        )
        self.assertEqual(exit_code, 0, stderr)

        exit_code, stdout, stderr = self.run_subprocess_cli(
            "task",
            "cancel",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--actor",
            "cancel-owner",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["task_state"]["actor"], "cancel-owner")
        exit_code, stdout, stderr = self.run_subprocess_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["task_show"]["actor"], "cancel-owner")

        exit_code, stdout, stderr = self.run_subprocess_cli(
            "task",
            "reopen",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--actor",
            "reopen-owner",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        reopened = json.loads(stdout)["task_state"]
        self.assertEqual(reopened["actor"], "reopen-owner")
        self.assertIn("--actor=reopen-owner", reopened["next_action"]["argv"])
        self.assertIn("--expected-actor=reopen-owner", reopened["next_action"]["argv"])

        exit_code, stdout, stderr = self.run_subprocess_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        shown = json.loads(stdout)["task_show"]
        self.assertEqual(shown["actor"], "reopen-owner")
        self.assertEqual(shown["next_action"]["argv"], reopened["next_action"]["argv"])

    def test_cancel_and_reopen_require_explicit_actor(self) -> None:
        profile = load_profile(self.root)
        upsert_workset(
            profile,
            {
                "id": "actor-required",
                "title": "Actor required",
                "tasks": [{"id": "ACT-1", "title": "Actor", "intent": "require actor"}],
            },
        )
        base_args = (
            "--project-root",
            str(self.root),
            "--workset",
            "actor-required",
            "--task",
            "ACT-1",
        )
        exit_code, stdout, stderr = self.run_subprocess_cli("task", "cancel", *base_args)
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--actor", stderr)
        exit_code, _, stderr = self.run_subprocess_cli(
            "task", "cancel", *base_args, "--actor", "explicit-owner"
        )
        self.assertEqual(exit_code, 0, stderr)
        exit_code, stdout, stderr = self.run_subprocess_cli("task", "reopen", *base_args)
        self.assertEqual(exit_code, 2)
        self.assertEqual(stdout, "")
        self.assertIn("--actor", stderr)

    def test_cancel_and_reopen_reject_blank_actor_before_any_store_write(self) -> None:
        from blackdog.wtam import cancel_task, reopen_task

        profile = load_profile(self.root)
        upsert_workset(
            profile,
            {
                "id": "actor-content",
                "title": "Actor content",
                "tasks": [
                    {
                        "id": "ACT-1",
                        "title": "Actor content",
                        "intent": "reject blank transition owners",
                    }
                ],
            },
        )

        def store_snapshot() -> tuple[bytes | None, bytes | None]:
            runtime = (
                profile.paths.runtime_file.read_bytes()
                if profile.paths.runtime_file.exists()
                else None
            )
            events = (
                profile.paths.events_file.read_bytes()
                if profile.paths.events_file.exists()
                else None
            )
            return runtime, events

        for actor in ("", " \t "):
            with self.subTest(surface="direct", operation="cancel", actor=repr(actor)):
                before = store_snapshot()
                with self.assertRaisesRegex(BacklogError, "task cancel requires a nonempty actor"):
                    cancel_task(
                        profile,
                        workset_id="actor-content",
                        task_id="ACT-1",
                        actor=actor,
                    )
                self.assertEqual(store_snapshot(), before)
            with self.subTest(surface="subprocess", operation="cancel", actor=repr(actor)):
                before = store_snapshot()
                exit_code, stdout, stderr = self.run_subprocess_cli(
                    "task",
                    "cancel",
                    "--project-root",
                    str(self.root),
                    "--workset",
                    "actor-content",
                    "--task",
                    "ACT-1",
                    "--actor",
                    actor,
                )
                self.assertEqual(exit_code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("task cancel requires a nonempty actor", stderr)
                self.assertEqual(store_snapshot(), before)

        canceled = cancel_task(
            profile,
            workset_id="actor-content",
            task_id="ACT-1",
            actor="  cancel-owner  ",
        ).to_dict()
        self.assertEqual(canceled["actor"], "cancel-owner")
        self.assertEqual(load_events(profile.paths.events_file)[-1]["actor"], "cancel-owner")

        for actor in ("", " \t "):
            with self.subTest(surface="direct", operation="reopen", actor=repr(actor)):
                before = store_snapshot()
                with self.assertRaisesRegex(BacklogError, "task reopen requires a nonempty actor"):
                    reopen_task(
                        profile,
                        workset_id="actor-content",
                        task_id="ACT-1",
                        actor=actor,
                    )
                self.assertEqual(store_snapshot(), before)
            with self.subTest(surface="subprocess", operation="reopen", actor=repr(actor)):
                before = store_snapshot()
                exit_code, stdout, stderr = self.run_subprocess_cli(
                    "task",
                    "reopen",
                    "--project-root",
                    str(self.root),
                    "--workset",
                    "actor-content",
                    "--task",
                    "ACT-1",
                    "--actor",
                    actor,
                )
                self.assertEqual(exit_code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("task reopen requires a nonempty actor", stderr)
                self.assertEqual(store_snapshot(), before)

        reopened = reopen_task(
            profile,
            workset_id="actor-content",
            task_id="ACT-1",
            actor="  reopen-owner  ",
        ).to_dict()
        self.assertEqual(reopened["actor"], "reopen-owner")
        self.assertEqual(load_events(profile.paths.events_file)[-1]["actor"], "reopen-owner")

    def test_cancel_and_reopen_repair_every_runtime_transition_fault_via_exact_cli_retry(self) -> None:
        profile = load_profile(self.root)
        boundaries = (
            ("request_before", "task.runtime-transition.request", False, "none", False, False),
            ("decision_before", "task.runtime-transition.decision", False, "preflight", True, False),
            ("decision_after", "task.runtime-transition.decision", True, "preflight", True, False),
            ("runtime_after", None, False, "runtime_finalized", True, False),
            ("owned_before", "owned", False, "runtime_finalized", True, False),
            ("owned_after", "owned", True, "event_finalized", True, True),
        )

        def invoke(
            surface: str,
            operation: str,
            *,
            workset_id: str,
            task_id: str,
            actor: str,
            summary: str,
        ) -> tuple[int, dict[str, object], str]:
            if surface == "direct":
                result = (
                    wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor=actor,
                        summary=summary,
                        failure_class=FAILURE_CLASS_UNKNOWN,
                        recovery_action="inspect",
                        prompt_issue=True,
                    )
                    if operation == "cancel"
                    else wtam.reopen_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor=actor,
                        summary=summary,
                    )
                )
                return (0 if result.operation_status == "succeeded" else 1), result.to_dict(), ""
            args = [
                "task",
                operation,
                "--project-root",
                str(self.root),
                "--workset",
                workset_id,
                "--task",
                task_id,
                "--actor",
                actor,
                "--summary",
                summary,
            ]
            if operation == "cancel":
                args.extend(
                    [
                        "--failure-class",
                        FAILURE_CLASS_UNKNOWN,
                        "--recovery-action",
                        "inspect",
                        "--prompt-issue",
                    ]
                )
            args.append("--json")
            exit_code, stdout, stderr = self.run_cli(*args, cwd=self.root)
            return exit_code, json.loads(stdout)["task_state"] if stdout else {}, stderr

        case_index = 0
        for surface in ("direct", "cli"):
            for operation in ("cancel", "reopen"):
                for (
                    boundary,
                    event_boundary,
                    after_append,
                    expected_phase,
                    expected_started,
                    expected_completed,
                ) in boundaries:
                    case_index += 1
                    with self.subTest(
                        surface=surface,
                        operation=operation,
                        boundary=boundary,
                    ):
                        workset_id = f"transition-fault-{case_index}"
                        task_id = "STATE-1"
                        actor = f"{operation}-owner"
                        summary = f"{operation} at {boundary}"
                        upsert_workset(
                            profile,
                            {
                                "id": workset_id,
                                "title": "Runtime transition fault",
                                "tasks": [
                                    {
                                        "id": task_id,
                                        "title": "Repair runtime transition",
                                        "intent": "repair deterministic state transition",
                                    }
                                ],
                            },
                        )
                        if operation == "reopen":
                            wtam.cancel_task(
                                profile,
                                workset_id=workset_id,
                                task_id=task_id,
                                actor="setup-owner",
                                summary="prepare canceled source",
                            )

                        tripped = False
                        if boundary == "runtime_after":
                            original_mutate = backlog.mutate_runtime_state

                            def interrupted_mutate(*args, **kwargs):
                                original_after_save = kwargs.get("after_save")

                                def stop_after_runtime(_runtime_state):
                                    nonlocal tripped
                                    if not tripped:
                                        tripped = True
                                        raise OSError("injected stop after runtime save")
                                    assert original_after_save is not None
                                    return original_after_save(_runtime_state)

                                call_kwargs = dict(kwargs)
                                call_kwargs["after_save"] = stop_after_runtime
                                return original_mutate(*args, **call_kwargs)

                            fault = patch.object(
                                backlog,
                                "mutate_runtime_state",
                                side_effect=interrupted_mutate,
                            )
                        else:
                            original_append = backlog.append_event_once
                            target_type = (
                                f"task.{operation}"
                                if event_boundary == "owned"
                                else event_boundary
                            )

                            def interrupted_append(*args, **kwargs):
                                nonlocal tripped
                                if kwargs.get("event_type") == target_type and not tripped:
                                    tripped = True
                                    if after_append:
                                        original_append(*args, **kwargs)
                                    raise OSError(f"injected stop at {boundary}")
                                return original_append(*args, **kwargs)

                            fault = patch.object(
                                backlog,
                                "append_event_once",
                                side_effect=interrupted_append,
                            )

                        with fault:
                            exit_code, partial, stderr = invoke(
                                surface,
                                operation,
                                workset_id=workset_id,
                                task_id=task_id,
                                actor=actor,
                                summary=summary,
                            )
                        self.assertTrue(tripped)
                        self.assertEqual(exit_code, 1, stderr)
                        self.assertEqual(partial["operation_status"], "partial")
                        self.assertEqual(partial["mutation_phase"], expected_phase)
                        self.assertEqual(partial["mutation_started"], expected_started)
                        self.assertEqual(partial["mutation_completed"], expected_completed)
                        self.assertEqual(
                            partial["next_action"]["action_id"],
                            f"retry_task_{operation}_finalization",
                        )
                        retry_argv = list(partial["next_action"]["argv"])
                        self.assertIn(f"--workset={workset_id}", retry_argv)
                        self.assertIn(f"--task={task_id}", retry_argv)
                        self.assertIn(f"--actor={actor}", retry_argv)
                        self.assertIn(f"--summary={summary}", retry_argv)
                        request_guards = [
                            value
                            for value in retry_argv
                            if value.startswith("--transition-request=")
                        ]
                        decision_guards = [
                            value
                            for value in retry_argv
                            if value.startswith("--transition-decision=")
                        ]
                        self.assertEqual(len(request_guards), 1)
                        self.assertEqual(
                            len(decision_guards),
                            0
                            if boundary in {"request_before", "decision_before"}
                            else 1,
                        )

                        repaired_code, repaired_stdout, repaired_stderr = self.run_cli(
                            *retry_argv[1:],
                            "--json",
                            cwd=self.root,
                        )
                        self.assertEqual(repaired_code, 0, repaired_stderr)
                        repaired = json.loads(repaired_stdout)["task_state"]
                        self.assertEqual(repaired["operation_status"], "succeeded")
                        target_event_type = f"task.{operation}"
                        matching_owned = [
                            event
                            for event in load_events(profile.paths.events_file)
                            if event.get("type") == target_event_type
                            and event.get("payload", {}).get("workset_id") == workset_id
                            and event.get("payload", {}).get("task_id") == task_id
                        ]
                        matching_decisions = [
                            event
                            for event in load_events(profile.paths.events_file)
                            if event.get("type") == "task.runtime-transition.decision"
                            and event.get("payload", {}).get("workset_id") == workset_id
                            and event.get("payload", {}).get("task_id") == task_id
                            and event.get("payload", {}).get("event_type") == target_event_type
                        ]
                        self.assertEqual(len(matching_owned), 1)
                        self.assertEqual(len(matching_decisions), 1)
                        self.assertEqual(
                            matching_owned[0]["payload"]["updated_at"],
                            matching_decisions[0]["payload"]["updated_at"],
                        )

                        runtime_before = profile.paths.runtime_file.read_bytes()
                        events_before = profile.paths.events_file.read_bytes()
                        third_code, third_stdout, third_stderr = self.run_cli(
                            *retry_argv[1:],
                            "--json",
                            cwd=self.root,
                        )
                        self.assertEqual(third_code, 0, third_stderr)
                        third = json.loads(third_stdout)["task_state"]
                        self.assertFalse(third["mutation_started"])
                        self.assertEqual(third["mutation_phase"], "none")
                        self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
                        self.assertEqual(profile.paths.events_file.read_bytes(), events_before)

    def test_identity_bound_transition_retries_never_start_a_later_generation(self) -> None:
        profile = load_profile(self.root)
        original_append = backlog.append_event_once

        def install_workset(workset_id: str) -> str:
            task_id = "STATE-1"
            upsert_workset(
                profile,
                {
                    "id": workset_id,
                    "title": "Guarded retry",
                    "tasks": [
                        {
                            "id": task_id,
                            "title": "Guard generation",
                            "intent": "prevent stale transition replay",
                        }
                    ],
                },
            )
            return task_id

        def assert_cli_guard_conflict(argv: tuple[str, ...]) -> None:
            runtime_before = profile.paths.runtime_file.read_bytes()
            events_before = profile.paths.events_file.read_bytes()
            code, stdout, stderr = self.run_cli(*argv[1:], "--json", cwd=self.root)
            self.assertEqual(code, 1, stderr)
            payload = json.loads(stdout)["task_state"]
            self.assertEqual(payload["operation_status"], "blocked")
            self.assertEqual(
                payload["next_action"]["action_id"],
                "inspect_task_runtime_transition_guard_conflict",
            )
            self.assertEqual(payload["next_action"]["argv"], [])
            self.assertFalse(payload["mutation_started"])
            self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(profile.paths.events_file.read_bytes(), events_before)

        workset_id = "transition-stale-pre-request"
        task_id = install_workset(workset_id)
        tripped = False

        def stop_before_request(*args, **kwargs):
            nonlocal tripped
            if (
                kwargs.get("event_type") == "task.runtime-transition.request"
                and not tripped
            ):
                tripped = True
                raise OSError("stop before request append")
            return original_append(*args, **kwargs)

        with patch.object(backlog, "append_event_once", side_effect=stop_before_request):
            partial = wtam.cancel_task(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                actor="owner-a",
                summary="saved pre-request cancel",
            )
        self.assertTrue(tripped)
        pre_request_argv = partial.next_action.argv
        self.assertTrue(
            any(value.startswith("--transition-request=") for value in pre_request_argv)
        )
        self.assertFalse(
            any(value.startswith("--transition-decision=") for value in pre_request_argv)
        )
        wtam.cancel_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="owner-b",
            summary="intervening cancel",
        )
        wtam.reopen_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="owner-b",
            summary="intervening progress",
        )
        assert_cli_guard_conflict(pre_request_argv)

        saved_actions: dict[str, tuple[str, ...]] = {}
        workset_id = "transition-stale-owned"
        task_id = install_workset(workset_id)
        for operation in ("cancel", "reopen"):
            if operation == "reopen" and task_state_index(
                load_runtime_state(profile.paths), workset_id
            )[task_id].status != TASK_STATUS_CANCELED:
                wtam.cancel_task(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor="owner-a",
                    summary="prepare stale reopen",
                )
            tripped = False

            def stop_after_owned(*args, **kwargs):
                nonlocal tripped
                result = original_append(*args, **kwargs)
                if kwargs.get("event_type") == f"task.{operation}" and not tripped:
                    tripped = True
                    raise OSError("stop after owned event")
                return result

            with patch.object(backlog, "append_event_once", side_effect=stop_after_owned):
                partial = (
                    wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="saved cancel",
                    )
                    if operation == "cancel"
                    else wtam.reopen_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="saved reopen",
                    )
                )
            self.assertTrue(tripped)
            self.assertEqual(partial.operation_status, "partial")
            self.assertTrue(
                any(
                    value.startswith("--transition-decision=")
                    for value in partial.next_action.argv
                )
            )
            saved_actions[operation] = partial.next_action.argv
            if operation == "cancel":
                wtam.reopen_task(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor="owner-b",
                    summary="legitimate reopen",
                )
                request_id = next(
                    value.split("=", 1)[1]
                    for value in saved_actions[operation]
                    if value.startswith("--transition-request=")
                )
                decision_id = next(
                    value.split("=", 1)[1]
                    for value in saved_actions[operation]
                    if value.startswith("--transition-decision=")
                )
                direct = wtam.cancel_task(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor="owner-a",
                    summary="saved cancel",
                    transition_request_event_id=request_id,
                    transition_decision_event_id=decision_id,
                )
                self.assertEqual(direct.operation_status, "blocked")
                self.assertEqual(
                    direct.next_action.action_id,
                    "inspect_task_runtime_transition_guard_conflict",
                )
            else:
                wtam.cancel_task(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor="owner-b",
                    summary="legitimate recancel",
                )
            assert_cli_guard_conflict(saved_actions[operation])

    def test_cancel_reopen_cycles_have_distinct_identity_and_conflicting_retry_is_strict(self) -> None:
        profile = load_profile(self.root)
        workset_id = "transition-cycles"
        task_id = "STATE-1"
        upsert_workset(
            profile,
            {
                "id": workset_id,
                "title": "Transition cycles",
                "tasks": [
                    {
                        "id": task_id,
                        "title": "Cycle state",
                        "intent": "separate repeated lifecycle generations",
                    }
                ],
            },
        )
        cancel_kwargs = {
            "workset_id": workset_id,
            "task_id": task_id,
            "actor": "cycle-owner",
            "summary": "same cancel request",
        }
        wtam.cancel_task(profile, **cancel_kwargs)
        wtam.reopen_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="cycle-owner",
            summary="same reopen request",
        )
        wtam.cancel_task(profile, **cancel_kwargs)

        events = load_events(profile.paths.events_file)
        cancel_decisions = [
            event
            for event in events
            if event.get("type") == "task.runtime-transition.decision"
            and event.get("payload", {}).get("workset_id") == workset_id
            and event.get("payload", {}).get("event_type") == "task.cancel"
        ]
        cancel_events = [
            event
            for event in events
            if event.get("type") == "task.cancel"
            and event.get("payload", {}).get("workset_id") == workset_id
        ]
        self.assertEqual(len(cancel_decisions), 2)
        self.assertEqual(len(cancel_events), 2)
        self.assertEqual(len({event["event_id"] for event in cancel_decisions}), 2)
        self.assertEqual(len({event["event_id"] for event in cancel_events}), 2)
        self.assertEqual(
            len({event["payload"]["updated_at"] for event in cancel_decisions}),
            2,
        )

        before = (
            profile.paths.runtime_file.read_bytes(),
            profile.paths.events_file.read_bytes(),
        )
        with self.assertRaisesRegex(BacklogError, "conflicts with its durable request"):
            wtam.cancel_task(
                profile,
                **{**cancel_kwargs, "summary": "conflicting cancel retry"},
            )
        self.assertEqual(
            (
                profile.paths.runtime_file.read_bytes(),
                profile.paths.events_file.read_bytes(),
            ),
            before,
        )

    def test_cancel_and_reopen_decision_retry_preserves_unrelated_workset_drift(self) -> None:
        profile = load_profile(self.root)
        original_append = backlog.append_event_once
        for operation in ("cancel", "reopen"):
            with self.subTest(operation=operation):
                workset_id = f"transition-drift-{operation}"
                task_id = "STATE-A"
                other_task_id = "STATE-B"
                upsert_workset(
                    profile,
                    {
                        "id": workset_id,
                        "title": "Transition drift",
                        "tasks": [
                            {
                                "id": task_id,
                                "title": "Interrupted transition",
                                "intent": "repair only this task slice",
                            },
                            {
                                "id": other_task_id,
                                "title": "Concurrent transition",
                                "intent": "remain untouched by retry",
                            },
                        ],
                    },
                )
                if operation == "reopen":
                    wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="prepare reopen",
                    )

                tripped = False

                def stop_after_decision(*args, **kwargs):
                    nonlocal tripped
                    result = original_append(*args, **kwargs)
                    payload = kwargs.get("payload")
                    if (
                        kwargs.get("event_type")
                        == "task.runtime-transition.decision"
                        and isinstance(payload, dict)
                        and payload.get("workset_id") == workset_id
                        and payload.get("task_id") == task_id
                        and not tripped
                    ):
                        tripped = True
                        raise OSError("injected decision interruption before runtime")
                    return result

                with patch.object(
                    backlog,
                    "append_event_once",
                    side_effect=stop_after_decision,
                ):
                    partial = (
                        wtam.cancel_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="owner-a",
                            summary="transition A",
                        )
                        if operation == "cancel"
                        else wtam.reopen_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="owner-a",
                            summary="transition A",
                        )
                    )
                self.assertTrue(tripped)
                self.assertEqual(partial.operation_status, "partial")
                self.assertEqual(partial.mutation_phase, "preflight")

                wtam.cancel_task(
                    profile,
                    workset_id=workset_id,
                    task_id=other_task_id,
                    actor="owner-b",
                    summary="unrelated durable transition B",
                )
                before_retry_state = load_runtime_state(profile.paths)
                before_retry_b = task_state_index(before_retry_state, workset_id)[
                    other_task_id
                ]
                before_retry_b_events = tuple(
                    event
                    for event in load_events(profile.paths.events_file)
                    if event.get("payload", {}).get("workset_id") == workset_id
                    and event.get("payload", {}).get("task_id") == other_task_id
                )

                repaired = (
                    wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="transition A",
                    )
                    if operation == "cancel"
                    else wtam.reopen_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="transition A",
                    )
                )
                self.assertEqual(repaired.operation_status, "succeeded")
                after_retry_state = load_runtime_state(profile.paths)
                self.assertEqual(
                    task_state_index(after_retry_state, workset_id)[other_task_id],
                    before_retry_b,
                )
                self.assertEqual(
                    tuple(
                        event
                        for event in load_events(profile.paths.events_file)
                        if event.get("payload", {}).get("workset_id") == workset_id
                        and event.get("payload", {}).get("task_id") == other_task_id
                    ),
                    before_retry_b_events,
                )

    def test_cancel_and_reopen_runtime_retry_preserves_unrelated_workset_drift(self) -> None:
        profile = load_profile(self.root)
        original_mutate = backlog.mutate_runtime_state
        for operation in ("cancel", "reopen"):
            with self.subTest(operation=operation):
                workset_id = f"transition-runtime-drift-{operation}"
                task_id = "STATE-A"
                other_task_id = "STATE-B"
                upsert_workset(
                    profile,
                    {
                        "id": workset_id,
                        "title": "Runtime transition drift",
                        "tasks": [
                            {"id": task_id, "title": "Repair A", "intent": "repair A"},
                            {"id": other_task_id, "title": "Preserve B", "intent": "preserve B"},
                        ],
                    },
                )
                if operation == "reopen":
                    wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="prepare reopen",
                    )
                tripped = False

                def stop_after_runtime(*args, **kwargs):
                    original_after_save = kwargs.get("after_save")

                    def interrupted(_runtime_state):
                        nonlocal tripped
                        if not tripped:
                            tripped = True
                            raise OSError("injected after runtime before owned event")
                        assert original_after_save is not None
                        return original_after_save(_runtime_state)

                    call_kwargs = dict(kwargs)
                    call_kwargs["after_save"] = interrupted
                    return original_mutate(*args, **call_kwargs)

                with patch.object(
                    backlog,
                    "mutate_runtime_state",
                    side_effect=stop_after_runtime,
                ):
                    partial = (
                        wtam.cancel_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="owner-a",
                            summary="transition A",
                        )
                        if operation == "cancel"
                        else wtam.reopen_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="owner-a",
                            summary="transition A",
                        )
                    )
                self.assertTrue(tripped)
                self.assertEqual(partial.mutation_phase, "runtime_finalized")
                wtam.cancel_task(
                    profile,
                    workset_id=workset_id,
                    task_id=other_task_id,
                    actor="owner-b",
                    summary="unrelated transition B",
                )
                state_before = load_runtime_state(profile.paths)
                b_before = task_state_index(state_before, workset_id)[other_task_id]
                b_events_before = tuple(
                    event
                    for event in load_events(profile.paths.events_file)
                    if event.get("payload", {}).get("workset_id") == workset_id
                    and event.get("payload", {}).get("task_id") == other_task_id
                )
                repaired = (
                    wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="transition A",
                    )
                    if operation == "cancel"
                    else wtam.reopen_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="transition A",
                    )
                )
                self.assertEqual(repaired.operation_status, "succeeded")
                state_after = load_runtime_state(profile.paths)
                self.assertEqual(task_state_index(state_after, workset_id)[other_task_id], b_before)
                self.assertEqual(
                    tuple(
                        event
                        for event in load_events(profile.paths.events_file)
                        if event.get("payload", {}).get("workset_id") == workset_id
                        and event.get("payload", {}).get("task_id") == other_task_id
                    ),
                    b_events_before,
                )
                owned = [
                    event
                    for event in load_events(profile.paths.events_file)
                    if event.get("type") == f"task.{operation}"
                    and event.get("payload", {}).get("workset_id") == workset_id
                    and event.get("payload", {}).get("task_id") == task_id
                    and event.get("payload", {}).get("summary") == "transition A"
                ]
                self.assertEqual(len(owned), 1)
                runtime_before = profile.paths.runtime_file.read_bytes()
                events_before = profile.paths.events_file.read_bytes()
                third = (
                    wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="transition A",
                    )
                    if operation == "cancel"
                    else wtam.reopen_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="transition A",
                    )
                )
                self.assertFalse(third.mutation_started)
                self.assertEqual(third.mutation_phase, "none")
                self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
                self.assertEqual(profile.paths.events_file.read_bytes(), events_before)

    def test_task_transition_noop_truth_ignores_interleaved_unrelated_mutation(self) -> None:
        profile = load_profile(self.root)
        workset_id = "transition-interleaved-truth"
        upsert_workset(
            profile,
            {
                "id": workset_id,
                "title": "Interleaved truth",
                "tasks": [
                    {"id": "STATE-A", "title": "No-op A", "intent": "remain a no-op"},
                    {"id": "STATE-B", "title": "Mutate B", "intent": "mutate independently"},
                ],
            },
        )
        kwargs_a = {
            "workset_id": workset_id,
            "task_id": "STATE-A",
            "actor": "owner-a",
            "summary": "cancel A",
        }
        wtam.cancel_task(profile, **kwargs_a)
        original_set = wtam.set_task_runtime_status
        injected = False

        def interleave_b(*args, **kwargs):
            nonlocal injected
            result = original_set(*args, **kwargs)
            if kwargs.get("task_id") == "STATE-A" and not injected:
                injected = True
                backlog.set_task_runtime_status(
                    profile,
                    workset_id=workset_id,
                    task_id="STATE-B",
                    status=TASK_STATUS_CANCELED,
                    actor="owner-b",
                    summary="cancel B concurrently",
                )
            return result

        with patch.object(wtam, "set_task_runtime_status", side_effect=interleave_b):
            no_op = wtam.cancel_task(profile, **kwargs_a)
        self.assertTrue(injected)
        self.assertEqual(no_op.operation_status, "succeeded")
        self.assertFalse(no_op.mutation_started)
        self.assertFalse(no_op.mutation_completed)
        self.assertEqual(no_op.mutation_phase, "none")
        state = load_runtime_state(profile.paths)
        self.assertEqual(task_state_index(state, workset_id)["STATE-B"].status, TASK_STATUS_CANCELED)

    def test_task_transition_owned_event_conflict_never_mutates_runtime(self) -> None:
        profile = load_profile(self.root)
        original_append = backlog.append_event_once
        case_index = 0
        for operation in ("cancel", "reopen"):
            for stage in ("pre", "post"):
                case_index += 1
                with self.subTest(operation=operation, stage=stage):
                    workset_id = f"transition-owned-conflict-{case_index}"
                    task_id = "STATE-A"
                    upsert_workset(
                        profile,
                        {
                            "id": workset_id,
                            "title": "Owned conflict",
                            "tasks": [{"id": task_id, "title": "Conflict", "intent": "block conflict"}],
                        },
                    )
                    if operation == "reopen":
                        wtam.cancel_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="owner-a",
                            summary="prepare reopen",
                        )
                    tripped = False

                    def interrupt(*args, **kwargs):
                        nonlocal tripped
                        event_type = kwargs.get("event_type")
                        target = (
                            event_type == "task.runtime-transition.decision"
                            if stage == "pre"
                            else event_type == f"task.{operation}"
                        )
                        if target and not tripped:
                            tripped = True
                            if stage == "pre":
                                original_append(*args, **kwargs)
                            raise OSError("injected before owned completion")
                        return original_append(*args, **kwargs)

                    with patch.object(backlog, "append_event_once", side_effect=interrupt):
                        partial = (
                            wtam.cancel_task(
                                profile,
                                workset_id=workset_id,
                                task_id=task_id,
                                actor="owner-a",
                                summary="transition A",
                            )
                            if operation == "cancel"
                            else wtam.reopen_task(
                                profile,
                                workset_id=workset_id,
                                task_id=task_id,
                                actor="owner-a",
                                summary="transition A",
                            )
                        )
                    self.assertTrue(tripped)
                    decision = next(
                        event["payload"]
                        for event in load_events(profile.paths.events_file)
                        if event.get("type") == "task.runtime-transition.decision"
                        and event.get("payload", {}).get("workset_id") == workset_id
                        and event.get("payload", {}).get("task_id") == task_id
                        and event.get("payload", {}).get("event_type") == f"task.{operation}"
                    )
                    conflicting_payload = dict(decision["owned_event_payload"])
                    conflicting_payload["summary"] = "conflicting durable event"
                    append_event(
                        profile.paths.events_file,
                        event_id=decision["owned_event_id"],
                        event_type=f"task.{operation}",
                        actor="owner-a",
                        payload=conflicting_payload,
                    )
                    before_runtime = profile.paths.runtime_file.read_bytes()
                    before_events = profile.paths.events_file.read_bytes()
                    with self.assertRaisesRegex(BacklogError, "owned event conflicts"):
                        if operation == "cancel":
                            wtam.cancel_task(
                                profile,
                                workset_id=workset_id,
                                task_id=task_id,
                                actor="owner-a",
                                summary="transition A",
                            )
                        else:
                            wtam.reopen_task(
                                profile,
                                workset_id=workset_id,
                                task_id=task_id,
                                actor="owner-a",
                                summary="transition A",
                            )
                    self.assertEqual(profile.paths.runtime_file.read_bytes(), before_runtime)
                    self.assertEqual(profile.paths.events_file.read_bytes(), before_events)

    def test_pending_task_transition_is_discoverable_and_gates_other_mutators(self) -> None:
        profile = load_profile(self.root)
        original_append = backlog.append_event_once
        original_mutate = backlog.mutate_runtime_state
        case_index = 0
        for operation in ("cancel", "reopen"):
            for stage in ("request", "decision", "runtime"):
                case_index += 1
                with self.subTest(operation=operation, stage=stage):
                    workset_id = f"pending-transition-{case_index}"
                    task_id = "STATE-A"
                    upsert_workset(
                        profile,
                        {
                            "id": workset_id,
                            "title": "Pending transition",
                            "tasks": [{"id": task_id, "title": "Pending", "intent": "finish pending"}],
                        },
                    )
                    if operation == "reopen":
                        wtam.cancel_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="owner-a",
                            summary="prepare reopen",
                        )
                    tripped = False
                    if stage == "runtime":
                        def stop_after_runtime(*args, **kwargs):
                            original_after = kwargs.get("after_save")

                            def interrupted(_state):
                                nonlocal tripped
                                if not tripped:
                                    tripped = True
                                    raise OSError("injected runtime interruption")
                                assert original_after is not None
                                return original_after(_state)

                            call_kwargs = dict(kwargs)
                            call_kwargs["after_save"] = interrupted
                            return original_mutate(*args, **call_kwargs)

                        fault = patch.object(
                            backlog,
                            "mutate_runtime_state",
                            side_effect=stop_after_runtime,
                        )
                    else:
                        target_type = f"task.runtime-transition.{stage}"

                        def stop_after_ledger(*args, **kwargs):
                            nonlocal tripped
                            result = original_append(*args, **kwargs)
                            if kwargs.get("event_type") == target_type and not tripped:
                                tripped = True
                                raise OSError(f"injected {stage} interruption")
                            return result

                        fault = patch.object(
                            backlog,
                            "append_event_once",
                            side_effect=stop_after_ledger,
                        )
                    with fault:
                        partial = (
                            wtam.cancel_task(
                                profile,
                                workset_id=workset_id,
                                task_id=task_id,
                                actor="owner-a",
                                summary="transition A",
                            )
                            if operation == "cancel"
                            else wtam.reopen_task(
                                profile,
                                workset_id=workset_id,
                                task_id=task_id,
                                actor="owner-a",
                                summary="transition A",
                            )
                        )
                    self.assertTrue(tripped)
                    self.assertEqual(partial.operation_status, "partial")

                    shown = wtam.show_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                    )
                    recovered = wtam.recover_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                    )
                    expected_action = f"retry_task_{operation}_finalization"
                    self.assertEqual(shown.next_action.action_id, expected_action)
                    self.assertEqual(recovered.next_action.action_id, expected_action)
                    self.assertEqual(shown.next_action.argv, recovered.next_action.argv)

                    before_runtime = profile.paths.runtime_file.read_bytes()
                    before_events = profile.paths.events_file.read_bytes()
                    blocked = (
                        wtam.reopen_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="other-owner",
                            summary="cross pending transition",
                        )
                        if operation == "cancel"
                        else wtam.cancel_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="other-owner",
                            summary="cross pending transition",
                        )
                    )
                    self.assertEqual(blocked.operation_status, "blocked")
                    self.assertEqual(blocked.next_action.action_id, expected_action)
                    begin = wtam.begin_task_worktree(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="begin-owner",
                        prompt="This prompt must not cross a pending transition.",
                        cwd=self.root,
                    )
                    close = wtam.close_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        status="blocked",
                        summary="must remain blocked",
                    )
                    land = wtam.land_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="must remain blocked",
                    )
                    stale_release = wtam.recover_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        release_stale_claim=True,
                        status="blocked",
                        summary="must remain blocked",
                    )
                    for result in (begin, close, land, stale_release):
                        self.assertEqual(result.operation_status, "blocked")
                        self.assertEqual(result.next_action.action_id, expected_action)
                        self.assertFalse(result.mutation_started)
                    self.assertEqual(profile.paths.runtime_file.read_bytes(), before_runtime)
                    self.assertEqual(profile.paths.events_file.read_bytes(), before_events)

                    retry_argv = shown.next_action.argv
                    retry_code, retry_stdout, retry_stderr = self.run_cli(
                        *retry_argv[1:],
                        "--json",
                        cwd=self.root,
                    )
                    self.assertEqual(retry_code, 0, retry_stderr)
                    repaired = json.loads(retry_stdout)["task_state"]
                    self.assertEqual(repaired["operation_status"], "succeeded")
                    self.assertIsNone(
                        backlog.pending_task_runtime_transition(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                        )
                    )

    def test_transition_normalization_and_completed_cycle_progression(self) -> None:
        profile = load_profile(self.root)
        workset_id = "transition-normalization"
        task_id = "STATE-A"
        upsert_workset(
            profile,
            {
                "id": workset_id,
                "title": "Normalize transitions",
                "tasks": [{"id": task_id, "title": "Normalize", "intent": "normalize retry"}],
            },
        )
        first = backlog.set_task_runtime_status(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            status=TASK_STATUS_CANCELED,
            actor="  owner-a  ",
            summary="   ",
            failure_class=" unknown ",
            return_transition_result=True,
        )
        second = backlog.set_task_runtime_status(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            status=TASK_STATUS_CANCELED,
            actor="owner-a",
            summary=None,
            failure_class=FAILURE_CLASS_UNKNOWN,
            return_transition_result=True,
        )
        self.assertTrue(first.runtime_changed)
        self.assertFalse(second.runtime_changed)
        self.assertFalse(second.events_changed)
        reopened = wtam.reopen_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="owner-a",
            summary="   ",
        )
        self.assertEqual(reopened.operation_status, "succeeded")
        reopen_noop = wtam.reopen_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="owner-a",
            summary=None,
        )
        self.assertFalse(reopen_noop.mutation_started)
        self.assertIsNone(
            backlog.pending_task_runtime_transition(
                profile,
                workset_id=workset_id,
                task_id=task_id,
            )
        )
        started = backlog.start_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="owner-a",
            prompt_receipt=wtam.create_prompt_receipt(
                "Completed transitions must permit progress.",
                source="unit-test",
            ),
        )
        finished = backlog.finish_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=started.attempt_id,
            actor="owner-a",
            status="success",
            summary="progressed after completed cycle",
        )
        self.assertEqual(finished.status, "success")

    def test_transition_ledger_tamper_blocks_before_runtime_mutation(self) -> None:
        profile = load_profile(self.root)
        for tamper in ("actor", "failure_class", "previous_status"):
            with self.subTest(tamper=tamper):
                workset_id = f"transition-request-tamper-{tamper}"
                task_id = "STATE-A"
                upsert_workset(
                    profile,
                    {
                        "id": workset_id,
                        "title": "Request tamper",
                        "tasks": [{"id": task_id, "title": "Tamper", "intent": "reject tamper"}],
                    },
                )
                runtime = load_runtime_state(profile.paths)
                identity = backlog._task_runtime_transition_identity(
                    runtime,
                    workset_id=workset_id,
                    task_id=task_id,
                )
                payload = backlog._task_runtime_transition_request_payload(
                    workset_id=workset_id,
                    task_id=task_id,
                    actor="owner-a",
                    status=TASK_STATUS_CANCELED,
                    summary="tamper test",
                    previous_status="planned",
                    failure_class=FAILURE_CLASS_UNKNOWN,
                    recovery_action=None,
                    prompt_issue=False,
                    operator_issue=False,
                )
                event_id = backlog._task_runtime_transition_request_event_id(
                    workset_id=workset_id,
                    task_id=task_id,
                    expected_pre_runtime_identity=identity,
                    request_semantics_hash=backlog._canonical_payload_hash(payload),
                )
                if tamper == "actor":
                    payload["actor"] = " owner-a "
                    event_actor = " owner-a "
                elif tamper == "previous_status":
                    payload["previous_status"] = "done"
                    event_actor = "owner-a"
                else:
                    payload["failure_class"] = None
                    event_actor = "owner-a"
                append_event(
                    profile.paths.events_file,
                    event_id=event_id,
                    event_type="task.runtime-transition.request",
                    actor=event_actor,
                    payload=payload,
                )
                before_runtime = profile.paths.runtime_file.read_bytes()
                before_events = profile.paths.events_file.read_bytes()
                with self.assertRaisesRegex(
                    BacklogError,
                    "request is not canonical|missing failure class|invalid source status",
                ):
                    backlog.set_task_runtime_status(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        status=TASK_STATUS_CANCELED,
                        actor="owner-a",
                        summary="tamper test",
                    )
                self.assertEqual(profile.paths.runtime_file.read_bytes(), before_runtime)
                self.assertEqual(profile.paths.events_file.read_bytes(), before_events)

        original_append = backlog.append_event_once
        for tamper in ("owned_event_id", "event_type", "target_record", "owned_event_payload"):
            with self.subTest(tamper=tamper):
                workset_id = f"transition-decision-tamper-{tamper}"
                task_id = "STATE-A"
                upsert_workset(
                    profile,
                    {
                        "id": workset_id,
                        "title": "Decision tamper",
                        "tasks": [
                            {
                                "id": task_id,
                                "title": "Tamper",
                                "intent": "reject coordinated tamper",
                            }
                        ],
                    },
                )
                tripped = False

                def stop_after_decision(*args, **kwargs):
                    nonlocal tripped
                    result = original_append(*args, **kwargs)
                    if (
                        kwargs.get("event_type") == "task.runtime-transition.decision"
                        and kwargs.get("payload", {}).get("workset_id") == workset_id
                        and not tripped
                    ):
                        tripped = True
                        raise OSError("stop before runtime")
                    return result

                with patch.object(backlog, "append_event_once", side_effect=stop_after_decision):
                    wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="canonical summary",
                    )
                rows = list(load_events(profile.paths.events_file))
                for row in rows:
                    if (
                        row.get("type") == "task.runtime-transition.decision"
                        and row.get("payload", {}).get("workset_id") == workset_id
                    ):
                        if tamper == "owned_event_id":
                            row["payload"]["owned_event_id"] = "0" * 64
                        elif tamper == "event_type":
                            row["payload"]["event_type"] = "task.reopen"
                        elif tamper == "target_record":
                            row["payload"]["target_record"]["note"] = "coordinated tamper"
                        else:
                            row["payload"]["owned_event_payload"]["summary"] = (
                                "coordinated tamper"
                            )
                profile.paths.events_file.write_text(
                    "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                    encoding="utf-8",
                )
                before_runtime = profile.paths.runtime_file.read_bytes()
                before_events = profile.paths.events_file.read_bytes()
                with self.assertRaisesRegex(
                    BacklogError,
                    "decision conflicts with its durable request",
                ):
                    wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="canonical summary",
                    )
                self.assertEqual(profile.paths.runtime_file.read_bytes(), before_runtime)
                self.assertEqual(profile.paths.events_file.read_bytes(), before_events)

    def test_pending_transition_validates_all_generations_and_decision_uniqueness(self) -> None:
        profile = load_profile(self.root)
        original_append = backlog.append_event_once

        def install(workset_id: str) -> str:
            task_id = "STATE-A"
            upsert_workset(
                profile,
                {
                    "id": workset_id,
                    "title": "Ledger generations",
                    "tasks": [
                        {
                            "id": task_id,
                            "title": "Validate ledger",
                            "intent": "reject hidden incomplete generations",
                        }
                    ],
                },
            )
            return task_id

        hidden_workset = "transition-hidden-generation"
        task_id = install(hidden_workset)
        tripped = False

        def stop_after_request(*args, **kwargs):
            nonlocal tripped
            result = original_append(*args, **kwargs)
            if (
                kwargs.get("event_type") == "task.runtime-transition.request"
                and kwargs.get("payload", {}).get("workset_id") == hidden_workset
                and not tripped
            ):
                tripped = True
                raise OSError("stop after old request")
            return result

        with patch.object(backlog, "append_event_once", side_effect=stop_after_request):
            wtam.cancel_task(
                profile,
                workset_id=hidden_workset,
                task_id=task_id,
                actor="old-owner",
                summary="old incomplete request",
            )
        self.assertTrue(tripped)
        rows = list(load_events(profile.paths.events_file))
        old_request = next(
            row
            for row in rows
            if row.get("type") == "task.runtime-transition.request"
            and row.get("payload", {}).get("workset_id") == hidden_workset
        )
        rows.remove(old_request)
        profile.paths.events_file.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        wtam.cancel_task(
            profile,
            workset_id=hidden_workset,
            task_id=task_id,
            actor="new-owner",
            summary="later completed request",
        )
        rows = list(load_events(profile.paths.events_file))
        later_request_index = next(
            index
            for index, row in enumerate(rows)
            if row.get("type") == "task.runtime-transition.request"
            and row.get("payload", {}).get("workset_id") == hidden_workset
        )
        rows.insert(later_request_index, old_request)
        profile.paths.events_file.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        pending = backlog.pending_task_runtime_transition(
            profile,
            workset_id=hidden_workset,
            task_id=task_id,
        )
        self.assertEqual(pending["stage"], "ledger_conflict")
        shown = wtam.show_task(
            profile,
            workset_id=hidden_workset,
            task_id=task_id,
        )
        self.assertEqual(
            shown.next_action.action_id,
            "inspect_task_runtime_transition_ledger_conflict",
        )
        self.assertEqual(shown.next_action.argv, ())

        duplicate_workset = "transition-duplicate-decision"
        task_id = install(duplicate_workset)
        tripped = False

        def stop_after_decision(*args, **kwargs):
            nonlocal tripped
            result = original_append(*args, **kwargs)
            if (
                kwargs.get("event_type") == "task.runtime-transition.decision"
                and kwargs.get("payload", {}).get("workset_id") == duplicate_workset
                and not tripped
            ):
                tripped = True
                raise OSError("stop after first decision")
            return result

        with patch.object(backlog, "append_event_once", side_effect=stop_after_decision):
            wtam.cancel_task(
                profile,
                workset_id=duplicate_workset,
                task_id=task_id,
                actor="owner-a",
                summary="duplicate decision",
            )
        self.assertTrue(tripped)
        decision = next(
            row
            for row in load_events(profile.paths.events_file)
            if row.get("type") == "task.runtime-transition.decision"
            and row.get("payload", {}).get("workset_id") == duplicate_workset
        )
        duplicate_payload = json.loads(json.dumps(decision["payload"]))
        duplicate_payload["pre_runtime_workset_hash"] = "0" * 64
        duplicate_id = backlog._task_runtime_transition_decision_event_id(
            request_event_id=duplicate_payload["request_event_id"],
            pre_runtime_workset_hash=duplicate_payload["pre_runtime_workset_hash"],
        )
        duplicate_payload["owned_event_id"] = (
            backlog._task_runtime_transition_owned_event_id(
                decision_event_id=duplicate_id
            )
        )
        duplicate_payload["owned_event_payload"][
            "transition_decision_event_id"
        ] = duplicate_id
        append_event(
            profile.paths.events_file,
            event_id=duplicate_id,
            event_type="task.runtime-transition.decision",
            actor="owner-a",
            payload=duplicate_payload,
        )
        pending = backlog.pending_task_runtime_transition(
            profile,
            workset_id=duplicate_workset,
            task_id=task_id,
        )
        self.assertEqual(pending["stage"], "ledger_conflict")
        before_runtime = profile.paths.runtime_file.read_bytes()
        before_events = profile.paths.events_file.read_bytes()
        blocked = wtam.cancel_task(
            profile,
            workset_id=duplicate_workset,
            task_id=task_id,
            actor="owner-a",
            summary="duplicate decision",
        )
        self.assertEqual(blocked.operation_status, "blocked")
        self.assertEqual(
            blocked.next_action.action_id,
            "inspect_task_runtime_transition_ledger_conflict",
        )
        self.assertEqual(profile.paths.runtime_file.read_bytes(), before_runtime)
        self.assertEqual(profile.paths.events_file.read_bytes(), before_events)

    def test_workset_upsert_cannot_overwrite_or_prune_a_pending_transition_target(self) -> None:
        profile = load_profile(self.root)
        workset_id = "transition-upsert-guard"
        task_id = "STATE-A"
        keep_id = "STATE-B"
        base_payload = {
            "id": workset_id,
            "title": "Guard pending target",
            "tasks": [
                {
                    "id": task_id,
                    "title": "Pending target",
                    "intent": "preserve transition state and attempt history",
                },
                {
                    "id": keep_id,
                    "title": "Other task",
                    "intent": "remain in the workset",
                },
            ],
        }
        upsert_workset(profile, base_payload)
        attempt = backlog.start_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="owner-a",
            prompt_receipt=wtam.create_prompt_receipt(
                "Create terminal attempt history before cancel.",
                source="unit-test",
            ),
        )
        backlog.finish_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
            actor="owner-a",
            status=ATTEMPT_STATUS_FAILED,
            summary="terminal predecessor",
        )
        original_append = backlog.append_event_once
        tripped = False

        def stop_after_decision(*args, **kwargs):
            nonlocal tripped
            result = original_append(*args, **kwargs)
            if (
                kwargs.get("event_type") == "task.runtime-transition.decision"
                and kwargs.get("payload", {}).get("workset_id") == workset_id
                and not tripped
            ):
                tripped = True
                raise OSError("reserve pending cancel")
            return result

        with patch.object(backlog, "append_event_once", side_effect=stop_after_decision):
            wtam.cancel_task(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                actor="owner-a",
                summary="pending cancel",
            )
        self.assertTrue(tripped)
        planning_before = profile.paths.planning_file.read_bytes()
        runtime_before = profile.paths.runtime_file.read_bytes()
        events_before = profile.paths.events_file.read_bytes()
        mutations = (
            {
                **base_payload,
                "task_states": [{"task_id": task_id, "status": "planned"}],
            },
            {
                **base_payload,
                "tasks": [base_payload["tasks"][1]],
            },
        )
        for index, payload in enumerate(mutations):
            with self.subTest(mutation=index):
                with self.assertRaisesRegex(
                    BacklogError,
                    "cannot overwrite or prune task",
                ):
                    upsert_workset(profile, payload)
                self.assertEqual(
                    profile.paths.planning_file.read_bytes(),
                    planning_before,
                )
                self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
                self.assertEqual(profile.paths.events_file.read_bytes(), events_before)

    def test_concurrent_begin_cannot_cross_cancel_decision_reservation(self) -> None:
        profile = load_profile(self.root)
        workset_id = "transition-begin-race"
        task_id = "STATE-A"
        upsert_workset(
            profile,
            {
                "id": workset_id,
                "title": "Begin race",
                "tasks": [{"id": task_id, "title": "Race", "intent": "serialize mutation"}],
            },
        )
        original_append = backlog.append_event_once
        decision_ready = threading.Event()
        release_cancel = threading.Event()
        results: dict[str, object] = {}

        def hold_after_decision(*args, **kwargs):
            result = original_append(*args, **kwargs)
            if (
                kwargs.get("event_type") == "task.runtime-transition.decision"
                and kwargs.get("payload", {}).get("workset_id") == workset_id
                and not decision_ready.is_set()
            ):
                decision_ready.set()
                self.assertTrue(release_cancel.wait(timeout=5))
                raise OSError("stop cancel after decision")
            return result

        def cancel_worker():
            results["cancel"] = wtam.cancel_task(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                actor="owner-a",
                summary="reserve cancel",
            )

        def begin_worker():
            try:
                results["begin"] = backlog.start_task(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor="owner-b",
                    prompt_receipt=wtam.create_prompt_receipt(
                        "Must not cross the cancel decision.",
                        source="unit-test",
                    ),
                )
            except Exception as exc:
                results["begin_error"] = exc

        with patch.object(backlog, "append_event_once", side_effect=hold_after_decision):
            cancel_thread = threading.Thread(target=cancel_worker)
            cancel_thread.start()
            self.assertTrue(decision_ready.wait(timeout=5))
            begin_thread = threading.Thread(target=begin_worker)
            begin_thread.start()
            release_cancel.set()
            cancel_thread.join(timeout=5)
            begin_thread.join(timeout=5)
        self.assertFalse(cancel_thread.is_alive())
        self.assertFalse(begin_thread.is_alive())
        self.assertEqual(results["cancel"].operation_status, "partial")
        self.assertIsInstance(results.get("begin_error"), BacklogError)
        self.assertIn("incomplete task cancel transition", str(results["begin_error"]))
        runtime = load_runtime_state(profile.paths)
        runtime_workset = next(row for row in runtime.worksets if row.workset_id == workset_id)
        self.assertEqual(runtime_workset.attempts, ())
        self.assertEqual(runtime_workset.task_claims, ())
        repaired = wtam.cancel_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="owner-a",
            summary="reserve cancel",
        )
        self.assertEqual(repaired.operation_status, "succeeded")

    def test_runtime_conflict_is_commandless_on_show_recover_and_transition_surfaces(self) -> None:
        profile = load_profile(self.root)
        original_append = backlog.append_event_once
        for operation in ("cancel", "reopen"):
            with self.subTest(operation=operation):
                workset_id = f"transition-runtime-conflict-{operation}"
                task_id = "STATE-A"
                upsert_workset(
                    profile,
                    {
                        "id": workset_id,
                        "title": "Runtime conflict",
                        "tasks": [{"id": task_id, "title": "Conflict", "intent": "inspect conflict"}],
                    },
                )
                if operation == "reopen":
                    wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="prepare reopen",
                    )
                tripped = False

                def stop_after_decision(*args, **kwargs):
                    nonlocal tripped
                    result = original_append(*args, **kwargs)
                    if kwargs.get("event_type") == "task.runtime-transition.decision" and not tripped:
                        tripped = True
                        raise OSError("stop before runtime")
                    return result

                with patch.object(backlog, "append_event_once", side_effect=stop_after_decision):
                    if operation == "cancel":
                        wtam.cancel_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="owner-a",
                            summary="transition A",
                        )
                    else:
                        wtam.reopen_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="owner-a",
                            summary="transition A",
                        )
                runtime = load_runtime_state(profile.paths)
                conflicted = merge_workset_runtime(
                    runtime,
                    workset_id=workset_id,
                    task_ids={task_id},
                    incoming_records=(
                        TaskRuntimeRecord(
                            task_id=task_id,
                            status=TASK_STATUS_BLOCKED,
                            updated_at=now_iso(),
                            actor="foreign-owner",
                            note="foreign runtime mutation",
                        ),
                    ),
                )
                save_runtime_state(profile.paths, conflicted)
                before_runtime = profile.paths.runtime_file.read_bytes()
                before_events = profile.paths.events_file.read_bytes()
                shown = wtam.show_task(profile, workset_id=workset_id, task_id=task_id)
                recovered = wtam.recover_task(profile, workset_id=workset_id, task_id=task_id)
                exact = (
                    wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="transition A",
                    )
                    if operation == "cancel"
                    else wtam.reopen_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="transition A",
                    )
                )
                for result in (shown, recovered, exact):
                    self.assertEqual(
                        result.next_action.action_id,
                        "inspect_task_runtime_transition_conflict",
                    )
                    self.assertEqual(result.next_action.kind, "blocked")
                    self.assertEqual(result.next_action.argv, ())
                    self.assertFalse(result.mutation_started)
                self.assertEqual(exact.operation_status, "blocked")
                self.assertEqual(profile.paths.runtime_file.read_bytes(), before_runtime)
                self.assertEqual(profile.paths.events_file.read_bytes(), before_events)

    def test_canonicalized_direct_partial_emits_parseable_exact_retry_argv(self) -> None:
        profile = load_profile(self.root)
        original_append = backlog.append_event_once
        for operation in ("cancel", "reopen"):
            with self.subTest(operation=operation):
                workset_id = f"transition-canonical-argv-{operation}"
                task_id = "STATE-A"
                upsert_workset(
                    profile,
                    {
                        "id": workset_id,
                        "title": "Canonical argv",
                        "tasks": [{"id": task_id, "title": "Canonical", "intent": "emit canonical argv"}],
                    },
                )
                if operation == "reopen":
                    wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="owner-a",
                        summary="prepare reopen",
                    )
                tripped = False

                def stop_after_decision(*args, **kwargs):
                    nonlocal tripped
                    result = original_append(*args, **kwargs)
                    if kwargs.get("event_type") == "task.runtime-transition.decision" and not tripped:
                        tripped = True
                        raise OSError("stop for canonical argv")
                    return result

                with patch.object(backlog, "append_event_once", side_effect=stop_after_decision):
                    partial = (
                        wtam.cancel_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="  owner-a  ",
                            summary="   ",
                            failure_class=" unknown ",
                            recovery_action=" inspect ",
                        )
                        if operation == "cancel"
                        else wtam.reopen_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="  owner-a  ",
                            summary="   ",
                        )
                    )
                argv = partial.next_action.argv
                self.assertIn("--actor=owner-a", argv)
                self.assertFalse(any(value.startswith("--summary=") for value in argv))
                if operation == "cancel":
                    self.assertIn("--failure-class=unknown", argv)
                    self.assertIn("--recovery-action=inspect", argv)
                code, stdout, stderr = self.run_cli(*argv[1:], "--json", cwd=self.root)
                self.assertEqual(code, 0, stderr)
                self.assertEqual(json.loads(stdout)["task_state"]["operation_status"], "succeeded")

    def test_resume_blocks_missing_and_tampered_prompt_artifacts(self) -> None:
        self.install_repo_runtime()
        cases = (
            ("tampered", "execution_prompt_artifact_hash_mismatch"),
            ("missing", "execution_prompt_artifact_missing"),
        )
        for label, expected_reason in cases:
            with self.subTest(label=label):
                prompt_file = self.root / f"{label}-execution.md"
                prompt_file.write_text(f"Original lineage fixture {label}.\n", encoding="utf-8")
                begin_args = [
                    "task",
                    "begin",
                    "--project-root",
                    str(self.root),
                    "--actor",
                    "lineage-agent",
                    "--execution-prompt-file",
                    str(prompt_file),
                ]
                begin_args.append("--json")
                exit_code, stdout, stderr = self.run_cli(*begin_args)
                self.assertEqual(exit_code, 0, stderr)
                started = json.loads(stdout)["task"]
                workspace = Path(started["worktree"]["worktree_path"])

                exit_code, stdout, stderr = self.run_cli(
                    "task",
                    "close",
                    "--project-root",
                    str(self.root),
                    "--status",
                    "failed",
                    "--summary",
                    f"Close {label} lineage fixture",
                    "--cleanup",
                    "--json",
                    cwd=workspace,
                )
                self.assertEqual(exit_code, 0, stderr)
                artifact = (
                    load_profile(self.root).paths.control_dir
                    / started["execution_prompt_replay_artifact_path"]
                )
                if label == "tampered":
                    artifact.write_text("Tampered replay artifact.", encoding="utf-8")
                else:
                    artifact.unlink()

                exit_code, stdout, stderr = self.run_cli(
                    "task",
                    "show",
                    "--project-root",
                    str(self.root),
                    "--workset",
                    started["workset_id"],
                    "--task",
                    started["task_id"],
                    "--json",
                )
                self.assertEqual(exit_code, 0, stderr)
                shown = json.loads(stdout)["task_show"]
                self.assertEqual(shown["resume_lineage"]["issue_code"], expected_reason)
                self.assertEqual(shown["next_action"]["action_id"], "resume_lineage_required")
                self.assertEqual(shown["next_action"]["kind"], "blocked")
                self.assertEqual(shown["next_action"]["argv"], [])
                self.assertIsNone(shown["next_action"]["command"])

    def test_partial_close_and_cleanup_noop_report_mutation_truthfully(self) -> None:
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "truth-agent",
            "--execution-prompt",
            "Exercise partial close and cleanup mutation reporting.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        started = json.loads(stdout)["task"]
        workset_id = started["workset_id"]
        task_id = started["task_id"]
        workspace = Path(started["worktree"]["worktree_path"])
        dirty_file = workspace / "retained.txt"
        dirty_file.write_text("retained work\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "blocked",
            "--summary",
            "Retain dirty work for operator review",
            "--cleanup",
            "--json",
            cwd=workspace,
        )
        self.assertEqual(exit_code, 1, stderr)
        closed = json.loads(stdout)["closure"]
        self.assertEqual(closed["operation_status"], "partial")
        self.assertTrue(closed["mutation_started"])
        self.assertFalse(closed["mutation_completed"])
        self.assertEqual(
            closed["mutation_phase"],
            "runtime_finalized_cleanup_pending",
        )
        self.assertFalse(closed["cleanup_performed"])
        self.assertIn("dirty", closed["cleanup_reason"])
        self.assertTrue(closed["cleanup"]["retained"])
        self.assertEqual(closed["cleanup"]["proof"], "dirty")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--json",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("uncommitted changes", stderr)

        dirty_file.unlink()
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        cleaned = json.loads(stdout)["cleanup"]
        self.assertTrue(cleaned["mutation_started"])
        self.assertTrue(cleaned["mutation_completed"])
        self.assertEqual(
            cleaned["mutation_phase"],
            "git_and_filesystem_and_event_finalized",
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        noop = json.loads(stdout)["cleanup"]
        self.assertFalse(noop["mutation_started"])
        self.assertFalse(noop["mutation_completed"])
        self.assertEqual(noop["mutation_phase"], "none")

    def test_cleanup_branch_failure_after_workspace_removal_is_retryable_and_idempotent(self) -> None:
        import blackdog.wtam as wtam_module

        self.install_repo_runtime()
        prompt_file = self.root / "cleanup-retry.md"
        prompt_file.write_text("Exercise cleanup post-mutation retry.\n", encoding="utf-8")
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "cleanup-agent",
            "--execution-prompt-file",
            str(prompt_file),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        started = json.loads(stdout)["task"]
        workspace = Path(started["worktree"]["worktree_path"])
        branch = started["worktree"]["branch"]
        exit_code, _, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "failed",
            "--summary",
            "Close before cleanup retry",
            "--json",
            cwd=workspace,
        )
        self.assertEqual(exit_code, 0, stderr)

        real_run_git_no_check = wtam_module._run_git_no_check

        def fail_branch_delete_once(repo_root: Path, *args: str):
            if len(args) >= 3 and args[0] == "branch" and args[1] in {"-d", "-D"}:
                return subprocess.CompletedProcess(
                    ["git", *args],
                    2,
                    stdout="",
                    stderr="simulated branch deletion failure",
                )
            return real_run_git_no_check(repo_root, *args)

        with patch("blackdog.wtam._run_git_no_check", side_effect=fail_branch_delete_once):
            exit_code, stdout, stderr = self.run_cli(
                "task",
                "cleanup",
                "--project-root",
                str(self.root),
                "--workset",
                started["workset_id"],
                "--task",
                started["task_id"],
                "--json",
            )
        self.assertEqual(exit_code, 1, stderr)
        partial = json.loads(stdout)["cleanup"]
        self.assertEqual(partial["operation_status"], "partial")
        self.assertTrue(partial["mutation_started"])
        self.assertFalse(partial["mutation_completed"])
        self.assertEqual(partial["mutation_phase"], "worktree_removed_branch_cleanup_pending")
        self.assertTrue(partial["worktree_removed"])
        self.assertFalse(partial["deleted_branch"])
        self.assertFalse(workspace.exists())
        self.assertEqual(
            self.git_output("show-ref", "--hash", f"refs/heads/{branch}"),
            self.git_output("rev-parse", branch),
        )
        self.assertEqual(partial["next_action"]["action_id"], "cleanup_terminal_branch")

        exit_code, stdout, stderr = self.run_cli(*partial["next_action"]["argv"][1:])
        self.assertEqual(exit_code, 0, stderr)
        retried = json.loads(stdout)["cleanup"] if stdout.lstrip().startswith("{") else None
        if retried is not None:
            self.assertTrue(retried["deleted_branch"])
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.root), "show-ref", "--hash", f"refs/heads/{branch}"],
                check=False,
                capture_output=True,
                text=True,
            ).returncode,
            1,
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--workset",
            started["workset_id"],
            "--task",
            started["task_id"],
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("already clean", stdout)
        self.assertNotIn(" removed:", stdout)

    def test_cleanup_event_faults_converge_before_or_after_append(self) -> None:
        import blackdog.wtam as wtam_module

        self.install_repo_runtime()
        profile = load_profile(self.root)
        real_append_event_once = wtam_module.append_event_once
        for fault_point in ("before_append", "after_append"):
            with self.subTest(fault_point=fault_point):
                prompt_file = self.root / f"cleanup-event-{fault_point}.md"
                prompt_file.write_text(
                    f"Exercise deterministic cleanup event recovery {fault_point}.\n",
                    encoding="utf-8",
                )
                exit_code, stdout, stderr = self.run_cli(
                    "task",
                    "begin",
                    "--project-root",
                    str(self.root),
                    "--actor",
                    "cleanup-event-agent",
                    "--execution-prompt-file",
                    str(prompt_file),
                    "--json",
                )
                self.assertEqual(exit_code, 0, stderr)
                started = json.loads(stdout)["task"]
                workspace = Path(started["worktree"]["worktree_path"])
                branch = started["worktree"]["branch"]
                exit_code, _, stderr = self.run_cli(
                    "task",
                    "close",
                    "--project-root",
                    str(self.root),
                    "--status",
                    "failed",
                    "--summary",
                    f"Close before cleanup event fault {fault_point}",
                    cwd=workspace,
                )
                self.assertEqual(exit_code, 0, stderr)

                def fail_before_append(*args, **kwargs):
                    raise OSError("simulated event write failure before append")

                def fail_after_append(*args, **kwargs):
                    real_append_event_once(*args, **kwargs)
                    raise OSError("simulated event write failure after append")

                fault = fail_before_append if fault_point == "before_append" else fail_after_append
                with patch("blackdog.wtam.append_event_once", side_effect=fault):
                    exit_code, stdout, stderr = self.run_cli(
                        "task",
                        "cleanup",
                        "--project-root",
                        str(self.root),
                        "--workset",
                        started["workset_id"],
                        "--task",
                        started["task_id"],
                        "--json",
                    )
                self.assertEqual(exit_code, 1, stderr)
                partial = json.loads(stdout)["cleanup"]
                self.assertEqual(partial["operation_status"], "partial")
                self.assertEqual(partial["mutation_phase"], "cleanup_event_finalization_pending")
                self.assertTrue(partial["mutation_started"])
                self.assertFalse(partial["mutation_completed"])
                self.assertFalse(partial["event_finalized"])
                self.assertTrue(partial["cleanup_complete"])
                self.assertFalse(workspace.exists())
                self.assertEqual(
                    subprocess.run(
                        ["git", "-C", str(self.root), "show-ref", "--hash", f"refs/heads/{branch}"],
                        check=False,
                        capture_output=True,
                        text=True,
                    ).returncode,
                    1,
                )
                action = partial["next_action"]
                self.assertEqual(action["action_id"], "finalize_cleanup_event")
                self.assertEqual(action["mutation_class"], "event")
                self.assertIn(f"--path={workspace}", action["argv"])
                self.assertIn(f"--branch={branch}", action["argv"])

                cleanup_events = [
                    event
                    for event in load_events(profile.paths.events_file)
                    if event.get("event_id") == partial["cleanup_event_id"]
                ]
                self.assertEqual(
                    len(cleanup_events),
                    0 if fault_point == "before_append" else 1,
                )

                exit_code, stdout, stderr = self.run_cli(*action["argv"][1:])
                self.assertEqual(exit_code, 0, stderr)
                cleanup_events = [
                    event
                    for event in load_events(profile.paths.events_file)
                    if event.get("event_id") == partial["cleanup_event_id"]
                ]
                self.assertEqual(len(cleanup_events), 1)
                self.assertEqual(
                    cleanup_events[0]["payload"],
                    {
                        "workset_id": started["workset_id"],
                        "task_id": started["task_id"],
                        "attempt_id": started["worktree"]["attempt_id"],
                        "branch": branch,
                        "worktree_path": str(workspace),
                        "cleanup_complete": True,
                        "worktree_absent": True,
                        "branch_absent": True,
                    },
                )
                events_after_retry = profile.paths.events_file.read_bytes()

                exit_code, stdout, stderr = self.run_cli(*action["argv"][1:])
                self.assertEqual(exit_code, 0, stderr)
                self.assertIn("already clean", stdout)
                self.assertEqual(profile.paths.events_file.read_bytes(), events_after_retry)
                self.assertEqual(
                    sum(
                        event.get("event_id") == partial["cleanup_event_id"]
                        for event in load_events(profile.paths.events_file)
                    ),
                    1,
                )

    def test_task_land_structured_non_success_outcomes_exit_nonzero(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "land-agent",
            "--execution-prompt",
            "Exercise no-change task landing.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        no_change = json.loads(stdout)["task"]
        no_change_workspace = Path(no_change["worktree"]["worktree_path"])
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "No change landing must close structurally",
            "--validation",
            "no-change-check=passed",
            "--json",
            cwd=no_change_workspace,
        )
        self.assertEqual(exit_code, 1, stderr)
        closed = json.loads(stdout)["landing"]
        self.assertEqual(closed["operation_status"], "closed")
        self.assertEqual(closed["failure_class"], "no_changes")
        self.assertEqual(closed["land_failure_disposition"], "closed")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "land-agent",
            "--execution-prompt",
            "Exercise dirty-primary task landing.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        dirty = json.loads(stdout)["task"]
        dirty_workspace = Path(dirty["worktree"]["worktree_path"])
        (dirty_workspace / "dirty-primary-fixture.txt").write_text("task change\n", encoding="utf-8")
        primary_dirty = self.root / "primary-dirty-fixture.txt"
        primary_dirty.write_text("primary change\n", encoding="utf-8")
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "Dirty primary must remain retryable",
            "--validation",
            "dirty-primary-check=passed",
            "--json",
            cwd=dirty_workspace,
        )
        self.assertEqual(exit_code, 1, stderr)
        blocked = json.loads(stdout)["landing"]
        self.assertEqual(blocked["operation_status"], "blocked")
        self.assertEqual(blocked["failure_class"], "dirty_primary")
        self.assertTrue(blocked["attempt_active"])
        primary_dirty.unlink()
        exit_code, _, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "Retry after primary cleanup",
            "--validation",
            "dirty-primary-check=passed",
            "--json",
            cwd=dirty_workspace,
        )
        self.assertEqual(exit_code, 0, stderr)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "land-agent",
            "--execution-prompt",
            "Exercise stale task landing.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        stale = json.loads(stdout)["task"]
        stale_workspace = Path(stale["worktree"]["worktree_path"])
        (stale_workspace / "stale-fixture.txt").write_text("task change\n", encoding="utf-8")
        (self.root / "advance-main.txt").write_text("advance\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "advance-main.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Advance main for stale landing"],
            check=True,
            capture_output=True,
            text=True,
        )
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "Stale task branch must remain retryable",
            "--validation",
            "stale-branch-check=passed",
            "--json",
            cwd=stale_workspace,
        )
        self.assertEqual(exit_code, 1, stderr)
        stale_blocked = json.loads(stdout)["landing"]
        self.assertEqual(stale_blocked["operation_status"], "blocked")
        self.assertEqual(stale_blocked["failure_class"], "stale_branch")
        self.assertTrue(stale_blocked["attempt_active"])
        self.assertFalse(stale_blocked["mutation_started"])
        self.assertFalse(stale_blocked["mutation_completed"])
        self.assertEqual(stale_blocked["mutation_phase"], "preflight")
        self.assertEqual(stale_blocked["next_action"]["kind"], "command")
        self.assertEqual(
            stale_blocked["next_action"]["action_id"],
            "rebase_task_branch",
        )
        self.assertEqual(
            stale_blocked["next_action"]["argv"],
            ["git", "-C", str(stale_workspace), "rebase", "main"],
        )
        subprocess.run(
            ["git", "-C", str(stale_workspace), "rebase", "main"],
            check=True,
            capture_output=True,
            text=True,
        )
        exit_code, _, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "Retry after task branch rebase",
            "--validation",
            "stale-branch-check=passed",
            "--json",
            cwd=stale_workspace,
        )
        self.assertEqual(exit_code, 0, stderr)

    def test_landing_evidence_is_required_before_any_mutation(self) -> None:
        self.install_repo_runtime()
        profile = load_profile(self.root)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "evidence-agent",
            "--execution-prompt",
            "Require honest closeout evidence before landing.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        started = json.loads(stdout)["task"]
        workset_id = started["workset_id"]
        task_id = started["task_id"]
        attempt_id = started["worktree"]["attempt_id"]
        workspace = Path(started["worktree"]["worktree_path"])
        branch = started["worktree"]["branch"]
        (workspace / "landing-evidence.txt").write_text("evidence\n", encoding="utf-8")

        for command in ("show", "recover"):
            exit_code, stdout, stderr = self.run_cli(
                "task",
                command,
                "--project-root",
                str(self.root),
                "--json",
                cwd=workspace,
            )
            self.assertEqual(exit_code, 0, stderr)
            wrapper = "task_show" if command == "show" else "recovery"
            observed = json.loads(stdout)[wrapper]
            self.assertEqual(observed["next_action"]["action_id"], "landing_evidence_required")
            self.assertEqual(observed["next_action"]["kind"], "blocked")
            self.assertEqual(observed["next_action"]["argv"], [])
            self.assertEqual(
                observed["next_action"]["required_inputs"],
                ["completion_summary", "validation_evidence"],
            )
            self.assertTrue(observed["recommended_commands"])
            self.assertTrue(
                all(
                    row["deprecated"]
                    and row["template"]
                    and not row["executable"]
                    and row["argv"] is None
                    for row in observed["recommended_commands"]
                )
            )

            exit_code, text_output, stderr = self.run_cli(
                "task",
                command,
                "--project-root",
                str(self.root),
                cwd=workspace,
            )
            self.assertEqual(exit_code, 0, stderr)
            self.assertNotIn("recommended actions:", text_output)
            self.assertLess(
                text_output.index(f"operation: task.{command}"),
                text_output.index("next action: landing_evidence_required"),
            )
            self.assertLess(
                text_output.index("next action: landing_evidence_required"),
                text_output.index(f"{command}: {task_id}"),
            )
            self.assertIn("required input: completion_summary", text_output)
            self.assertIn("required input: validation_evidence", text_output)

        runtime_before = profile.paths.runtime_file.read_bytes()
        events_before = profile.paths.events_file.read_bytes()
        branch_before = self.git_output("rev-parse", branch)
        target_before = self.git_output("rev-parse", "main")
        status_before = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain=v1", "-z"],
            check=True,
            capture_output=True,
        ).stdout

        incomplete_requests = (
            ("missing both", ()),
            (
                "summary only",
                ("--summary", "Completed without recorded validation"),
            ),
            ("validation only", ("--validation", "not-run=skipped")),
        )
        for label, extra in incomplete_requests:
            with self.subTest(label=label):
                exit_code, stdout, stderr = self.run_cli(
                    "task",
                    "land",
                    "--project-root",
                    str(self.root),
                    *extra,
                    "--json",
                    cwd=workspace,
                )
                self.assertEqual(exit_code, 1, stderr)
                blocked = json.loads(stdout)["landing"]
                self.assertEqual(blocked["operation_status"], "blocked")
                self.assertFalse(blocked["mutation_started"])
                self.assertFalse(blocked["mutation_completed"])
                self.assertEqual(blocked["mutation_phase"], "none")
                self.assertEqual(blocked["next_action"]["action_id"], "landing_evidence_required")
                self.assertEqual(blocked["next_action"]["argv"], [])
                self.assertEqual(blocked.get("validations", []), [])
                self.assertTrue(blocked["recommended_commands"])
                self.assertTrue(
                    all(
                        row["deprecated"]
                        and row["template"]
                        and not row["executable"]
                        and row["argv"] is None
                        for row in blocked["recommended_commands"]
                    )
                )
                self.assertIsNone(
                    load_landing_transaction(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        attempt_id=attempt_id,
                    )
                )
                self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
                self.assertEqual(profile.paths.events_file.read_bytes(), events_before)
                self.assertEqual(self.git_output("rev-parse", branch), branch_before)
                self.assertEqual(self.git_output("rev-parse", "main"), target_before)
                self.assertEqual(
                    subprocess.run(
                        ["git", "-C", str(workspace), "status", "--porcelain=v1", "-z"],
                        check=True,
                        capture_output=True,
                    ).stdout,
                    status_before,
                )

        exit_code, text_output, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "Still missing validation evidence",
            cwd=workspace,
        )
        self.assertEqual(exit_code, 1, stderr)
        self.assertNotIn("recommended actions:", text_output)
        self.assertLess(
            text_output.index("operation: task.land"),
            text_output.index("next action: landing_evidence_required"),
        )
        self.assertLess(
            text_output.index("next action: landing_evidence_required"),
            text_output.index("land blocked:"),
        )
        self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(profile.paths.events_file.read_bytes(), events_before)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "Landed with explicit skipped validation evidence",
            "--validation",
            "not-run=skipped",
            "--json",
            cwd=workspace,
        )
        self.assertEqual(exit_code, 0, stderr)
        landed = json.loads(stdout)["landing"]
        self.assertEqual(landed["operation_status"], "succeeded")
        finished = wtam.find_task_attempt(
            load_runtime_state(profile.paths),
            workset_id,
            attempt_id,
        )
        self.assertIsNotNone(finished)
        self.assertEqual(
            [(row.name, row.status) for row in finished.validations],
            [("not-run", "skipped")],
        )

    def test_incomplete_landing_replays_recorded_evidence_without_resupply(self) -> None:
        self.install_repo_runtime()
        profile = load_profile(self.root)
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "replay-agent",
            "--execution-prompt",
            "Replay immutable landing evidence after interruption.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        started = json.loads(stdout)["task"]
        workspace = Path(started["worktree"]["worktree_path"])
        attempt_id = started["worktree"]["attempt_id"]
        (workspace / "landing-replay.txt").write_text("replay\n", encoding="utf-8")

        with patch(
            "blackdog.wtam._run_landing_transaction",
            side_effect=WorktreeError("simulated interruption after landing intent"),
        ):
            exit_code, stdout, stderr = self.run_cli(
                "task",
                "land",
                "--project-root",
                str(self.root),
                "--summary",
                "Preserve this immutable completion summary",
                "--validation",
                "unit=passed",
                "--residual",
                "recorded residual",
                "--json",
                cwd=workspace,
            )
        self.assertEqual(exit_code, 1, stderr)
        partial = json.loads(stdout)["landing"]
        self.assertEqual(partial["operation_status"], "partial")
        self.assertEqual(partial["mutation_phase"], "landing_intent_recorded")
        self.assertEqual(partial["next_action"]["action_id"], "resume_landing_transaction")

        transaction = load_landing_transaction(
            profile,
            workset_id=started["workset_id"],
            task_id=started["task_id"],
            attempt_id=attempt_id,
        )
        self.assertIsNotNone(transaction)
        self.assertEqual(transaction.intent.summary, "Preserve this immutable completion summary")
        self.assertEqual(transaction.intent.validations, (("unit", "passed"),))

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--json",
            cwd=workspace,
        )
        self.assertEqual(exit_code, 0, stderr)
        replayed = json.loads(stdout)["landing"]
        self.assertEqual(replayed["operation_status"], "succeeded")
        finished = wtam.find_task_attempt(
            load_runtime_state(profile.paths),
            started["workset_id"],
            attempt_id,
        )
        self.assertIsNotNone(finished)
        self.assertEqual(finished.summary, "Preserve this immutable completion summary")
        self.assertEqual(
            [(row.name, row.status) for row in finished.validations],
            [("unit", "passed")],
        )
        self.assertEqual(finished.residuals, ("recorded residual",))

    def test_git_reference_inspection_error_is_typed_and_never_advertises_land(self) -> None:
        import blackdog.wtam as wtam_module

        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "reference-agent",
            "--execution-prompt",
            "Exercise Git reference inspection failure.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        started = json.loads(stdout)["task"]
        branch = started["worktree"]["branch"]
        real_run_git_no_check = wtam_module._run_git_no_check

        def fail_task_ref_inspection(repo_root: Path, *args: str):
            if args == ("show-ref", "--hash", f"refs/heads/{branch}"):
                return subprocess.CompletedProcess(
                    ["git", *args],
                    2,
                    stdout="",
                    stderr="simulated repository read failure",
                )
            return real_run_git_no_check(repo_root, *args)

        with patch("blackdog.wtam._run_git_no_check", side_effect=fail_task_ref_inspection):
            exit_code, stdout, stderr = self.run_cli(
                "task",
                "show",
                "--project-root",
                str(self.root),
                "--workset",
                started["workset_id"],
                "--task",
                started["task_id"],
                "--json",
            )
        self.assertEqual(exit_code, 0, stderr)
        shown = json.loads(stdout)["task_show"]
        self.assertEqual(shown["branch_reference"]["state"], "error")
        self.assertEqual(shown["branch_reference"]["return_code"], 2)
        self.assertEqual(shown["reference_issue_code"], "task_branch_inspection_failed")
        self.assertEqual(shown["next_action"]["kind"], "blocked")
        self.assertEqual(shown["next_action"]["action_id"], "inspect_reference_failure")
        self.assertEqual(shown["next_action"]["argv"], [])
        self.assertNotIn("land", json.dumps(shown["next_action"]))

        workspace = Path(started["worktree"]["worktree_path"])
        exit_code, _, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "failed",
            "--summary",
            "Close reference inspection fixture",
            cwd=workspace,
        )
        self.assertEqual(exit_code, 0, stderr)

        events_before = load_profile(self.root).paths.events_file.read_bytes()
        with patch("blackdog.wtam._run_git_no_check", side_effect=fail_task_ref_inspection):
            exit_code, stdout, stderr = self.run_cli(
                "task",
                "cleanup",
                "--project-root",
                str(self.root),
                "--workset",
                started["workset_id"],
                "--task",
                started["task_id"],
                "--json",
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("state", stderr)
        self.assertIn("return_code", stderr)
        self.assertTrue(workspace.exists())
        self.assertEqual(
            self.git_output("show-ref", "--hash", f"refs/heads/{branch}"),
            self.git_output("rev-parse", branch),
        )
        self.assertEqual(load_profile(self.root).paths.events_file.read_bytes(), events_before)

        exit_code, _, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--workset",
            started["workset_id"],
            "--task",
            started["task_id"],
        )
        self.assertEqual(exit_code, 0, stderr)

    def test_git_commit_inspection_distinguishes_missing_from_error(self) -> None:
        import blackdog.wtam as wtam_module

        missing_branch = wtam_module._inspect_branch_ref(
            self.root,
            "definitely-missing-branch",
            role="task_branch",
        )
        self.assertEqual(missing_branch.state, "missing")
        self.assertEqual(missing_branch.return_code, 1)

        error_process = subprocess.CompletedProcess(
            ["git", "rev-parse"],
            128,
            stdout="",
            stderr="simulated repository failure",
        )
        missing_commit = wtam_module._inspect_commit(
            self.root,
            "f" * 40,
            role="recorded_task_commit",
        )
        with patch("blackdog.wtam._run_git_no_check", return_value=error_process):
            error_commit = wtam_module._inspect_commit(
                self.root,
                "uninspectable-commit",
                role="recorded_task_commit",
            )
        self.assertEqual(missing_commit.state, "missing")
        self.assertEqual(missing_commit.return_code, 1)
        self.assertIsNone(missing_commit.resolved_commit)
        self.assertEqual(error_commit.state, "error")
        self.assertEqual(error_commit.return_code, 128)
        self.assertIsNone(error_commit.resolved_commit)

    def test_commit_inspection_error_blocks_landed_cleanup_before_mutation(self) -> None:
        import blackdog.wtam as wtam_module

        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "commit-proof-owner",
            "--execution-prompt",
            "Exercise retained landed cleanup commit proof.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        started = json.loads(stdout)["task"]
        workspace = Path(started["worktree"]["worktree_path"])
        branch = started["worktree"]["branch"]
        (workspace / "landed-cleanup-proof.txt").write_text("proof\n", encoding="utf-8")
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "Land while retaining the task workspace",
            "--validation",
            "retained-cleanup-check=passed",
            "--keep-worktree",
            "--json",
            cwd=workspace,
        )
        self.assertEqual(exit_code, 0, stderr)
        landed = json.loads(stdout)["landing"]
        recorded_commit = landed["commit"]
        self.assertTrue(recorded_commit)
        self.assertTrue(workspace.exists())
        events_before = load_profile(self.root).paths.events_file.read_bytes()
        real_run_git_no_check = wtam_module._run_git_no_check

        def fail_recorded_commit(repo_root: Path, *args: str):
            if args == (
                "rev-parse",
                "--verify",
                "--quiet",
                f"{recorded_commit}^{{commit}}",
            ):
                return subprocess.CompletedProcess(
                    ["git", *args],
                    128,
                    stdout="",
                    stderr="simulated object database inspection failure",
                )
            return real_run_git_no_check(repo_root, *args)

        with patch("blackdog.wtam._run_git_no_check", side_effect=fail_recorded_commit):
            exit_code, stdout, stderr = self.run_cli(
                "task",
                "cleanup",
                "--project-root",
                str(self.root),
                "--workset",
                started["workset_id"],
                "--task",
                started["task_id"],
                "--json",
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("recorded_task_commit", stderr)
        self.assertIn("return_code", stderr)
        self.assertIn("128", stderr)
        self.assertTrue(workspace.exists())
        self.assertEqual(
            self.git_output("show-ref", "--hash", f"refs/heads/{branch}"),
            self.git_output("rev-parse", branch),
        )
        self.assertEqual(load_profile(self.root).paths.events_file.read_bytes(), events_before)

        exit_code, _, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--workset",
            started["workset_id"],
            "--task",
            started["task_id"],
        )
        self.assertEqual(exit_code, 0, stderr)


if __name__ == "__main__":
    import unittest

    unittest.main()
