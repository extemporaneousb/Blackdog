from __future__ import annotations

from contextlib import chdir, redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import blackdog.wtam as wtam
from blackdog.contract import managed_skill_relative_path
from blackdog_core.backlog import finish_task, load_planning_state, start_task, upsert_workset
from blackdog_core.codex_sessions import codex_task_context_path
from blackdog_core.profile import load_profile
from blackdog_core.state import (
    ValidationRecord,
    create_prompt_receipt,
    load_events,
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

    def put_workset(self, payload: dict[str, object]) -> tuple[int, str, str]:
        with patch.dict(os.environ, {"BLACKDOG_ENABLE_WORKSET_COMMANDS": "1"}, clear=False):
            return self.run_cli(
                "workset",
                "put",
                "--project-root",
                str(self.root),
                "--json",
                json.dumps(payload),
            )

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
        skill_metadata_path = managed_skill_relative_path(profile).parent / "agents" / "openai.yaml"
        tracked_paths = [
            "blackdog.toml",
            "AGENTS.md",
            str(managed_skill_relative_path(profile)),
        ]
        if (self.root / skill_metadata_path).exists():
            tracked_paths.append(str(skill_metadata_path))
        subprocess.run(
            ["git", "-C", str(self.root), "add", *tracked_paths],
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

    def test_workset_put_is_disabled_without_explicit_opt_in(self) -> None:
        exit_code, stdout, stderr = self.run_cli(
            "workset",
            "put",
            "--project-root",
            str(self.root),
            "--json",
            json.dumps({"id": "accidental", "title": "Accidental", "tasks": []}),
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("direct workset authoring is disabled by default", stderr)
        self.assertIn("BLACKDOG_ENABLE_WORKSET_COMMANDS=1", stderr)

    def test_default_help_hides_planned_workset_commands(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                blackdog_main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("task", help_text)
        self.assertIn("summary", help_text)
        self.assertNotIn("workset", help_text)
        self.assertNotIn("next", help_text)

    def test_task_begin_partial_existing_target_points_to_normal_new_task_path(self) -> None:
        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Implement a new task.",
            "--workset",
            "invented-workset",
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("For new work, omit both flags", stderr)
        self.assertIn("provide both", stderr)

    def test_worktree_start_unknown_workset_points_to_task_begin(self) -> None:
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "invented-workset",
            "--task",
            "TASK-1",
            "--actor",
            "codex",
            "--prompt",
            "Implement a new task.",
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Unknown workset 'invented-workset'", stderr)
        self.assertIn("use `blackdog task begin` without --workset/--task", stderr)

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

        exit_code, stdout, stderr = self.put_workset(payload)
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["workset"]["id"], "vertical-slice")

        exit_code, stdout, stderr = self.run_cli("summary", "--project-root", str(self.root))
        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("Ready tasks:", stdout)
        self.assertIn("vertical-slice/VS-2 Read status", stdout)

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
        self.assertNotIn("worksets", scoped_summary)
        self.assertEqual(scoped_summary["ready_tasks"][0]["task_ref"], "vertical-slice/VS-2")

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "vertical-slice",
            "--include-legacy-worksets",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["worksets"][0]["id"], "vertical-slice")

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
        self.assertEqual(next_payload["ready_tasks"][0]["task_ref"], "vertical-slice/VS-2")

        exit_code, stdout, stderr = self.run_cli(
            "snapshot",
            "--project-root",
            str(self.root),
            "--workset",
            "vertical-slice",
        )
        self.assertEqual(exit_code, 0, stderr)
        snapshot = json.loads(stdout)
        self.assertNotIn("worksets", snapshot["runtime_model"])
        self.assertEqual(len(snapshot["runtime_model"]["tasks"]), 2)
        self.assertEqual(snapshot["runtime_model"]["counts"]["ready"], 1)
        self.assertEqual(snapshot["runtime_model"]["tasks"][1]["task_ref"], "vertical-slice/VS-2")
        self.assertEqual(snapshot["runtime_model"]["counts"]["attempts"], 0)

    def test_workset_put_rejects_non_object_payload(self) -> None:
        with patch.dict(os.environ, {"BLACKDOG_ENABLE_WORKSET_COMMANDS": "1"}, clear=False):
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
        exit_code, _, stderr = self.put_workset(payload)
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
            "--failure-class",
            "superseded",
            "--recovery-action",
            "leave_canceled",
            "--operator-issue",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_state = json.loads(stdout)["task_state"]
        self.assertEqual(task_state["status"], "canceled")
        self.assertEqual(task_state["failure_class"], "superseded")
        self.assertEqual(task_state["recovery_action"], "leave_canceled")
        self.assertTrue(task_state["operator_issue"])

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
        self.assertEqual(json.loads(stdout)["tasks"][0]["failure_class"], "superseded")

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
        exit_code, stdout, stderr = self.put_workset(payload)
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
        self.assertEqual(preview["handlers"]["source_mode"], "local-override")
        self.assertTrue(any(action["action"] == "ensure-worktree-venv" for action in preview["handlers"]["actions"]))
        self.assertTrue(any(item["path"] == str(skill_path.resolve()) for item in preview["contract_documents"]))
        self.assertTrue(any(item["path"] == str(agents_path.resolve()) for item in preview["contract_documents"]))
        self.assertTrue(any(item["text"] == "repo skill\n" for item in preview["contract_documents"]))

    def test_worktree_preflight_ignores_configured_generated_primary_paths(self) -> None:
        self.install_repo_runtime()
        profile_path = self.root / "blackdog.toml"
        profile_text = profile_path.read_text(encoding="utf-8")
        profile_text = profile_text.replace('root_path = ".VE"', 'root_path = "generated-env"', 1)
        profile_text = profile_text.replace('worktree_path = ".VE"', 'worktree_path = "generated-env"', 1)
        profile_text = profile_text.replace('launcher_path = ".VE/bin/blackdog"', 'launcher_path = "generated-env/bin/blackdog"', 1)
        profile_path.write_text(profile_text, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "blackdog.toml"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Use visible generated handler path"],
            check=True,
            capture_output=True,
            text=True,
        )

        generated_path = self.root / "generated-env" / "generated.txt"
        generated_path.parent.mkdir(parents=True, exist_ok=True)
        generated_path.write_text("generated\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "preflight",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        preflight_payload = json.loads(stdout)
        self.assertTrue(preflight_payload["dirty"])
        self.assertFalse(preflight_payload["primary_dirty"])
        self.assertFalse(preflight_payload["implementation_dirty"])
        self.assertEqual(preflight_payload["primary_dirty_paths"], [])

        (self.root / "real-dirty.txt").write_text("dirty\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "preflight",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        preflight_payload = json.loads(stdout)
        self.assertTrue(preflight_payload["primary_dirty"])
        self.assertEqual(preflight_payload["primary_dirty_paths"], ["real-dirty.txt"])

    def test_worktree_start_land_and_cleanup_drive_the_kept_change_flow(self) -> None:
        payload = {
            "id": "direct-mode",
            "title": "Direct mode",
            "workspace": {"identity": "direct-mode-workspace"},
            "branch_intent": {"target_branch": "main", "integration_branch": "feature/direct-mode"},
            "tasks": [{"id": "DM-1", "title": "Record stats", "intent": "exercise direct-agent mode"}],
        }
        exit_code, stdout, stderr = self.put_workset(payload)
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
        self.assertEqual(start_payload["source_mode"], "local-override")
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

        exit_code, stdout, stderr = self.run_cli(
            "snapshot",
            "--project-root",
            str(self.root),
            "--include-legacy-worksets",
        )
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
        self.assertIn("Recent attempts:", stdout)
        self.assertIn("status=success", stdout)
        self.assertIn("branch=", stdout)
        self.assertIn("prompt=", stdout)

        exit_code, stdout, stderr = self.run_cli(
            "snapshot",
            "--project-root",
            str(self.root),
            "--include-legacy-worksets",
        )
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

    def test_linked_worktree_preflight_and_task_begin_target_the_current_branch(self) -> None:
        self.install_repo_runtime()
        task_worktree_path: Path | None = None
        task_branch: str | None = None
        expected_worktrees_dir = (self.root.parent / f".worktrees-{self.root.name}").resolve()
        with tempfile.TemporaryDirectory() as linked_base:
            linked_worktree = Path(linked_base) / "wt-feature"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.root),
                    "worktree",
                    "add",
                    "-b",
                    "feature/stable",
                    str(linked_worktree),
                    "main",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            try:
                for project_root in (linked_worktree, self.root):
                    with self.subTest(project_root=project_root):
                        exit_code, stdout, stderr = self.run_cli(
                            "worktree",
                            "preflight",
                            "--project-root",
                            str(project_root),
                            "--json",
                            cwd=linked_worktree,
                        )
                        self.assertEqual(exit_code, 0, stderr)
                        preflight_payload = json.loads(stdout)
                        self.assertFalse(preflight_payload["current_is_primary"])
                        self.assertEqual(preflight_payload["workspace_role"], "linked")
                        self.assertEqual(preflight_payload["current_branch"], "feature/stable")
                        self.assertEqual(preflight_payload["primary_branch"], "main")
                        self.assertEqual(preflight_payload["target_branch"], "feature/stable")
                        self.assertEqual(Path(preflight_payload["worktrees_dir"]), expected_worktrees_dir)

                subprocess.run(
                    [sys.executable, "-m", "venv", str(linked_worktree / ".VE")],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                exit_code, stdout, stderr = self.run_cli(
                    "task",
                    "begin",
                    "--project-root",
                    str(self.root),
                    "--actor",
                    "codex",
                    "--prompt",
                    "Exercise linked-branch task targeting.",
                    "--json",
                    cwd=linked_worktree,
                )
                self.assertEqual(exit_code, 0, stderr)
                task_payload = json.loads(stdout)["task"]
                self.assertEqual(task_payload["worktree"]["target_branch"], "feature/stable")
                self.assertEqual(task_payload["worktree"]["current_worktree"], str(linked_worktree.resolve()))
                task_branch = task_payload["worktree"]["branch"]
                task_worktree_path = Path(task_payload["worktree"]["worktree_path"])
                self.assertEqual(task_worktree_path.parent, expected_worktrees_dir)
            finally:
                if task_worktree_path is not None:
                    subprocess.run(
                        ["git", "-C", str(self.root), "worktree", "remove", "--force", str(task_worktree_path)],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                subprocess.run(
                    ["git", "-C", str(self.root), "worktree", "remove", "--force", str(linked_worktree)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if task_branch:
                    subprocess.run(
                        ["git", "-C", str(self.root), "branch", "-D", task_branch],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                subprocess.run(
                    ["git", "-C", str(self.root), "branch", "-D", "feature/stable"],
                    check=False,
                    capture_output=True,
                    text=True,
                )

    def test_task_begin_creates_a_single_task_envelope_and_lands_from_the_task_worktree(self) -> None:
        self.install_repo_runtime()
        codex_home = self.root / ".git" / "codex-home"
        (codex_home / "sessions" / "2026" / "05" / "04").mkdir(parents=True)
        session_path = codex_home / "sessions" / "2026" / "05" / "04" / "rollout-2026-05-04T12-00-00-thread-task-begin.jsonl"
        session_path.write_text("", encoding="utf-8")
        (codex_home / "config.toml").write_text(
            'model = "gpt-5.5"\nmodel_reasoning_effort = "xhigh"\n',
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"CODEX_HOME": str(codex_home), "CODEX_THREAD_ID": "thread-task-begin"},
            clear=False,
        ):
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
        self.assertEqual(task_payload["worktree"]["setup_receipt"]["status"], "ok")
        self.assertEqual(task_payload["worktree"]["setup_receipt"]["task_class"], "implementation")

        exit_code, stdout, stderr = self.run_cli("snapshot", "--project-root", str(self.root))
        self.assertEqual(exit_code, 0, stderr)
        started_attempt = json.loads(stdout)["runtime_model"]["recent_attempts"][0]
        self.assertEqual(started_attempt["model"], "gpt-5.5")
        self.assertEqual(started_attempt["reasoning_effort"], "xhigh")
        self.assertEqual(started_attempt["codex_session"]["thread_id"], "thread-task-begin")
        self.assertEqual(
            started_attempt["codex_session"]["session_path"],
            "sessions/2026/05/04/rollout-2026-05-04T12-00-00-thread-task-begin.jsonl",
        )
        self.assertIsNone(started_attempt["prompt_receipt"]["text"])
        attempt_row = json.loads(stdout)["runtime_model"]["attempts"][0]
        self.assertEqual(attempt_row["setup_status"], "ok")
        self.assertEqual(attempt_row["task_class"], "implementation")
        self.assertEqual(attempt_row["setup_blockers_count"], 0)
        self.assertEqual(attempt_row["setup_receipt"]["status"], "ok")

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
        self.assertIn("Blackdog-Model: gpt-5.5", landed_message)
        self.assertIn("Blackdog-Reasoning-Effort: xhigh", landed_message)
        self.assertIn("Blackdog-Codex-Thread: thread-task-begin", landed_message)
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

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            "TASK-1",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        completed_payload = json.loads(stdout)["task_show"]
        self.assertFalse(completed_payload["active_attempt"])
        self.assertEqual(completed_payload["latest_attempt_status"], "success")
        self.assertEqual(completed_payload["task_runtime_status"], "done")
        self.assertEqual(completed_payload["recovery_state"], "idle")
        self.assertFalse(completed_payload["branch_exists"])
        self.assertTrue(completed_payload["target_branch_exists"])
        self.assertIsNone(completed_payload["branch_ahead_error"])
        self.assertIsNone(completed_payload["failure_class"])
        self.assertIsNone(completed_payload["recovery_action"])
        self.assertFalse(completed_payload["operator_issue"])
        self.assertEqual(completed_payload["recommended_actions"], [])
        self.assertEqual(completed_payload["recommended_commands"], [])

    def test_task_begin_deployment_guard_blocks_before_auto_task_creation(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Deploy production now.",
            "--json",
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("task start blocked by setup guard", stderr)
        self.assertIn("deployment tasks must name the CI/GitHub Actions route", stderr)
        profile = load_profile(self.root)
        self.assertEqual(load_planning_state(profile.paths).worksets, ())
        self.assertEqual(load_runtime_state(profile.paths).worksets, ())

    def test_task_begin_deployment_route_records_setup_receipt(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Deploy production through GitHub Actions workflow_dispatch.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        branch = task_payload["worktree"]["branch"]
        setup_receipt = task_payload["worktree"]["setup_receipt"]
        self.assertEqual(setup_receipt["status"], "ok")
        self.assertEqual(setup_receipt["task_class"], "deployment")
        self.assertEqual(setup_receipt["blockers"], [])
        self.assertTrue(any(row["name"] == "deployment_route" and row["status"] == "ok" for row in setup_receipt["probes"]))

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "abandoned",
            "--summary",
            "closed deployment route receipt smoke test",
            "--cleanup",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertFalse(worktree_path.exists())
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(self.root), "branch", "--list", branch],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "",
        )

    def test_codex_coverage_and_history_cli_read_codex_sessions(self) -> None:
        codex_home = self.root / ".codex-home"
        session_path = codex_home / "sessions" / "2026" / "05" / "04" / "rollout-2026-05-04T12-00-00-thread-cli.jsonl"
        session_path.parent.mkdir(parents=True)
        session_path.write_text(
            "\n".join(
                json.dumps(row)
                for row in [
                    {
                        "timestamp": "2026-05-04T19:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "thread-cli", "timestamp": "2026-05-04T19:00:00Z", "cwd": str(self.root)},
                    },
                    {
                        "timestamp": "2026-05-04T19:00:01Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started", "turn_id": "turn-cli", "started_at": 1777921201},
                    },
                    {
                        "timestamp": "2026-05-04T19:00:01Z",
                        "type": "turn_context",
                        "payload": {"turn_id": "turn-cli", "cwd": str(self.root), "model": "gpt-5.5", "effort": "xhigh"},
                    },
                    {
                        "timestamp": "2026-05-04T19:00:02Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": "Implement a CLI-visible Codex history row."},
                    },
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
            exit_code, stdout, stderr = self.run_cli("codex", "coverage", "--project-root", str(self.root), "--json")
            self.assertEqual(exit_code, 0, stderr)
            coverage = json.loads(stdout)["codex_coverage"]
            self.assertEqual(coverage["counts"]["codex_user_turns"], 1)
            self.assertEqual(coverage["counts"]["implementation_like_unlinked_turns"], 1)

            exit_code, stdout, stderr = self.run_cli("codex", "history", "--project-root", str(self.root), "--jsonl")
            self.assertEqual(exit_code, 0, stderr)
            rows = [json.loads(line) for line in stdout.splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "codex_turn")
            self.assertNotIn("message_excerpt", rows[0])

            exit_code, stdout, stderr = self.run_cli("codex", "history", "--project-root", str(self.root), "--write")
            self.assertEqual(exit_code, 0, stderr)
            self.assertTrue((self.root / ".blackdog" / "history.jsonl").exists())

    def test_codex_hook_stamp_cli_records_active_task_context(self) -> None:
        profile = load_profile(self.root)
        upsert_workset(
            profile,
            {
                "id": "hook-cli",
                "title": "Hook CLI",
                "tasks": [{"id": "TASK-1", "title": "Hook CLI task"}],
            },
        )
        attempt = start_task(
            profile,
            workset_id="hook-cli",
            task_id="TASK-1",
            actor="codex",
            prompt_receipt=create_prompt_receipt("Implement hook CLI stamping."),
            worktree_path=str(self.root),
            branch="main",
            target_branch="main",
        )
        event_payload = {
            "hook_event_name": "Stop",
            "session_id": "thread-cli-hook",
            "turn_id": "turn-cli-hook",
            "cwd": str(self.root),
            "prompt": "this text must not be persisted",
        }

        exit_code, stdout, stderr = self.run_cli(
            "codex",
            "hook",
            "stamp",
            "--project-root",
            str(self.root),
            "--event-json",
            json.dumps(event_payload),
            "--json",
            cwd=self.root,
        )

        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["codex_hook_stamp"]
        self.assertTrue(payload["context_found"])
        self.assertEqual(payload["active_attempt"]["attempt_id"], attempt.attempt_id)
        self.assertEqual(payload["turn_classification"]["source"], "heuristic")
        rows = load_events(codex_task_context_path(profile))
        self.assertEqual(rows[0]["payload"]["hook"]["session_id"], "thread-cli-hook")
        self.assertEqual(rows[0]["payload"]["turn_classification"], payload["turn_classification"])
        self.assertNotIn("this text", json.dumps(rows[0]["payload"]))

        silent_payload = {**event_payload, "turn_id": "turn-cli-hook-silent"}
        exit_code, stdout, stderr = self.run_cli(
            "codex",
            "hook",
            "stamp",
            "--project-root",
            str(self.root),
            "--event-json",
            json.dumps(silent_payload),
            cwd=self.root,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "")
        self.assertEqual(
            load_events(codex_task_context_path(profile))[-1]["payload"]["hook"]["turn_id"],
            "turn-cli-hook-silent",
        )

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
        self.assertNotIn("worksets", summary_payload)
        self.assertEqual(summary_payload["tasks"], [])

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
        self.assertEqual(summary_payload["recent_attempts"][0]["user_prompt_hash"], task_payload["user_prompt_hash"])
        self.assertEqual(
            summary_payload["recent_attempts"][0]["execution_prompt_hash"],
            task_payload["execution_prompt_hash"],
        )
        self.assertIsNone(summary_payload["recent_attempts"][0]["prompt_hash"])

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
        self.assertTrue(cleanup_payload["worktree_existed"])
        self.assertTrue(cleanup_payload["deleted_branch"])
        self.assertTrue(cleanup_payload["force_deleted_branch"])
        self.assertEqual(cleanup_payload["branch_cleanup_proof"], "patch_equivalent")
        self.assertIn("canonical landed commit", cleanup_payload["branch_cleanup_reason"])
        self.assertFalse(worktree_path.exists())

    def test_task_cleanup_removes_missing_retained_workspace_when_branch_is_proven(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Land and retain a workspace that later disappears.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        branch = task_payload["worktree"]["branch"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        (worktree_path / "missing-cleanup.txt").write_text("cleanup\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "landed but retained before external cleanup",
            "--keep-worktree",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)

        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        table = json.loads(stdout)["worktree_table"]
        self.assertEqual(table["counts"]["rows"], 1)
        row = table["rows"][0]
        self.assertEqual(row["cleanup_status"], "cleanup_ready")
        self.assertEqual(row["cleanup_proof"], "patch_equivalent")
        self.assertIn("worktree already absent", row["cleanup_reason"])

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "cleanup",
            "--project-root",
            str(self.root),
            "--all",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        cleanup = json.loads(stdout)["cleanup"]
        self.assertEqual(len(cleanup["cleaned"]), 1)
        self.assertFalse(cleanup["cleaned"][0]["worktree_existed"])
        self.assertEqual(cleanup["cleaned"][0]["branch_cleanup_proof"], "patch_equivalent")
        self.assertEqual(cleanup["remaining"]["counts"]["rows"], 0)
        self.assertNotIn(branch, self.git_output("branch", "--format=%(refname:short)").splitlines())

    def test_task_cleanup_accepts_abandoned_branch_with_patches_already_on_target(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Create a task patch that is later landed outside the canonical Blackdog path.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        branch = task_payload["worktree"]["branch"]
        (worktree_path / "manual-equivalent.txt").write_text("landed manually\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(worktree_path), "add", "manual-equivalent.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(worktree_path), "commit", "-m", "Add manually landed cleanup fixture"],
            check=True,
            capture_output=True,
            text=True,
        )
        branch_tip = self.git_output("rev-parse", branch)
        subprocess.run(
            ["git", "-C", str(self.root), "cherry-pick", "--no-commit", branch_tip],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Land task patch with an alternate commit"],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "abandoned",
            "--summary",
            "patch landed manually on the target branch",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["closure"]["status"], "abandoned")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        row = json.loads(stdout)["worktree_table"]["rows"][0]
        self.assertEqual(row["cleanup_status"], "cleanup_ready")
        self.assertEqual(row["cleanup_proof"], "patch_equivalent")
        self.assertIn("all terminal task-branch patches", row["cleanup_reason"])

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)
        cleanup = json.loads(stdout)["cleanup"]
        self.assertTrue(cleanup["deleted_branch"])
        self.assertTrue(cleanup["force_deleted_branch"])
        self.assertEqual(cleanup["branch_cleanup_proof"], "patch_equivalent")
        self.assertFalse(worktree_path.exists())

    def test_task_cleanup_refuses_active_attempt_even_when_worktree_is_clean(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Start active work that must not be cleaned directly.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        worktree_path = Path(json.loads(stdout)["task"]["worktree"]["worktree_path"])

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("active attempts must be landed or closed before cleanup", stderr)
        self.assertTrue(worktree_path.exists())

    def test_task_cleanup_refuses_unproven_branch_after_landing(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Keep the task workspace around, then add unlanded work after landing.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        branch = task_payload["worktree"]["branch"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        (worktree_path / "landed.txt").write_text("landed\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "landed but retained the workspace",
            "--keep-worktree",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)

        (worktree_path / "unlanded.txt").write_text("unlanded\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(worktree_path), "add", "unlanded.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(worktree_path), "commit", "-m", "Add unlanded follow-up work"],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "cleanup",
            "--project-root",
            str(self.root),
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("not proven landed", stderr)
        self.assertIn("branch tip changed", stderr)
        self.assertTrue(worktree_path.exists())
        worktree_head = subprocess.run(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(self.git_output("rev-parse", "--verify", branch), worktree_head)

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

    def test_worktree_table_reports_active_dirty_task_worktree(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Leave active work in the task worktree.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        (worktree_path / "unlanded.txt").write_text("unlanded\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        table = json.loads(stdout)["worktree_table"]
        self.assertEqual(table["counts"]["rows"], 1)
        row = table["rows"][0]
        self.assertEqual(row["workset_id"], task_payload["workset_id"])
        self.assertEqual(row["task_id"], task_payload["task_id"])
        self.assertEqual(row["state"], "active_attempt")
        self.assertEqual(row["cleanup_status"], "blocked_dirty")
        self.assertEqual(row["worktree_dirty_count"], 1)
        self.assertEqual(row["changed_paths_count"], 1)
        self.assertEqual(row["worktree_path"], str(worktree_path))
        self.assertEqual(row["cleanup_reason"], "worktree has uncommitted changes")
        self.assertIn("blackdog worktree land", row["recommended_action"])

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("last_commit_message", stdout.splitlines()[0])
        self.assertIn("size_bytes", stdout.splitlines()[0])

    def test_worktree_table_reports_no_ahead_cleanup_proof(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Close a clean no-ahead task workspace.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        worktree_path = Path(json.loads(stdout)["task"]["worktree"]["worktree_path"])

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "blocked",
            "--summary",
            "closed without changes",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        row = json.loads(stdout)["worktree_table"]["rows"][0]
        self.assertEqual(row["cleanup_status"], "cleanup_ready")
        self.assertEqual(row["cleanup_proof"], "no_ahead")
        self.assertIn("no commits ahead", row["cleanup_reason"])

    def test_worktree_table_reports_contained_branch_cleanup_proof(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Close a branch already contained by main.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        branch = task_payload["worktree"]["branch"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        (worktree_path / "contained.txt").write_text("contained\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(worktree_path), "add", "contained.txt"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(worktree_path), "commit", "-m", "Add contained work"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "merge", "--ff-only", branch],
            check=True,
            capture_output=True,
            text=True,
        )
        (self.root / "after-contained.txt").write_text("after\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "after-contained.txt"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Advance after contained work"],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "close",
            "--project-root",
            str(self.root),
            "--status",
            "blocked",
            "--summary",
            "closed after branch was already contained",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        row = json.loads(stdout)["worktree_table"]["rows"][0]
        self.assertEqual(row["cleanup_status"], "cleanup_ready")
        self.assertEqual(row["cleanup_proof"], "contained")
        self.assertIn("already merged", row["cleanup_reason"])

    def test_worktree_cleanup_all_removes_cleanup_ready_rows_until_table_is_empty(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Retain a landed task worktree for bulk cleanup.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        (worktree_path / "landed.txt").write_text("landed\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "landed but retained for table cleanup",
            "--keep-worktree",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        table = json.loads(stdout)["worktree_table"]
        self.assertEqual(table["counts"]["rows"], 1)
        self.assertEqual(table["counts"]["cleanup_ready"], 1)
        self.assertEqual(table["rows"][0]["cleanup_status"], "cleanup_ready")
        self.assertIn("blackdog task cleanup", table["rows"][0]["cleanup_command"])

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "cleanup",
            "--project-root",
            str(self.root),
            "--all",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        cleanup = json.loads(stdout)["cleanup"]
        self.assertEqual(len(cleanup["cleaned"]), 1)
        self.assertEqual(cleanup["remaining"]["counts"]["rows"], 0)
        self.assertFalse(worktree_path.exists())

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["worktree_table"]["counts"]["rows"], 0)

    def test_worktree_table_reuses_primary_worktree_lookup_for_multiple_rows(self) -> None:
        self.install_repo_runtime()

        for index in range(2):
            exit_code, stdout, stderr = self.run_cli(
                "task",
                "begin",
                "--project-root",
                str(self.root),
                "--actor",
                "codex",
                "--prompt",
                f"Retain landed task worktree {index}.",
                "--json",
            )
            self.assertEqual(exit_code, 0, stderr)
            task_payload = json.loads(stdout)["task"]
            worktree_path = Path(task_payload["worktree"]["worktree_path"])
            (worktree_path / f"landed-{index}.txt").write_text(f"landed {index}\n", encoding="utf-8")

            exit_code, _stdout, stderr = self.run_cli(
                "task",
                "land",
                "--project-root",
                str(self.root),
                "--summary",
                f"landed retained task {index}",
                "--keep-worktree",
                "--json",
                cwd=worktree_path,
            )
            self.assertEqual(exit_code, 0, stderr)

        with patch("blackdog.wtam.find_primary_worktree", wraps=wtam.find_primary_worktree) as find_primary:
            exit_code, stdout, stderr = self.run_cli(
                "worktree",
                "table",
                "--project-root",
                str(self.root),
                "--json",
            )
        self.assertEqual(exit_code, 0, stderr)
        table = json.loads(stdout)["worktree_table"]
        self.assertEqual(table["counts"]["rows"], 2)
        self.assertEqual(table["counts"]["cleanup_ready"], 2)
        self.assertEqual(find_primary.call_count, 1)

    def test_worktree_cleanup_all_handles_missing_landed_worktree(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Retain a landed task worktree, then lose the directory before cleanup.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        branch = task_payload["worktree"]["branch"]
        (worktree_path / "landed.txt").write_text("landed\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "land",
            "--project-root",
            str(self.root),
            "--summary",
            "landed but retained before external cleanup",
            "--keep-worktree",
            "--json",
            cwd=worktree_path,
        )
        self.assertEqual(exit_code, 0, stderr)

        subprocess.run(["git", "-C", str(self.root), "worktree", "remove", str(worktree_path)], check=True)
        self.assertFalse(worktree_path.exists())
        worktree_path.mkdir(parents=True)
        (worktree_path / "leftover.txt").write_text("not a git worktree\n", encoding="utf-8")

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "table",
            "--project-root",
            str(self.root),
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        table = json.loads(stdout)["worktree_table"]
        self.assertEqual(table["counts"]["rows"], 1)
        self.assertEqual(table["counts"]["cleanup_ready"], 1)
        self.assertEqual(table["rows"][0]["cleanup_status"], "cleanup_ready")
        self.assertIn("worktree already absent", table["rows"][0]["cleanup_reason"])

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "cleanup",
            "--project-root",
            str(self.root),
            "--all",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        cleanup = json.loads(stdout)["cleanup"]
        self.assertEqual(len(cleanup["cleaned"]), 1)
        self.assertEqual(cleanup["remaining"]["counts"]["rows"], 0)
        branch_list = subprocess.run(
            ["git", "-C", str(self.root), "branch", "--list", branch],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(branch_list, "")

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
        command_rows = recovery_payload["recommended_commands"]
        commands = [row["command"] for row in command_rows]
        self.assertIn('blackdog task land --summary "..."', commands)
        self.assertIn('blackdog task close --status blocked|failed|abandoned --summary "..."', commands)
        self.assertTrue(all(row["reason"] for row in command_rows))
        self.assertTrue(all(row["disposition"] for row in command_rows))

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

    def test_task_show_reports_missing_target_branch_without_crashing(self) -> None:
        payload = {
            "id": "missing-target",
            "title": "Missing target",
            "tasks": [{"id": "MT-1", "title": "Inspect missing target", "intent": "recover stale target refs"}],
        }
        self.put_workset(payload)
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "missing-target",
            "--task",
            "MT-1",
            "--actor",
            "codex",
            "--prompt",
            "Start the missing target slice.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        attempt_id = start_payload["attempt_id"]
        branch = start_payload["branch"]
        worktree_path = Path(start_payload["worktree_path"])
        profile = load_profile(self.root)
        finished = finish_task(
            profile,
            workset_id="missing-target",
            task_id="MT-1",
            attempt_id=attempt_id,
            actor="codex",
            status="blocked",
            summary="blocked before stale target inspection",
        )
        runtime_state = load_runtime_state(profile.paths)
        runtime_workset = next(item for item in runtime_state.worksets if item.workset_id == "missing-target")
        runtime_task_state = next(item for item in runtime_workset.task_states if item.task_id == "MT-1")
        rewritten_runtime = merge_workset_runtime(
            runtime_state,
            workset_id="missing-target",
            task_ids={"MT-1"},
            incoming_records=(replace(runtime_task_state, failure_class=None, recovery_action=None),),
            incoming_attempts=(replace(finished, target_branch="v3", failure_class=None, recovery_action=None),),
        )
        save_runtime_state(profile.paths, rewritten_runtime)

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "show",
            "--project-root",
            str(self.root),
            "--workset",
            "missing-target",
            "--task",
            "MT-1",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        show_payload = json.loads(stdout)["task_show"]
        self.assertEqual(show_payload["recovery_state"], "stale_reference")
        self.assertEqual(show_payload["target_branch"], "v3")
        self.assertTrue(show_payload["branch_exists"])
        self.assertFalse(show_payload["target_branch_exists"])
        self.assertEqual(show_payload["failure_class"], "stale_branch")
        self.assertEqual(show_payload["recovery_action"], "restore_ref_or_cancel_task")
        self.assertIn("target branch 'v3' is missing", show_payload["branch_ahead_error"])
        self.assertIn("restore target branch `v3`", "\n".join(show_payload["recommended_actions"]))

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

    def test_task_recover_reports_missing_task_branch_without_crashing(self) -> None:
        payload = {
            "id": "missing-branch",
            "title": "Missing branch",
            "tasks": [{"id": "MB-1", "title": "Inspect missing branch", "intent": "recover stale branch refs"}],
        }
        self.put_workset(payload)
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "missing-branch",
            "--task",
            "MB-1",
            "--actor",
            "codex",
            "--prompt",
            "Start the missing branch slice.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        attempt_id = start_payload["attempt_id"]
        branch = start_payload["branch"]
        worktree_path = Path(start_payload["worktree_path"])
        profile = load_profile(self.root)
        finished = finish_task(
            profile,
            workset_id="missing-branch",
            task_id="MB-1",
            attempt_id=attempt_id,
            actor="codex",
            status="blocked",
            summary="blocked before stale branch inspection",
        )
        runtime_state = load_runtime_state(profile.paths)
        runtime_workset = next(item for item in runtime_state.worksets if item.workset_id == "missing-branch")
        runtime_task_state = next(item for item in runtime_workset.task_states if item.task_id == "MB-1")
        rewritten_runtime = merge_workset_runtime(
            runtime_state,
            workset_id="missing-branch",
            task_ids={"MB-1"},
            incoming_records=(replace(runtime_task_state, failure_class=None, recovery_action=None),),
            incoming_attempts=(replace(finished, failure_class=None, recovery_action=None),),
        )
        save_runtime_state(profile.paths, rewritten_runtime)
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

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "recover",
            "--project-root",
            str(self.root),
            "--workset",
            "missing-branch",
            "--task",
            "MB-1",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        recovery_payload = json.loads(stdout)["recovery"]
        self.assertEqual(recovery_payload["recovery_state"], "stale_reference")
        self.assertEqual(recovery_payload["branch"], branch)
        self.assertFalse(recovery_payload["branch_exists"])
        self.assertTrue(recovery_payload["target_branch_exists"])
        self.assertEqual(recovery_payload["failure_class"], "stale_branch")
        self.assertIn(f"task branch {branch!r} is missing", recovery_payload["branch_ahead_error"])
        self.assertIn("use `blackdog task cancel`", "\n".join(recovery_payload["recommended_actions"]))

    def test_task_recover_reports_missing_active_attempt_worktree(self) -> None:
        self.install_repo_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "begin",
            "--project-root",
            str(self.root),
            "--actor",
            "codex",
            "--prompt",
            "Inspect a missing active attempt worktree.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        workset_id = task_payload["workset_id"]
        task_id = task_payload["task_id"]
        branch = task_payload["worktree"]["branch"]
        worktree_path = Path(task_payload["worktree"]["worktree_path"])
        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "task",
            "recover",
            "--project-root",
            str(self.root),
            "--workset",
            workset_id,
            "--task",
            task_id,
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        recovery_payload = json.loads(stdout)["recovery"]
        self.assertTrue(recovery_payload["active_attempt"])
        self.assertFalse(recovery_payload["worktree_exists"])
        self.assertTrue(recovery_payload["branch_exists"])
        self.assertTrue(recovery_payload["target_branch_exists"])
        self.assertEqual(recovery_payload["failure_class"], "missing_worktree")
        self.assertEqual(recovery_payload["recovery_action"], "restore_or_cleanup_worktree")
        self.assertIn("restore the task workspace", "\n".join(recovery_payload["recommended_actions"]))

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
        self.put_workset(payload)
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

    def test_worktree_land_keeps_attempt_active_when_landing_is_blocked_and_can_retry(self) -> None:
        payload = {
            "id": "blocked-land",
            "title": "Blocked land",
            "tasks": [{"id": "BL-1", "title": "Block landing", "intent": "retry after landing cannot proceed"}],
        }
        self.put_workset(payload)
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
        attempt_id = start_payload["attempt_id"]
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
        self.assertEqual(land_payload["attempt_id"], attempt_id)
        self.assertTrue(land_payload["attempt_active"])
        self.assertEqual(land_payload["land_failure_disposition"], "retryable")
        self.assertIn("dirty primary worktree", land_payload["error"])
        self.assertEqual(land_payload["failure_class"], "dirty_primary")
        self.assertEqual(land_payload["recovery_action"], "clean_primary_worktree")
        self.assertTrue(land_payload["operator_issue"])

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
        self.assertEqual(summary["counts"]["active_attempts"], 1)
        self.assertEqual(summary["counts"]["claimed_tasks"], 1)
        self.assertEqual(summary["recent_attempts"][0]["status"], "in_progress")

        (self.root / "primary-dirty.txt").unlink()

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
            "retried the blocked land slice",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        retry_payload = json.loads(stdout)["landing"]
        self.assertEqual(retry_payload["status"], "success")
        self.assertEqual(retry_payload["attempt_id"], attempt_id)
        self.assertIn("blocked.txt", retry_payload["changed_paths"])
        self.assertFalse(worktree_path.exists())
        self.assertEqual((self.root / "blocked.txt").read_text(encoding="utf-8"), "blocked\n")

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
        self.assertEqual(summary["recent_attempts"][0]["status"], "success")

    def test_worktree_land_classifies_stale_branch_blocker(self) -> None:
        payload = {
            "id": "stale-land",
            "title": "Stale land",
            "tasks": [{"id": "SL-1", "title": "Block stale branch", "intent": "detect stale task branch"}],
        }
        self.put_workset(payload)
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "stale-land",
            "--task",
            "SL-1",
            "--actor",
            "codex",
            "--prompt",
            "Attempt the stale branch land slice.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        worktree_path = Path(start_payload["worktree_path"])
        (worktree_path / "stale.txt").write_text("stale\n", encoding="utf-8")
        (self.root / "main-advanced.txt").write_text("advanced\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "main-advanced.txt"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Advance main"],
            check=True,
            capture_output=True,
            text=True,
        )

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "land",
            "--project-root",
            str(self.root),
            "--workset",
            "stale-land",
            "--task",
            "SL-1",
            "--actor",
            "codex",
            "--summary",
            "attempted stale branch land",
            "--json",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "blocked")
        self.assertTrue(land_payload["attempt_active"])
        self.assertEqual(land_payload["land_failure_disposition"], "retryable")
        self.assertEqual(land_payload["failure_class"], "stale_branch")
        self.assertEqual(land_payload["recovery_action"], "rebase_task_branch")
        self.assertIn(f"git -C {worktree_path} rebase main", land_payload["error"])
        self.assertIn(f"git -C {worktree_path} rebase main", land_payload["recommended_actions"][0])

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

    def test_worktree_land_closes_terminal_no_change_failure_without_extra_close_call(self) -> None:
        payload = {
            "id": "terminal-land",
            "title": "Terminal land",
            "tasks": [{"id": "TL-1", "title": "Close no-op land", "intent": "close terminal land failures"}],
        }
        self.put_workset(payload)
        self.install_repo_runtime()
        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "start",
            "--project-root",
            str(self.root),
            "--workset",
            "terminal-land",
            "--task",
            "TL-1",
            "--actor",
            "codex",
            "--prompt",
            "Attempt to land a no-op slice.",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        start_payload = json.loads(stdout)["worktree"]
        attempt_id = start_payload["attempt_id"]
        worktree_path = Path(start_payload["worktree_path"])

        exit_code, stdout, stderr = self.run_cli(
            "worktree",
            "land",
            "--project-root",
            str(self.root),
            "--workset",
            "terminal-land",
            "--task",
            "TL-1",
            "--actor",
            "codex",
            "--summary",
            "attempted a no-op land",
            "--json",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        land_payload = json.loads(stdout)["landing"]
        self.assertEqual(land_payload["status"], "blocked")
        self.assertEqual(land_payload["attempt_id"], attempt_id)
        self.assertFalse(land_payload["attempt_active"])
        self.assertEqual(land_payload["land_failure_disposition"], "closed")
        self.assertIn("has no changes relative to", land_payload["error"])
        self.assertTrue(land_payload["cleanup_performed"])
        self.assertFalse(worktree_path.exists())

        exit_code, stdout, stderr = self.run_cli(
            "summary",
            "--project-root",
            str(self.root),
            "--workset",
            "terminal-land",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["counts"]["active_attempts"], 0)
        self.assertEqual(summary["counts"]["claimed_tasks"], 0)
        self.assertEqual(summary["recent_attempts"][0]["status"], "blocked")
        self.assertEqual(summary["recent_attempts"][0]["failure_class"], "no_changes")

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
        self.assertEqual(summary["tasks"][0]["task_ref"], "attempt-audit/AT-1")
        self.assertNotIn("worksets", summary)
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
        self.assertEqual(table["columns"][0], "task_ref")
        self.assertNotIn("workset_id", table["columns"])
        self.assertIn("model", table["columns"])
        self.assertIn("reasoning_effort", table["columns"])
        self.assertIn("prompt_source", table["columns"])
        self.assertIn("user_prompt_source", table["columns"])
        self.assertIn("execution_prompt_hash", table["columns"])
        self.assertIn("commit", table["columns"])
        self.assertIn("failure_class", table["columns"])
        self.assertIn("summary", table["columns"])
        self.assertEqual(len(table["rows"]), 2)
        self.assertEqual(table["workset_scope"], "attempt-audit")
        self.assertTrue(table["rows"][0]["task_ref"].startswith("attempt-audit/"))
        self.assertIsNone(table["rows"][0]["prompt_source"])
        self.assertIsNone(table["rows"][0]["prompt_hash"])
        self.assertEqual(table["rows"][0]["user_prompt_source"], "user-test")
        self.assertEqual(table["rows"][0]["user_prompt_hash"], table["rows"][0]["execution_prompt_hash"])
        self.assertIn(table["rows"][0]["validation_summary"], {"passed=1 failed=0 skipped=0", "passed=0 failed=1 skipped=0"})
        self.assertEqual(
            {row["landed_commit"] for row in table["rows"]},
            {"def456", None},
        )

        exit_code, stdout, stderr = self.run_cli(
            "attempts",
            "table",
            "--project-root",
            str(self.root),
            "--workset",
            "attempt-audit",
            "--include-legacy-worksets",
            "--json",
        )
        self.assertEqual(exit_code, 0, stderr)
        legacy_table = json.loads(stdout)
        self.assertEqual(legacy_table["columns"][0], "workset_id")
        self.assertEqual(legacy_table["rows"][0]["workset_id"], "attempt-audit")

    def test_worktree_land_rejects_invalid_validation_status(self) -> None:
        payload = {
            "id": "invalid-validation",
            "title": "Invalid validation",
            "tasks": [{"id": "IV-1", "title": "Reject invalid validation", "intent": "guard the CLI"}],
        }
        self.put_workset(payload)
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
