from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from blackdog.codex_hooks import stamp_codex_task_context
from blackdog_core.backlog import start_task, upsert_workset
from blackdog_core.codex_sessions import codex_task_context_path
from blackdog_core.state import CodexSessionRefRecord, create_prompt_receipt, load_events, load_runtime_state
from tests.core_audit_support import CoreAuditTestCase


class CodexHookTests(CoreAuditTestCase):
    def test_hook_stamp_records_active_attempt_context_without_prompt_text(self) -> None:
        self.write_profile("Hook Demo")
        profile = self.load_test_profile()
        upsert_workset(
            profile,
            {
                "id": "hook-workset",
                "title": "Hook workset",
                "tasks": [{"id": "TASK-1", "title": "Hook task"}],
            },
        )
        worktree_path = self.root / "task-worktree"
        worktree_path.mkdir()
        attempt = start_task(
            profile,
            workset_id="hook-workset",
            task_id="TASK-1",
            actor="codex",
            prompt_receipt=create_prompt_receipt("Implement hook stamping."),
            worktree_path=str(worktree_path),
            branch="agent/hook-task",
            target_branch="main",
            codex_session=CodexSessionRefRecord(thread_id="stored-thread"),
        )

        prompt = "On page xyz.html, there is too much padding around the outside of the main panel, remove it"
        tool_command = "python -m private_tool --opaque-value"
        result = stamp_codex_task_context(
            profile,
            hook_payload={
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-hook",
                "turn_id": "turn-hook",
                "cwd": str(worktree_path / "subdir"),
                "model": "gpt-5.5",
                "prompt": prompt,
                "tool_input": {"command": tool_command},
            },
            cwd=worktree_path,
        )

        self.assertTrue(result["stamped"])
        self.assertTrue(result["context_found"])
        self.assertEqual(result["active_attempt"]["attempt_id"], attempt.attempt_id)
        self.assertEqual(result["turn_classification"]["intent"], "implementation")
        self.assertEqual(result["turn_classification"]["domains"], ["ui"])
        self.assertEqual(result["turn_classification"]["risk"], "normal")
        rows = load_events(codex_task_context_path(profile))
        self.assertEqual(len(rows), 1)
        payload = rows[0]["payload"]
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["hook"]["session_id"], "thread-hook")
        self.assertEqual(payload["hook"]["turn_id"], "turn-hook")
        self.assertEqual(payload["active_attempt"]["workset_id"], "hook-workset")
        self.assertEqual(payload["active_attempt"]["matched_by"], "worktree_path")
        self.assertEqual(payload["turn_classification"], result["turn_classification"])
        self.assertIn("prompt_hash", payload["hook"])
        self.assertIn("tool_command_hash", payload["hook"])
        self.assertNotIn(prompt, json.dumps(payload))
        self.assertNotIn(tool_command, json.dumps(payload))

    def test_hook_stamp_without_active_attempt_is_still_observability(self) -> None:
        self.write_profile("Hook Demo")
        profile = self.load_test_profile()

        result = stamp_codex_task_context(
            profile,
            hook_payload={
                "hook_event_name": "Stop",
                "session_id": "thread-no-task",
                "turn_id": "turn-no-task",
                "cwd": str(self.root),
            },
            cwd=Path(self.root),
        )

        self.assertTrue(result["stamped"])
        self.assertFalse(result["context_found"])
        self.assertEqual(
            result["turn_classification"],
            {
                "intent": "unknown",
                "domains": [],
                "risk": "unknown",
                "source": "heuristic",
                "confidence": "low",
            },
        )
        rows = load_events(codex_task_context_path(profile))
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["payload"]["context_found"])
        self.assertIsNone(rows[0]["payload"]["active_attempt"])
        self.assertEqual(rows[0]["payload"]["turn_classification"], result["turn_classification"])

    def test_hook_stamp_classifies_question_from_message(self) -> None:
        self.write_profile("Hook Demo")
        profile = self.load_test_profile()

        result = stamp_codex_task_context(
            profile,
            hook_payload={
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-question",
                "turn_id": "turn-question",
                "cwd": str(self.root),
                "message": "What does the Codex hook stamp record?",
            },
            cwd=Path(self.root),
        )

        self.assertEqual(result["turn_classification"]["intent"], "question")
        self.assertEqual(result["turn_classification"]["risk"], "normal")

    def test_status_intent_does_not_capture_status_page_implementation(self) -> None:
        self.write_profile("Hook Demo")
        profile = self.load_test_profile()

        status_result = stamp_codex_task_context(
            profile,
            hook_payload={"message": "Can you give me a status update?"},
            cwd=Path(self.root),
        )
        deployment_status_result = stamp_codex_task_context(
            profile,
            hook_payload={"message": "Can you update me on the deployment?"},
            cwd=Path(self.root),
        )
        implementation_result = stamp_codex_task_context(
            profile,
            hook_payload={"message": "Update the status page layout."},
            cwd=Path(self.root),
        )

        self.assertEqual(status_result["turn_classification"]["intent"], "status")
        self.assertEqual(deployment_status_result["turn_classification"]["intent"], "status")
        self.assertEqual(deployment_status_result["turn_classification"]["risk"], "guarded")
        self.assertEqual(implementation_result["turn_classification"]["intent"], "implementation")
        self.assertIn("ui", implementation_result["turn_classification"]["domains"])

    def test_release_notes_are_not_guarded_deployment_work(self) -> None:
        self.write_profile("Hook Demo")
        profile = self.load_test_profile()

        result = stamp_codex_task_context(
            profile,
            hook_payload={"message": "Review the release notes for typos."},
            cwd=Path(self.root),
        )

        self.assertEqual(result["turn_classification"]["intent"], "analysis")
        self.assertIn("docs", result["turn_classification"]["domains"])
        self.assertNotIn("deployment", result["turn_classification"]["domains"])
        self.assertEqual(result["turn_classification"]["risk"], "normal")

    def test_guarded_deployment_classification_does_not_create_a_task(self) -> None:
        self.write_profile("Hook Demo")
        profile = self.load_test_profile()
        self.assertEqual(load_runtime_state(profile.paths).worksets, ())

        result = stamp_codex_task_context(
            profile,
            hook_payload={
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-deploy",
                "turn_id": "turn-deploy",
                "cwd": str(self.root),
                "prompt": "Deploy this release to production through GitHub Actions.",
            },
            cwd=Path(self.root),
        )

        self.assertTrue(result["stamped"])
        self.assertFalse(result["context_found"])
        self.assertEqual(result["turn_classification"]["intent"], "implementation")
        self.assertIn("deployment", result["turn_classification"]["domains"])
        self.assertEqual(result["turn_classification"]["risk"], "guarded")
        self.assertEqual(load_runtime_state(profile.paths).worksets, ())

    def test_classifier_failure_falls_back_without_blocking_or_persisting_text(self) -> None:
        self.write_profile("Hook Demo")
        profile = self.load_test_profile()
        prompt = "Implement a private classifier fallback."

        with patch("blackdog.codex_hooks._classify_turn", side_effect=RuntimeError("classifier failure detail")):
            result = stamp_codex_task_context(
                profile,
                hook_payload={
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "thread-fallback",
                    "turn_id": "turn-fallback",
                    "cwd": str(self.root),
                    "prompt": prompt,
                },
                cwd=Path(self.root),
            )

        self.assertTrue(result["stamped"])
        self.assertEqual(result["turn_classification"]["intent"], "unknown")
        self.assertEqual(result["turn_classification"]["risk"], "unknown")
        rows = load_events(codex_task_context_path(profile))
        serialized = json.dumps(rows[0]["payload"])
        self.assertIn("prompt_hash", rows[0]["payload"]["hook"])
        self.assertNotIn(prompt, serialized)
        self.assertNotIn("classifier failure detail", serialized)
