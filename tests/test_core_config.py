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

    def test_git_common_resolution_uses_repo_common_dir(self) -> None:
        with patch("blackdog_core.profile._run_git", return_value=".git"):
            resolved = profile_module._resolve_path_value(self.root, "@git-common/blackdog")
        self.assertEqual(resolved, (self.root / ".git" / "blackdog").resolve())

    def test_write_default_profile_refuses_to_overwrite_without_force(self) -> None:
        profile_path = profile_module.write_default_profile(self.root, "Demo", force=True)
        self.assertEqual(profile_path, (self.root / "blackdog.toml").resolve())
        with self.assertRaises(profile_module.ConfigError):
            profile_module.write_default_profile(self.root, "Demo")
