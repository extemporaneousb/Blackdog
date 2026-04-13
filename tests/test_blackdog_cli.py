from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess

from blackdog_core.backlog import finish_task, start_task, upsert_workset
from blackdog_core.profile import load_profile
from blackdog_core.state import ValidationRecord, create_prompt_receipt
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

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
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
        subprocess.run(
            ["git", "-C", str(self.root), "add", "blackdog.toml", ".codex/skills/blackdog/SKILL.md"],
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

    def test_worktree_preview_shows_the_start_plan_and_contract_inputs(self) -> None:
        (self.root / ".codex" / "skills" / "blackdog").mkdir(parents=True, exist_ok=True)
        skill_path = self.root / ".codex" / "skills" / "blackdog" / "SKILL.md"
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
        self.assertEqual(snapshot["runtime_model"]["recent_attempts"][0]["execution_model"], "direct_wtam")
        self.assertIsNone(snapshot["runtime_model"]["worksets"][0]["claim"])
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["task_claims"], [])
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["attempts"][0]["worktree_role"], "task")
        self.assertEqual(snapshot["runtime_model"]["worksets"][0]["attempts"][0]["landed_commit"], land_payload["landed_commit"])
        self.assertEqual((self.root / "notes.txt").read_text(encoding="utf-8"), "WTAM kept change\n")

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
        self.assertEqual(next_payload["selection_mode"], "start")
        self.assertEqual(next_payload["selected_task"]["task_id"], "RC-1")

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
            prompt_receipt=create_prompt_receipt("Land the audit slice.", source="unit-test"),
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
            prompt_receipt=create_prompt_receipt("Block the audit slice.", source="unit-test"),
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
        self.assertEqual(summary["recent_completed_attempts"][0]["prompt_source"], "unit-test")

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
        self.assertIn("commit", table["columns"])
        self.assertIn("summary", table["columns"])
        self.assertEqual(len(table["rows"]), 2)
        self.assertEqual(table["workset_scope"], "attempt-audit")
        self.assertEqual(table["rows"][0]["workset_id"], "attempt-audit")
        self.assertEqual(table["rows"][0]["prompt_source"], "unit-test")
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
