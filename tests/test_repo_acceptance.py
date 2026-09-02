from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from blackdog.contract import managed_skill_name, managed_skill_relative_path
from blackdog.landing import load_landing_transaction
from blackdog_cli.main import main as blackdog_main
from blackdog_core.profile import load_profile
from blackdog_core.state import (
    ATTEMPT_STATUS_ABANDONED,
    ATTEMPT_STATUS_SUCCESS,
    RUNTIME_SCHEMA_VERSION,
    RUNTIME_STORE_VERSION,
    TASK_STATUS_CANCELED,
    TASK_STATUS_DONE,
    ValidationRecord,
    load_events,
    load_runtime_state,
    task_record,
)
from blackdog_core.tasks import create_task, finish_task, set_task_runtime_status, start_task
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

    def test_root_read_commands_accept_the_runtime_v4_store(self) -> None:
        self.write_profile(project_name="Runtime V4")
        profile = load_profile(self.root)
        cleaned_path = self.root / ".cleaned-terminal-task"
        completed_task = create_task(profile, title="Cleaned terminal task")
        completed_attempt = start_task(
            profile,
            task_id=completed_task.task_id,
            actor="codex",
            workspace_mode="git-worktree",
            worktree_role="task",
            worktree_path=str(cleaned_path),
            branch="agent/cleaned-terminal-task",
            target_branch="main",
        )
        finish_task(
            profile,
            task_id=completed_task.task_id,
            attempt_id=completed_attempt.attempt_id,
            actor="codex",
            status=ATTEMPT_STATUS_SUCCESS,
            summary="Completed runtime-v4 acceptance",
            validations=(ValidationRecord("focused", "passed"),),
        )
        canceled_task = create_task(profile, title="Canceled task")
        set_task_runtime_status(
            profile,
            task_id=canceled_task.task_id,
            actor="codex",
            status=TASK_STATUS_CANCELED,
            summary="Canceled runtime-v4 acceptance",
        )
        planned_task = create_task(profile, title="Planned task")
        active_path = self.root / ".active-task"
        active_path.mkdir()
        active_task = create_task(profile, title="Active task")
        active_attempt = start_task(
            profile,
            task_id=active_task.task_id,
            actor="codex",
            workspace_mode="git-worktree",
            worktree_role="task",
            worktree_path=str(active_path),
            branch="agent/active-task",
            target_branch="main",
        )
        retained_path = self.root / ".retained-terminal-task"
        retained_path.mkdir()
        retained_task = create_task(profile, title="Retained terminal task")
        retained_attempt = start_task(
            profile,
            task_id=retained_task.task_id,
            actor="codex",
            workspace_mode="git-worktree",
            worktree_role="task",
            worktree_path=str(retained_path),
            branch="agent/retained-terminal-task",
            target_branch="main",
        )
        finish_task(
            profile,
            task_id=retained_task.task_id,
            attempt_id=retained_attempt.attempt_id,
            actor="codex",
            status=ATTEMPT_STATUS_ABANDONED,
            summary="Retained runtime-v4 acceptance workspace",
            validations=(ValidationRecord("focused", "passed"),),
        )

        runtime_payload = json.loads(profile.paths.runtime_file.read_text(encoding="utf-8"))
        self.assertEqual(runtime_payload["schema_version"], RUNTIME_SCHEMA_VERSION)
        self.assertEqual(runtime_payload["store_version"], RUNTIME_STORE_VERSION)

        exit_code, stdout, stderr = self.run_cli(
            "summary", "--project-root", str(self.root), "--json"
        )
        self.assertEqual(exit_code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["counts"]["tasks"], 5)
        self.assertEqual(summary["counts"]["canceled"], 2)
        self.assertEqual(summary["counts"]["planned"], 1)
        self.assertEqual(summary["counts"]["in_progress"], 1)
        self.assertEqual(
            {task["task_id"] for task in summary["tasks"]},
            {
                completed_task.task_id,
                canceled_task.task_id,
                planned_task.task_id,
                active_task.task_id,
                retained_task.task_id,
            },
        )

        exit_code, stdout, stderr = self.run_cli(
            "summary", "--project-root", str(self.root)
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertIn(f"[CANCELED] {canceled_task.task_id} Canceled task", stdout)

        exit_code, stdout, stderr = self.run_cli(
            "snapshot", "--project-root", str(self.root)
        )
        self.assertEqual(exit_code, 0, stderr)
        snapshot = json.loads(stdout)
        self.assertEqual(snapshot["counts"]["tasks"], 5)
        self.assertEqual(len(snapshot["attempts"]), 3)

        exit_code, stdout, stderr = self.run_cli(
            "attempts", "summary", "--project-root", str(self.root), "--json"
        )
        self.assertEqual(exit_code, 0, stderr)
        attempts_summary = json.loads(stdout)
        self.assertEqual(attempts_summary["completed_attempts"], 2)
        self.assertEqual(
            attempts_summary["status_counts"],
            {"abandoned": 1, "success": 1},
        )

        exit_code, stdout, stderr = self.run_cli(
            "attempts", "table", "--project-root", str(self.root), "--json"
        )
        self.assertEqual(exit_code, 0, stderr)
        attempts_table = json.loads(stdout)
        self.assertEqual(len(attempts_table["rows"]), 2)
        self.assertEqual(
            {row["attempt_id"] for row in attempts_table["rows"]},
            {completed_attempt.attempt_id, retained_attempt.attempt_id},
        )
        self.assertNotIn(
            active_attempt.attempt_id,
            {row["attempt_id"] for row in attempts_table["rows"]},
        )

        with patch("blackdog.codex_sessions.collect_codex_turns", return_value=()):
            exit_code, stdout, stderr = self.run_cli(
                "codex", "coverage", "--project-root", str(self.root), "--json"
            )
            self.assertEqual(exit_code, 0, stderr)
            coverage = json.loads(stdout)["codex_coverage"]
            self.assertEqual(coverage["counts"]["blackdog_attempts"], 3)

            exit_code, stdout, stderr = self.run_cli(
                "codex", "history", "--project-root", str(self.root), "--jsonl"
            )
        self.assertEqual(exit_code, 0, stderr)
        history_rows = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(
            {row["attempt_id"] for row in history_rows},
            {
                completed_attempt.attempt_id,
                active_attempt.attempt_id,
                retained_attempt.attempt_id,
            },
        )

        exit_code, stdout, stderr = self.run_cli(
            "worktree", "table", "--project-root", str(self.root), "--json"
        )
        self.assertEqual(exit_code, 0, stderr)
        worktree_table = json.loads(stdout)["worktree_table"]
        self.assertEqual(
            {row["attempt_id"] for row in worktree_table["rows"]},
            {active_attempt.attempt_id, retained_attempt.attempt_id},
        )
        self.assertEqual(
            worktree_table["counts"],
            {"active_attempts": 1, "attempts": 2, "retained_worktrees": 1, "tasks": 2},
        )

        exit_code, stdout, stderr = self.run_cli(
            "repo",
            "table",
            "--project-root",
            str(self.root),
            "--no-codex",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        repo_row = json.loads(stdout)["repo_table"]["rows"][0]
        self.assertEqual(repo_row["tasks_total"], 5)
        self.assertEqual(repo_row["current_ready_tasks"], 1)
        self.assertEqual(repo_row["current_active_attempts"], 1)
        self.assertEqual(repo_row["attempts_total"], 3)

        with patch("blackdog.stats.collect_codex_turns", return_value=()):
            exit_code, stdout, stderr = self.run_cli(
                "stats", "--project-root", str(self.root), "--json"
            )
        self.assertEqual(exit_code, 0, stderr)
        stats = json.loads(stdout)["stats"]["summary"]
        self.assertEqual(stats["tasks_total"], 5)
        self.assertEqual(stats["attempts_total"], 3)

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
        retired_contract_terms = (
            "retry_stale_" + "claim_release_finalization",
            "work" + "set",
            "back" + "log",
            "super" + "visor",
        )
        for term in retired_contract_terms:
            self.assertNotIn(term, agents_text.lower())

        skill_text = skill_path.read_text(encoding="utf-8")
        self.assertIn(f"name: {managed_skill_name(profile)}", skill_text)
        self.assertIn("## Workflow", skill_text)
        self.assertIn("a concise goal, relevant context, constraints, and done condition", skill_text)
        self.assertIn("`AGENTS.md` owns the detailed workflow contract", skill_text)
        self.assertNotIn("Docs To Review", skill_text)
        self.assertLessEqual(len(skill_text.splitlines()), 18)
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
        refreshed_agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        for term in retired_contract_terms:
            self.assertNotIn(term, refreshed_agents.lower())

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
        self.assertTrue(preflight_payload["workspace_has_local_blackdog"])

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
                    "--execution-prompt",
                    "Implement linked target branch behavior.",
                    "--request",
                    "Implement linked target branch behavior.",
                    "--json",
                ],
                cwd=str(linked_worktree),
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(begin.stdout)["task"]
            task_worktree = Path(payload["worktree_path"])
            self.assertEqual(payload["target_branch"], "feature/acceptance")
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
                    "--validation",
                    "acceptance=passed",
                    "--cleanup",
                    "--json",
                ],
                cwd=str(task_worktree),
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(close.stdout)["closure"]["status"], "canceled")
        finally:
            if task_worktree is not None and task_worktree.exists():
                subprocess.run(["git", "-C", str(self.root), "worktree", "remove", "--force", str(task_worktree)], check=False)
            if linked_worktree.exists():
                subprocess.run(["git", "-C", str(self.root), "worktree", "remove", "--force", str(linked_worktree)], check=False)
            subprocess.run(["git", "-C", str(self.root), "branch", "-D", "feature/acceptance"], check=False, capture_output=True, text=True)
            linked_parent.cleanup()

    def test_default_task_land_from_task_worktree_returns_success_after_cleanup(self) -> None:
        self.install_with_local_source()
        launcher = self.root / ".VE" / "bin" / "blackdog"
        subprocess.run(
            ["git", "-C", str(self.root), "add", "blackdog.toml", "AGENTS.md", ".codex"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Install Blackdog fixture"],
            check=True,
            capture_output=True,
            text=True,
        )
        begin = subprocess.run(
            [
                str(launcher),
                "task",
                "begin",
                "--project-root",
                str(self.root),
                "--actor",
                "codex",
                "--execution-prompt",
                "Implement the default-cleanup landing fixture.",
                "--request",
                "Implement the default-cleanup landing fixture.",
                "--json",
            ],
            cwd=str(self.root),
            check=True,
            capture_output=True,
            text=True,
        )
        begun = json.loads(begin.stdout)["task"]
        task_id = str(begun["task_id"])
        attempt_id = str(begun["attempt_id"])
        task_branch = str(begun["branch"])
        task_worktree = Path(str(begun["worktree_path"]))
        task_launcher = task_worktree / ".VE" / "bin" / "blackdog"
        task_context_show = subprocess.run(
            [
                str(task_launcher),
                "task",
                "show",
                "--project-root",
                str(task_worktree),
                "--json",
            ],
            cwd=str(task_worktree),
            check=True,
            capture_output=True,
            text=True,
        )
        shown_before = json.loads(task_context_show.stdout)["task_show"]
        self.assertEqual(shown_before["task_id"], task_id)
        self.assertTrue(shown_before["worktree_exists"])
        (task_worktree / "landed.txt").write_text(
            "default cleanup landing\n",
            encoding="utf-8",
        )

        completed = subprocess.run(
            [
                str(task_launcher),
                "task",
                "land",
                "--project-root",
                str(task_worktree),
                "--summary",
                "Complete default cleanup landing",
                "--validation",
                "acceptance=passed",
                "--json",
            ],
            cwd=str(task_worktree),
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            completed.returncode,
            0,
            f"stderr:\n{completed.stderr}\nstdout:\n{completed.stdout}",
        )
        landing = json.loads(completed.stdout)["landing"]
        self.assertEqual(landing["operation_status"], "succeeded")
        self.assertEqual(landing["mutation_phase"], "landing_complete")
        self.assertEqual(landing["next_action"]["action_id"], "landing_complete")
        self.assertEqual(landing["next_action"]["kind"], "complete")
        landed_commit = str(landing["landed_commit"])
        self.assertEqual(self.git_output("rev-parse", "main"), landed_commit)
        self.assertEqual(
            self.git_output("show", f"{landed_commit}:landed.txt"),
            "default cleanup landing",
        )

        profile = load_profile(self.root)
        task = task_record(load_runtime_state(profile.paths), task_id)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.status, TASK_STATUS_DONE)
        attempt = next(item for item in task.attempts if item.attempt_id == attempt_id)
        self.assertEqual(attempt.status, ATTEMPT_STATUS_SUCCESS)
        self.assertEqual(attempt.landed_commit, landed_commit)
        event_types = [str(event["type"]) for event in load_events(profile.paths.events_file)]
        self.assertIn("worktree.land", event_types)
        self.assertIn("task.cleanup", event_types)
        transaction = load_landing_transaction(
            profile,
            task_id=task_id,
            attempt_id=attempt_id,
        )
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertTrue(transaction.complete)
        temporary_worktree = Path(transaction.intent.temporary_worktree_path)
        temporary_branch = f"blackdog/land-{transaction.intent.transaction_id[:16]}"
        self.assertFalse(temporary_worktree.exists())
        self.assertNotIn(
            f"refs/heads/{temporary_branch}",
            self.git_output("for-each-ref", "--format=%(refname)", "refs/heads"),
        )
        self.assertFalse(task_worktree.exists())
        self.assertNotIn(str(task_worktree), self.git_output("worktree", "list", "--porcelain"))
        self.assertNotIn(
            f"refs/heads/{task_branch}",
            self.git_output("for-each-ref", "--format=%(refname)", "refs/heads"),
        )

        primary_context_show = subprocess.run(
            [
                str(launcher),
                "task",
                "show",
                "--project-root",
                str(self.root),
                "--task",
                task_id,
                "--json",
            ],
            cwd=str(self.root),
            check=True,
            capture_output=True,
            text=True,
        )
        shown_after = json.loads(primary_context_show.stdout)["task_show"]
        self.assertEqual(shown_after["task_id"], task_id)
        self.assertEqual(shown_after["status"], TASK_STATUS_DONE)
        self.assertFalse(shown_after["worktree_exists"])
        self.assertFalse(shown_after["branch_exists"])

        runtime_before_replay = profile.paths.runtime_file.read_bytes()
        events_before_replay = profile.paths.events_file.read_bytes()
        target_before_replay = self.git_output("rev-parse", "main")
        terminal_replay = subprocess.run(
            [
                str(launcher),
                "task",
                "land",
                "--project-root",
                str(self.root),
                "--task",
                task_id,
                "--actor",
                "codex",
                "--summary",
                "Complete default cleanup landing",
                "--validation",
                "acceptance=passed",
                "--json",
            ],
            cwd=str(self.root),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(terminal_replay.returncode, 0, terminal_replay.stderr)
        replayed = json.loads(terminal_replay.stdout)["landing"]
        self.assertEqual(replayed["operation_status"], "succeeded")
        self.assertEqual(replayed["next_action"]["action_id"], "landing_complete")
        self.assertEqual(replayed["next_action"]["kind"], "complete")
        self.assertEqual(replayed["landed_commit"], landed_commit)
        self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before_replay)
        self.assertEqual(profile.paths.events_file.read_bytes(), events_before_replay)
        self.assertEqual(self.git_output("rev-parse", "main"), target_before_replay)

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
