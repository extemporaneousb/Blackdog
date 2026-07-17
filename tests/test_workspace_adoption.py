from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import blackdog.wtam as wtam
import blackdog_core.backlog as backlog
from blackdog_core.backlog import BacklogError, finish_task, start_task, upsert_workset
from blackdog_core.state import (
    create_prompt_receipt,
    load_events,
    load_runtime_state,
    save_runtime_state,
)
from tests.core_audit_support import CoreAuditTestCase
from tests.test_landing_transaction_faults import LandingRepo, _git_output


class WorkspaceAdoptionContractTests(CoreAuditTestCase):
    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        self.write_profile("Workspace adoption contracts")
        self.profile = self.load_test_profile()

    def _put_workset(self, *, workset_id: str, task_ids: tuple[str, ...]) -> None:
        upsert_workset(
            self.profile,
            {
                "id": workset_id,
                "title": f"Exercise {workset_id}",
                "branch_intent": {
                    "target_branch": "main",
                    "integration_branch": "main",
                },
                "tasks": [
                    {
                        "id": task_id,
                        "title": f"Exercise {task_id}",
                        "intent": f"prove {task_id} runtime behavior",
                    }
                    for task_id in task_ids
                ],
            },
        )

    def _start_then_block(self, *, workset_id: str, task_id: str, actor: str):
        receipt = create_prompt_receipt(
            f"Exercise the predecessor for {task_id}.",
            source=f"unit:{task_id}",
        )
        attempt = start_task(
            self.profile,
            workset_id=workset_id,
            task_id=task_id,
            actor=actor,
            prompt_receipt=receipt,
        )
        finish_task(
            self.profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt.attempt_id,
            actor=actor,
            status="blocked",
            summary="Create a terminal predecessor for deterministic restart.",
        )
        return attempt

    def _atomic_start_guards(self, *, workset_id: str, predecessor) -> dict[str, str]:
        runtime = load_runtime_state(self.profile.paths)
        task_state = next(
            state
            for workset in runtime.worksets
            if workset.workset_id == workset_id
            for state in workset.task_states
            if state.task_id == predecessor.task_id
        )
        assert predecessor.prompt_receipt is not None
        assert predecessor.user_prompt_receipt is not None
        assert predecessor.prompt_receipt.mode is not None
        assert predecessor.user_prompt_receipt.mode is not None
        assert task_state.updated_at is not None
        return {
            "atomic_start_kind": "adoption",
            "expected_task_actor": task_state.actor or predecessor.actor,
            "expected_execution_prompt_hash": predecessor.prompt_receipt.prompt_hash,
            "expected_execution_prompt_mode": predecessor.prompt_receipt.mode,
            "expected_request_prompt_hash": predecessor.user_prompt_receipt.prompt_hash,
            "expected_request_prompt_mode": predecessor.user_prompt_receipt.mode,
            "expected_task_updated_at": task_state.updated_at,
        }

    def test_ordinary_start_emits_one_normal_workset_claim(self) -> None:
        self._put_workset(workset_id="ordinary", task_ids=("ORD-1",))

        attempt = start_task(
            self.profile,
            workset_id="ordinary",
            task_id="ORD-1",
            actor="codex",
            prompt_receipt=create_prompt_receipt(
                "Exercise an ordinary task start with deterministic evidence.",
                source="unit:ordinary",
            ),
        )

        claims = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("type") == "workset.claim"
            and event.get("payload", {}).get("workset_id") == "ordinary"
        ]
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["actor"], "codex")
        self.assertEqual(
            claims[0]["event_id"],
            backlog._task_start_event_id(
                attempt_id=attempt.attempt_id,
                event_type="workset.claim",
            ),
        )

    def test_deterministic_start_rejects_incompatible_reusable_workset_claim(self) -> None:
        self._put_workset(
            workset_id="claim-compatibility",
            task_ids=("CLAIM-1", "CLAIM-2"),
        )
        predecessor = self._start_then_block(
            workset_id="claim-compatibility",
            task_id="CLAIM-2",
            actor="owner",
        )
        start_task(
            self.profile,
            workset_id="claim-compatibility",
            task_id="CLAIM-1",
            actor="owner",
            prompt_receipt=create_prompt_receipt(
                "Hold the reusable workset claim.",
                source="unit:claim-holder",
            ),
        )
        successor_receipt = predecessor.prompt_receipt
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()

        guards = self._atomic_start_guards(
            workset_id="claim-compatibility",
            predecessor=predecessor,
        )
        with self.assertRaisesRegex(BacklogError, "task actor or runtime generation"):
            start_task(
                self.profile,
                workset_id="claim-compatibility",
                task_id="CLAIM-2",
                actor="other",
                prompt_receipt=successor_receipt,
                attempt_id="CLAIM-2-deterministic",
                expected_predecessor_attempt_id=predecessor.attempt_id,
                **guards,
            )
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

        with patch.object(
            backlog,
            "EXECUTION_MODELS",
            frozenset({"direct_wtam", "alternate-test-model"}),
        ):
            with self.assertRaisesRegex(
                BacklogError,
                "already claimed for execution_model 'direct_wtam'",
            ):
                start_task(
                    self.profile,
                    workset_id="claim-compatibility",
                    task_id="CLAIM-2",
                    actor="owner",
                    execution_model="alternate-test-model",
                    prompt_receipt=successor_receipt,
                    attempt_id="CLAIM-2-deterministic",
                    expected_predecessor_attempt_id=predecessor.attempt_id,
                    **guards,
                )
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

    def test_deterministic_retry_repairs_missing_start_event_then_byte_noops(self) -> None:
        self._put_workset(workset_id="event-repair", task_ids=("REPAIR-1",))
        predecessor = self._start_then_block(
            workset_id="event-repair",
            task_id="REPAIR-1",
            actor="codex",
        )
        receipt = predecessor.prompt_receipt
        start_kwargs = {
            "profile": self.profile,
            "workset_id": "event-repair",
            "task_id": "REPAIR-1",
            "actor": "codex",
            "prompt_receipt": receipt,
            "setup_receipt": {"handler": "validated"},
            "attempt_id": "REPAIR-1-deterministic",
            "expected_predecessor_attempt_id": predecessor.attempt_id,
            **self._atomic_start_guards(
                workset_id="event-repair",
                predecessor=predecessor,
            ),
        }
        original_append_once = backlog.append_event_once

        def fail_before_task_start(*args, **kwargs):
            if kwargs.get("event_type") == "task.start":
                raise OSError("fault before deterministic task.start")
            return original_append_once(*args, **kwargs)

        with patch.object(
            backlog,
            "append_event_once",
            side_effect=fail_before_task_start,
        ):
            with self.assertRaisesRegex(OSError, "fault before deterministic task.start"):
                start_task(**start_kwargs)

        first_events = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("payload", {}).get("attempt_id")
            == "REPAIR-1-deterministic"
        ]
        self.assertEqual(
            [event["type"] for event in first_events],
            ["task.claim"],
        )
        runtime_after_fault = load_runtime_state(self.profile.paths)
        self.assertEqual(
            [
                attempt.attempt_id
                for workset in runtime_after_fault.worksets
                if workset.workset_id == "event-repair"
                for attempt in workset.attempts
            ],
            [predecessor.attempt_id, "REPAIR-1-deterministic"],
        )

        repaired = start_task(**start_kwargs)
        self.assertEqual(repaired.attempt_id, "REPAIR-1-deterministic")
        repaired_events = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("payload", {}).get("attempt_id")
            == repaired.attempt_id
        ]
        self.assertEqual(
            [event["type"] for event in repaired_events],
            ["task.claim", "task.start"],
        )
        self.assertEqual(
            sum(
                event.get("type") == "workset.claim"
                and event.get("payload", {}).get("workset_id") == "event-repair"
                and event.get("event_id")
                == backlog._task_start_event_id(
                    attempt_id=repaired.attempt_id,
                    event_type="workset.claim",
                )
                for event in load_events(self.profile.paths.events_file)
            ),
            1,
        )
        runtime_before_third = self.profile.paths.runtime_file.read_bytes()
        events_before_third = self.profile.paths.events_file.read_bytes()

        exact_retry = start_task(**start_kwargs)

        self.assertEqual(exact_retry, repaired)
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before_third)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before_third)

    def test_workspace_adoption_prompt_mismatches_do_not_mutate_runtime_or_events(self) -> None:
        repo = LandingRepo(suffix="adoption-prompt-mismatch")
        try:
            execution_path = repo.base / "execution-prompt.md"
            request_path = repo.base / "request-prompt.md"
            execution_text = "Execute the retained landing workspace exactly as requested."
            request_text = "Continue the distinct user request from the retained workspace."
            execution_path.write_text(execution_text + "\n", encoding="utf-8")
            request_path.write_text(request_text + "\n", encoding="utf-8")
            execution_receipt = create_prompt_receipt(
                execution_text,
                source=str(execution_path.resolve()),
            )
            request_receipt = create_prompt_receipt(
                request_text,
                source=str(request_path.resolve()),
            )
            self.assertNotEqual(
                execution_receipt.prompt_hash,
                request_receipt.prompt_hash,
            )
            runtime = load_runtime_state(repo.profile.paths)
            rewritten = replace(
                runtime,
                worksets=tuple(
                    replace(
                        workset,
                        attempts=tuple(
                            replace(
                                attempt,
                                prompt_receipt=execution_receipt,
                                user_prompt_receipt=request_receipt,
                            )
                            if attempt.attempt_id == repo.attempt.attempt_id
                            else attempt
                            for attempt in workset.attempts
                        ),
                    )
                    if workset.workset_id == repo.workset_id
                    else workset
                    for workset in runtime.worksets
                ),
            )
            save_runtime_state(repo.profile.paths, rewritten)

            blocker = wtam.StaleTaskBranchError(
                branch=repo.branch,
                target_branch="main",
                branch_worktree=repo.worktree,
            )
            with patch.object(wtam, "_update_landing_target", side_effect=blocker):
                interrupted = repo.land()
            self.assertEqual(interrupted.operation_status, "partial")
            closed = repo.close_attempt()
            self.assertEqual(closed.operation_status, "succeeded", closed.to_dict())
            transaction = repo.transaction()
            self.assertIsNotNone(transaction)
            assert transaction is not None
            self.assertTrue(transaction.abort_complete)
            assert transaction.abort_data is not None

            adoption_kwargs = {
                "profile": repo.profile,
                "actor": repo.actor,
                "prompt_source": str(execution_path.resolve()),
                "user_prompt_source": str(request_path.resolve()),
                "prompt_mode": "raw",
                "expected_actor": repo.actor,
                "expected_execution_prompt_hash": execution_receipt.prompt_hash,
                "expected_execution_prompt_mode": execution_receipt.mode,
                "expected_request_prompt_hash": request_receipt.prompt_hash,
                "expected_request_prompt_mode": request_receipt.mode,
                "adopt_aborted_landing_source": True,
                "expected_predecessor_attempt": repo.attempt.attempt_id,
                "expected_landing_transaction": transaction.transaction_id,
                "expected_source_commit": transaction.abort_data["source_commit"],
                "expected_source_tree": transaction.intent.expected_source_tree_hash,
                "expected_branch": transaction.intent.branch,
                "expected_path": transaction.intent.worktree_path,
                "expected_target_branch": transaction.intent.target_branch,
                "expected_target_commit": _git_output(repo.root, "rev-parse", "main"),
                "workset_id": repo.workset_id,
                "task_id": repo.task_id,
                "cwd": repo.root,
            }

            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            events_before = repo.profile.paths.events_file.read_bytes()
            with self.assertRaisesRegex(
                BacklogError,
                "actor or prompt lineage does not match",
            ):
                wtam.begin_task_worktree(
                    **adoption_kwargs,
                    prompt="Execute a different instruction in the retained workspace.",
                    user_prompt=request_text,
                )
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)

            with self.assertRaisesRegex(
                BacklogError,
                "actor or prompt lineage does not match",
            ):
                wtam.begin_task_worktree(
                    **adoption_kwargs,
                    prompt=execution_text,
                    user_prompt="Continue a different user request from this workspace.",
                )
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)
        finally:
            repo.close()

    def test_normal_preview_and_start_preserve_worktree_collision_guards(self) -> None:
        self._put_workset(workset_id="collision", task_ids=("COLLIDE-1",))
        branch = "agent/collision-contract"
        worktree_path = (
            self.root.parent / f"{self.root.name}-collision-worktree"
        ).resolve()
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree_path),
                "main",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            preview = wtam.preview_task_worktree(
                self.profile,
                workset_id="collision",
                task_id="COLLIDE-1",
                actor="codex",
                prompt="Exercise the existing worktree collision guards.",
                branch=branch,
                path=str(worktree_path),
                cwd=self.root,
            )
            self.assertFalse(preview.start_ready)
            self.assertIn(
                f"branch already has a worktree: {worktree_path}",
                preview.conflicts,
            )
            self.assertIn(
                f"worktree path already exists: {worktree_path}",
                preview.conflicts,
            )

            with self.assertRaisesRegex(
                wtam.WorktreeError,
                "branch already has a worktree.*worktree path already exists",
            ):
                wtam.start_task_worktree(
                    self.profile,
                    workset_id="collision",
                    task_id="COLLIDE-1",
                    actor="codex",
                    prompt="Exercise the existing worktree collision guards.",
                    branch=branch,
                    path=str(worktree_path),
                    cwd=self.root,
                )
        finally:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.root),
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )


if __name__ == "__main__":
    unittest.main()
