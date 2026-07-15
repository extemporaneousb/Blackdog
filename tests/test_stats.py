from __future__ import annotations

from contextlib import chdir, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from blackdog.repo_lifecycle import RepoLifecycleError
from blackdog.stats import build_stats
from blackdog_core.backlog import finish_task, set_task_runtime_status, start_task, upsert_workset
from blackdog_core.codex_sessions import build_codex_coverage as build_core_codex_coverage
from blackdog_core.profile import render_default_profile
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

    def test_stats_windows_runtime_and_codex_attempts_by_started_at(self) -> None:
        profile = self.load_test_profile()
        foreign_root = self.root.parent / "source-repo"
        inside_session = _write_stats_session(
            self.codex_home,
            cwd=foreign_root,
            thread_id="thread-started-inside",
            turn_id="turn-started-inside",
            started_at="2026-06-02T12:00:00+00:00",
        )
        before_session = _write_stats_session(
            self.codex_home,
            cwd=foreign_root,
            thread_id="thread-started-before",
            turn_id="turn-started-before",
            started_at="2026-06-01T12:00:00+00:00",
        )
        upsert_workset(
            profile,
            {
                "id": "stats-window-anchor",
                "title": "Stats window anchor",
                "tasks": [
                    {"id": "SWA-1", "title": "Starts inside and ends after"},
                    {"id": "SWA-2", "title": "Starts before and ends inside"},
                ],
            },
        )
        receipt = prompt_receipt_reference(
            create_prompt_receipt("Implement stats window anchor coverage.")
        )
        with patch(
            "blackdog_core.backlog.now_iso",
            side_effect=["2026-06-02T12:00:00+00:00", "2026-06-04T12:00:00+00:00"],
        ):
            inside_attempt = start_task(
                profile,
                workset_id="stats-window-anchor",
                task_id="SWA-1",
                actor="codex",
                prompt_receipt=receipt,
                codex_session=CodexSessionRefRecord(
                    thread_id="thread-started-inside",
                    session_path=str(inside_session.relative_to(self.codex_home)),
                    turn_id="turn-started-inside",
                ),
            )
            finish_task(
                profile,
                workset_id="stats-window-anchor",
                task_id="SWA-1",
                attempt_id=inside_attempt.attempt_id,
                actor="codex",
                status="success",
                summary="finished after the window",
            )
        with patch(
            "blackdog_core.backlog.now_iso",
            side_effect=["2026-06-01T12:00:00+00:00", "2026-06-02T12:00:00+00:00"],
        ):
            before_attempt = start_task(
                profile,
                workset_id="stats-window-anchor",
                task_id="SWA-2",
                actor="codex",
                prompt_receipt=receipt,
                codex_session=CodexSessionRefRecord(
                    thread_id="thread-started-before",
                    session_path=str(before_session.relative_to(self.codex_home)),
                    turn_id="turn-started-before",
                ),
            )
            finish_task(
                profile,
                workset_id="stats-window-anchor",
                task_id="SWA-2",
                attempt_id=before_attempt.attempt_id,
                actor="codex",
                status="failed",
                summary="finished inside the window",
            )

        coverage_calls: list[tuple[dict[str, object], dict[str, object]]] = []

        def capture_coverage(*args, **kwargs):
            payload = build_core_codex_coverage(*args, **kwargs)
            coverage_calls.append((dict(kwargs), payload))
            return payload

        with (
            patch.dict(
                os.environ,
                {"CODEX_HOME": str(self.codex_home), "BLACKDOG_HOME": str(self.blackdog_home)},
                clear=False,
            ),
            patch("blackdog.stats.build_codex_coverage", side_effect=capture_coverage),
        ):
            result = build_stats(
                project_roots=(self.root,),
                since="2026-06-02",
                until="2026-06-03",
                timezone_name="UTC",
            )

        self.assertEqual(len(coverage_calls), 1)
        coverage_kwargs, coverage = coverage_calls[0]
        self.assertEqual(coverage_kwargs["attempt_window_anchor"], "started_at")
        self.assertEqual(coverage["counts"]["blackdog_attempts"], 1)
        self.assertEqual(coverage["counts"]["linked_attempts"], 1)
        self.assertEqual(coverage["counts"]["unlinked_attempts"], 0)
        self.assertEqual(coverage["exact_reference_resolution_counts"], {"resolved": 1})
        self.assertEqual([row["attempt_id"] for row in coverage["attempts"]], [inside_attempt.attempt_id])

        summary = result.summary
        self.assertEqual(summary["attempts_total"], 2)
        self.assertEqual(summary["completed_attempts"], 1)
        self.assertEqual(summary["success_attempts"], 1)
        self.assertEqual(summary["failed_attempts"], 0)
        self.assertEqual(summary["codex_linked_attempts"], 1)
        self.assertEqual(summary["codex_unlinked_attempts"], 0)
        self.assertEqual(summary["codex_linked_user_turns"], 1)
        self.assertEqual(summary["codex_unlinked_user_turns"], 0)
        self.assertEqual(len(result.buckets), 1)
        bucket = result.buckets[0]
        self.assertEqual(bucket["bucket"], "2026-06-02")
        self.assertEqual(bucket["attempts_started"], 1)
        self.assertEqual(bucket["completed_attempts"], 1)
        self.assertEqual(bucket["success_attempts"], 1)
        self.assertEqual(bucket["failed_attempts"], 0)
        self.assertEqual(bucket["codex_user_turns"], 1)
        self.assertEqual(bucket["codex_linked_user_turns"], 1)

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

    def test_stats_discovers_profiles_without_mutating_or_requiring_registry(self) -> None:
        with patch("blackdog.stats.registered_project_roots", return_value=()):
            result = build_stats(discovery_roots=(self.root,))

        payload = result.to_dict()
        self.assertEqual(payload["scope_source"], "discovery_roots")
        self.assertEqual(payload["discovery_roots"], [str(self.root.resolve())])
        self.assertEqual(payload["project_roots"], [str(self.root.resolve())])

        deduped_result = build_stats(discovery_roots=(self.root, self.root / "blackdog.toml"))
        self.assertEqual(deduped_result.project_roots, (str(self.root.resolve()),))
        self.assertEqual(deduped_result.deduped_project_roots, (str(self.root.resolve()),))

        exit_code, stdout, stderr = self.run_cli("stats", "--root", str(self.root), "--json")
        self.assertEqual(exit_code, 0, stderr)
        cli_payload = json.loads(stdout)["stats"]
        self.assertEqual(cli_payload["scope_source"], "discovery_roots")
        self.assertEqual(cli_payload["discovery_roots"], [str(self.root.resolve())])
        self.assertEqual(cli_payload["project_roots"], [str(self.root.resolve())])

        with patch("blackdog.stats.registered_project_roots", return_value=(self.root,)):
            registry_result = build_stats()
        self.assertEqual(registry_result.scope_source, "registry")
        self.assertEqual(registry_result.project_roots, (str(self.root.resolve()),))

        explicit_result = build_stats(project_roots=(self.root,))
        self.assertEqual(explicit_result.scope_source, "explicit_project_roots")
        self.assertEqual(explicit_result.discovery_roots, ())

    def test_stats_discovery_rejects_ambiguous_or_invalid_scope(self) -> None:
        with self.assertRaisesRegex(RepoLifecycleError, "either --project-root or --root"):
            build_stats(project_roots=(self.root,), discovery_roots=(self.root,))

        missing = self.root / "missing-fleet"
        with self.assertRaisesRegex(RepoLifecycleError, "discovery root does not exist"):
            build_stats(discovery_roots=(missing,))

        malformed_root = self.root / "malformed-fleet" / "bad-repo"
        malformed_root.mkdir(parents=True)
        (malformed_root / "blackdog.toml").write_text("[project\n", encoding="utf-8")
        with self.assertRaisesRegex(RepoLifecycleError, "is not a Blackdog repo"):
            build_stats(discovery_roots=(malformed_root.parent,))

    def test_stats_deduplicates_shared_codex_turns_only_in_fleet_aggregates(self) -> None:
        other_root = self.root / "other-repo"
        other_root.mkdir()
        self.init_git_repo(other_root)
        (other_root / "blackdog.toml").write_text(render_default_profile("Other Stats"), encoding="utf-8")
        shared_turn = {
            "thread_id": "shared-thread",
            "session_path": "sessions/shared.jsonl",
            "turn_id": "shared-turn",
            "turn_index": 0,
            "started_at": "2026-06-03T01:30:00+00:00",
            "user_message_hash": "hash",
            "classification": "implementation_likely",
            "linked_attempt_ids": [],
            "tool_call_count": 2,
            "input_tokens": 10,
            "cached_input_tokens": 2,
            "output_tokens": 3,
            "reasoning_output_tokens": 4,
            "total_tokens": 13,
        }

        def coverage_for(profile, **_kwargs):
            self.assertFalse(_kwargs["include_environment_evidence"])
            turn = dict(shared_turn)
            if profile.project_name == "Stats Demo":
                turn["linked_attempt_ids"] = ["attempt-in-stats-demo"]
                turn["classification"] = "blackdog_attempt"
            return {
                "counts": {"codex_sessions": 1, "linked_attempts": 0, "unlinked_attempts": 0},
                "turns": [turn],
            }

        with (
            patch("blackdog.stats.collect_codex_turns", return_value=()),
            patch("blackdog.stats.build_codex_coverage", side_effect=coverage_for),
        ):
            result = build_stats(project_roots=(self.root, other_root), timezone_name="UTC")

        self.assertEqual(result.summary["codex_sessions"], 1)
        self.assertEqual(result.summary["codex_user_turns"], 1)
        self.assertEqual(result.summary["codex_linked_user_turns"], 1)
        self.assertEqual(result.summary["codex_unlinked_user_turns"], 0)
        self.assertEqual(result.summary["codex_tool_calls"], 2)
        self.assertEqual(result.summary["codex_total_tokens"], 13)
        repos = {str(row["project_name"]): row for row in result.repos}
        self.assertEqual(repos["Stats Demo"]["codex_linked_user_turns"], 1)
        self.assertEqual(repos["Other Stats"]["codex_unlinked_user_turns"], 1)
        self.assertEqual(result.buckets[0]["repos"], 2)
        self.assertEqual(result.buckets[0]["codex_user_turns"], 1)
        self.assertEqual(result.buckets[0]["codex_tool_calls"], 2)
        self.assertEqual(result.buckets[0]["codex_total_tokens"], 13)

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
