from __future__ import annotations

import json

from tests import test_blackdog_cli as cli_tests
from tests.core_audit_support import CoreAuditTestCase


class CoreStoreAuditTests(CoreAuditTestCase):
    def test_core_audit_store_state_and_tracked_install_normalization_enforces_shape(self) -> None:
        state_file = self.root / "backlog-state.json"
        installs_file = self.root / "tracked-installs.json"

        normalized_state = cli_tests.store_module.normalize_state({}, state_file=state_file)
        self.assertEqual(normalized_state["schema_version"], 1)
        self.assertEqual(normalized_state["approval_tasks"], {})
        self.assertEqual(normalized_state["task_claims"], {})
        self.assertEqual(normalized_state["task_attempts"], {})
        self.assertEqual(normalized_state["wait_conditions"], {})
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_state({"approval_tasks": []}, state_file=state_file)

        installs = cli_tests.installs_module.normalize_tracked_installs(
            {
                "repos": [
                    {"project_root": "/tmp/zeta", "project_name": "Zeta"},
                    {"project_root": "/tmp/alpha", "project_name": "Alpha"},
                    {"project_root": "/tmp/zeta", "project_name": "Duplicate"},
                ]
            },
            installs_file=installs_file,
        )
        self.assertEqual([row["project_root"] for row in installs["repos"]], ["/tmp/alpha", "/tmp/zeta"])
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.installs_module.normalize_tracked_installs({"repos": [{}]}, installs_file=installs_file)

    def test_core_audit_store_artifact_normalizers_enforce_core_shapes(self) -> None:
        cli_tests.run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        cli_tests.append_jsonl(
            paths.inbox_file,
            {
                "action": "message",
                "message_id": "msg-1",
                "at": "2026-04-07T12:00:00-07:00",
                "sender": "supervisor",
                "recipient": "child",
                "kind": "instruction",
                "task_id": "BLACK-demo",
                "reply_to": "",
                "tags": [" supervisor-run ", "", "git-worktree"],
                "body": "Do the task.",
            },
        )
        cli_tests.append_jsonl(
            paths.inbox_file,
            {
                "action": "resolve",
                "message_id": "msg-1",
                "at": "2026-04-07T12:05:00-07:00",
                "actor": "supervisor",
                "note": "done",
            },
        )
        inbox_rows = cli_tests.load_inbox(paths)
        self.assertEqual(inbox_rows[0]["tags"], ["supervisor-run", "git-worktree"])
        self.assertEqual(inbox_rows[0]["status"], "resolved")
        self.assertEqual(inbox_rows[0]["resolved_by"], "supervisor")

        paths.events_file.write_text(
            json.dumps(
                {
                    "event_id": "evt-1",
                    "type": "claim",
                    "at": "2026-04-07T12:00:00-07:00",
                    "actor": "codex",
                    "task_id": "BLACK-demo",
                    "payload": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.load_events(paths)

    def test_core_audit_task_result_loading_rejects_missing_summary_fields(self) -> None:
        cli_tests.run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        result_dir = paths.results_dir / "BLACK-demo"
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "20260407-120000-demo.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": "BLACK-demo",
                    "recorded_at": "2026-04-07T12:00:00-07:00",
                    "actor": "codex",
                    "run_id": "demo",
                    "status": "success",
                    "what_changed": ["did the thing"],
                    "validation": [],
                    "residual": [],
                    "followup_candidates": [],
                    "metadata": {},
                    "task_shaping_telemetry": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.load_task_results(paths, task_id="BLACK-demo")
