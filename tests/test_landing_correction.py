from __future__ import annotations

import hashlib
import json

from blackdog.landing_correction import (
    LANDING_CORRECTION_EVENT_TYPE,
    LANDING_CORRECTION_SCHEMA_VERSION,
    PHASE_BLOCKED,
    PHASE_HANDED_TO_LANDING,
    PHASE_INTENT_RECORDED,
    PHASE_REBASE_COMPLETED,
    PHASE_VALIDATION_COMPLETED,
    LandingCorrectionError,
    LandingCorrectionIntent,
    landing_correction_phase_event_id,
    load_landing_correction_selection,
    record_landing_correction_blocked,
    record_landing_correction_handed_to_landing,
    record_landing_correction_intent,
    record_landing_correction_phase,
    record_landing_correction_rebase_completed,
    record_landing_correction_validation_completed,
)
from blackdog.validation import ValidationCommandResult, ValidationRunResult
from blackdog_core.state import append_event, load_events

from core_audit_support import CoreAuditTestCase


class LandingCorrectionTest(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile()
        self.profile = self.load_test_profile()
        self.events_file = self.profile.paths.events_file
        self.intent = self._intent()

    def _digest(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _evidence_hash(self, value: object) -> str:
        rendered = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    def _intent(
        self,
        *,
        source_head_commit: str | None = None,
        target_commit: str | None = None,
        request_identity_hash: str | None = None,
        generation: int = 1,
    ) -> LandingCorrectionIntent:
        head = "1" * 40
        return LandingCorrectionIntent(
            workset_id="ws",
            task_id="task",
            attempt_id="attempt-1",
            actor="codex",
            branch="blackdog/task",
            target_branch="main",
            worktree_path=str(self.root),
            source_head_commit=source_head_commit or head,
            target_commit=target_commit or head,
            source_tree_hash=self._digest("source-tree"),
            request_identity_hash=(
                request_identity_hash or self._digest("request")
            ),
            validation_policy_hash=self._digest("validation-policy"),
            generation=generation,
        )

    def _validation(self, intent: LandingCorrectionIntent) -> None:
        validation = self._passed_validation(2)
        record_landing_correction_validation_completed(
            self.profile,
            intent=intent,
            source_head_commit=intent.source_head_commit,
            target_commit=intent.target_commit,
            source_tree_hash=intent.source_tree_hash,
            validation_evidence_hash=self._evidence_hash(validation.to_dict()),
            validation=validation,
        )

    def _passed_validation(self, count: int) -> ValidationRunResult:
        return ValidationRunResult(
            command_count=count,
            results=tuple(
                ValidationCommandResult(
                    index=index,
                    command_sha256=self._digest(f"command-{index}"),
                    status="passed",
                    returncode=0,
                    elapsed_ms=index + 1,
                    stdout_bytes=0,
                    stderr_bytes=0,
                )
                for index in range(count)
            ),
        )

    def _raw_payload(
        self,
        *,
        intent: LandingCorrectionIntent,
        phase: str,
        data: dict[str, object],
    ) -> dict[str, object]:
        return {
            "schema_version": LANDING_CORRECTION_SCHEMA_VERSION,
            "correction_id": intent.correction_id,
            "workset_id": intent.workset_id,
            "task_id": intent.task_id,
            "attempt_id": intent.attempt_id,
            "phase": phase,
            "data": data,
        }

    def test_normal_prefix_recovers_and_hands_to_landing(self) -> None:
        self.assertTrue(
            record_landing_correction_intent(
                self.profile,
                intent=self.intent,
            )
        )
        selection = load_landing_correction_selection(
            self.profile,
            workset_id="ws",
            task_id="task",
            attempt_id="attempt-1",
        )
        self.assertEqual(selection.active.phases, (PHASE_INTENT_RECORDED,))
        self.assertIsNone(selection.latest_terminal)

        self._validation(self.intent)
        selection = load_landing_correction_selection(
            self.profile,
            workset_id="ws",
            task_id="task",
            attempt_id="attempt-1",
        )
        self.assertEqual(
            selection.active.phases,
            (PHASE_INTENT_RECORDED, PHASE_VALIDATION_COMPLETED),
        )

        record_landing_correction_handed_to_landing(
            self.profile,
            intent=self.intent,
            landing_transaction_id=self._digest("transaction"),
            landing_intent_event_id=self._digest("landing-intent-event"),
        )
        selection = load_landing_correction_selection(
            self.profile,
            workset_id="ws",
            task_id="task",
            attempt_id="attempt-1",
        )
        self.assertIsNone(selection.active)
        self.assertEqual(
            selection.latest_terminal.phases,
            (
                PHASE_INTENT_RECORDED,
                PHASE_VALIDATION_COMPLETED,
                PHASE_HANDED_TO_LANDING,
            ),
        )
        self.assertTrue(selection.latest_terminal.handed_to_landing)

    def test_exact_retry_is_append_once_no_op(self) -> None:
        self.assertTrue(
            record_landing_correction_intent(
                self.profile,
                intent=self.intent,
            )
        )
        before = self.events_file.read_bytes()
        self.assertFalse(
            record_landing_correction_intent(
                self.profile,
                intent=self.intent,
            )
        )
        self.assertEqual(self.events_file.read_bytes(), before)

    def test_conflicting_phase_evidence_is_rejected(self) -> None:
        record_landing_correction_intent(self.profile, intent=self.intent)
        self._validation(self.intent)
        with self.assertRaisesRegex(
            LandingCorrectionError, "conflicts with recorded evidence"
        ):
            conflicting_validation = self._passed_validation(1)
            record_landing_correction_validation_completed(
                self.profile,
                intent=self.intent,
                source_head_commit=self.intent.source_head_commit,
                target_commit=self.intent.target_commit,
                source_tree_hash=self.intent.source_tree_hash,
                validation_evidence_hash=self._evidence_hash(
                    conflicting_validation.to_dict()
                ),
                validation=conflicting_validation,
            )

    def test_validation_evidence_hash_must_bind_typed_rows(self) -> None:
        record_landing_correction_intent(self.profile, intent=self.intent)
        validation = self._passed_validation(1)

        with self.assertRaisesRegex(
            LandingCorrectionError,
            "validation_evidence_hash does not match",
        ):
            record_landing_correction_validation_completed(
                self.profile,
                intent=self.intent,
                source_head_commit=self.intent.source_head_commit,
                target_commit=self.intent.target_commit,
                source_tree_hash=self.intent.source_tree_hash,
                validation_evidence_hash=self._digest("unrelated"),
                validation=validation,
            )

    def test_blocked_is_terminal_and_preserves_bounded_evidence(self) -> None:
        record_landing_correction_intent(self.profile, intent=self.intent)
        record_landing_correction_blocked(
            self.profile,
            intent=self.intent,
            reason_code="rebase_conflict",
        )
        selection = load_landing_correction_selection(
            self.profile,
            workset_id="ws",
            task_id="task",
            attempt_id="attempt-1",
        )
        terminal = selection.latest_terminal
        self.assertTrue(terminal.blocked)
        self.assertEqual(
            terminal.phase_data[PHASE_BLOCKED],
            {
                "reason_code": "rebase_conflict",
                "blocked_after": PHASE_INTENT_RECORDED,
                "validation": None,
            },
        )
        serialized = json.dumps(terminal.to_dict(), sort_keys=True)
        self.assertNotIn("command", serialized)
        self.assertNotIn("output", serialized)
        before = self.events_file.read_bytes()
        self.assertFalse(
            record_landing_correction_blocked(
                self.profile,
                intent=self.intent,
                reason_code="rebase_conflict",
            )
        )
        self.assertEqual(self.events_file.read_bytes(), before)
        with self.assertRaisesRegex(LandingCorrectionError, "already terminal"):
            self._validation(self.intent)

    def test_selection_exposes_latest_terminal_and_one_active_generation(self) -> None:
        record_landing_correction_intent(self.profile, intent=self.intent)
        record_landing_correction_blocked(
            self.profile,
            intent=self.intent,
            reason_code="validation_failed",
        )
        next_intent = self._intent(
            request_identity_hash=self._digest("request-2"),
            generation=2,
        )
        record_landing_correction_intent(self.profile, intent=next_intent)
        selection = load_landing_correction_selection(
            self.profile,
            workset_id="ws",
            task_id="task",
            attempt_id="attempt-1",
        )
        self.assertEqual(selection.active.correction_id, next_intent.correction_id)
        self.assertEqual(
            selection.latest_terminal.correction_id,
            self.intent.correction_id,
        )
        self.assertEqual(len(selection.corrections), 2)

        third_intent = self._intent(
            request_identity_hash=self._digest("request-3"),
            generation=3,
        )
        payload = self._raw_payload(
            intent=third_intent,
            phase=PHASE_INTENT_RECORDED,
            data=third_intent.to_dict(),
        )
        append_event(
            self.events_file,
            event_id=landing_correction_phase_event_id(
                third_intent.correction_id, PHASE_INTENT_RECORDED
            ),
            event_type=LANDING_CORRECTION_EVENT_TYPE,
            actor=third_intent.actor,
            payload=payload,
        )
        with self.assertRaisesRegex(
            LandingCorrectionError, "multiple active"
        ):
            load_landing_correction_selection(
                self.profile,
                workset_id="ws",
                task_id="task",
                attempt_id="attempt-1",
            )

    def test_rebase_prefix_binds_validation_to_corrected_tree(self) -> None:
        record_landing_correction_intent(self.profile, intent=self.intent)
        rebased_source = "a" * 40
        rebased_target = "b" * 40
        rebased_tree = self._digest("rebased-tree")
        record_landing_correction_rebase_completed(
            self.profile,
            intent=self.intent,
            source_head_commit=rebased_source,
            target_commit=rebased_target,
            source_tree_hash=rebased_tree,
        )
        with self.assertRaisesRegex(
            LandingCorrectionError, "exact corrected tree"
        ):
            self._validation(self.intent)
        validation = self._passed_validation(1)
        record_landing_correction_validation_completed(
            self.profile,
            intent=self.intent,
            source_head_commit=rebased_source,
            target_commit=rebased_target,
            source_tree_hash=rebased_tree,
            validation_evidence_hash=self._evidence_hash(validation.to_dict()),
            validation=validation,
        )
        selection = load_landing_correction_selection(
            self.profile,
            workset_id="ws",
            task_id="task",
            attempt_id="attempt-1",
        )
        self.assertEqual(
            selection.active.phases,
            (
                PHASE_INTENT_RECORDED,
                PHASE_REBASE_COMPLETED,
                PHASE_VALIDATION_COMPLETED,
            ),
        )

    def test_corrupt_phase_order_is_rejected(self) -> None:
        payload = self._raw_payload(
            intent=self.intent,
            phase=PHASE_VALIDATION_COMPLETED,
            data={
                "source_head_commit": self.intent.source_head_commit,
                "target_commit": self.intent.target_commit,
                "source_tree_hash": self.intent.source_tree_hash,
                "validation_policy_hash": self.intent.validation_policy_hash,
                "validation_evidence_hash": self._evidence_hash(
                    self._passed_validation(1).to_dict()
                ),
                "validation_count": 1,
                "all_passed": True,
                "validation": self._passed_validation(1).to_dict(),
            },
        )
        append_event(
            self.events_file,
            event_id=landing_correction_phase_event_id(
                self.intent.correction_id, PHASE_VALIDATION_COMPLETED
            ),
            event_type=LANDING_CORRECTION_EVENT_TYPE,
            actor=self.intent.actor,
            payload=payload,
        )
        with self.assertRaisesRegex(
            LandingCorrectionError, "no intent_recorded"
        ):
            load_landing_correction_selection(
                self.profile,
                workset_id="ws",
                task_id="task",
                attempt_id="attempt-1",
            )

    def test_record_rejects_phase_that_forks_order(self) -> None:
        record_landing_correction_intent(self.profile, intent=self.intent)
        with self.assertRaisesRegex(
            LandingCorrectionError, "valid ordered prefix"
        ):
            record_landing_correction_phase(
                self.profile,
                intent=self.intent,
                phase=PHASE_HANDED_TO_LANDING,
                data={
                    "landing_transaction_id": self._digest("transaction"),
                    "landing_intent_event_id": self._digest("intent-event"),
                },
            )

    def test_events_use_deterministic_ids_and_no_unbounded_evidence(self) -> None:
        record_landing_correction_intent(self.profile, intent=self.intent)
        events = load_events(self.events_file)
        event = events[-1]
        self.assertEqual(
            event["event_id"],
            landing_correction_phase_event_id(
                self.intent.correction_id, PHASE_INTENT_RECORDED
            ),
        )
        self.assertEqual(event["type"], LANDING_CORRECTION_EVENT_TYPE)
        self.assertEqual(
            set(event["payload"]["data"]),
            {
                "workset_id",
                "task_id",
                "attempt_id",
                "actor",
                "branch",
                "target_branch",
                "worktree_path",
                "source_head_commit",
                "target_commit",
                "source_tree_hash",
                "request_identity_hash",
                "validation_policy_hash",
                "generation",
                "resume_argv",
            },
        )
        self.assertEqual(
            event["payload"]["data"]["resume_argv"],
            ["blackdog", "task", "land"],
        )
        self.assertNotIn("output", json.dumps(event, sort_keys=True))
