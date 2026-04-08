from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import test_blackdog_cli as cli_tests


class CoreAuditTestCase(unittest.TestCase):
    def runtime_paths(self):
        return cli_tests.load_profile(self.root).paths

    def init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "blackdog@example.com"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Blackdog Test"], check=True, capture_output=True, text=True)
        (root / ".gitignore").write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "Initial test commit"], check=True, capture_output=True, text=True)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.init_git_repo(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()
