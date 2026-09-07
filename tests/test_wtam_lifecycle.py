from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import blackdog.wtam as wtam
import blackdog_core.tasks as core_tasks
from blackdog.runtime_distribution import install_runtime
from blackdog.contract import managed_skill_relative_path
from blackdog.landing import load_landing_transaction
from blackdog.prompt_artifacts import persist_prompt_receipts
from blackdog_core.profile import load_profile, render_default_profile
from blackdog_core.state import (
    ATTEMPT_STATUS_SUCCESS,
    PROMPT_MODE_SKILL,
    TASK_STATUS_CANCELED,
    TASK_STATUS_DONE,
    TASK_STATUS_PLANNED,
    ValidationRecord,
    create_prompt_receipt,
    load_events,
    load_runtime_state,
    prompt_receipt_reference,
    task_record,
)
from blackdog_core.tasks import TaskError, create_task, start_task


def _git(root: Path, *args: str, input_text: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@dataclass(frozen=True)
class _ReadyHandlers:
    ready: bool = True
    remediation: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"ready": self.ready, "actions": []}


class ProductRepo:
    def __init__(self, suffix: str) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix=f"blackdog-product-{suffix}-")
        self.base = Path(self._temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        _git(self.root, "init", "-b", "main")
        _git(self.root, "config", "user.email", "blackdog@example.com")
        _git(self.root, "config", "user.name", "Blackdog Test")
        (self.root / ".gitignore").write_text(".VE/\n", encoding="utf-8")
        (self.root / "blackdog.toml").write_text(
            render_default_profile(f"Product {suffix}"), encoding="utf-8"
        )
        _git(self.root, "add", ".gitignore", "blackdog.toml")
        _git(self.root, "commit", "-m", "Initialize product fixture")
        self.profile = load_profile(self.root)
        self.task_id = f"task-{hashlib.sha256(suffix.encode('utf-8')).hexdigest()[:32]}"
        self.branch = f"agent/{suffix}"
        self.worktree = self.base / ".worktrees" / suffix
        self.actor = "codex"

    def close(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(self.worktree)],
            check=False,
            capture_output=True,
            text=True,
        )
        self._temporary.cleanup()

    def start(self, *, dirty: bool = True, commit_change: bool = False):
        task = create_task(self.profile, task_id=self.task_id, title=f"Exercise {self.task_id}")
        self.worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(self.root, "worktree", "add", "-b", self.branch, str(self.worktree), "main")
        request, execution = persist_prompt_receipts(
            self.profile.paths.control_dir,
            (
                create_prompt_receipt("Exact user request", source="request.txt", mode="raw"),
                create_prompt_receipt("Exact execution prompt", source="execution.txt", mode="raw"),
            ),
        )
        attempt = start_task(
            self.profile,
            task_id=task.task_id,
            actor=self.actor,
            workspace_identity=task.task_id,
            workspace_mode="git-worktree",
            worktree_role="task",
            worktree_path=str(self.worktree),
            branch=self.branch,
            target_branch="main",
            integration_branch=self.branch,
            start_commit=_git(self.root, "rev-parse", "main"),
            prompt_receipt=prompt_receipt_reference(execution),
            user_prompt_receipt=prompt_receipt_reference(request),
            setup_receipt={"ready": True, "actions": []},
        )
        if dirty:
            (self.worktree / f"{self.task_id}.txt").write_text("change\n", encoding="utf-8")
        if commit_change:
            _git(self.worktree, "add", "-A")
            _git(self.worktree, "commit", "-m", "Commit product fixture change")
        return task, attempt

    def snapshot(self) -> tuple[bytes, bytes, str, str]:
        runtime = self.profile.paths.runtime_file.read_bytes()
        events = self.profile.paths.events_file.read_bytes()
        refs = _git(self.root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
        worktrees = _git(self.root, "worktree", "list", "--porcelain")
        return runtime, events, refs, worktrees

    def foreign_clone_same_branch(self, name: str = "foreign") -> Path:
        foreign = self.base / name
        subprocess.run(
            ["git", "clone", "--no-checkout", str(self.root), str(foreign)],
            check=True,
            capture_output=True,
            text=True,
        )
        _git(foreign, "checkout", "-b", self.branch, f"origin/{self.branch}")
        return foreign


class ProductLifecycleTests(unittest.TestCase):
    maxDiff = None

    def test_begin_preflight_refusal_is_zero_mutation(self) -> None:
        repo = ProductRepo("begin-preflight")
        try:
            (repo.root / "dirty.txt").write_text("primary change\n", encoding="utf-8")
            before = (
                repo.profile.paths.runtime_file.exists(),
                repo.profile.paths.events_file.exists(),
                _git(repo.root, "worktree", "list", "--porcelain"),
            )
            with self.assertRaises(wtam.TaskBeginPreflightError):
                wtam.begin_task_worktree(
                    repo.profile,
                    actor=repo.actor,
                    prompt="execution",
                    prompt_source="execution.txt",
                    user_prompt="request",
                    user_prompt_source="request.txt",
                    cwd=repo.root,
                )
            self.assertEqual(
                (
                    repo.profile.paths.runtime_file.exists(),
                    repo.profile.paths.events_file.exists(),
                    _git(repo.root, "worktree", "list", "--porcelain"),
                ),
                before,
            )
            self.assertFalse(repo.profile.paths.control_dir.joinpath("prompts").exists())
        finally:
            repo.close()

    def test_start_event_fault_preserves_workspace_and_exact_retry_noops(self) -> None:
        repo = ProductRepo("begin-fault")
        original = core_tasks.append_event_once
        failed = False

        def fail_start_once(*args, **kwargs):
            nonlocal failed
            if kwargs.get("event_type") == "task.start" and not failed:
                failed = True
                raise OSError("fault before task.start")
            return original(*args, **kwargs)

        try:
            with patch.object(wtam, "plan_worktree_handlers", return_value=_ReadyHandlers()), patch.object(
                wtam, "execute_worktree_handlers", return_value=_ReadyHandlers()
            ), patch.object(wtam, "validate_existing_worktree_handlers", return_value=_ReadyHandlers()), patch.object(
                core_tasks, "append_event_once", side_effect=fail_start_once
            ):
                first = wtam.begin_task_worktree(
                    repo.profile,
                    actor=repo.actor,
                    prompt="execution",
                    prompt_source="execution.txt",
                    user_prompt="request",
                    user_prompt_source="request.txt",
                    task_id=repo.task_id,
                    branch=repo.branch,
                    path=str(repo.worktree),
                    cwd=repo.root,
                )
            self.assertEqual(first.operation_status, "partial")
            self.assertEqual(first.next_action.action_id, "retry_task_start_finalization")
            self.assertTrue(repo.worktree.is_dir())
            self.assertEqual(_git(repo.root, "rev-parse", "--abbrev-ref", repo.branch), repo.branch)
            with patch.object(wtam, "validate_existing_worktree_handlers", return_value=_ReadyHandlers()):
                second = wtam.begin_task_worktree(
                    repo.profile,
                    actor=repo.actor,
                    prompt="execution",
                    prompt_source="execution.txt",
                    user_prompt="request",
                    user_prompt_source="request.txt",
                    task_id=repo.task_id,
                    cwd=repo.root,
                )
                after_second = repo.snapshot()
                third = wtam.begin_task_worktree(
                    repo.profile,
                    actor=repo.actor,
                    prompt="execution",
                    prompt_source="execution.txt",
                    user_prompt="request",
                    user_prompt_source="request.txt",
                    task_id=repo.task_id,
                    cwd=repo.root,
                )
            self.assertEqual(second.operation_status, "succeeded")
            self.assertEqual(third.operation_status, "succeeded")
            self.assertEqual(repo.snapshot(), after_second)
        finally:
            repo.close()

    def test_skill_mode_persists_one_canonical_prompt_and_replays_without_recomposition(self) -> None:
        repo = ProductRepo("skill-replay")
        try:
            skill = repo.root / managed_skill_relative_path(repo.profile)
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: product-skill-replay\n---\n\nUse the task protocol.\n", encoding="utf-8")
            _git(repo.root, "add", skill.relative_to(repo.root).as_posix())
            _git(repo.root, "commit", "-m", "Add managed skill fixture")
            repo.profile = load_profile(repo.root)
            with patch.object(wtam, "plan_worktree_handlers", return_value=_ReadyHandlers()), patch.object(
                wtam, "execute_worktree_handlers", return_value=_ReadyHandlers()
            ), patch.object(wtam, "validate_existing_worktree_handlers", return_value=_ReadyHandlers()):
                first = wtam.begin_task_worktree(
                    repo.profile,
                    actor=repo.actor,
                    prompt="Implement the exact requested repair.",
                    prompt_source="execution.txt",
                    user_prompt="Repair the product.",
                    user_prompt_source="request.txt",
                    prompt_mode=PROMPT_MODE_SKILL,
                    task_id=repo.task_id,
                    branch=repo.branch,
                    path=str(repo.worktree),
                    cwd=repo.root,
                )
                task = task_record(load_runtime_state(repo.profile.paths), repo.task_id)
                self.assertIsNotNone(task)
                assert task is not None
                attempt = task.attempts[-1]
                assert attempt.prompt_receipt is not None
                assert attempt.prompt_receipt.replay_artifact_path is not None
                artifact = repo.profile.paths.control_dir / attempt.prompt_receipt.replay_artifact_path
                canonical = artifact.read_text(encoding="utf-8")
                self.assertEqual(canonical, canonical.strip())
                self.assertEqual(canonical.count("You are working in the repo"), 1)
                self.assertEqual(
                    hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    attempt.prompt_receipt.prompt_hash,
                )
                self.assertEqual(
                    attempt.setup_receipt["skill_provenance"]["path"],
                    managed_skill_relative_path(repo.profile).as_posix(),
                )
                argv = wtam._resume_begin_argv(repo.profile, task, attempt)
                self.assertIsNotNone(argv)
                assert argv is not None
                self.assertFalse(any(item.startswith("--execution-prompt-source=") for item in argv))

                replay = wtam.begin_task_worktree(
                    repo.profile,
                    actor=repo.actor,
                    prompt=canonical,
                    prompt_source=str(artifact.resolve()),
                    user_prompt="Repair the product.",
                    user_prompt_source="request.txt",
                    prompt_mode=PROMPT_MODE_SKILL,
                    task_id=repo.task_id,
                    cwd=repo.root,
                )
            self.assertEqual(first["prompt_hash"], replay["prompt_hash"])
            self.assertEqual(first["prompt_mode"], PROMPT_MODE_SKILL)
            self.assertEqual(replay["prompt_mode"], PROMPT_MODE_SKILL)
            self.assertEqual(artifact.read_text(encoding="utf-8"), canonical)
        finally:
            repo.close()

    def test_task_begin_guard_pass_receipt_and_refusal_are_pre_mutation(self) -> None:
        for status in ("passed", "blocked"):
            repo = ProductRepo(f"guard-{status}")
            try:
                guard = repo.root / "guard.sh"
                required = "[]" if status == "passed" else '["owner_approval"]'
                guard.write_text(
                    "printf '%s\\n' '"
                    f'{{"schema_version":1,"status":"{status}","reason_code":"fixture_{status}",'
                    f'"message":"fixture {status}","required_inputs":{required}}}'
                    "'\n",
                    encoding="utf-8",
                )
                with (repo.root / "blackdog.toml").open("a", encoding="utf-8") as handle:
                    handle.write(
                        "\n[[guards]]\n"
                        "schema_version = 1\n"
                        'id = "fixture-policy"\n'
                        'phase = "task_begin"\n'
                        'command = ["sh", "guard.sh"]\n'
                        "timeout_seconds = 5\n"
                    )
                _git(repo.root, "add", "blackdog.toml", "guard.sh")
                _git(repo.root, "commit", "-m", f"Add {status} guard fixture")
                repo.profile = load_profile(repo.root)
                before = (
                    repo.profile.paths.runtime_file.exists(),
                    repo.profile.paths.events_file.exists(),
                    _git(repo.root, "worktree", "list", "--porcelain"),
                )
                handlers = (
                    patch.object(wtam, "plan_worktree_handlers", return_value=_ReadyHandlers()),
                    patch.object(wtam, "execute_worktree_handlers", return_value=_ReadyHandlers()),
                )
                with handlers[0], handlers[1]:
                    if status == "blocked":
                        with self.assertRaises(wtam.TaskBeginPreflightError) as raised:
                            wtam.begin_task_worktree(
                                repo.profile,
                                actor=repo.actor,
                                prompt="execution",
                                user_prompt="request",
                                task_id=repo.task_id,
                                branch=repo.branch,
                                path=str(repo.worktree),
                                cwd=repo.root,
                            )
                        refusal = wtam.task_begin_preflight_result(
                            raised.exception,
                            actor=repo.actor,
                            prompt_mode="raw",
                            task_id=repo.task_id,
                        )
                        self.assertEqual(refusal.next_action.action_id, "repository_guard_blocked")
                        self.assertEqual(refusal.next_action.reason_code, "fixture_blocked")
                        self.assertEqual(refusal.next_action.required_inputs, ("owner_approval",))
                        self.assertEqual(
                            (
                                repo.profile.paths.runtime_file.exists(),
                                repo.profile.paths.events_file.exists(),
                                _git(repo.root, "worktree", "list", "--porcelain"),
                            ),
                            before,
                        )
                        self.assertFalse(repo.profile.paths.control_dir.joinpath("prompts").exists())
                    else:
                        wtam.begin_task_worktree(
                            repo.profile,
                            actor=repo.actor,
                            prompt="execution",
                            user_prompt="request",
                            task_id=repo.task_id,
                            branch=repo.branch,
                            path=str(repo.worktree),
                            cwd=repo.root,
                        )
                        task = task_record(load_runtime_state(repo.profile.paths), repo.task_id)
                        self.assertIsNotNone(task)
                        assert task is not None
                        receipts = task.attempts[-1].setup_receipt["guard_receipts"]
                        self.assertEqual(len(receipts), 1)
                        self.assertEqual(receipts[0]["id"], "fixture-policy")
                        self.assertEqual(receipts[0]["status"], "passed")
            finally:
                repo.close()

    def test_resume_source_provenance_conflict_is_zero_mutation(self) -> None:
        repo = ProductRepo("resume-lineage")
        try:
            task, attempt = repo.start(dirty=False)
            wtam.close_task(
                repo.profile,
                task_id=task.task_id,
                actor=repo.actor,
                status="blocked",
                summary="Retain exact workspace",
                validations=(ValidationRecord("unit", "passed"),),
                cleanup=False,
            )
            wtam.cancel_task(repo.profile, task_id=task.task_id, actor=repo.actor, summary="Cancel blocked task")
            wtam.reopen_task(repo.profile, task_id=task.task_id, actor=repo.actor, summary="Reopen exact task")
            before = repo.snapshot()
            with self.assertRaisesRegex(TaskError, "prompt lineage"):
                wtam.begin_task_worktree(
                    repo.profile,
                    actor=repo.actor,
                    prompt="Exact execution prompt",
                    prompt_source="different-execution.txt",
                    user_prompt="Exact user request",
                    user_prompt_source="request.txt",
                    task_id=task.task_id,
                    cwd=repo.root,
                )
            self.assertEqual(repo.snapshot(), before)
        finally:
            repo.close()

    def test_show_recover_and_active_begin_require_registered_worktree_proof(self) -> None:
        repo = ProductRepo("active-proof")
        try:
            task, _attempt = repo.start(dirty=False)
            valid = wtam.show_task(repo.profile, task_id=task.task_id)
            self.assertTrue(valid["worktree_proven"])
            self.assertEqual(valid.next_action.action_id, "continue_active_attempt")

            _git(repo.root, "worktree", "remove", "--force", str(repo.worktree))
            repo.worktree.mkdir(parents=True)
            before = repo.snapshot()
            shown = wtam.show_task(repo.profile, task_id=task.task_id)
            recovered = wtam.recover_task(repo.profile, task_id=task.task_id)
            self.assertFalse(shown["worktree_proven"])
            self.assertEqual(shown.next_action.action_id, "close_evidence_required")
            self.assertEqual(recovered.next_action.action_id, "close_evidence_required")
            with patch.object(
                wtam,
                "validate_existing_worktree_handlers",
                side_effect=AssertionError("handlers must not run for an unproven workspace"),
            ):
                begun = wtam.begin_task_worktree(
                    repo.profile,
                    actor=repo.actor,
                    prompt="Exact execution prompt",
                    prompt_source="execution.txt",
                    user_prompt="Exact user request",
                    user_prompt_source="request.txt",
                    task_id=task.task_id,
                    cwd=repo.root,
                )
            self.assertEqual(begun.operation_status, "blocked")
            self.assertEqual(begun.next_action.action_id, "close_evidence_required")
            self.assertEqual(repo.snapshot(), before)
        finally:
            repo.close()

    def test_active_worktree_head_must_descend_from_recorded_start(self) -> None:
        repo = ProductRepo("active-lineage")
        try:
            task, _attempt = repo.start(dirty=False)
            tree = _git(repo.worktree, "rev-parse", "HEAD^{tree}")
            unrelated = _git(repo.worktree, "commit-tree", tree, input_text="Unrelated root\n")
            _git(repo.worktree, "reset", "--hard", unrelated)
            result = wtam.show_task(repo.profile, task_id=task.task_id)
            self.assertFalse(result["worktree_proven"])
            self.assertIn("not descended", result["worktree_proof"]["reason"])
            self.assertEqual(result.next_action.action_id, "close_evidence_required")
        finally:
            repo.close()

    def test_foreign_same_branch_clone_cannot_resolve_or_mutate_task(self) -> None:
        repo = ProductRepo("foreign-clone")
        try:
            task, _attempt = repo.start(dirty=False)
            foreign = repo.foreign_clone_same_branch()
            before = repo.snapshot()

            with self.assertRaisesRegex(TaskError, "task identity is not certified"):
                wtam.show_task(repo.profile, task_id=None, cwd=foreign)
            explicit = wtam.show_task(
                repo.profile,
                task_id=task.task_id,
                cwd=foreign,
            )
            self.assertEqual(explicit.operation_status, "observed")
            self.assertEqual(repo.snapshot(), before)

            mutations = (
                lambda: wtam.recover_task(
                    repo.profile,
                    task_id=task.task_id,
                    status="blocked",
                    summary="Foreign recovery",
                    cwd=foreign,
                ),
                lambda: wtam.cancel_task(
                    repo.profile,
                    task_id=task.task_id,
                    actor=repo.actor,
                    summary="Foreign cancel",
                    cwd=foreign,
                ),
                lambda: wtam.reopen_task(
                    repo.profile,
                    task_id=task.task_id,
                    actor=repo.actor,
                    summary="Foreign reopen",
                    cwd=foreign,
                ),
                lambda: wtam.cleanup_task(
                    repo.profile,
                    task_id=task.task_id,
                    cwd=foreign,
                ),
                lambda: wtam.land_task(
                    repo.profile,
                    task_id=task.task_id,
                    actor=repo.actor,
                    summary="Foreign landing",
                    validations=(ValidationRecord("unit", "passed"),),
                    cwd=foreign,
                ),
                lambda: wtam.close_task(
                    repo.profile,
                    task_id=task.task_id,
                    actor=repo.actor,
                    status="blocked",
                    summary="Foreign close",
                    validations=(ValidationRecord("unit", "passed"),),
                    cwd=foreign,
                ),
            )
            for mutate in mutations:
                with self.assertRaisesRegex(TaskError, "task mutation requires"):
                    mutate()
                self.assertEqual(repo.snapshot(), before)
        finally:
            repo.close()

    def test_same_repository_sibling_worktree_cannot_mutate_task(self) -> None:
        repo = ProductRepo("sibling-worktree")
        sibling = repo.base / "sibling"
        try:
            task, _attempt = repo.start(dirty=False)
            _git(repo.root, "worktree", "add", "-b", "sibling-worktree", str(sibling), "main")
            sibling_profile = load_profile(sibling)
            before = repo.snapshot()
            with self.assertRaisesRegex(TaskError, "task mutation requires"):
                wtam.recover_task(
                    sibling_profile,
                    task_id=task.task_id,
                    status="blocked",
                    summary="Sibling recovery",
                    cwd=sibling,
                )
            self.assertEqual(repo.snapshot(), before)
        finally:
            subprocess.run(
                ["git", "-C", str(repo.root), "worktree", "remove", "--force", str(sibling)],
                check=False,
                capture_output=True,
                text=True,
            )
            repo.close()

    def test_cleanup_ahead_refusal_is_typed_and_zero_mutation(self) -> None:
        repo = ProductRepo("cleanup-ahead")
        try:
            task, _attempt = repo.start(commit_change=True)
            wtam.close_task(
                repo.profile,
                task_id=task.task_id,
                actor=repo.actor,
                status="blocked",
                summary="Retain unlanded branch",
                validations=(ValidationRecord("unit", "passed"),),
                cleanup=False,
            )
            before = repo.snapshot()
            result = wtam.cleanup_task(repo.profile, task_id=task.task_id)
            self.assertEqual(result.operation_status, "blocked")
            self.assertEqual(result.next_action.action_id, "inspect_cleanup_ownership")
            self.assertFalse(result.mutation_started)
            self.assertEqual(repo.snapshot(), before)
            self.assertTrue(repo.worktree.is_dir())
            self.assertEqual(_git(repo.root, "rev-parse", repo.branch), _git(repo.worktree, "rev-parse", "HEAD"))
        finally:
            repo.close()

    def test_close_cleanup_retains_unlanded_branch(self) -> None:
        repo = ProductRepo("close-retain")
        try:
            task, _attempt = repo.start(commit_change=True)
            result = wtam.close_task(
                repo.profile,
                task_id=task.task_id,
                actor=repo.actor,
                status="blocked",
                summary="Close and retain unlanded work",
                validations=(ValidationRecord("unit", "passed"),),
                cleanup=True,
            )
            self.assertEqual(result.operation_status, "succeeded")
            self.assertTrue(result["cleanup"]["retained"])
            self.assertIn("not proven", result["cleanup"]["retained_reason"])
            self.assertTrue(repo.worktree.is_dir())
        finally:
            repo.close()

    def test_close_fault_replays_every_immutable_input_and_byte_noops(self) -> None:
        repo = ProductRepo("close-fault")
        original = core_tasks.append_event_once
        failed = False

        def fail_finish_once(*args, **kwargs):
            nonlocal failed
            result = original(*args, **kwargs)
            if kwargs.get("event_type") == "task.finish" and not failed:
                failed = True
                raise OSError("fault after task.finish")
            return result

        close_kwargs = {
            "task_id": repo.task_id,
            "actor": repo.actor,
            "status": "blocked",
            "summary": "Exact close summary",
            "validations": (ValidationRecord("unit", "passed"),),
            "residuals": ("residual",),
            "followup_candidates": ("followup",),
            "note": "note",
            "cleanup": False,
            "failure_class": "unknown",
            "recovery_action": "inspect",
            "operator_issue": True,
        }
        try:
            repo.start(dirty=False)
            with patch.object(core_tasks, "append_event_once", side_effect=fail_finish_once):
                first = wtam.close_task(repo.profile, **close_kwargs)
            self.assertEqual(first.operation_status, "partial")
            argv = first.next_action.argv
            for expected in (
                "--summary=Exact close summary",
                "--validation=unit=passed",
                "--residual=residual",
                "--followup=followup",
                "--note=note",
                "--failure-class=unknown",
                "--recovery-action=inspect",
                "--operator-issue",
            ):
                self.assertIn(expected, argv)
            second = wtam.close_task(repo.profile, **close_kwargs)
            after_second = repo.snapshot()
            third = wtam.close_task(repo.profile, **close_kwargs)
            self.assertEqual(second.operation_status, "succeeded")
            self.assertEqual(third.operation_status, "succeeded")
            self.assertEqual(repo.snapshot(), after_second)
        finally:
            repo.close()

    def test_recover_cancel_and_reopen_event_faults_return_exact_typed_retries(self) -> None:
        repo = ProductRepo("state-retry-faults")
        original = core_tasks.append_event_once

        try:
            task, _attempt = repo.start(dirty=False)

            recover_failed = False

            def fail_recover_finish(*args, **kwargs):
                nonlocal recover_failed
                result = original(*args, **kwargs)
                if kwargs.get("event_type") == "task.finish" and not recover_failed:
                    recover_failed = True
                    raise OSError("recover finish event fault")
                return result

            recover_kwargs = {
                "task_id": task.task_id,
                "status": "blocked",
                "summary": "Exact recovery summary",
                "note": "Exact recovery note",
            }
            with patch.object(core_tasks, "append_event_once", side_effect=fail_recover_finish):
                recover_partial = wtam.recover_task(repo.profile, **recover_kwargs)
            self.assertEqual(recover_partial.operation_status, "partial")
            self.assertEqual(
                recover_partial.next_action.action_id,
                "retry_task_recovery_finalization",
            )
            self.assertIn("--status=blocked", recover_partial.next_action.argv)
            self.assertIn("--summary=Exact recovery summary", recover_partial.next_action.argv)
            self.assertIn("--note=Exact recovery note", recover_partial.next_action.argv)
            self.assertEqual(
                wtam.recover_task(repo.profile, **recover_kwargs).operation_status,
                "succeeded",
            )

            cancel_failed = False

            def fail_cancel_event(*args, **kwargs):
                nonlocal cancel_failed
                result = original(*args, **kwargs)
                if kwargs.get("event_type") == "task.transition" and not cancel_failed:
                    cancel_failed = True
                    raise OSError("cancel transition event fault")
                return result

            cancel_kwargs = {
                "task_id": task.task_id,
                "actor": repo.actor,
                "summary": "Exact cancel summary",
                "failure_class": "unknown",
                "recovery_action": "inspect",
                "operator_issue": True,
            }
            with patch.object(core_tasks, "append_event_once", side_effect=fail_cancel_event):
                cancel_partial = wtam.cancel_task(repo.profile, **cancel_kwargs)
            self.assertEqual(cancel_partial.operation_status, "partial")
            self.assertEqual(cancel_partial.next_action.action_id, "retry_task_cancel_finalization")
            for expected in (
                "--summary=Exact cancel summary",
                "--failure-class=unknown",
                "--recovery-action=inspect",
                "--operator-issue",
            ):
                self.assertIn(expected, cancel_partial.next_action.argv)
            self.assertEqual(
                task_record(load_runtime_state(repo.profile.paths), task.task_id).status,
                TASK_STATUS_CANCELED,
            )
            self.assertEqual(
                wtam.cancel_task(repo.profile, **cancel_kwargs).operation_status,
                "succeeded",
            )

            reopen_failed = False

            def fail_reopen_event(*args, **kwargs):
                nonlocal reopen_failed
                result = original(*args, **kwargs)
                if kwargs.get("event_type") == "task.transition" and not reopen_failed:
                    reopen_failed = True
                    raise OSError("reopen transition event fault")
                return result

            reopen_kwargs = {
                "task_id": task.task_id,
                "actor": repo.actor,
                "summary": "Exact reopen summary",
            }
            with patch.object(core_tasks, "append_event_once", side_effect=fail_reopen_event):
                reopen_partial = wtam.reopen_task(repo.profile, **reopen_kwargs)
            self.assertEqual(reopen_partial.operation_status, "partial")
            self.assertEqual(reopen_partial.next_action.action_id, "retry_task_reopen_finalization")
            self.assertIn("--summary=Exact reopen summary", reopen_partial.next_action.argv)
            self.assertEqual(
                task_record(load_runtime_state(repo.profile.paths), task.task_id).status,
                TASK_STATUS_PLANNED,
            )
            self.assertEqual(
                wtam.reopen_task(repo.profile, **reopen_kwargs).operation_status,
                "succeeded",
            )
        finally:
            repo.close()

    def test_landing_records_full_current_format_and_cleans_source(self) -> None:
        repo = ProductRepo("land-complete")
        try:
            task, attempt = repo.start()
            result = wtam.land_task(
                repo.profile,
                task_id=task.task_id,
                actor=repo.actor,
                summary="Land complete fixture\nPreserve exact evidence",
                validations=(ValidationRecord("unit", "passed"),),
                residuals=("none",),
                followup_candidates=("none",),
                cleanup=True,
            )
            self.assertEqual(result.operation_status, "succeeded")
            transaction = load_landing_transaction(
                repo.profile, task_id=task.task_id, attempt_id=attempt.attempt_id
            )
            self.assertIsNotNone(transaction)
            assert transaction is not None
            self.assertTrue(transaction.complete)
            commit = _git(repo.root, "rev-parse", "main")
            trailers = wtam._commit_trailers(repo.root, commit)
            for key in (
                "Blackdog-Task",
                "Blackdog-Attempt",
                "Blackdog-Landing-Transaction",
                "Blackdog-Target-Base-Commit",
                "Blackdog-Source-Commit",
                "Blackdog-Source-Tree",
                "Blackdog-Execution-Prompt-SHA256",
                "Blackdog-Request-Prompt-SHA256",
                "Blackdog-Validation",
            ):
                self.assertIn(key, trailers)
            self.assertFalse(repo.worktree.exists())
            state = load_runtime_state(repo.profile.paths)
            terminal = task_record(state, task.task_id)
            self.assertIsNotNone(terminal)
            assert terminal is not None
            self.assertEqual(terminal.status, TASK_STATUS_DONE)
            self.assertEqual(terminal.attempts[-1].status, ATTEMPT_STATUS_SUCCESS)
        finally:
            repo.close()

    def test_partial_transition_rejects_modified_retry_and_repairs_exact_request(self) -> None:
        repo = ProductRepo("transition-exact-retry")
        original = core_tasks.append_event_once

        def exercise(
            *,
            event_summary: str,
            invoke,
            mismatch,
        ) -> None:
            failed = False

            def fail_owned_event_before_write(*args, **kwargs):
                nonlocal failed
                if kwargs.get("event_type") == "task.transition" and not failed:
                    failed = True
                    raise OSError("fault before owned transition event")
                return original(*args, **kwargs)

            with patch.object(
                core_tasks,
                "append_event_once",
                side_effect=fail_owned_event_before_write,
            ):
                partial = invoke(event_summary)
            self.assertEqual(partial.operation_status, "partial")
            self.assertTrue(partial.mutation_started)
            before_mismatch = repo.snapshot()
            conflict = mismatch()
            self.assertEqual(conflict.operation_status, "blocked")
            self.assertEqual(
                conflict.next_action.action_id,
                "task_transition_retry_conflict",
            )
            self.assertEqual(
                conflict.next_action.required_inputs,
                ("exact_transition_request",),
            )
            self.assertFalse(conflict.mutation_started)
            self.assertEqual(repo.snapshot(), before_mismatch)
            repaired = invoke(event_summary)
            self.assertEqual(repaired.operation_status, "succeeded")

        try:
            task, _attempt = repo.start(dirty=False)
            wtam.close_task(
                repo.profile,
                task_id=task.task_id,
                actor=repo.actor,
                status="blocked",
                summary="Prepare transition exactness fixture",
                validations=(ValidationRecord("unit", "passed"),),
                cleanup=False,
            )
            exercise(
                event_summary="Exact cancel request",
                invoke=lambda summary: wtam.cancel_task(
                    repo.profile,
                    task_id=task.task_id,
                    actor=repo.actor,
                    summary=summary,
                    failure_class="unknown",
                    recovery_action="inspect",
                ),
                mismatch=lambda: wtam.cancel_task(
                    repo.profile,
                    task_id=task.task_id,
                    actor=repo.actor,
                    summary="Modified cancel request",
                    failure_class="unknown",
                    recovery_action="inspect",
                ),
            )
            self.assertEqual(
                task_record(load_runtime_state(repo.profile.paths), task.task_id).status,
                TASK_STATUS_CANCELED,
            )
            exercise(
                event_summary="Exact reopen request",
                invoke=lambda summary: wtam.reopen_task(
                    repo.profile,
                    task_id=task.task_id,
                    actor=repo.actor,
                    summary=summary,
                ),
                mismatch=lambda: wtam.reopen_task(
                    repo.profile,
                    task_id=task.task_id,
                    actor=repo.actor,
                    summary="Modified reopen request",
                ),
            )
            self.assertEqual(
                task_record(load_runtime_state(repo.profile.paths), task.task_id).status,
                TASK_STATUS_PLANNED,
            )
        finally:
            repo.close()

    def test_landing_fault_after_target_side_effect_converges(self) -> None:
        for after_record in (False, True):
            repo = ProductRepo(f"land-target-{after_record}")
            original = wtam._record_phase
            failed = False

            def fault(profile, intent, phase, data):
                nonlocal failed
                if phase == "target_updated" and not failed:
                    failed = True
                    if after_record:
                        original(profile, intent, phase, data)
                    raise OSError("target phase fault")
                return original(profile, intent, phase, data)

            try:
                task, _attempt = repo.start()
                kwargs = {
                    "task_id": task.task_id,
                    "actor": repo.actor,
                    "summary": "Converge target side effect",
                    "validations": (ValidationRecord("unit", "passed"),),
                    "cleanup": True,
                }
                with patch.object(wtam, "_record_phase", side_effect=fault):
                    first = wtam.land_task(repo.profile, **kwargs)
                self.assertEqual(first.operation_status, "partial")
                self.assertEqual(first.next_action.action_id, "resume_landing_transaction")
                landed = _git(repo.root, "rev-parse", "main")
                second = wtam.land_task(repo.profile, **kwargs)
                self.assertEqual(second.operation_status, "succeeded")
                self.assertEqual(_git(repo.root, "rev-parse", "main"), landed)
            finally:
                repo.close()

    def test_task_rooted_landing_partial_retry_uses_surviving_primary_root(self) -> None:
        repo = ProductRepo("land-task-root-retry")
        original = wtam._record_phase
        failed = False

        def lose_target_receipt(profile, intent, phase, data):
            nonlocal failed
            if phase == "target_updated" and not failed:
                failed = True
                raise OSError("fault after target compare-and-swap")
            return original(profile, intent, phase, data)

        try:
            task, _attempt = repo.start()
            primary_launcher = install_runtime(repo.profile)
            task_profile = load_profile(repo.worktree)
            kwargs = {
                "task_id": task.task_id,
                "actor": repo.actor,
                "summary": "Retain a valid landing retry root",
                "validations": (ValidationRecord("unit", "passed"),),
                "cleanup": True,
            }
            with patch.object(wtam, "_record_phase", side_effect=lose_target_receipt):
                partial = wtam.land_task(
                    task_profile,
                    cwd=repo.worktree,
                    **kwargs,
                )

            self.assertEqual(partial.operation_status, "partial")
            self.assertEqual(partial.next_action.action_id, "resume_landing_transaction")
            self.assertEqual(
                Path(partial.next_action.argv[0]).resolve(),
                primary_launcher.resolve(),
            )
            retry_root_arg = next(
                item for item in partial.next_action.argv if item.startswith("--project-root=")
            )
            retry_root = Path(retry_root_arg.partition("=")[2]).resolve()
            self.assertEqual(retry_root, repo.root.resolve())
            self.assertNotEqual(retry_root, repo.worktree.resolve())
            self.assertTrue(repo.root.is_dir())
            self.assertTrue(repo.worktree.is_dir())

            completed = wtam.land_task(
                load_profile(repo.root),
                cwd=repo.root,
                **kwargs,
            )
            self.assertEqual(completed.operation_status, "succeeded")
            self.assertFalse(repo.worktree.exists())
        finally:
            repo.close()

    def test_post_cas_retry_accepts_landed_commit_below_later_target_advance(self) -> None:
        repo = ProductRepo("land-target-advance")
        original = wtam._record_phase
        failed = False

        def lose_target_receipt(profile, intent, phase, data):
            nonlocal failed
            if phase == "target_updated" and not failed:
                failed = True
                raise OSError("fault after target compare-and-swap")
            return original(profile, intent, phase, data)

        try:
            task, attempt = repo.start()
            kwargs = {
                "task_id": task.task_id,
                "actor": repo.actor,
                "summary": "Accept later target advance",
                "validations": (ValidationRecord("unit", "passed"),),
                "cleanup": True,
            }
            with patch.object(wtam, "_record_phase", side_effect=lose_target_receipt):
                partial = wtam.land_task(repo.profile, **kwargs)
            self.assertEqual(partial.operation_status, "partial")
            landed_commit = _git(repo.root, "rev-parse", "main")
            (repo.root / "later.txt").write_text("later valid target work\n", encoding="utf-8")
            _git(repo.root, "add", "later.txt")
            _git(repo.root, "commit", "-m", "Advance target after landing")
            advanced = _git(repo.root, "rev-parse", "main")

            completed = wtam.land_task(repo.profile, **kwargs)
            self.assertEqual(completed.operation_status, "succeeded")
            self.assertEqual(completed["landed_commit"], landed_commit)
            self.assertEqual(_git(repo.root, "rev-parse", "main"), advanced)
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(repo.root), "merge-base", "--is-ancestor", landed_commit, advanced],
                    check=False,
                ).returncode,
                0,
            )
            transaction = load_landing_transaction(
                repo.profile,
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
            )
            self.assertIsNotNone(transaction)
            assert transaction is not None
            self.assertTrue(transaction.complete)
        finally:
            repo.close()

    def test_durable_pre_target_abort_retry_runs_only_abort_finalization(self) -> None:
        repo = ProductRepo("abort-only-retry")
        original_phase = wtam._record_phase
        original_cleanup = wtam.record_landing_abort_cleanup
        dirtied = False
        cleanup_failed = False

        def dirty_after_canonical(profile, intent, phase, data):
            nonlocal dirtied
            result = original_phase(profile, intent, phase, data)
            if phase == "canonical_commit_created" and not dirtied:
                dirtied = True
                (repo.root / "target-dirty.txt").write_text("block target update\n", encoding="utf-8")
            return result

        def fail_abort_cleanup_once(*args, **kwargs):
            nonlocal cleanup_failed
            if not cleanup_failed:
                cleanup_failed = True
                raise OSError("fault after durable abort intent")
            return original_cleanup(*args, **kwargs)

        try:
            task, attempt = repo.start()
            kwargs = {
                "task_id": task.task_id,
                "actor": repo.actor,
                "summary": "Abort before target update",
                "validations": (ValidationRecord("unit", "passed"),),
                "cleanup": True,
            }
            target_before = _git(repo.root, "rev-parse", "main")
            with patch.object(wtam, "_record_phase", side_effect=dirty_after_canonical), patch.object(
                wtam,
                "record_landing_abort_cleanup",
                side_effect=fail_abort_cleanup_once,
            ):
                partial = wtam.land_task(repo.profile, **kwargs)
            self.assertEqual(partial.operation_status, "partial")
            self.assertEqual(partial.next_action.action_id, "resume_landing_abort")
            transaction = load_landing_transaction(
                repo.profile,
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
            )
            self.assertIsNotNone(transaction)
            assert transaction is not None
            self.assertTrue(transaction.abort_requested)
            self.assertFalse(transaction.abort_complete)
            self.assertEqual(_git(repo.root, "rev-parse", "main"), target_before)

            with patch.object(
                wtam,
                "_run_landing_transaction",
                side_effect=AssertionError("forward landing must not run after durable abort"),
            ):
                completed_abort = wtam.land_task(repo.profile, **kwargs)
            self.assertEqual(completed_abort.operation_status, "blocked")
            self.assertEqual(completed_abort["landing_transaction"]["abort_completion"]["outcome"], "blocked_before_target_update")
            self.assertEqual(_git(repo.root, "rev-parse", "main"), target_before)
            self.assertTrue(repo.worktree.is_dir())
            replay = wtam.land_task(repo.profile, **kwargs)
            self.assertEqual(replay.operation_status, "blocked")
            self.assertEqual(replay.next_action.action_id, "cancel_inactive_blocked_task")
            self.assertEqual(_git(repo.root, "rev-parse", "main"), target_before)
        finally:
            repo.close()

    def test_reconciliation_fault_after_core_finalization_is_typed_and_converges(self) -> None:
        repo = ProductRepo("reconcile-core-fault")
        original = wtam._record_phase
        stop_after_target = False

        def temporary_cleanup_fault(profile, intent, phase, data):
            nonlocal stop_after_target
            if phase == "temporary_cleanup_complete" and not stop_after_target:
                stop_after_target = True
                raise OSError("temporary cleanup phase fault")
            return original(profile, intent, phase, data)

        try:
            task, attempt = repo.start()
            landing_kwargs = {
                "task_id": task.task_id,
                "actor": repo.actor,
                "summary": "Reconcile exact target update",
                "validations": (ValidationRecord("unit", "passed"),),
                "cleanup": True,
            }
            with patch.object(wtam, "_record_phase", side_effect=temporary_cleanup_fault):
                stopped = wtam.land_task(repo.profile, **landing_kwargs)
            self.assertEqual(stopped.operation_status, "partial")
            transaction = load_landing_transaction(
                repo.profile, task_id=task.task_id, attempt_id=attempt.attempt_id
            )
            self.assertIsNotNone(transaction)
            assert transaction is not None
            self.assertEqual(transaction.last_phase, "target_updated")
            landed_commit = _git(repo.root, "rev-parse", "main")
            (repo.root / "post-landing.txt").write_text("later target advance\n", encoding="utf-8")
            _git(repo.root, "add", "post-landing.txt")
            _git(repo.root, "commit", "-m", "Advance target before reconciliation")
            advanced_target = _git(repo.root, "rev-parse", "main")

            core_finalized = False

            def runtime_phase_fault(profile, intent, phase, data):
                nonlocal core_finalized
                if phase == "runtime_finalized" and not core_finalized:
                    core_finalized = True
                    raise wtam.LandingTransactionError("runtime phase fault")
                return original(profile, intent, phase, data)

            with patch.object(wtam, "_record_phase", side_effect=runtime_phase_fault):
                partial = wtam.reconcile_task_landing(
                    repo.profile,
                    task_id=task.task_id,
                    attempt_id=attempt.attempt_id,
                    landed_commit=landed_commit,
                    actor=repo.actor,
                    apply=True,
                    reason="recover injected core boundary",
                )
            self.assertEqual(partial.operation_status, "partial")
            self.assertEqual(partial.next_action.action_id, "apply_landing_reconciliation")
            self.assertTrue(partial.mutation_started)
            self.assertEqual(partial.mutation_phase, "landing_runtime_finalized")
            self.assertTrue(partial["post_cas_proven"])
            self.assertTrue(partial["runtime_reconciled"])
            self.assertIn("--apply", partial.next_action.argv)
            self.assertIn("--reason=recover injected core boundary", partial.next_action.argv)
            current = task_record(load_runtime_state(repo.profile.paths), task.task_id)
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(current.status, TASK_STATUS_DONE)

            completed = wtam.reconcile_task_landing(
                repo.profile,
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                landed_commit=landed_commit,
                actor=repo.actor,
                apply=True,
                reason="recover injected core boundary",
            )
            self.assertEqual(completed.operation_status, "succeeded")
            self.assertEqual(_git(repo.root, "rev-parse", "main"), advanced_target)
            final = load_landing_transaction(
                repo.profile, task_id=task.task_id, attempt_id=attempt.attempt_id
            )
            self.assertIsNotNone(final)
            assert final is not None
            self.assertTrue(final.complete)
            snapshot = repo.snapshot()
            replay = wtam.reconcile_task_landing(
                repo.profile,
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                landed_commit=landed_commit,
                actor=repo.actor,
                apply=True,
                reason="recover injected core boundary",
            )
            self.assertEqual(replay.operation_status, "succeeded")
            self.assertEqual(repo.snapshot(), snapshot)
        finally:
            repo.close()

    def test_abort_cleanup_nonzero_commands_remain_partial_until_resources_are_absent(self) -> None:
        repo = ProductRepo("abort-cleanup-proof")
        original_phase = wtam._record_phase
        original_no_check = wtam._run_git_no_check
        dirtied = False

        def dirty_after_canonical(profile, intent, phase, data):
            nonlocal dirtied
            result = original_phase(profile, intent, phase, data)
            if phase == "canonical_commit_created" and not dirtied:
                dirtied = True
                (repo.root / "target-dirty.txt").write_text(
                    "block target update\n",
                    encoding="utf-8",
                )
            return result

        def refuse_abort_cleanup(root, *args):
            if args[:3] == ("worktree", "remove", "--force"):
                return subprocess.CompletedProcess(
                    ["git", *args],
                    1,
                    "",
                    "forced worktree cleanup failure",
                )
            if args[:2] == ("branch", "-D") and str(args[-1]).startswith("blackdog/land-"):
                return subprocess.CompletedProcess(
                    ["git", *args],
                    1,
                    "",
                    "forced branch cleanup failure",
                )
            return original_no_check(root, *args)

        try:
            task, attempt = repo.start()
            kwargs = {
                "task_id": task.task_id,
                "actor": repo.actor,
                "summary": "Prove abort cleanup convergence",
                "validations": (ValidationRecord("unit", "passed"),),
                "cleanup": True,
            }
            target_before = _git(repo.root, "rev-parse", "main")
            with patch.object(wtam, "_record_phase", side_effect=dirty_after_canonical), patch.object(
                wtam,
                "_run_git_no_check",
                side_effect=refuse_abort_cleanup,
            ):
                partial = wtam.land_task(repo.profile, **kwargs)
            self.assertEqual(partial.operation_status, "partial")
            self.assertEqual(partial.next_action.action_id, "resume_landing_abort")
            transaction = load_landing_transaction(
                repo.profile,
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
            )
            self.assertIsNotNone(transaction)
            assert transaction is not None
            self.assertTrue(transaction.abort_requested)
            self.assertFalse(transaction.abort_cleanup_complete)
            self.assertFalse(transaction.abort_runtime_finalized)
            self.assertFalse(transaction.abort_complete)
            self.assertEqual(_git(repo.root, "rev-parse", "main"), target_before)

            with patch.object(
                wtam,
                "_run_landing_transaction",
                side_effect=AssertionError("abort retry must not execute forward landing"),
            ):
                converged = wtam.land_task(repo.profile, **kwargs)
            self.assertEqual(converged.operation_status, "blocked")
            self.assertEqual(converged.next_action.action_id, "cancel_inactive_blocked_task")
            cleanup = converged["landing_transaction"]["abort_cleanup"]
            self.assertTrue(cleanup["temporary_worktree_removed"])
            self.assertTrue(cleanup["temporary_worktree_registration_removed"])
            self.assertTrue(cleanup["temporary_branch_removed"])
            self.assertEqual(_git(repo.root, "rev-parse", "main"), target_before)
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
