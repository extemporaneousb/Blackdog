from __future__ import annotations

from blackdog_core.backlog import finish_task, start_task, upsert_workset
from blackdog_core.runtime_model import load_runtime_model, scope_runtime_model
from blackdog_core.snapshot import build_runtime_snapshot
from blackdog_core.state import create_prompt_receipt
from tests.core_audit_support import CoreAuditTestCase


class RuntimeModelTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile("Blackdog")
        self.profile = self.load_test_profile()

    def test_runtime_model_projects_worksets_tasks_and_branch_identity(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "kernel",
                "title": "Kernel rewrite",
                "scope": {"kind": "repo", "paths": ["src/blackdog_core", "docs"]},
                "visibility": {"kind": "workset"},
                "policies": {"validation": ["make test"]},
                "workspace": {"identity": "kernel-workspace", "exported_root": "src/blackdog_core"},
                "branch_intent": {"target_branch": "main", "integration_branch": "main"},
                "tasks": [
                    {
                        "id": "KERN-1",
                        "title": "Replace planning store",
                        "intent": "move semantic truth into planning.json",
                    },
                    {
                        "id": "KERN-2",
                        "title": "Rebuild snapshot",
                        "intent": "project the new workset structure",
                        "depends_on": ["KERN-1"],
                    },
                ],
                "task_states": [{"task_id": "KERN-1", "status": "done"}],
            },
        )

        model = load_runtime_model(self.profile)

        self.assertEqual(model.repository.project_name, "Blackdog")
        self.assertEqual(model.counts["worksets"], 1)
        self.assertEqual(model.counts["claimed_worksets"], 0)
        self.assertEqual(model.counts["tasks"], 2)
        self.assertEqual(model.counts["attempts"], 0)
        self.assertEqual(model.counts["ready"], 1)
        self.assertEqual(model.worksets[0].workspace["identity"], "kernel-workspace")
        self.assertEqual(model.worksets[0].branch_intent["target_branch"], "main")
        self.assertEqual(model.worksets[0].tasks[0].readiness, "done")
        self.assertEqual(model.worksets[0].tasks[1].readiness, "ready")
        self.assertEqual(model.worksets[0].tasks[1].workset_id, "kernel")
        self.assertEqual(model.next_tasks[0].task_id, "KERN-2")
        self.assertEqual(model.next_tasks[0].workset_id, "kernel")

    def test_runtime_snapshot_embeds_the_typed_runtime_model(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "snapshot",
                "title": "Snapshot",
                "workspace": {"identity": "snapshot-workspace"},
                "branch_intent": {"target_branch": "main", "integration_branch": "main"},
                "tasks": [{"id": "SNAP-1", "title": "Emit runtime snapshot", "intent": "print JSON"}],
            },
        )

        snapshot = build_runtime_snapshot(self.profile)

        self.assertEqual(snapshot["format"], "blackdog.snapshot/vnext1")
        self.assertEqual(snapshot["runtime_model"]["repository"]["project_name"], "Blackdog")
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["workset_id"], "snapshot")
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["next_task_ids"], ["SNAP-1"])

    def test_runtime_model_surfaces_recent_attempts_and_latest_task_result(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "direct",
                "title": "Direct",
                "workspace": {"identity": "direct-workspace"},
                "branch_intent": {"target_branch": "main", "integration_branch": "feature/direct"},
                "tasks": [{"id": "DIR-1", "title": "Record result stats", "intent": "finish one task"}],
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
            start_commit="feedface1234",
            model="gpt-5.4",
            prompt_receipt=create_prompt_receipt(
                "Record result stats for the direct execution slice.",
                recorded_at="2026-04-12T09:00:00-07:00",
                source="unit-test",
            ),
        )
        finish_task(
            self.profile,
            workset_id="direct",
            task_id="DIR-1",
            attempt_id=attempt.attempt_id,
            actor="codex",
            status="success",
            summary="captured the result",
            changed_paths=("src/blackdog_core/state.py",),
            elapsed_seconds=15,
        )

        model = load_runtime_model(self.profile)

        self.assertEqual(model.counts["attempts"], 1)
        self.assertEqual(model.counts["active_attempts"], 0)
        self.assertEqual(model.counts["claimed_worksets"], 0)
        self.assertEqual(model.counts["claimed_tasks"], 0)
        self.assertEqual(model.recent_attempts[0].task_id, "DIR-1")
        self.assertEqual(model.recent_attempts[0].status, "success")
        self.assertEqual(model.recent_attempts[0].worktree_role, "linked")
        self.assertEqual(model.recent_attempts[0].start_commit, "feedface1234")
        self.assertEqual(model.recent_attempts[0].execution_model, "direct_wtam")
        self.assertEqual(model.recent_attempts[0].prompt_receipt.source, "unit-test")
        self.assertEqual(model.worksets[0].tasks[0].latest_attempt_status, "success")
        self.assertIsNone(model.worksets[0].claim)
        self.assertEqual(model.worksets[0].task_claims, ())
        self.assertEqual(model.worksets[0].attempts[0].elapsed_seconds, 15)

    def test_runtime_model_can_scope_to_one_workset(self) -> None:
        upsert_workset(
            self.profile,
            {
                "id": "alpha",
                "title": "Alpha",
                "tasks": [{"id": "A-1", "title": "Alpha task", "intent": "stay scoped"}],
            },
        )
        upsert_workset(
            self.profile,
            {
                "id": "beta",
                "title": "Beta",
                "tasks": [{"id": "B-1", "title": "Beta task", "intent": "drop out"}],
            },
        )

        scoped = scope_runtime_model(load_runtime_model(self.profile), workset_id="alpha")

        self.assertEqual(scoped.counts["worksets"], 1)
        self.assertEqual(scoped.worksets[0].workset_id, "alpha")
        self.assertEqual(scoped.next_tasks[0].workset_id, "alpha")
        self.assertEqual(len(scoped.events), 1)
