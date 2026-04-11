from __future__ import annotations

import tomllib

from tests import test_blackdog_cli as cli_tests
from tests.core_audit_support import CoreAuditTestCase


class CoreContractAuditTests(CoreAuditTestCase):
    def test_pyproject_freezes_one_public_cli_and_three_python_packages(self) -> None:
        pyproject = tomllib.loads((cli_tests.ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["scripts"], {"blackdog": "blackdog_cli.main:main"})
        include = pyproject["tool"]["setuptools"]["packages"]["find"]["include"]
        self.assertIn("blackdog_core", include)
        self.assertIn("blackdog", include)
        self.assertIn("blackdog_cli", include)

    def test_core_coverage_surface_points_at_blackdog_core_modules(self) -> None:
        pyproject = tomllib.loads((cli_tests.ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        coverage_settings = pyproject["tool"]["blackdog"]["coverage"]
        self.assertEqual(
            coverage_settings["shipped_surface"],
            [
                "src/blackdog_core/backlog.py",
                "src/blackdog_core/profile.py",
                "src/blackdog_core/runtime_model.py",
                "src/blackdog_core/snapshot.py",
                "src/blackdog_core/state.py",
            ],
        )

    def test_makefile_uses_blackdog_cli_and_extensions_emacs(self) -> None:
        makefile = (cli_tests.ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("extensions/emacs/lisp", makefile)
        self.assertIn("extensions/emacs/test", makefile)
        self.assertIn("python3 -m blackdog_cli", makefile)
        self.assertIn("CORE_COVERAGE_OUTPUT = coverage/core-latest.json", makefile)

    def test_docs_freeze_new_package_vocabulary(self) -> None:
        readme = (cli_tests.ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("`blackdog_core`", readme)
        self.assertIn("`blackdog`", readme)
        self.assertIn("`blackdog_cli`", readme)
        self.assertIn("`blackdog-core`", readme)
        self.assertIn("`blackdog-cli`", readme)
        self.assertIn("`extensions/emacs/`", readme)
        self.assertIn("`runtime_snapshot`", readme)

        architecture = (cli_tests.ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        self.assertIn("`blackdog_core`", architecture)
        self.assertIn("`blackdog`", architecture)
        self.assertIn("`blackdog_cli`", architecture)
        self.assertIn("`extensions/emacs/`", architecture)

        cli_doc = (cli_tests.ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
        self.assertIn("`blackdog` executable", cli_doc)
        self.assertIn("`blackdog_cli` package", cli_doc)
        self.assertIn("`blackdog-core`", cli_doc)
        self.assertIn("`runtime_snapshot`", cli_doc)
        self.assertIn("`blackdog architecture-docs`", cli_doc)

        file_formats = (cli_tests.ROOT / "docs" / "FILE_FORMATS.md").read_text(encoding="utf-8")
        self.assertIn("`blackdog_core` contract", file_formats)
        self.assertIn("`runtime_snapshot`", file_formats)
        self.assertIn("state machine", file_formats)
        self.assertIn("`blackdog_core.snapshot.load_runtime_artifacts()`", file_formats)

    def test_closeout_docs_still_point_to_migration_release_and_acceptance(self) -> None:
        readme = (cli_tests.ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("[docs/MIGRATION.md](docs/MIGRATION.md)", readme)
        self.assertIn("[docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md)", readme)
        self.assertIn("[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)", readme)

        docs_index = (cli_tests.ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("[docs/MIGRATION.md](docs/MIGRATION.md)", docs_index)
        self.assertIn("[docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md)", docs_index)
        self.assertIn("[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)", docs_index)
        self.assertIn("[docs/architecture-diagrams.html](docs/architecture-diagrams.html)", docs_index)

        emacs_readme = (cli_tests.ROOT / "extensions" / "emacs" / "README.md").read_text(encoding="utf-8")
        self.assertIn("[docs/MIGRATION.md](../../docs/MIGRATION.md)", emacs_readme)
        self.assertIn("[docs/RELEASE_NOTES.md](../../docs/RELEASE_NOTES.md)", emacs_readme)

    def test_target_model_doc_is_linked_and_routed(self) -> None:
        profile = tomllib.loads((cli_tests.ROOT / "blackdog.toml").read_text(encoding="utf-8"))
        self.assertIn("docs/TARGET_MODEL.md", profile["taxonomy"]["doc_routing_defaults"])

        docs_index = (cli_tests.ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("[docs/TARGET_MODEL.md](docs/TARGET_MODEL.md)", docs_index)
        self.assertIn("[docs/TARGET_MODEL_EXECUTION_PLAN.md](docs/TARGET_MODEL_EXECUTION_PLAN.md)", docs_index)

        architecture = (cli_tests.ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        self.assertIn("[docs/TARGET_MODEL.md](docs/TARGET_MODEL.md)", architecture)

        target_model = (cli_tests.ROOT / "docs" / "TARGET_MODEL.md").read_text(encoding="utf-8")
        self.assertIn("`TaskAttempt`", target_model)
        self.assertIn("`PromptReceipt`", target_model)
        self.assertIn("runtime kernel", target_model)
        self.assertIn("## Scope And Boundaries", target_model)
        self.assertIn("## Terms of Art", target_model)
        self.assertIn("`WorksetExecution`", target_model)
        self.assertIn("## What We Should Learn From Adjacent Systems", target_model)

        skill = (cli_tests.ROOT / ".codex" / "skills" / "blackdog" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("`docs/TARGET_MODEL.md`", skill)

    def test_docs_and_generated_skill_present_workset_runtime_vocabulary(self) -> None:
        charter = (cli_tests.ROOT / "docs" / "CHARTER.md").read_text(encoding="utf-8")
        self.assertIn("worksets", charter)
        self.assertIn("multi-agent workset execution", charter)
        self.assertIn("legacy `epic` / `lane` / `wave`", charter)

        integration = (cli_tests.ROOT / "docs" / "INTEGRATION.md").read_text(encoding="utf-8")
        self.assertIn("workset-shaped deliverable", integration)
        self.assertIn("compatibility alias", integration)

        file_formats = (cli_tests.ROOT / "docs" / "FILE_FORMATS.md").read_text(encoding="utf-8")
        self.assertIn("`workset execution`", file_formats)
        self.assertIn("preferred planning model", file_formats)
        self.assertIn("legacy `plan.lanes` compatibility projection", file_formats)

        skill = (cli_tests.ROOT / ".codex" / "skills" / "blackdog" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("workset-scoped deliverable", skill)
        self.assertIn("Current artifacts still use `run_id` as a compatibility alias", skill)

    def test_core_import_boundaries_stay_within_blackdog_core(self) -> None:
        violations = self.core_import_boundary_violations()
        self.assertEqual(
            violations,
            [],
            "Core modules must not import Blackdog product or extension surfaces:\n" + "\n".join(violations),
        )

    def test_state_machine_vocab_is_frozen_in_code(self) -> None:
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

    def test_supervisor_state_normalizers_handle_legacy_aliases(self) -> None:
        self.assertEqual(cli_tests._normalize_supervisor_runtime_status("swept"), "running")
        self.assertEqual(cli_tests._normalize_supervisor_runtime_status("complete"), "idle")
        self.assertEqual(cli_tests._normalize_supervisor_runtime_status("finished"), "idle")
        self.assertEqual(cli_tests._normalize_supervisor_attempt_status("finished"), "done")
        self.assertEqual(cli_tests._normalize_supervisor_attempt_status("partial"), "partial")
