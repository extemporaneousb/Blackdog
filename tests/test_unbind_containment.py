from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blackdog import repo_membership as membership
from tests.core_audit_support import CoreAuditTestCase


class UnbindContainmentTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.root.resolve()
        self.profile_path = self.write_profile()
        with self.profile_path.open("a", encoding="utf-8") as stream:
            stream.write(
                '\n[[handlers]]\nkind = "blackdog-runtime"\n'
                'source_mode = "managed-checkout"\nlauncher_path = ".VE/bin/blackdog"\n'
            )
        self.control = self.root / ".git" / "blackdog"
        self.control.mkdir()
        (self.control / "events.jsonl").write_text("retained evidence\n", encoding="utf-8")
        self.launcher = self.root / ".VE" / "bin" / "blackdog"

    def test_external_ancestor_is_preserved_in_preview_and_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external).resolve()
            outside_launcher = outside / "bin" / "blackdog"
            outside_launcher.parent.mkdir()
            outside_launcher.write_text("user-owned\n", encoding="utf-8")
            (self.root / ".VE").symlink_to(outside, target_is_directory=True)
            for confirm in (False, True):
                with self.subTest(confirm=confirm):
                    result = membership.unbind_repo(self.root, confirm=confirm)
                    self.assertNotIn(str(self.launcher), result.planned_removals)
                    self.assertIn(str(self.launcher), result.preserved)
                    self.assertTrue(result.warnings)
                    self.assertEqual(outside_launcher.read_text(encoding="utf-8"), "user-owned\n")

    def test_internal_and_dangling_ancestors_are_preserved(self) -> None:
        for dangling in (False, True):
            with self.subTest(dangling=dangling):
                target = self.root / ("missing-environment" if dangling else "environment")
                if not dangling:
                    (target / "bin").mkdir(parents=True)
                    (target / "bin" / "blackdog").write_text("user-owned\n", encoding="utf-8")
                link = self.root / ".VE"
                link.symlink_to(target, target_is_directory=True)
                try:
                    result = membership.unbind_repo(self.root)
                    self.assertNotIn(str(self.launcher), result.planned_removals)
                    self.assertIn(str(self.launcher), result.preserved)
                    self.assertTrue(link.is_symlink())
                finally:
                    link.unlink()

    def test_final_launcher_symlink_is_unlinked_without_following_target(self) -> None:
        with tempfile.TemporaryDirectory() as external:
            target = Path(external) / "user-owned"
            target.write_text("keep\n", encoding="utf-8")
            self.launcher.parent.mkdir(parents=True)
            self.launcher.symlink_to(target)
            preview = membership.unbind_repo(self.root)
            self.assertIn(str(self.launcher), preview.planned_removals)
            result = membership.unbind_repo(self.root, confirm=True)
            self.assertIn(str(self.launcher), result.removed)
            self.assertFalse(self.launcher.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    def test_dangling_final_launcher_symlink_is_unlinked(self) -> None:
        self.launcher.parent.mkdir(parents=True)
        self.launcher.symlink_to(self.root / "missing-launcher")
        result = membership.unbind_repo(self.root, confirm=True)
        self.assertIn(str(self.launcher), result.removed)
        self.assertFalse(self.launcher.is_symlink())

    def test_internal_launcher_removal_preserves_environment_and_user_content(self) -> None:
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("owned launcher\n", encoding="utf-8")
        sibling = self.launcher.with_name("user-tool")
        sibling.write_text("keep\n", encoding="utf-8")
        result = membership.unbind_repo(self.root, confirm=True)
        self.assertIn(str(self.launcher), result.removed)
        self.assertFalse(self.launcher.exists())
        self.assertEqual(sibling.read_text(encoding="utf-8"), "keep\n")


    def test_launcher_directory_is_preserved(self) -> None:
        self.launcher.mkdir(parents=True)
        sentinel = self.launcher / "user-file"
        sentinel.write_text("keep", encoding="utf-8")
        result = membership.unbind_repo(self.root, confirm=True)
        self.assertIn(str(self.launcher), result.preserved)
        self.assertNotIn(str(self.launcher), result.planned_removals)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_nested_symlink_ancestor_is_preserved_on_confirm(self) -> None:
        target = self.root / "environment-bin"
        target.mkdir()
        sentinel = target / "blackdog"
        sentinel.write_text("keep", encoding="utf-8")
        self.launcher.parent.parent.mkdir()
        self.launcher.parent.symlink_to(target, target_is_directory=True)
        result = membership.unbind_repo(self.root, confirm=True)
        self.assertIn(str(self.launcher), result.preserved)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_dangling_ancestor_is_preserved_on_confirm(self) -> None:
        ancestor = self.root / ".VE"
        ancestor.symlink_to(self.root / "missing", target_is_directory=True)
        result = membership.unbind_repo(self.root, confirm=True)
        self.assertIn(str(self.launcher), result.preserved)
        self.assertTrue(ancestor.is_symlink())

    def test_final_managed_skill_symlink_does_not_remove_target_directory(self) -> None:
        with tempfile.TemporaryDirectory() as external:
            target = Path(external)
            sentinel = target / "user-file"
            sentinel.write_text("keep", encoding="utf-8")
            skill = membership._managed_skill_dir(self.root, "Demo")
            skill.parent.mkdir(parents=True)
            skill.symlink_to(target, target_is_directory=True)
            preview = membership.unbind_repo(self.root)
            self.assertIn(str(skill), preview.planned_removals)
            result = membership.unbind_repo(self.root, confirm=True)
            self.assertIn(str(skill), result.removed)
            self.assertFalse(skill.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_managed_directory_removal_does_not_follow_child_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as external:
            target = Path(external)
            sentinel = target / "user-file"
            sentinel.write_text("keep", encoding="utf-8")
            skill = membership._managed_skill_dir(self.root, "Demo")
            skill.mkdir(parents=True)
            (skill / "linked-content").symlink_to(target, target_is_directory=True)
            result = membership.unbind_repo(self.root, confirm=True)
            self.assertIn(str(skill), result.removed)
            self.assertFalse(skill.exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_managed_skill_ancestor_symlink_does_not_authorize_target_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as external:
            target = Path(external)
            skill = membership._managed_skill_dir(self.root, "Demo")
            external_skill = target / "skills" / skill.name
            external_skill.mkdir(parents=True)
            sentinel = external_skill / "SKILL.md"
            sentinel.write_text("user-owned", encoding="utf-8")
            (self.root / ".codex").symlink_to(target, target_is_directory=True)
            result = membership.unbind_repo(self.root, confirm=True)
            self.assertIn(str(skill), result.preserved)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "user-owned")

    def test_agents_symlink_preserves_target_and_user_text(self) -> None:
        target = self.root / "user-agents"
        original = (
            "User instructions\n\n" + membership.AGENTS_MANAGED_BEGIN + "\nManaged\n"
            + membership.AGENTS_MANAGED_END + "\n"
        )
        target.write_text(original, encoding="utf-8")
        agents = self.root / "AGENTS.md"
        agents.symlink_to(target)
        result = membership.unbind_repo(self.root, confirm=True)
        self.assertIn(str(agents), result.preserved)
        self.assertFalse(result.updated)
        self.assertTrue(agents.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_profile_symlink_removes_only_the_link(self) -> None:
        with tempfile.TemporaryDirectory() as external:
            target = Path(external) / "profile.toml"
            text = self.profile_path.read_text(encoding="utf-8")
            self.profile_path.rename(target)
            self.profile_path.symlink_to(target)
            result = membership.unbind_repo(self.root, confirm=True)
            self.assertIn(str(self.profile_path), result.removed)
            self.assertFalse(self.profile_path.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), text)

    def test_legacy_history_and_unmanaged_skill_are_preserved(self) -> None:
        self.profile_path.write_text(
            self.profile_path.read_text(encoding="utf-8").replace(
                'control_dir = "@git-common/blackdog"', 'control_dir = ".blackdog"',
            ), encoding="utf-8",
        )
        control = self.root / ".blackdog"
        control.mkdir()
        history = control / "history.jsonl"
        history.write_text("history evidence", encoding="utf-8")
        (control / "runtime.json").write_text("runtime evidence", encoding="utf-8")
        legacy_skill = membership._legacy_managed_skill_dir(self.root)
        legacy_skill.mkdir(parents=True)
        user_skill = legacy_skill / "SKILL.md"
        user_skill.write_text("user-owned", encoding="utf-8")
        result = membership.unbind_repo(self.root, confirm=True)
        self.assertIn(str(history), result.preserved)
        self.assertEqual(history.read_text(encoding="utf-8"), "history evidence")
        self.assertFalse((control / "runtime.json").exists())
        self.assertEqual(user_skill.read_text(encoding="utf-8"), "user-owned")

    def test_ancestor_swap_after_preview_fails_before_membership_or_evidence_removal(self) -> None:
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("owned", encoding="utf-8")
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external)
            (outside / "bin").mkdir()
            sentinel = outside / "bin" / "blackdog"
            sentinel.write_text("keep", encoding="utf-8")

            def swap_ancestor(*args: object) -> tuple[str, ...]:
                (self.root / ".VE").rename(self.root / "original-environment")
                (self.root / ".VE").symlink_to(outside, target_is_directory=True)
                return ()

            with patch.object(membership, "_unrelated_dirty_paths", side_effect=swap_ancestor):
                with self.assertRaises(OSError):
                    membership.unbind_repo(self.root, confirm=True)
            self.assertTrue(self.profile_path.is_file())
            self.assertEqual((self.control / "events.jsonl").read_text(encoding="utf-8"), "retained evidence\n")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_ancestor_swap_during_unlink_cannot_redirect_the_removal(self) -> None:
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("owned", encoding="utf-8")
        parked = self.root / "original-environment"
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external)
            (outside / "bin").mkdir()
            sentinel = outside / "bin" / "blackdog"
            sentinel.write_text("keep", encoding="utf-8")
            original_unlink = os.unlink

            def swap_then_unlink(path: str, *, dir_fd: int | None = None) -> None:
                if path == "blackdog" and not parked.exists():
                    (self.root / ".VE").rename(parked)
                    (self.root / ".VE").symlink_to(outside, target_is_directory=True)
                original_unlink(path, dir_fd=dir_fd)

            with patch.object(membership.os, "unlink", side_effect=swap_then_unlink):
                result = membership.unbind_repo(self.root, confirm=True, keep_control_dir=True)
            self.assertIn(str(self.launcher), result.removed)
            self.assertFalse((parked / "bin" / "blackdog").exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_replaced_final_entry_is_rejected_before_removal(self) -> None:
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("owned", encoding="utf-8")
        replacement = self.launcher.with_name("replacement")
        replacement.write_text("user-owned", encoding="utf-8")

        def replace_launcher(*args: object) -> tuple[str, ...]:
            replacement.replace(self.launcher)
            return ()

        with patch.object(membership, "_unrelated_dirty_paths", side_effect=replace_launcher):
            with self.assertRaisesRegex(membership.RepoLifecycleError, "changed after inspection"):
                membership.unbind_repo(self.root, confirm=True)
        self.assertEqual(self.launcher.read_text(encoding="utf-8"), "user-owned")
        self.assertTrue(self.profile_path.exists())
        self.assertTrue((self.control / "events.jsonl").is_file())

    def test_launcher_removal_failure_preserves_profile_and_control_evidence(self) -> None:
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_text("owned", encoding="utf-8")
        with patch.object(membership, "_remove_path", side_effect=PermissionError("synthetic failure")):
            with self.assertRaises(PermissionError):
                membership.unbind_repo(self.root, confirm=True)
        self.assertTrue(self.launcher.exists())
        self.assertTrue(self.profile_path.exists())
        self.assertEqual((self.control / "events.jsonl").read_text(encoding="utf-8"), "retained evidence\n")


    def set_control_path(self, value: str) -> None:
        self.profile_path.write_text(
            self.profile_path.read_text(encoding="utf-8").replace(
                'control_dir = "@git-common/blackdog"', f'control_dir = "{value}"',
            ), encoding="utf-8",
        )

    def test_control_ancestor_symlink_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external)
            external_control = outside / "control"
            external_control.mkdir()
            sentinel = external_control / "events.jsonl"
            sentinel.write_text("external evidence", encoding="utf-8")
            ancestor = self.root / "state"
            ancestor.symlink_to(outside, target_is_directory=True)
            self.set_control_path("state/control")
            result = membership.unbind_repo(self.root, confirm=True)
            self.assertIn(str(ancestor / "control"), result.preserved)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "external evidence")

    def test_final_control_symlink_is_unlinked_without_removing_evidence_target(self) -> None:
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external)
            sentinel = outside / "events.jsonl"
            sentinel.write_text("external evidence", encoding="utf-8")
            link = self.root / "linked-control"
            link.symlink_to(outside, target_is_directory=True)
            self.set_control_path("linked-control")
            result = membership.unbind_repo(self.root, confirm=True)
            self.assertIn(str(link), result.removed)
            self.assertFalse(link.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "external evidence")

    def test_git_common_root_cannot_be_removed_as_a_control_directory(self) -> None:
        self.set_control_path("@git-common")
        result = membership.unbind_repo(self.root, confirm=True)
        self.assertIn(str(self.root / ".git"), result.preserved)
        self.assertNotIn(str(self.root / ".git"), result.planned_removals)
        self.assertTrue((self.root / ".git" / "HEAD").is_file())
        self.assertTrue((self.control / "events.jsonl").is_file())

    def test_repository_root_cannot_be_removed_as_a_control_directory(self) -> None:
        self.set_control_path(".")
        result = membership.unbind_repo(self.root, confirm=True)
        self.assertIn(str(self.root), result.preserved)
        self.assertNotIn(str(self.root), result.planned_removals)
        self.assertTrue((self.root / ".git" / "HEAD").is_file())

    def test_legacy_history_symlink_preserves_control_and_its_target(self) -> None:
        self.set_control_path(".blackdog")
        control = self.root / ".blackdog"
        control.mkdir()
        target = control / "historical-events.jsonl"
        target.write_text("history evidence", encoding="utf-8")
        history = control / "history.jsonl"
        history.symlink_to(target.name)
        result = membership.unbind_repo(self.root, confirm=True)
        self.assertIn(str(control), result.preserved)
        self.assertNotIn(str(control), result.planned_removals)
        self.assertTrue(history.is_symlink())
        self.assertEqual(history.read_text(encoding="utf-8"), "history evidence")

    def test_control_removal_failure_retains_membership_for_retry(self) -> None:
        with patch.object(membership, "_remove_control_dir", side_effect=PermissionError("synthetic failure")):
            with self.assertRaises(PermissionError):
                membership.unbind_repo(self.root, confirm=True)
        self.assertTrue(self.profile_path.is_file())
        self.assertTrue((self.control / "events.jsonl").is_file())

    def test_regular_agents_strip_preserves_user_instructions(self) -> None:
        agents = self.root / "AGENTS.md"
        agents.write_text(
            "User before\n\n" + membership.AGENTS_MANAGED_BEGIN + "\nManaged\n"
            + membership.AGENTS_MANAGED_END + "\n\nUser after\n", encoding="utf-8",
        )
        result = membership.unbind_repo(self.root, confirm=True)
        self.assertIn(str(agents), result.updated)
        self.assertEqual(agents.read_text(encoding="utf-8"), "User before\n\nUser after\n")

    def test_agents_replacement_before_update_is_not_followed(self) -> None:
        agents = self.root / "AGENTS.md"
        agents.write_text(
            membership.AGENTS_MANAGED_BEGIN + "\nManaged\n" + membership.AGENTS_MANAGED_END,
            encoding="utf-8",
        )
        target = self.root / "user-instructions"
        target.write_text("keep", encoding="utf-8")
        original_strip = membership._strip_unbind_agents

        def replace_then_strip(path: Path, **kwargs: object) -> bool:
            path.unlink()
            path.symlink_to(target)
            return original_strip(path, **kwargs)

        with patch.object(membership, "_strip_unbind_agents", side_effect=replace_then_strip):
            with self.assertRaises(OSError):
                membership.unbind_repo(self.root, confirm=True)
        self.assertTrue(self.profile_path.is_file())
        self.assertTrue((self.control / "events.jsonl").is_file())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
