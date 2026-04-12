from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess

from blackdog_cli.main import main as blackdog_main
from tests.core_audit_support import CoreAuditTestCase


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

        exit_code, stdout, stderr = self.run_cli("next", "--project-root", str(self.root), "--json")
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout), [{"task_id": "VS-2", "title": "Read status", "intent": "surface a machine-readable snapshot"}])

        exit_code, stdout, stderr = self.run_cli("snapshot", "--project-root", str(self.root))
        self.assertEqual(exit_code, 0, stderr)
        snapshot = json.loads(stdout)
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
        self.assertEqual(start_payload["primary_worktree"], str(self.root.resolve()))
        self.assertTrue(start_payload["branch"].startswith("agent/"))
        self.assertEqual(start_payload["base_commit"], self.git_output("rev-parse", "HEAD"))

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
        subprocess.run(["git", "-C", str(worktree_path), "add", "notes.txt"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(worktree_path), "commit", "-m", "Implement DM-1"], check=True, capture_output=True, text=True)

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
        self.assertEqual(land_payload["attempt_id"], attempt_id)
        self.assertEqual(land_payload["branch"], start_payload["branch"])
        self.assertIn("notes.txt", land_payload["changed_paths"])
        self.assertEqual(land_payload["commit"], land_payload["landed_commit"])

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

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "cleanup",
            "--project-root",
            str(self.root),
            "--workset",
            "direct-mode",
            "--task",
            "DM-1",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        cleanup_payload = json.loads(stdout)["cleanup"]
        self.assertEqual(cleanup_payload["branch"], start_payload["branch"])
        self.assertTrue(cleanup_payload["deleted_branch"])
        self.assertFalse(worktree_path.exists())
        self.assertEqual((self.root / "notes.txt").read_text(encoding="utf-8"), "WTAM kept change\n")

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
        subprocess.run(["git", "-C", str(worktree_path), "add", "invalid.txt"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(worktree_path), "commit", "-m", "Invalid validation"], check=True, capture_output=True, text=True)

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
