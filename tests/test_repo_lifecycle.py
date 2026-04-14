from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess

from blackdog.contract import legacy_managed_skill_relative_path, managed_skill_relative_path, managed_skill_name
from blackdog_cli.main import main as blackdog_main
from blackdog_core.profile import load_profile
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class RepoLifecycleCliTests(CoreAuditTestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = blackdog_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_repo_analyze_reports_unconverted_repo_and_conversion_plan(self) -> None:
        docs_dir = self.root / "docs"
        docs_dir.mkdir()
        (docs_dir / "AGENT_START.md").write_text("start here\n", encoding="utf-8")
        (docs_dir / "INDEX.md").write_text("index\n", encoding="utf-8")
        skill_dir = self.root / ".codex" / "skills" / "cmg-platform"
        (skill_dir / "agents").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("custom repo skill\n", encoding="utf-8")
        (skill_dir / "agents" / "openai.yaml").write_text(
            "interface:\n  default_prompt: \"Use $cmg-platform.\"\n",
            encoding="utf-8",
        )

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "analyze",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["repo_analysis"]

        self.assertEqual(payload["action"], "analyze")
        self.assertEqual(payload["conversion_status"], "not-installed")
        self.assertFalse(payload["profile_exists"])
        self.assertEqual(payload["suggested_doc_routing"], ["AGENTS.md", "docs/AGENT_START.md", "docs/INDEX.md"])
        finding_codes = {item["code"] for item in payload["findings"]}
        self.assertIn("missing-blackdog-profile", finding_codes)
        self.assertIn("custom-skills-bypass-blackdog", finding_codes)
        install_commands = [step["command"] for step in payload["proposed_steps"] if step["command"]]
        self.assertTrue(any("repo install" in command for command in install_commands))
        self.assertTrue(any("--project-root" in command for command in install_commands))

    def test_repo_analyze_reports_partial_conversion_and_ambiguity_sources(self) -> None:
        docs_dir = self.root / "docs"
        docs_dir.mkdir()
        (docs_dir / "AGENT_START.md").write_text("start here\n", encoding="utf-8")

        exit_code, _, stderr = self.run_cli(
            "repo",
            "install",
            "--project-root",
            str(self.root),
            "--source-root",
            str(REPO_ROOT),
        )
        self.assertEqual(exit_code, 0, stderr)

        (docs_dir / "AGENT_WORKFLOW.md").write_text("workflow here\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# AGENTS\n\nRepo-specific rule only.\n", encoding="utf-8")
        skill_dir = self.root / ".codex" / "skills" / "cmg-platform"
        (skill_dir / "agents").mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("custom repo skill\n", encoding="utf-8")
        (skill_dir / "agents" / "openai.yaml").write_text(
            "interface:\n  default_prompt: \"Use $cmg-platform.\"\n",
            encoding="utf-8",
        )

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "analyze",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["repo_analysis"]

        self.assertEqual(payload["conversion_status"], "partial")
        finding_codes = {item["code"] for item in payload["findings"]}
        self.assertIn("missing-managed-agents-contract", finding_codes)
        self.assertIn("unrouted-agent-entrypoints", finding_codes)
        self.assertIn("custom-skills-bypass-blackdog", finding_codes)
        self.assertEqual(payload["current_doc_routing"], ["AGENTS.md", "docs/AGENT_START.md"])
        self.assertEqual(
            payload["suggested_doc_routing"],
            ["AGENTS.md", "docs/AGENT_START.md", "docs/AGENT_WORKFLOW.md"],
        )

    def test_repo_install_bootstraps_profile_skill_and_launcher(self) -> None:
        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "install",
            "--project-root",
            str(self.root),
            "--project-name",
            "Lifecycle Demo",
            "--source-root",
            str(REPO_ROOT),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["repo"]

        profile_path = self.root / "blackdog.toml"
        profile = load_profile(self.root)
        agents_path = self.root / "AGENTS.md"
        skill_path = (self.root / managed_skill_relative_path(profile)).resolve()
        launcher_path = self.root / ".VE" / "bin" / "blackdog"

        self.assertEqual(payload["action"], "install")
        self.assertEqual(payload["source_mode"], "local-override")
        self.assertEqual(payload["source_root"], str(REPO_ROOT))
        self.assertIsNotNone(payload["handlers"])
        self.assertTrue(profile_path.is_file())
        self.assertTrue(agents_path.is_file())
        self.assertTrue(skill_path.is_file())
        self.assertTrue(launcher_path.is_file())
        self.assertIn("[[handlers]]", profile_path.read_text(encoding="utf-8"))
        self.assertEqual(profile.doc_routing_defaults, ("AGENTS.md",))

        agents_text = agents_path.read_text(encoding="utf-8")
        self.assertIn("BLACKDOG MANAGED CONTRACT:BEGIN", agents_text)
        self.assertIn("worktree preflight", agents_text)
        self.assertIn("primary worktree: yes", agents_text)

        skill_text = skill_path.read_text(encoding="utf-8")
        self.assertIn(f"name: {managed_skill_name(profile)}", skill_text)
        self.assertIn("Lifecycle Demo", skill_text)
        self.assertIn("repo install", skill_text)
        self.assertIn("AGENTS.md", skill_text)
        self.assertNotIn("docs/INDEX.md", skill_text)

        launcher_text = launcher_path.read_text(encoding="utf-8")
        self.assertIn("blackdog_cli", launcher_text)
        self.assertIn(str((REPO_ROOT / "src").resolve()), launcher_text)
        self.assertIn(str(self.root / ".VE" / "bin" / "python"), launcher_text)
        self.assertTrue(any(action["action"] == "create-root-venv" for action in payload["handlers"]["actions"]))

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
            "--source-root",
            str(REPO_ROOT),
        )
        self.assertEqual(exit_code, 0, stderr)

        profile = load_profile(self.root)
        skill_path = (self.root / managed_skill_relative_path(profile)).resolve()
        launcher_path = self.root / ".VE" / "bin" / "blackdog"
        skill_path.write_text("custom skill\n", encoding="utf-8")
        launcher_path.write_text("#!/bin/sh\necho broken\n", encoding="utf-8")
        launcher_path.chmod(0o755)

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "update",
            "--project-root",
            str(self.root),
            "--source-root",
            str(REPO_ROOT),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["repo"]

        self.assertEqual(payload["action"], "update")
        self.assertTrue(any(action["action"] == "write-blackdog-launcher" for action in payload["handlers"]["actions"]))
        self.assertEqual(skill_path.read_text(encoding="utf-8"), "custom skill\n")
        self.assertIn("blackdog_cli", launcher_path.read_text(encoding="utf-8"))
        self.assertIn(str((REPO_ROOT / "src").resolve()), launcher_path.read_text(encoding="utf-8"))
        self.assertIn(str(self.root / ".VE" / "bin" / "python"), launcher_path.read_text(encoding="utf-8"))

    def test_repo_refresh_regenerates_skill_from_profile_contract(self) -> None:
        exit_code, _, stderr = self.run_cli(
            "repo",
            "install",
            "--project-root",
            str(self.root),
            "--source-root",
            str(REPO_ROOT),
        )
        self.assertEqual(exit_code, 0, stderr)

        profile_path = self.root / "blackdog.toml"
        profile_text = profile_path.read_text(encoding="utf-8")
        profile_text = profile_text.replace(
            'doc_routing_defaults = ["AGENTS.md"]',
            'doc_routing_defaults = ["AGENTS.md", "docs/CUSTOM.md"]',
        )
        profile_path.write_text(profile_text, encoding="utf-8")

        profile = load_profile(self.root)
        agents_path = self.root / "AGENTS.md"
        skill_path = (self.root / managed_skill_relative_path(profile)).resolve()
        agents_path.write_text(
            "# AGENTS\n\nRepo-specific rule.\n\n"
            "<!-- BLACKDOG MANAGED CONTRACT:BEGIN -->\nold contract\n<!-- BLACKDOG MANAGED CONTRACT:END -->\n",
            encoding="utf-8",
        )
        skill_path.write_text("stale skill\n", encoding="utf-8")
        legacy_skill_path = (self.root / legacy_managed_skill_relative_path()).resolve()
        if legacy_skill_path != skill_path:
            legacy_skill_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_skill_path.write_text("legacy skill\n", encoding="utf-8")
        legacy_backlog = self.root / ".git" / "blackdog" / "backlog.md"
        legacy_backlog.parent.mkdir(parents=True, exist_ok=True)
        legacy_backlog.write_text("legacy backlog\n", encoding="utf-8")

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
        self.assertIn(str(legacy_backlog.resolve()), payload["removed"])
        if legacy_skill_path != skill_path:
            self.assertIn(str(legacy_skill_path), payload["removed"])
            self.assertFalse(legacy_skill_path.exists())
        self.assertFalse(legacy_backlog.exists())
        self.assertIsNotNone(payload["handlers"])
        agents_text = agents_path.read_text(encoding="utf-8")
        self.assertIn("Repo-specific rule.", agents_text)
        self.assertNotIn("old contract", agents_text)
        self.assertIn("docs/CUSTOM.md", agents_text)
        skill_text = skill_path.read_text(encoding="utf-8")
        self.assertNotIn("stale skill", skill_text)
        self.assertIn("docs/CUSTOM.md", skill_text)
        self.assertIn("repo refresh", skill_text)

    def test_prompt_preview_and_tune_use_repo_contract_inputs(self) -> None:
        exit_code, _, stderr = self.run_cli(
            "repo",
            "install",
            "--project-root",
            str(self.root),
            "--source-root",
            str(REPO_ROOT),
        )
        self.assertEqual(exit_code, 0, stderr)

        (self.root / "AGENTS.md").write_text("repo contract\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "prompt",
            "preview",
            "--project-root",
            str(self.root),
            "--prompt",
            "Round out repo lifecycle behavior.",
            "--show-prompt",
            "--expand-skill-text",
            "--expand-contract",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        preview = json.loads(stdout)["prompt_preview"]
        self.assertEqual(preview["workflow_family"], "repo-lifecycle")
        self.assertEqual(preview["prompt_text"], "Round out repo lifecycle behavior.")
        self.assertIn("blackdog repo install", preview["composed_prompt"])
        self.assertTrue(
            any(item["kind"] == "skill" and item["text"] is not None for item in preview["contract_documents"])
        )
        self.assertTrue(
            any(item["path"] == str((self.root / "AGENTS.md").resolve()) and item["text"] == "repo contract\n" for item in preview["contract_documents"])
        )

        exit_code, stdout, stderr = self.run_cli(
            "prompt",
            "tune",
            "--project-root",
            str(self.root),
            "--prompt",
            "Round out repo lifecycle behavior.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        tuned = json.loads(stdout)["prompt_tune"]
        self.assertEqual(tuned["workflow_family"], "repo-lifecycle")
        self.assertIn("Round out repo lifecycle behavior.", tuned["tuned_prompt"])
        self.assertIn("blackdog repo refresh", tuned["tuned_prompt"])
