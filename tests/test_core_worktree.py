from __future__ import annotations

import subprocess

from tests import test_blackdog_cli as cli_tests
from tests.core_audit_support import CoreAuditTestCase


class CoreWorktreeAuditTests(CoreAuditTestCase):
    def test_core_audit_worktree_branch_mapping_round_trips_task_ids(self) -> None:
        cli_tests.run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        cli_tests.run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Core branch mapping",
            "--bucket",
            "core",
            "--why",
            "Core worktree semantics need a direct branch-to-task invariant check.",
            "--evidence",
            "The broad supervisor tests exercise branch names indirectly.",
            "--safe-first-slice",
            "Round-trip the task id through the default branch naming helper.",
            "--path",
            "tests/test_core_worktree.py",
            "--wave",
            "0",
        )
        profile = cli_tests.load_profile(self.root)
        snapshot = cli_tests.load_backlog(profile.paths, profile)
        task_id = next(iter(snapshot.tasks))
        task = snapshot.tasks[task_id]
        branch = cli_tests.default_task_branch(task)
        self.assertEqual(cli_tests.task_id_for_branch(profile, branch), task_id)
        self.assertEqual(cli_tests.task_id_for_branch(profile, f"{branch}-run-1234"), task_id)
        self.assertIsNone(cli_tests.task_id_for_branch(profile, "agent/unrelated-task"))

    def test_core_audit_worktree_contract_reports_branch_task_invariants(self) -> None:
        cli_tests.run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        cli_tests.run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "WTAM invariant task",
            "--bucket",
            "core",
            "--why",
            "Worktree contract should report the task implied by the current branch.",
            "--evidence",
            "Preflight only exposed the branch name, not whether it matched a known task.",
            "--safe-first-slice",
            "Surface the derived task id in the worktree contract.",
            "--path",
            "src/blackdog/worktree.py",
            "--wave",
            "0",
        )
        profile = cli_tests.load_profile(self.root)
        snapshot = cli_tests.load_backlog(profile.paths, profile)
        task = next(iter(snapshot.tasks.values()))
        branch = cli_tests.default_task_branch(task)
        subprocess.run(["git", "-C", str(self.root), "checkout", "-b", branch], check=True, capture_output=True, text=True)
        contract = cli_tests.worktree_contract(profile, workspace=self.root)
        self.assertEqual(contract["current_task_id"], task.id)
        self.assertTrue(contract["current_branch_is_task_branch"])
        self.assertEqual(contract["current_branch"], branch)
        self.assertEqual(contract["workspace_role"], "primary")
        self.assertEqual(contract["landing_state"], "blocked" if contract["primary_dirty"] else "ready")
