"""Enforce the core's static import direction without importing product code."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


def _product_imports(source: str) -> list[tuple[int, str]]:
    violations = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            if module.split(".")[0] in {"blackdog", "blackdog_cli"}:
                violations.append((node.lineno, module))
    return sorted(violations)


class CoreImportBoundaryTests(unittest.TestCase):
    def test_core_does_not_import_product_or_cli_packages(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "blackdog_core"
        modules = sorted(root.rglob("*.py"))
        self.assertTrue(modules)
        for module in modules:
            with self.subTest(module=module.relative_to(root).as_posix()):
                self.assertEqual(_product_imports(module.read_text(encoding="utf-8")), [])

    def test_rule_rejects_direct_aliased_nested_and_conditional_product_imports(self) -> None:
        sources = (
            ("import blackdog", "blackdog"),
            ("import os, blackdog.wtam as lifecycle", "blackdog.wtam"),
            ("from blackdog import wtam", "blackdog"),
            ("from blackdog_cli.main import main as run", "blackdog_cli.main"),
            ("def run():\n    import blackdog_cli", "blackdog_cli"),
            ("if TYPE_CHECKING:\n    from blackdog.evidence import EvidenceError", "blackdog.evidence"),
        )
        for source, module in sources:
            with self.subTest(source=source):
                self.assertEqual(_product_imports(source), [(len(source.splitlines()), module)])

    def test_rule_accepts_stdlib_core_and_relative_imports(self) -> None:
        self.assertEqual(_product_imports("""
import json, pathlib
import blackdog_core.state as state
from blackdog_core.tasks import create_task
from .profile import RepoProfile
from . import state
# import blackdog
description = 'from blackdog_cli import main'
"""), [])


if __name__ == "__main__":
    unittest.main()
