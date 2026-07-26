from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import blackdog_core.profile as profile_module
from tests.core_audit_support import CoreAuditTestCase


class CoreConfigTests(CoreAuditTestCase):
    def test_load_profile_defaults_to_machine_native_control_files(self) -> None:
        self.write_profile("Demo")
        profile = self.load_test_profile()

        self.assertEqual(profile.status, profile_module.PROJECT_STATUS_ACTIVE)
        self.assertEqual(profile.paths.control_dir, (self.root / ".git" / "blackdog").resolve())
        self.assertEqual(profile.paths.planning_file, profile.paths.control_dir / "planning.json")
        self.assertEqual(profile.paths.runtime_file, profile.paths.control_dir / "runtime.json")
        self.assertEqual(profile.paths.events_file, profile.paths.control_dir / "events.jsonl")
        self.assertEqual(profile.paths.worktrees_dir, (self.root.parent / f".worktrees-{self.root.name}").resolve())
        self.assertFalse(profile.landing.automatic_stale_rebase)
        self.assertEqual(profile.landing.schema_version, 1)
        self.assertEqual(profile.landing.validation_timeout_seconds, 900)
        self.assertTrue(profile.validation_commands_explicit)
        self.assertTrue(profile.handlers_explicit)
        self.assertEqual(profile.handlers[0].kind, profile_module.HANDLER_KIND_PYTHON_OVERLAY_VENV)
        self.assertEqual(profile.handlers[1].kind, profile_module.HANDLER_KIND_BLACKDOG_RUNTIME)

    def test_load_profile_read_only_does_not_prepare_control_layout(self) -> None:
        self.write_profile("Demo")
        control_dir = (self.root / ".git" / "blackdog").resolve()
        self.assertFalse(control_dir.exists())

        with patch("blackdog_core.profile._prune_stale_git_worktrees") as prune:
            profile = profile_module.load_profile(self.root, read_only=True)

        self.assertEqual(profile.paths.control_dir, control_dir)
        self.assertFalse(control_dir.exists())
        prune.assert_not_called()

        with patch("blackdog_core.profile._prune_stale_git_worktrees") as prune:
            profile_module.load_profile(self.root)

        self.assertTrue(control_dir.is_dir())
        prune.assert_called_once_with(self.root.resolve())

    def test_load_profile_accepts_explicit_runtime_paths_without_control_dir(self) -> None:
        (self.root / "blackdog.toml").write_text(
            "[project]\nname = \"Demo\"\n\n"
            "[paths]\n"
            "planning_file = \".git/coord/planning.json\"\n"
            "runtime_file = \".git/coord/runtime.json\"\n"
            "events_file = \".git/coord/events.jsonl\"\n\n"
            "[taxonomy]\n"
            "validation_commands = [\"make test\"]\n",
            encoding="utf-8",
        )

        profile = self.load_test_profile()

        self.assertEqual(profile.paths.control_dir, (self.root / ".git" / "coord").resolve())
        self.assertEqual(profile.paths.planning_file, profile.paths.control_dir / "planning.json")
        self.assertEqual(profile.paths.runtime_file, profile.paths.control_dir / "runtime.json")
        self.assertEqual(profile.paths.events_file, profile.paths.control_dir / "events.jsonl")
        self.assertEqual(profile.validation_commands, ("make test",))
        self.assertEqual(profile.status, profile_module.PROJECT_STATUS_ACTIVE)
        self.assertFalse(profile.handlers_explicit)
        self.assertEqual(profile.handlers[0].handler_id, "python")
        self.assertEqual(profile.handlers[1].handler_id, "blackdog")

    def test_load_profile_accepts_automatic_stale_rebase_policy(self) -> None:
        self.write_profile("Demo")
        profile_path = self.root / "blackdog.toml"
        profile_path.write_text(
            profile_path.read_text(encoding="utf-8").replace(
                "automatic_stale_rebase = false",
                "automatic_stale_rebase = true",
            ),
            encoding="utf-8",
        )

        profile = self.load_test_profile()

        self.assertTrue(profile.landing.automatic_stale_rebase)
        self.assertEqual(profile.landing.validation_timeout_seconds, 900)

    def test_automatic_stale_rebase_requires_explicit_validation_commands(self) -> None:
        self.write_profile("Demo")
        profile_path = self.root / "blackdog.toml"
        profile_text = profile_path.read_text(encoding="utf-8")
        profile_text = profile_text.replace(
            "validation_commands = [\"PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'\"]\n",
            "",
        ).replace(
            "automatic_stale_rebase = false",
            "automatic_stale_rebase = true",
        )
        profile_path.write_text(profile_text, encoding="utf-8")

        with self.assertRaisesRegex(
            profile_module.ConfigError,
            "requires a nonempty explicit taxonomy.validation_commands",
        ):
            self.load_test_profile()

    def test_load_profile_rejects_invalid_landing_policy(self) -> None:
        self.write_profile("Demo")
        profile_path = self.root / "blackdog.toml"
        original = profile_path.read_text(encoding="utf-8")

        invalid_rows = (
            ("schema_version = 1", "schema_version = 2", "landing.schema_version"),
            (
                "automatic_stale_rebase = false",
                'automatic_stale_rebase = "yes"',
                "landing.automatic_stale_rebase",
            ),
            (
                "validation_timeout_seconds = 900",
                "validation_timeout_seconds = 0",
                "landing.validation_timeout_seconds",
            ),
        )
        for old, new, expected in invalid_rows:
            with self.subTest(expected=expected):
                profile_path.write_text(original.replace(old, new), encoding="utf-8")
                with self.assertRaisesRegex(profile_module.ConfigError, expected):
                    self.load_test_profile()

        malformed = original.replace(
            "[landing]\n"
            "schema_version = 1\n"
            "automatic_stale_rebase = false\n"
            "validation_timeout_seconds = 900\n\n",
            "",
        ).replace("[project]\n", "landing = false\n\n[project]\n", 1)
        profile_path.write_text(malformed, encoding="utf-8")
        with self.assertRaisesRegex(profile_module.ConfigError, "landing must be a table"):
            self.load_test_profile()

    def test_load_profile_accepts_project_statuses(self) -> None:
        self.write_profile("Demo")
        profile_path = self.root / "blackdog.toml"
        original = profile_path.read_text(encoding="utf-8")

        profile_path.write_text(
            original.replace('[project]\n', '[project]\nstatus = "active"\n'),
            encoding="utf-8",
        )
        self.assertEqual(self.load_test_profile().status, profile_module.PROJECT_STATUS_ACTIVE)

        profile_path.write_text(
            original.replace('[project]\n', '[project]\nstatus = "archived"\n'),
            encoding="utf-8",
        )
        self.assertEqual(self.load_test_profile().status, profile_module.PROJECT_STATUS_ARCHIVED)

    def test_load_profile_rejects_invalid_project_status(self) -> None:
        self.write_profile("Demo")
        profile_path = self.root / "blackdog.toml"
        profile_path.write_text(
            profile_path.read_text(encoding="utf-8").replace('[project]\n', '[project]\nstatus = "paused"\n'),
            encoding="utf-8",
        )

        with self.assertRaises(profile_module.ConfigError):
            self.load_test_profile()

    def test_load_profile_rejects_invalid_handler_kind(self) -> None:
        (self.root / "blackdog.toml").write_text(
            "[project]\nname = \"Demo\"\n\n"
            "[paths]\n"
            "control_dir = \"@git-common/blackdog\"\n\n"
            "[[handlers]]\n"
            "id = \"broken\"\n"
            "kind = \"not-real\"\n"
            "enabled = true\n",
            encoding="utf-8",
        )

        with self.assertRaises(profile_module.ConfigError):
            self.load_test_profile()

    def test_load_profile_rejects_handler_dependency_cycles(self) -> None:
        (self.root / "blackdog.toml").write_text(
            "[project]\nname = \"Demo\"\n\n"
            "[paths]\n"
            "control_dir = \"@git-common/blackdog\"\n\n"
            "[[handlers]]\n"
            "id = \"python\"\n"
            "kind = \"python-overlay-venv\"\n"
            "enabled = true\n"
            "depends_on = [\"blackdog\"]\n"
            "root_path = \".VE\"\n"
            "worktree_path = \".VE\"\n"
            "script_policy = \"root-bin-fallback\"\n\n"
            "[[handlers]]\n"
            "id = \"blackdog\"\n"
            "kind = \"blackdog-runtime\"\n"
            "enabled = true\n"
            "depends_on = [\"python\"]\n"
            "launcher_path = \".VE/bin/blackdog\"\n"
            "source_mode = \"managed-checkout\"\n"
            "managed_source_dir = \"@git-common/blackdog/source/blackdog\"\n"
            "self_repo_install_mode = \"editable-worktree-source\"\n"
            "other_repo_install_mode = \"launcher-shim\"\n",
            encoding="utf-8",
        )

        with self.assertRaises(profile_module.ConfigError):
            self.load_test_profile()

    def test_ensure_default_handlers_appends_blocks_once(self) -> None:
        profile_path = self.root / "blackdog.toml"
        profile_path.write_text(
            "[project]\nname = \"Demo\"\n\n"
            "[paths]\n"
            "control_dir = \"@git-common/blackdog\"\n",
            encoding="utf-8",
        )

        self.assertTrue(profile_module.ensure_default_handlers_in_profile(profile_path))
        self.assertFalse(profile_module.ensure_default_handlers_in_profile(profile_path))
        profile = self.load_test_profile()
        self.assertTrue(profile.handlers_explicit)
        self.assertEqual(len(profile.handlers), 2)

    def test_git_common_resolution_uses_repo_common_dir(self) -> None:
        with patch("blackdog_core.profile._run_git", return_value=".git"):
            resolved = profile_module._resolve_path_value(self.root, "@git-common/blackdog")
        self.assertEqual(resolved, (self.root / ".git" / "blackdog").resolve())

    def test_write_default_profile_prefers_existing_agent_docs(self) -> None:
        docs_dir = self.root / "docs"
        docs_dir.mkdir()
        (docs_dir / "AGENT_START.md").write_text("start here\n", encoding="utf-8")
        (docs_dir / "INDEX.md").write_text("index\n", encoding="utf-8")
        (self.root / "README.md").write_text("readme\n", encoding="utf-8")

        profile_module.write_default_profile(self.root, "Demo", force=True)
        profile = self.load_test_profile()

        self.assertEqual(
            profile.doc_routing_defaults,
            ("AGENTS.md", "docs/AGENT_START.md", "docs/INDEX.md"),
        )

    def test_write_default_profile_refuses_to_overwrite_without_force(self) -> None:
        profile_path = profile_module.write_default_profile(self.root, "Demo", force=True)
        self.assertEqual(profile_path, (self.root / "blackdog.toml").resolve())
        with self.assertRaises(profile_module.ConfigError):
            profile_module.write_default_profile(self.root, "Demo")
