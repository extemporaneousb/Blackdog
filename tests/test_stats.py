from __future__ import annotations

from contextlib import chdir, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from blackdog_core.backlog import finish_task, set_task_runtime_status, start_task, upsert_workset
from blackdog_core.state import CodexSessionRefRecord, create_prompt_receipt, prompt_receipt_reference
from blackdog_cli.main import main as blackdog_main
from tests.core_audit_support import CoreAuditTestCase


def _write_stats_session(
    home: Path,
    *,
    cwd: Path,
    thread_id: str,
    turn_id: str,
    started_at: str,
    message: str = "Implement stats fixture.",
) -> Path:
    path = home / "sessions" / "2026" / "06" / "02" / f"rollout-2026-06-02T12-00-00-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": started_at,
            "type": "session_meta",
            "payload": {"id": thread_id, "timestamp": started_at, "cwd": str(cwd), "originator": "Codex Desktop"},
        },
        {
            "timestamp": started_at,
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn_id, "started_at": started_at},
        },
        {
            "timestamp": started_at,
            "type": "turn_context",
            "payload": {"turn_id": turn_id, "started_at": started_at, "cwd": str(cwd), "model": "gpt-5.5"},
        },
        {
            "timestamp": started_at,
            "type": "event_msg",
            "payload": {"type": "user_message", "message": message},
        },
        {
            "timestamp": started_at,
            "type": "response_item",
            "payload": {"type": "function_call", "name": "exec_command", "call_id": "call-1"},
        },
        {
            "timestamp": started_at,
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 10,
                        "cached_input_tokens": 2,
                        "output_tokens": 3,
                        "reasoning_output_tokens": 4,
                        "total_tokens": 13,
                    }
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


class StatsTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile("Stats Demo")
        subprocess.run(["git", "-C", str(self.root), "add", "blackdog.toml"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-m", "Add profile"], check=True, capture_output=True, text=True)
        self.codex_home = self.root / ".codex-home"
        self.blackdog_home = self.root / ".blackdog-home"

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with chdir(self.root), redirect_stdout(stdout), redirect_stderr(stderr):
            with patch.dict(
                os.environ,
                {"CODEX_HOME": str(self.codex_home), "BLACKDOG_HOME": str(self.blackdog_home)},
                clear=False,
            ):
                exit_code = blackdog_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def seed_runtime(self) -> None:
        profile = self.load_test_profile()
        upsert_workset(
            profile,
            {
                "id": "stats",
                "title": "Stats",
                "tasks": [
                    {"id": "S-1", "title": "Success", "intent": "landed success"},
                    {"id": "S-2", "title": "Failed", "intent": "failed attempt"},
                    {"id": "S-3", "title": "Canceled", "intent": "canceled task"},
                ],
            },
        )
        receipt = prompt_receipt_reference(create_prompt_receipt("Implement stats.", recorded_at="2026-06-02T16:00:00+00:00"))
        with patch("blackdog_core.backlog.now_iso", side_effect=["2026-06-02T16:00:00+00:00", "2026-06-02T16:10:00+00:00"]):
            attempt = start_task(profile, workset_id="stats", task_id="S-1", actor="codex", prompt_receipt=receipt)
            finish_task(
                profile,
                workset_id="stats",
                task_id="S-1",
                attempt_id=attempt.attempt_id,
                actor="codex",
                status="success",
                summary="landed",
                landed_commit="abc123",
            )
        with patch("blackdog_core.backlog.now_iso", side_effect=["2026-06-03T01:00:00+00:00", "2026-06-03T01:05:00+00:00"]):
            attempt = start_task(profile, workset_id="stats", task_id="S-2", actor="codex", prompt_receipt=receipt)
            finish_task(
                profile,
                workset_id="stats",
                task_id="S-2",
                attempt_id=attempt.attempt_id,
                actor="codex",
                status="failed",
                summary="failed",
            )
        set_task_runtime_status(
            profile,
            workset_id="stats",
            task_id="S-3",
            actor="codex",
            status="canceled",
            summary="canceled",
        )
        _write_stats_session(
            self.codex_home,
            cwd=self.root,
            thread_id="thread-stats",
            turn_id="turn-stats",
            started_at="2026-06-03T01:30:00+00:00",
        )

    def test_local_repo_registry_add_list_remove(self) -> None:
        exit_code, stdout, stderr = self.run_cli("local-repo", "add", "--project-root", str(self.root), "--json")
        self.assertEqual(exit_code, 0, stderr)
        payload = json.loads(stdout)["local_repos"]
        self.assertTrue(payload["changed"])
        self.assertEqual(Path(payload["rows"][0]["project_root"]).resolve(), self.root.resolve())

        exit_code, stdout, stderr = self.run_cli("local-repo", "list", "--json")
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(len(json.loads(stdout)["local_repos"]["rows"]), 1)

        (self.root / "blackdog.toml").unlink()
        exit_code, stdout, stderr = self.run_cli("local-repo", "remove", "--project-root", str(self.root), "--json")
        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(json.loads(stdout)["local_repos"]["rows"], [])

    def test_stats_command_reports_precise_counts_and_day_buckets(self) -> None:
        self.seed_runtime()

        exit_code, stdout, stderr = self.run_cli(
            "stats",
            "--project-root",
            str(self.root),
            "--since",
            "2026-06-02",
            "--until",
            "2026-06-03",
            "--timezone",
            "America/Los_Angeles",
            "--json",
        )

        self.assertEqual(exit_code, 0, stderr)
        stats = json.loads(stdout)["stats"]
        summary = stats["summary"]
        self.assertEqual(summary["tasks_total"], 3)
        self.assertEqual(summary["current_tasks"], 1)
        self.assertEqual(summary["canceled_tasks"], 1)
        self.assertEqual(summary["completed_attempts"], 2)
        self.assertEqual(summary["success_attempts"], 1)
        self.assertEqual(summary["failed_attempts"], 1)
        self.assertEqual(summary["landed_attempts"], 1)
        self.assertEqual(summary["not_landed_attempts"], 1)
        self.assertEqual(summary["cleanup_terminal_attempts"], 0)
        self.assertEqual(summary["codex_user_turns"], 1)
        self.assertEqual(summary["codex_unlinked_user_turns"], 1)
        self.assertEqual(summary["codex_tool_calls"], 1)
        self.assertEqual(summary["codex_total_tokens"], 13)
        buckets = {row["bucket"]: row for row in stats["buckets"]}
        self.assertEqual(buckets["2026-06-02"]["attempts_started"], 2)
        self.assertEqual(buckets["2026-06-02"]["codex_user_turns"], 1)
        self.assertEqual(buckets["2026-06-02"]["codex_unlinked_user_turns"], 1)
        self.assertEqual(buckets["2026-06-02"]["repos"], 1)

        exit_code, stdout, stderr = self.run_cli("stats", "--project-root", str(self.root), "--tsv")
        self.assertEqual(exit_code, 0, stderr)
        self.assertIn("bucket\trepos\tattempts_started", stdout)

    def test_stats_prunes_unrelated_codex_sessions_for_explicit_root(self) -> None:
        other_root = self.root.parent / "other-repo"
        other_root.mkdir(exist_ok=True)
        _write_stats_session(
            self.codex_home,
            cwd=other_root,
            thread_id="thread-unrelated-stats",
            turn_id="turn-unrelated-stats",
            started_at="2026-06-03T01:30:00+00:00",
        )

        with patch(
            "blackdog_core.codex_sessions.read_codex_session",
            side_effect=AssertionError("parsed unrelated session"),
        ):
            exit_code, stdout, stderr = self.run_cli("stats", "--project-root", str(self.root), "--json")

        self.assertEqual(exit_code, 0, stderr)
        stats = json.loads(stdout)["stats"]
        self.assertEqual(stats["summary"]["codex_user_turns"], 0)
        self.assertEqual(stats["summary"]["codex_sessions"], 0)

    def test_stats_reports_cleanup_and_codex_coverage_health(self) -> None:
        profile = self.load_test_profile()
        upsert_workset(
            profile,
            {
                "id": "health",
                "title": "Health",
                "tasks": [
                    {"id": "H-1", "title": "Linked retained", "intent": "record linked retained cleanup"},
                    {"id": "H-2", "title": "Unlanded", "intent": "record unlanded cleanup"},
                ],
            },
        )
        linked_message = "Implement linked cleanup coverage."
        linked_path = _write_stats_session(
            self.codex_home,
            cwd=self.root,
            thread_id="thread-linked-health",
            turn_id="turn-linked-health",
            started_at="2026-06-02T17:00:00+00:00",
            message=linked_message,
        )
        _write_stats_session(
            self.codex_home,
            cwd=self.root,
            thread_id="thread-unlinked-health",
            turn_id="turn-unlinked-health",
            started_at="2026-06-02T18:00:00+00:00",
            message="Implement untracked cleanup health.",
        )
        retained_worktree = self.root / "retained-worktree"
        retained_worktree.mkdir()
        receipt = create_prompt_receipt(linked_message, recorded_at="2026-06-02T17:00:00+00:00")
        with patch("blackdog_core.backlog.now_iso", side_effect=["2026-06-02T17:00:00+00:00", "2026-06-02T17:05:00+00:00"]):
            attempt = start_task(
                profile,
                workset_id="health",
                task_id="H-1",
                actor="codex",
                worktree_path=str(retained_worktree),
                branch="feature/linked-health",
                target_branch="main",
                prompt_receipt=prompt_receipt_reference(receipt),
                codex_session=CodexSessionRefRecord(
                    thread_id="thread-linked-health",
                    session_path=str(linked_path.relative_to(self.codex_home)),
                    turn_id="turn-linked-health",
                    user_prompt_hash=receipt.prompt_hash,
                    execution_prompt_hash=receipt.prompt_hash,
                ),
            )
            finish_task(
                profile,
                workset_id="health",
                task_id="H-1",
                attempt_id=attempt.attempt_id,
                actor="codex",
                status="success",
                summary="landed but retained",
                landed_commit="abc123",
            )
        with patch("blackdog_core.backlog.now_iso", side_effect=["2026-06-02T18:00:00+00:00", "2026-06-02T18:05:00+00:00"]):
            attempt = start_task(
                profile,
                workset_id="health",
                task_id="H-2",
                actor="codex",
                worktree_path=str(self.root / "missing-worktree"),
                branch="feature/unlanded-health",
                target_branch="main",
                prompt_receipt=create_prompt_receipt("Different prompt.", source="unit", mode="raw"),
            )
            finish_task(
                profile,
                workset_id="health",
                task_id="H-2",
                attempt_id=attempt.attempt_id,
                actor="codex",
                status="failed",
                summary="not landed",
            )

        exit_code, stdout, stderr = self.run_cli(
            "stats",
            "--project-root",
            str(self.root),
            "--since",
            "2026-06-02",
            "--until",
            "2026-06-02",
            "--timezone",
            "UTC",
            "--json",
        )

        self.assertEqual(exit_code, 0, stderr)
        stats = json.loads(stdout)["stats"]
        summary = stats["summary"]
        self.assertEqual(summary["cleanup_terminal_attempts"], 2)
        self.assertEqual(summary["cleanup_retained_worktrees"], 1)
        self.assertEqual(summary["cleanup_landed_retained_worktrees"], 1)
        self.assertEqual(summary["cleanup_unlanded_terminal_attempts"], 1)
        self.assertEqual(summary["codex_user_turns"], 2)
        self.assertEqual(summary["codex_linked_user_turns"], 1)
        self.assertEqual(summary["codex_unlinked_user_turns"], 1)
        self.assertEqual(summary["codex_implementation_like_unlinked_turns"], 1)
        self.assertEqual(summary["codex_linked_attempts"], 1)
        self.assertEqual(summary["codex_unlinked_attempts"], 1)
        bucket = stats["buckets"][0]
        self.assertEqual(bucket["codex_linked_user_turns"], 1)
        self.assertEqual(bucket["codex_unlinked_user_turns"], 1)
