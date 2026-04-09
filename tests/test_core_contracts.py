from __future__ import annotations

import io
import tomllib
from contextlib import redirect_stderr

from tests import test_blackdog_cli as cli_tests
from tests.core_audit_support import CoreAuditTestCase


class CoreContractAuditTests(CoreAuditTestCase):
    def test_core_audit_pyproject_scripts_freeze_public_executables(self) -> None:
        pyproject = tomllib.loads((cli_tests.ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(
            pyproject["project"]["scripts"],
            {
                "blackdog": "blackdog.cli:main",
                "blackdog-core": "blackdog.cli:main_core",
                "blackdog-proper": "blackdog.cli:main_proper",
                "blackdog-devtool": "blackdog.cli:main_devtool",
                "blackdog-skill": "blackdog.skill_cli:main",
            },
        )

    def test_core_audit_pyproject_shipped_surface_tracks_core_modules(self) -> None:
        pyproject = tomllib.loads((cli_tests.ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        coverage_settings = pyproject["tool"]["blackdog"]["coverage"]
        self.assertEqual(
            coverage_settings["shipped_surface"],
            [
                "src/blackdog/core/backlog.py",
                "src/blackdog/core/config.py",
                "src/blackdog/core/store.py",
            ],
        )

    def test_core_audit_makefile_freezes_core_audit_command(self) -> None:
        makefile = (cli_tests.ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("\nacceptance:\n\t$(MAKE) test\n\t$(MAKE) test-emacs\n", makefile)
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
        self.assertIn("pass/fail signal is command success plus the frozen artifact write", file_formats)
        self.assertIn("explicitly turns on the Phase 1\nnumeric gate", file_formats)
        self.assertIn(
            "require 100.0 percent aggregate coverage across the shipped\n  surface and 100.0 percent coverage for each shipped module",
            file_formats,
        )

    def test_core_audit_cli_docs_keep_phase_zero_coverage_gate_non_numeric(self) -> None:
        cli_doc = (cli_tests.ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
        self.assertIn("`make coverage-core` must complete successfully and write its artifact", cli_doc)
        self.assertIn("does not fail the command just because the shipped-surface percentage", cli_doc)

    def test_core_audit_owner_scoped_parsers_gate_public_commands(self) -> None:
        core_parser = cli_tests.cli_module.build_parser(
            description="Blackdog core CLI",
            allowed_owners=frozenset({"core"}),
        )
        core_args = core_parser.parse_args(["summary"])
        self.assertEqual(core_args.command_owner, "core")
        preflight_args = core_parser.parse_args(["worktree", "preflight"])
        self.assertEqual(preflight_args.command_owner, "core")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                core_parser.parse_args(["render"])
        self.assertEqual(error.exception.code, 2)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                core_parser.parse_args(["worktree", "start", "--id", "BLACK-demo"])
        self.assertEqual(error.exception.code, 2)

        proper_parser = cli_tests.cli_module.build_parser(
            description="Blackdog proper CLI",
            allowed_owners=frozenset({"blackdog-proper"}),
        )
        proper_args = proper_parser.parse_args(["render"])
        self.assertEqual(proper_args.command_owner, "blackdog-proper")
        start_args = proper_parser.parse_args(["worktree", "start", "--id", "BLACK-demo"])
        self.assertEqual(start_args.command_owner, "blackdog-proper")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                proper_parser.parse_args(["summary"])
        self.assertEqual(error.exception.code, 2)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                proper_parser.parse_args(["worktree", "preflight"])
        self.assertEqual(error.exception.code, 2)

        devtool_parser = cli_tests.cli_module.build_parser(
            description="Blackdog devtool CLI",
            allowed_owners=frozenset({"devtool"}),
        )
        devtool_args = devtool_parser.parse_args(["coverage"])
        self.assertEqual(devtool_args.command_owner, "devtool")
        bootstrap_args = devtool_parser.parse_args(["bootstrap", "--project-root", ".", "--project-name", "Demo"])
        self.assertEqual(bootstrap_args.command_owner, "devtool")
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                devtool_parser.parse_args(["summary"])
        self.assertEqual(error.exception.code, 2)

    def test_core_audit_docs_freeze_public_executables_and_packaging_scope(self) -> None:
        readme = (cli_tests.ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("`blackdog-core`", readme)
        self.assertIn("`blackdog-proper`", readme)
        self.assertIn("`blackdog-devtool`", readme)
        self.assertIn("`python -m blackdog`", readme)
        self.assertIn("compatibility umbrella CLI", readme)
        self.assertIn("legacy compatibility wrapper", readme)

        architecture = (cli_tests.ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        self.assertIn("`blackdog-core`", architecture)
        self.assertIn("`blackdog-proper`", architecture)
        self.assertIn("`blackdog-devtool`", architecture)
        self.assertIn("stable public surface", architecture)

        cli_doc = (cli_tests.ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
        self.assertIn("The packaged executable contract is the `[project.scripts]` table", cli_doc)
        self.assertIn("owner-filtered parser surface", cli_doc)

        file_formats = (cli_tests.ROOT / "docs" / "FILE_FORMATS.md").read_text(encoding="utf-8")
        self.assertIn("Executable and module packaging surfaces are intentionally out of scope", file_formats)
        self.assertIn("[docs/CLI.md](docs/CLI.md)", file_formats)

    def test_core_audit_closeout_docs_publish_migration_release_and_acceptance_story(self) -> None:
        readme = (cli_tests.ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("[docs/MIGRATION.md](docs/MIGRATION.md)", readme)
        self.assertIn("[docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md)", readme)
        self.assertIn("[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)", readme)
        self.assertIn("make acceptance", readme)

        docs_index = (cli_tests.ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("[docs/MIGRATION.md](docs/MIGRATION.md)", docs_index)
        self.assertIn("[docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md)", docs_index)
        self.assertIn("[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)", docs_index)

        emacs_readme = (cli_tests.ROOT / "editors" / "emacs" / "README.md").read_text(encoding="utf-8")
        self.assertIn("[docs/MIGRATION.md](../../docs/MIGRATION.md)", emacs_readme)
        self.assertIn("[docs/RELEASE_NOTES.md](../../docs/RELEASE_NOTES.md)", emacs_readme)
        self.assertIn("make acceptance", emacs_readme)

        migration = (cli_tests.ROOT / "docs" / "MIGRATION.md").read_text(encoding="utf-8")
        self.assertIn("`blackdog-core`", migration)
        self.assertIn("`blackdog-proper`", migration)
        self.assertIn("`blackdog-devtool`", migration)
        self.assertIn("`snapshot.core_export`", migration)
        self.assertIn("`blackdog-skill`", migration)
        self.assertIn("`blackdog.scaffold`", migration)
        self.assertIn("editors/emacs/lisp/blackdog-thread.el", migration)
        self.assertIn("editors/emacs/lisp/blackdog-spec.el", migration)

        release_notes = (cli_tests.ROOT / "docs" / "RELEASE_NOTES.md").read_text(encoding="utf-8")
        self.assertIn("`blackdog.scaffold`", release_notes)
        self.assertIn("`core_export`", release_notes)
        self.assertIn("`blackdog-skill`", release_notes)
        self.assertIn("editors/emacs/lisp/blackdog-thread.el", release_notes)

        acceptance = (cli_tests.ROOT / "docs" / "ACCEPTANCE.md").read_text(encoding="utf-8")
        self.assertIn("`pyproject.toml`", acceptance)
        self.assertIn("[docs/MODULE_INVENTORY.md](docs/MODULE_INVENTORY.md)", acceptance)
        self.assertIn("[tests/test_core_contracts.py](../tests/test_core_contracts.py)", acceptance)
        self.assertIn("make acceptance", acceptance)
        self.assertIn("make test", acceptance)
        self.assertIn("make test-emacs", acceptance)

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
