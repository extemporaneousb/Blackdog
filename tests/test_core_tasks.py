from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import json
import hashlib
import subprocess

from blackdog_core.profile import load_profile
from blackdog_core.runtime_model import load_runtime_model
from blackdog_core.state import (
    ATTEMPT_STATUS_SUCCESS,
    RUNTIME_SCHEMA_VERSION,
    RUNTIME_STORE_VERSION,
    TASK_STATUS_DONE,
    JsonRuntimeStore,
    RuntimeState,
    StoreError,
    TaskAttemptRecord,
    TaskRecord,
    ValidationRecord,
    append_event_once,
    create_prompt_receipt,
    load_events,
    load_runtime_state,
    new_attempt_id,
    new_task_id,
    now_iso,
    prompt_receipt_reference,
    runtime_state_to_payload,
)
from blackdog_core.tasks import (
    TaskError,
    TaskFinalizationError,
    TaskRuntimeTransitionError,
    create_task,
    finish_task,
    inspect_task_finalization,
    inspect_task_runtime_transition,
    reconcile_landed_attempt,
    set_task_runtime_status,
    start_task,
)


class CoreTaskTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "blackdog.toml").write_text(
            """
[project]
name = "Test"
profile_version = 1

[paths]
control_dir = ".git/blackdog"
worktrees_dir = "../worktrees"

[taxonomy]
validation_commands = ["python -m unittest"]
doc_routing_defaults = ["AGENTS.md"]
""".strip()
            + "\n",
            encoding="utf-8",
        )
        self.profile = load_profile(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_runtime_round_trip_owns_definition_state_and_attempts(self) -> None:
        task_id = new_task_id()
        attempt_id = new_attempt_id()
        receipt = prompt_receipt_reference(create_prompt_receipt("private prompt", mode="raw"))
        state = RuntimeState(
            RUNTIME_SCHEMA_VERSION,
            RUNTIME_STORE_VERSION,
            (
                TaskRecord(
                    task_id=task_id,
                    title="One task",
                    created_at="2026-01-01T00:00:00+00:00",
                    status="in_progress",
                    attempts=(
                        TaskAttemptRecord(
                            task_id=task_id,
                            attempt_id=attempt_id,
                            status="in_progress",
                            actor="codex",
                            started_at="2026-01-01T00:00:01+00:00",
                            execution_model="direct_wtam",
                            prompt_receipt=receipt,
                        ),
                    ),
                ),
            ),
        )
        store = JsonRuntimeStore()
        store.save(self.profile.paths.runtime_file, state)
        loaded = store.load(self.profile.paths.runtime_file)
        self.assertEqual(loaded, state)
        payload = json.loads(self.profile.paths.runtime_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["tasks"][0]["id"], task_id)
        self.assertNotIn("task_id", payload["tasks"][0]["attempts"][0])
        self.assertNotIn("text", payload["tasks"][0]["attempts"][0]["prompt_receipt"])

    def test_active_attempt_is_exclusive_ownership(self) -> None:
        task = create_task(self.profile, title="Exclusive")
        attempt = start_task(self.profile, task_id=task.task_id, actor="codex")
        with self.assertRaisesRegex(TaskError, "already executing"):
            start_task(self.profile, task_id=task.task_id, actor="other")
        state = load_runtime_state(self.profile.paths)
        self.assertEqual(state.tasks[0].attempts[0].attempt_id, attempt.attempt_id)
        self.assertEqual(state.tasks[0].status, "in_progress")

    def test_start_retry_repairs_event_after_complete_attempt_save(self) -> None:
        task = create_task(self.profile, title="Repair start")
        receipt = prompt_receipt_reference(create_prompt_receipt("execute", mode="raw"))
        kwargs = {
            "task_id": task.task_id,
            "actor": "codex",
            "worktree_path": "/tmp/task",
            "branch": "agent/task",
            "target_branch": "main",
            "start_commit": "a" * 40,
            "prompt_receipt": receipt,
        }
        from blackdog_core import tasks as task_module

        original = task_module.append_event_once

        def fail_start(path, **call):
            if call.get("event_type") == "task.start":
                raise StoreError("injected")
            return original(path, **call)

        with patch("blackdog_core.tasks.append_event_once", side_effect=fail_start):
            with self.assertRaises(StoreError):
                start_task(self.profile, **kwargs)
        saved = load_runtime_state(self.profile.paths).tasks[0].attempts[0]
        repaired = start_task(self.profile, **kwargs)

        self.assertEqual(repaired, saved)
        self.assertEqual(len(load_runtime_state(self.profile.paths).tasks[0].attempts), 1)
        self.assertEqual(sum(row["type"] == "task.start" for row in load_events(self.profile.paths.events_file)), 1)

    def test_create_retry_repairs_event_before_runtime_fault(self) -> None:
        class FailFirstSave:
            def __init__(self) -> None:
                self.delegate = JsonRuntimeStore()
                self.failed = False

            def load(self, path):
                return self.delegate.load(path)

            def save(self, path, state):
                if not self.failed:
                    self.failed = True
                    raise StoreError("injected")
                self.delegate.save(path, state)

        task_id = new_task_id()
        store = FailFirstSave()
        with self.assertRaises(StoreError):
            create_task(self.profile, task_id=task_id, title="Repair", runtime_store=store)
        created = create_task(self.profile, task_id=task_id, title="Repair", runtime_store=store)
        self.assertEqual(created.task_id, task_id)
        self.assertEqual(len(load_events(self.profile.paths.events_file)), 1)

    def test_invalid_lifecycle_inputs_are_zero_mutation_and_unknown_arguments_fail(self) -> None:
        with self.assertRaises(StoreError):
            create_task(
                self.profile,
                task_id=new_task_id(),
                title="Invalid timestamp",
                created_at="not-a-timestamp",
            )
        self.assertFalse(self.profile.paths.runtime_file.exists())
        self.assertFalse(self.profile.paths.events_file.exists())

        task = create_task(self.profile, title="Strict boundary")
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        with self.assertRaises(TypeError):
            start_task(
                self.profile,
                task_id=task.task_id,
                actor="codex",
                unsupported_guard="ignored-no-more",
            )
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

        attempt = start_task(self.profile, task_id=task.task_id, actor="codex")
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        with self.assertRaises(TaskError):
            finish_task(
                self.profile,
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                actor="codex",
                status="success",
                summary="invalid evidence",
                changed_paths=(" ",),
            )
        with self.assertRaisesRegex(TaskError, "invalid task finalization request"):
            finish_task(
                self.profile,
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                actor="codex",
                status="success",
                summary="invalid commit",
                commit="not-a-commit",
            )
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

    def test_finalization_is_exact_idempotent_and_keeps_lineage(self) -> None:
        task = create_task(self.profile, title="Finish")
        execution = prompt_receipt_reference(create_prompt_receipt("execute", mode="skill"))
        request = prompt_receipt_reference(create_prompt_receipt("request", mode="raw"))
        attempt = start_task(
            self.profile,
            task_id=task.task_id,
            actor="codex",
            prompt_receipt=execution,
            user_prompt_receipt=request,
            worktree_path="/tmp/task",
            branch="agent/task",
            target_branch="main",
            start_commit="a" * 40,
        )
        kwargs = dict(
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            actor="codex",
            status=ATTEMPT_STATUS_SUCCESS,
            summary="finished",
            changed_paths=("src/a.py",),
            validations=(ValidationRecord("tests", "passed"),),
            commit="b" * 40,
            landed_commit="c" * 40,
            finalization_id="land-1",
        )
        first = finish_task(self.profile, **kwargs)
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        second = finish_task(self.profile, **kwargs)
        self.assertEqual(second, first)
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)
        self.assertEqual(first.prompt_receipt, execution)
        self.assertEqual(first.user_prompt_receipt, request)
        self.assertEqual(first.worktree_path, "/tmp/task")
        self.assertEqual(load_runtime_state(self.profile.paths).tasks[0].status, TASK_STATUS_DONE)
        evidence = inspect_task_finalization(
            self.profile, task_id=task.task_id, attempt_id=attempt.attempt_id
        )
        self.assertTrue(evidence.complete)
        self.assertEqual(evidence.stage, "complete")

    def test_conflicting_finalization_reuses_no_identity(self) -> None:
        task = create_task(self.profile, title="Conflict")
        attempt = start_task(self.profile, task_id=task.task_id, actor="codex")
        finish_task(
            self.profile,
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            actor="codex",
            status="failed",
            summary="first",
            finalization_id="same",
        )
        with self.assertRaises((TaskError, StoreError)):
            finish_task(
                self.profile,
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                actor="codex",
                status="blocked",
                summary="different",
                finalization_id="same",
            )

    def test_wrong_owner_and_finalization_identity_are_zero_mutation(self) -> None:
        task = create_task(self.profile, title="Owner")
        attempt = start_task(self.profile, task_id=task.task_id, actor="owner")
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        with self.assertRaisesRegex(TaskError, "belongs to actor"):
            finish_task(
                self.profile,
                task_id=task.task_id,
                attempt_id=attempt.attempt_id,
                actor="intruder",
                status="success",
                summary="wrong owner",
                finalization_id="intruder",
            )
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

        from blackdog_core import tasks as task_module

        original = task_module.append_event_once

        def fail_decision(path, **call):
            if call.get("event_type") == "task.finalization.decision":
                raise StoreError("injected")
            return original(path, **call)

        kwargs = {
            "task_id": task.task_id,
            "attempt_id": attempt.attempt_id,
            "actor": "owner",
            "status": "success",
            "summary": "owner completion",
            "finalization_id": "owner-finalization",
        }
        with patch("blackdog_core.tasks.append_event_once", side_effect=fail_decision):
            with self.assertRaises(TaskFinalizationError):
                finish_task(self.profile, **kwargs)
        runtime_after_request = self.profile.paths.runtime_file.read_bytes()
        events_after_request = self.profile.paths.events_file.read_bytes()
        with self.assertRaisesRegex(TaskError, "different finalization identity"):
            finish_task(self.profile, **{**kwargs, "finalization_id": "different"})
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_after_request)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_after_request)
        self.assertEqual(finish_task(self.profile, **kwargs).status, "success")

    def test_finalization_retry_converges_after_decision_before_runtime_fault(self) -> None:
        class FailFirstSave:
            def __init__(self) -> None:
                self.delegate = JsonRuntimeStore()
                self.failed = False

            def load(self, path):
                return self.delegate.load(path)

            def save(self, path, state):
                if not self.failed:
                    self.failed = True
                    raise StoreError("injected")
                self.delegate.save(path, state)

        task = create_task(self.profile, title="Repair finish")
        attempt = start_task(self.profile, task_id=task.task_id, actor="codex")
        kwargs = dict(
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            actor="codex",
            status="success",
            summary="done",
            finalization_id="repair-finish",
        )
        with self.assertRaises(TaskFinalizationError):
            finish_task(self.profile, runtime_store=FailFirstSave(), **kwargs)
        repaired = finish_task(self.profile, **kwargs)
        self.assertEqual(repaired.status, "success")
        evidence = inspect_task_finalization(
            self.profile, task_id=task.task_id, attempt_id=attempt.attempt_id
        )
        self.assertTrue(evidence.complete)

    def test_finalization_retry_repairs_finish_event_after_runtime_save(self) -> None:
        task = create_task(self.profile, title="Repair event")
        attempt = start_task(self.profile, task_id=task.task_id, actor="codex")
        kwargs = dict(
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            actor="codex",
            status="success",
            summary="done",
            finalization_id="repair-event",
        )
        from blackdog_core import tasks as task_module

        original = task_module.append_event_once

        def fail_finish(path, **call):
            if call.get("event_type") == "task.finish":
                raise StoreError("injected")
            return original(path, **call)

        with patch("blackdog_core.tasks.append_event_once", side_effect=fail_finish):
            with self.assertRaises(TaskFinalizationError):
                finish_task(self.profile, **kwargs)
        self.assertEqual(load_runtime_state(self.profile.paths).tasks[0].status, TASK_STATUS_DONE)
        repaired = finish_task(self.profile, **kwargs)
        self.assertEqual(repaired.status, "success")
        self.assertTrue(
            inspect_task_finalization(
                self.profile, task_id=task.task_id, attempt_id=attempt.attempt_id
            ).complete
        )

    def test_explicit_status_transition_refuses_active_attempt(self) -> None:
        task = create_task(self.profile, title="Transition")
        start_task(self.profile, task_id=task.task_id, actor="codex")
        with self.assertRaisesRegex(Exception, "in-progress attempt"):
            set_task_runtime_status(
                self.profile,
                task_id=task.task_id,
                actor="codex",
                status="canceled",
                summary="cancel",
            )

    def test_completed_task_is_immutable_under_explicit_status_transition(self) -> None:
        task = create_task(self.profile, title="Immutable completion")
        attempt = start_task(self.profile, task_id=task.task_id, actor="codex")
        finish_task(
            self.profile,
            task_id=task.task_id,
            attempt_id=attempt.attempt_id,
            actor="codex",
            status="success",
            summary="complete",
        )
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        with self.assertRaises(TaskRuntimeTransitionError):
            set_task_runtime_status(
                self.profile,
                task_id=task.task_id,
                actor="codex",
                status="canceled",
                summary="rewrite completion",
            )
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

    def test_status_transition_retry_converges_after_decision_before_runtime_save(self) -> None:
        class FailFirstSave:
            def __init__(self) -> None:
                self.delegate = JsonRuntimeStore()
                self.failed = False

            def load(self, path):
                return self.delegate.load(path)

            def save(self, path, state):
                if not self.failed:
                    self.failed = True
                    raise StoreError("injected")
                self.delegate.save(path, state)

        task = create_task(self.profile, title="Transition repair")
        kwargs = {
            "task_id": task.task_id,
            "actor": "codex",
            "status": "blocked",
            "summary": "blocked by evidence",
            "failure_class": "unknown",
        }
        with self.assertRaises(StoreError):
            set_task_runtime_status(self.profile, runtime_store=FailFirstSave(), **kwargs)
        repaired = set_task_runtime_status(self.profile, return_transition_result=True, **kwargs)
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        repeated = set_task_runtime_status(self.profile, return_transition_result=True, **kwargs)

        self.assertEqual(repaired.record.status, "blocked")
        self.assertTrue(repaired.runtime_changed)
        self.assertFalse(repeated.runtime_changed)
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)
        transition_types = [row["type"] for row in load_events(self.profile.paths.events_file)]
        self.assertEqual(transition_types.count("task.runtime-transition.request"), 1)
        self.assertEqual(transition_types.count("task.runtime-transition.decision"), 1)
        self.assertEqual(transition_types.count("task.transition"), 1)

    def test_cancel_and_reopen_transition_faults_require_exact_retry(self) -> None:
        from blackdog_core import tasks as task_module

        original_append = task_module.append_event_once

        class FailFirstSave:
            def __init__(self) -> None:
                self.delegate = JsonRuntimeStore()
                self.failed = False

            def load(self, path):
                return self.delegate.load(path)

            def save(self, path, state):
                if not self.failed:
                    self.failed = True
                    raise StoreError("injected after decision")
                self.delegate.save(path, state)

        for action in ("cancel", "reopen"):
            for fault_stage, expected_stage in (
                ("before_decision", "request_recorded"),
                ("after_decision", "decision_recorded"),
                ("after_runtime", "runtime_applied"),
            ):
                with self.subTest(action=action, fault_stage=fault_stage):
                    task = create_task(self.profile, title=f"{action} {fault_stage}")
                    if action == "reopen":
                        set_task_runtime_status(
                            self.profile,
                            task_id=task.task_id,
                            actor="preparer",
                            status="canceled",
                            summary="prepare reopen",
                        )
                    kwargs = {
                        "task_id": task.task_id,
                        "actor": "codex",
                        "status": "canceled" if action == "cancel" else "planned",
                        "summary": f"exact {action}",
                        "failure_class": "unknown" if action == "cancel" else None,
                    }
                    before_types = [
                        row["type"]
                        for row in load_events(self.profile.paths.events_file)
                        if isinstance(row.get("payload"), dict)
                        and row["payload"].get("task_id") == task.task_id
                    ]

                    if fault_stage == "after_decision":
                        with self.assertRaisesRegex(StoreError, "injected after decision"):
                            set_task_runtime_status(
                                self.profile,
                                runtime_store=FailFirstSave(),
                                **kwargs,
                            )
                    else:
                        failed = False
                        failed_type = (
                            "task.runtime-transition.decision"
                            if fault_stage == "before_decision"
                            else "task.transition"
                        )

                        def fail_event(path, **call):
                            nonlocal failed
                            if call.get("event_type") == failed_type and not failed:
                                failed = True
                                raise StoreError(f"injected {fault_stage}")
                            return original_append(path, **call)

                        with patch("blackdog_core.tasks.append_event_once", side_effect=fail_event):
                            with self.assertRaisesRegex(StoreError, f"injected {fault_stage}"):
                                set_task_runtime_status(self.profile, **kwargs)

                    evidence = inspect_task_runtime_transition(
                        self.profile,
                        task_id=task.task_id,
                    )
                    self.assertTrue(evidence.unfinished)
                    self.assertEqual(evidence.stage, expected_stage)
                    self.assertIsNotNone(evidence.request_event_id)
                    runtime_partial = self.profile.paths.runtime_file.read_bytes()
                    events_partial = self.profile.paths.events_file.read_bytes()

                    mismatches = (
                        {**kwargs, "summary": f"modified {action}"},
                        {
                            **kwargs,
                            "status": "planned" if action == "cancel" else "canceled",
                        },
                    )
                    for mismatch in mismatches:
                        with self.assertRaisesRegex(
                            TaskRuntimeTransitionError,
                            "only the exact original request may retry",
                        ):
                            set_task_runtime_status(self.profile, **mismatch)
                        self.assertEqual(
                            self.profile.paths.runtime_file.read_bytes(),
                            runtime_partial,
                        )
                        self.assertEqual(
                            self.profile.paths.events_file.read_bytes(),
                            events_partial,
                        )

                    repaired = set_task_runtime_status(
                        self.profile,
                        return_transition_result=True,
                        **kwargs,
                    )
                    self.assertEqual(repaired.record.status, kwargs["status"])
                    self.assertEqual(
                        repaired.runtime_changed,
                        fault_stage != "after_runtime",
                    )
                    self.assertFalse(repaired.request_event_appended)
                    self.assertEqual(
                        repaired.decision_event_appended,
                        fault_stage == "before_decision",
                    )
                    self.assertTrue(repaired.owned_event_appended)
                    complete = inspect_task_runtime_transition(
                        self.profile,
                        task_id=task.task_id,
                    )
                    self.assertFalse(complete.unfinished)
                    self.assertEqual(complete.stage, "complete")
                    self.assertEqual(complete.request_event_id, evidence.request_event_id)

                    final_runtime = self.profile.paths.runtime_file.read_bytes()
                    final_events = self.profile.paths.events_file.read_bytes()
                    repeated = set_task_runtime_status(
                        self.profile,
                        return_transition_result=True,
                        **kwargs,
                    )
                    self.assertFalse(repeated.runtime_changed)
                    self.assertFalse(repeated.events_changed)
                    self.assertEqual(self.profile.paths.runtime_file.read_bytes(), final_runtime)
                    self.assertEqual(self.profile.paths.events_file.read_bytes(), final_events)
                    after_types = [
                        row["type"]
                        for row in load_events(self.profile.paths.events_file)
                        if isinstance(row.get("payload"), dict)
                        and row["payload"].get("task_id") == task.task_id
                    ]
                    for event_type in (
                        "task.runtime-transition.request",
                        "task.runtime-transition.decision",
                        "task.transition",
                    ):
                        self.assertEqual(
                            after_types.count(event_type),
                            before_types.count(event_type) + 1,
                        )

    def test_landing_reconciliation_only_finalizes_guarded_active_attempt(self) -> None:
        task = create_task(self.profile, title="Reconcile")
        attempt = start_task(
            self.profile,
            task_id=task.task_id,
            actor="codex",
            branch="agent/task",
            target_branch="main",
            start_commit="a" * 40,
        )
        kwargs = {
            "task_id": task.task_id,
            "attempt_id": attempt.attempt_id,
            "landed_commit": "c" * 40,
            "actor": "codex",
            "summary": "landed safely",
            "source_commit": "b" * 40,
            "target_commit": "c" * 40,
            "target_branch": "main",
            "landing_transaction_id": "transaction-1",
            "landing_transaction_phases": ("intent_recorded", "target_updated"),
            "current_format": True,
            "changed_paths": ("src/a.py",),
            "validations": (ValidationRecord("tests", "passed"),),
        }
        first = reconcile_landed_attempt(self.profile, **kwargs)
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        second = reconcile_landed_attempt(self.profile, **kwargs)
        finished = load_runtime_state(self.profile.paths).tasks[0].attempts[0]

        self.assertTrue(first["runtime_changed"])
        self.assertFalse(second["runtime_changed"])
        self.assertEqual(finished.status, "success")
        self.assertEqual(finished.commit, "b" * 40)
        self.assertEqual(finished.landed_commit, "c" * 40)
        self.assertEqual(finished.validations, (ValidationRecord("tests", "passed"),))
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

        other = create_task(self.profile, title="Terminal cannot be rewritten")
        other_attempt = start_task(self.profile, task_id=other.task_id, actor="codex", target_branch="main")
        finish_task(
            self.profile,
            task_id=other.task_id,
            attempt_id=other_attempt.attempt_id,
            actor="codex",
            status="failed",
            summary="failed",
        )
        with self.assertRaisesRegex(TaskError, "cannot rewrite a terminal attempt"):
            reconcile_landed_attempt(
                self.profile,
                **{
                    **kwargs,
                    "task_id": other.task_id,
                    "attempt_id": other_attempt.attempt_id,
                },
            )

    def test_concurrent_start_cancel_and_close_preserve_single_authority(self) -> None:
        start_task_record = create_task(self.profile, title="Concurrent start")
        start_barrier = Barrier(2)

        def competing_start(actor: str):
            start_barrier.wait()
            try:
                return start_task(self.profile, task_id=start_task_record.task_id, actor=actor)
            except TaskError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            start_results = list(executor.map(competing_start, ("one", "two")))
        self.assertEqual(sum(isinstance(item, TaskAttemptRecord) for item in start_results), 1)
        self.assertEqual(len(load_runtime_state(self.profile.paths).tasks[0].attempts), 1)

        cancel_task = create_task(self.profile, title="Concurrent cancel")
        cancel_barrier = Barrier(2)

        def begin_or_cancel(operation: str):
            cancel_barrier.wait()
            try:
                if operation == "begin":
                    return start_task(self.profile, task_id=cancel_task.task_id, actor="owner")
                return set_task_runtime_status(
                    self.profile,
                    task_id=cancel_task.task_id,
                    actor="owner",
                    status="canceled",
                    summary="cancel",
                )
            except TaskError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            cancel_results = list(executor.map(begin_or_cancel, ("begin", "cancel")))
        self.assertEqual(sum(not isinstance(item, TaskError) for item in cancel_results), 1)
        canceled_or_active = next(
            item for item in load_runtime_state(self.profile.paths).tasks if item.task_id == cancel_task.task_id
        )
        self.assertIn(canceled_or_active.status, {"canceled", "in_progress"})

        close_task = create_task(self.profile, title="Concurrent close")
        close_attempt = start_task(self.profile, task_id=close_task.task_id, actor="owner")
        close_barrier = Barrier(2)
        close_kwargs = {
            "task_id": close_task.task_id,
            "attempt_id": close_attempt.attempt_id,
            "actor": "owner",
            "status": "success",
            "summary": "closed",
            "finalization_id": "one-close",
        }

        def close_once(_: int):
            close_barrier.wait()
            return finish_task(self.profile, **close_kwargs)

        with ThreadPoolExecutor(max_workers=2) as executor:
            close_results = list(executor.map(close_once, (1, 2)))
        self.assertEqual([item.status for item in close_results], ["success", "success"])
        close_events = [
            row
            for row in load_events(self.profile.paths.events_file)
            if isinstance(row.get("payload"), dict)
            and row["payload"].get("attempt_id") == close_attempt.attempt_id
        ]
        self.assertEqual(
            [row["type"] for row in close_events].count("task.finalization.request"),
            1,
        )

    def test_store_rejects_multiple_active_attempts_and_global_duplicate_attempt_ids(self) -> None:
        shared = new_attempt_id()
        first_id = new_task_id()
        second_id = new_task_id()
        active = TaskAttemptRecord(
            attempt_id=shared,
            task_id=first_id,
            status="in_progress",
            actor="codex",
            started_at=now_iso(),
        )
        invalid = RuntimeState(
            RUNTIME_SCHEMA_VERSION,
            RUNTIME_STORE_VERSION,
            (
                TaskRecord(first_id, "one", status="in_progress", attempts=(active, replace(active, attempt_id=new_attempt_id()))),
                TaskRecord(second_id, "two", status="blocked", attempts=(replace(active, task_id=second_id),)),
            ),
        )
        with self.assertRaises(StoreError):
            JsonRuntimeStore().save(self.profile.paths.runtime_file, invalid)

    def test_done_without_retained_attempt_is_historical_only(self) -> None:
        task_id = new_task_id()
        timestamp = "2026-01-01T00:00:00+00:00"
        historical = RuntimeState(
            RUNTIME_SCHEMA_VERSION,
            RUNTIME_STORE_VERSION,
            (
                TaskRecord(
                    task_id=task_id,
                    title="Imported terminal task",
                    created_at=timestamp,
                    updated_at=timestamp,
                    status="done",
                ),
            ),
        )
        JsonRuntimeStore().save(self.profile.paths.runtime_file, historical)
        self.assertEqual(JsonRuntimeStore().load(self.profile.paths.runtime_file), historical)

        failed = TaskAttemptRecord(
            task_id=task_id,
            attempt_id=new_attempt_id(),
            status="failed",
            actor="codex",
            started_at=timestamp,
            ended_at=timestamp,
            failure_class="unknown",
        )
        with self.assertRaisesRegex(StoreError, "done without a successful retained attempt"):
            JsonRuntimeStore().save(
                self.profile.paths.runtime_file,
                replace(historical, tasks=(replace(historical.tasks[0], attempts=(failed,)),)),
            )

    def test_loader_rejects_corrupt_task_attempt_and_receipt_semantics(self) -> None:
        artifact_text = b"artifact"
        artifact_hash = hashlib.sha256(artifact_text).hexdigest()
        artifact_relative = Path("prompts") / "sha256" / f"{artifact_hash}.txt"
        artifact_path = self.profile.paths.control_dir / artifact_relative
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(artifact_text)
        artifact_path.chmod(0o600)
        task_id = new_task_id()
        attempt_id = new_attempt_id()
        state = RuntimeState(
            RUNTIME_SCHEMA_VERSION,
            RUNTIME_STORE_VERSION,
            (
                TaskRecord(
                    task_id=task_id,
                    title="Validate",
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                    status="in_progress",
                    attempts=(
                        TaskAttemptRecord(
                            task_id=task_id,
                            attempt_id=attempt_id,
                            status="in_progress",
                            actor="codex",
                            started_at="2026-01-01T00:00:00+00:00",
                            execution_model="direct_wtam",
                            workspace_mode="git-worktree",
                            worktree_role="task",
                            prompt_receipt=prompt_receipt_reference(
                                create_prompt_receipt(
                                    artifact_text.decode(),
                                    mode="raw",
                                    recorded_at="2026-01-01T00:00:00+00:00",
                                    replay_artifact_path=artifact_relative.as_posix(),
                                )
                            ),
                        ),
                    ),
                ),
            ),
        )
        base = runtime_state_to_payload(state)
        path = self.profile.paths.runtime_file
        path.parent.mkdir(parents=True, exist_ok=True)

        def mutate_started(payload):
            payload["tasks"][0]["attempts"][0]["started_at"] = "invalid"

        def mutate_active_ended(payload):
            payload["tasks"][0]["attempts"][0]["ended_at"] = "2026-01-01T00:01:00+00:00"

        def mutate_terminal_missing_end(payload):
            payload["tasks"][0]["status"] = "done"
            payload["tasks"][0]["attempts"][0]["status"] = "success"

        def mutate_execution(payload):
            payload["tasks"][0]["attempts"][0]["execution_model"] = "unknown"

        def mutate_workspace(payload):
            payload["tasks"][0]["attempts"][0]["workspace_mode"] = "unknown"

        def mutate_role(payload):
            payload["tasks"][0]["attempts"][0]["worktree_role"] = "primary"

        def mutate_failure(payload):
            payload["tasks"][0]["attempts"][0]["failure_class"] = "unknown-class"

        def mutate_capture(payload):
            payload["tasks"][0]["attempts"][0]["codex_session"] = {
                "thread_id": "thread",
                "capture_status": "captured",
            }

        def mutate_prompt_hash(payload):
            payload["tasks"][0]["attempts"][0]["prompt_receipt"]["prompt_hash"] = "G" * 64

        def mutate_prompt_path(payload):
            payload["tasks"][0]["attempts"][0]["prompt_receipt"]["replay_artifact_path"] = "/tmp/prompt"

        def mutate_prompt_text(payload):
            payload["tasks"][0]["attempts"][0]["prompt_receipt"]["text"] = "must not be durable"

        def mutate_unknown_field(payload):
            payload["tasks"][0]["obsolete"] = True

        for label, mutator in (
            ("timestamp", mutate_started),
            ("active-ended", mutate_active_ended),
            ("terminal-end", mutate_terminal_missing_end),
            ("execution", mutate_execution),
            ("workspace", mutate_workspace),
            ("role", mutate_role),
            ("failure", mutate_failure),
            ("capture", mutate_capture),
            ("prompt-hash", mutate_prompt_hash),
            ("prompt-path", mutate_prompt_path),
            ("prompt-text", mutate_prompt_text),
            ("unknown-field", mutate_unknown_field),
        ):
            with self.subTest(label=label):
                payload = json.loads(json.dumps(base))
                mutator(payload)
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(StoreError):
                    JsonRuntimeStore().load(path)

        path.write_text(json.dumps(base), encoding="utf-8")
        artifact_path.write_bytes(b"different")
        with self.assertRaisesRegex(StoreError, "artifact content"):
            JsonRuntimeStore().load(path)

        path.write_text(json.dumps(base).replace('"schema_version": 4', '"schema_version": NaN'), encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "invalid number"):
            JsonRuntimeStore().load(path)

    def test_event_identity_is_strict_and_append_once(self) -> None:
        path = self.profile.paths.events_file
        self.assertTrue(append_event_once(path, event_id="one", event_type="x", payload={"value": 1}))
        self.assertFalse(append_event_once(path, event_id="one", event_type="x", payload={"value": 1}))
        with self.assertRaises(StoreError):
            append_event_once(path, event_id="one", event_type="x", payload={"value": True})
        self.assertEqual(len(load_events(path)), 1)

        original = path.read_text(encoding="utf-8")
        row = json.loads(original)
        row["at"] = "invalid"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "invalid at timestamp"):
            load_events(path)
        path.write_text(original + original, encoding="utf-8")
        with self.assertRaisesRegex(StoreError, "not unique"):
            load_events(path)

    def test_runtime_model_is_flat(self) -> None:
        task = create_task(self.profile, title="Flat")
        attempt = start_task(self.profile, task_id=task.task_id, actor="codex")
        model = load_runtime_model(self.profile)
        self.assertEqual([item.task_id for item in model.tasks], [task.task_id])
        self.assertEqual([item.attempt_id for item in model.attempts], [attempt.attempt_id])
        self.assertEqual(model.counts["active_attempts"], 1)
