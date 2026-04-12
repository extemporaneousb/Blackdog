from __future__ import annotations

import tomllib

from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class CoreContractTests(CoreAuditTestCase):
    def test_pyproject_and_makefile_keep_the_shipped_cli_surface(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["scripts"], {"blackdog": "blackdog_cli.main:main"})
        self.assertEqual(
            pyproject["tool"]["blackdog"]["coverage"]["shipped_surface"],
            [
                "src/blackdog_core/backlog.py",
                "src/blackdog_core/profile.py",
                "src/blackdog_core/runtime_model.py",
                "src/blackdog_core/snapshot.py",
                "src/blackdog_core/state.py",
            ],
        )
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("python3 -m blackdog_cli", makefile)

    def test_core_import_boundaries_exclude_blackdog_product_code(self) -> None:
        self.assertEqual(self.core_import_boundary_violations(), [])

    def test_docs_freeze_the_vnext_machine_owned_contract(self) -> None:
        index_doc = (REPO_ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
        architecture = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        target_model = (REPO_ROOT / "docs" / "TARGET_MODEL.md").read_text(encoding="utf-8")
        product_spec = (REPO_ROOT / "docs" / "PRODUCT_SPEC.md").read_text(encoding="utf-8")
        cli_doc = (REPO_ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
        file_formats = (REPO_ROOT / "docs" / "FILE_FORMATS.md").read_text(encoding="utf-8")

        self.assertIn("[docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md)", index_doc)
        self.assertIn("supported human/agent stories", architecture)
        self.assertIn("[docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md)", target_model)
        self.assertIn("## V1 Stories", product_spec)
        self.assertIn("## Keep / Change / Combine / Defer / Remove", product_spec)
        self.assertIn("## Required Stats For Dogfooding", product_spec)
        self.assertIn("prompt receipt", product_spec)
        self.assertIn("worktree-backed", product_spec)
        self.assertIn("planning.json", architecture)
        self.assertIn("runtime.json", architecture)
        self.assertIn("agents mutate planning and runtime state", architecture)
        self.assertIn("prompt receipts", architecture)
        self.assertIn("Workset", target_model)
        self.assertIn("TaskAttemptRecord", target_model)
        self.assertIn("epic", target_model)
        self.assertIn("removed", target_model)
        self.assertIn("workset put", cli_doc)
        self.assertIn("worktree preflight", cli_doc)
        self.assertIn("worktree start", cli_doc)
        self.assertIn("worktree land", cli_doc)
        self.assertIn("worktree cleanup", cli_doc)
        self.assertIn("--prompt", cli_doc)
        self.assertIn("summary", cli_doc)
        self.assertIn("snapshot", cli_doc)
        self.assertIn("planning.json", file_formats)
        self.assertIn("runtime.json", file_formats)
        self.assertIn("attempts", file_formats)
        self.assertIn("prompt_receipt", file_formats)
        self.assertIn("worktree.start", file_formats)
        self.assertIn("backlog.md is not part of the vNext contract", file_formats)

    def test_doc_routing_still_points_at_required_repo_contract_docs(self) -> None:
        profile = tomllib.loads((REPO_ROOT / "blackdog.toml").read_text(encoding="utf-8"))
        routed = profile["taxonomy"]["doc_routing_defaults"]
        self.assertEqual(
            routed,
            [
                "AGENTS.md",
                "docs/INDEX.md",
                "docs/PRODUCT_SPEC.md",
                "docs/ARCHITECTURE.md",
                "docs/TARGET_MODEL.md",
                "docs/CLI.md",
                "docs/FILE_FORMATS.md",
            ],
        )
