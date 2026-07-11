from __future__ import annotations

import json
from pathlib import Path

from blackdog.codex_hooks import stamp_codex_task_context
from blackdog_core.backlog import start_task, upsert_workset
from blackdog_core.codex_sessions import codex_task_context_path
from blackdog_core.state import CodexSessionRefRecord, create_prompt_receipt, load_events
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

        result = stamp_codex_task_context(
            profile,
            hook_payload={
                "hook_event_name": "UserPromptSubmit",
                "session_id": "thread-hook",
                "turn_id": "turn-hook",
                "cwd": str(worktree_path / "subdir"),
                "model": "gpt-5.5",
                "prompt": "do not persist this prompt text",
            },
            cwd=worktree_path,
        )

        self.assertTrue(result["stamped"])
        self.assertTrue(result["context_found"])
        self.assertEqual(result["active_attempt"]["attempt_id"], attempt.attempt_id)
        rows = load_events(codex_task_context_path(profile))
        self.assertEqual(len(rows), 1)
        payload = rows[0]["payload"]
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["hook"]["session_id"], "thread-hook")
        self.assertEqual(payload["hook"]["turn_id"], "turn-hook")
        self.assertEqual(payload["active_attempt"]["workset_id"], "hook-workset")
        self.assertEqual(payload["active_attempt"]["matched_by"], "worktree_path")
        self.assertIn("prompt_hash", payload["hook"])
        self.assertNotIn("do not persist", json.dumps(payload))

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
        rows = load_events(codex_task_context_path(profile))
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["payload"]["context_found"])
        self.assertIsNone(rows[0]["payload"]["active_attempt"])
