from __future__ import annotations

import tomllib

from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class CoreContractTests(CoreAuditTestCase):
    def test_pyproject_and_makefile_keep_the_shipped_cli_surface(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["scripts"], {"blackdog": "blackdog_cli.main:main"})
        self.assertEqual(
            pyproject["project"]["description"],
            "Repo-scoped workset runtime for AI-assisted local development",
        )
        self.assertNotIn("blackdog", pyproject.get("tool", {}))
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("python3 -m unittest discover", makefile)
        self.assertNotIn("coverage:", makefile)
        self.assertNotIn("test-emacs:", makefile)

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
        self.assertIn("claims attach to both worksets and tasks", product_spec)
        self.assertIn("direct_wtam", product_spec)
        self.assertIn("workset_manager", product_spec)
        self.assertIn("repo lifecycle workflows", product_spec)
        self.assertIn("install or update Blackdog in a repo", product_spec)
        self.assertIn("worksets, tasks, claims, or attempts", product_spec)
        self.assertIn("planning.json", architecture)
        self.assertIn("runtime.json", architecture)
        self.assertIn("agents mutate planning and runtime state", architecture)
        self.assertIn("prompt receipts", architecture)
        self.assertIn("workset claims", architecture)
        self.assertIn("repo lifecycle workflows", architecture)
        self.assertIn("repo install", architecture)
        self.assertIn("repo update", architecture)
        self.assertIn("repo refresh", architecture)
        self.assertIn("Workset", target_model)
        self.assertIn("TaskAttemptRecord", target_model)
        self.assertIn("WorksetClaimRecord", target_model)
        self.assertIn("TaskClaimRecord", target_model)
        self.assertIn("epic", target_model)
        self.assertIn("removed", target_model)
        self.assertIn("repo install", cli_doc)
        self.assertIn("repo update", cli_doc)
        self.assertIn("repo refresh", cli_doc)
        self.assertIn("prompt preview", cli_doc)
        self.assertIn("prompt tune", cli_doc)
        self.assertIn("attempts summary", cli_doc)
        self.assertIn("attempts table", cli_doc)
        self.assertIn("workset put", cli_doc)
        self.assertIn("worktree preflight", cli_doc)
        self.assertIn("worktree preview", cli_doc)
        self.assertIn("worktree start", cli_doc)
        self.assertIn("worktree land", cli_doc)
        self.assertIn("worktree cleanup", cli_doc)
        self.assertIn("--prompt", cli_doc)
        self.assertIn("--show-prompt", cli_doc)
        self.assertIn("summary", cli_doc)
        self.assertIn("snapshot", cli_doc)
        self.assertNotIn("analysis-only workflow", cli_doc)
        self.assertIn("Install/update/refresh/tune", cli_doc)
        self.assertIn("planning.json", file_formats)
        self.assertIn("runtime.json", file_formats)
        self.assertIn("attempts", file_formats)
        self.assertIn("workset_claim", file_formats)
        self.assertIn("task_claims", file_formats)
        self.assertIn("execution_model", file_formats)
        self.assertIn("prompt_receipt", file_formats)
        self.assertIn("worktree.start", file_formats)
        self.assertIn("backlog.md is not part of the vNext contract", file_formats)

    def test_repo_prunes_legacy_product_modules_and_docs(self) -> None:
        removed_paths = [
            "src/blackdog/architecture.py",
            "src/blackdog/board.py",
            "src/blackdog/conversations.py",
            "src/blackdog/execution_context.py",
            "src/blackdog/installs.py",
            "src/blackdog/scaffold.py",
            "src/blackdog/supervisor.py",
            "src/blackdog/supervisor_policy.py",
            "src/blackdog/tuning.py",
            "src/blackdog/ui.css",
            "src/blackdog/worktree.py",
            "docs/ACCEPTANCE.md",
            "docs/BOUNDARIES.md",
            "docs/EMACS.md",
            "docs/EXTRACTION_AUDIT.md",
            "docs/INTEGRATION.md",
            "docs/MIGRATION.md",
            "docs/MODULE_INVENTORY.md",
            "docs/OWNERSHIP_INVENTORY.md",
            "docs/RELEASE_NOTES.md",
            "docs/architecture-diagrams.html",
        ]
        for relative_path in removed_paths:
            with self.subTest(path=relative_path):
                self.assertFalse((REPO_ROOT / relative_path).exists(), f"{relative_path} should be removed")
        self.assertFalse(
            any(path.is_file() for path in (REPO_ROOT / "extensions").rglob("*")),
            "legacy editor surfaces under extensions/ should be removed",
        )

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
