from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

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

    def install_with_local_source(self, *, project_name: str = "Acceptance Demo") -> dict[str, object]:
        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "install",
            "--project-root",
            str(self.root),
            "--project-name",
            project_name,
            "--source-root",
            str(REPO_ROOT),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        return json.loads(stdout)["repo"]

    def test_repo_install_refresh_and_analyze_keep_target_layering_lean(self) -> None:
        install_payload = self.install_with_local_source()
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

    def test_managed_checkout_source_mode_reuses_seeded_managed_source(self) -> None:
        self.install_with_local_source()
        launcher_path = self.root / ".VE" / "bin" / "blackdog"

        updated = subprocess.run(
            [str(launcher_path), "repo", "update", "--project-root", str(self.root), "--json"],
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(updated.stdout)["repo"]
        self.assertEqual(payload["action"], "update")
        self.assertEqual(payload["source_mode"], "managed-checkout")
        self.assertTrue(Path(str(payload["source_root"])).is_dir())
        self.assertTrue((self.root / ".git" / "blackdog" / "source" / "blackdog").is_dir())

    def test_repo_install_repairs_missing_root_venv_and_launcher(self) -> None:
        self.install_with_local_source()
        shutil.rmtree(self.root / ".VE")

        exit_code, stdout, stderr = self.run_cli("repo", "analyze", "--project-root", str(self.root), "--json")
        self.assertEqual(exit_code, 0, stderr)
        finding_codes = {row["code"] for row in json.loads(stdout)["repo_analysis"]["findings"]}
        self.assertIn("missing-root-venv", finding_codes)

        repaired = self.install_with_local_source()

        self.assertEqual(repaired["action"], "install")
        self.assertTrue((self.root / ".VE" / "bin" / "blackdog").is_file())
        exit_code, stdout, stderr = self.run_cli("repo", "analyze", "--project-root", str(self.root), "--json")
        self.assertEqual(exit_code, 0, stderr)
        self.assertNotIn("missing-root-venv", {row["code"] for row in json.loads(stdout)["repo_analysis"]["findings"]})

        (self.root / ".VE" / "bin" / "blackdog").unlink()
        exit_code, stdout, stderr = self.run_cli("repo", "analyze", "--project-root", str(self.root), "--json")
        self.assertEqual(exit_code, 0, stderr)
        finding_codes = {row["code"] for row in json.loads(stdout)["repo_analysis"]["findings"]}
        self.assertIn("missing-blackdog-launcher", finding_codes)

        repaired = self.install_with_local_source()

        self.assertEqual(repaired["action"], "install")
        self.assertTrue((self.root / ".VE" / "bin" / "blackdog").is_file())

    def test_task_begin_from_linked_worktree_targets_linked_branch(self) -> None:
        self.install_with_local_source()
        subprocess.run(
            ["git", "-C", str(self.root), "add", "blackdog.toml", "AGENTS.md", ".codex"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Install Blackdog"],
            check=True,
            capture_output=True,
            text=True,
        )
        launcher_path = self.root / ".VE" / "bin" / "blackdog"
        linked_parent = tempfile.TemporaryDirectory()
        linked_worktree = Path(linked_parent.name) / "linked"
        task_worktree: Path | None = None
        try:
            subprocess.run(
                ["git", "-C", str(self.root), "worktree", "add", "-b", "feature/acceptance", str(linked_worktree), "main"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    str(launcher_path),
                    "repo",
                    "install",
                    "--project-root",
                    str(linked_worktree),
                    "--source-root",
                    str(REPO_ROOT),
                    "--json",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            linked_launcher = linked_worktree / ".VE" / "bin" / "blackdog"
            self.assertTrue(linked_launcher.is_file())
            begin = subprocess.run(
                [
                    str(linked_launcher),
                    "task",
                    "begin",
                    "--project-root",
                    str(linked_worktree),
                    "--actor",
                    "codex",
                    "--prompt",
                    "Implement linked target branch behavior.",
                    "--json",
                ],
                cwd=str(linked_worktree),
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(begin.stdout)["task"]
            task_worktree = Path(payload["worktree"]["worktree_path"])
            self.assertEqual(payload["worktree"]["target_branch"], "feature/acceptance")
            close = subprocess.run(
                [
                    str(linked_launcher),
                    "task",
                    "close",
                    "--project-root",
                    str(linked_worktree),
                    "--status",
                    "abandoned",
                    "--summary",
                    "acceptance test cleanup",
                    "--cleanup",
                    "--json",
                ],
                cwd=str(task_worktree),
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(close.stdout)["closure"]["status"], "abandoned")
        finally:
            if task_worktree is not None and task_worktree.exists():
                subprocess.run(["git", "-C", str(self.root), "worktree", "remove", "--force", str(task_worktree)], check=False)
            if linked_worktree.exists():
                subprocess.run(["git", "-C", str(self.root), "worktree", "remove", "--force", str(linked_worktree)], check=False)
            subprocess.run(["git", "-C", str(self.root), "branch", "-D", "feature/acceptance"], check=False, capture_output=True, text=True)
            linked_parent.cleanup()

    def test_archived_and_unarchived_repos_are_reflected_in_repo_table(self) -> None:
        self.install_with_local_source()

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "table",
            "--root",
            str(self.root),
            "--no-codex",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual([row["project_name"] for row in json.loads(stdout)["repo_table"]["rows"]], ["Acceptance Demo"])

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "archive",
            "--project-root",
            str(self.root),
            "--reason",
            "acceptance matrix",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["repo"]["status"], "archived")

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "table",
            "--root",
            str(self.root),
            "--no-codex",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["repo_table"]["rows"], [])

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "table",
            "--root",
            str(self.root),
            "--include-archived",
            "--no-codex",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        rows = json.loads(stdout)["repo_table"]["rows"]
        self.assertEqual(rows[0]["status"], "archived")

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "unarchive",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["repo"]["status"], "active")
