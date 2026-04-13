from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys

from blackdog_cli.main import main as blackdog_main
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class EnvHandlerTests(CoreAuditTestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = blackdog_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def _commit_if_dirty(self, repo_root: Path, message: str) -> None:
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--short"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not status:
            return
        subprocess.run(["git", "-C", str(repo_root), "add", "."], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo_root), "commit", "-m", message], check=True, capture_output=True, text=True)

    def test_host_repo_worktree_start_links_root_bin_fallback_and_uses_local_python_path(self) -> None:
        self.write_profile("Env Host")
        self._commit_if_dirty(self.root, "Add profile")

        exit_code, _, stderr = self.run_cli(
            "repo",
            "install",
            "--project-root",
            str(self.root),
            "--source-root",
            str(REPO_ROOT),
        )
        self.assertEqual(exit_code, 0, stderr)
        self._commit_if_dirty(self.root, "Add repo runtime")

        demo_tool = self.root / ".VE" / "bin" / "demo-tool"
        demo_tool.write_text("#!/bin/sh\necho root-tool\n", encoding="utf-8")
        demo_tool.chmod(0o755)

        payload = {
            "id": "env-host",
            "title": "Env Host",
            "tasks": [{"id": "ENVH-1", "title": "Start worktree", "intent": "exercise env handlers"}],
        }
        exit_code, _, stderr = self.run_cli(
            "workset",
            "put",
            "--project-root",
            str(self.root),
            "--json",
            json.dumps(payload),
        )
        self.assertEqual(exit_code, 0, stderr)

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "env-host",
            "--task",
            "ENVH-1",
            "--actor",
            "codex",
            "--prompt",
            "Start the env host worktree.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["worktree"]
        worktree_path = Path(payload["worktree_path"])
        launcher_path = worktree_path / ".VE" / "bin" / "blackdog"
        tool_path = worktree_path / ".VE" / "bin" / "demo-tool"

        self.assertTrue(launcher_path.is_file())
        self.assertTrue(tool_path.exists())
        launcher_text = launcher_path.read_text(encoding="utf-8")
        self.assertIn(str(worktree_path / ".VE" / "bin" / "python"), launcher_text)
        self.assertIn(str((self.root / ".git" / "blackdog" / "source" / "blackdog" / "src").resolve()), launcher_text)
        self.assertTrue(any(action["action"] == "root-bin-fallback" for action in payload["handlers"]["actions"]))

    def test_self_repo_worktree_start_uses_editable_worktree_source_overlay(self) -> None:
        self_repo = self.root / "self-blackdog"
        shutil.copytree(
            REPO_ROOT,
            self_repo,
            ignore=shutil.ignore_patterns(".git", ".VE", "__pycache__", "*.pyc"),
        )
        subprocess.run(["git", "init", "-b", "main", str(self_repo)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self_repo), "config", "user.email", "blackdog@example.com"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self_repo), "config", "user.name", "Blackdog Test"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self_repo), "add", "."], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self_repo), "commit", "-m", "Import Blackdog source"], check=True, capture_output=True, text=True)
        subprocess.run([sys.executable, "-m", "venv", str(self_repo / ".VE")], check=True, capture_output=True, text=True)

        payload = {
            "id": "self-env",
            "title": "Self Env",
            "tasks": [{"id": "SELF-1", "title": "Use target repo mode", "intent": "exercise self-host env handlers"}],
        }
        exit_code, _, stderr = self.run_cli(
            "workset",
            "put",
            "--project-root",
            str(self_repo),
            "--json",
            json.dumps(payload),
        )
        self.assertEqual(exit_code, 0, stderr)

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "preview",
            "--project-root",
            str(self_repo),
            "--workset",
            "self-env",
            "--task",
            "SELF-1",
            "--actor",
            "codex",
            "--prompt",
            "Preview self-host env handling.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        preview = json.loads(stdout)["worktree_preview"]
        self.assertTrue(preview["start_ready"])
        self.assertEqual(preview["handlers"]["runtime_mode"], "editable-worktree-source")
        self.assertEqual(preview["handlers"]["source_mode"], "target-repo")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self_repo),
            "--workset",
            "self-env",
            "--task",
            "SELF-1",
            "--actor",
            "codex",
            "--prompt",
            "Start self-host env handling.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["worktree"]
        worktree_path = Path(payload["worktree_path"])
        launcher_path = worktree_path / ".VE" / "bin" / "blackdog"
        overlay_path = next(worktree_path.glob(".VE/lib/python*/site-packages/blackdog-worktree-source.pth"))

        self.assertTrue(launcher_path.is_file())
        self.assertTrue(overlay_path.is_file())
        self.assertEqual(payload["runtime_mode"], "editable-worktree-source")
        self.assertEqual(payload["source_mode"], "target-repo")
        launcher_text = launcher_path.read_text(encoding="utf-8")
        self.assertIn(str(worktree_path / ".VE" / "bin" / "python"), launcher_text)
        self.assertNotIn("PYTHONPATH=", launcher_text)
        self.assertEqual(overlay_path.read_text(encoding="utf-8").strip(), str(worktree_path / "src"))

        completed = subprocess.run(
            [str(launcher_path), "summary", "--project-root", str(self_repo)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Project: Blackdog", completed.stdout)
