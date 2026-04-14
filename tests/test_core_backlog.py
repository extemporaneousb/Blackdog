from __future__ import annotations

from pathlib import Path

from blackdog_core.backlog import (
    JsonPlanningStore,
    PlanningState,
    TaskSpec,
    Workset,
    claim_workset_manager,
    default_planning_state,
    finish_task,
    load_planning_state,
    next_ready_tasks,
    release_workset_manager,
    save_planning_state,
    start_task,
    upsert_workset,
)
from blackdog_core.state import JsonRuntimeStore, create_prompt_receipt, load_runtime_state
from tests.core_audit_support import CoreAuditTestCase


class _MemoryPlanningStore:
    def __init__(self) -> None:
        self.state = default_planning_state()

    def load(self, path: Path) -> PlanningState:
        return self.state

    def save(self, path: Path, state: PlanningState) -> None:
        self.state = state


class CorePlanningTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile("Demo")
        self.profile = self.load_test_profile()

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

        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertEqual(runtime_state.worksets[0].workset_claim.actor, "codex")
        self.assertEqual(runtime_state.worksets[0].workset_claim.execution_model, "direct_wtam")
        self.assertEqual(runtime_state.worksets[0].task_claims[0].task_id, "DIR-1")
        self.assertEqual(runtime_state.worksets[0].task_claims[0].attempt_id, attempt.attempt_id)

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

    def test_abandoned_attempt_releases_claims_and_returns_task_to_planned(self) -> None:
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
        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertEqual(runtime_state.worksets[0].task_states[0].status, "planned")
        self.assertIsNone(runtime_state.worksets[0].workset_claim)
        self.assertEqual(runtime_state.worksets[0].task_claims, ())

    def test_workset_manager_claim_can_host_serial_worker_attempts_until_release(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "managed",
                "title": "Managed",
                "tasks": [
                    {"id": "MG-1", "title": "First slice", "intent": "land the first serial task"},
                    {
                        "id": "MG-2",
                        "title": "Second slice",
                        "intent": "land the second serial task",
                        "depends_on": ["MG-1"],
                    },
                ],
            },
        )

        claim = claim_workset_manager(self.profile, workset_id="managed", actor="supervisor", note="serial run")
        self.assertEqual(claim.execution_model, "workset_manager")

        attempt_one = start_task(
            self.profile,
            workset_id="managed",
            task_id="MG-1",
            actor="worker-a",
            prompt_receipt=create_prompt_receipt("Execute the first managed slice.", source="unit-test"),
        )
        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertEqual(runtime_state.worksets[0].workset_claim.actor, "supervisor")
        self.assertEqual(runtime_state.worksets[0].workset_claim.execution_model, "workset_manager")
        self.assertEqual(runtime_state.worksets[0].task_claims[0].actor, "worker-a")
        self.assertEqual(runtime_state.worksets[0].task_claims[0].execution_model, "direct_wtam")

        finish_task(
            self.profile,
            workset_id="managed",
            task_id="MG-1",
            attempt_id=attempt_one.attempt_id,
            actor="worker-a",
            status="success",
            summary="first slice landed",
        )
        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertEqual(runtime_state.worksets[0].task_states[0].status, "done")
        self.assertEqual(runtime_state.worksets[0].workset_claim.actor, "supervisor")
        self.assertEqual(runtime_state.worksets[0].workset_claim.execution_model, "workset_manager")
        self.assertEqual(runtime_state.worksets[0].task_claims, ())

        attempt_two = start_task(
            self.profile,
            workset_id="managed",
            task_id="MG-2",
            actor="worker-b",
            prompt_receipt=create_prompt_receipt("Execute the second managed slice.", source="unit-test"),
        )
        self.assertEqual(attempt_two.execution_model, "direct_wtam")

        finish_task(
            self.profile,
            workset_id="managed",
            task_id="MG-2",
            attempt_id=attempt_two.attempt_id,
            actor="worker-b",
            status="success",
            summary="second slice landed",
        )
        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertEqual(runtime_state.worksets[0].task_states[1].status, "done")
        self.assertEqual(runtime_state.worksets[0].workset_claim.actor, "supervisor")
        self.assertEqual(runtime_state.worksets[0].task_claims, ())

        release_workset_manager(
            self.profile,
            workset_id="managed",
            actor="supervisor",
            summary="serial run complete",
        )
        runtime_state = load_runtime_state(self.profile.paths, store=JsonRuntimeStore())
        self.assertIsNone(runtime_state.worksets[0].workset_claim)
