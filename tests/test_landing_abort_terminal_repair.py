from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import blackdog.landing as landing
import blackdog.wtam as wtam
from blackdog_core.backlog import BacklogError
from blackdog_core.state import ATTEMPT_STATUS_BLOCKED, ATTEMPT_STATUS_SUCCESS
from tests.test_landing_transaction_faults import LandingRepo, _run_git


def _event_rows(repo: LandingRepo) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in repo.profile.paths.events_file.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


def _replace_event_rows(
    repo: LandingRepo,
    rows: list[dict[str, object]],
) -> None:
    repo.profile.paths.events_file.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


class LandingAbortTerminalRepairTests(unittest.TestCase):
    maxDiff = None

    def _abort_complete(
        self,
        repo: LandingRepo,
    ) -> tuple[bytes, landing.LandingTransaction]:
        blocker = wtam.StaleTaskBranchError(
            branch=repo.branch,
            target_branch="main",
            branch_worktree=repo.worktree,
        )
        with patch.object(wtam, "_update_landing_target", side_effect=blocker):
            interrupted = repo.land()
        self.assertEqual(interrupted.operation_status, "partial")
        pre_finalization_runtime = repo.profile.paths.runtime_file.read_bytes()

        closed = repo.close_attempt()
        self.assertEqual(closed.operation_status, "succeeded", closed.to_dict())
        transaction = repo.transaction()
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertTrue(transaction.abort_complete)
        self.assertEqual(repo.latest_attempt().status, ATTEMPT_STATUS_BLOCKED)
        return pre_finalization_runtime, transaction

    def _remove_close_receipt(
        self,
        repo: LandingRepo,
        transaction: landing.LandingTransaction,
    ) -> None:
        close_event_id = wtam._landing_abort_close_event_id(
            transaction.transaction_id
        )
        rows = [
            row
            for row in _event_rows(repo)
            if row.get("event_id") != close_event_id
        ]
        _replace_event_rows(repo, rows)

    def test_terminal_retry_repairs_missing_close_receipt_once(self) -> None:
        repo = LandingRepo(suffix="abort-repair-close")
        try:
            _runtime, transaction = self._abort_complete(repo)
            self._remove_close_receipt(repo, transaction)

            repaired = repo.close_attempt()
            self.assertEqual(repaired.operation_status, "succeeded", repaired.to_dict())
            self.assertTrue(repaired.mutation_started, repaired.to_dict())
            self.assertTrue(repaired.mutation_completed, repaired.to_dict())
            close_event_id = wtam._landing_abort_close_event_id(
                transaction.transaction_id
            )
            self.assertEqual(
                sum(row.get("event_id") == close_event_id for row in _event_rows(repo)),
                1,
            )

            repaired_snapshot = repo.snapshot()
            exact_retry = repo.close_attempt()
            self.assertEqual(exact_retry.operation_status, "succeeded")
            self.assertFalse(exact_retry.mutation_started, exact_retry.to_dict())
            self.assertFalse(exact_retry.mutation_completed, exact_retry.to_dict())
            self.assertEqual(repo.snapshot(), repaired_snapshot)
        finally:
            repo.close()

    def test_terminal_retry_rejects_conflicting_close_receipt(self) -> None:
        repo = LandingRepo(suffix="abort-conflict-close")
        try:
            _runtime, transaction = self._abort_complete(repo)
            close_event_id = wtam._landing_abort_close_event_id(
                transaction.transaction_id
            )
            rows = _event_rows(repo)
            conflicting = next(
                row for row in rows if row.get("event_id") == close_event_id
            )
            payload = conflicting.get("payload")
            assert isinstance(payload, dict)
            payload["summary"] = "conflicting close receipt"
            _replace_event_rows(repo, rows)
            before_events = repo.profile.paths.events_file.read_bytes()
            before_runtime = repo.profile.paths.runtime_file.read_bytes()

            with self.assertRaisesRegex(
                landing.LandingTransactionError,
                "close event conflicts with durable close request",
            ):
                repo.close_attempt()
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), before_events)
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), before_runtime)
        finally:
            repo.close()

    def test_terminal_retry_repairs_rolled_back_runtime_and_owned_events(self) -> None:
        repo = LandingRepo(suffix="abort-repair-runtime")
        try:
            pre_finalization_runtime, transaction = self._abort_complete(repo)
            assert transaction.abort_data is not None
            request = transaction.abort_data["close_request"]
            assert isinstance(request, dict)
            finalization_id = request["finalization_id"]
            owned_types = {"task.release", "workset.release", "task.finish"}
            rows = [
                row
                for row in _event_rows(repo)
                if not (
                    row.get("type") in owned_types
                    and isinstance(row.get("payload"), dict)
                    and row["payload"].get("finalization_id") == finalization_id
                )
            ]
            _replace_event_rows(repo, rows)
            repo.profile.paths.runtime_file.write_bytes(pre_finalization_runtime)

            repaired = repo.close_attempt()
            self.assertEqual(repaired.operation_status, "succeeded", repaired.to_dict())
            self.assertTrue(repaired.mutation_started, repaired.to_dict())
            self.assertTrue(repaired.mutation_completed, repaired.to_dict())
            self.assertEqual(repo.latest_attempt().status, ATTEMPT_STATUS_BLOCKED)
            repaired_rows = _event_rows(repo)
            for event_type in ("task.release", "workset.release", "task.finish"):
                self.assertEqual(
                    sum(
                        row.get("type") == event_type
                        and isinstance(row.get("payload"), dict)
                        and row["payload"].get("finalization_id") == finalization_id
                        for row in repaired_rows
                    ),
                    1,
                    event_type,
                )

            repaired_snapshot = repo.snapshot()
            exact_retry = repo.close_attempt()
            self.assertFalse(exact_retry.mutation_started, exact_retry.to_dict())
            self.assertFalse(exact_retry.mutation_completed, exact_retry.to_dict())
            self.assertEqual(repo.snapshot(), repaired_snapshot)
        finally:
            repo.close()

    def test_terminal_retry_rejects_incompatible_terminal_runtime(self) -> None:
        repo = LandingRepo(suffix="abort-conflict-runtime")
        try:
            _runtime, _transaction = self._abort_complete(repo)
            runtime_payload = json.loads(
                repo.profile.paths.runtime_file.read_text(encoding="utf-8")
            )
            attempt_payload = next(
                attempt
                for workset in runtime_payload["worksets"]
                if workset["id"] == repo.workset_id
                for attempt in workset["attempts"]
                if attempt["attempt_id"] == repo.attempt.attempt_id
            )
            attempt_payload["status"] = "failed"
            repo.profile.paths.runtime_file.write_text(
                json.dumps(runtime_payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
                encoding="utf-8",
            )
            before_events = repo.profile.paths.events_file.read_bytes()
            before_runtime = repo.profile.paths.runtime_file.read_bytes()

            with self.assertRaisesRegex(
                BacklogError,
                "finalization retry conflicts with terminal runtime state",
            ):
                repo.close_attempt()
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), before_events)
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), before_runtime)
        finally:
            repo.close()

    def test_reconciled_abort_retry_preserves_success_while_repairing_receipt(self) -> None:
        repo = LandingRepo(suffix="abort-reconciled-repair")
        try:
            _runtime, transaction = self._abort_complete(repo)
            assert transaction.abort_data is not None
            candidate = transaction.abort_data["landed_commit"]
            assert isinstance(candidate, str)
            _run_git(repo.root, "merge", "--ff-only", candidate)
            reconciled = wtam.reconcile_task_landing(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=repo.attempt.attempt_id,
                landed_commit=candidate,
                actor=repo.actor,
                apply=True,
                reason="prove reconciled abort repair",
            )
            self.assertEqual(reconciled.operation_status, "succeeded", reconciled.to_dict())
            self.assertEqual(repo.latest_attempt().status, ATTEMPT_STATUS_SUCCESS)
            self._remove_close_receipt(repo, transaction)

            repaired = repo.close_attempt()
            self.assertEqual(repaired.operation_status, "succeeded", repaired.to_dict())
            self.assertTrue(repaired.mutation_started, repaired.to_dict())
            self.assertTrue(repaired.legacy_payload["abort_reconciled"])
            self.assertEqual(repo.latest_attempt().status, ATTEMPT_STATUS_SUCCESS)

            repaired_snapshot = repo.snapshot()
            exact_retry = repo.close_attempt()
            self.assertFalse(exact_retry.mutation_started, exact_retry.to_dict())
            self.assertEqual(repo.snapshot(), repaired_snapshot)
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
