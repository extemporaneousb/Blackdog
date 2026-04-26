from __future__ import annotations

from contextlib import chdir, redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import subprocess

from blackdog.contract import managed_skill_relative_path
from blackdog_core.backlog import finish_task, start_task, upsert_workset
from blackdog_core.profile import load_profile
from blackdog_core.state import (
    ValidationRecord,
    create_prompt_receipt,
    load_runtime_state,
    merge_workset_runtime,
    now_iso,
    save_runtime_state,
)
from blackdog_cli.main import main as blackdog_main
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class BlackdogCliTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile("CLI Demo")
        subprocess.run(["git", "-C", str(self.root), "add", "blackdog.toml"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Add Blackdog profile"],
            check=True,
            capture_output=True,
            text=True,
        )

    def run_cli(self, *args: str, cwd: Path | None = None) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with chdir(cwd or Path.cwd()), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = blackdog_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def install_repo_runtime(self) -> None:
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
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "add",
                "blackdog.toml",
                "AGENTS.md",
                str(managed_skill_relative_path(profile)),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if self.git_output("status", "--short"):
            subprocess.run(
                ["git", "-C", str(self.root), "commit", "-m", "Add Blackdog repo runtime"],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_workset_put_summary_next_and_snapshot_form_one_vertical_slice(self) -> None:
        payload = {
            "id": "vertical-slice",
            "title": "Vertical slice",
            "scope": {"kind": "repo", "paths": ["src", "docs"]},
            "visibility": {"kind": "workset"},
            "policies": {"validation": ["make test"]},
            "workspace": {"identity": "vertical-slice-workspace"},
            "branch_intent": {"target_branch": "main", "integration_branch": "main"},
            "tasks": [
                {
                    "id": "VS-1",
                    "title": "Create planning data",
                    "intent": "write a workset payload through the CLI",
                },
                {
                    "id": "VS-2",
                    "title": "Read status",
                    "intent": "surface a machine-readable snapshot",
                    "depends_on": ["VS-1"],
                },
            ],
            "task_states": [{"task_id": "VS-1", "status": "done"}],
        }

        exit_code, stdout, stderr = self.run_cli(
            "workset",
            "put",
            "--project-root",
            str(self.root),
            "--json",
            json.dumps(payload),
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["workset"]["id"], "vertical-slice")

        exit_code, stdout, stderr = self.run_cli("summary", "--project-root", str(self.root))
        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("vertical-slice: Vertical slice", stdout)
        self.assertIn("[READY] VS-2 Read status", stdout)

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "vertical-slice",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        scoped_summary = json.loads(stdout)
        self.assertEqual(scoped_summary["workset_scope"], "vertical-slice")
        self.assertEqual(scoped_summary["counts"]["worksets"], 1)
        self.assertEqual(scoped_summary["worksets"][0]["id"], "vertical-slice")

        exit_code, stdout, stderr = self.run_cli(
            "next",
            "--project-root",
            str(self.root),
            "--workset",
            "vertical-slice",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        next_payload = json.loads(stdout)
        self.assertEqual(next_payload["workset_id"], "vertical-slice")
        self.assertEqual(next_payload["selection_mode"], "start")
        self.assertEqual(next_payload["selected_task"]["task_id"], "VS-2")
        self.assertEqual(next_payload["ready_tasks"][0]["workset_id"], "vertical-slice")

        exit_code, stdout, stderr = self.run_cli(
            "snapshot",
            "--project-root",
            str(self.root),
            "--workset",
            "vertical-slice",
        )
        self.assertEqual(exit_code, 0, stderr)
        snapshot = json.loads(stdout)
        self.assertEqual(len(snapshot["runtime_model"]["worksets"]), 1)
        self.assertEqual(snapshot["runtime_model"]["counts"]["ready"], 1)
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["workspace"]["identity"], "vertical-slice-workspace")
        self.assertEqual(snapshot["runtime_model"]["counts"]["attempts"], 0)

    def test_workset_put_rejects_non_object_payload(self) -> None:
        exit_code, stdout, stderr = self.run_cli(
            "workset",
            "put",
            "--project-root",
            str(self.root),
            "--json",
            '["not-an-object"]',
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("JSON object payload", stderr)

    def test_task_cancel_and_reopen_control_normal_visibility(self) -> None:
        payload = {
            "id": "manual-cancel",
            "title": "Manual cancel",
            "tasks": [{"id": "CAN-1", "title": "Cancel this", "intent": "hide stale work"}],
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
            "task",
            "cancel",
            "--project-root",
            str(self.root),
            "--workset",
            "manual-cancel",
            "--task",
            "CAN-1",
            "--summary",
            "stale",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["task_state"]["status"], "canceled")

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "manual-cancel",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["counts"]["tasks"], 0)

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "manual-cancel",
            "--include-canceled",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["counts"]["canceled"], 1)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "reopen",
            "--project-root",
            str(self.root),
            "--workset",
            "manual-cancel",
            "--task",
            "CAN-1",
            "--summary",
            "needed",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["task_state"]["status"], "planned")

        exit_code, stdout, stderr = self.run_cli(
            "next",
            "--project-root",
            str(self.root),
            "--workset",
            "manual-cancel",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        next_payload = json.loads(stdout)
        self.assertEqual(next_payload["selection_mode"], "start")
        self.assertEqual(next_payload["selected_task"]["task_id"], "CAN-1")

    def test_worktree_preview_shows_the_start_plan_and_contract_inputs(self) -> None:
        profile = load_profile(self.root)
        skill_path = (self.root / managed_skill_relative_path(profile)).resolve()
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text("repo skill\n", encoding="utf-8")
        agents_path = self.root / "AGENTS.md"
        agents_path.write_text("repo contract\n", encoding="utf-8")

        payload = {
            "id": "preview-mode",
            "title": "Preview mode",
            "scope": {"kind": "repo", "paths": ["src", "docs"]},
            "workspace": {"identity": "preview-workspace"},
            "branch_intent": {"target_branch": "main", "integration_branch": "feature/preview"},
            "tasks": [
                {
                    "id": "PV-1",
                    "title": "Preview the WTAM plan",
                    "intent": "surface the prompt receipt and contract inputs",
                    "paths": ["src/blackdog/wtam.py"],
                    "docs": ["docs/CLI.md"],
                    "checks": ["make test"],
                }
            ],
        }
        exit_code, stdout, stderr = self.run_cli(
            "workset",
            "put",
            "--project-root",
            str(self.root),
            "--json",
            json.dumps(payload),
        )
        self.assertEqual(exit_code, 0, stderr)
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "preview",
            "--project-root",
            str(self.root),
            "--workset",
            "preview-mode",
            "--task",
            "PV-1",
            "--actor",
            "codex",
            "--prompt",
            "Show me the exact WTAM start plan.",
            "--show-prompt",
            "--expand-contract",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        preview = json.loads(stdout)["worktree_preview"]
        self.assertTrue(preview["start_ready"])
        self.assertEqual(preview["execution_model"], "direct_wtam")
        self.assertEqual(preview["workspace_identity"], "preview-workspace")
        self.assertEqual(preview["prompt_text"], "Show me the exact WTAM start plan.")
        self.assertEqual(preview["prompt_source"], "inline:--prompt")
        self.assertEqual(preview["task_paths"], ["src/blackdog/wtam.py"])
        self.assertEqual(preview["task_docs"], ["docs/CLI.md"])
        self.assertEqual(preview["task_checks"], ["make test"])
        self.assertEqual(preview["handlers"]["runtime_mode"], "launcher-shim")
        self.assertEqual(preview["handlers"]["source_mode"], "managed-checkout")
        self.assertTrue(any(action["action"] == "ensure-worktree-venv" for action in preview["handlers"]["actions"]))
        self.assertTrue(any(item["path"] == str(skill_path.resolve()) for item in preview["contract_documents"]))
        self.assertTrue(any(item["path"] == str(agents_path.resolve()) for item in preview["contract_documents"]))
        self.assertTrue(any(item["text"] == "repo skill\n" for item in preview["contract_documents"]))

    def test_worktree_start_land_and_cleanup_drive_the_kept_change_flow(self) -> None:
        payload = {
            "id": "direct-mode",
            "title": "Direct mode",
            "workspace": {"identity": "direct-mode-workspace"},
            "branch_intent": {"target_branch": "main", "integration_branch": "feature/direct-mode"},
            "tasks": [{"id": "DM-1", "title": "Record stats", "intent": "exercise direct-agent mode"}],
        }
        exit_code, stdout, stderr = self.run_cli(
            "workset",
            "put",
            "--project-root",
            str(self.root),
            "--json",
            json.dumps(payload),
        )
        self.assertEqual(exit_code, 0, stderr)
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "preflight",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        preflight_payload = json.loads(stdout)
        self.assertTrue(preflight_payload["current_is_primary"])
        self.assertEqual(preflight_payload["workspace_role"], "primary")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "direct-mode",
            "--task",
            "DM-1",
            "--actor",
            "codex",
            "--prompt",
            "Implement the direct slice and record repo execution lineage.",
            "--model",
            "gpt-5.4",
            "--reasoning-effort",
            "high",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        attempt_id = start_payload["attempt_id"]
        prompt_hash = hashlib.sha256(
            "Implement the direct slice and record repo execution lineage.".encode("utf-8")
        ).hexdigest()
        worktree_path = Path(start_payload["worktree_path"])
        self.assertTrue(worktree_path.exists())
        self.assertEqual(start_payload["runtime_mode"], "launcher-shim")
        self.assertEqual(start_payload["source_mode"], "managed-checkout")
        self.assertEqual(start_payload["script_policy"], "root-bin-fallback")
        self.assertEqual(start_payload["primary_worktree"], str(self.root.resolve()))
        self.assertTrue(start_payload["branch"].startswith("agent/"))
        self.assertEqual(start_payload["base_commit"], self.git_output("rev-parse", "HEAD"))
        workspace_cli = worktree_path / ".VE" / "bin" / "blackdog"
        self.assertTrue(workspace_cli.is_file())
        completed = subprocess.run(
            [str(workspace_cli), "summary", "--project-root", str(self.root)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Project: CLI Demo", completed.stdout)

        exit_code, stdout, stderr = self.run_cli("snapshot", "--project-root", str(self.root))
        self.assertEqual(exit_code, 0, stderr)
        snapshot = json.loads(stdout)
        self.assertEqual(snapshot["runtime_model"]["counts"]["claimed_worksets"], 1)
        self.assertEqual(snapshot["runtime_model"]["counts"]["claimed_tasks"], 1)
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["execution_model"], "direct_wtam")
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["claim"]["actor"], "codex")
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["claim"]["execution_model"], "direct_wtam")
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["task_claims"][0]["task_id"], "DM-1")

        note_path = worktree_path / "notes.txt"
        note_path.write_text("WTAM kept change\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "land",
            "--project-root",
            str(self.root),
            "--workset",
            "direct-mode",
            "--task",
            "DM-1",
            "--actor",
            "codex",
            "--summary",
            "finished direct mode",
            "--validation",
            "unit=passed",
            "--residual",
            "none",
            "--followup",
            "publish",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "success")
        self.assertEqual(land_payload["attempt_id"], attempt_id)
        self.assertEqual(land_payload["branch"], start_payload["branch"])
        self.assertIn("notes.txt", land_payload["changed_paths"])
        self.assertNotEqual(land_payload["commit"], land_payload["landed_commit"])
        self.assertTrue(land_payload["deleted_branch"])
        self.assertEqual(land_payload["cleaned_worktree"], str(worktree_path))
        self.assertFalse(worktree_path.exists())
        landed_message = self.git_output("show", "-s", "--format=%B", land_payload["landed_commit"])
        self.assertIn("blackdog(direct-mode/DM-1): Record stats", landed_message)
        self.assertIn("Blackdog-Workset: direct-mode", landed_message)
        self.assertIn("Blackdog-Task: DM-1", landed_message)
        self.assertIn("Blackdog-Status: success", landed_message)
        self.assertIn("Blackdog-Execution-Model: direct_wtam", landed_message)
        self.assertIn("Blackdog-Model: gpt-5.4", landed_message)
        self.assertIn("Blackdog-Reasoning-Effort: high", landed_message)
        self.assertIn(f"Blackdog-Prompt-Hash: {prompt_hash}", landed_message)
        self.assertIn("Blackdog-Prompt-Source: inline:--prompt", landed_message)
        self.assertIn("Blackdog-Prompt-Mode: raw", landed_message)
        self.assertIn("Blackdog-Changed-Path: notes.txt", landed_message)
        self.assertNotIn("Blackdog-User-Prompt-Hash:", landed_message)

        exit_code, stdout, stderr = self.run_cli("summary", "--project-root", str(self.root))
        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("Attempts: 1 | Active attempts: 0", stdout)
        self.assertIn("latest_attempt=success", stdout)
        self.assertIn("branch=", stdout)
        self.assertIn("prompt=", stdout)

        exit_code, stdout, stderr = self.run_cli("snapshot", "--project-root", str(self.root))
        self.assertEqual(exit_code, 0, stderr)
        snapshot = json.loads(stdout)
        self.assertEqual(snapshot["runtime_model"]["counts"]["attempts"], 1)
        self.assertEqual(snapshot["runtime_model"]["counts"]["claimed_worksets"], 0)
        self.assertEqual(snapshot["runtime_model"]["counts"]["claimed_tasks"], 0)
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["attempt_id"], attempt_id)
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["prompt_receipt"]["prompt_hash"], prompt_hash)
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["user_prompt_receipt"]["prompt_hash"], prompt_hash)
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["prompt_receipt"]["mode"], "raw")
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["execution_model"], "direct_wtam")
        self.assertIsNone(snapshot["runtime_model"]["worksets"][0]["claim"])
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["task_claims"], [])
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["attempts"][0]["worktree_role"], "task")
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["attempts"][0]["landed_commit"], land_payload["landed_commit"])
        self.assertEqual((self.root / "notes.txt").read_text(encoding="utf-8"), "WTAM kept change\n")

    def test_task_begin_creates_a_single_task_envelope_and_lands_from_the_task_worktree(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Implement the same-thread task flow and capture the lineage.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        workset_id = task_payload["workset_id"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        self.assertTrue(task_payload["created_workset"])
        self.assertEqual(task_payload["task_id"], "TASK-1")
        self.assertEqual(task_payload["prompt_mode"], "raw")
        self.assertEqual(task_payload["user_prompt_hash"], task_payload["execution_prompt_hash"])
        self.assertTrue(workset_id.startswith("task-"))
        self.assertTrue(worktree_path.exists())

        (worktree_path / "task-begin.txt").write_text("task begin\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        show_payload = json.loads(stdout)["task_show"]
        self.assertTrue(show_payload["active_attempt"])
        self.assertEqual(show_payload["workset_id"], workset_id)
        self.assertEqual(show_payload["task_id"], "TASK-1")
        self.assertIn("task-begin.txt", show_payload["changed_paths"])
        self.assertEqual(show_payload["user_prompt_hash"], task_payload["user_prompt_hash"])
        self.assertEqual(show_payload["execution_prompt_hash"], task_payload["execution_prompt_hash"])

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "finished the same-thread task flow",
            "--validation",
            "unit=passed",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "success")
        self.assertEqual(land_payload["task_id"], "TASK-1")
        self.assertIn("task-begin.txt", land_payload["changed_paths"])
        self.assertFalse(worktree_path.exists())
        landed_message = self.git_output("show", "-s", "--format=%B", land_payload["landed_commit"])
        self.assertIn(f"blackdog({workset_id}/TASK-1)", landed_message)
        self.assertIn("Blackdog-Changed-Path: task-begin.txt", landed_message)
        self.assertIn("Blackdog-Validation: unit=passed", landed_message)
        self.assertNotIn("Blackdog-User-Prompt-Hash:", landed_message)

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary_payload = json.loads(stdout)
        self.assertEqual(summary_payload["counts"]["active_attempts"], 0)
        self.assertEqual(summary_payload["counts"]["claimed_tasks"], 0)
        self.assertEqual((self.root / "task-begin.txt").read_text(encoding="utf-8"), "task begin\n")

    def test_task_begin_can_tune_the_prompt_and_task_close_can_infer_the_current_attempt(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Make a tuned execution prompt for this slice.",
            "--prompt-mode",
            "tuned",
            "--show-prompt",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        workset_id = task_payload["workset_id"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        self.assertEqual(task_payload["prompt_mode"], "tuned")
        self.assertNotEqual(task_payload["user_prompt_hash"], task_payload["execution_prompt_hash"])
        self.assertIn("You are working in the repo", task_payload["execution_prompt_text"])

        (worktree_path / "tuned.txt").write_text("tuned\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        show_payload = json.loads(stdout)["task_show"]
        self.assertEqual(show_payload["user_prompt_hash"], task_payload["user_prompt_hash"])
        self.assertEqual(show_payload["user_prompt_mode"], "raw")
        self.assertEqual(show_payload["execution_prompt_hash"], task_payload["execution_prompt_hash"])
        self.assertEqual(show_payload["execution_prompt_mode"], "tuned")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "abandoned",
            "--summary",
            "abandoned the tuned slice",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        close_payload = json.loads(stdout)["closure"]
        self.assertEqual(close_payload["status"], "abandoned")
        self.assertIn("tuned.txt", close_payload["changed_paths"])

        exit_code, stdout, stderr = self.run_cli(
            "next",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        next_payload = json.loads(stdout)
        self.assertEqual(next_payload["selection_mode"], "none")
        self.assertIsNone(next_payload["selected_task"])

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary_payload = json.loads(stdout)
        self.assertEqual(summary_payload["counts"]["tasks"], 0)
        self.assertEqual(summary_payload["worksets"], [])

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--include-canceled",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary_payload = json.loads(stdout)
        self.assertEqual(summary_payload["counts"]["canceled"], 1)
        self.assertEqual(summary_payload["worksets"][0]["recent_attempts"][0]["user_prompt_hash"], task_payload["user_prompt_hash"])
        self.assertEqual(
            summary_payload["worksets"][0]["recent_attempts"][0]["execution_prompt_hash"],
            task_payload["execution_prompt_hash"],
        )
        self.assertIsNone(summary_payload["worksets"][0]["recent_attempts"][0]["prompt_hash"])

    def test_task_begin_accepts_skill_execution_prompt_and_user_prompt(self) -> None:
        self.install_repo_runtime()
        user_prompt_path = self.root / "USER_PROMPT.txt"
        execution_prompt_path = self.root / "EXECUTION_PROMPT.txt"
        user_prompt_path.write_text("Add a repo-local feature.\n", encoding="utf-8")
        execution_prompt_path.write_text("Implement the feature with the repo skill guardrails.\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt-file",
            str(execution_prompt_path),
            "--prompt-mode",
            "skill",
            "--user-prompt-file",
            str(user_prompt_path),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        workset_id = task_payload["workset_id"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        self.assertEqual(task_payload["prompt_mode"], "skill")
        self.assertNotEqual(task_payload["user_prompt_hash"], task_payload["execution_prompt_hash"])

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        show_payload = json.loads(stdout)["task_show"]
        self.assertEqual(show_payload["user_prompt_mode"], "raw")
        self.assertEqual(show_payload["execution_prompt_mode"], "skill")
        self.assertEqual(show_payload["user_prompt_hash"], task_payload["user_prompt_hash"])
        self.assertEqual(show_payload["execution_prompt_hash"], task_payload["execution_prompt_hash"])

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "abandoned",
            "--summary",
            "closed the skill prompt smoke",
            "--cleanup",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertFalse(worktree_path.exists())

        exit_code, stdout, stderr = self.run_cli(
            "snapshot",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
        )
        self.assertEqual(exit_code, 0, stderr)
        snapshot_payload = json.loads(stdout)
        self.assertEqual(
            snapshot_payload["runtime_model"]["recent_attempts"][0]["user_prompt_receipt"]["prompt_hash"],
            task_payload["user_prompt_hash"],
        )
        self.assertEqual(
            snapshot_payload["runtime_model"]["recent_attempts"][0]["prompt_receipt"]["prompt_hash"],
            task_payload["execution_prompt_hash"],
        )

    def test_task_land_records_user_and_execution_prompt_lineage_when_prompt_was_tuned(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Make a tuned execution prompt and then land it.",
            "--prompt-mode",
            "tuned",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        self.assertNotEqual(task_payload["user_prompt_hash"], task_payload["execution_prompt_hash"])

        (worktree_path / "tuned-land.txt").write_text("tuned land\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "finished the tuned landing flow",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "success")
        landed_message = self.git_output("show", "-s", "--format=%B", land_payload["landed_commit"])
        self.assertIn(f"Blackdog-Prompt-Hash: {task_payload['execution_prompt_hash']}", landed_message)
        self.assertIn("Blackdog-Prompt-Source: inline:--prompt", landed_message)
        self.assertIn("Blackdog-Prompt-Mode: tuned", landed_message)
        self.assertIn(f"Blackdog-User-Prompt-Hash: {task_payload['user_prompt_hash']}", landed_message)
        self.assertIn("Blackdog-User-Prompt-Source: inline:--prompt", landed_message)
        self.assertIn("Blackdog-User-Prompt-Mode: raw", landed_message)
        self.assertIn("Blackdog-Changed-Path: tuned-land.txt", landed_message)

    def test_task_cleanup_removes_a_retained_task_workspace(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Keep the task workspace around, then clean it up through the task surface.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        (worktree_path / "cleanup.txt").write_text("cleanup\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "kept the workspace for explicit cleanup",
            "--keep-worktree",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "success")
        self.assertTrue(worktree_path.exists())
        self.assertIsNone(land_payload["cleaned_worktree"])

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        cleanup_payload = json.loads(stdout)["cleanup"]
        self.assertEqual(cleanup_payload["worktree_path"], str(worktree_path))
        self.assertTrue(cleanup_payload["deleted_branch"])
        self.assertFalse(worktree_path.exists())

    def test_task_recover_reports_dirty_same_thread_recovery_state(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Recover a dirty task worktree through the task surface.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        branch = task_payload["worktree"]["branch"]
        (worktree_path / "recover-task.txt").write_text("recover\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "recover",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        recovery_payload = json.loads(stdout)["recovery"]
        self.assertEqual(recovery_payload["recovery_state"], "active_attempt")
        self.assertFalse(recovery_payload["stale_claim"])
        self.assertEqual(recovery_payload["task_runtime_status"], "in_progress")
        self.assertEqual(recovery_payload["task_claim"]["actor"], "codex")
        self.assertTrue(recovery_payload["worktree_dirty"])
        actions = "\n".join(recovery_payload["recommended_actions"])
        self.assertIn("blackdog task land", actions)
        self.assertIn("blackdog task close", actions)

        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", branch],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_task_recover_can_release_a_stale_claim(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Recover a stale claim without editing snapshots by hand.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        workset_id = task_payload["workset_id"]
        task_id = task_payload["task_id"]
        attempt_id = task_payload["worktree"]["attempt_id"]
        branch = task_payload["worktree"]["branch"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])

        profile = load_profile(self.root)
        runtime_state = load_runtime_state(profile.paths)
        runtime_workset = next(item for item in runtime_state.worksets if item.workset_id == workset_id)
        active_attempt = next(item for item in runtime_workset.attempts if item.attempt_id == attempt_id)
        stale_attempt = replace(
            active_attempt,
            status="blocked",
            ended_at=now_iso(),
            summary="agent interrupted before releasing claims",
            elapsed_seconds=1,
        )
        stale_runtime_state = merge_workset_runtime(
            runtime_state,
            workset_id=workset_id,
            task_ids={task_id},
            incoming_records=None,
            incoming_attempts=(stale_attempt,),
        )
        save_runtime_state(profile.paths, stale_runtime_state)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "recover",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        recovery_payload = json.loads(stdout)["recovery"]
        self.assertEqual(recovery_payload["recovery_state"], "stale_claim")
        self.assertTrue(recovery_payload["stale_claim"])
        self.assertFalse(recovery_payload["active_attempt"])
        self.assertEqual(recovery_payload["task_claim"]["attempt_id"], attempt_id)
        self.assertIn("release-stale-claim", "\n".join(recovery_payload["recommended_actions"]))

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "recover",
            "--project-root",
            str(self.root),
            "--release-stale-claim",
            "--status",
            "abandoned",
            "--summary",
            "released the stale claim after interruption",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        released_payload = json.loads(stdout)["recovery"]
        self.assertTrue(released_payload["released_stale_claim"])
        self.assertFalse(released_payload["stale_claim"])
        self.assertIsNone(released_payload["task_claim"])
        self.assertIsNone(released_payload["workset_claim"])
        self.assertEqual(released_payload["task_runtime_status"], "canceled")
        self.assertEqual(released_payload["repaired_runtime_status"], "canceled")

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary_payload = json.loads(stdout)
        self.assertEqual(summary_payload["counts"]["claimed_tasks"], 0)
        self.assertEqual(summary_payload["counts"]["claimed_worksets"], 0)
        self.assertEqual(summary_payload["counts"]["active_attempts"], 0)

        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", branch],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_worktree_show_and_close_surface_active_attempt_recovery(self) -> None:
        payload = {
            "id": "recovery-mode",
            "title": "Recovery mode",
            "tasks": [{"id": "RC-1", "title": "Recover the slice", "intent": "inspect and close an active attempt"}],
        }
        self.run_cli(
            "workset",
            "put",
            "--project-root",
            str(self.root),
            "--json",
            json.dumps(payload),
        )
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "recovery-mode",
            "--task",
            "RC-1",
            "--actor",
            "codex",
            "--prompt",
            "Start the recovery slice.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        worktree_path = Path(start_payload["worktree_path"])
        (worktree_path / "recover.txt").write_text("recover\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "show",
            "--project-root",
            str(self.root),
            "--workset",
            "recovery-mode",
            "--task",
            "RC-1",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        show_payload = json.loads(stdout)["worktree_show"]
        self.assertTrue(show_payload["active_attempt"])
        self.assertTrue(show_payload["worktree_dirty"])
        self.assertIn("recover.txt", show_payload["changed_paths"])

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "close",
            "--project-root",
            str(self.root),
            "--workset",
            "recovery-mode",
            "--task",
            "RC-1",
            "--actor",
            "codex",
            "--status",
            "abandoned",
            "--summary",
            "abandoned the recovery slice",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        close_payload = json.loads(stdout)["closure"]
        self.assertEqual(close_payload["status"], "abandoned")
        self.assertIn("recover.txt", close_payload["changed_paths"])

        exit_code, stdout, stderr = self.run_cli(
            "next",
            "--project-root",
            str(self.root),
            "--workset",
            "recovery-mode",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        next_payload = json.loads(stdout)
        self.assertEqual(next_payload["selection_mode"], "none")
        self.assertIsNone(next_payload["selected_task"])

        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", start_payload["branch"]],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_worktree_land_closes_the_attempt_when_landing_is_blocked(self) -> None:
        payload = {
            "id": "blocked-land",
            "title": "Blocked land",
            "tasks": [{"id": "BL-1", "title": "Block landing", "intent": "close the attempt when landing cannot proceed"}],
        }
        self.run_cli(
            "workset",
            "put",
            "--project-root",
            str(self.root),
            "--json",
            json.dumps(payload),
        )
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "blocked-land",
            "--task",
            "BL-1",
            "--actor",
            "codex",
            "--prompt",
            "Attempt the blocked land slice.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        worktree_path = Path(start_payload["worktree_path"])
        (worktree_path / "blocked.txt").write_text("blocked\n", encoding="utf-8")
        (self.root / "primary-dirty.txt").write_text("dirty\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "land",
            "--project-root",
            str(self.root),
            "--workset",
            "blocked-land",
            "--task",
            "BL-1",
            "--actor",
            "codex",
            "--summary",
            "attempted the blocked land slice",
            "--json",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "blocked")
        self.assertIn("dirty primary worktree", land_payload["error"])

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "blocked-land",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["counts"]["active_attempts"], 0)
        self.assertEqual(summary["counts"]["claimed_tasks"], 0)
        self.assertEqual(summary["worksets"][0]["recent_attempts"][0]["status"], "blocked")

        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", start_payload["branch"]],
            check=True,
            capture_output=True,
            text=True,
        )
        (self.root / "primary-dirty.txt").unlink()

    def test_attempts_summary_and_table_report_completed_history(self) -> None:
        profile = load_profile(self.root)
        upsert_workset(
            profile,
            {
                "id": "attempt-audit",
                "title": "Attempt audit",
                "workspace": {"identity": "attempt-audit-workspace"},
                "branch_intent": {"target_branch": "main", "integration_branch": "main"},
                "tasks": [
                    {"id": "AT-1", "title": "Land a change", "intent": "record a landed attempt"},
                    {"id": "AT-2", "title": "Block a change", "intent": "record a blocked attempt"},
                ],
            },
        )
        landed_attempt = start_task(
            profile,
            workset_id="attempt-audit",
            task_id="AT-1",
            actor="codex",
            workspace_mode="git-worktree",
            worktree_role="linked",
            worktree_path="/tmp/attempt-audit-1",
            branch="feature/attempt-audit-1",
            start_commit="abc123",
            prompt_receipt=create_prompt_receipt("Land the audit slice.", source="unit-test", mode="tuned"),
            user_prompt_receipt=create_prompt_receipt("Land the audit slice.", source="user-test", mode="raw"),
        )
        finish_task(
            profile,
            workset_id="attempt-audit",
            task_id="AT-1",
            attempt_id=landed_attempt.attempt_id,
            actor="codex",
            status="success",
            summary="landed the slice",
            changed_paths=("src/blackdog_cli/main.py",),
            validations=(ValidationRecord(name="unit", status="passed"),),
            landed_commit="def456",
            elapsed_seconds=11,
        )
        blocked_attempt = start_task(
            profile,
            workset_id="attempt-audit",
            task_id="AT-2",
            actor="codex",
            workspace_mode="git-worktree",
            worktree_role="linked",
            worktree_path="/tmp/attempt-audit-2",
            branch="feature/attempt-audit-2",
            start_commit="abc124",
            prompt_receipt=create_prompt_receipt("Block the audit slice.", source="unit-test", mode="tuned"),
            user_prompt_receipt=create_prompt_receipt("Block the audit slice.", source="user-test", mode="raw"),
        )
        finish_task(
            profile,
            workset_id="attempt-audit",
            task_id="AT-2",
            attempt_id=blocked_attempt.attempt_id,
            actor="codex",
            status="blocked",
            summary="waiting on review",
            validations=(ValidationRecord(name="unit", status="failed"),),
            elapsed_seconds=7,
        )

        exit_code, stdout, stderr = self.run_cli(
            "attempts",
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "attempt-audit",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["counts"]["completed_attempts"], 2)
        self.assertEqual(summary["counts"]["landed"], 1)
        self.assertEqual(summary["counts"]["not_landed"], 1)
        self.assertEqual(summary["counts"]["validation_passed"], 1)
        self.assertEqual(summary["counts"]["validation_failed"], 1)
        self.assertEqual(summary["workset_scope"], "attempt-audit")
        self.assertEqual(summary["worksets"][0]["workset_id"], "attempt-audit")
        self.assertIsNone(summary["recent_completed_attempts"][0]["prompt_source"])
        self.assertIsNone(summary["recent_completed_attempts"][0]["prompt_hash"])
        self.assertEqual(summary["recent_completed_attempts"][0]["user_prompt_source"], "user-test")
        self.assertEqual(summary["recent_completed_attempts"][0]["execution_prompt_source"], "unit-test")
        self.assertEqual(
            summary["recent_completed_attempts"][0]["user_prompt_hash"],
            summary["recent_completed_attempts"][0]["execution_prompt_hash"],
        )

        exit_code, stdout, stderr = self.run_cli(
            "attempts",
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "attempt-audit",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("user_prompt=user-test:", stdout)
        self.assertIn("execution_prompt=unit-test:", stdout)
        self.assertNotIn(" prompt=unit-test:", stdout)

        exit_code, stdout, stderr = self.run_cli(
            "attempts",
            "table",
            "--project-root",
            str(self.root),
            "--workset",
            "attempt-audit",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        table = json.loads(stdout)
        self.assertEqual(table["columns"][0], "workset_id")
        self.assertIn("model", table["columns"])
        self.assertIn("reasoning_effort", table["columns"])
        self.assertIn("prompt_source", table["columns"])
        self.assertIn("user_prompt_source", table["columns"])
        self.assertIn("execution_prompt_hash", table["columns"])
        self.assertIn("commit", table["columns"])
        self.assertIn("summary", table["columns"])
        self.assertEqual(len(table["rows"]), 2)
        self.assertEqual(table["workset_scope"], "attempt-audit")
        self.assertEqual(table["rows"][0]["workset_id"], "attempt-audit")
        self.assertIsNone(table["rows"][0]["prompt_source"])
        self.assertIsNone(table["rows"][0]["prompt_hash"])
        self.assertEqual(table["rows"][0]["user_prompt_source"], "user-test")
        self.assertEqual(table["rows"][0]["user_prompt_hash"], table["rows"][0]["execution_prompt_hash"])
        self.assertIn(table["rows"][0]["validation_summary"], {"passed=1 failed=0 skipped=0", "passed=0 failed=1 skipped=0"})
        self.assertEqual(
            {row["landed_commit"] for row in table["rows"]},
            {"def456", None},
        )

    def test_worktree_land_rejects_invalid_validation_status(self) -> None:
        payload = {
            "id": "invalid-validation",
            "title": "Invalid validation",
            "tasks": [{"id": "IV-1", "title": "Reject invalid validation", "intent": "guard the CLI"}],
        }
        self.run_cli(
            "workset",
            "put",
            "--project-root",
            str(self.root),
            "--json",
            json.dumps(payload),
        )
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "invalid-validation",
            "--task",
            "IV-1",
            "--actor",
            "codex",
            "--prompt",
            "Attempt the invalid validation task.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        worktree_payload = json.loads(stdout)["worktree"]
        worktree_path = Path(worktree_payload["worktree_path"])
        (worktree_path / "invalid.txt").write_text("invalid\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "land",
            "--project-root",
            str(self.root),
            "--workset",
            "invalid-validation",
            "--task",
            "IV-1",
            "--actor",
            "codex",
            "--summary",
            "attempt the invalid validation closure",
            "--validation",
            "unit=unknown",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("validation status must be one of", stderr)
        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "branch", "-D", worktree_payload["branch"]],
            check=True,
            capture_output=True,
            text=True,
        )
