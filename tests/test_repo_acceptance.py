from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import subprocess

from blackdog.contract import managed_skill_name, managed_skill_relative_path
from blackdog_cli.main import main as blackdog_main
from blackdog_core.profile import load_profile
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class RepoAcceptanceTests(CoreAuditTestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = blackdog_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_repo_install_refresh_and_analyze_keep_target_layering_lean(self) -> None:
        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "install",
            "--project-root",
            str(self.root),
            "--project-name",
            "Acceptance Demo",
            "--source-root",
            str(REPO_ROOT),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        install_payload = json.loads(stdout)["repo"]
        self.assertEqual(install_payload["action"], "install")
        self.assertEqual(install_payload["source_mode"], "local-override")

        profile = load_profile(self.root)
        skill_path = self.root / managed_skill_relative_path(profile)
        metadata_path = skill_path.parent / "agents" / "openai.yaml"
        launcher_path = self.root / ".VE" / "bin" / "blackdog"

        self.assertEqual(profile.project_name, "Acceptance Demo")
        self.assertEqual(profile.doc_routing_defaults, ("AGENTS.md",))
        self.assertEqual([handler.kind for handler in profile.handlers], ["python-overlay-venv", "blackdog-runtime"])
        self.assertTrue((self.root / "blackdog.toml").is_file())
        self.assertTrue((self.root / "AGENTS.md").is_file())
        self.assertTrue(skill_path.is_file())
        self.assertTrue(metadata_path.is_file())
        self.assertTrue(launcher_path.is_file())

        agents_text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("BLACKDOG MANAGED CONTRACT:BEGIN", agents_text)
        self.assertIn("workspace role", agents_text)
        self.assertNotIn("docs/PRODUCT_SPEC.md", agents_text)
        self.assertNotIn("docs/TARGET_MODEL.md", agents_text)

        skill_text = skill_path.read_text(encoding="utf-8")
        self.assertIn(f"name: {managed_skill_name(profile)}", skill_text)
        self.assertIn("Execution Contract", skill_text)
        self.assertIn("goal, context, constraints, and done condition", skill_text)
        self.assertIn("delegate setup, state, recovery, and landing to the Blackdog CLI", skill_text)
        self.assertIn("- `AGENTS.md`", skill_text)
        self.assertNotIn("docs/PRODUCT_SPEC.md", skill_text)
        self.assertNotIn("docs/TARGET_MODEL.md", skill_text)

        refresh = subprocess.run(
            [str(launcher_path), "repo", "refresh", "--project-root", str(self.root), "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
        refresh_payload = json.loads(refresh.stdout)["repo"]
        self.assertEqual(refresh_payload["action"], "refresh")
        self.assertIsNotNone(refresh_payload["handlers"])
        self.assertEqual(skill_path.read_text(encoding="utf-8"), skill_text)

        analyze = subprocess.run(
            [str(launcher_path), "repo", "analyze", "--project-root", str(self.root), "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
        analysis = json.loads(analyze.stdout)["repo_analysis"]
        self.assertEqual(analysis["conversion_status"], "blackdog-backed")
        self.assertEqual(analysis["current_doc_routing"], ["AGENTS.md"])
        finding_codes = {row["code"] for row in analysis["findings"]}
        self.assertNotIn("missing-blackdog-profile", finding_codes)
        self.assertNotIn("missing-managed-agents-contract", finding_codes)
        self.assertNotIn("missing-managed-skill", finding_codes)

        preflight = subprocess.run(
            [str(launcher_path), "worktree", "preflight", "--project-root", str(self.root), "--json"],
            check=True,
            capture_output=True,
            text=True,
        )
        preflight_payload = json.loads(preflight.stdout)
        self.assertEqual(preflight_payload["workspace_role"], "primary")
        self.assertTrue(preflight_payload["current_worktree_has_local_blackdog"])
