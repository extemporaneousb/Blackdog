from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from blackdog.contract import legacy_managed_skill_relative_path, managed_skill_relative_path, managed_skill_name
from blackdog.repo_lifecycle import render_repo_skill
from blackdog_core.backlog import finish_task, start_task, upsert_workset
from blackdog_cli.main import main as blackdog_main
from blackdog_core.profile import PROJECT_STATUS_ACTIVE, PROJECT_STATUS_ARCHIVED, load_profile, render_default_profile
from blackdog_core.state import ValidationRecord, create_prompt_receipt
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class RepoLifecycleCliTests(CoreAuditTestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = blackdog_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def make_member_repo(self, parent: Path, name: str, *, project_name: str | None = None, status: str | None = None) -> Path:
        repo_root = parent / name
        repo_root.mkdir(parents=True)
        self.init_git_repo(repo_root)
        profile_text = render_default_profile(project_name or name)
        if status is not None:
            profile_text = profile_text.replace("[project]\n", f'[project]\nstatus = "{status}"\n')
        (repo_root / "blackdog.toml").write_text(profile_text, encoding="utf-8")
        subprocess.run(["git", "-C", str(repo_root), "add", "blackdog.toml"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(repo_root), "commit", "-m", "Add Blackdog profile"],
            check=True,
            capture_output=True,
            text=True,
        )
        return repo_root

    def profile_without_status(self, text: str) -> str:
        return "\n".join(line for line in text.splitlines() if not line.strip().startswith("status = ")) + "\n"

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

    def test_repo_scaffold_dry_run_reports_plan_without_mutating_target(self) -> None:
        (self.root / "AGENTS.md").write_text("# AGENTS\n\nUse repo rules.\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Exemplar\n", encoding="utf-8")
        docs_dir = self.root / "docs"
        docs_dir.mkdir()
        (docs_dir / "ARCHITECTURE.md").write_text("# Architecture\n", encoding="utf-8")
        target = self.root / "new-project"

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "scaffold",
            "--target-root",
            str(target),
            "--project-name",
            "Scaffold Demo",
            "--like",
            str(self.root),
            "--source-root",
            str(REPO_ROOT),
            "--dry-run",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["repo_scaffold"]

        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["project_name"], "Scaffold Demo")
        self.assertEqual(payload["target_root"], str(target.resolve()))
        self.assertIn("AGENTS.md", payload["planned_seed_files"])
        self.assertIn("docs/ARCHITECTURE.md", payload["planned_seed_files"])
        self.assertFalse(target.exists())
        self.assertTrue(any("would initialize a new git repo" in note for note in payload["notes"]))
        self.assertTrue(any("repo install" in note for note in payload["notes"]))

    def test_repo_scaffold_creates_blackdog_backed_project_from_exemplar(self) -> None:
        (self.root / "AGENTS.md").write_text(
            "# AGENTS\n\nRepo-owned exemplar rule.\n\n"
            "<!-- BLACKDOG MANAGED CONTRACT:BEGIN -->\nstale exemplar contract\n<!-- BLACKDOG MANAGED CONTRACT:END -->\n",
            encoding="utf-8",
        )
        docs_dir = self.root / "docs"
        docs_dir.mkdir()
        (docs_dir / "ARCHITECTURE.md").write_text("# Architecture\n", encoding="utf-8")
        target = self.root / "new-project"

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "scaffold",
            "--target-root",
            str(target),
            "--project-name",
            "Scaffold Demo",
            "--like",
            str(self.root),
            "--source-root",
            str(REPO_ROOT),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["repo_scaffold"]

        self.assertFalse(payload["dry_run"])
        self.assertTrue(payload["initialized_git"])
        self.assertEqual(payload["install_result"]["action"], "install")
        self.assertEqual(payload["install_result"]["source_root"], str(REPO_ROOT))
        self.assertTrue((target / ".git").exists())
        self.assertTrue((target / ".VE" / "bin" / "blackdog").is_file())
        profile = load_profile(target)
        skill_path = (target / managed_skill_relative_path(profile)).resolve()
        self.assertEqual(profile.project_name, "Scaffold Demo")
        self.assertIn("docs/ARCHITECTURE.md", profile.doc_routing_defaults)
        self.assertTrue(skill_path.is_file())
        self.assertNotIn("repo scaffold", skill_path.read_text(encoding="utf-8"))

        agents_text = (target / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Repo-owned exemplar rule.", agents_text)
        self.assertNotIn("stale exemplar contract", agents_text)
        self.assertIn("BLACKDOG MANAGED CONTRACT:BEGIN", agents_text)

        completed = subprocess.run(
            [str(target / ".VE" / "bin" / "blackdog"), "summary", "--project-root", str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Project: Scaffold Demo", completed.stdout)

    def test_blackdog_source_skill_rendering_keeps_scaffold_workflow(self) -> None:
        self.write_profile("Blackdog")
        (self.root / "pyproject.toml").write_text('[project]\nname = "blackdog"\n', encoding="utf-8")
        cli_dir = self.root / "src" / "blackdog_cli"
        cli_dir.mkdir(parents=True)
        (cli_dir / "main.py").write_text("# blackdog cli marker\n", encoding="utf-8")

        skill_text = render_repo_skill(load_profile(self.root))

        self.assertIn("$blackdog scaffold project <description>", skill_text)
        self.assertIn("repo scaffold", skill_text)
        self.assertIn("repo table", skill_text)
        self.assertIn("repo bind", skill_text)
        self.assertIn("repo archive", skill_text)
        self.assertIn("repo unarchive", skill_text)
        self.assertIn("repo unbind", skill_text)

    def test_repo_table_discovers_members_and_skips_worktrees(self) -> None:
        fleet_root = self.root / "fleet"
        self.make_member_repo(fleet_root, "active-repo", project_name="Active Repo")
        self.make_member_repo(fleet_root / ".worktrees", "hidden-repo", project_name="Hidden Repo")

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "table",
            "--root",
            str(fleet_root),
            "--no-codex",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["repo_table"]
        self.assertEqual(payload["columns"][0], "project_name")
        self.assertEqual([row["project_name"] for row in payload["rows"]], ["Active Repo"])
        self.assertIsNone(payload["rows"][0]["codex_sessions"])

    def test_repo_table_excludes_archived_repos_by_default(self) -> None:
        fleet_root = self.root / "fleet"
        self.make_member_repo(fleet_root, "active-repo", project_name="Active Repo")
        self.make_member_repo(
            fleet_root,
            "archived-repo",
            project_name="Archived Repo",
            status=PROJECT_STATUS_ARCHIVED,
        )

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "table",
            "--root",
            str(fleet_root),
            "--no-codex",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        rows = json.loads(stdout)["repo_table"]["rows"]
        self.assertEqual([row["project_name"] for row in rows], ["Active Repo"])

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "table",
            "--root",
            str(fleet_root),
            "--include-archived",
            "--no-codex",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        rows = json.loads(stdout)["repo_table"]["rows"]
        self.assertEqual({row["project_name"] for row in rows}, {"Active Repo", "Archived Repo"})

    def test_repo_table_reports_runtime_and_codex_columns(self) -> None:
        fleet_root = self.root / "fleet"
        empty_repo = self.make_member_repo(fleet_root, "empty-repo", project_name="Empty Repo")
        active_repo = self.make_member_repo(fleet_root, "attempt-repo", project_name="Attempt Repo")
        profile = load_profile(active_repo)
        upsert_workset(
            profile,
            {
                "id": "membership",
                "title": "Membership",
                "tasks": [
                    {"id": "MEM-1", "title": "Finish row", "intent": "record completed attempt"},
                    {"id": "MEM-2", "title": "Active row", "intent": "record active attempt"},
                ],
            },
        )
        completed_attempt = start_task(
            profile,
            workset_id="membership",
            task_id="MEM-1",
            actor="codex",
            branch="feature/membership-1",
            model="gpt-5.5",
            prompt_receipt=create_prompt_receipt("Finish membership row.", source="unit", mode="skill"),
        )
        finish_task(
            profile,
            workset_id="membership",
            task_id="MEM-1",
            attempt_id=completed_attempt.attempt_id,
            actor="codex",
            status="success",
            validations=(ValidationRecord(name="unit", status="passed"),),
            landed_commit="abc123",
        )
        start_task(
            profile,
            workset_id="membership",
            task_id="MEM-2",
            actor="codex",
            branch="feature/membership-2",
            prompt_receipt=create_prompt_receipt("Keep membership row active.", source="unit", mode="raw"),
        )

        codex_home = self.root / "codex-home"
        (codex_home / "sessions").mkdir(parents=True)
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
            exit_code, stdout, stderr = self.run_cli("repo", "table", "--root", str(fleet_root), "--json")
        self.assertEqual(exit_code, 0, stderr)
        rows = {row["project_name"]: row for row in json.loads(stdout)["repo_table"]["rows"]}

        self.assertEqual(rows["Empty Repo"]["worksets"], 0)
        self.assertEqual(rows["Empty Repo"]["attempts"], 0)
        self.assertEqual(rows["Attempt Repo"]["worksets"], 1)
        self.assertEqual(rows["Attempt Repo"]["tasks"], 2)
        self.assertEqual(rows["Attempt Repo"]["attempts"], 2)
        self.assertEqual(rows["Attempt Repo"]["active_attempts"], 1)
        self.assertEqual(rows["Attempt Repo"]["done"], 1)
        self.assertEqual(rows["Attempt Repo"]["codex_sessions"], 0)
        self.assertEqual(rows["Attempt Repo"]["prompt_modes"], "skill")
        self.assertEqual(rows["Attempt Repo"]["models"], "gpt-5.5")
        self.assertEqual(rows["Attempt Repo"]["status"], PROJECT_STATUS_ACTIVE)
        self.assertEqual(rows["Attempt Repo"]["project_root"], str(active_repo.resolve()))
        self.assertEqual(rows["Empty Repo"]["project_root"], str(empty_repo.resolve()))

    def test_repo_bind_creates_install_contract_with_bind_action(self) -> None:
        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "bind",
            "--project-root",
            str(self.root),
            "--project-name",
            "Bind Demo",
            "--source-root",
            str(REPO_ROOT),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["repo"]
        profile = load_profile(self.root)
        skill_path = (self.root / managed_skill_relative_path(profile)).resolve()

        self.assertEqual(payload["action"], "bind")
        self.assertEqual(profile.project_name, "Bind Demo")
        self.assertTrue((self.root / "blackdog.toml").is_file())
        self.assertTrue((self.root / "AGENTS.md").is_file())
        self.assertTrue(skill_path.is_file())
        self.assertTrue((self.root / ".VE" / "bin" / "blackdog").is_file())

    def test_repo_archive_and_unarchive_update_only_project_status(self) -> None:
        self.write_profile("Archive Demo")
        profile_path = self.root / "blackdog.toml"
        before = profile_path.read_text(encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "archive",
            "--project-root",
            str(self.root),
            "--reason",
            "finished",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        archive_payload = json.loads(stdout)["repo"]
        self.assertEqual(archive_payload["action"], "archive")
        self.assertEqual(archive_payload["previous_status"], PROJECT_STATUS_ACTIVE)
        self.assertEqual(archive_payload["status"], PROJECT_STATUS_ARCHIVED)
        self.assertIn("archive reason: finished", archive_payload["notes"])
        self.assertEqual(load_profile(self.root).status, PROJECT_STATUS_ARCHIVED)
        self.assertEqual(self.profile_without_status(profile_path.read_text(encoding="utf-8")), before)

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "unarchive",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        unarchive_payload = json.loads(stdout)["repo"]
        self.assertEqual(unarchive_payload["action"], "unarchive")
        self.assertEqual(unarchive_payload["previous_status"], PROJECT_STATUS_ARCHIVED)
        self.assertEqual(unarchive_payload["status"], PROJECT_STATUS_ACTIVE)
        self.assertEqual(load_profile(self.root).status, PROJECT_STATUS_ACTIVE)
        self.assertEqual(self.profile_without_status(profile_path.read_text(encoding="utf-8")), before)

    def test_repo_unbind_preview_and_confirm_only_touch_managed_paths(self) -> None:
        exit_code, _, stderr = self.run_cli(
            "repo",
            "bind",
            "--project-root",
            str(self.root),
            "--project-name",
            "Unbind Demo",
            "--source-root",
            str(REPO_ROOT),
        )
        self.assertEqual(exit_code, 0, stderr)
        profile = load_profile(self.root)
        skill_dir = (self.root / managed_skill_relative_path(profile)).parent.resolve()
        legacy_skill_dir = (self.root / legacy_managed_skill_relative_path()).parent.resolve()
        if legacy_skill_dir != skill_dir:
            legacy_skill_dir.mkdir(parents=True)
            (legacy_skill_dir / "SKILL.md").write_text(render_repo_skill(profile), encoding="utf-8")
        agents_path = self.root / "AGENTS.md"
        agents_path.write_text(
            "repo-owned before\n\n"
            + agents_path.read_text(encoding="utf-8")
            + "\nrepo-owned after\n",
            encoding="utf-8",
        )
        before_agents = agents_path.read_text(encoding="utf-8")
        unrelated_path = self.root / "README.md"
        unrelated_path.write_text("repo-owned dirty file\n", encoding="utf-8")
        history_path = self.root / ".blackdog" / "history.jsonl"
        history_path.parent.mkdir(parents=True)
        history_path.write_text("{}\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "unbind",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        preview = json.loads(stdout)["repo_unbind"]
        self.assertFalse(preview["confirmed"])
        self.assertIn(str(agents_path.resolve()), preview["planned_updates"])
        self.assertIn(str((self.root / "blackdog.toml").resolve()), preview["planned_removals"])
        self.assertIn(str(skill_dir), preview["planned_removals"])
        if legacy_skill_dir != skill_dir:
            self.assertIn(str(legacy_skill_dir), preview["planned_removals"])
        self.assertIn(str((self.root / ".VE" / "bin" / "blackdog").resolve()), preview["planned_removals"])
        self.assertIn("README.md", preview["unrelated_dirty_paths"])
        self.assertEqual(agents_path.read_text(encoding="utf-8"), before_agents)
        self.assertTrue((self.root / "blackdog.toml").exists())
        self.assertTrue(skill_dir.exists())

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "unbind",
            "--project-root",
            str(self.root),
            "--confirm",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        confirmed = json.loads(stdout)["repo_unbind"]
        self.assertTrue(confirmed["confirmed"])
        self.assertIn(str(agents_path.resolve()), confirmed["updated"])
        self.assertIn(str((self.root / "blackdog.toml").resolve()), confirmed["removed"])
        self.assertIn(str(skill_dir), confirmed["removed"])
        self.assertFalse((self.root / "blackdog.toml").exists())
        self.assertFalse(skill_dir.exists())
        if legacy_skill_dir != skill_dir:
            self.assertFalse(legacy_skill_dir.exists())
        self.assertFalse((self.root / ".VE" / "bin" / "blackdog").exists())
        self.assertTrue(unrelated_path.exists())
        self.assertEqual(unrelated_path.read_text(encoding="utf-8"), "repo-owned dirty file\n")
        self.assertTrue(history_path.exists())
        agents_text = agents_path.read_text(encoding="utf-8")
        self.assertIn("repo-owned before", agents_text)
        self.assertIn("repo-owned after", agents_text)
        self.assertNotIn("BLACKDOG MANAGED CONTRACT", agents_text)

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
        skill_metadata_path = skill_path.parent / "agents" / "openai.yaml"
        launcher_path = self.root / ".VE" / "bin" / "blackdog"

        self.assertEqual(payload["action"], "install")
        self.assertEqual(payload["source_mode"], "local-override")
        self.assertEqual(payload["source_root"], str(REPO_ROOT))
        self.assertIsNotNone(payload["handlers"])
        self.assertTrue(any("repo lifecycle changed managed worktree files" in note for note in payload["notes"]))
        self.assertTrue(profile_path.is_file())
        self.assertTrue(agents_path.is_file())
        self.assertTrue(skill_path.is_file())
        self.assertTrue(skill_metadata_path.is_file())
        self.assertTrue(launcher_path.is_file())
        self.assertIn("[[handlers]]", profile_path.read_text(encoding="utf-8"))
        self.assertEqual(profile.doc_routing_defaults, ("AGENTS.md",))

        agents_text = agents_path.read_text(encoding="utf-8")
        self.assertIn("BLACKDOG MANAGED CONTRACT:BEGIN", agents_text)
        self.assertIn("worktree preflight", agents_text)
        self.assertIn("primary worktree: yes", agents_text)
        self.assertIn("Do not launch an external browser", agents_text)
        self.assertIn("repo install`, `repo update`, or `repo refresh", agents_text)
        self.assertIn("re-check branch and dirty state", agents_text)

        skill_text = skill_path.read_text(encoding="utf-8")
        self.assertIn(f"name: {managed_skill_name(profile)}", skill_text)
        self.assertIn("Lifecycle Demo", skill_text)
        self.assertIn("repo install", skill_text)
        self.assertIn("git status --short", skill_text)
        self.assertIn("do <task-description>", skill_text)
        self.assertNotIn("PM-mode", skill_text)
        self.assertNotIn("workset put", skill_text)
        self.assertNotIn("next --workset", skill_text)
        self.assertIn("--prompt-mode skill", skill_text)
        self.assertIn("Operator Guardrails", skill_text)
        self.assertIn("Do not launch an external browser", skill_text)
        self.assertNotIn("Shipped Workflow Families", skill_text)
        self.assertIn("AGENTS.md", skill_text)
        self.assertNotIn("docs/INDEX.md", skill_text)
        skill_metadata = skill_metadata_path.read_text(encoding="utf-8")
        self.assertIn("display_name: \"Lifecycle Demo Development\"", skill_metadata)
        self.assertIn("default_prompt: \"Use $lifecycle-demo do <task-description> for repo work.\"", skill_metadata)
        self.assertNotIn("PM-mode", skill_metadata)
        self.assertFalse((skill_path.parent / "references").exists())

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
        self.assertFalse(any("repo lifecycle changed managed worktree files" in note for note in payload["notes"]))
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
        skill_metadata_path = skill_path.parent / "agents" / "openai.yaml"
        agents_path.write_text(
            "# AGENTS\n\nRepo-specific rule.\n\n"
            "<!-- BLACKDOG MANAGED CONTRACT:BEGIN -->\nold contract\n<!-- BLACKDOG MANAGED CONTRACT:END -->\n",
            encoding="utf-8",
        )
        skill_path.write_text("stale skill\n", encoding="utf-8")
        skill_metadata_path.parent.mkdir(parents=True, exist_ok=True)
        skill_metadata_path.write_text("stale metadata\n", encoding="utf-8")
        stale_reference = skill_path.parent / "references" / "task-shaping.md"
        stale_reference.parent.mkdir(parents=True, exist_ok=True)
        stale_reference.write_text("stale reference\n", encoding="utf-8")
        stale_marker = skill_path.parent / ".blackdog-managed.json"
        stale_marker.write_text("{}\n", encoding="utf-8")
        legacy_skill_path = (self.root / legacy_managed_skill_relative_path()).resolve()
        if legacy_skill_path != skill_path:
            legacy_skill_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_skill_path.write_text("legacy skill\n", encoding="utf-8")
            (legacy_skill_path.parent / "agents").mkdir(parents=True, exist_ok=True)
            (legacy_skill_path.parent / "agents" / "openai.yaml").write_text("legacy metadata\n", encoding="utf-8")
        old_prefixed_skill_dir = self.root / ".codex" / "skills" / f"blackdog-{managed_skill_name(profile)}"
        if old_prefixed_skill_dir != skill_path.parent:
            old_prefixed_skill_dir.mkdir(parents=True, exist_ok=True)
            (old_prefixed_skill_dir / ".blackdog-managed.json").write_text("{}\n", encoding="utf-8")
            (old_prefixed_skill_dir / "SKILL.md").write_text("old prefixed skill\n", encoding="utf-8")
        old_supervisor_skill_dir = self.root / ".codex" / "skills" / f"{managed_skill_name(profile)}-supervisor"
        old_supervisor_skill_dir.mkdir(parents=True, exist_ok=True)
        (old_supervisor_skill_dir / "SKILL.md").write_text("old supervisor skill\n", encoding="utf-8")
        legacy_backlog = self.root / ".git" / "blackdog" / "backlog.md"
        legacy_backlog.parent.mkdir(parents=True, exist_ok=True)
        legacy_backlog.write_text("legacy backlog\n", encoding="utf-8")
        removed_orchestration_dir = self.root / ".git" / "blackdog" / "supervisor-runs"
        removed_orchestration_dir.mkdir(parents=True, exist_ok=True)
        (removed_orchestration_dir / "run.json").write_text("{\"status\": \"legacy\"}\n", encoding="utf-8")

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
        self.assertTrue(any("repo lifecycle changed managed worktree files" in note for note in payload["notes"]))
        self.assertIn(str(legacy_backlog.resolve()), payload["removed"])
        self.assertIn(str(removed_orchestration_dir.resolve()), payload["removed"])
        if legacy_skill_path != skill_path:
            self.assertIn(str(legacy_skill_path.parent), payload["removed"])
            self.assertFalse(legacy_skill_path.exists())
        if old_prefixed_skill_dir != skill_path.parent:
            self.assertIn(str(old_prefixed_skill_dir.resolve()), payload["removed"])
            self.assertFalse(old_prefixed_skill_dir.exists())
        self.assertIn(str(old_supervisor_skill_dir.resolve()), payload["removed"])
        self.assertIn(str(stale_reference.resolve()), payload["removed"])
        self.assertIn(str(stale_marker.resolve()), payload["removed"])
        self.assertFalse(legacy_backlog.exists())
        self.assertFalse(removed_orchestration_dir.exists())
        self.assertFalse(stale_reference.exists())
        self.assertFalse(stale_marker.exists())
        self.assertFalse(old_supervisor_skill_dir.exists())
        self.assertIsNotNone(payload["handlers"])
        agents_text = agents_path.read_text(encoding="utf-8")
        self.assertIn("Repo-specific rule.", agents_text)
        self.assertNotIn("old contract", agents_text)
        self.assertIn("docs/CUSTOM.md", agents_text)
        skill_text = skill_path.read_text(encoding="utf-8")
        self.assertNotIn("stale skill", skill_text)
        self.assertIn("docs/CUSTOM.md", skill_text)
        self.assertIn("repo refresh", skill_text)
        self.assertIn("task cancel", skill_text)
        skill_metadata = skill_metadata_path.read_text(encoding="utf-8")
        self.assertNotIn("stale metadata", skill_metadata)
        self.assertIn(f"default_prompt: \"Use ${managed_skill_name(profile)} do <task-description>", skill_metadata)

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
