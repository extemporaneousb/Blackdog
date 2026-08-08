from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
import queue
import threading
import traceback
from pathlib import Path
from unittest.mock import patch

import blackdog_core.backlog as backlog_module
from blackdog_core.backlog import (
    AbandonedLandingEligibility,
    BacklogError,
    JsonPlanningStore,
    PlanningState,
    StaleClaimReleaseConflictError,
    StaleClaimReleaseFinalizationError,
    TaskSpec,
    Workset,
    default_planning_state,
    finish_task,
    load_planning_state,
    next_ready_tasks,
    pending_stale_claim_release,
    reconcile_landed_attempt,
    release_stale_task_claim,
    require_no_pending_stale_claim_release_for_workset,
    save_planning_state,
    set_task_runtime_status,
    start_task,
    upsert_workset,
)
from blackdog_core.profile import load_profile
from blackdog_core.state import (
    JsonRuntimeStore,
    RUNTIME_SCHEMA_VERSION,
    RUNTIME_STORE_VERSION,
    StoreError,
    TaskClaimRecord,
    ValidationRecord,
    append_event,
    append_event_once,
    create_prompt_receipt,
    exclusive_file_lock,
    load_events,
    load_runtime_state,
    merge_workset_runtime,
    now_iso,
    save_runtime_state,
)
from tests.core_audit_support import CoreAuditTestCase


def _finish_task_worker(
    project_root: str,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    start_event,
    result_queue,
) -> None:
    try:
        start_event.wait()
        profile = load_profile(Path(project_root))
        finish_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor="codex",
            status="success",
            summary=f"finished {task_id}",
        )
        result_queue.put(("ok", task_id, None))
    except Exception:
        result_queue.put(("error", task_id, traceback.format_exc()))


def _cancel_task_worker(
    project_root: str,
    workset_id: str,
    task_id: str,
    start_event,
    result_queue,
) -> None:
    try:
        start_event.wait()
        profile = load_profile(Path(project_root))
        set_task_runtime_status(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="codex",
            status="canceled",
            summary=f"canceled {task_id}",
        )
        result_queue.put(("ok", task_id, None))
    except Exception:
        result_queue.put(("error", task_id, traceback.format_exc()))


def _reconcile_landed_attempt_worker(
    project_root: str,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    start_event,
    result_queue,
) -> None:
    try:
        start_event.wait()
        profile = load_profile(Path(project_root))
        reconcile_landed_attempt(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
            landed_commit="c" * 40,
            actor="concurrent-auditor",
            changed_paths=("concurrent.txt",),
        )
        result_queue.put(("ok", task_id, None))
    except Exception:
        result_queue.put(("error", task_id, traceback.format_exc()))


def _retry_finish_task_worker(
    project_root: str,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    finalization_id: str,
    start_event,
    result_queue,
) -> None:
    try:
        start_event.wait()
        profile = load_profile(Path(project_root))
        finish_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor="owner",
            status="success",
            summary="durably finalized",
            changed_paths=("src/finalized.py",),
            validations=(ValidationRecord(name="unit", status="passed"),),
            residuals=("none",),
            followup_candidates=("publish",),
            commit="a" * 40,
            landed_commit="b" * 40,
            elapsed_seconds=17,
            note="durable note",
            finalization_id=finalization_id,
        )
        result_queue.put(("ok", task_id, None))
    except Exception:
        result_queue.put(("error", task_id, traceback.format_exc()))


def _append_event_once_worker(
    events_path: str,
    payload,
    start_event,
    result_queue,
) -> None:
    try:
        start_event.wait()
        appended = append_event_once(
            Path(events_path),
            event_id="concurrent-strict-event",
            event_type="task.finish",
            actor="owner",
            payload=payload,
        )
        result_queue.put(("ok", appended, None))
    except Exception:
        result_queue.put(("error", None, traceback.format_exc()))


def _conflicting_finalization_request_worker(
    project_root: str,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    finalization_id: str,
    summary: str,
    start_event,
    result_queue,
) -> None:
    try:
        start_event.wait()
        profile = load_profile(Path(project_root))
        result = finish_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor="owner",
            status="success",
            summary=summary,
            changed_paths=("src/finalized.py",),
            finalization_id=finalization_id,
        )
        result_queue.put(("ok", result.summary, None))
    except Exception:
        result_queue.put(("error", summary, traceback.format_exc()))


def _stale_claim_release_worker(
    project_root: str,
    workset_id: str,
    task_id: str,
    status: str,
    summary: str,
    start_event,
    result_queue,
) -> None:
    try:
        start_event.wait()
        profile = load_profile(Path(project_root))
        result = release_stale_task_claim(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            status=status,
            summary=summary,
        )
        result_queue.put(
            (
                "ok",
                task_id,
                result.request_event_id,
                result.decision_event_id,
                result.workset_release_event_id,
            )
        )
    except Exception:
        result_queue.put(("error", task_id, traceback.format_exc()))


class _DelegatingRuntimeStore:
    """Protocol-only wrapper: deliberately exposes no unlocked-save helper."""

    def __init__(self) -> None:
        self._delegate = JsonRuntimeStore()

    def load(self, path: Path):
        return self._delegate.load(path)

    def save(self, path: Path, state) -> None:
        self._delegate.save(path, state)


def _delegating_store_finish_worker(
    project_root: str,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    start_event,
    result_queue,
) -> None:
    try:
        start_event.wait()
        profile = load_profile(Path(project_root))
        finish_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
            actor="owner",
            status="success",
            summary=f"finished {task_id}",
            finalization_id=f"delegating-{attempt_id}",
            runtime_store=_DelegatingRuntimeStore(),
        )
        result_queue.put(("ok", task_id, None))
    except Exception:
        result_queue.put(("error", task_id, traceback.format_exc()))


def _nested_alias_lock_worker(
    real_path: str,
    alias_path: str,
    use_lockdir: bool,
    result_queue,
) -> None:
    try:
        def acquire_nested() -> None:
            with exclusive_file_lock(Path(real_path)):
                with exclusive_file_lock(Path(alias_path)):
                    pass

        if use_lockdir:
            with patch("blackdog_core.state.fcntl", None):
                acquire_nested()
        else:
            acquire_nested()
        result_queue.put(("ok", alias_path, None))
    except Exception:
        result_queue.put(("error", alias_path, traceback.format_exc()))


def _hold_alias_lock_worker(
    path: str,
    use_lockdir: bool,
    entered_event,
    release_event,
    result_queue,
) -> None:
    try:
        def hold() -> None:
            with exclusive_file_lock(Path(path)):
                entered_event.set()
                if not release_event.wait(timeout=10):
                    raise TimeoutError("lock holder release was not signaled")

        if use_lockdir:
            with patch("blackdog_core.state.fcntl", None):
                hold()
        else:
            hold()
        result_queue.put(("ok", "holder", None))
    except Exception:
        result_queue.put(("error", "holder", traceback.format_exc()))


def _probe_alias_lock_worker(
    path: str,
    use_lockdir: bool,
    attempting_event,
    acquired_event,
    result_queue,
) -> None:
    try:
        def probe() -> None:
            attempting_event.set()
            with exclusive_file_lock(Path(path)):
                acquired_event.set()

        if use_lockdir:
            with patch("blackdog_core.state.fcntl", None):
                probe()
        else:
            probe()
        result_queue.put(("ok", "probe", None))
    except Exception:
        result_queue.put(("error", "probe", traceback.format_exc()))


class _MemoryPlanningStore:
    def __init__(self) -> None:
        self.state = default_planning_state()

    def load(self, path: Path) -> PlanningState:
        return self.state

    def save(self, path: Path, state: PlanningState) -> None:
        self.state = state


class _BlockingPlanningStore:
    """Return one pre-upsert snapshot, then release the task operation."""

    def __init__(self, loaded: threading.Event, proceed: threading.Event) -> None:
        self._delegate = JsonPlanningStore()
        self._loaded = loaded
        self._proceed = proceed
        self._lock = threading.Lock()
        self._blocked = False

    def load(self, path: Path) -> PlanningState:
        state = self._delegate.load(path)
        with self._lock:
            should_block = not self._blocked
            if should_block:
                self._blocked = True
        if should_block:
            self._loaded.set()
            if not self._proceed.wait(timeout=10):
                raise TimeoutError("planning race was not released")
        return state

    def save(self, path: Path, state: PlanningState) -> None:
        self._delegate.save(path, state)


class CorePlanningTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile("Demo")
        self.profile = self.load_test_profile()

    def _run_workers(self, processes, start_event, result_queue) -> None:
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=20)
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                self.fail(f"worker did not exit: pid={process.pid}")
            self.assertEqual(process.exitcode, 0)
        results = []
        for _process in processes:
            try:
                results.append(result_queue.get(timeout=5))
            except queue.Empty as exc:
                raise AssertionError("worker did not report a result") from exc
        errors = [result for result in results if result[0] != "ok"]
        self.assertEqual(errors, [])

    def _start_durable_fixture(
        self,
        workset_id: str,
        *,
        include_second_task: bool = False,
    ):
        tasks = [
            {
                "id": "FIN-A",
                "title": "Finalize A",
                "intent": "exercise durable task finalization",
            }
        ]
        if include_second_task:
            tasks.append(
                {
                    "id": "FIN-B",
                    "title": "Finalize B",
                    "intent": "exercise same-workset claim drift",
                }
            )
        upsert_workset(
            self.profile,
            {
                "id": workset_id,
                "title": "Durable finalization fixture",
                "tasks": tasks,
            },
        )
        attempt = start_task(
            self.profile,
            workset_id=workset_id,
            task_id="FIN-A",
            actor="owner",
            prompt_receipt=create_prompt_receipt("Finalize A durably.", source="unit-test"),
            note="active note",
        )
        kwargs = {
            "workset_id": workset_id,
            "task_id": "FIN-A",
            "attempt_id": attempt.attempt_id,
            "actor": "owner",
            "status": "success",
            "summary": "durably finalized",
            "changed_paths": ("src/finalized.py",),
            "validations": (ValidationRecord(name="unit", status="passed"),),
            "residuals": ("none",),
            "followup_candidates": ("publish",),
            "commit": "a" * 40,
            "landed_commit": "b" * 40,
            "elapsed_seconds": 17,
            "note": "durable note",
            "finalization_id": f"{workset_id}-finalization",
        }
        return attempt, kwargs

    def _stale_claim_fixture(
        self,
        workset_id: str,
        *,
        remaining_claim: bool = False,
        workset_claim_present: bool = True,
        target_attempt_id_present: bool = True,
        legacy_timestamps: bool = False,
    ):
        tasks = [
            {
                "id": "STALE-A",
                "title": "Release stale A",
                "intent": "exercise durable stale-claim release",
            }
        ]
        if remaining_claim:
            tasks.append(
                {
                    "id": "STALE-B",
                    "title": "Preserve claimed B",
                    "intent": "preserve unrelated claim bytes",
                }
            )
        upsert_workset(
            self.profile,
            {
                "id": workset_id,
                "title": "Stale claim fixture",
                "tasks": tasks,
            },
        )
        attempt = start_task(
            self.profile,
            workset_id=workset_id,
            task_id="STALE-A",
            actor="owner",
            prompt_receipt=create_prompt_receipt(
                "Release a stale claim durably.", source="unit-test"
            ),
            note="stale owner note",
        )
        runtime = load_runtime_state(self.profile.paths)
        runtime_workset = next(
            row for row in runtime.worksets if row.workset_id == workset_id
        )
        active_attempt = next(
            row for row in runtime_workset.attempts
            if row.attempt_id == attempt.attempt_id
        )
        target_claim = next(
            row for row in runtime_workset.task_claims
            if row.task_id == "STALE-A"
        )
        incoming_claims = [
            replace(
                target_claim,
                claimed_at=(
                    "legacy-claim-time"
                    if legacy_timestamps
                    else target_claim.claimed_at
                ),
                attempt_id=(
                    target_claim.attempt_id if target_attempt_id_present else None
                ),
            )
        ]
        if remaining_claim:
            incoming_claims.append(
                TaskClaimRecord(
                    task_id="STALE-B",
                    actor="owner",
                    execution_model=target_claim.execution_model,
                    claimed_at=target_claim.claimed_at,
                    attempt_id=None,
                    note="preserve exactly",
                )
            )
        stale_attempt = replace(
            active_attempt,
            status="blocked",
            ended_at=now_iso(),
            summary="interrupted before claim release",
            elapsed_seconds=1,
        )
        stale_runtime = merge_workset_runtime(
            runtime,
            workset_id=workset_id,
            task_ids={task["id"] for task in tasks},
            incoming_records=(
                tuple(
                    replace(state, updated_at="legacy-state-time")
                    for state in runtime_workset.task_states
                    if state.task_id == "STALE-A"
                )
                if legacy_timestamps
                else None
            ),
            incoming_workset_claim=(
                replace(
                    runtime_workset.workset_claim,
                    claimed_at="legacy-workset-claim-time",
                )
                if workset_claim_present
                and legacy_timestamps
                and runtime_workset.workset_claim is not None
                else runtime_workset.workset_claim
                if workset_claim_present
                else None
            ),
            incoming_task_claims=tuple(incoming_claims),
            incoming_attempts=(stale_attempt,),
        )
        save_runtime_state(self.profile.paths, stale_runtime)
        return attempt, stale_runtime

    def _runtime_task_slice(self, workset_id: str, task_id: str):
        runtime_workset = next(
            row for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == workset_id
        )
        return (
            next(
                (row for row in runtime_workset.task_states if row.task_id == task_id),
                None,
            ),
            next(
                (row for row in runtime_workset.task_claims if row.task_id == task_id),
                None,
            ),
            tuple(
                row for row in runtime_workset.attempts if row.task_id == task_id
            ),
        )

    def _add_active_sibling(
        self,
        *,
        workset_id: str,
        retained_task_id: str,
        sibling_task_id: str = "RACE-C",
    ):
        upsert_workset(
            self.profile,
            {
                "id": workset_id,
                "title": "Task-scoped merge race",
                "tasks": [
                    {
                        "id": retained_task_id,
                        "title": retained_task_id,
                        "intent": "retain target",
                    },
                    {
                        "id": sibling_task_id,
                        "title": sibling_task_id,
                        "intent": "preserve concurrent sibling",
                    },
                ],
            },
        )
        return start_task(
            self.profile,
            workset_id=workset_id,
            task_id=sibling_task_id,
            actor="owner",
            prompt_receipt=create_prompt_receipt(
                "Preserve the concurrent sibling.", source="unit-test"
            ),
        )

    def _inject_decision_owned_workset_release(self, attempt_id: str) -> None:
        events = load_events(self.profile.paths.events_file)
        request_row = next(
            event
            for event in events
            if event.get("type") == "task.finalization.request"
            and event.get("payload", {}).get("attempt_id") == attempt_id
        )
        decision_row = next(
            event
            for event in events
            if event.get("type") == "task.finalization.decision"
            and event.get("payload", {}).get("attempt_id") == attempt_id
        )
        request = request_row["payload"]
        runtime = load_runtime_state(self.profile.paths)
        runtime_attempt = next(
            row
            for workset in runtime.worksets
            if workset.workset_id == request["workset_id"]
            for row in workset.attempts
            if row.attempt_id == attempt_id
        )
        if runtime_attempt.status == "in_progress":
            finished_attempt = backlog_module._terminal_attempt_from_request(
                runtime_attempt,
                request=request,
                ended_at=decision_row["payload"]["ended_at"],
            )
        else:
            finished_attempt = runtime_attempt
        payloads = backlog_module._finalization_owned_payloads(
            request_event_id=request_row["event_id"],
            decision_event_id=decision_row["event_id"],
            request=request,
            finished_attempt=finished_attempt,
        )
        append_event(
            self.profile.paths.events_file,
            event_id=backlog_module._finalization_owned_event_id(
                decision_event_id=decision_row["event_id"],
                event_type="workset.release",
            ),
            event_type="workset.release",
            actor="owner",
            payload=payloads["workset.release"],
        )

    def test_workset_round_trip_ignores_legacy_markdown_files(self) -> None:
        legacy_backlog = self.profile.paths.control_dir / "backlog.md"
        legacy_backlog.parent.mkdir(parents=True, exist_ok=True)
        legacy_backlog.write_text("```json backlog-task\nnot valid anymore\n```", encoding="utf-8")

        workset = upsert_workset(
            self.profile,
            {
                "id": "foundation",
                "title": "Foundation",
                "scope": {"kind": "repo", "paths": ["src/blackdog_core"]},
                "visibility": {"kind": "workset"},
                "workspace": {"identity": "blackdog-main"},
                "branch_intent": {"target_branch": "main", "integration_branch": "main"},
                "tasks": [
                    {
                        "id": "FOUND-1",
                        "title": "Create planning store",
                        "intent": "replace backlog markdown with planning.json",
                        "paths": ["src/blackdog_core/backlog.py"],
                        "docs": ["docs/FILE_FORMATS.md"],
                        "checks": ["make test"],
                    }
                ],
            },
        )

        self.assertEqual(workset.workset_id, "foundation")
        planning_state = load_planning_state(self.profile.paths)
        self.assertEqual(len(planning_state.worksets), 1)
        self.assertEqual(planning_state.worksets[0].tasks[0].task_id, "FOUND-1")
        self.assertTrue(self.profile.paths.planning_file.is_file())
        self.assertTrue(legacy_backlog.is_file())

    def test_planning_store_protocol_allows_non_json_backends(self) -> None:
        store = _MemoryPlanningStore()
        state = PlanningState(
            schema_version=1,
            store_version="blackdog.planning/vnext1",
            worksets=(
                Workset(
                    workset_id="memory",
                    title="Memory",
                    scope={},
                    visibility={},
                    policies={},
                    workspace={},
                    branch_intent={},
                    tasks=(
                        TaskSpec(
                            task_id="MEM-1",
                            title="Stored in memory",
                            intent="prove provider boundary",
                            description=None,
                            depends_on=(),
                            paths=(),
                            docs=(),
                            checks=(),
                            metadata={},
                        ),
                    ),
                    metadata={},
                ),
            ),
        )

        save_planning_state(self.profile.paths, state, store=store)
        loaded = load_planning_state(self.profile.paths, store=store)

        self.assertEqual(loaded.worksets[0].workset_id, "memory")
        self.assertEqual(loaded.worksets[0].tasks[0].task_id, "MEM-1")

    def test_runtime_save_preserves_concurrently_added_worksets(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "first",
                "title": "First",
                "tasks": [{"id": "F-1", "title": "First task", "intent": "seed stale writer"}],
            },
        )
        stale_state = load_runtime_state(self.profile.paths)

        upsert_workset(
            self.profile,
            {
                "id": "second",
                "title": "Second",
                "tasks": [{"id": "S-1", "title": "Second task", "intent": "concurrent writer"}],
            },
        )

        save_runtime_state(self.profile.paths, stale_state)

        current = load_runtime_state(self.profile.paths)
        self.assertEqual({workset.workset_id for workset in current.worksets}, {"first", "second"})

    def test_next_ready_tasks_follow_workset_dag_and_runtime_state(self) -> None:
        payload = {
            "id": "rewrite",
            "title": "Rewrite",
            "workspace": {"identity": "rewrite-workspace"},
            "branch_intent": {"target_branch": "main", "integration_branch": "main"},
            "tasks": [
                {
                    "id": "RW-1",
                    "title": "Replace planning store",
                    "intent": "introduce planning.json",
                },
                {
                    "id": "RW-2",
                    "title": "Rebuild snapshot",
                    "intent": "project worksets into runtime_model",
                    "depends_on": ["RW-1"],
                },
            ],
        }

        upsert_workset(self.profile, payload)
        planning_state = load_planning_state(self.profile.paths)
        runtime_state = load_runtime_state(self.profile.paths)
        self.assertEqual(
            [(workset.workset_id, task.task_id) for workset, task in next_ready_tasks(planning_state, runtime_state=runtime_state)],
            [("rewrite", "RW-1")],
        )

        upsert_workset(
            self.profile,
            {
                **payload,
                "task_states": [{"task_id": "RW-1", "status": "done"}],
            },
        )
        runtime_state = load_runtime_state(self.profile.paths)
        self.assertEqual(
            [(workset.workset_id, task.task_id) for workset, task in next_ready_tasks(planning_state, runtime_state=runtime_state)],
            [("rewrite", "RW-2")],
        )

    def test_json_runtime_round_trip_uses_typed_runtime_rows(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "runtime",
                "title": "Runtime",
                "tasks": [{"id": "RUN-1", "title": "Track runtime", "intent": "write runtime.json"}],
                "task_states": [
                    {
                        "task_id": "RUN-1",
                        "status": "in_progress",
                        "note": "editing",
                    }
                ],
            },
        )
        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())

        self.assertEqual(runtime_state.worksets[0].workset_id, "runtime")
        self.assertEqual(runtime_state.worksets[0].task_states[0].status, "in_progress")
        self.assertIsNone(runtime_state.worksets[0].workset_claim)
        self.assertEqual(runtime_state.worksets[0].task_claims, ())
        self.assertEqual(runtime_state.worksets[0].attempts, ())

    def test_runtime_loader_reads_legacy_managed_claims_and_start_task_migrates_them(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "legacy-runtime",
                "title": "Legacy runtime",
                "tasks": [{"id": "LEG-1", "title": "Migrate claim", "intent": "continue from old runtime state"}],
            },
        )
        self.profile.paths.runtime_file.write_text(
            json.dumps(
                {
                    "schema_version": RUNTIME_SCHEMA_VERSION,
                    "store_version": RUNTIME_STORE_VERSION,
                    "worksets": [
                        {
                            "id": "legacy-runtime",
                            "workset_claim": {
                                "actor": "legacy-manager",
                                "execution_model": "workset_manager",
                                "claimed_at": "2026-04-17T10:00:00-07:00",
                                "note": "legacy note",
                            },
                            "task_claims": [],
                            "task_states": [],
                            "attempts": [],
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertEqual(runtime_state.worksets[0].workset_claim.execution_model, "workset_manager")

        attempt = start_task(
            self.profile,
            workset_id="legacy-runtime",
            task_id="LEG-1",
            actor="codex",
            prompt_receipt=create_prompt_receipt(
                "Migrate the legacy managed claim into the direct WTAM flow.",
                recorded_at="2026-04-17T10:05:00-07:00",
                source="unit-test",
                mode="raw",
            ),
        )

        self.assertEqual(attempt.execution_model, "direct_wtam")
        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertEqual(runtime_state.worksets[0].workset_claim.actor, "codex")
        self.assertEqual(runtime_state.worksets[0].workset_claim.execution_model, "direct_wtam")
        self.assertEqual(runtime_state.worksets[0].workset_claim.note, "legacy note")
        self.assertEqual(runtime_state.worksets[0].task_claims[0].execution_model, "direct_wtam")

    def test_finish_task_releases_last_legacy_managed_workset_claim(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "legacy-finish",
                "title": "Legacy finish",
                "tasks": [{"id": "LEG-1", "title": "Finish task", "intent": "release the leftover legacy claim"}],
            },
        )
        prompt_receipt = create_prompt_receipt(
            "Finish the leftover legacy attempt.",
            recorded_at="2026-04-17T11:00:00-07:00",
            source="unit-test",
            mode="raw",
        )
        self.profile.paths.runtime_file.write_text(
            json.dumps(
                {
                    "schema_version": RUNTIME_SCHEMA_VERSION,
                    "store_version": RUNTIME_STORE_VERSION,
                    "worksets": [
                        {
                            "id": "legacy-finish",
                            "workset_claim": {
                                "actor": "legacy-manager",
                                "execution_model": "workset_manager",
                                "claimed_at": "2026-04-17T10:55:00-07:00",
                            },
                            "task_claims": [
                                {
                                    "task_id": "LEG-1",
                                    "actor": "codex",
                                    "execution_model": "direct_wtam",
                                    "claimed_at": "2026-04-17T11:00:00-07:00",
                                    "attempt_id": "LEG-1-legacy",
                                }
                            ],
                            "task_states": [
                                {
                                    "task_id": "LEG-1",
                                    "status": "in_progress",
                                    "updated_at": "2026-04-17T11:00:00-07:00",
                                }
                            ],
                            "attempts": [
                                {
                                    "attempt_id": "LEG-1-legacy",
                                    "task_id": "LEG-1",
                                    "status": "in_progress",
                                    "actor": "codex",
                                    "started_at": "2026-04-17T11:00:00-07:00",
                                    "execution_model": "direct_wtam",
                                    "prompt_receipt": {
                                        "text": prompt_receipt.text,
                                        "prompt_hash": prompt_receipt.prompt_hash,
                                        "recorded_at": prompt_receipt.recorded_at,
                                        "source": prompt_receipt.source,
                                        "mode": prompt_receipt.mode,
                                    },
                                    "user_prompt_receipt": {
                                        "text": prompt_receipt.text,
                                        "prompt_hash": prompt_receipt.prompt_hash,
                                        "recorded_at": prompt_receipt.recorded_at,
                                        "source": prompt_receipt.source,
                                        "mode": prompt_receipt.mode,
                                    },
                                    "changed_paths": [],
                                    "validations": [],
                                    "residuals": [],
                                    "followup_candidates": [],
                                }
                            ],
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        finished = finish_task(
            self.profile,
            workset_id="legacy-finish",
            task_id="LEG-1",
            attempt_id="LEG-1-legacy",
            actor="codex",
            status="success",
            summary="finished the migrated legacy task",
        )

        self.assertEqual(finished.execution_model, "direct_wtam")
        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertIsNone(runtime_state.worksets[0].workset_claim)
        self.assertEqual(runtime_state.worksets[0].task_claims, ())

    def test_start_and_finish_task_record_attempt_stats(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "direct",
                "title": "Direct",
                "workspace": {"identity": "direct-workspace"},
                "branch_intent": {"target_branch": "main", "integration_branch": "feature/direct"},
                "tasks": [
                    {"id": "DIR-1", "title": "Start task", "intent": "capture direct-agent attempt"},
                ],
            },
        )

        attempt = start_task(
            self.profile,
            workset_id="direct",
            task_id="DIR-1",
            actor="codex",
            workspace_mode="git-worktree",
            worktree_role="linked",
            worktree_path="/tmp/direct-worktree",
            branch="feature/direct",
            start_commit="0123456789abcdef",
            model="gpt-5.4",
            reasoning_effort="high",
            prompt_receipt=create_prompt_receipt(
                "Implement the direct slice and record runtime stats.",
                recorded_at="2026-04-12T09:00:00-07:00",
                source="unit-test",
                mode="tuned",
            ),
            user_prompt_receipt=create_prompt_receipt(
                "User asked to implement the direct slice.",
                recorded_at="2026-04-12T08:59:00-07:00",
                source="user-test",
                mode="raw",
            ),
            setup_receipt={
                "schema_version": 2,
                "status": "ok",
                "blockers": [],
                "guard_receipts": [
                    {
                        "schema_version": 1,
                        "id": "repo-policy",
                        "phase": "task_begin",
                        "config_sha256": "0" * 64,
                        "status": "passed",
                        "reason_code": "policy_passed",
                        "message": "Repository policy passed.",
                        "required_inputs": [],
                    }
                ],
                "probes": [],
            },
            note="starting work",
        )
        self.assertEqual(attempt.status, "in_progress")
        self.assertEqual(attempt.workspace_identity, "direct-workspace")
        self.assertEqual(attempt.branch, "feature/direct")
        self.assertEqual(attempt.worktree_role, "linked")
        self.assertEqual(attempt.worktree_path, "/tmp/direct-worktree")
        self.assertEqual(attempt.start_commit, "0123456789abcdef")
        self.assertEqual(attempt.execution_model, "direct_wtam")
        self.assertEqual(attempt.prompt_receipt.prompt_hash, create_prompt_receipt("Implement the direct slice and record runtime stats.").prompt_hash)
        self.assertEqual(attempt.user_prompt_receipt.source, "user-test")
        self.assertEqual(attempt.setup_receipt["status"], "ok")
        self.assertEqual(attempt.setup_receipt["guard_receipts"][0]["id"], "repo-policy")

        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertEqual(runtime_state.worksets[0].workset_claim.actor, "codex")
        self.assertEqual(runtime_state.worksets[0].workset_claim.execution_model, "direct_wtam")
        self.assertEqual(runtime_state.worksets[0].task_claims[0].task_id, "DIR-1")
        self.assertEqual(runtime_state.worksets[0].task_claims[0].attempt_id, attempt.attempt_id)
        self.assertEqual(runtime_state.worksets[0].attempts[0].setup_receipt["status"], "ok")

        finished = finish_task(
            self.profile,
            workset_id="direct",
            task_id="DIR-1",
            attempt_id=attempt.attempt_id,
            actor="codex",
            status="success",
            summary="finished the direct slice",
            changed_paths=("src/blackdog_core/backlog.py",),
            residuals=("none",),
            followup_candidates=("ship it",),
            commit="abc123",
            landed_commit="def456",
            elapsed_seconds=42,
        )
        self.assertEqual(finished.status, "success")
        self.assertEqual(finished.elapsed_seconds, 42)

        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertEqual(runtime_state.worksets[0].task_states[0].status, "done")
        self.assertIsNone(runtime_state.worksets[0].workset_claim)
        self.assertEqual(runtime_state.worksets[0].task_claims, ())
        self.assertEqual(runtime_state.worksets[0].attempts[0].attempt_id, attempt.attempt_id)
        self.assertEqual(runtime_state.worksets[0].attempts[0].commit, "abc123")
        self.assertEqual(runtime_state.worksets[0].attempts[0].landed_commit, "def456")
        self.assertEqual(runtime_state.worksets[0].attempts[0].execution_model, "direct_wtam")
        self.assertEqual(runtime_state.worksets[0].attempts[0].prompt_receipt.source, "unit-test")
        self.assertEqual(runtime_state.worksets[0].attempts[0].prompt_receipt.mode, "tuned")
        self.assertEqual(runtime_state.worksets[0].attempts[0].user_prompt_receipt.source, "user-test")
        self.assertEqual(runtime_state.worksets[0].attempts[0].user_prompt_receipt.mode, "raw")
        self.assertEqual(runtime_state.worksets[0].attempts[0].setup_receipt["status"], "ok")
        events = [
            json.loads(line)
            for line in self.profile.paths.events_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        start_event = next(event for event in events if event["type"] == "task.start")
        self.assertEqual(
            start_event["payload"]["setup_receipt"]["guard_receipts"][0]["id"],
            "repo-policy",
        )

    def test_reconcile_landed_attempt_preserves_terminal_lineage_and_is_idempotent(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "reconcile",
                "title": "Reconcile",
                "tasks": [{"id": "REC-1", "title": "Repair ledger", "intent": "record the landed commit"}],
            },
        )
        started = start_task(
            self.profile,
            workset_id="reconcile",
            task_id="REC-1",
            actor="attempt-owner",
            prompt_receipt=create_prompt_receipt("Repair the ledger.", source="unit-test"),
        )
        failed = finish_task(
            self.profile,
            workset_id="reconcile",
            task_id="REC-1",
            attempt_id=started.attempt_id,
            actor="attempt-owner",
            status="failed",
            summary="runtime finalization failed after Git landing",
            validations=(ValidationRecord(name="unit", status="passed"),),
            elapsed_seconds=17,
            failure_class="unknown",
            recovery_action="inspect",
            operator_issue=True,
        )

        first = reconcile_landed_attempt(
            self.profile,
            workset_id="reconcile",
            task_id="REC-1",
            attempt_id=started.attempt_id,
            landed_commit="a" * 40,
            actor="commit-actor",
            changed_paths=("src/repaired.py",),
            reason="canonical commit proved reachable",
            proof={"reachable_from_target": True},
        )

        self.assertTrue(first["runtime_changed"])
        self.assertTrue(first["event_appended"])
        current = load_runtime_state(self.profile.paths)
        attempt = current.worksets[0].attempts[0]
        self.assertEqual(attempt.status, "success")
        self.assertEqual(attempt.actor, "attempt-owner")
        self.assertEqual(attempt.started_at, failed.started_at)
        self.assertEqual(attempt.ended_at, failed.ended_at)
        self.assertEqual(attempt.summary, failed.summary)
        self.assertEqual(attempt.validations, failed.validations)
        self.assertEqual(attempt.elapsed_seconds, 17)
        self.assertEqual(attempt.changed_paths, ("src/repaired.py",))
        self.assertEqual(attempt.landed_commit, "a" * 40)
        self.assertIsNone(attempt.failure_class)
        self.assertIsNone(attempt.recovery_action)
        self.assertFalse(attempt.operator_issue)
        self.assertEqual(current.worksets[0].task_states[0].status, "done")
        self.assertIsNone(current.worksets[0].task_states[0].failure_class)

        second = reconcile_landed_attempt(
            self.profile,
            workset_id="reconcile",
            task_id="REC-1",
            attempt_id=started.attempt_id,
            landed_commit="a" * 40,
            actor="commit-actor",
            changed_paths=("src/repaired.py",),
            reason="retry",
        )
        self.assertFalse(second["runtime_changed"])
        self.assertFalse(second["event_appended"])
        reconciliation_events = [
            row
            for row in (
                json.loads(line)
                for line in self.profile.paths.events_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if row["type"] == "task.landing.reconciled"
        ]
        self.assertEqual(len(reconciliation_events), 1)
        self.assertEqual(reconciliation_events[0]["actor"], "commit-actor")
        self.assertEqual(reconciliation_events[0]["payload"]["attempt_actor"], "attempt-owner")

    def test_reconcile_landed_attempt_retry_repairs_event_after_append_failure(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "repair-event",
                "title": "Repair event",
                "tasks": [{"id": "REC-1", "title": "Repair event", "intent": "exercise retry"}],
            },
        )
        started = start_task(
            self.profile,
            workset_id="repair-event",
            task_id="REC-1",
            actor="owner",
            prompt_receipt=create_prompt_receipt("Repair the event.", source="unit-test"),
        )
        finish_task(
            self.profile,
            workset_id="repair-event",
            task_id="REC-1",
            attempt_id=started.attempt_id,
            actor="owner",
            status="blocked",
            summary="event append will fail",
        )
        with patch("blackdog_core.backlog.append_event", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(OSError, "disk unavailable"):
                reconcile_landed_attempt(
                    self.profile,
                    workset_id="repair-event",
                    task_id="REC-1",
                    attempt_id=started.attempt_id,
                    landed_commit="b" * 40,
                    actor="owner",
                    changed_paths=("repaired.txt",),
                )

        current = load_runtime_state(self.profile.paths)
        self.assertEqual(current.worksets[0].attempts[0].status, "success")
        repaired = reconcile_landed_attempt(
            self.profile,
            workset_id="repair-event",
            task_id="REC-1",
            attempt_id=started.attempt_id,
            landed_commit="b" * 40,
            actor="owner",
            changed_paths=("repaired.txt",),
        )
        self.assertFalse(repaired["runtime_changed"])
        self.assertTrue(repaired["event_appended"])
        self.assertTrue(repaired["event_repaired"])

    def test_abandoned_reconciliation_requires_exact_explicit_native_eligibility(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "abandoned-reconcile",
                "title": "Abandoned reconcile",
                "tasks": [
                    {
                        "id": "REC-1",
                        "title": "Guard abandoned correction",
                        "intent": "require product-proved native abort eligibility",
                    }
                ],
            },
        )
        started = start_task(
            self.profile,
            workset_id="abandoned-reconcile",
            task_id="REC-1",
            actor="owner",
            prompt_receipt=create_prompt_receipt(
                "Guard abandoned reconciliation.",
                source="unit-test",
            ),
        )
        finish_task(
            self.profile,
            workset_id="abandoned-reconcile",
            task_id="REC-1",
            attempt_id=started.attempt_id,
            actor="owner",
            status="abandoned",
            summary="Terminal without native landing proof.",
        )
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        kwargs = {
            "profile": self.profile,
            "workset_id": "abandoned-reconcile",
            "task_id": "REC-1",
            "attempt_id": started.attempt_id,
            "landed_commit": "d" * 40,
            "actor": "auditor",
            "changed_paths": ("abandoned.txt",),
        }

        with self.assertRaisesRegex(BacklogError, "explicit native abort-complete eligibility"):
            reconcile_landed_attempt(**kwargs)
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

        with self.assertRaisesRegex(BacklogError, "exact attempt and canonical candidate"):
            reconcile_landed_attempt(
                **kwargs,
                abandoned_eligibility=AbandonedLandingEligibility(
                    attempt_id=started.attempt_id,
                    transaction_id="native-abort-transaction",
                    canonical_candidate="e" * 40,
                ),
            )
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

        corrected = reconcile_landed_attempt(
            **kwargs,
            abandoned_eligibility=AbandonedLandingEligibility(
                attempt_id=started.attempt_id,
                transaction_id="native-abort-transaction",
                canonical_candidate="d" * 40,
            ),
        )
        self.assertEqual(corrected["previous_status"], "abandoned")
        self.assertTrue(corrected["runtime_changed"])

    def test_reconcile_landed_attempt_concurrent_apply_appends_exactly_one_event(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "concurrent-reconcile",
                "title": "Concurrent reconcile",
                "tasks": [{"id": "REC-1", "title": "Reconcile once", "intent": "serialize event append"}],
            },
        )
        started = start_task(
            self.profile,
            workset_id="concurrent-reconcile",
            task_id="REC-1",
            actor="owner",
            prompt_receipt=create_prompt_receipt("Reconcile concurrently.", source="unit-test"),
        )
        finish_task(
            self.profile,
            workset_id="concurrent-reconcile",
            task_id="REC-1",
            attempt_id=started.attempt_id,
            actor="owner",
            status="failed",
            summary="runtime finalization failed after Git landing",
        )
        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_reconcile_landed_attempt_worker,
                args=(
                    str(self.root),
                    "concurrent-reconcile",
                    "REC-1",
                    started.attempt_id,
                    start_event,
                    result_queue,
                ),
            )
            for _ in range(4)
        ]

        self._run_workers(processes, start_event, result_queue)

        reconciliation_events = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event["type"] == "task.landing.reconciled"
        ]
        self.assertEqual(len(reconciliation_events), 1)
        self.assertEqual(
            reconciliation_events[0]["payload"]["reconciliation_id"],
            reconcile_landed_attempt(
                self.profile,
                workset_id="concurrent-reconcile",
                task_id="REC-1",
                attempt_id=started.attempt_id,
                landed_commit="c" * 40,
                actor="concurrent-auditor",
                changed_paths=("concurrent.txt",),
            )["reconciliation_id"],
        )

    def test_abandoned_attempt_releases_claims_and_cancels_task(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "abandon",
                "title": "Abandon",
                "tasks": [{"id": "AB-1", "title": "Abort task", "intent": "release the claim without completing work"}],
            },
        )

        attempt = start_task(
            self.profile,
            workset_id="abandon",
            task_id="AB-1",
            actor="codex",
            prompt_receipt=create_prompt_receipt("Abort the direct slice.", source="unit-test"),
        )
        finished = finish_task(
            self.profile,
            workset_id="abandon",
            task_id="AB-1",
            attempt_id=attempt.attempt_id,
            actor="codex",
            status="abandoned",
            summary="abandoned the slice",
        )

        self.assertEqual(finished.status, "abandoned")
        self.assertEqual(finished.failure_class, "abandoned")
        self.assertTrue(finished.operator_issue)
        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertEqual(runtime_state.worksets[0].task_states[0].status, "canceled")
        self.assertEqual(runtime_state.worksets[0].task_states[0].failure_class, "abandoned")
        self.assertIsNone(runtime_state.worksets[0].workset_claim)
        self.assertEqual(runtime_state.worksets[0].task_claims, ())

    def test_stale_claim_release_derives_claim_topology_and_retries_without_writes(self) -> None:
        topologies = (
            ("stale-last", False, True, True),
            ("stale-remaining", True, True, False),
            ("stale-no-workset", False, False, False),
            ("stale-null-attempt", False, True, True),
        )
        for workset_id, remaining_claim, workset_claim_present, releases_workset in topologies:
            with self.subTest(workset_id=workset_id):
                _attempt, stale_runtime = self._stale_claim_fixture(
                    workset_id,
                    remaining_claim=remaining_claim,
                    workset_claim_present=workset_claim_present,
                    target_attempt_id_present=workset_id != "stale-null-attempt",
                    legacy_timestamps=workset_id == "stale-null-attempt",
                )
                before_workset = next(
                    row for row in stale_runtime.worksets
                    if row.workset_id == workset_id
                )
                result = release_stale_task_claim(
                    self.profile,
                    workset_id=workset_id,
                    task_id="STALE-A",
                    status="abandoned",
                    summary="release the stale claim",
                    note="operator approved",
                )
                self.assertEqual(result.release_workset_claim, releases_workset)
                self.assertEqual(
                    result.workset_release_event_id is not None,
                    releases_workset,
                )
                runtime = load_runtime_state(self.profile.paths)
                current = next(
                    row for row in runtime.worksets if row.workset_id == workset_id
                )
                self.assertEqual(
                    [claim.task_id for claim in current.task_claims],
                    ["STALE-B"] if remaining_claim else [],
                )
                self.assertEqual(
                    current.workset_claim,
                    None if releases_workset else before_workset.workset_claim,
                )
                self.assertEqual(current.attempts, before_workset.attempts)
                target_state = next(
                    state for state in current.task_states
                    if state.task_id == "STALE-A"
                )
                self.assertEqual(target_state.status, "canceled")
                matching = [
                    event
                    for event in load_events(self.profile.paths.events_file)
                    if event.get("payload", {}).get("workset_id") == workset_id
                    and event.get("type")
                    in {
                        "task.stale-claim-release.request",
                        "task.stale-claim-release.decision",
                        "task.release",
                        "workset.release",
                    }
                ]
                self.assertEqual(
                    [event["type"] for event in matching],
                    [
                        "task.stale-claim-release.request",
                        "task.stale-claim-release.decision",
                        "task.release",
                        *( ["workset.release"] if releases_workset else [] ),
                    ],
                )
                runtime_bytes = self.profile.paths.runtime_file.read_bytes()
                event_bytes = self.profile.paths.events_file.read_bytes()
                retried = release_stale_task_claim(
                    self.profile,
                    workset_id=workset_id,
                    task_id="STALE-A",
                    status="abandoned",
                    summary="release the stale claim",
                    note="operator approved",
                    expected_request_event_id=result.request_event_id,
                    expected_decision_event_id=result.decision_event_id,
                )
                self.assertEqual(retried.request_event_id, result.request_event_id)
                self.assertEqual(retried.decision_event_id, result.decision_event_id)
                self.assertFalse(retried.runtime_changed)
                self.assertFalse(retried.request_event_appended)
                self.assertFalse(retried.decision_event_appended)
                self.assertFalse(retried.task_release_event_appended)
                self.assertFalse(retried.workset_release_event_appended)
                self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_bytes)
                self.assertEqual(self.profile.paths.events_file.read_bytes(), event_bytes)

    def test_stale_claim_release_rejects_type_only_and_phantom_owned_tamper(self) -> None:
        self._stale_claim_fixture("stale-type-tamper")
        result = release_stale_task_claim(
            self.profile,
            workset_id="stale-type-tamper",
            task_id="STALE-A",
            status="failed",
            summary="release stale",
        )
        rows = list(load_events(self.profile.paths.events_file))
        request = next(
            row for row in rows if row.get("event_id") == result.request_event_id
        )
        request["type"] = "task.unrelated"
        self.profile.paths.events_file.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            StaleClaimReleaseConflictError, "conflicting event type"
        ):
            pending_stale_claim_release(
                self.profile,
                workset_id="stale-type-tamper",
                task_id="STALE-A",
            )

        self._stale_claim_fixture(
            "stale-phantom",
            remaining_claim=True,
        )
        phantom_result = release_stale_task_claim(
            self.profile,
            workset_id="stale-phantom",
            task_id="STALE-A",
            status="failed",
            summary="preserve other claim",
        )
        forbidden_id = backlog_module._stale_claim_release_owned_event_id(
            decision_event_id=phantom_result.decision_event_id,
            event_type="workset.release",
        )
        append_event(
            self.profile.paths.events_file,
            event_id=forbidden_id,
            event_type="workset.release",
            actor="owner",
            payload={"workset_id": "stale-phantom"},
        )
        with self.assertRaisesRegex(
            StaleClaimReleaseConflictError, "unexpected workset.release"
        ):
            pending_stale_claim_release(
                self.profile,
                workset_id="stale-phantom",
                task_id="STALE-A",
            )

    def test_stale_claim_release_rejects_bool_int_tamper_in_all_owned_layers(self) -> None:
        cases = (
            ("repaired-record", "decision", "repaired_task_record"),
            ("embedded-event", "decision", "task_release_event_payload"),
            ("owned-event", "owned", "task_release_event_payload"),
        )
        for suffix, row_kind, payload_field in cases:
            with self.subTest(layer=suffix):
                workset_id = f"stale-bool-{suffix}"
                self._stale_claim_fixture(workset_id)
                result = release_stale_task_claim(
                    self.profile,
                    workset_id=workset_id,
                    task_id="STALE-A",
                    status="failed",
                    summary="reject loose bool equality",
                )
                rows = list(load_events(self.profile.paths.events_file))
                if row_kind == "decision":
                    target = next(
                        row for row in rows
                        if row.get("event_id") == result.decision_event_id
                    )
                    target["payload"][payload_field]["prompt_issue"] = 0
                else:
                    target = next(
                        row for row in rows
                        if row.get("event_id") == result.task_release_event_id
                    )
                    target["payload"]["prompt_issue"] = 0
                self.profile.paths.events_file.write_text(
                    "".join(
                        json.dumps(row, sort_keys=True) + "\n" for row in rows
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(StaleClaimReleaseConflictError):
                    pending_stale_claim_release(
                        self.profile,
                        workset_id=workset_id,
                        task_id="STALE-A",
                    )

    def test_stale_claim_release_rejects_identity_type_actor_payload_and_duplicate_tamper(self) -> None:
        cases = (
            ("request-id", "request", "id"),
            ("request-type", "request", "type"),
            ("request-actor", "request", "actor"),
            ("request-payload", "request", "payload"),
            ("request-schema-type", "request", "schema-type"),
            ("request-duplicate", "request", "duplicate"),
            ("decision-id", "decision", "id"),
            ("decision-type", "decision", "type"),
            ("decision-actor", "decision", "actor"),
            ("decision-payload", "decision", "payload"),
            ("decision-bool-type", "decision", "schema-type"),
            ("decision-duplicate", "decision", "duplicate"),
            ("task-owned-id", "task", "id"),
            ("task-owned-type", "task", "type"),
            ("task-owned-actor", "task", "actor"),
            ("task-owned-payload", "task", "payload"),
            ("task-owned-duplicate", "task", "duplicate"),
            ("workset-owned-id", "workset", "id"),
            ("workset-owned-type", "workset", "type"),
            ("workset-owned-actor", "workset", "actor"),
            ("workset-owned-payload", "workset", "payload"),
            ("workset-owned-duplicate", "workset", "duplicate"),
        )
        for suffix, layer, mutation in cases:
            with self.subTest(case=suffix):
                workset_id = f"stale-tamper-{suffix}"
                self._stale_claim_fixture(workset_id)
                result = release_stale_task_claim(
                    self.profile,
                    workset_id=workset_id,
                    task_id="STALE-A",
                    status="failed",
                    summary="create canonical tamper target",
                )
                event_id = {
                    "request": result.request_event_id,
                    "decision": result.decision_event_id,
                    "task": result.task_release_event_id,
                    "workset": result.workset_release_event_id,
                }[layer]
                self.assertIsNotNone(event_id)
                rows = [
                    json.loads(json.dumps(row))
                    for row in load_events(self.profile.paths.events_file)
                ]
                target = next(row for row in rows if row.get("event_id") == event_id)
                if mutation == "id":
                    target["event_id"] = hashlib.sha256(
                        f"tampered-{suffix}".encode("utf-8")
                    ).hexdigest()
                elif mutation == "type":
                    target["type"] = "task.tampered"
                elif mutation == "actor":
                    target["actor"] = "intruder"
                elif mutation == "payload":
                    target["payload"]["summary"] = "tampered summary"
                elif mutation == "schema-type":
                    if layer == "request":
                        target["payload"]["schema_version"] = True
                    else:
                        target["payload"]["release_workset_claim"] = 1
                elif mutation == "duplicate":
                    rows.append(json.loads(json.dumps(target)))
                else:
                    self.fail(f"unknown mutation {mutation}")
                self.profile.paths.events_file.write_text(
                    "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                    encoding="utf-8",
                )
                tampered_bytes = self.profile.paths.events_file.read_bytes()
                runtime_bytes = self.profile.paths.runtime_file.read_bytes()
                with self.assertRaises(StaleClaimReleaseConflictError):
                    pending_stale_claim_release(
                        self.profile,
                        workset_id=workset_id,
                        task_id="STALE-A",
                    )
                self.assertEqual(
                    self.profile.paths.events_file.read_bytes(), tampered_bytes
                )
                self.assertEqual(
                    self.profile.paths.runtime_file.read_bytes(), runtime_bytes
                )

    def test_stale_claim_release_workset_gate_rejects_orphan_decision(self) -> None:
        workset_id = "stale-orphan-decision"
        self._stale_claim_fixture(workset_id)
        result = release_stale_task_claim(
            self.profile,
            workset_id=workset_id,
            task_id="STALE-A",
            status="failed",
            summary="create a decision",
        )
        rows = [
            row for row in load_events(self.profile.paths.events_file)
            if row.get("event_id") != result.request_event_id
        ]
        self.profile.paths.events_file.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        runtime = load_runtime_state(self.profile.paths)
        with self.assertRaisesRegex(
            StaleClaimReleaseConflictError, "no durable request"
        ):
            require_no_pending_stale_claim_release_for_workset(
                self.profile,
                workset_id=workset_id,
                runtime_state=runtime,
            )

    def test_stale_claim_release_post_save_faults_report_truthful_partial_phase(self) -> None:
        real_append_event_once = append_event_once
        cases = (
            ("before-task-event", False, "runtime_finalized", []),
            (
                "after-task-event",
                True,
                "event_finalization_partial",
                ["task.release"],
            ),
        )
        for suffix, append_then_fail, phase, expected_owned in cases:
            with self.subTest(boundary=suffix):
                workset_id = f"stale-fault-{suffix}"
                self._stale_claim_fixture(workset_id)

                def fail_owned(*args, **kwargs):
                    if kwargs.get("event_type") != "task.release":
                        return real_append_event_once(*args, **kwargs)
                    if not append_then_fail:
                        raise StoreError("injected task.release outage")
                    real_append_event_once(*args, **kwargs)
                    raise StoreError("injected post-task.release outage")

                with patch(
                    "blackdog_core.backlog.append_event_once",
                    side_effect=fail_owned,
                ):
                    with self.assertRaises(
                        StaleClaimReleaseFinalizationError
                    ) as raised:
                        release_stale_task_claim(
                            self.profile,
                            workset_id=workset_id,
                            task_id="STALE-A",
                            status="failed",
                            summary="recover after owned event fault",
                        )
                partial = raised.exception
                self.assertTrue(partial.mutation_started)
                self.assertEqual(partial.mutation_phase, phase)
                runtime = next(
                    row for row in load_runtime_state(self.profile.paths).worksets
                    if row.workset_id == workset_id
                )
                self.assertEqual(runtime.task_claims, ())
                self.assertIsNone(runtime.workset_claim)
                owned = [
                    event["type"]
                    for event in load_events(self.profile.paths.events_file)
                    if event.get("event_id")
                    in {
                        partial.task_release_event_id,
                        partial.workset_release_event_id,
                    }
                ]
                self.assertEqual(owned, expected_owned)
                repaired = release_stale_task_claim(
                    self.profile,
                    workset_id=workset_id,
                    task_id="STALE-A",
                    status="failed",
                    summary="recover after owned event fault",
                    expected_request_event_id=partial.request_event_id,
                    expected_decision_event_id=partial.decision_event_id,
                )
                self.assertEqual(repaired.request_event_id, partial.request_event_id)
                self.assertEqual(repaired.decision_event_id, partial.decision_event_id)
                final_owned = [
                    event["type"]
                    for event in load_events(self.profile.paths.events_file)
                    if event.get("event_id")
                    in {
                        repaired.task_release_event_id,
                        repaired.workset_release_event_id,
                    }
                ]
                self.assertEqual(final_owned, ["task.release", "workset.release"])

    def test_stale_release_partial_stages_allow_unrelated_state_drift_and_gate_claim_mutation(self) -> None:
        for boundary in ("decision", "runtime"):
            with self.subTest(boundary=boundary):
                workset_id = f"stale-unrelated-drift-{boundary}"
                self._stale_claim_fixture(workset_id)
                upsert_workset(
                    self.profile,
                    {
                        "id": workset_id,
                        "title": "Stale unrelated drift",
                        "tasks": [
                            {"id": "STALE-A", "title": "A", "intent": "repair A"},
                            {"id": "DRIFT-C", "title": "C", "intent": "mutate C"},
                        ],
                    },
                )
                if boundary == "decision":
                    runtime_path = self.profile.paths.runtime_file.resolve()
                    real_replace = os.replace
                    injected = False

                    def fail_runtime(source, destination):
                        nonlocal injected
                        if Path(destination).resolve() == runtime_path and not injected:
                            injected = True
                            raise OSError("stop after durable decision")
                        return real_replace(source, destination)

                    fault = patch(
                        "blackdog_core.state.os.replace",
                        side_effect=fail_runtime,
                    )
                else:
                    real_append = append_event_once
                    injected = False

                    def fail_task_event(*args, **kwargs):
                        nonlocal injected
                        if kwargs.get("event_type") == "task.release" and not injected:
                            injected = True
                            raise StoreError("stop after runtime replacement")
                        return real_append(*args, **kwargs)

                    fault = patch(
                        "blackdog_core.backlog.append_event_once",
                        side_effect=fail_task_event,
                    )
                with fault:
                    with self.assertRaises(
                        StaleClaimReleaseFinalizationError
                    ) as raised:
                        release_stale_task_claim(
                            self.profile,
                            workset_id=workset_id,
                            task_id="STALE-A",
                            status="failed",
                            summary="repair after unrelated drift",
                        )
                partial = raised.exception
                self.assertEqual(
                    partial.mutation_phase,
                    "preflight" if boundary == "decision" else "runtime_finalized",
                )
                before_start_runtime = self.profile.paths.runtime_file.read_bytes()
                before_start_events = self.profile.paths.events_file.read_bytes()
                with self.assertRaises(StaleClaimReleaseConflictError):
                    start_task(
                        self.profile,
                        workset_id=workset_id,
                        task_id="DRIFT-C",
                        actor="owner",
                        prompt_receipt=create_prompt_receipt(
                            "Claim mutation must wait.", source="unit-test"
                        ),
                    )
                self.assertEqual(
                    self.profile.paths.runtime_file.read_bytes(), before_start_runtime
                )
                self.assertEqual(
                    self.profile.paths.events_file.read_bytes(), before_start_events
                )
                set_task_runtime_status(
                    self.profile,
                    workset_id=workset_id,
                    task_id="DRIFT-C",
                    actor="owner",
                    status="canceled",
                    summary="unrelated state-only drift is allowed",
                )
                drift_slice = self._runtime_task_slice(workset_id, "DRIFT-C")
                planning_before_prune = self.profile.paths.planning_file.read_bytes()
                with self.assertRaises((BacklogError, StaleClaimReleaseConflictError)):
                    upsert_workset(
                        self.profile,
                        {
                            "id": workset_id,
                            "title": "Stale unrelated drift",
                            "tasks": [
                                {"id": "DRIFT-C", "title": "C", "intent": "retain C"}
                            ],
                        },
                    )
                self.assertEqual(
                    self.profile.paths.planning_file.read_bytes(), planning_before_prune
                )
                repaired = release_stale_task_claim(
                    self.profile,
                    workset_id=workset_id,
                    task_id="STALE-A",
                    status="failed",
                    summary="repair after unrelated drift",
                    expected_request_event_id=partial.request_event_id,
                    expected_decision_event_id=partial.decision_event_id,
                )
                self.assertEqual(repaired.request_event_id, partial.request_event_id)
                self.assertEqual(
                    self._runtime_task_slice(workset_id, "DRIFT-C"), drift_slice
                )
                set_task_runtime_status(
                    self.profile,
                    workset_id=workset_id,
                    task_id="DRIFT-C",
                    actor="owner",
                    status="planned",
                    summary="claim mutation may resume",
                )
                started = start_task(
                    self.profile,
                    workset_id=workset_id,
                    task_id="DRIFT-C",
                    actor="owner",
                    prompt_receipt=create_prompt_receipt(
                        "Claim mutation now proceeds.", source="unit-test"
                    ),
                )
                self.assertEqual(started.status, "in_progress")

    def test_stale_claim_release_concurrent_exact_retries_append_once(self) -> None:
        workset_id = "stale-concurrent-exact"
        self._stale_claim_fixture(workset_id)
        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_stale_claim_release_worker,
                args=(
                    str(self.root),
                    workset_id,
                    "STALE-A",
                    "failed",
                    "same durable semantics",
                    start_event,
                    result_queue,
                ),
            )
            for _index in range(4)
        ]
        self._run_workers(processes, start_event, result_queue)
        events = [
            event for event in load_events(self.profile.paths.events_file)
            if event.get("payload", {}).get("workset_id") == workset_id
            and event.get("type")
            in {
                "task.stale-claim-release.request",
                "task.stale-claim-release.decision",
                "task.release",
                "workset.release",
            }
        ]
        self.assertEqual(
            [event["type"] for event in events],
            [
                "task.stale-claim-release.request",
                "task.stale-claim-release.decision",
                "task.release",
                "workset.release",
            ],
        )
        self.assertEqual(len({event["event_id"] for event in events}), 4)

    def test_task_start_and_finish_cannot_cross_stale_release_decision(self) -> None:
        for operation in ("start", "finish"):
            with self.subTest(operation=operation):
                workset_id = f"stale-decision-crossing-{operation}"
                self._stale_claim_fixture(workset_id)
                upsert_workset(
                    self.profile,
                    {
                        "id": workset_id,
                        "title": "Decision crossing",
                        "tasks": [
                            {"id": "STALE-A", "title": "A", "intent": "release A"},
                            {"id": "CROSS-B", "title": "B", "intent": operation},
                        ],
                    },
                )
                active_b = None
                if operation == "finish":
                    active_b = start_task(
                        self.profile,
                        workset_id=workset_id,
                        task_id="CROSS-B",
                        actor="owner",
                        prompt_receipt=create_prompt_receipt(
                            "Finish B after A decision.", source="unit-test"
                        ),
                    )
                decision_entered = threading.Event()
                release_decision = threading.Event()
                contender_started = threading.Event()
                contender_finished = threading.Event()
                release_result: list[object] = []
                contender_result: list[object] = []
                real_append = append_event_once

                def block_decision(*args, **kwargs):
                    if kwargs.get("event_type") == "task.stale-claim-release.decision":
                        decision_entered.set()
                        if not release_decision.wait(timeout=10):
                            raise TimeoutError("stale decision was not released")
                    return real_append(*args, **kwargs)

                def run_release() -> None:
                    try:
                        release_result.append(
                            release_stale_task_claim(
                                self.profile,
                                workset_id=workset_id,
                                task_id="STALE-A",
                                status="failed",
                                summary=f"serialize before {operation}",
                            )
                        )
                    except Exception as exc:
                        release_result.append(exc)

                def run_contender() -> None:
                    contender_started.set()
                    try:
                        if operation == "start":
                            contender_result.append(
                                start_task(
                                    self.profile,
                                    workset_id=workset_id,
                                    task_id="CROSS-B",
                                    actor="owner",
                                    prompt_receipt=create_prompt_receipt(
                                        "Start B after A decision.", source="unit-test"
                                    ),
                                )
                            )
                        else:
                            assert active_b is not None
                            contender_result.append(
                                finish_task(
                                    self.profile,
                                    workset_id=workset_id,
                                    task_id="CROSS-B",
                                    attempt_id=active_b.attempt_id,
                                    actor="owner",
                                    status="success",
                                    summary="finish B after A decision",
                                )
                            )
                    except Exception as exc:
                        contender_result.append(exc)
                    finally:
                        contender_finished.set()

                with patch(
                    "blackdog_core.backlog.append_event_once",
                    side_effect=block_decision,
                ):
                    release_thread = threading.Thread(target=run_release)
                    release_thread.start()
                    self.assertTrue(decision_entered.wait(timeout=5))
                    contender_thread = threading.Thread(target=run_contender)
                    contender_thread.start()
                    self.assertTrue(contender_started.wait(timeout=5))
                    self.assertFalse(contender_finished.wait(timeout=0.2))
                    release_decision.set()
                    release_thread.join(timeout=10)
                    contender_thread.join(timeout=10)
                self.assertFalse(release_thread.is_alive())
                self.assertFalse(contender_thread.is_alive())
                self.assertEqual(len(release_result), 1)
                self.assertEqual(len(contender_result), 1)
                self.assertNotIsInstance(release_result[0], Exception)
                self.assertNotIsInstance(contender_result[0], Exception)
                events = [
                    event for event in load_events(self.profile.paths.events_file)
                    if event.get("payload", {}).get("workset_id") == workset_id
                ]
                decision_index = next(
                    index for index, event in enumerate(events)
                    if event.get("type") == "task.stale-claim-release.decision"
                )
                contender_event_type = (
                    "task.claim" if operation == "start" else "task.finish"
                )
                contender_index = next(
                    index for index, event in enumerate(events)
                    if event.get("type") == contender_event_type
                    and event.get("payload", {}).get("task_id") == "CROSS-B"
                )
                self.assertLess(decision_index, contender_index)
                task_release_index = next(
                    index for index, event in enumerate(events)
                    if event.get("type") == "task.release"
                    and event.get("payload", {}).get("task_id") == "STALE-A"
                    and event.get("payload", {}).get("recovery") == "stale_claim"
                )
                self.assertLess(task_release_index, contender_index)
                workset_release_indexes = [
                    index for index, event in enumerate(events)
                    if event.get("type") == "workset.release"
                    and event.get("payload", {}).get("recovery") == "stale_claim"
                ]
                self.assertEqual(bool(workset_release_indexes), operation == "start")
                for index in workset_release_indexes:
                    self.assertLess(index, contender_index)
                runtime_workset = next(
                    row for row in load_runtime_state(self.profile.paths).worksets
                    if row.workset_id == workset_id
                )
                if operation == "start":
                    self.assertEqual(
                        [claim.task_id for claim in runtime_workset.task_claims],
                        ["CROSS-B"],
                    )
                    self.assertIsNotNone(runtime_workset.workset_claim)
                else:
                    self.assertEqual(runtime_workset.task_claims, ())
                    self.assertIsNone(runtime_workset.workset_claim)

    def test_stale_claim_release_concurrent_semantics_choose_one_generation(self) -> None:
        workset_id = "stale-concurrent-conflict"
        self._stale_claim_fixture(workset_id)
        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_stale_claim_release_worker,
                args=(
                    str(self.root),
                    workset_id,
                    "STALE-A",
                    "failed",
                    summary,
                    start_event,
                    result_queue,
                ),
            )
            for summary in ("semantic alpha", "semantic beta")
        ]
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=20)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
        results = [result_queue.get(timeout=5) for _process in processes]
        self.assertEqual(sum(row[0] == "ok" for row in results), 1)
        self.assertEqual(sum(row[0] == "error" for row in results), 1)
        self.assertIn(
            "different semantics",
            next(row[2] for row in results if row[0] == "error"),
        )
        request_rows = [
            event for event in load_events(self.profile.paths.events_file)
            if event.get("type") == "task.stale-claim-release.request"
            and event.get("payload", {}).get("workset_id") == workset_id
        ]
        self.assertEqual(len(request_rows), 1)
        self.assertIn(
            request_rows[0]["payload"]["summary"],
            {"semantic alpha", "semantic beta"},
        )

    def test_two_stale_tasks_serialize_and_only_final_releaser_releases_workset(self) -> None:
        workset_id = "stale-concurrent-two-tasks"
        self._stale_claim_fixture(workset_id, remaining_claim=True)
        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_stale_claim_release_worker,
                args=(
                    str(self.root),
                    workset_id,
                    task_id,
                    "failed",
                    f"release {task_id}",
                    start_event,
                    result_queue,
                ),
            )
            for task_id in ("STALE-A", "STALE-B")
        ]
        self._run_workers(processes, start_event, result_queue)
        events = [
            event for event in load_events(self.profile.paths.events_file)
            if event.get("payload", {}).get("workset_id") == workset_id
        ]
        self.assertEqual(
            sum(event["type"] == "task.release" for event in events),
            2,
        )
        self.assertEqual(
            sum(event["type"] == "workset.release" for event in events),
            1,
        )
        runtime = next(
            row for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == workset_id
        )
        self.assertEqual(runtime.task_claims, ())
        self.assertIsNone(runtime.workset_claim)

    def test_task_scoped_start_preserves_sibling_added_after_planning_load(self) -> None:
        workset_id = "planning-race-start-add"
        upsert_workset(
            self.profile,
            {
                "id": workset_id,
                "title": "Start race",
                "tasks": [{"id": "RACE-A", "title": "A", "intent": "start A"}],
            },
        )
        loaded = threading.Event()
        proceed = threading.Event()
        store = _BlockingPlanningStore(loaded, proceed)
        result: list[object] = []

        def run_start() -> None:
            try:
                result.append(
                    start_task(
                        self.profile,
                        workset_id=workset_id,
                        task_id="RACE-A",
                        actor="owner",
                        prompt_receipt=create_prompt_receipt(
                            "Start A after the race.", source="unit-test"
                        ),
                        planning_store=store,
                    )
                )
            except Exception as exc:
                result.append(exc)

        thread = threading.Thread(target=run_start)
        thread.start()
        self.assertTrue(loaded.wait(timeout=5))
        self._add_active_sibling(
            workset_id=workset_id,
            retained_task_id="RACE-A",
        )
        sibling_before = self._runtime_task_slice(workset_id, "RACE-C")
        proceed.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertNotIsInstance(result[0], Exception)
        self.assertEqual(
            self._runtime_task_slice(workset_id, "RACE-C"),
            sibling_before,
        )

    def test_task_scoped_finish_paths_preserve_sibling_added_after_planning_load(self) -> None:
        for durable in (False, True):
            with self.subTest(durable=durable):
                workset_id = f"planning-race-finish-{'durable' if durable else 'legacy'}"
                upsert_workset(
                    self.profile,
                    {
                        "id": workset_id,
                        "title": "Finish race",
                        "tasks": [{"id": "RACE-A", "title": "A", "intent": "finish A"}],
                    },
                )
                attempt = start_task(
                    self.profile,
                    workset_id=workset_id,
                    task_id="RACE-A",
                    actor="owner",
                    prompt_receipt=create_prompt_receipt(
                        "Finish A after the race.", source="unit-test"
                    ),
                )
                loaded = threading.Event()
                proceed = threading.Event()
                store = _BlockingPlanningStore(loaded, proceed)
                result: list[object] = []

                def run_finish() -> None:
                    try:
                        result.append(
                            finish_task(
                                self.profile,
                                workset_id=workset_id,
                                task_id="RACE-A",
                                attempt_id=attempt.attempt_id,
                                actor="owner",
                                status="success",
                                summary="finish A after concurrent upsert",
                                finalization_id=(
                                    f"{workset_id}-finalization" if durable else None
                                ),
                                planning_store=store,
                            )
                        )
                    except Exception as exc:
                        result.append(exc)

                thread = threading.Thread(target=run_finish)
                thread.start()
                self.assertTrue(loaded.wait(timeout=5))
                self._add_active_sibling(
                    workset_id=workset_id,
                    retained_task_id="RACE-A",
                )
                sibling_before = self._runtime_task_slice(workset_id, "RACE-C")
                proceed.set()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(result), 1)
                self.assertNotIsInstance(result[0], Exception)
                self.assertEqual(
                    self._runtime_task_slice(workset_id, "RACE-C"),
                    sibling_before,
                )

    def test_task_state_transitions_preserve_sibling_added_after_planning_load(self) -> None:
        for operation in ("cancel", "reopen"):
            with self.subTest(operation=operation):
                workset_id = f"planning-race-{operation}"
                upsert_workset(
                    self.profile,
                    {
                        "id": workset_id,
                        "title": "State race",
                        "tasks": [{"id": "RACE-A", "title": "A", "intent": operation}],
                    },
                )
                if operation == "reopen":
                    set_task_runtime_status(
                        self.profile,
                        workset_id=workset_id,
                        task_id="RACE-A",
                        actor="owner",
                        status="canceled",
                        summary="prepare reopen",
                    )
                loaded = threading.Event()
                proceed = threading.Event()
                store = _BlockingPlanningStore(loaded, proceed)
                result: list[object] = []

                def run_transition() -> None:
                    try:
                        result.append(
                            set_task_runtime_status(
                                self.profile,
                                workset_id=workset_id,
                                task_id="RACE-A",
                                actor="owner",
                                status=(
                                    "canceled" if operation == "cancel" else "planned"
                                ),
                                summary=f"{operation} after concurrent upsert",
                                planning_store=store,
                            )
                        )
                    except Exception as exc:
                        result.append(exc)

                thread = threading.Thread(target=run_transition)
                thread.start()
                self.assertTrue(loaded.wait(timeout=5))
                self._add_active_sibling(
                    workset_id=workset_id,
                    retained_task_id="RACE-A",
                )
                sibling_before = self._runtime_task_slice(workset_id, "RACE-C")
                proceed.set()
                thread.join(timeout=10)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(result), 1)
                self.assertNotIsInstance(result[0], Exception)
                self.assertEqual(
                    self._runtime_task_slice(workset_id, "RACE-C"),
                    sibling_before,
                )

    def test_reconciliation_preserves_sibling_added_after_planning_load(self) -> None:
        workset_id = "planning-race-reconcile"
        upsert_workset(
            self.profile,
            {
                "id": workset_id,
                "title": "Reconcile race",
                "tasks": [{"id": "RACE-A", "title": "A", "intent": "reconcile A"}],
            },
        )
        attempt = start_task(
            self.profile,
            workset_id=workset_id,
            task_id="RACE-A",
            actor="owner",
            prompt_receipt=create_prompt_receipt("Fail A first.", source="unit-test"),
        )
        finish_task(
            self.profile,
            workset_id=workset_id,
            task_id="RACE-A",
            attempt_id=attempt.attempt_id,
            actor="owner",
            status="failed",
            summary="failed before canonical landing proof",
        )
        loaded = threading.Event()
        proceed = threading.Event()
        store = _BlockingPlanningStore(loaded, proceed)
        result: list[object] = []

        def run_reconcile() -> None:
            try:
                result.append(
                    reconcile_landed_attempt(
                        self.profile,
                        workset_id=workset_id,
                        task_id="RACE-A",
                        attempt_id=attempt.attempt_id,
                        landed_commit="d" * 40,
                        actor="auditor",
                        changed_paths=("race.txt",),
                        planning_store=store,
                    )
                )
            except Exception as exc:
                result.append(exc)

        thread = threading.Thread(target=run_reconcile)
        thread.start()
        self.assertTrue(loaded.wait(timeout=5))
        self._add_active_sibling(
            workset_id=workset_id,
            retained_task_id="RACE-A",
        )
        sibling_before = self._runtime_task_slice(workset_id, "RACE-C")
        proceed.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertNotIsInstance(result[0], Exception)
        self.assertEqual(
            self._runtime_task_slice(workset_id, "RACE-C"),
            sibling_before,
        )

    def test_stale_release_preserves_sibling_added_after_planning_load(self) -> None:
        workset_id = "planning-race-stale-release"
        self._stale_claim_fixture(workset_id)
        loaded = threading.Event()
        proceed = threading.Event()
        store = _BlockingPlanningStore(loaded, proceed)
        result: list[object] = []

        def run_release() -> None:
            try:
                result.append(
                    release_stale_task_claim(
                        self.profile,
                        workset_id=workset_id,
                        task_id="STALE-A",
                        status="failed",
                        summary="release after concurrent upsert",
                        planning_store=store,
                    )
                )
            except Exception as exc:
                result.append(exc)

        thread = threading.Thread(target=run_release)
        thread.start()
        self.assertTrue(loaded.wait(timeout=5))
        self._add_active_sibling(
            workset_id=workset_id,
            retained_task_id="STALE-A",
        )
        sibling_before = self._runtime_task_slice(workset_id, "RACE-C")
        proceed.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertNotIsInstance(result[0], Exception)
        self.assertEqual(
            self._runtime_task_slice(workset_id, "RACE-C"),
            sibling_before,
        )
        runtime_workset = next(
            row for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == workset_id
        )
        self.assertEqual(
            [claim.task_id for claim in runtime_workset.task_claims],
            ["RACE-C"],
        )
        self.assertIsNotNone(runtime_workset.workset_claim)

    def test_task_mutator_rejects_target_removed_after_planning_load(self) -> None:
        workset_id = "planning-race-remove-target"
        upsert_workset(
            self.profile,
            {
                "id": workset_id,
                "title": "Remove race",
                "tasks": [
                    {"id": "RACE-A", "title": "A", "intent": "removed target"},
                    {"id": "RACE-B", "title": "B", "intent": "retained task"},
                ],
            },
        )
        loaded = threading.Event()
        proceed = threading.Event()
        store = _BlockingPlanningStore(loaded, proceed)
        result: list[object] = []

        def run_start() -> None:
            try:
                result.append(
                    start_task(
                        self.profile,
                        workset_id=workset_id,
                        task_id="RACE-A",
                        actor="owner",
                        prompt_receipt=create_prompt_receipt(
                            "Do not resurrect A.", source="unit-test"
                        ),
                        planning_store=store,
                    )
                )
            except Exception as exc:
                result.append(exc)

        thread = threading.Thread(target=run_start)
        thread.start()
        self.assertTrue(loaded.wait(timeout=5))
        upsert_workset(
            self.profile,
            {
                "id": workset_id,
                "title": "Remove race",
                "tasks": [
                    {"id": "RACE-B", "title": "B", "intent": "retained task"}
                ],
            },
        )
        proceed.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], BacklogError)
        self.assertIn("Unknown task 'RACE-A'", str(result[0]))
        planning = load_planning_state(self.profile.paths)
        workset = next(row for row in planning.worksets if row.workset_id == workset_id)
        self.assertEqual([task.task_id for task in workset.tasks], ["RACE-B"])
        self.assertEqual(self._runtime_task_slice(workset_id, "RACE-A"), (None, None, ()))

    def test_upsert_rejects_live_prune_and_preserves_terminal_attempt_history(self) -> None:
        for suffix, make_stale in (("active", False), ("stale-claim", True)):
            with self.subTest(live_slice=suffix):
                workset_id = f"prune-reject-{suffix}"
                upsert_workset(
                    self.profile,
                    {
                        "id": workset_id,
                        "title": "Prune guard",
                        "tasks": [
                            {"id": "PRUNE-A", "title": "A", "intent": "live"},
                            {"id": "PRUNE-B", "title": "B", "intent": "retain"},
                        ],
                    },
                )
                attempt = start_task(
                    self.profile,
                    workset_id=workset_id,
                    task_id="PRUNE-A",
                    actor="owner",
                    prompt_receipt=create_prompt_receipt("Own A.", source="unit-test"),
                )
                if make_stale:
                    runtime = load_runtime_state(self.profile.paths)
                    runtime_workset = next(
                        row for row in runtime.worksets if row.workset_id == workset_id
                    )
                    save_runtime_state(
                        self.profile.paths,
                        merge_workset_runtime(
                            runtime,
                            workset_id=workset_id,
                            task_ids={"PRUNE-A", "PRUNE-B"},
                            incoming_records=None,
                            incoming_attempts=(
                                replace(
                                    attempt,
                                    status="blocked",
                                    ended_at=now_iso(),
                                    summary="stale",
                                ),
                            ),
                        ),
                    )
                with self.assertRaisesRegex(BacklogError, "cannot remove claimed task"):
                    upsert_workset(
                        self.profile,
                        {
                            "id": workset_id,
                            "title": "Prune guard",
                            "tasks": [
                                {"id": "PRUNE-B", "title": "B", "intent": "retain"}
                            ],
                        },
                    )

        for legacy_missing_end in (False, True):
            with self.subTest(terminal_history=legacy_missing_end):
                workset_id = f"prune-terminal-{legacy_missing_end}"
                upsert_workset(
                    self.profile,
                    {
                        "id": workset_id,
                        "title": "Terminal history",
                        "tasks": [
                            {"id": "PRUNE-A", "title": "A", "intent": "terminal"},
                            {"id": "PRUNE-B", "title": "B", "intent": "retain"},
                        ],
                    },
                )
                attempt = start_task(
                    self.profile,
                    workset_id=workset_id,
                    task_id="PRUNE-A",
                    actor="owner",
                    prompt_receipt=create_prompt_receipt("Finish A.", source="unit-test"),
                )
                finished = finish_task(
                    self.profile,
                    workset_id=workset_id,
                    task_id="PRUNE-A",
                    attempt_id=attempt.attempt_id,
                    actor="owner",
                    status="failed",
                    summary="terminal history",
                )
                if legacy_missing_end:
                    runtime = load_runtime_state(self.profile.paths)
                    save_runtime_state(
                        self.profile.paths,
                        merge_workset_runtime(
                            runtime,
                            workset_id=workset_id,
                            task_ids={"PRUNE-A", "PRUNE-B"},
                            incoming_records=None,
                            incoming_attempts=(replace(finished, ended_at=None),),
                        ),
                    )
                upsert_workset(
                    self.profile,
                    {
                        "id": workset_id,
                        "title": "Terminal history",
                        "tasks": [
                            {"id": "PRUNE-B", "title": "B", "intent": "retain"}
                        ],
                    },
                )
                state, claim, attempts = self._runtime_task_slice(
                    workset_id, "PRUNE-A"
                )
                self.assertIsNone(state)
                self.assertIsNone(claim)
                self.assertEqual([row.attempt_id for row in attempts], [attempt.attempt_id])
                self.assertEqual(attempts[0].ended_at is None, legacy_missing_end)

    def test_canceled_tasks_are_not_ready_until_reopened(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "cancel",
                "title": "Cancel",
                "tasks": [{"id": "CAN-1", "title": "Cancel task", "intent": "hide this work"}],
            },
        )

        set_task_runtime_status(
            self.profile,
            workset_id="cancel",
            task_id="CAN-1",
            actor="codex",
            status="canceled",
            summary="not needed",
        )
        planning_state = load_planning_state(self.profile.paths)
        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertEqual(next_ready_tasks(planning_state, runtime_state=runtime_state), [])
        self.assertEqual(runtime_state.worksets[0].task_states[0].status, "canceled")

        set_task_runtime_status(
            self.profile,
            workset_id="cancel",
            task_id="CAN-1",
            actor="codex",
            status="planned",
            summary="needed again",
        )
        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertEqual(
            [(workset.workset_id, task.task_id) for workset, task in next_ready_tasks(planning_state, runtime_state=runtime_state)],
            [("cancel", "CAN-1")],
        )

    def test_concurrent_finish_and_cancel_preserve_same_workset_runtime_writes(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "concurrent-close-cancel",
                "title": "Concurrent Close Cancel",
                "tasks": [
                    {"id": "CON-1", "title": "Close task", "intent": "finish this attempt"},
                    {"id": "CON-2", "title": "Cancel task", "intent": "cancel this planned task"},
                ],
            },
        )
        attempt = start_task(
            self.profile,
            workset_id="concurrent-close-cancel",
            task_id="CON-1",
            actor="codex",
            prompt_receipt=create_prompt_receipt("Finish one task while canceling another.", source="unit-test"),
        )

        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_finish_task_worker,
                args=(str(self.root), "concurrent-close-cancel", "CON-1", attempt.attempt_id, start_event, result_queue),
            ),
            ctx.Process(
                target=_cancel_task_worker,
                args=(str(self.root), "concurrent-close-cancel", "CON-2", start_event, result_queue),
            ),
        ]
        self._run_workers(processes, start_event, result_queue)

        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        runtime = runtime_state.worksets[0]
        task_states = {task_state.task_id: task_state.status for task_state in runtime.task_states}
        attempts = {task_attempt.attempt_id: task_attempt for task_attempt in runtime.attempts}

        self.assertEqual(task_states["CON-1"], "done")
        self.assertEqual(task_states["CON-2"], "canceled")
        self.assertEqual(attempts[attempt.attempt_id].status, "success")
        self.assertEqual(runtime.task_claims, ())
        self.assertIsNone(runtime.workset_claim)

    def test_concurrent_cancels_preserve_all_runtime_state_writes(self) -> None:
        task_ids = tuple(f"CAN-{index}" for index in range(6))
        upsert_workset(
            self.profile,
            {
                "id": "concurrent-cancel",
                "title": "Concurrent Cancel",
                "tasks": [
                    {"id": task_id, "title": f"Cancel {task_id}", "intent": "cancel planned work"}
                    for task_id in task_ids
                ],
            },
        )

        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_cancel_task_worker,
                args=(str(self.root), "concurrent-cancel", task_id, start_event, result_queue),
            )
            for task_id in task_ids
        ]
        self._run_workers(processes, start_event, result_queue)

        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        task_states = {task_state.task_id: task_state.status for task_state in runtime_state.worksets[0].task_states}

        self.assertEqual(task_states, {task_id: "canceled" for task_id in task_ids})

    def test_append_event_once_is_durable_idempotent_and_conflict_strict(self) -> None:
        events_file = self.profile.paths.events_file
        payload = {"workset_id": "durable", "task_id": "DUR-1", "status": "success"}

        with patch("blackdog_core.state.os.fsync") as fsync:
            self.assertTrue(
                append_event_once(
                    events_file,
                    event_id="durable-event-id",
                    event_type="task.finish",
                    actor="owner",
                    payload=payload,
                )
            )
            self.assertFalse(
                append_event_once(
                    events_file,
                    event_id="durable-event-id",
                    event_type="task.finish",
                    actor="owner",
                    payload=payload,
                )
            )
        self.assertEqual(fsync.call_count, 2)
        self.assertEqual(len(load_events(events_file)), 1)

        conflicts = (
            {"event_type": "task.release", "actor": "owner", "payload": payload},
            {"event_type": "task.finish", "actor": "other", "payload": payload},
            {
                "event_type": "task.finish",
                "actor": "owner",
                "payload": {**payload, "status": "failed"},
            },
        )
        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                with self.assertRaisesRegex(StoreError, "already exists with different content"):
                    append_event_once(
                        events_file,
                        event_id="durable-event-id",
                        **conflict,
                    )

        append_event(
            events_file,
            event_id="durable-event-id",
            event_type="task.finish",
            actor="owner",
            payload=payload,
        )
        with self.assertRaisesRegex(StoreError, "occurs more than once"):
            append_event_once(
                events_file,
                event_id="durable-event-id",
                event_type="task.finish",
                actor="owner",
                payload=payload,
            )

    def test_append_event_once_repairs_an_fsync_interruption_without_duplication(self) -> None:
        events_file = self.profile.paths.events_file
        kwargs = {
            "event_id": "fsync-interruption",
            "event_type": "task.finish",
            "actor": "owner",
            "payload": {"attempt_id": "attempt-1", "status": "success"},
        }
        with patch("blackdog_core.state.os.fsync", side_effect=OSError("fsync interrupted")):
            with self.assertRaisesRegex(OSError, "fsync interrupted"):
                append_event_once(events_file, **kwargs)
        self.assertEqual(len(load_events(events_file)), 1)

        with patch("blackdog_core.state.os.fsync") as fsync:
            self.assertFalse(append_event_once(events_file, **kwargs))
        fsync.assert_called_once()
        self.assertEqual(len(load_events(events_file)), 1)

    def test_finish_task_finalization_retry_repairs_every_event_boundary(self) -> None:
        real_append_event_once = append_event_once
        for fail_after in range(4):
            with self.subTest(fail_after=fail_after):
                workset_id = f"durable-finish-{fail_after}"
                task_id = "FIN-1"
                finalization_id = f"finalization-{fail_after}"
                upsert_workset(
                    self.profile,
                    {
                        "id": workset_id,
                        "title": "Durable finish",
                        "tasks": [
                            {
                                "id": task_id,
                                "title": "Finalize durably",
                                "intent": "repair interrupted canonical events",
                            }
                        ],
                    },
                )
                attempt = start_task(
                    self.profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor="owner",
                    prompt_receipt=create_prompt_receipt("Finalize durably.", source="unit-test"),
                    note="active note",
                )
                finish_kwargs = {
                    "workset_id": workset_id,
                    "task_id": task_id,
                    "attempt_id": attempt.attempt_id,
                    "actor": "owner",
                    "status": "success",
                    "summary": "durably finalized",
                    "changed_paths": ("src/finalized.py",),
                    "validations": (ValidationRecord(name="unit", status="passed"),),
                    "residuals": ("none",),
                    "followup_candidates": ("publish",),
                    "commit": "a" * 40,
                    "landed_commit": "b" * 40,
                    "elapsed_seconds": None if fail_after == 0 else 17,
                    "note": "durable note",
                    "finalization_id": finalization_id,
                }
                call_count = 0

                def interrupted_append(*args, **kwargs):
                    nonlocal call_count
                    if kwargs.get("event_type") not in {
                        "task.release",
                        "workset.release",
                        "task.finish",
                    }:
                        return real_append_event_once(*args, **kwargs)
                    if fail_after == 0 and call_count == 0:
                        raise OSError("injected stop after runtime mutation")
                    result = real_append_event_once(*args, **kwargs)
                    call_count += 1
                    if call_count == fail_after:
                        raise OSError(f"injected stop after canonical event {fail_after}")
                    return result

                with patch("blackdog_core.backlog.append_event_once", side_effect=interrupted_append):
                    with self.assertRaisesRegex(OSError, "injected stop"):
                        finish_task(self.profile, **finish_kwargs)

                runtime_state = load_runtime_state(self.profile.paths)
                runtime = next(row for row in runtime_state.worksets if row.workset_id == workset_id)
                self.assertEqual(runtime.attempts[0].status, "success")
                self.assertEqual(runtime.task_states[0].status, "done")
                self.assertEqual(runtime.task_claims, ())
                self.assertIsNone(runtime.workset_claim)

                repaired = finish_task(self.profile, **finish_kwargs)
                self.assertEqual(repaired.status, "success")
                matching_events = [
                    event
                    for event in load_events(self.profile.paths.events_file)
                    if event.get("payload", {}).get("finalization_id") == finalization_id
                    and event.get("type") in {"task.release", "workset.release", "task.finish"}
                ]
                self.assertEqual(
                    [event["type"] for event in matching_events],
                    ["task.release", "workset.release", "task.finish"],
                )
                self.assertEqual(len({event["event_id"] for event in matching_events}), 3)

                runtime_before = self.profile.paths.runtime_file.read_bytes()
                events_before = self.profile.paths.events_file.read_bytes()
                third = finish_task(self.profile, **finish_kwargs)
                self.assertEqual(third, repaired)
                self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
                self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

    def test_finish_task_finalization_retry_rejects_semantic_mismatches(self) -> None:
        workset_id = "durable-conflict"
        task_id = "FIN-1"
        upsert_workset(
            self.profile,
            {
                "id": workset_id,
                "title": "Durable conflict",
                "tasks": [{"id": task_id, "title": "Conflict", "intent": "reject divergent retries"}],
            },
        )
        attempt = start_task(
            self.profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="owner",
            prompt_receipt=create_prompt_receipt("Reject divergent retries.", source="unit-test"),
        )
        finish_kwargs = {
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt.attempt_id,
            "actor": "owner",
            "status": "success",
            "summary": "durably finalized",
            "changed_paths": ("src/finalized.py",),
            "validations": (ValidationRecord(name="unit", status="passed"),),
            "residuals": ("none",),
            "followup_candidates": ("publish",),
            "commit": "a" * 40,
            "landed_commit": "b" * 40,
            "elapsed_seconds": 17,
            "note": "durable note",
            "finalization_id": "finalization-conflict",
        }
        finish_task(self.profile, **finish_kwargs)
        mismatches = {
            "status": "failed",
            "summary": "different summary",
            "changed_paths": ("src/other.py",),
            "validations": (ValidationRecord(name="unit", status="failed"),),
            "residuals": ("different",),
            "followup_candidates": ("different",),
            "commit": "c" * 40,
            "landed_commit": "d" * 40,
            "elapsed_seconds": 18,
            "failure_class": "unknown",
            "recovery_action": "inspect",
            "prompt_issue": True,
            "operator_issue": True,
            "note": "different note",
        }
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        for field, value in mismatches.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(BacklogError, "finalization request conflicts"):
                    finish_task(self.profile, **{**finish_kwargs, field: value})
        with self.assertRaisesRegex(BacklogError, "finalization request conflicts"):
            finish_task(
                self.profile,
                **{**finish_kwargs, "finalization_id": "different-finalization-identity"},
            )
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

    def test_finish_task_rejects_a_different_identity_before_repairing_partial_events(self) -> None:
        workset_id = "partial-identity-conflict"
        task_id = "FIN-1"
        upsert_workset(
            self.profile,
            {
                "id": workset_id,
                "title": "Partial identity conflict",
                "tasks": [{"id": task_id, "title": "Conflict", "intent": "preserve partial identity"}],
            },
        )
        attempt = start_task(
            self.profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="owner",
            prompt_receipt=create_prompt_receipt("Preserve partial identity.", source="unit-test"),
        )
        finish_kwargs = {
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt.attempt_id,
            "actor": "owner",
            "status": "success",
            "summary": "durably finalized",
            "changed_paths": ("src/finalized.py",),
            "commit": "a" * 40,
            "landed_commit": "b" * 40,
            "elapsed_seconds": 17,
            "finalization_id": "partial-identity-a",
        }
        real_append_event_once = append_event_once
        call_count = 0

        def stop_after_first(*args, **kwargs):
            nonlocal call_count
            if kwargs.get("event_type") not in {
                "task.release",
                "workset.release",
                "task.finish",
            }:
                return real_append_event_once(*args, **kwargs)
            result = real_append_event_once(*args, **kwargs)
            call_count += 1
            if call_count == 1:
                raise OSError("stop after first finalization event")
            return result

        with patch("blackdog_core.backlog.append_event_once", side_effect=stop_after_first):
            with self.assertRaisesRegex(OSError, "stop after first"):
                finish_task(self.profile, **finish_kwargs)
        partial_events = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("payload", {}).get("finalization_id") == "partial-identity-a"
            and event.get("type") in {"task.release", "workset.release", "task.finish"}
        ]
        self.assertEqual([event["type"] for event in partial_events], ["task.release"])

        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        with self.assertRaisesRegex(BacklogError, "finalization request conflicts"):
            finish_task(
                self.profile,
                **{**finish_kwargs, "finalization_id": "partial-identity-b"},
            )
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

    def test_finish_task_without_finalization_id_preserves_legacy_non_retryable_behavior(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "legacy-finish",
                "title": "Legacy finish",
                "tasks": [{"id": "LEG-1", "title": "Finish once", "intent": "preserve legacy behavior"}],
            },
        )
        attempt = start_task(
            self.profile,
            workset_id="legacy-finish",
            task_id="LEG-1",
            actor="owner",
            prompt_receipt=create_prompt_receipt("Finish once.", source="unit-test"),
        )
        kwargs = {
            "workset_id": "legacy-finish",
            "task_id": "LEG-1",
            "attempt_id": attempt.attempt_id,
            "actor": "owner",
            "status": "success",
            "summary": "finished once",
        }
        finish_task(self.profile, **kwargs)
        with self.assertRaisesRegex(BacklogError, "is not active"):
            finish_task(self.profile, **kwargs)
        terminal_events = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event["type"] in {"task.release", "workset.release", "task.finish"}
            and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
        ]
        self.assertTrue(terminal_events)
        self.assertTrue(all("finalization_id" not in event["payload"] for event in terminal_events))

    def test_concurrent_finalization_retries_append_each_logical_event_once(self) -> None:
        workset_id = "concurrent-finalization"
        task_id = "FIN-1"
        finalization_id = "concurrent-finalization-id"
        upsert_workset(
            self.profile,
            {
                "id": workset_id,
                "title": "Concurrent finalization",
                "tasks": [{"id": task_id, "title": "Finalize once", "intent": "serialize retries"}],
            },
        )
        attempt = start_task(
            self.profile,
            workset_id=workset_id,
            task_id=task_id,
            actor="owner",
            prompt_receipt=create_prompt_receipt("Finalize concurrently.", source="unit-test"),
        )
        with patch("blackdog_core.backlog.append_event_once", side_effect=OSError("injected event outage")):
            with self.assertRaisesRegex(OSError, "injected event outage"):
                finish_task(
                    self.profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    attempt_id=attempt.attempt_id,
                    actor="owner",
                    status="success",
                    summary="durably finalized",
                    changed_paths=("src/finalized.py",),
                    validations=(ValidationRecord(name="unit", status="passed"),),
                    residuals=("none",),
                    followup_candidates=("publish",),
                    commit="a" * 40,
                    landed_commit="b" * 40,
                    elapsed_seconds=17,
                    note="durable note",
                    finalization_id=finalization_id,
                )

        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_retry_finish_task_worker,
                args=(
                    str(self.root),
                    workset_id,
                    task_id,
                    attempt.attempt_id,
                    finalization_id,
                    start_event,
                    result_queue,
                ),
            )
            for _index in range(4)
        ]
        self._run_workers(processes, start_event, result_queue)

        matching_events = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("payload", {}).get("finalization_id") == finalization_id
            and event.get("type") in {"task.release", "workset.release", "task.finish"}
        ]
        self.assertEqual(
            [event["type"] for event in matching_events],
            ["task.release", "workset.release", "task.finish"],
        )
        runtime = next(
            row
            for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == workset_id
        )
        self.assertEqual(runtime.attempts[0].status, "success")
        self.assertEqual(runtime.task_states[0].status, "done")
        self.assertEqual(runtime.task_claims, ())
        self.assertIsNone(runtime.workset_claim)
        request_and_decision = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("payload", {}).get("attempt_id") == attempt.attempt_id
            and event.get("type") in {
                "task.finalization.request",
                "task.finalization.decision",
            }
        ]
        self.assertEqual(
            [event["type"] for event in request_and_decision],
            ["task.finalization.request", "task.finalization.decision"],
        )

    def test_append_event_once_uses_strict_canonical_json_semantics(self) -> None:
        events_file = self.profile.paths.events_file
        self.assertTrue(
            append_event_once(
                events_file,
                event_id="strict-json",
                event_type="task.finish",
                actor="owner",
                payload={"number": 1, "array": ("a", {"nested": 2})},
            )
        )
        self.assertFalse(
            append_event_once(
                events_file,
                event_id="strict-json",
                event_type="task.finish",
                actor="owner",
                payload={"array": ["a", {"nested": 2}], "number": 1},
            )
        )
        for conflicting_number in (True, 1.0):
            with self.subTest(conflicting_number=conflicting_number):
                with self.assertRaisesRegex(StoreError, "different content"):
                    append_event_once(
                        events_file,
                        event_id="strict-json",
                        event_type="task.finish",
                        actor="owner",
                        payload={
                            "number": conflicting_number,
                            "array": ["a", {"nested": 2}],
                        },
                    )
        invalid_payloads = (
            {"bad": {"set-value"}},
            {"bad": float("nan")},
            {1: "non-string-key"},
        )
        for index, payload in enumerate(invalid_payloads):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(StoreError, "non-JSON|non-finite|non-string"):
                    append_event_once(
                        events_file,
                        event_id=f"invalid-json-{index}",
                        event_type="task.finish",
                        actor="owner",
                        payload=payload,
                    )
        with self.assertRaisesRegex(StoreError, "nonempty actor"):
            append_event_once(
                events_file,
                event_id="numeric-actor",
                event_type="task.finish",
                actor=1,  # type: ignore[arg-type]
                payload={},
            )
        with self.assertRaisesRegex(StoreError, "nonempty event_type"):
            append_event_once(
                events_file,
                event_id="boolean-event-type",
                event_type=True,  # type: ignore[arg-type]
                actor="owner",
                payload={},
            )

    def test_append_event_once_serializes_concurrent_exact_requests(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_append_event_once_worker,
                args=(
                    str(self.profile.paths.events_file),
                    {"attempt_id": "attempt-1", "items": [1, 2, 3]},
                    start_event,
                    result_queue,
                ),
            )
            for _index in range(4)
        ]
        self._run_workers(processes, start_event, result_queue)
        rows = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("event_id") == "concurrent-strict-event"
        ]
        self.assertEqual(len(rows), 1)

    def test_finalization_request_faults_never_mutate_runtime_and_retry_exactly(self) -> None:
        real_append_event_once = append_event_once
        for fault in ("before", "after", "fsync"):
            with self.subTest(fault=fault):
                attempt, finish_kwargs = self._start_durable_fixture(f"intent-{fault}")

                if fault == "fsync":
                    context = patch(
                        "blackdog_core.state.os.fsync",
                        side_effect=OSError("injected request fsync failure"),
                    )
                else:
                    def fail_request(*args, **kwargs):
                        if kwargs.get("event_type") != "task.finalization.request":
                            return real_append_event_once(*args, **kwargs)
                        if fault == "before":
                            raise OSError("injected before request append")
                        result = real_append_event_once(*args, **kwargs)
                        raise OSError("injected after request append")

                    context = patch("blackdog_core.backlog.append_event_once", side_effect=fail_request)

                with context:
                    with self.assertRaisesRegex(OSError, "injected"):
                        finish_task(self.profile, **finish_kwargs)

                runtime = next(
                    row
                    for row in load_runtime_state(self.profile.paths).worksets
                    if row.workset_id == finish_kwargs["workset_id"]
                )
                current_attempt = next(
                    row for row in runtime.attempts if row.attempt_id == attempt.attempt_id
                )
                self.assertEqual(current_attempt.status, "in_progress")
                decisions = [
                    event
                    for event in load_events(self.profile.paths.events_file)
                    if event.get("type") == "task.finalization.decision"
                    and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
                ]
                self.assertEqual(decisions, [])

                finished = finish_task(self.profile, **finish_kwargs)
                self.assertEqual(finished.status, "success")
                request_rows = [
                    event
                    for event in load_events(self.profile.paths.events_file)
                    if event.get("type") == "task.finalization.request"
                    and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
                ]
                self.assertEqual(len(request_rows), 1)

    def test_finalization_runtime_replacement_faults_converge_from_either_side(self) -> None:
        real_replace = os.replace
        for fault in ("before", "after"):
            with self.subTest(fault=fault):
                attempt, finish_kwargs = self._start_durable_fixture(f"runtime-{fault}")
                runtime_path = self.profile.paths.runtime_file.resolve()
                injected = False

                def fail_runtime_replace(source, destination):
                    nonlocal injected
                    if Path(destination).resolve() != runtime_path or injected:
                        return real_replace(source, destination)
                    injected = True
                    if fault == "before":
                        raise OSError("injected before runtime replacement")
                    real_replace(source, destination)
                    raise OSError("injected after runtime replacement")

                with patch("blackdog_core.state.os.replace", side_effect=fail_runtime_replace):
                    with self.assertRaisesRegex(OSError, "runtime replacement"):
                        finish_task(self.profile, **finish_kwargs)

                interrupted = next(
                    row
                    for row in load_runtime_state(self.profile.paths).worksets
                    if row.workset_id == finish_kwargs["workset_id"]
                )
                interrupted_attempt = next(
                    row for row in interrupted.attempts if row.attempt_id == attempt.attempt_id
                )
                self.assertEqual(
                    interrupted_attempt.status,
                    "in_progress" if fault == "before" else "success",
                )
                decisions = [
                    event
                    for event in load_events(self.profile.paths.events_file)
                    if event.get("type") == "task.finalization.decision"
                    and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
                ]
                self.assertEqual(len(decisions), 1)

                repaired = finish_task(self.profile, **finish_kwargs)
                self.assertEqual(repaired.status, "success")
                canonical = [
                    event["type"]
                    for event in load_events(self.profile.paths.events_file)
                    if event.get("payload", {}).get("attempt_id") == attempt.attempt_id
                    and event.get("type") in {"task.release", "workset.release", "task.finish"}
                ]
                self.assertEqual(canonical, ["task.release", "workset.release", "task.finish"])

    def test_release_true_repairs_historical_event_after_later_same_workset_claim(self) -> None:
        attempt, finish_kwargs = self._start_durable_fixture(
            "release-true-drift",
            include_second_task=True,
        )
        real_replace = os.replace
        runtime_path = self.profile.paths.runtime_file.resolve()
        injected = False

        def fail_after_runtime_replace(source, destination):
            nonlocal injected
            real_replace(source, destination)
            if Path(destination).resolve() == runtime_path and not injected:
                injected = True
                raise OSError("injected after runtime replacement")

        with patch("blackdog_core.state.os.replace", side_effect=fail_after_runtime_replace):
            with self.assertRaisesRegex(OSError, "after runtime replacement"):
                finish_task(self.profile, **finish_kwargs)

        second_attempt = start_task(
            self.profile,
            workset_id=finish_kwargs["workset_id"],
            task_id="FIN-B",
            actor="owner",
            prompt_receipt=create_prompt_receipt("Start B.", source="unit-test"),
        )
        before = next(
            row
            for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == finish_kwargs["workset_id"]
        )
        before_b = (
            before.workset_claim,
            tuple(claim for claim in before.task_claims if claim.task_id == "FIN-B"),
            tuple(state for state in before.task_states if state.task_id == "FIN-B"),
            tuple(row for row in before.attempts if row.attempt_id == second_attempt.attempt_id),
        )

        finish_task(self.profile, **finish_kwargs)
        after = next(
            row
            for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == finish_kwargs["workset_id"]
        )
        after_b = (
            after.workset_claim,
            tuple(claim for claim in after.task_claims if claim.task_id == "FIN-B"),
            tuple(state for state in after.task_states if state.task_id == "FIN-B"),
            tuple(row for row in after.attempts if row.attempt_id == second_attempt.attempt_id),
        )
        self.assertEqual(after_b, before_b)
        releases = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("type") == "workset.release"
            and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
        ]
        self.assertEqual(len(releases), 1)
        self.assertTrue(
            next(
                event
                for event in load_events(self.profile.paths.events_file)
                if event.get("type") == "task.finalization.decision"
                and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
            )["payload"]["release_workset_claim"]
        )

    def test_release_false_preserves_existing_same_workset_claim_and_emits_no_release(self) -> None:
        attempt, finish_kwargs = self._start_durable_fixture(
            "release-false-drift",
            include_second_task=True,
        )
        second_attempt = start_task(
            self.profile,
            workset_id=finish_kwargs["workset_id"],
            task_id="FIN-B",
            actor="owner",
            prompt_receipt=create_prompt_receipt("Start B first.", source="unit-test"),
        )
        real_replace = os.replace
        runtime_path = self.profile.paths.runtime_file.resolve()
        injected = False

        def fail_after_runtime_replace(source, destination):
            nonlocal injected
            real_replace(source, destination)
            if Path(destination).resolve() == runtime_path and not injected:
                injected = True
                raise OSError("injected after runtime replacement")

        with patch("blackdog_core.state.os.replace", side_effect=fail_after_runtime_replace):
            with self.assertRaisesRegex(OSError, "after runtime replacement"):
                finish_task(self.profile, **finish_kwargs)

        before = next(
            row
            for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == finish_kwargs["workset_id"]
        )
        before_b = (
            before.workset_claim,
            tuple(claim for claim in before.task_claims if claim.task_id == "FIN-B"),
            tuple(row for row in before.attempts if row.attempt_id == second_attempt.attempt_id),
        )
        finish_task(self.profile, **finish_kwargs)
        after = next(
            row
            for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == finish_kwargs["workset_id"]
        )
        after_b = (
            after.workset_claim,
            tuple(claim for claim in after.task_claims if claim.task_id == "FIN-B"),
            tuple(row for row in after.attempts if row.attempt_id == second_attempt.attempt_id),
        )
        self.assertEqual(after_b, before_b)
        decision = next(
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("type") == "task.finalization.decision"
            and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
        )
        self.assertFalse(decision["payload"]["release_workset_claim"])
        releases = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("type") == "workset.release"
            and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
        ]
        self.assertEqual(releases, [])

    def test_event_backed_runtime_rollback_repairs_only_target_task_slice(self) -> None:
        attempt, finish_kwargs = self._start_durable_fixture(
            "runtime-rollback",
            include_second_task=True,
        )
        active_state = load_runtime_state(self.profile.paths)
        active_workset = next(
            row for row in active_state.worksets if row.workset_id == finish_kwargs["workset_id"]
        )
        active_attempt = next(row for row in active_workset.attempts if row.attempt_id == attempt.attempt_id)
        active_claim = next(row for row in active_workset.task_claims if row.task_id == "FIN-A")
        active_task_state = next(row for row in active_workset.task_states if row.task_id == "FIN-A")

        finish_task(self.profile, **finish_kwargs)
        second_attempt = start_task(
            self.profile,
            workset_id=finish_kwargs["workset_id"],
            task_id="FIN-B",
            actor="owner",
            prompt_receipt=create_prompt_receipt("Start B after A.", source="unit-test"),
        )
        current = load_runtime_state(self.profile.paths)
        current_workset = next(
            row for row in current.worksets if row.workset_id == finish_kwargs["workset_id"]
        )
        rolled_back = merge_workset_runtime(
            current,
            workset_id=finish_kwargs["workset_id"],
            task_ids={"FIN-A", "FIN-B"},
            incoming_records=(active_task_state,),
            incoming_workset_claim=current_workset.workset_claim,
            incoming_task_claims=(active_claim,),
            incoming_attempts=(active_attempt,),
        )
        save_runtime_state(self.profile.paths, rolled_back)

        before = next(
            row
            for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == finish_kwargs["workset_id"]
        )
        before_b = (
            before.workset_claim,
            tuple(claim for claim in before.task_claims if claim.task_id == "FIN-B"),
            tuple(state for state in before.task_states if state.task_id == "FIN-B"),
            tuple(row for row in before.attempts if row.attempt_id == second_attempt.attempt_id),
        )
        events_before = self.profile.paths.events_file.read_bytes()

        repaired = finish_task(self.profile, **finish_kwargs)
        self.assertEqual(repaired.status, "success")
        after = next(
            row
            for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == finish_kwargs["workset_id"]
        )
        after_b = (
            after.workset_claim,
            tuple(claim for claim in after.task_claims if claim.task_id == "FIN-B"),
            tuple(state for state in after.task_states if state.task_id == "FIN-B"),
            tuple(row for row in after.attempts if row.attempt_id == second_attempt.attempt_id),
        )
        self.assertEqual(after_b, before_b)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)

        runtime_before_third = self.profile.paths.runtime_file.read_bytes()
        events_before_third = self.profile.paths.events_file.read_bytes()
        finish_task(self.profile, **finish_kwargs)
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before_third)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before_third)

    def test_conflicting_owned_event_blocks_before_runtime_replacement(self) -> None:
        attempt, finish_kwargs = self._start_durable_fixture("conflicting-owned-row")
        real_replace = os.replace
        runtime_path = self.profile.paths.runtime_file.resolve()

        def fail_before_runtime_replace(source, destination):
            if Path(destination).resolve() == runtime_path:
                raise OSError("injected before runtime replacement")
            return real_replace(source, destination)

        with patch("blackdog_core.state.os.replace", side_effect=fail_before_runtime_replace):
            with self.assertRaisesRegex(OSError, "before runtime replacement"):
                finish_task(self.profile, **finish_kwargs)

        decision = next(
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("type") == "task.finalization.decision"
            and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
        )
        append_event(
            self.profile.paths.events_file,
            event_id=backlog_module._finalization_owned_event_id(
                decision_event_id=decision["event_id"],
                event_type="task.release",
            ),
            event_type="task.release",
            actor="owner",
            payload={"conflict": True},
        )
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        with self.assertRaisesRegex(BacklogError, "conflicting content"):
            finish_task(self.profile, **finish_kwargs)
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        runtime = next(
            row
            for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == finish_kwargs["workset_id"]
        )
        self.assertEqual(
            next(row for row in runtime.attempts if row.attempt_id == attempt.attempt_id).status,
            "in_progress",
        )

    def test_concurrent_conflicting_finalization_requests_choose_exactly_one(self) -> None:
        attempt, finish_kwargs = self._start_durable_fixture("concurrent-request-conflict")
        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        summaries = ("request alpha", "request beta")
        processes = [
            ctx.Process(
                target=_conflicting_finalization_request_worker,
                args=(
                    str(self.root),
                    finish_kwargs["workset_id"],
                    finish_kwargs["task_id"],
                    attempt.attempt_id,
                    finish_kwargs["finalization_id"],
                    summary,
                    start_event,
                    result_queue,
                ),
            )
            for summary in summaries
        ]
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=20)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
        results = [result_queue.get(timeout=5) for _process in processes]
        self.assertEqual(sum(result[0] == "ok" for result in results), 1)
        self.assertEqual(sum(result[0] == "error" for result in results), 1)

        request_rows = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("type") == "task.finalization.request"
            and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
        ]
        decision_rows = [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("type") == "task.finalization.decision"
            and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
        ]
        self.assertEqual(len(request_rows), 1)
        self.assertEqual(len(decision_rows), 1)
        runtime = next(
            row
            for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == finish_kwargs["workset_id"]
        )
        terminal = next(row for row in runtime.attempts if row.attempt_id == attempt.attempt_id)
        self.assertEqual(terminal.status, "success")
        self.assertIn(terminal.summary, summaries)
        self.assertEqual(terminal.summary, request_rows[0]["payload"]["summary"])

    def test_release_false_forbidden_row_cannot_back_active_runtime_rollback(self) -> None:
        attempt, finish_kwargs = self._start_durable_fixture(
            "release-false-active-conflict",
            include_second_task=True,
        )
        second_attempt = start_task(
            self.profile,
            workset_id=finish_kwargs["workset_id"],
            task_id="FIN-B",
            actor="owner",
            prompt_receipt=create_prompt_receipt("Start B first.", source="unit-test"),
        )
        real_replace = os.replace
        runtime_path = self.profile.paths.runtime_file.resolve()

        def fail_before_runtime_replace(source, destination):
            if Path(destination).resolve() == runtime_path:
                raise OSError("injected before runtime replacement")
            return real_replace(source, destination)

        with patch("blackdog_core.state.os.replace", side_effect=fail_before_runtime_replace):
            with self.assertRaisesRegex(OSError, "before runtime replacement"):
                finish_task(self.profile, **finish_kwargs)

        decision = next(
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("type") == "task.finalization.decision"
            and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
        )
        self.assertFalse(decision["payload"]["release_workset_claim"])
        finish_task(
            self.profile,
            workset_id=finish_kwargs["workset_id"],
            task_id="FIN-B",
            attempt_id=second_attempt.attempt_id,
            actor="owner",
            status="success",
            summary="finished B",
        )
        self._inject_decision_owned_workset_release(attempt.attempt_id)
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()

        with self.assertRaisesRegex(BacklogError, "release_workset_claim=false"):
            finish_task(self.profile, **finish_kwargs)
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)
        runtime = next(
            row
            for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == finish_kwargs["workset_id"]
        )
        self.assertEqual(
            next(row for row in runtime.attempts if row.attempt_id == attempt.attempt_id).status,
            "in_progress",
        )

    def test_release_false_forbidden_row_blocks_terminal_retry_without_writes(self) -> None:
        attempt, finish_kwargs = self._start_durable_fixture(
            "release-false-terminal-conflict",
            include_second_task=True,
        )
        start_task(
            self.profile,
            workset_id=finish_kwargs["workset_id"],
            task_id="FIN-B",
            actor="owner",
            prompt_receipt=create_prompt_receipt("Keep B active.", source="unit-test"),
        )
        real_replace = os.replace
        runtime_path = self.profile.paths.runtime_file.resolve()
        injected = False

        def fail_after_runtime_replace(source, destination):
            nonlocal injected
            real_replace(source, destination)
            if Path(destination).resolve() == runtime_path and not injected:
                injected = True
                raise OSError("injected after runtime replacement")

        with patch("blackdog_core.state.os.replace", side_effect=fail_after_runtime_replace):
            with self.assertRaisesRegex(OSError, "after runtime replacement"):
                finish_task(self.profile, **finish_kwargs)

        decision = next(
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("type") == "task.finalization.decision"
            and event.get("payload", {}).get("attempt_id") == attempt.attempt_id
        )
        self.assertFalse(decision["payload"]["release_workset_claim"])
        self._inject_decision_owned_workset_release(attempt.attempt_id)
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()

        with self.assertRaisesRegex(BacklogError, "release_workset_claim=false"):
            finish_task(self.profile, **finish_kwargs)
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)
        runtime = next(
            row
            for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == finish_kwargs["workset_id"]
        )
        self.assertEqual(
            next(row for row in runtime.attempts if row.attempt_id == attempt.attempt_id).status,
            "success",
        )

    def test_delegating_runtime_store_is_reentrant_and_concurrency_safe(self) -> None:
        workset_id = "delegating-runtime-store"
        upsert_workset(
            self.profile,
            {
                "id": workset_id,
                "title": "Delegating runtime store",
                "tasks": [
                    {"id": "DELEGATE-A", "title": "A", "intent": "finish A"},
                    {"id": "DELEGATE-B", "title": "B", "intent": "finish B"},
                ],
            },
        )
        attempts = [
            start_task(
                self.profile,
                workset_id=workset_id,
                task_id=task_id,
                actor="owner",
                prompt_receipt=create_prompt_receipt(f"Start {task_id}.", source="unit-test"),
            )
            for task_id in ("DELEGATE-A", "DELEGATE-B")
        ]
        ctx = multiprocessing.get_context("spawn")
        start_event = ctx.Event()
        result_queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_delegating_store_finish_worker,
                args=(
                    str(self.root),
                    workset_id,
                    attempt.task_id,
                    attempt.attempt_id,
                    start_event,
                    result_queue,
                ),
            )
            for attempt in attempts
        ]
        self._run_workers(processes, start_event, result_queue)

        runtime = next(
            row
            for row in load_runtime_state(self.profile.paths).worksets
            if row.workset_id == workset_id
        )
        self.assertEqual(
            {attempt.attempt_id: attempt.status for attempt in runtime.attempts},
            {attempt.attempt_id: "success" for attempt in attempts},
        )
        self.assertEqual(
            {state.task_id: state.status for state in runtime.task_states},
            {"DELEGATE-A": "done", "DELEGATE-B": "done"},
        )
        self.assertEqual(runtime.task_claims, ())
        self.assertIsNone(runtime.workset_claim)

    def test_file_lock_nested_aliases_are_reentrant_for_flock_and_lockdir(self) -> None:
        target_dir = self.root / "lock-target"
        target_dir.mkdir()
        real_path = target_dir / "runtime.json"
        real_path.write_text("{}\n", encoding="utf-8")
        directory_alias = self.root / "lock-directory-alias"
        directory_alias.symlink_to(target_dir, target_is_directory=True)
        file_alias = self.root / "lock-file-alias.json"
        file_alias.symlink_to(real_path)

        ctx = multiprocessing.get_context("spawn")
        for alias_path in (directory_alias / "runtime.json", file_alias):
            for use_lockdir in (False, True):
                with self.subTest(alias=alias_path.name, use_lockdir=use_lockdir):
                    result_queue = ctx.Queue()
                    process = ctx.Process(
                        target=_nested_alias_lock_worker,
                        args=(str(real_path), str(alias_path), use_lockdir, result_queue),
                    )
                    process.start()
                    process.join(timeout=5)
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
                        self.fail("nested alias lock acquisition deadlocked")
                    self.assertEqual(process.exitcode, 0)
                    result = result_queue.get(timeout=5)
                    self.assertEqual(result[0], "ok", result[2])

    def test_file_lock_aliases_serialize_other_threads(self) -> None:
        def run_probe(*, use_lockdir: bool) -> None:
            suffix = "lockdir" if use_lockdir else "flock"
            real_path = self.root / f"thread-lock-runtime-{suffix}.json"
            real_path.write_text("{}\n", encoding="utf-8")
            alias_path = self.root / f"thread-lock-alias-{suffix}.json"
            alias_path.symlink_to(real_path)
            holder_entered = threading.Event()
            release_holder = threading.Event()
            contender_attempting = threading.Event()
            contender_acquired = threading.Event()
            errors: list[str] = []

            def holder() -> None:
                try:
                    with exclusive_file_lock(real_path):
                        holder_entered.set()
                        if not release_holder.wait(timeout=5):
                            raise TimeoutError("thread holder release was not signaled")
                except Exception:
                    errors.append(traceback.format_exc())

            def contender() -> None:
                try:
                    contender_attempting.set()
                    with exclusive_file_lock(alias_path):
                        contender_acquired.set()
                except Exception:
                    errors.append(traceback.format_exc())

            holder_thread = threading.Thread(target=holder)
            contender_thread = threading.Thread(target=contender)
            holder_thread.start()
            self.assertTrue(holder_entered.wait(timeout=5))
            contender_thread.start()
            self.assertTrue(contender_attempting.wait(timeout=5))
            acquired_while_held = contender_acquired.wait(timeout=0.25)
            release_holder.set()
            holder_thread.join(timeout=5)
            contender_thread.join(timeout=5)
            self.assertFalse(holder_thread.is_alive())
            self.assertFalse(contender_thread.is_alive())
            self.assertFalse(acquired_while_held)
            self.assertTrue(contender_acquired.is_set())
            self.assertEqual(errors, [])

        run_probe(use_lockdir=False)
        with patch("blackdog_core.state.fcntl", None):
            run_probe(use_lockdir=True)

    def test_file_lock_aliases_serialize_processes_on_lockdir_fallback(self) -> None:
        target_dir = self.root / "process-lock-target"
        target_dir.mkdir()
        real_path = target_dir / "runtime.json"
        real_path.write_text("{}\n", encoding="utf-8")
        directory_alias = self.root / "process-lock-alias"
        directory_alias.symlink_to(target_dir, target_is_directory=True)
        alias_path = directory_alias / "runtime.json"

        ctx = multiprocessing.get_context("spawn")
        holder_entered = ctx.Event()
        release_holder = ctx.Event()
        contender_attempting = ctx.Event()
        contender_acquired = ctx.Event()
        result_queue = ctx.Queue()
        holder = ctx.Process(
            target=_hold_alias_lock_worker,
            args=(str(real_path), True, holder_entered, release_holder, result_queue),
        )
        contender = ctx.Process(
            target=_probe_alias_lock_worker,
            args=(str(alias_path), True, contender_attempting, contender_acquired, result_queue),
        )
        holder.start()
        self.assertTrue(holder_entered.wait(timeout=5))
        contender.start()
        self.assertTrue(contender_attempting.wait(timeout=5))
        acquired_while_held = contender_acquired.wait(timeout=0.25)
        release_holder.set()
        holder.join(timeout=5)
        contender.join(timeout=5)
        for process in (holder, contender):
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
                self.fail(f"lockdir serialization worker did not exit: pid={process.pid}")
            self.assertEqual(process.exitcode, 0)
        results = [result_queue.get(timeout=5) for _process in (holder, contender)]
        self.assertEqual([result for result in results if result[0] != "ok"], [])
        self.assertFalse(acquired_while_held)
        self.assertTrue(contender_acquired.is_set())
