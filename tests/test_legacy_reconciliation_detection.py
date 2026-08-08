from __future__ import annotations

from contextlib import chdir, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
from unittest.mock import patch

from blackdog import wtam
from blackdog.landing import (
    LANDING_PHASES,
    LandingIntent,
    record_landing_abort,
    record_landing_abort_cleanup,
    record_landing_abort_close_event,
    record_landing_abort_complete,
    record_landing_abort_runtime,
    record_landing_phase,
)
from blackdog_core.backlog import (
    finish_task,
    start_task,
    task_resume_attempt_id,
    upsert_workset,
)
from blackdog_core.profile import load_profile
from blackdog_core.state import (
    ATTEMPT_STATUS_ABANDONED,
    ATTEMPT_STATUS_BLOCKED,
    ATTEMPT_STATUS_FAILED,
    EXECUTION_MODEL_DIRECT_WTAM,
    TaskClaimRecord,
    create_prompt_receipt,
    load_events,
    load_runtime_state,
    merge_workset_runtime,
    now_iso,
    save_runtime_state,
)
from blackdog_cli.main import main as blackdog_main
from tests.core_audit_support import CoreAuditTestCase


WORKSET_ID = "legacy-reconcile"
TASK_ID = "LEG-1"
ACTOR = "legacy owner"
AUTOMATIC_REASON = "Automatically detected canonical legacy landing"
DETECTION_KEYS = frozenset(
    {
        "state",
        "reason_code",
        "reason_detail",
        "candidate_count",
        "candidate_commit",
        "candidate_commits",
        "scan_limit",
        "inspected_commit_count",
        "sentinel_commit",
        "proof",
        "actor_mismatch",
    }
)


