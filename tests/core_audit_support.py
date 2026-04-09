from __future__ import annotations

import ast
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import test_blackdog_cli as cli_tests


FORBIDDEN_CORE_IMPORT_PREFIXES = (
    "blackdog_cli",
    "blackdog.supervisor",
    "blackdog.conversations",
    "blackdog.board",
    "blackdog.worktree",
    "extensions",
)


class CoreAuditTestCase(unittest.TestCase):
    def core_import_boundary_violations(self) -> list[str]:
        violations: list[str] = []
        for path in sorted((cli_tests.ROOT / "src" / "blackdog_core").rglob("*.py")):
            module = ".".join(path.relative_to(cli_tests.ROOT / "src").with_suffix("").parts)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                for target in self._import_targets(module, node):
                    if self._is_forbidden_core_import(target):
                        violations.append(f"{path.relative_to(cli_tests.ROOT)} imports {target}")
        return violations

    def _import_targets(self, module: str, node: ast.AST) -> list[str]:
        if isinstance(node, ast.Import):
            return [alias.name.strip() for alias in node.names if alias.name.strip()]
        if not isinstance(node, ast.ImportFrom):
            return []
        if node.module == "__future__":
            return ["__future__"]
        if node.level == 0:
            target_parts = [part for part in (node.module or "").split(".") if part]
        else:
            package = module.split(".")[:-1]
            keep = len(package) - (node.level - 1)
            if keep < 0:
                return []
            target_parts = package[:keep]
            if node.module:
                target_parts.extend(part for part in node.module.split(".") if part)
        if not target_parts and all(alias.name == "*" for alias in node.names):
            return []
        targets: list[str] = []
        for alias in node.names:
            alias_target = list(target_parts)
            if alias.name != "*":
                alias_target.extend(part for part in alias.name.split(".") if part)
            target = ".".join(alias_target)
            if target:
                targets.append(target)
        if not targets:
            target = ".".join(target_parts)
            if target:
                targets.append(target)
        return targets

    def _is_forbidden_core_import(self, target: str) -> bool:
        if target == "__future__":
            return False
        for prefix in FORBIDDEN_CORE_IMPORT_PREFIXES:
            if target == prefix or target.startswith(prefix + "."):
                return True
        if target.startswith("blackdog.") or target.startswith("blackdog_cli."):
            return True
        return False

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
