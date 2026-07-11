from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from blackdog_core import codex_sessions as codex_sessions_module
from blackdog_core.backlog import finish_task, start_task, upsert_workset
from blackdog_core.codex_sessions import (
    RELATIONSHIP_HOOK_CONTEXT,
    build_codex_coverage,
    build_codex_history,
    codex_session_cache_path,
    codex_task_context_path,
    collect_codex_turns,
    current_codex_session_ref,
    load_codex_task_context_stamps,
    read_codex_session,
)
from blackdog_core.state import CodexSessionRefRecord, append_event, create_prompt_receipt, prompt_receipt_reference
from tests.core_audit_support import CoreAuditTestCase


def _write_session(
    home: Path,
    *,
    thread_id: str,
    cwd: Path,
    turn_id: str,
    message: str,
    session_dir: str = "sessions",
    duration_ms: int | None = 42_000,
    tool_calls: int = 0,
    tool_outputs: tuple[str, ...] = (),
    assistant_text: str = "done",
    model: str | None = "gpt-5.5",
    reasoning_effort: str | None = "xhigh",
    originator: str = "Codex Desktop",
) -> Path:
    path = home / session_dir / "2026" / "05" / "04" / f"rollout-2026-05-04T12-00-00-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": "2026-05-04T19:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "timestamp": "2026-05-04T19:00:00Z",
                "cwd": str(cwd),
                "originator": originator,
                "model_provider": "openai",
            },
        },
        {
            "timestamp": "2026-05-04T19:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": turn_id, "started_at": 1777921201},
        },
        {
            "timestamp": "2026-05-04T19:00:01Z",
            "type": "turn_context",
            "payload": {
                "turn_id": turn_id,
                "cwd": str(cwd),
                "model": model,
                "effort": reasoning_effort,
            },
        },
        {
            "timestamp": "2026-05-04T19:00:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": message},
        },
        {
            "timestamp": "2026-05-04T19:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": assistant_text}],
            },
        },
        {
            "timestamp": "2026-05-04T19:00:04Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 25,
                        "output_tokens": 12,
                        "reasoning_output_tokens": 5,
                        "total_tokens": 112,
                    }
                },
            },
        },
    ]
    rows.extend(
        {
            "timestamp": "2026-05-04T19:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "arguments": "{}",
                "call_id": f"call-{index}",
            },
        }
        for index in range(max(tool_calls, len(tool_outputs)))
    )
    for index, output in enumerate(tool_outputs):
        rows.append(
            {
                "timestamp": "2026-05-04T19:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": f"call-{index}",
                    "output": output,
                },
            }
        )
    if duration_ms is not None:
        rows.append(
            {
                "timestamp": "2026-05-04T19:00:05Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": turn_id,
                    "completed_at": 1777921205,
                    "duration_ms": duration_ms,
                    "time_to_first_token_ms": 1200,
                },
            }
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _write_multi_turn_session(
    home: Path,
    *,
    thread_id: str,
    cwd: Path,
    turns: tuple[dict[str, str], ...],
) -> Path:
    path = home / "sessions" / "2026" / "05" / "04" / f"rollout-2026-05-04T12-30-00-{thread_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": "2026-05-04T19:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "timestamp": "2026-05-04T19:00:00Z",
                "cwd": str(cwd),
                "originator": "Codex Desktop",
                "model_provider": "openai",
            },
        },
    ]
    for index, turn in enumerate(turns):
        turn_id = turn["turn_id"]
        started_at = turn["started_at"]
        rows.extend(
            [
                {
                    "timestamp": started_at,
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn_id, "started_at": started_at},
                },
                {
                    "timestamp": started_at,
                    "type": "turn_context",
                    "payload": {
                        "turn_id": turn_id,
                        "started_at": started_at,
                        "cwd": str(cwd),
                        "model": "gpt-5.5",
                        "effort": "xhigh",
                    },
                },
                {
                    "timestamp": started_at,
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": turn["message"]},
                },
                {
                    "timestamp": started_at,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": turn.get("assistant_text", "done")}],
                    },
                },
                {
                    "timestamp": started_at,
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": turn_id,
                        "completed_at": started_at,
                        "duration_ms": 30_000 + index,
                    },
                },
            ]
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


class CodexSessionTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile("Codex Session Demo")
        self.profile = self.load_test_profile()
        self.codex_home = self.root / ".codex-home"

    def test_read_codex_session_extracts_turn_context_and_hashes(self) -> None:
        session_path = _write_session(
            self.codex_home,
            thread_id="thread-read",
            cwd=self.root,
            turn_id="turn-read",
            message="Analyze the repo and explain what changed.",
            model=None,
            reasoning_effort=None,
            originator="codex_exec",
        )

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)}, clear=False):
            session = read_codex_session(session_path)

        self.assertIsNotNone(session)
        self.assertEqual(session.thread_id, "thread-read")
        self.assertEqual(session.originator, "codex_exec")
        self.assertEqual(len(session.turns), 1)
        turn = session.turns[0]
        self.assertEqual(turn.turn_id, "turn-read")
        self.assertEqual(turn.cwd, str(self.root))
        self.assertEqual(turn.classification, "analysis_only")
        self.assertEqual(turn.input_tokens, 100)
        self.assertEqual(turn.cached_input_tokens, 25)
        self.assertEqual(turn.output_tokens, 12)
        self.assertEqual(turn.reasoning_output_tokens, 5)
        self.assertEqual(turn.total_tokens, 112)
        self.assertEqual(turn.duration_ms, 42_000)
        self.assertEqual(turn.time_to_first_token_ms, 1200)
        self.assertIsNotNone(turn.completed_at)
        self.assertEqual(
            turn.user_message_hash,
            hashlib.sha256("Analyze the repo and explain what changed.".encode("utf-8")).hexdigest(),
        )
        self.assertTrue(turn.has_assistant_response)

    def test_collect_codex_turns_reuses_persistent_session_cache(self) -> None:
        _write_session(
            self.codex_home,
            thread_id="thread-cache",
            cwd=self.root,
            turn_id="turn-cache",
            message="Implement cached Codex parsing.",
        )
        blackdog_home = self.root / ".blackdog-home"

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home), "BLACKDOG_HOME": str(blackdog_home)}, clear=False):
            turns = collect_codex_turns()
            self.assertEqual(len(turns), 1)
            self.assertTrue(codex_session_cache_path().is_file())
            with patch("blackdog_core.codex_sessions.read_codex_session", side_effect=AssertionError("cache miss")):
                cached_turns = collect_codex_turns()

        self.assertEqual(len(cached_turns), 1)
        self.assertEqual(cached_turns[0].turn_id, "turn-cache")

    def test_lightweight_collection_skips_environment_scan_without_poisoning_full_cache(self) -> None:
        _write_session(
            self.codex_home,
            thread_id="thread-light-cache",
            cwd=self.root,
            turn_id="turn-light-cache",
            message="Implement lightweight stats parsing.",
            tool_outputs=("ModuleNotFoundError: No module named 'utter'",),
        )
        blackdog_home = self.root / ".blackdog-home"

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home), "BLACKDOG_HOME": str(blackdog_home)}, clear=False):
            light_turns = collect_codex_turns(include_environment_evidence=False)
            light_cache = json.loads(codex_session_cache_path().read_text(encoding="utf-8"))
            full_turns = collect_codex_turns()
            full_cache = json.loads(codex_session_cache_path().read_text(encoding="utf-8"))

        self.assertEqual(len(light_turns), 1)
        self.assertEqual(light_turns[0].environment_issue_classes, ())
        light_entries = list(light_cache["sessions"].values())
        self.assertEqual(len(light_entries), 1)
        self.assertFalse(light_entries[0]["environment_scan"])
        self.assertEqual(len(full_turns), 1)
        self.assertEqual(full_turns[0].environment_issue_classes, ("missing_python_module",))
        full_entries = list(full_cache["sessions"].values())
        self.assertEqual(len(full_entries), 1)
        self.assertTrue(full_entries[0]["environment_scan"])

    def test_collect_codex_turns_skips_files_outside_since_window_before_cache(self) -> None:
        _write_session(
            self.codex_home,
            thread_id="thread-old",
            cwd=self.root,
            turn_id="turn-old",
            message="Old session outside the stats window.",
        )

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)}, clear=False):
            with patch("blackdog_core.codex_sessions.read_codex_session", side_effect=AssertionError("parsed old file")):
                turns = collect_codex_turns(since="2026-05-10T00:00:00+00:00", use_cache=False)

        self.assertEqual(turns, ())

    def test_collect_codex_turns_skips_files_outside_cwd_roots_before_full_parse(self) -> None:
        other_root = self.root.parent / "other-repo"
        other_root.mkdir(exist_ok=True)
        _write_session(
            self.codex_home,
            thread_id="thread-other-root",
            cwd=other_root,
            turn_id="turn-other-root",
            message="Implement unrelated repo work.",
        )

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)}, clear=False):
            with patch(
                "blackdog_core.codex_sessions.read_codex_session",
                side_effect=AssertionError("parsed unrelated file"),
            ):
                turns = collect_codex_turns(cwd_roots=(self.root,), use_cache=False)

        self.assertEqual(turns, ())

    def test_collect_codex_turns_prunes_unrelated_cached_sessions_before_materializing(self) -> None:
        other_root = self.root.parent / "other-repo"
        other_root.mkdir(exist_ok=True)
        _write_session(
            self.codex_home,
            thread_id="thread-cache-local",
            cwd=self.root,
            turn_id="turn-cache-local",
            message="Implement cached local work.",
        )
        _write_session(
            self.codex_home,
            thread_id="thread-cache-other",
            cwd=other_root,
            turn_id="turn-cache-other",
            message="Implement cached unrelated work.",
        )
        blackdog_home = self.root / ".blackdog-home"

        with patch.dict(
            "os.environ",
            {"CODEX_HOME": str(self.codex_home), "BLACKDOG_HOME": str(blackdog_home)},
            clear=False,
        ):
            self.assertEqual(len(collect_codex_turns()), 2)
            original_from_cache = codex_sessions_module._codex_session_from_cache_payload

            def from_cache(payload: object) -> object:
                if isinstance(payload, dict) and payload.get("thread_id") == "thread-cache-other":
                    raise AssertionError("materialized unrelated cached session")
                return original_from_cache(payload)

            with patch(
                "blackdog_core.codex_sessions._codex_session_from_cache_payload",
                side_effect=from_cache,
            ):
                turns = collect_codex_turns(cwd_roots=(self.root,))

        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].thread_id, "thread-cache-local")

    def test_current_codex_session_ref_recovers_turn_fields_from_prompt_hash(self) -> None:
        message = "Implement the current turn capture."
        session_path = _write_session(
            self.codex_home,
            thread_id="thread-current",
            cwd=self.root,
            turn_id="turn-current",
            message=message,
        )
        prompt_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()

        with patch.dict(
            "os.environ",
            {"CODEX_HOME": str(self.codex_home), "CODEX_THREAD_ID": "thread-current"},
            clear=False,
        ):
            ref = current_codex_session_ref(user_prompt_hash=prompt_hash, execution_prompt_hash=prompt_hash)

        self.assertIsNotNone(ref)
        self.assertEqual(ref.session_path, str(session_path.relative_to(self.codex_home)))
        self.assertEqual(ref.turn_id, "turn-current")
        self.assertIsNotNone(ref.turn_started_at)
        self.assertEqual(ref.user_prompt_hash, prompt_hash)

    def test_coverage_and_history_join_attempts_without_copying_prompt_text(self) -> None:
        linked_message = "Implement linked task."
        analysis_message = "Assess whether this repo is ready."
        linked_path = _write_session(
            self.codex_home,
            thread_id="thread-linked",
            cwd=self.root,
            turn_id="turn-linked",
            message=linked_message,
            duration_ms=90_000,
        )
        _write_session(
            self.codex_home,
            thread_id="thread-analysis",
            cwd=self.root,
            turn_id="turn-analysis",
            message=analysis_message,
            duration_ms=120_000,
        )
        upsert_workset(
            self.profile,
            {
                "id": "codex-history",
                "title": "Codex history",
                "tasks": [{"id": "CH-1", "title": "Link attempt", "intent": "link one attempt"}],
            },
        )
        receipt = create_prompt_receipt(linked_message, recorded_at="2026-05-04T19:00:02+00:00")
        attempt = start_task(
            self.profile,
            workset_id="codex-history",
            task_id="CH-1",
            actor="codex",
            prompt_receipt=prompt_receipt_reference(receipt),
            codex_session=CodexSessionRefRecord(
                thread_id="thread-linked",
                session_path=str(linked_path.relative_to(self.codex_home)),
                turn_id="turn-linked",
                user_prompt_hash=receipt.prompt_hash,
                execution_prompt_hash=receipt.prompt_hash,
            ),
        )
        finish_task(
            self.profile,
            workset_id="codex-history",
            task_id="CH-1",
            attempt_id=attempt.attempt_id,
            actor="codex",
            status="success",
            summary="linked",
            changed_paths=("src/example.py",),
        )

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)}, clear=False):
            coverage = build_codex_coverage(self.profile)
            history = build_codex_history(self.profile, write=True)

        self.assertEqual(coverage["counts"]["codex_user_turns"], 2)
        self.assertEqual(coverage["counts"]["linked_attempts"], 1)
        linked_turn = next(row for row in coverage["turns"] if row["thread_id"] == "thread-linked")
        self.assertEqual(linked_turn["linked_attempt_ids"], [attempt.attempt_id])
        self.assertEqual(linked_turn["related_attempt_ids"], [attempt.attempt_id])
        self.assertEqual(linked_turn["attempt_relationships"][0]["relationship"], "launch_turn")
        self.assertIsNone(linked_turn["turn_classification"])
        self.assertEqual(
            coverage["turn_classification_counts"],
            {"by_intent": {}, "by_domain": {}, "by_risk": {}},
        )
        self.assertEqual(coverage["counts"]["analysis_only_turns"], 1)
        self.assertEqual(coverage["counts"]["unlinked_user_turns"], 1)
        self.assertEqual(coverage["counts"]["related_user_turns"], 1)
        self.assertEqual(coverage["counts"]["unrelated_user_turns"], 1)
        self.assertEqual(coverage["counts"]["input_tokens"], 200)
        self.assertEqual(coverage["counts"]["cached_input_tokens"], 50)
        self.assertEqual(coverage["counts"]["output_tokens"], 24)
        self.assertEqual(coverage["counts"]["reasoning_output_tokens"], 10)
        self.assertEqual(coverage["counts"]["total_tokens"], 224)
        self.assertEqual(coverage["counts"]["longest_completed_turn_duration_ms"], 120_000)
        self.assertEqual(coverage["counts"]["longest_completed_turn_thread_id"], "thread-analysis")
        self.assertEqual(coverage["counts"]["longest_completed_turn_id"], "turn-analysis")
        self.assertEqual(history["counts"]["attempt_rows"], 1)
        self.assertEqual(history["counts"]["codex_turn_rows"], 2)
        rendered_rows = "\n".join(json.dumps(row, sort_keys=True) for row in history["rows"])
        self.assertIn('"kind": "attempt"', rendered_rows)
        self.assertIn('"kind": "codex_turn"', rendered_rows)
        self.assertIn('"linked_attempt_ids": ["' + attempt.attempt_id + '"]', rendered_rows)
        self.assertIn('"input_tokens": 100', rendered_rows)
        self.assertIn('"duration_ms": 120000', rendered_rows)
        self.assertTrue(
            all(
                row["turn_classification"] is None
                for row in history["rows"]
                if row["kind"] == "codex_turn"
            )
        )
        self.assertNotIn(linked_message, rendered_rows)
        self.assertNotIn(analysis_message, rendered_rows)
        self.assertEqual(
            Path(history["history_path"]).resolve(),
            (self.root / ".blackdog" / "history.jsonl").resolve(),
        )
        self.assertTrue(Path(history["history_path"]).is_file())

    def test_coverage_links_attempt_by_prompt_hash_and_session_ref_without_turn_id(self) -> None:
        linked_message = "Implement recoverable linked task."
        linked_path = _write_session(
            self.codex_home,
            thread_id="thread-hash",
            cwd=self.root,
            turn_id="turn-hash",
            message=linked_message,
        )
        _write_session(
            self.codex_home,
            thread_id="thread-unlinked",
            cwd=self.root,
            turn_id="turn-unlinked",
            message="Implement an unrelated task outside the tracked run.",
        )
        upsert_workset(
            self.profile,
            {
                "id": "codex-hash",
                "title": "Codex hash",
                "tasks": [{"id": "CH-1", "title": "Link by hash", "intent": "link without a turn id"}],
            },
        )
        receipt = create_prompt_receipt(linked_message, recorded_at="2026-05-04T19:00:02+00:00")
        attempt = start_task(
            self.profile,
            workset_id="codex-hash",
            task_id="CH-1",
            actor="codex",
            prompt_receipt=prompt_receipt_reference(receipt),
            codex_session=CodexSessionRefRecord(
                thread_id="thread-hash",
                session_path=str(linked_path.relative_to(self.codex_home)),
                user_prompt_hash=receipt.prompt_hash,
                execution_prompt_hash=receipt.prompt_hash,
            ),
        )
        finish_task(
            self.profile,
            workset_id="codex-hash",
            task_id="CH-1",
            attempt_id=attempt.attempt_id,
            actor="codex",
            status="success",
            summary="linked by hash",
        )

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)}, clear=False):
            coverage = build_codex_coverage(self.profile)

        self.assertEqual(coverage["counts"]["linked_attempts"], 1)
        linked_turn = next(row for row in coverage["turns"] if row["thread_id"] == "thread-hash")
        unlinked_turn = next(row for row in coverage["turns"] if row["thread_id"] == "thread-unlinked")
        self.assertEqual(linked_turn["linked_attempt_ids"], [attempt.attempt_id])
        self.assertEqual(unlinked_turn["linked_attempt_ids"], [])
        self.assertEqual(coverage["counts"]["implementation_like_unlinked_turns"], 1)

    def test_coverage_respects_until_bound_for_turns_and_attempts(self) -> None:
        old_message = "Implement windowed coverage."
        future_message = "Implement future coverage."
        _write_multi_turn_session(
            self.codex_home,
            thread_id="thread-window",
            cwd=self.root,
            turns=(
                {
                    "turn_id": "turn-window",
                    "started_at": "2026-05-04T19:00:00+00:00",
                    "message": old_message,
                },
                {
                    "turn_id": "turn-future",
                    "started_at": "2026-05-06T19:00:00+00:00",
                    "message": future_message,
                },
            ),
        )
        upsert_workset(
            self.profile,
            {
                "id": "codex-window",
                "title": "Codex window",
                "tasks": [
                    {"id": "CW-1", "title": "Window task", "intent": "inside window"},
                    {"id": "CW-2", "title": "Future task", "intent": "outside window"},
                ],
            },
        )
        old_receipt = create_prompt_receipt(old_message, recorded_at="2026-05-04T19:00:00+00:00")
        future_receipt = create_prompt_receipt(future_message, recorded_at="2026-05-06T19:00:00+00:00")
        with patch("blackdog_core.backlog.now_iso", side_effect=["2026-05-04T19:00:00+00:00", "2026-05-04T19:05:00+00:00"]):
            attempt = start_task(
                self.profile,
                workset_id="codex-window",
                task_id="CW-1",
                actor="codex",
                prompt_receipt=prompt_receipt_reference(old_receipt),
            )
            finish_task(
                self.profile,
                workset_id="codex-window",
                task_id="CW-1",
                attempt_id=attempt.attempt_id,
                actor="codex",
                status="success",
                summary="inside window",
            )
        with patch("blackdog_core.backlog.now_iso", side_effect=["2026-05-06T19:00:00+00:00", "2026-05-06T19:05:00+00:00"]):
            attempt = start_task(
                self.profile,
                workset_id="codex-window",
                task_id="CW-2",
                actor="codex",
                prompt_receipt=prompt_receipt_reference(future_receipt),
            )
            finish_task(
                self.profile,
                workset_id="codex-window",
                task_id="CW-2",
                attempt_id=attempt.attempt_id,
                actor="codex",
                status="success",
                summary="outside window",
            )

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)}, clear=False):
            coverage = build_codex_coverage(
                self.profile,
                since="2026-05-04T00:00:00+00:00",
                until="2026-05-05T00:00:00+00:00",
            )

        self.assertEqual(coverage["counts"]["codex_user_turns"], 1)
        self.assertEqual(coverage["counts"]["blackdog_attempts"], 1)
        self.assertEqual(coverage["counts"]["linked_attempts"], 1)
        self.assertEqual(coverage["turns"][0]["turn_id"], "turn-window")

    def test_coverage_marks_same_session_followups_as_related_without_linking_them(self) -> None:
        launch_message = "Implement the task launch."
        followup_message = "Run the validation after the branch is ready."
        session_path = _write_multi_turn_session(
            self.codex_home,
            thread_id="thread-episode",
            cwd=self.root,
            turns=(
                {
                    "turn_id": "turn-launch",
                    "started_at": "2026-05-04T19:00:05+00:00",
                    "message": launch_message,
                },
                {
                    "turn_id": "turn-followup",
                    "started_at": "2026-05-04T19:05:00+00:00",
                    "message": followup_message,
                    "assistant_text": "validation passed",
                },
            ),
        )
        upsert_workset(
            self.profile,
            {
                "id": "codex-episode",
                "title": "Codex episode",
                "tasks": [{"id": "CE-1", "title": "Episode task", "intent": "relate follow-up turn"}],
            },
        )
        receipt = create_prompt_receipt(launch_message, recorded_at="2026-05-04T19:00:02+00:00")
        with patch("blackdog_core.backlog.now_iso", side_effect=["2026-05-04T19:00:00+00:00", "2026-05-04T19:10:00+00:00"]):
            attempt = start_task(
                self.profile,
                workset_id="codex-episode",
                task_id="CE-1",
                actor="codex",
                prompt_receipt=prompt_receipt_reference(receipt),
                codex_session=CodexSessionRefRecord(
                    thread_id="thread-episode",
                    session_path=str(session_path.relative_to(self.codex_home)),
                    turn_id="turn-launch",
                    user_prompt_hash=receipt.prompt_hash,
                    execution_prompt_hash=receipt.prompt_hash,
                ),
            )
            finish_task(
                self.profile,
                workset_id="codex-episode",
                task_id="CE-1",
                attempt_id=attempt.attempt_id,
                actor="codex",
                status="success",
                summary="related follow-up",
            )

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)}, clear=False):
            coverage = build_codex_coverage(self.profile)
            history = build_codex_history(self.profile)

        self.assertEqual(coverage["counts"]["linked_user_turns"], 1)
        self.assertEqual(coverage["counts"]["related_user_turns"], 2)
        self.assertEqual(coverage["relationship_counts"]["launch_turn"], 1)
        self.assertEqual(coverage["relationship_counts"]["active_attempt_window"], 1)
        launch_turn = next(row for row in coverage["turns"] if row["turn_id"] == "turn-launch")
        followup_turn = next(row for row in coverage["turns"] if row["turn_id"] == "turn-followup")
        self.assertEqual(launch_turn["linked_attempt_ids"], [attempt.attempt_id])
        self.assertEqual(followup_turn["linked_attempt_ids"], [])
        self.assertEqual(followup_turn["related_attempt_ids"], [attempt.attempt_id])
        self.assertEqual(followup_turn["attempt_relationships"][0]["relationship"], "active_attempt_window")
        attempt_row = next(row for row in coverage["attempts"] if row["attempt_id"] == attempt.attempt_id)
        self.assertEqual(attempt_row["linked_codex_turn_ids"], ["turn-launch"])
        self.assertEqual(attempt_row["related_codex_turn_ids"], ["turn-launch", "turn-followup"])
        turn_history = [row for row in history["rows"] if row["kind"] == "codex_turn"]
        self.assertEqual(len(turn_history), 2)
        self.assertTrue(any(row["linked_attempt_ids"] == [attempt.attempt_id] for row in turn_history))
        self.assertTrue(any(row["related_attempt_ids"] == [attempt.attempt_id] for row in turn_history))

    def test_coverage_links_attempts_from_hook_task_context_stamps(self) -> None:
        message = "Continue the active task with hook context."
        _write_session(
            self.codex_home,
            thread_id="thread-hook",
            cwd=self.root,
            turn_id="turn-hook",
            message=message,
        )
        upsert_workset(
            self.profile,
            {
                "id": "hook-context",
                "title": "Hook context",
                "tasks": [{"id": "TASK-1", "title": "Hook context task"}],
            },
        )
        attempt = start_task(
            self.profile,
            workset_id="hook-context",
            task_id="TASK-1",
            actor="codex",
            prompt_receipt=create_prompt_receipt("Different launch prompt."),
            worktree_path=str(self.root),
            branch="agent/hook-context",
            target_branch="main",
        )
        useful_classification = {
            "intent": "implementation",
            "domains": ["tests", "ui"],
            "risk": "normal",
            "source": "heuristic",
            "confidence": "high",
        }
        unknown_classification = {
            "intent": "unknown",
            "domains": [],
            "risk": "unknown",
            "source": "heuristic",
            "confidence": "low",
        }
        for hook_event_name, turn_classification in (
            ("SessionStart", None),
            (
                "PreToolUse",
                {
                    "intent": "implementation",
                    "domains": ["ui", "not-a-domain"],
                    "risk": "normal",
                    "source": "heuristic",
                    "confidence": "high",
                },
            ),
            ("PostToolUse", unknown_classification),
            ("UserPromptSubmit", useful_classification),
            (
                "Stop",
                {
                    "intent": "analysis",
                    "domains": ["ui", "tests", "backend"],
                    "risk": "normal",
                    "source": "heuristic",
                    "confidence": "high",
                },
            ),
        ):
            payload = {
                "schema_version": 1,
                "hook": {
                    "hook_event_name": hook_event_name,
                    "session_id": "thread-hook",
                    "turn_id": "turn-hook",
                    "cwd": str(self.root),
                },
                "context_found": True,
                "active_attempt": {
                    "workset_id": "hook-context",
                    "task_id": "TASK-1",
                    "attempt_id": attempt.attempt_id,
                },
            }
            if turn_classification is not None:
                payload["turn_classification"] = turn_classification
            append_event(
                codex_task_context_path(self.profile),
                event_type="codex.hook.task_context",
                actor="codex-hook",
                payload=payload,
            )

        stamps = load_codex_task_context_stamps(self.profile)
        self.assertEqual(len(stamps), 5)
        self.assertIsNone(stamps[0].turn_classification)
        self.assertIsNone(stamps[1].turn_classification)
        self.assertEqual(stamps[2].turn_classification.intent, "unknown")

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)}, clear=False):
            coverage = build_codex_coverage(self.profile)
            history = build_codex_history(self.profile)

        self.assertEqual(coverage["counts"]["hook_task_context_stamps"], 5)
        self.assertEqual(coverage["relationship_counts"][RELATIONSHIP_HOOK_CONTEXT], 1)
        self.assertEqual(coverage["counts"]["linked_attempts"], 1)
        turn_row = coverage["turns"][0]
        self.assertEqual(turn_row["linked_attempt_ids"], [attempt.attempt_id])
        self.assertEqual(turn_row["attempt_relationships"][0]["relationship"], RELATIONSHIP_HOOK_CONTEXT)
        self.assertEqual(turn_row["classification"], "blackdog_attempt")
        self.assertEqual(
            turn_row["turn_classification"],
            {
                "intent": "implementation",
                "domains": ["ui", "tests"],
                "risk": "normal",
                "source": "heuristic",
                "confidence": "high",
            },
        )
        self.assertEqual(
            coverage["turn_classification_counts"],
            {
                "by_intent": {"implementation": 1},
                "by_domain": {"ui": 1, "tests": 1},
                "by_risk": {"normal": 1},
            },
        )
        attempt_row = next(row for row in history["rows"] if row.get("kind") == "attempt")
        self.assertEqual(attempt_row["linked_codex_turn_ids"], ["turn-hook"])
        turn_history_row = next(row for row in history["rows"] if row.get("kind") == "codex_turn")
        self.assertEqual(turn_history_row["classification"], "unclassified")
        self.assertEqual(turn_history_row["turn_classification"], turn_row["turn_classification"])
        self.assertNotIn(message, json.dumps(history, sort_keys=True))

    def test_coverage_reads_hook_classification_without_active_attempt_context(self) -> None:
        message = "What is the current deployment status?"
        _write_session(
            self.codex_home,
            thread_id="thread-hook-unlinked",
            cwd=self.root,
            turn_id="turn-hook-unlinked",
            message=message,
        )
        turn_classification = {
            "intent": "status",
            "domains": ["deployment"],
            "risk": "guarded",
            "source": "heuristic",
            "confidence": "high",
        }
        append_event(
            codex_task_context_path(self.profile),
            event_type="codex.hook.task_context",
            actor="codex-hook",
            payload={
                "schema_version": 1,
                "hook": {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "thread-hook-unlinked",
                    "turn_id": "turn-hook-unlinked",
                    "cwd": str(self.root),
                },
                "context_found": False,
                "turn_classification": turn_classification,
                "active_attempt": None,
            },
        )

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)}, clear=False):
            coverage = build_codex_coverage(self.profile)
            history = build_codex_history(self.profile)

        self.assertEqual(coverage["counts"]["hook_task_context_stamps"], 1)
        self.assertEqual(coverage["counts"]["linked_user_turns"], 0)
        self.assertNotIn(RELATIONSHIP_HOOK_CONTEXT, coverage["relationship_counts"])
        turn_row = coverage["turns"][0]
        self.assertEqual(turn_row["linked_attempt_ids"], [])
        self.assertEqual(turn_row["turn_classification"], turn_classification)
        self.assertEqual(
            coverage["turn_classification_counts"],
            {
                "by_intent": {"status": 1},
                "by_domain": {"deployment": 1},
                "by_risk": {"guarded": 1},
            },
        )
        history_turn = next(row for row in history["rows"] if row["kind"] == "codex_turn")
        self.assertEqual(history_turn["turn_classification"], turn_classification)
        self.assertNotIn(message, json.dumps(history, sort_keys=True))

    def test_coverage_and_history_extract_environment_issue_classes_from_outputs(self) -> None:
        prompt = "Implement the environment diagnosis."
        _write_session(
            self.codex_home,
            thread_id="thread-env",
            cwd=self.root,
            turn_id="turn-env",
            message=prompt,
            assistant_text="The run failed with ModuleNotFoundError: No module named 'utter'.",
            tool_outputs=(
                "docker: command not found",
                "zipfile.BadZipFile: File is not a zip file",
            ),
        )

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)}, clear=False):
            coverage = build_codex_coverage(self.profile)
            history = build_codex_history(self.profile)

        self.assertEqual(coverage["counts"]["environment_issue_turns"], 1)
        self.assertEqual(coverage["environment_issue_counts"]["missing_container_runtime"], 1)
        self.assertNotIn("missing_python_module", coverage["environment_issue_counts"])
        self.assertEqual(coverage["environment_issue_counts"]["source_file_bad_format"], 1)
        self.assertEqual(coverage["environment_issue_evidence_counts"]["missing_python_module"], 1)
        self.assertEqual(coverage["observed_environment_issue_evidence_counts"]["missing_container_runtime"], 1)
        self.assertNotIn("missing_python_module", coverage["observed_environment_issue_evidence_counts"])
        self.assertEqual(coverage["operator_guidance_environment_issue_evidence_counts"]["missing_python_module"], 1)
        self.assertEqual(coverage["counts"]["observed_environment_issue_evidence"], 2)
        self.assertEqual(coverage["counts"]["operator_guidance_environment_issue_evidence"], 1)
        env_turn = coverage["turns"][0]
        self.assertEqual(env_turn["primary_environment_issue_class"], "missing_container_runtime")
        self.assertEqual(
            env_turn["environment_issue_classes"],
            ["missing_container_runtime", "source_file_bad_format"],
        )
        self.assertGreaterEqual(len(env_turn["environment_issue_evidence"]), 3)
        self.assertEqual(
            {
                (row["class"], row["evidence_kind"])
                for row in env_turn["environment_issue_evidence"]
            },
            {
                ("missing_container_runtime", "observed_failure"),
                ("missing_python_module", "operator_guidance"),
                ("source_file_bad_format", "observed_failure"),
            },
        )
        rendered_rows = "\n".join(json.dumps(row, sort_keys=True) for row in history["rows"])
        self.assertIn('"environment_issue_classes": ["missing_container_runtime", "source_file_bad_format"]', rendered_rows)
        self.assertIn('"evidence_kind": "operator_guidance"', rendered_rows)
        self.assertNotIn(prompt, rendered_rows)

    def test_repo_matching_includes_git_worktree_cwds(self) -> None:
        worktree_path = self.root.parent / f"{self.root.name}-linked-worktree"
        try:
            subprocess.run(
                ["git", "-C", str(self.root), "worktree", "add", "-b", "feature/codex-session", str(worktree_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            _write_session(
                self.codex_home,
                thread_id="thread-worktree",
                cwd=worktree_path,
                turn_id="turn-worktree",
                message="Implement from the linked worktree.",
            )
            with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)}, clear=False):
                coverage = build_codex_coverage(self.profile)
            self.assertEqual(coverage["counts"]["codex_sessions"], 1)
            self.assertEqual(coverage["turns"][0]["cwd"], str(worktree_path))
        finally:
            if worktree_path.exists():
                subprocess.run(
                    ["git", "-C", str(self.root), "worktree", "remove", "--force", str(worktree_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            subprocess.run(
                ["git", "-C", str(self.root), "branch", "-D", "feature/codex-session"],
                check=False,
                capture_output=True,
                text=True,
            )

    def test_coverage_includes_archived_sessions_and_dedupes_same_turn(self) -> None:
        _write_session(
            self.codex_home,
            thread_id="thread-dupe",
            cwd=self.root,
            turn_id="turn-dupe",
            message="Implement the durable turn once.",
            duration_ms=100_000,
            tool_calls=2,
        )
        _write_session(
            self.codex_home,
            thread_id="thread-dupe",
            cwd=self.root,
            turn_id="turn-dupe",
            message="Implement the durable turn once.",
            session_dir="archived_sessions",
            duration_ms=200_000,
            tool_calls=0,
            model=None,
            reasoning_effort=None,
        )
        _write_session(
            self.codex_home,
            thread_id="thread-archived",
            cwd=self.root,
            turn_id="turn-archived",
            message="Run the archived-only long request.",
            session_dir="archived_sessions",
            duration_ms=240_000,
        )

        with patch.dict("os.environ", {"CODEX_HOME": str(self.codex_home)}, clear=False):
            coverage = build_codex_coverage(self.profile)

        self.assertEqual(coverage["counts"]["codex_user_turns"], 2)
        self.assertEqual(coverage["counts"]["tool_calls"], 2)
        self.assertEqual(coverage["counts"]["longest_completed_turn_duration_ms"], 240_000)
        self.assertEqual(coverage["counts"]["longest_completed_turn_thread_id"], "thread-archived")
        self.assertEqual(coverage["counts"]["longest_completed_turn_id"], "turn-archived")
        dupe_turn = next(row for row in coverage["turns"] if row["thread_id"] == "thread-dupe")
        self.assertEqual(dupe_turn["duration_ms"], 100_000)
        self.assertEqual(dupe_turn["tool_call_count"], 2)
