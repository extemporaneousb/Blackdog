from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from blackdog_core.profile import DEFAULT_WORKTREES_DIR, load_profile, render_default_profile


REPO_ROOT = Path(__file__).resolve().parents[1]


class CoreAuditTestCase(unittest.TestCase):
    def init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "blackdog@example.com"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Blackdog Test"], check=True, capture_output=True, text=True)
        (root / ".gitignore").write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "Initial test commit"], check=True, capture_output=True, text=True)

    def git_output(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def write_profile(self, project_name: str = "Demo") -> Path:
        profile_path = self.root / "blackdog.toml"
        worktrees_dir = f"../.worktrees-{self.root.name}"
        profile_text = render_default_profile(project_name).replace(
            f'worktrees_dir = "{DEFAULT_WORKTREES_DIR}"',
            f'worktrees_dir = "{worktrees_dir}"',
        )
        profile_path.write_text(profile_text, encoding="utf-8")
        return profile_path

    def load_test_profile(self):
        return load_profile(self.root)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.init_git_repo(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()
