from __future__ import annotations

import tomllib

from tests import test_blackdog_cli as cli_tests
from tests.core_audit_support import CoreAuditTestCase

class CoreContractAuditTests(CoreAuditTestCase):
    def test_core_audit_pyproject_shipped_surface_tracks_core_modules(self) -> None:
        pyproject = tomllib.loads((cli_tests.ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        coverage_settings = pyproject["tool"]["blackdog"]["coverage"]
        self.assertEqual(
            coverage_settings["shipped_surface"],
            [
                "src/blackdog/backlog.py",
                "src/blackdog/config.py",
                "src/blackdog/store.py",
                "src/blackdog/worktree.py",
            ],
        )

    def test_core_audit_makefile_freezes_core_audit_command(self) -> None:
        makefile = (cli_tests.ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn(
            "CORE_AUDIT_COMMAND = PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_core_*.py'",
            makefile,
        )
        self.assertIn("CORE_COVERAGE_OUTPUT = coverage/core-latest.json", makefile)
        self.assertIn("\ntest-core:\n\t$(CORE_AUDIT_COMMAND)\n", makefile)
        self.assertIn(
            '\ncoverage-core:\n\tPYTHONPATH=src python3 -m blackdog.cli coverage --project-root . --command "$(CORE_AUDIT_COMMAND)" --output $(CORE_COVERAGE_OUTPUT)\n',
            makefile,
        )

    def test_core_audit_file_formats_freezes_state_machines_and_gate_plan(self) -> None:
        file_formats = (cli_tests.ROOT / "docs" / "FILE_FORMATS.md").read_text(encoding="utf-8")
        self.assertIn("## Core durable state tables, state machines, and invariants", file_formats)
        self.assertIn("### `approval_tasks` semantic state machine", file_formats)
        self.assertIn("### `task_claims` semantic state machine", file_formats)
        self.assertIn("### `inbox.jsonl` replay state machine", file_formats)
        self.assertIn("### `blackdog worktree` lifecycle and landing state machine", file_formats)
        self.assertIn("### `supervisor-runs/*/status.json` run state machine", file_formats)
        self.assertIn("## Core coverage gate plan", file_formats)
        self.assertIn("make test-core", file_formats)
        self.assertIn("make coverage-core", file_formats)
        self.assertIn("test_core_*.py", file_formats)
        self.assertIn("do not enforce a numeric coverage threshold yet", file_formats)
        self.assertIn(
            "require 100.0 percent aggregate coverage across the shipped\n  surface and 100.0 percent coverage for each shipped module",
            file_formats,
        )

    def test_core_audit_import_boundaries_stay_within_blackdog_core(self) -> None:
        violations = self.core_import_boundary_violations()
        self.assertEqual(
            violations,
            [],
            "Core modules must not import Blackdog proper or extension surfaces:\n" + "\n".join(violations),
        )

    def test_core_audit_state_machine_vocab_is_frozen_in_code(self) -> None:
        self.assertEqual(
            cli_tests.APPROVAL_STATE_MACHINE_STATES,
            frozenset({"absent", "pending", "approved", "denied", "deferred", "done"}),
        )
        self.assertEqual(
            cli_tests.store_module.APPROVAL_STATUSES,
            frozenset({"pending", "approved", "denied", "deferred", "done"}),
        )
        self.assertEqual(
            cli_tests.store_module.APPROVAL_SATISFIED_STATUSES,
            frozenset({"approved", "done"}),
        )
        self.assertEqual(cli_tests.CLAIM_STATE_MACHINE_STATES, frozenset({"absent", "claimed", "released", "done"}))
        self.assertEqual(cli_tests.store_module.CLAIM_STATUSES, frozenset({"claimed", "released", "done"}))
        self.assertEqual(cli_tests.INBOX_ACTIONS, frozenset({"message", "resolve"}))
        self.assertEqual(cli_tests.INBOX_STATE_MACHINE_STATES, frozenset({"open", "resolved"}))
        self.assertEqual(cli_tests.worktree_module.WORKSPACE_MODE_GIT_WORKTREE, "git-worktree")
        self.assertEqual(cli_tests.worktree_module.WORKTREE_MODEL_BRANCH_BACKED, "branch-backed")
        self.assertEqual(cli_tests.WORKTREE_ROLES, frozenset({"primary", "task", "linked"}))
        self.assertEqual(cli_tests.WORKTREE_LANDING_STATES, frozenset({"ready", "blocked"}))
        self.assertEqual(
            cli_tests.WORKTREE_LIFECYCLE_STATES,
            frozenset({"prepared", "dirty", "ahead", "blocked", "landed", "cleaned"}),
        )
        self.assertEqual(
            cli_tests.SUPERVISOR_RUN_STEP_STATUSES,
            frozenset({"swept", "running", "draining", "stopped", "idle"}),
        )
        self.assertEqual(
            cli_tests.supervisor_module.SUPERVISOR_RUN_FINAL_STATUSES,
            frozenset({"idle", "stopped", "interrupted"}),
        )
        self.assertEqual(
            cli_tests.SUPERVISOR_RUN_RUNTIME_STATUSES,
            frozenset({"running", "draining", "idle", "stopped", "interrupted", "historical"}),
        )
        self.assertEqual(
            cli_tests.SUPERVISOR_ATTEMPT_STATUSES,
            frozenset({"prepared", "running", "launch-failed", "interrupted", "blocked", "failed", "released", "done", "partial", "unknown"}),
        )
        self.assertEqual(
            cli_tests.supervisor_module.SUPERVISOR_RECOVERY_CASES,
            frozenset({"blocked_by_dirty_primary", "blocked_land", "partial_run", "landed_but_unfinished"}),
        )

    def test_core_audit_supervisor_state_normalizers_handle_legacy_aliases(self) -> None:
        self.assertEqual(cli_tests._normalize_supervisor_runtime_status("swept"), "running")
        self.assertEqual(cli_tests._normalize_supervisor_runtime_status("complete"), "idle")
        self.assertEqual(cli_tests._normalize_supervisor_runtime_status("finished"), "idle")
        self.assertEqual(cli_tests._normalize_supervisor_attempt_status("finished"), "done")
        self.assertEqual(cli_tests._normalize_supervisor_attempt_status("partial"), "partial")
