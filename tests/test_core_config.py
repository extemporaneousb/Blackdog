from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import blackdog_core.profile as profile_module
from tests.core_audit_support import CoreAuditTestCase


class CoreConfigTests(CoreAuditTestCase):
    def test_load_profile_defaults_to_machine_native_control_files(self) -> None:
        self.write_profile("Demo")
        profile = self.load_test_profile()

        self.assertEqual(profile.paths.control_dir, (self.root / ".git" / "blackdog").resolve())
        self.assertEqual(profile.paths.planning_file, profile.paths.control_dir / "planning.json")
        self.assertEqual(profile.paths.runtime_file, profile.paths.control_dir / "runtime.json")
        self.assertEqual(profile.paths.events_file, profile.paths.control_dir / "events.jsonl")
        self.assertEqual(profile.paths.worktrees_dir, (self.root.parent / f".worktrees-{self.root.name}").resolve())
        self.assertTrue(profile.handlers_explicit)
        self.assertEqual(profile.handlers[0].kind, profile_module.HANDLER_KIND_PYTHON_OVERLAY_VENV)
        self.assertEqual(profile.handlers[1].kind, profile_module.HANDLER_KIND_BLACKDOG_RUNTIME)

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
        self.assertFalse(profile.handlers_explicit)
        self.assertEqual(profile.handlers[0].handler_id, "python")
        self.assertEqual(profile.handlers[1].handler_id, "blackdog")

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
