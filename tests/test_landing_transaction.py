from __future__ import annotations

import json

from blackdog.landing import (
    LANDING_EVENT_SCHEMA_VERSION,
    LANDING_PHASE_EVENT_TYPE,
    LandingIntent,
    LandingTransactionError,
    landing_phase_event_id,
    load_landing_transaction,
    record_landing_abort,
    record_landing_abort_cleanup,
    record_landing_abort_superseded,
    record_landing_phase,
    strict_json_equal,
)
from blackdog_core.state import append_event
from tests.core_audit_support import CoreAuditTestCase


class LandingLedgerTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile("Landing ledger")
        self.profile = self.load_test_profile()
        self.intent = LandingIntent(
            workset_id="ledger-workset",
            task_id="LEDGER-1",
            attempt_id="LEDGER-1-attempt",
            actor="codex",
            branch="agent/ledger",
            target_branch="main",
            worktree_path=str(self.root.parent / "ledger-source"),
            primary_worktree=str(self.root),
            target_base_commit=self.git_output("rev-parse", "HEAD"),
            source_head_commit=self.git_output("rev-parse", "HEAD"),
            source_fingerprint="source-fingerprint",
            expected_source_tree_hash="source-tree-hash",
            source_dirty=True,
            summary="land the ledger change",
            note=None,
            validations=(("unit", "passed"),),
            residuals=("retained source",),
            followup_candidates=("retry",),
            changed_paths=("ledger.txt",),
            cleanup=True,
            commit_message="ledger commit\n",
            temporary_worktree_path=str(self.root.parent / "ledger-temporary"),
        )

    def _load(self):
        return load_landing_transaction(
            self.profile,
            workset_id=self.intent.workset_id,
            task_id=self.intent.task_id,
            attempt_id=self.intent.attempt_id,
        )

    def _event_rows(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.profile.paths.events_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _write_event_rows(self, rows: list[dict[str, object]]) -> None:
        self.profile.paths.events_file.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_intent_rejects_bool_integer_coercion(self) -> None:
        record_landing_phase(
            self.profile,
            intent=self.intent,
            phase="intent_recorded",
            data=self.intent.to_dict(),
        )
        rows = self._event_rows()
        rows[0]["payload"]["data"]["source_dirty"] = 1
        self._write_event_rows(rows)

        with self.assertRaisesRegex(
            LandingTransactionError,
            "source_dirty and cleanup values must be booleans",
        ):
            self._load()
        self.assertFalse(strict_json_equal(True, 1))
        self.assertFalse(strict_json_equal(1, 1.0))

    def test_canonical_phase_identity_collision_is_corruption(self) -> None:
        append_event(
            self.profile.paths.events_file,
            event_id=landing_phase_event_id(
                self.intent.transaction_id,
                "intent_recorded",
            ),
            event_type="unrelated.event",
            actor=self.intent.actor,
            payload={"transaction_id": self.intent.transaction_id},
        )
        with self.assertRaisesRegex(
            LandingTransactionError,
            "occupied by conflicting content",
        ):
            self._load()

    def test_phase_after_abort_requires_ordered_supersession(self) -> None:
        record_landing_phase(
            self.profile,
            intent=self.intent,
            phase="intent_recorded",
            data=self.intent.to_dict(),
        )
        record_landing_abort(
            self.profile,
            intent=self.intent,
            data={"proof": "abort"},
        )
        append_event(
            self.profile.paths.events_file,
            event_id=landing_phase_event_id(
                self.intent.transaction_id,
                "source_prepared",
            ),
            event_type=LANDING_PHASE_EVENT_TYPE,
            actor=self.intent.actor,
            payload={
                "schema_version": LANDING_EVENT_SCHEMA_VERSION,
                "transaction_id": self.intent.transaction_id,
                "workset_id": self.intent.workset_id,
                "task_id": self.intent.task_id,
                "attempt_id": self.intent.attempt_id,
                "phase": "source_prepared",
                "data": {"source_commit": "candidate"},
            },
        )
        with self.assertRaisesRegex(
            LandingTransactionError,
            "phase occurs after abort without supersession",
        ):
            self._load()

    def test_supersession_must_follow_abort_cleanup(self) -> None:
        record_landing_phase(
            self.profile,
            intent=self.intent,
            phase="intent_recorded",
            data=self.intent.to_dict(),
        )
        record_landing_abort(
            self.profile,
            intent=self.intent,
            data={"proof": "abort"},
        )
        record_landing_abort_cleanup(
            self.profile,
            intent=self.intent,
            data={"cleanup": True},
        )
        record_landing_abort_superseded(
            self.profile,
            intent=self.intent,
            data={"superseded": True},
        )
        rows = self._event_rows()
        rows[-2], rows[-1] = rows[-1], rows[-2]
        self._write_event_rows(rows)

        with self.assertRaisesRegex(
            LandingTransactionError,
            "abort supersession is reordered",
        ):
            self._load()


if __name__ == "__main__":
    import unittest

    unittest.main()
