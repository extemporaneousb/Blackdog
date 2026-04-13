from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess

from blackdog_cli.main import main as blackdog_main
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class RepoLifecycleCliTests(CoreAuditTestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = blackdog_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_repo_install_bootstraps_profile_skill_and_launcher(self) -> None:
        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "install",
            "--project-root",
            str(self.root),
            "--project-name",
            "Lifecycle Demo",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["repo"]

        profile_path = self.root / "blackdog.toml"
        skill_path = self.root / ".codex" / "skills" / "blackdog" / "SKILL.md"
        launcher_path = self.root / ".VE" / "bin" / "blackdog"

        self.assertEqual(payload["action"], "install")
        self.assertTrue(profile_path.is_file())
        self.assertTrue(skill_path.is_file())
        self.assertTrue(launcher_path.is_file())

        skill_text = skill_path.read_text(encoding="utf-8")
        self.assertIn("Lifecycle Demo", skill_text)
        self.assertIn("repo install", skill_text)
        self.assertIn("docs/INDEX.md", skill_text)

        launcher_text = launcher_path.read_text(encoding="utf-8")
        self.assertIn("blackdog_cli", launcher_text)
        self.assertIn(str((REPO_ROOT / "src").resolve()), launcher_text)

        completed = subprocess.run(
            [str(launcher_path), "summary", "--project-root", str(self.root)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Project: Lifecycle Demo", completed.stdout)

    def test_repo_update_repairs_launcher_without_overwriting_skill(self) -> None:
        exit_code, _, stderr = self.run_cli(
            "repo",
            "install",
            "--project-root",
            str(self.root),
        )
        self.assertEqual(exit_code, 0, stderr)

        skill_path = self.root / ".codex" / "skills" / "blackdog" / "SKILL.md"
        launcher_path = self.root / ".VE" / "bin" / "blackdog"
        skill_path.write_text("custom skill\n", encoding="utf-8")
        launcher_path.write_text("#!/bin/sh\necho broken\n", encoding="utf-8")
        launcher_path.chmod(0o755)

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "update",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["repo"]

        self.assertEqual(payload["action"], "update")
        self.assertEqual(skill_path.read_text(encoding="utf-8"), "custom skill\n")
        self.assertIn("blackdog_cli", launcher_path.read_text(encoding="utf-8"))
        self.assertIn(str((REPO_ROOT / "src").resolve()), launcher_path.read_text(encoding="utf-8"))

    def test_repo_refresh_regenerates_skill_from_profile_contract(self) -> None:
        exit_code, _, stderr = self.run_cli(
            "repo",
            "install",
            "--project-root",
            str(self.root),
        )
        self.assertEqual(exit_code, 0, stderr)

        profile_path = self.root / "blackdog.toml"
        profile_text = profile_path.read_text(encoding="utf-8")
        profile_text = profile_text.replace(
            'doc_routing_defaults = ["AGENTS.md", "docs/INDEX.md", "docs/PRODUCT_SPEC.md", "docs/ARCHITECTURE.md", "docs/TARGET_MODEL.md", "docs/CLI.md", "docs/FILE_FORMATS.md"]',
            'doc_routing_defaults = ["AGENTS.md", "docs/CUSTOM.md"]',
        )
        profile_path.write_text(profile_text, encoding="utf-8")

        skill_path = self.root / ".codex" / "skills" / "blackdog" / "SKILL.md"
        skill_path.write_text("stale skill\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "refresh",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["repo"]

        self.assertEqual(payload["action"], "refresh")
        skill_text = skill_path.read_text(encoding="utf-8")
        self.assertNotIn("stale skill", skill_text)
        self.assertIn("docs/CUSTOM.md", skill_text)
        self.assertIn("repo refresh", skill_text)