class LegacyReconciliationDetectionTests(CoreAuditTestCase):
    """Independent real-Git acceptance contract for bounded legacy detection."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "repo with spaces"
        self.root.mkdir()
        self.init_git_repo(self.root)
        self.write_profile("Legacy reconciliation detection")
        (self.root / ".gitignore").write_text(
            ".blackdog/\n.VE/\n",
            encoding="utf-8",
        )
        self._git("add", ".gitignore", "blackdog.toml")
        self._git("commit", "-q", "-m", "Install Blackdog test profile")
        self.profile = load_profile(self.root)
        upsert_workset(
            self.profile,
            {
                "id": WORKSET_ID,
                "title": "Legacy reconciliation",
                "branch_intent": {
                    "target_branch": "main",
                    "integration_branch": "main",
                },
                "tasks": [
                    {
                        "id": TASK_ID,
                        "title": "Repair historical landing",
                        "intent": "detect but never automatically mutate a historical landing gap",
                    }
                ],
            },
        )

    def _git(
        self,
        *args: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            input=input_text,
            check=check,
            capture_output=True,
            text=True,
        )

    def _git_out(self, *args: str) -> str:
        return self._git(*args).stdout.strip()

    def _run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with chdir(self.root), redirect_stdout(stdout), redirect_stderr(stderr):
            code = blackdog_main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def _start_attempt(
        self,
        *,
        setup_receipt: dict[str, object] | None = None,
        start_commit: str | None = None,
    ):
        resolved_start = start_commit or self._git_out("rev-parse", "main")
        branch = f"agent/{TASK_ID.lower()}-{len(load_events(self.profile.paths.events_file))}"
        attempt = start_task(
            self.profile,
            workset_id=WORKSET_ID,
            task_id=TASK_ID,
            actor=ACTOR,
            branch=branch,
            target_branch="main",
            integration_branch="main",
            start_commit=resolved_start,
            prompt_receipt=create_prompt_receipt(
                "Repair the historical landing without mutating during discovery.",
                source="unit:legacy-reconciliation",
            ),
            setup_receipt=setup_receipt,
        )
        return attempt

    def _source_commit(
        self,
        attempt,
        *,
        path: str = "legacy source.txt",
        content: str = "historical source\n",
    ) -> str:
        assert attempt.start_commit is not None
        assert attempt.branch is not None
        self._git("branch", attempt.branch, attempt.start_commit)
        self._git("checkout", "-q", attempt.branch)
        source_path = self.root / path
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(content, encoding="utf-8")
        self._git("add", "--", path)
        self._git("commit", "-q", "-m", "Prepare historical source")
        source_commit = self._git_out("rev-parse", "HEAD")
        self._git("checkout", "-q", "main")
        return source_commit

    def _candidate_message(
        self,
        attempt,
        *,
        actor: str = ACTOR,
        status: str = "success",
        target_branch: str = "main",
        paths: tuple[str, ...] = ("legacy source.txt",),
        duplicate_status: bool = False,
        commit_format: str | None = None,
    ) -> str:
        lines = [
            (
                "Restore historical landing evidence"
                if commit_format is not None
                else f"blackdog({WORKSET_ID}/{TASK_ID}): Historical landing"
            ),
            "",
            "Git mutation completed before the old runtime ledger was finalized.",
            "",
            f"Blackdog-Workset: {WORKSET_ID}",
            f"Blackdog-Task: {TASK_ID}",
            f"Blackdog-Attempt: {attempt.attempt_id}",
            f"Blackdog-Actor: {actor}",
            f"Blackdog-Status: {status}",
        ]
        if duplicate_status:
            lines.append(f"Blackdog-Status: {status}")
        if commit_format is not None:
            lines.append(f"Blackdog-Commit-Format: {commit_format}")
        lines.append(f"Blackdog-Target-Branch: {target_branch}")
        lines.extend(f"Blackdog-Changed-Path: {path}" for path in paths)
        return "\n".join(lines) + "\n"

    def _canonical_candidate(
        self,
        attempt,
        *,
        source_commit: str,
        actor: str = ACTOR,
        status: str = "success",
        target_branch: str = "main",
        duplicate_status: bool = False,
        commit_format: str | None = None,
        path: str = "legacy source.txt",
        content: str | None = None,
    ) -> str:
        if content is None:
            self._git("cherry-pick", "-n", source_commit)
        else:
            candidate_path = self.root / path
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text(content, encoding="utf-8")
            self._git("add", "--", path)
        self._git(
            "commit",
            "-q",
            "-F",
            "-",
            input_text=self._candidate_message(
                attempt,
                actor=actor,
                status=status,
                target_branch=target_branch,
                paths=(path,),
                duplicate_status=duplicate_status,
                commit_format=commit_format,
            ),
        )
        return self._git_out("rev-parse", "HEAD")

    def _filler_commits(self, count: int, *, prefix: str = "filler") -> None:
        for index in range(count):
            path = f"history/{prefix}-{index:03d}.txt"
            absolute = self.root / path
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.write_text(f"{prefix} {index}\n", encoding="utf-8")
            self._git("add", "--", path)
            self._git("commit", "-q", "-m", f"Historical filler {prefix} {index}")

    def _finish(
        self,
        attempt,
        *,
        source_commit: str | None,
        status: str = ATTEMPT_STATUS_FAILED,
        summary: str = "Historical runtime finalization failed after the Git mutation.",
        landed_commit: str | None = None,
    ):
        return finish_task(
            self.profile,
            workset_id=WORKSET_ID,
            task_id=TASK_ID,
            attempt_id=attempt.attempt_id,
            actor=ACTOR,
            status=status,
            summary=summary,
            changed_paths=("legacy source.txt",),
            commit=source_commit,
            landed_commit=landed_commit,
        )

    def _ready_history(
        self,
        *,
        status: str = ATTEMPT_STATUS_FAILED,
        source_recorded: bool = True,
        candidate_actor: str = ACTOR,
        terminal_summary: str = "Historical runtime finalization failed after the Git mutation.",
    ):
        attempt = self._start_attempt()
        source_commit = self._source_commit(attempt)
        candidate = self._canonical_candidate(
            attempt,
            source_commit=source_commit,
            actor=candidate_actor,
        )
        finished = self._finish(
            attempt,
            source_commit=source_commit if source_recorded else "f" * 40,
            status=status,
            summary=terminal_summary,
        )
        return finished, source_commit, candidate

    def _state_fingerprint(self) -> dict[str, object]:
        index_path = self.root / ".git" / "index"
        return {
            "runtime": self.profile.paths.runtime_file.read_bytes(),
            "events": self.profile.paths.events_file.read_bytes(),
            "head": self._git_out("rev-parse", "HEAD"),
            "main": self._git_out("rev-parse", "main"),
            "refs": self._git_out("show-ref"),
            "status": self._git_out("status", "--porcelain=v2", "--untracked-files=all"),
            "index": index_path.read_bytes(),
            "cached_diff": self._git("diff", "--cached", "--binary").stdout,
            "worktree_diff": self._git("diff", "--binary").stdout,
            "worktrees": self._git_out("worktree", "list", "--porcelain"),
        }

    def _assert_read_only(self, before: dict[str, object]) -> None:
        self.assertEqual(self._state_fingerprint(), before)
        self.assertFalse(
            any(
                str(event.get("type") or "").startswith("task.reconciliation.detect")
                or str(event.get("type") or "").startswith("worktree.reconciliation.detect")
                for event in load_events(self.profile.paths.events_file)
            )
        )

    def _detection(self, payload: dict[str, object]) -> dict[str, object]:
        detection = payload.get("legacy_reconciliation_detection")
        self.assertIsInstance(detection, dict)
        assert isinstance(detection, dict)
        self.assertTrue(set(detection).issubset(DETECTION_KEYS), detection)
        self.assertIn(detection.get("state"), {
            "ready",
            "none",
            "unproven",
            "ambiguous",
            "inconclusive",
            "error",
        })
        self.assertIsInstance(detection.get("reason_code"), str)
        self.assertTrue(str(detection["reason_code"]).strip())
        self.assertLessEqual(len(str(detection["reason_code"])), 96)
        self.assertIsInstance(detection.get("reason_detail"), str)
        self.assertTrue(str(detection["reason_detail"]).strip())
        self.assertLessEqual(len(str(detection["reason_detail"])), 512)
        self.assertIs(type(detection.get("candidate_count")), int)
        self.assertGreaterEqual(int(detection["candidate_count"]), 0)
        self.assertEqual(detection.get("scan_limit"), 64)
        self.assertIs(type(detection.get("inspected_commit_count")), int)
        self.assertGreaterEqual(int(detection["inspected_commit_count"]), 0)
        self.assertLessEqual(int(detection["inspected_commit_count"]), 64)
        self.assertLessEqual(
            int(detection["candidate_count"]),
            int(detection["scan_limit"]),
        )
        candidates = detection.get("candidate_commits")
        self.assertIsInstance(candidates, list)
        assert isinstance(candidates, list)
        self.assertLessEqual(len(candidates), 64)
        self.assertTrue(
            all(isinstance(commit, str) and len(commit) == 40 for commit in candidates)
        )
        sentinel = detection.get("sentinel_commit")
        self.assertTrue(
            sentinel is None or (isinstance(sentinel, str) and len(sentinel) == 40)
        )
        candidate = detection.get("candidate_commit")
        self.assertTrue(candidate is None or (isinstance(candidate, str) and len(candidate) == 40))
        return detection

    def _show_json(self) -> dict[str, object]:
        code, stdout, stderr = self._run_cli(
            "task",
            "show",
            f"--project-root={self.root}",
            f"--workset={WORKSET_ID}",
            f"--task={TASK_ID}",
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        return json.loads(stdout)["task_show"]

    def _assert_no_reconcile_action(self, payload: dict[str, object]) -> None:
        action = payload["next_action"]
        assert isinstance(action, dict)
        self.assertNotIn("reconcile-landing", action.get("argv", []))
        for alternative in action.get("alternatives", []):
            self.assertNotIn("reconcile-landing", alternative.get("argv", []))
        for choice in action.get("choices", []):
            self.assertNotIn("reconcile-landing", choice.get("argv", []))

    def test_latest_failed_and_blocked_candidate_are_ready_and_exact_dry_run_is_read_only(self) -> None:
        for terminal_status in (ATTEMPT_STATUS_FAILED, ATTEMPT_STATUS_BLOCKED):
            with self.subTest(terminal_status=terminal_status):
                if terminal_status == ATTEMPT_STATUS_BLOCKED:
                    # Each iteration needs an independent latest attempt and target history.
                    self.tearDown()
                    self.setUp()
                attempt, _source, candidate = self._ready_history(status=terminal_status)
                before = self._state_fingerprint()

                shown = self._show_json()
                detection = self._detection(shown)
                self.assertEqual(detection["state"], "ready")
                self.assertEqual(detection["candidate_count"], 1)
                self.assertEqual(detection["candidate_commit"], candidate)
                proof = detection["proof"]
                assert isinstance(proof, dict)
                self.assertTrue(proof["reachable_from_target"])
                self.assertTrue(proof["changed_paths_match"])
                self.assertTrue(proof["source_patch_equivalent"])

                next_action = shown["next_action"]
                assert isinstance(next_action, dict)
                expected_argv = [
                    "blackdog",
                    "task",
                    "reconcile-landing",
                    f"--project-root={self.profile.paths.project_root}",
                    f"--workset={WORKSET_ID}",
                    f"--task={TASK_ID}",
                    f"--attempt={attempt.attempt_id}",
                    f"--landed-commit={candidate}",
                    f"--actor={ACTOR}",
                    f"--reason={AUTOMATIC_REASON}",
                ]
                self.assertEqual(next_action["kind"], "command")
                self.assertEqual(next_action["argv"], expected_argv)
                self.assertEqual(next_action["command"], shlex.join(expected_argv))
                self.assertEqual(next_action["safety_class"], "read_only")
                self.assertEqual(next_action["mutation_class"], "none")
                self.assertNotIn("--apply", next_action["argv"])
                self.assertEqual(shown["recommended_commands"], [
                    {
                        "action_id": next_action["action_id"],
                        "argv": expected_argv,
                        "command": shlex.join(expected_argv),
                        "reason": next_action["reason_detail"],
                        "disposition": next_action["disposition"],
                    }
                ])
                self._assert_read_only(before)

                code, stdout, stderr = self._run_cli(*expected_argv[1:])
                self.assertEqual(code, 0, stderr)
                self.assertIn("--apply", stdout)
                self.assertIn("Apply the proven landing reconciliation", stdout)
                self._assert_read_only(before)

    def test_v2_human_first_candidate_is_ready(self) -> None:
        attempt = self._start_attempt()
        source_commit = self._source_commit(attempt)
        candidate = self._canonical_candidate(
            attempt,
            source_commit=source_commit,
            commit_format="2",
        )
        self._finish(attempt, source_commit=source_commit)

        detection = self._detection(self._show_json())
        self.assertEqual(detection["state"], "ready")
        self.assertEqual(detection["candidate_commit"], candidate)
        self.assertEqual(detection["proof"]["commit_format"], 2)

    def test_unsupported_commit_format_candidate_is_unproven(self) -> None:
        attempt = self._start_attempt()
        source_commit = self._source_commit(attempt)
        self._canonical_candidate(
            attempt,
            source_commit=source_commit,
            commit_format="99",
        )
        self._finish(attempt, source_commit=source_commit)

        detection = self._detection(self._show_json())
        self.assertEqual(detection["state"], "unproven")
        self.assertIn("Blackdog-Commit-Format", detection["reason_detail"])

    def test_source_object_missing_is_ready_but_source_patch_mismatch_is_unproven(self) -> None:
        attempt, _source, candidate = self._ready_history(source_recorded=False)
        before = self._state_fingerprint()
        detection = self._detection(self._show_json())
        self.assertEqual(detection["state"], "ready")
        self.assertEqual(detection["candidate_commit"], candidate)
        proof = detection["proof"]
        assert isinstance(proof, dict)
        self.assertIsNone(proof["source_commit"])
        self.assertIsNone(proof["source_patch_equivalent"])
        self._assert_read_only(before)

        self.tearDown()
        self.setUp()
        attempt = self._start_attempt()
        source = self._source_commit(attempt, content="source version\n")
        candidate = self._canonical_candidate(
            attempt,
            source_commit=source,
            content="different candidate version\n",
        )
        self._finish(attempt, source_commit=source)
        before = self._state_fingerprint()
        detection = self._detection(self._show_json())
        self.assertEqual(detection["state"], "unproven")
        self.assertEqual(detection["candidate_count"], 1)
        self.assertEqual(detection["candidate_commit"], candidate)
        self._assert_read_only(before)

    def test_zero_plausible_candidates_is_none_and_two_are_ambiguous_before_proof_selection(self) -> None:
        attempt = self._start_attempt()
        source = self._source_commit(attempt)
        self._filler_commits(3, prefix="none")
        self._finish(attempt, source_commit=source)
        before = self._state_fingerprint()
        payload = self._show_json()
        detection = self._detection(payload)
        self.assertEqual(detection["state"], "none")
        self.assertEqual(detection["candidate_count"], 0)
        self.assertIsNone(detection["candidate_commit"])
        self._assert_no_reconcile_action(payload)
        self._assert_read_only(before)

        self.tearDown()
        self.setUp()
        attempt = self._start_attempt()
        source = self._source_commit(attempt)
        first = self._canonical_candidate(attempt, source_commit=source)
        second_path = "second plausible.txt"
        (self.root / second_path).write_text("second\n", encoding="utf-8")
        self._git("add", "--", second_path)
        self._git(
            "commit",
            "-q",
            "-F",
            "-",
            input_text=self._candidate_message(attempt, paths=(second_path,)),
        )
        second = self._git_out("rev-parse", "HEAD")
        self._finish(attempt, source_commit=source)
        before = self._state_fingerprint()
        payload = self._show_json()
        detection = self._detection(payload)
        self.assertEqual(detection["state"], "ambiguous")
        self.assertEqual(detection["candidate_count"], 2)
        self.assertIsNone(detection["candidate_commit"])
        self.assertNotEqual(first, second)
        self._assert_no_reconcile_action(payload)
        self._assert_read_only(before)

    def test_malformed_duplicate_trailer_and_merge_candidate_are_unproven_not_none(self) -> None:
        attempt = self._start_attempt()
        source = self._source_commit(attempt)
        duplicate = self._canonical_candidate(
            attempt,
            source_commit=source,
            duplicate_status=True,
        )
        self._finish(attempt, source_commit=source)
        before = self._state_fingerprint()
        detection = self._detection(self._show_json())
        self.assertEqual(detection["state"], "unproven")
        self.assertEqual(detection["candidate_count"], 1)
        self.assertEqual(detection["candidate_commit"], duplicate)
        self._assert_read_only(before)

        self.tearDown()
        self.setUp()
        attempt = self._start_attempt()
        source = self._source_commit(attempt, path="merge path.txt")
        side = f"merge-side-{attempt.attempt_id[-6:]}"
        self._git("branch", side, source)
        self._git(
            "merge",
            "--no-ff",
            "-q",
            side,
            "-m",
            self._candidate_message(attempt, paths=("merge path.txt",)),
        )
        merge_candidate = self._git_out("rev-parse", "HEAD")
        self._finish(attempt, source_commit=source)
        before = self._state_fingerprint()
        detection = self._detection(self._show_json())
        self.assertEqual(detection["state"], "unproven")
        self.assertEqual(detection["candidate_commit"], merge_candidate)
        self._assert_read_only(before)

    def test_empty_identity_shaped_candidate_without_source_is_unproven(self) -> None:
        attempt = self._start_attempt()
        self._git(
            "commit",
            "--allow-empty",
            "-q",
            "-F",
            "-",
            input_text=self._candidate_message(attempt, paths=()),
        )
        candidate = self._git_out("rev-parse", "HEAD")
        self._finish(attempt, source_commit=None)
        before = self._state_fingerprint()

        payload = self._show_json()
        detection = self._detection(payload)

        self.assertEqual(detection["state"], "unproven")
        self.assertEqual(detection["candidate_count"], 1)
        self.assertEqual(detection["candidate_commit"], candidate)
        self.assertIn("no changed paths", detection["reason_detail"])
        self._assert_no_reconcile_action(payload)
        self._assert_read_only(before)

    def test_actor_mismatch_requires_exact_terminal_evidence_and_rejects_prose_abuse(self) -> None:
        exact_summary = (
            "Git landing completed, but runtime finalization rejected the deliberately "
            "mismatched actor after mutation."
        )
        attempt, _source, candidate = self._ready_history(
            candidate_actor="different actor",
            terminal_summary=exact_summary,
        )
        before = self._state_fingerprint()
        detection = self._detection(self._show_json())
        self.assertEqual(detection["state"], "ready")
        self.assertEqual(detection["candidate_commit"], candidate)
        proof = detection["proof"]
        assert isinstance(proof, dict)
        self.assertFalse(proof["actor_matches_attempt"])
        self.assertEqual(proof["actor_mismatch_evidence"]["event_actor"], ACTOR)
        self._assert_read_only(before)

        self.tearDown()
        self.setUp()
        contradictory = (
            "Git landing completed, but runtime finalization rejected the deliberately mismatched actor "
            "after mutation; actor mismatch did not occur."
        )
        _attempt, _source, candidate = self._ready_history(
            candidate_actor="different actor",
            terminal_summary=contradictory,
        )
        before = self._state_fingerprint()
        detection = self._detection(self._show_json())
        self.assertEqual(detection["state"], "unproven")
        self.assertEqual(detection["candidate_commit"], candidate)
        self._assert_read_only(before)

    def test_scan_limit_includes_commit_64_and_reports_65_as_inconclusive(self) -> None:
        for filler_count, expected_state in ((63, "ready"), (64, "inconclusive")):
            with self.subTest(commits_after_start=filler_count + 1):
                if filler_count:
                    self.tearDown()
                    self.setUp()
                attempt = self._start_attempt()
                source = self._source_commit(attempt)
                candidate = self._canonical_candidate(attempt, source_commit=source)
                self._filler_commits(filler_count, prefix=f"boundary-{filler_count}")
                self._finish(attempt, source_commit=source)
                before = self._state_fingerprint()
                payload = self._show_json()
                detection = self._detection(payload)
                self.assertEqual(detection["state"], expected_state)
                if expected_state == "ready":
                    self.assertEqual(detection["candidate_commit"], candidate)
                else:
                    self._assert_no_reconcile_action(payload)
                self._assert_read_only(before)

    def test_missing_start_sentinel_is_inconclusive(self) -> None:
        attempt = self._start_attempt(start_commit="e" * 40)
        # Create the actual source from main because the deliberately missing sentinel cannot be checked out.
        actual_start = self._git_out("rev-parse", "main")
        rewritten = replace(attempt, start_commit=actual_start)
        runtime = load_runtime_state(self.profile.paths)
        save_runtime_state(
            self.profile.paths,
            merge_workset_runtime(
                runtime,
                workset_id=WORKSET_ID,
                task_ids={TASK_ID},
                incoming_records=None,
                incoming_attempts=(rewritten,),
            ),
        )
        source = self._source_commit(rewritten)
        candidate = self._canonical_candidate(rewritten, source_commit=source)
        finished = self._finish(rewritten, source_commit=source)
        runtime = load_runtime_state(self.profile.paths)
        save_runtime_state(
            self.profile.paths,
            merge_workset_runtime(
                runtime,
                workset_id=WORKSET_ID,
                task_ids={TASK_ID},
                incoming_records=None,
                incoming_attempts=(replace(finished, start_commit="e" * 40),),
            ),
        )
        before = self._state_fingerprint()
        payload = self._show_json()
        detection = self._detection(payload)
        self.assertEqual(detection["state"], "inconclusive")
        self.assertEqual(detection["candidate_count"], 0)
        self.assertIsNone(detection["candidate_commit"])
        self.assertEqual(candidate, self._git_out("rev-parse", "main"))
        self._assert_no_reconcile_action(payload)
        self._assert_read_only(before)

    def test_operational_git_inspection_error_is_error_not_unproven(self) -> None:
        self._ready_history()
        before = self._state_fingerprint()
        real_run = wtam._run_git_no_check

        def fail_history(repo_root: Path, *args: str):
            if args and args[0] in {"log", "rev-list"} and "--first-parent" in args:
                return subprocess.CompletedProcess(
                    ["git", *args],
                    128,
                    stdout="",
                    stderr="simulated object database failure",
                )
            return real_run(repo_root, *args)

        with patch.object(wtam, "_run_git_no_check", side_effect=fail_history):
            payload = self._show_json()
        detection = self._detection(payload)
        self.assertEqual(detection["state"], "error")
        self.assertNotEqual(detection["state"], "unproven")
        self.assertIn("simulated object database failure", detection["reason_detail"])
        self._assert_no_reconcile_action(payload)
        self._assert_read_only(before)

    def test_git_subprocess_oserror_is_bounded_error_on_show_and_recover(self) -> None:
        self._ready_history()
        before = self._state_fingerprint()
        real_run = wtam._run_git_no_check

        def raise_history(repo_root: Path, *args: str):
            if args and args[0] == "rev-list" and "--first-parent" in args:
                raise OSError("simulated detector subprocess launch failure")
            return real_run(repo_root, *args)

        with patch.object(wtam, "_run_git_no_check", side_effect=raise_history):
            shown = self._show_json()
            code, stdout, stderr = self._run_cli(
                "task",
                "recover",
                f"--project-root={self.root}",
                f"--workset={WORKSET_ID}",
                f"--task={TASK_ID}",
                "--json",
            )
        self.assertEqual(code, 0, stderr)
        recovered = json.loads(stdout)["recovery"]
        for payload in (shown, recovered):
            detection = self._detection(payload)
            self.assertEqual(detection["state"], "error")
            self.assertIn("simulated detector subprocess launch failure", detection["reason_detail"])
            self._assert_no_reconcile_action(payload)
        self._assert_read_only(before)

    def test_ineligible_attempt_and_ownership_states_never_scan_or_offer_reconciliation(self) -> None:
        # Active attempt.
        attempt = self._start_attempt()
        source = self._source_commit(attempt)
        candidate = self._canonical_candidate(attempt, source_commit=source)
        before = self._state_fingerprint()
        payload = self._show_json()
        detection = self._detection(payload)
        self.assertEqual(detection["state"], "none")
        self.assertEqual(detection["candidate_count"], 0)
        self._assert_no_reconcile_action(payload)
        self._assert_read_only(before)

        # Terminal task with a claim.
        self._finish(attempt, source_commit=source)
        runtime = load_runtime_state(self.profile.paths)
        claimed = merge_workset_runtime(
            runtime,
            workset_id=WORKSET_ID,
            task_ids={TASK_ID},
            incoming_records=None,
            incoming_task_claims=(
                TaskClaimRecord(
                    task_id=TASK_ID,
                    actor=ACTOR,
                    execution_model=EXECUTION_MODEL_DIRECT_WTAM,
                    claimed_at=now_iso(),
                    attempt_id=attempt.attempt_id,
                ),
            ),
        )
        save_runtime_state(self.profile.paths, claimed)
        before = self._state_fingerprint()
        payload = self._show_json()
        detection = self._detection(payload)
        self.assertEqual(detection["state"], "none")
        self.assertEqual(detection["candidate_count"], 0)
        self.assertEqual(candidate, self._git_out("rev-parse", "main"))
        self._assert_no_reconcile_action(payload)
        self._assert_read_only(before)

    def test_later_attempt_landed_commit_abandoned_and_adoption_marker_are_excluded(self) -> None:
        # A later attempt makes the historical candidate ineligible.
        first, _source, candidate = self._ready_history()
        runtime_before_resume = load_runtime_state(self.profile.paths)
        task_state = next(
            state
            for workset in runtime_before_resume.worksets
            if workset.workset_id == WORKSET_ID
            for state in workset.task_states
            if state.task_id == TASK_ID
        )
        assert first.prompt_receipt is not None
        assert first.user_prompt_receipt is not None
        assert first.prompt_receipt.mode is not None
        assert first.user_prompt_receipt.mode is not None
        assert task_state.updated_at is not None
        second_attempt_id = task_resume_attempt_id(
            workset_id=WORKSET_ID,
            task_id=TASK_ID,
            predecessor_attempt_id=first.attempt_id,
            actor=ACTOR,
            execution_prompt_hash=first.prompt_receipt.prompt_hash,
            execution_prompt_mode=first.prompt_receipt.mode,
            request_prompt_hash=first.user_prompt_receipt.prompt_hash,
            request_prompt_mode=first.user_prompt_receipt.mode,
        )
        second = start_task(
            self.profile,
            workset_id=WORKSET_ID,
            task_id=TASK_ID,
            actor=ACTOR,
            branch="agent/later-attempt",
            target_branch="main",
            integration_branch="main",
            start_commit=self._git_out("rev-parse", "main"),
            prompt_receipt=first.prompt_receipt,
            user_prompt_receipt=first.user_prompt_receipt,
            attempt_id=second_attempt_id,
            expected_predecessor_attempt_id=first.attempt_id,
            atomic_start_kind="resume",
            expected_task_actor=task_state.actor or first.actor,
            expected_execution_prompt_hash=first.prompt_receipt.prompt_hash,
            expected_execution_prompt_mode=first.prompt_receipt.mode,
            expected_request_prompt_hash=first.user_prompt_receipt.prompt_hash,
            expected_request_prompt_mode=first.user_prompt_receipt.mode,
            expected_task_updated_at=task_state.updated_at,
        )
        second_finished = self._finish(
            second,
            source_commit=None,
            status=ATTEMPT_STATUS_BLOCKED,
        )
        # Runtime timestamps are intentionally second-resolution.  Exact ties
        # must preserve durable append order, never random attempt-id order.
        runtime = load_runtime_state(self.profile.paths)
        save_runtime_state(
            self.profile.paths,
            merge_workset_runtime(
                runtime,
                workset_id=WORKSET_ID,
                task_ids={TASK_ID},
                incoming_records=None,
                incoming_attempts=(
                    replace(
                        second_finished,
                        started_at=first.started_at,
                        ended_at=first.ended_at,
                    ),
                ),
            ),
        )
        before = self._state_fingerprint()
        payload = self._show_json()
        detection = self._detection(payload)
        self.assertEqual(detection["state"], "none")
        self.assertEqual(first.attempt_id != second.attempt_id, True)
        self.assertEqual(candidate, self._git_out("rev-parse", "main"))
        self._assert_no_reconcile_action(payload)
        self._assert_read_only(before)

        for exclusion in ("landed_commit", "abandoned", "adoption"):
            with self.subTest(exclusion=exclusion):
                self.tearDown()
                self.setUp()
                attempt, _source, candidate = self._ready_history(
                    status=(
                        ATTEMPT_STATUS_ABANDONED
                        if exclusion == "abandoned"
                        else ATTEMPT_STATUS_FAILED
                    )
                )
                runtime = load_runtime_state(self.profile.paths)
                replacement = attempt
                if exclusion == "landed_commit":
                    replacement = replace(attempt, landed_commit=candidate)
                elif exclusion == "adoption":
                    replacement = replace(
                        attempt,
                        setup_receipt={"workspace_adoption": {"marker": "owned"}},
                    )
                save_runtime_state(
                    self.profile.paths,
                    merge_workset_runtime(
                        runtime,
                        workset_id=WORKSET_ID,
                        task_ids={TASK_ID},
                        incoming_records=None,
                        incoming_attempts=(replacement,),
                    ),
                )
                before = self._state_fingerprint()
                payload = self._show_json()
                detection = self._detection(payload)
                self.assertEqual(detection["state"], "none")
                self.assertEqual(detection["candidate_count"], 0)
                self._assert_no_reconcile_action(payload)
                self._assert_read_only(before)

    def _record_native_transaction(self, attempt, *, outcome: str) -> None:
        assert attempt.start_commit is not None
        assert attempt.branch is not None
        source = attempt.commit or attempt.start_commit
        intent = LandingIntent(
            workset_id=WORKSET_ID,
            task_id=TASK_ID,
            attempt_id=attempt.attempt_id,
            actor=ACTOR,
            branch=attempt.branch,
            target_branch="main",
            worktree_path=str(self.root.parent / "native source"),
            primary_worktree=str(self.root),
            target_base_commit=attempt.start_commit,
            source_head_commit=source,
            source_fingerprint="native-source-fingerprint",
            expected_source_tree_hash="native-source-tree",
            source_dirty=False,
            summary="Native landing transaction owns this attempt.",
            note=None,
            validations=(),
            residuals=(),
            followup_candidates=(),
            changed_paths=("legacy source.txt",),
            cleanup=False,
            commit_message="native landing\n",
            temporary_worktree_path=str(self.root.parent / "native temp"),
        )
        record_landing_phase(
            self.profile,
            intent=intent,
            phase="intent_recorded",
            data=intent.to_dict(),
        )
        if outcome == "landed_complete":
            for phase in LANDING_PHASES[1:]:
                record_landing_phase(
                    self.profile,
                    intent=intent,
                    phase=phase,
                    data={"owned_by": "native-landing", "phase": phase},
                )
            return
        record_landing_abort(
            self.profile,
            intent=intent,
            data={"landed_commit": self._git_out("rev-parse", "main")},
        )
        record_landing_abort_cleanup(self.profile, intent=intent, data={"cleanup": True})
        record_landing_abort_runtime(self.profile, intent=intent, data={"runtime": True})
        record_landing_abort_close_event(self.profile, intent=intent, data={"close": True})
        record_landing_abort_complete(self.profile, intent=intent, data={"complete": True})

    def test_terminal_native_landed_and_abort_transactions_exclude_legacy_scan(self) -> None:
        for outcome in ("landed_complete", "abort_complete"):
            with self.subTest(outcome=outcome):
                if outcome == "abort_complete":
                    self.tearDown()
                    self.setUp()
                attempt, _source, _candidate = self._ready_history()
                self._record_native_transaction(attempt, outcome=outcome)
                before = self._state_fingerprint()
                payload = self._show_json()
                detection = self._detection(payload)
                self.assertEqual(detection["state"], "none")
                self.assertEqual(detection["candidate_count"], 0)
                if outcome == "landed_complete":
                    self._assert_no_reconcile_action(payload)
                else:
                    action = payload["next_action"]
                    assert isinstance(action, dict)
                    self.assertEqual(
                        action["reason_code"],
                        "abort_complete_target_contains_candidate",
                    )
                    self.assertFalse(
                        any(AUTOMATIC_REASON in value for value in action.get("argv", []))
                    )
                self._assert_read_only(before)

    def test_task_show_recover_and_cli_worktree_show_opt_in_but_internal_payload_defaults_off(self) -> None:
        _attempt, _source, candidate = self._ready_history()
        before = self._state_fingerprint()

        shown = self._show_json()
        self.assertEqual(self._detection(shown)["state"], "ready")

        code, stdout, stderr = self._run_cli(
            "task",
            "recover",
            f"--project-root={self.root}",
            f"--workset={WORKSET_ID}",
            f"--task={TASK_ID}",
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        recovered = json.loads(stdout)["recovery"]
        self.assertEqual(self._detection(recovered)["candidate_commit"], candidate)

        code, stdout, stderr = self._run_cli(
            "worktree",
            "show",
            f"--project-root={self.root}",
            f"--workset={WORKSET_ID}",
            f"--task={TASK_ID}",
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        worktree_shown = json.loads(stdout)["worktree_show"]
        self.assertEqual(self._detection(worktree_shown)["candidate_commit"], candidate)

        with patch.object(
            wtam,
            "_detect_legacy_landing_reconciliation",
            side_effect=AssertionError("internal path invoked legacy detector"),
        ):
            internal = wtam._task_recovery_payload(
                self.profile,
                workset_id=WORKSET_ID,
                task_id=TASK_ID,
            )
            directly_inspected = wtam.inspect_task_worktree(
                self.profile,
                workset_id=WORKSET_ID,
                task_id=TASK_ID,
            )
            table = wtam.build_worktree_table(self.profile)
        self.assertNotIn("legacy_reconciliation_detection", internal)
        self.assertNotIn("legacy_reconciliation_detection", directly_inspected)
        self.assertTrue(
            all("legacy_reconciliation_detection" not in row for row in table["rows"])
        )
        self._assert_read_only(before)

    def test_mutating_stale_claim_recovery_does_not_invoke_detector(self) -> None:
        attempt, _source, _candidate = self._ready_history()
        runtime = load_runtime_state(self.profile.paths)
        save_runtime_state(
            self.profile.paths,
            merge_workset_runtime(
                runtime,
                workset_id=WORKSET_ID,
                task_ids={TASK_ID},
                incoming_records=None,
                incoming_task_claims=(
                    TaskClaimRecord(
                        task_id=TASK_ID,
                        actor=ACTOR,
                        execution_model=EXECUTION_MODEL_DIRECT_WTAM,
                        claimed_at=now_iso(),
                        attempt_id=attempt.attempt_id,
                    ),
                ),
            ),
        )

        with patch.object(
            wtam,
            "_detect_legacy_landing_reconciliation",
            side_effect=AssertionError("mutation path invoked legacy detector"),
        ):
            code, stdout, stderr = self._run_cli(
                "task",
                "recover",
                f"--project-root={self.root}",
                f"--workset={WORKSET_ID}",
                f"--task={TASK_ID}",
                "--release-stale-claim",
                "--status=blocked",
                "--summary=Release only the stale claim without scanning Git history",
                "--json",
            )

        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)["recovery"]
        self.assertTrue(payload["released_stale_claim"])
        self.assertNotIn("legacy_reconciliation_detection", payload)
        runtime_after = load_runtime_state(self.profile.paths)
        workset = next(row for row in runtime_after.worksets if row.workset_id == WORKSET_ID)
        self.assertEqual(workset.task_claims, ())

    def test_text_and_json_expose_one_coherent_bounded_action(self) -> None:
        _attempt, _source, candidate = self._ready_history()
        before = self._state_fingerprint()
        payload = self._show_json()
        detection = self._detection(payload)
        self.assertEqual(detection["candidate_commit"], candidate)
        self.assertEqual(payload["next_action"]["kind"], "command")
        self.assertEqual(payload["next_action"]["alternatives"], [])
        self.assertEqual(payload["next_action"]["choices"], [])

        code, stdout, stderr = self._run_cli(
            "task",
            "show",
            f"--project-root={self.root}",
            f"--workset={WORKSET_ID}",
            f"--task={TASK_ID}",
        )
        self.assertEqual(code, 0, stderr)
        self.assertIn("Legacy reconciliation detection: ready", stdout)
        self.assertIn(candidate, stdout)
        self.assertIn(shlex.join(payload["next_action"]["argv"]), stdout)
        self.assertEqual(stdout.count("reconcile-landing"), 1)
        self._assert_read_only(before)


if __name__ == "__main__":
    import unittest

    unittest.main()
