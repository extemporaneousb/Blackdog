from __future__ import annotations

from contextlib import chdir, nullcontext, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest.mock import patch

from blackdog_core.codex_sessions import build_codex_coverage, build_codex_history
from blackdog_core.profile import load_profile
from blackdog_core.runtime_model import load_runtime_model
from blackdog_core.snapshot import build_runtime_snapshot
from blackdog_core.state import load_events
from blackdog_cli.main import main as blackdog_main
from tests.core_audit_support import CoreAuditTestCase


def _write_session(
    home: Path,
    *,
    thread_id: str,
    cwd: Path,
    turns: tuple[dict[str, object], ...],
) -> Path:
    path = home / "sessions" / "2026" / "07" / "16" / f"rollout-2026-07-16T12-00-00-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = [
        {
            "timestamp": "2026-07-16T19:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "timestamp": "2026-07-16T19:00:00Z",
                "cwd": str(cwd),
                "originator": "Codex Desktop",
                "model_provider": "openai",
            },
        }
    ]
    for index, turn in enumerate(turns):
        turn_id = str(turn["turn_id"])
        timestamp = f"2026-07-16T19:{index:02d}:00Z"
        rows.extend(
            (
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn_id, "started_at": timestamp},
                },
                {
                    "timestamp": timestamp,
                    "type": "turn_context",
                    "payload": {
                        "turn_id": turn_id,
                        "cwd": str(cwd),
                        "model": "gpt-5",
                        "effort": "high",
                    },
                },
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": str(turn["message"])},
                },
            )
        )
        if bool(turn.get("completed")):
            rows.append(
                {
                    "timestamp": timestamp,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn_id,
                        "completed_at": timestamp,
                        "duration_ms": 1_000 + index,
                    },
                }
            )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


class CrossRepoCodexAttributionIntegrationTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        profile_path = self.write_profile("Cross Repo Attribution")
        profile_text = profile_path.read_text(encoding="utf-8")
        profile_text = "handlers = []\n\n" + profile_text.partition("[[handlers]]")[0].rstrip() + "\n"
        profile_path.write_text(profile_text, encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "blackdog.toml"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Add Blackdog profile"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.profile = load_profile(self.root)
        self.source_tmp = tempfile.TemporaryDirectory()
        self.source_root = Path(self.source_tmp.name)
        self.codex_home = self.source_root / ".codex"
        self.worktrees: list[tuple[Path, str]] = []

    def tearDown(self) -> None:
        for worktree_path, branch in reversed(self.worktrees):
            subprocess.run(
                ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(self.root), "branch", "-D", branch],
                check=False,
                capture_output=True,
                text=True,
            )
        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "prune"],
            check=False,
            capture_output=True,
            text=True,
        )
        shutil.rmtree(self.profile.paths.worktrees_dir, ignore_errors=True)
        self.source_tmp.cleanup()
        super().tearDown()

    def _run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with chdir(self.source_root), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = blackdog_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def _begin(
        self,
        *,
        case: str,
        thread_id: str,
        execution_prompt: str,
        request_prompt: str,
        capture_error: bool = False,
    ):
        execution_path = self.source_root / f"{case}-execution.md"
        request_path = self.source_root / f"{case}-request.md"
        execution_path.write_text(execution_prompt, encoding="utf-8")
        request_path.write_text(request_prompt, encoding="utf-8")
        capture_context = (
            patch("blackdog.wtam.current_codex_session_ref", side_effect=RuntimeError("capture failed"))
            if capture_error
            else nullcontext()
        )
        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_HOME": str(self.codex_home),
                    "CODEX_THREAD_ID": thread_id,
                    "BLACKDOG_HOME": str(self.source_root / ".blackdog-home"),
                },
                clear=False,
            ),
            capture_context,
        ):
            exit_code, stdout, stderr = self._run_cli(
                "task",
                "begin",
                "--project-root",
                str(self.root),
                "--actor",
                "codex",
                "--execution-prompt-file",
                str(execution_path),
                "--request-file",
                str(request_path),
                "--prompt-mode",
                "raw",
                "--json",
            )
        self.assertEqual(exit_code, 0, stderr)
        task_payload = json.loads(stdout)["task"]
        worktree_payload = task_payload["worktree"]
        self.worktrees.append((Path(worktree_payload["worktree_path"]), worktree_payload["branch"]))
        attempt_id = worktree_payload["attempt_id"]
        model = load_runtime_model(self.profile)
        return next(
            attempt
            for workset in model.worksets
            for attempt in workset.attempts
            if attempt.attempt_id == attempt_id
        )

    def test_normal_begin_captures_only_the_foreign_invoking_turn_and_missingness_never_blocks(self) -> None:
        execution_prompt = "Implement the target change from the reusable execution prompt."
        _write_session(
            self.codex_home,
            thread_id="thread-cross-repo",
            cwd=self.source_root,
            turns=(
                {
                    "turn_id": "turn-completed-sibling",
                    "message": execution_prompt,
                    "completed": True,
                },
                {
                    "turn_id": "turn-invoking",
                    "message": "Wrapper-composed live request whose hash differs from both receipts.",
                    "completed": False,
                },
            ),
        )
        captured = self._begin(
            case="captured",
            thread_id="thread-cross-repo",
            execution_prompt=execution_prompt,
            request_prompt="Please make the target repository change.",
        )
        self.assertEqual(captured.codex_session.capture_status, "captured")
        self.assertEqual(captured.codex_session.capture_method, "exact_active_turn")
        self.assertEqual(captured.codex_session.turn_id, "turn-invoking")

        ambiguous_prompt = "A repeated completed prompt cannot identify this invocation."
        _write_session(
            self.codex_home,
            thread_id="thread-ambiguous",
            cwd=self.source_root,
            turns=(
                {"turn_id": "turn-ambiguous-a", "message": ambiguous_prompt, "completed": True},
                {"turn_id": "turn-ambiguous-b", "message": ambiguous_prompt, "completed": True},
            ),
        )
        ambiguous = self._begin(
            case="ambiguous",
            thread_id="thread-ambiguous",
            execution_prompt=ambiguous_prompt,
            request_prompt=ambiguous_prompt,
        )
        self.assertEqual(ambiguous.codex_session.capture_status, "missing")
        self.assertEqual(ambiguous.codex_session.capture_missing_reason, "prompt_hash_ambiguous")

        _write_session(
            self.codex_home,
            thread_id="thread-no-open",
            cwd=self.source_root,
            turns=(
                {"turn_id": "turn-no-open", "message": "Unrelated completed work.", "completed": True},
            ),
        )
        no_open = self._begin(
            case="no-open",
            thread_id="thread-no-open",
            execution_prompt="A new prompt with no current open turn.",
            request_prompt="A different request with no current open turn.",
        )
        self.assertEqual(no_open.codex_session.capture_status, "missing")
        self.assertEqual(no_open.codex_session.capture_missing_reason, "no_open_turn")

        _write_session(
            self.codex_home,
            thread_id="thread-error",
            cwd=self.source_root,
            turns=(
                {"turn_id": "turn-error", "message": "Live error fixture.", "completed": False},
            ),
        )
        capture_error = self._begin(
            case="error",
            thread_id="thread-error",
            execution_prompt="Capture adapter error fixture.",
            request_prompt="Capture adapter error fixture request.",
            capture_error=True,
        )
        self.assertEqual(capture_error.codex_session.capture_status, "missing")
        self.assertEqual(capture_error.codex_session.capture_missing_reason, "capture_error")

        runtime_payload = json.loads(self.profile.paths.runtime_file.read_text(encoding="utf-8"))
        persisted_attempts = {
            row["attempt_id"]: row
            for workset in runtime_payload["worksets"]
            for row in workset["attempts"]
        }
        self.assertEqual(
            persisted_attempts[captured.attempt_id]["codex_session"]["capture"],
            {"method": "exact_active_turn", "missing_reason": None, "status": "captured"},
        )
        self.assertEqual(
            persisted_attempts[capture_error.attempt_id]["codex_session"]["capture"],
            {"method": None, "missing_reason": "capture_error", "status": "missing"},
        )
        snapshot_attempts = {
            row["attempt_id"]: row
            for row in build_runtime_snapshot(self.profile)["runtime_model"]["attempts"]
        }
        self.assertEqual(snapshot_attempts[captured.attempt_id]["codex_capture_status"], "captured")
        self.assertEqual(snapshot_attempts[captured.attempt_id]["codex_capture_method"], "exact_active_turn")
        self.assertEqual(
            snapshot_attempts[capture_error.attempt_id]["codex_capture_missing_reason"],
            "capture_error",
        )

        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = tuple(load_events(self.profile.paths.events_file))
        with patch.dict(
            os.environ,
            {
                "CODEX_HOME": str(self.codex_home),
                "BLACKDOG_HOME": str(self.source_root / ".blackdog-home"),
            },
            clear=False,
        ):
            coverage = build_codex_coverage(self.profile)
            history = build_codex_history(self.profile)

        self.assertEqual([row["turn_id"] for row in coverage["turns"]], ["turn-invoking"])
        self.assertNotIn("turn-completed-sibling", {row["turn_id"] for row in coverage["turns"]})
        self.assertEqual(coverage["turns"][0]["linked_attempt_ids"], [captured.attempt_id])
        self.assertEqual(coverage["relationship_counts"], {"launch_turn": 1})
        self.assertEqual(
            coverage["exact_reference_resolution_counts"],
            {"capture_missing": 3, "resolved": 1},
        )
        referenced_attempts = sum(
            row.get("codex_session") is not None for row in persisted_attempts.values()
        )
        self.assertEqual(
            sum(coverage["exact_reference_resolution_counts"].values()),
            referenced_attempts,
        )
        attempts = {row["attempt_id"]: row for row in coverage["attempts"]}
        self.assertEqual(
            sum(row["exact_reference_resolution"] is not None for row in attempts.values()),
            referenced_attempts,
        )
        self.assertEqual(attempts[captured.attempt_id]["codex_capture_method"], "exact_active_turn")
        self.assertEqual(attempts[captured.attempt_id]["exact_reference_resolution"], "resolved")
        self.assertEqual(attempts[ambiguous.attempt_id]["codex_capture_missing_reason"], "prompt_hash_ambiguous")
        self.assertEqual(attempts[no_open.attempt_id]["codex_capture_missing_reason"], "no_open_turn")
        self.assertEqual(attempts[capture_error.attempt_id]["codex_capture_missing_reason"], "capture_error")
        history_turns = [row for row in history["rows"] if row["kind"] == "codex_turn"]
        self.assertEqual([row["codex_turn_id"] for row in history_turns], ["turn-invoking"])
        self.assertEqual(
            history["exact_reference_resolution_counts"],
            coverage["exact_reference_resolution_counts"],
        )
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(tuple(load_events(self.profile.paths.events_file)), events_before)
