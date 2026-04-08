from __future__ import annotations

import json
from pathlib import Path

from tests import test_blackdog_cli as cli_tests
from tests.core_audit_support import CoreAuditTestCase


class CoreStoreHelperAuditTests(CoreAuditTestCase):
    def init_demo_paths(self):
        cli_tests.run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        return self.runtime_paths()

    def test_core_store_scalar_helpers_cover_valid_and_invalid_inputs(self) -> None:
        source = self.root / "store.json"

        self.assertEqual(cli_tests.store_module._normalize_string_list(None, field="tags", source=source), [])
        self.assertEqual(
            cli_tests.store_module._normalize_string_list([" one ", "", "two"], field="tags", source=source),
            ["one", "two"],
        )
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module._normalize_string_list("oops", field="tags", source=source)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module._normalize_string_list(["ok", 7], field="tags", source=source)

        self.assertEqual(cli_tests.store_module._normalize_optional_object(None, field="payload", source=source), {})
        self.assertEqual(
            cli_tests.store_module._normalize_optional_object({"status": "ok"}, field="payload", source=source),
            {"status": "ok"},
        )
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module._normalize_optional_object([], field="payload", source=source)

        self.assertEqual(cli_tests.store_module._normalize_positive_int("2", field="claimed_pid", source=source), 2)
        self.assertEqual(
            cli_tests.store_module._normalize_non_negative_int("0", field="missing_scans", source=source),
            0,
        )
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module._normalize_positive_int("0", field="claimed_pid", source=source)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module._normalize_positive_int("bad", field="claimed_pid", source=source)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module._normalize_non_negative_int(-1, field="missing_scans", source=source)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module._normalize_non_negative_int("bad", field="missing_scans", source=source)

        self.assertFalse(cli_tests.store_module.approval_is_satisfied(None))
        self.assertTrue(cli_tests.store_module.approval_is_satisfied({"status": "approved"}))
        self.assertFalse(cli_tests.store_module.claim_is_done(None))
        self.assertTrue(cli_tests.store_module.claim_is_done({"status": "done"}))
        self.assertTrue(cli_tests.store_module.claim_is_active({"status": "claimed"}))
        self.assertFalse(cli_tests.store_module.claim_is_active({"status": "released"}))

    def test_core_store_entry_normalizers_cover_optional_and_runtime_fields(self) -> None:
        source = self.root / "state.json"

        approval = cli_tests.store_module.normalize_approval_entry(
            "BLACK-1",
            {
                "status": "approved",
                "first_seen": " 2026-04-08T10:00:00-07:00 ",
                "last_seen": " 2026-04-08T10:05:00-07:00 ",
                "title": " Review request ",
                "bucket": " core ",
                "approval_reason": " policy ",
                "paths": [" docs/FILE_FORMATS.md ", "", "docs/FILE_FORMATS.md"],
            },
            state_file=source,
        )
        self.assertEqual(approval["status"], "approved")
        self.assertEqual(approval["title"], "Review request")
        self.assertEqual(approval["bucket"], "core")
        self.assertEqual(approval["approval_reason"], "policy")
        self.assertEqual(approval["paths"], ["docs/FILE_FORMATS.md", "docs/FILE_FORMATS.md"])

        default_approval = cli_tests.store_module.normalize_approval_entry("BLACK-2", {}, state_file=source)
        self.assertEqual(default_approval["status"], "pending")
        self.assertEqual(default_approval["paths"], [])
        self.assertNotIn("title", default_approval)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_approval_entry("BLACK-3", {"status": "maybe"}, state_file=source)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_approval_entry("BLACK-4", [], state_file=source)

        claimed = cli_tests.store_module.normalize_claim_entry(
            "BLACK-5",
            {
                "status": "claimed",
                "title": " Claim ",
                "claimed_by": " codex ",
                "claimed_at": " 2026-04-08T10:00:00-07:00 ",
                "paths": [" src/blackdog/core/store.py "],
                "claimed_pid": "12",
                "claimed_process_missing_scans": "0",
                "claimed_process_last_seen_at": " seen ",
                "claimed_process_last_checked_at": " checked ",
            },
            state_file=source,
        )
        self.assertEqual(claimed["claimed_pid"], 12)
        self.assertEqual(claimed["claimed_process_missing_scans"], 0)
        self.assertEqual(claimed["claimed_process_last_seen_at"], "seen")
        self.assertEqual(claimed["claimed_process_last_checked_at"], "checked")

        done = cli_tests.store_module.normalize_claim_entry(
            "BLACK-6",
            {
                "status": "done",
                "claimed_pid": 12,
                "claimed_process_missing_scans": 4,
                "claimed_process_last_seen_at": "seen",
                "claimed_process_last_checked_at": "checked",
            },
            state_file=source,
        )
        self.assertEqual(done["status"], "done")
        self.assertNotIn("claimed_pid", done)
        self.assertNotIn("claimed_process_missing_scans", done)

        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_claim_entry("BLACK-7", {"status": "oops"}, state_file=source)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_claim_entry("BLACK-8", [], state_file=source)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_claim_entry(
                "BLACK-9",
                {"status": "claimed", "claimed_pid": 0},
                state_file=source,
            )

        minimal_claimed = cli_tests.store_module.normalize_claim_entry(
            "BLACK-10",
            {"status": "claimed"},
            state_file=source,
        )
        self.assertNotIn("claimed_pid", minimal_claimed)
        self.assertNotIn("claimed_process_missing_scans", minimal_claimed)
        self.assertNotIn("claimed_process_last_seen_at", minimal_claimed)
        self.assertNotIn("claimed_process_last_checked_at", minimal_claimed)

    def test_core_store_event_inbox_and_result_normalizers_cover_error_paths(self) -> None:
        source = self.root / "events.jsonl"

        event = cli_tests.store_module.normalize_event_row(
            {
                "event_id": "evt-1",
                "type": "comment",
                "actor": "codex",
                "at": "2026-04-08T10:00:00-07:00",
                "task_id": "BLACK-1",
                "payload": None,
            },
            events_file=source,
        )
        self.assertEqual(event["payload"], {})
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_event_row([], events_file=source)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_event_row({"type": "comment", "actor": "codex", "at": "now"}, events_file=source)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_event_row(
                {"event_id": "evt-2", "actor": "codex", "at": "now"},
                events_file=source,
            )
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_event_row(
                {"event_id": "evt-3", "type": "comment", "at": "now"},
                events_file=source,
            )
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_event_row(
                {"event_id": "evt-4", "type": "comment", "actor": "codex"},
                events_file=source,
            )

        inbox_file = self.root / "inbox.jsonl"
        message = cli_tests.store_module.normalize_inbox_row(
            {
                "action": "message",
                "message_id": "msg-1",
                "at": "2026-04-08T10:00:00-07:00",
                "sender": "supervisor",
                "recipient": "child",
                "kind": "instruction",
                "body": "Do the thing",
                "tags": [" core ", ""],
            },
            inbox_file=inbox_file,
        )
        self.assertEqual(message["tags"], ["core"])
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_inbox_row([], inbox_file=inbox_file)
        resolve = cli_tests.store_module.normalize_inbox_row(
            {
                "action": "resolve",
                "message_id": "msg-1",
                "at": "2026-04-08T10:05:00-07:00",
                "actor": "supervisor",
            },
            inbox_file=inbox_file,
        )
        self.assertEqual(resolve["note"], "")
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_inbox_row({"action": "noop"}, inbox_file=inbox_file)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_inbox_row(
                {"action": "message", "at": "now"},
                inbox_file=inbox_file,
            )
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_inbox_row(
                {"action": "message", "message_id": "msg-2"},
                inbox_file=inbox_file,
            )
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_inbox_row(
                {"action": "message", "message_id": "msg-2", "at": "now", "recipient": "child", "kind": "instruction"},
                inbox_file=inbox_file,
            )
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_inbox_row(
                {"action": "resolve", "message_id": "msg-2", "at": "now"},
                inbox_file=inbox_file,
            )

        result_file = self.root / "result.json"
        result = cli_tests.store_module.normalize_task_result(
            {
                "task_id": "BLACK-1",
                "recorded_at": "2026-04-08T10:00:00-07:00",
                "actor": "codex",
                "run_id": "demo",
                "status": "success",
                "what_changed": None,
                "validation": [],
                "residual": [],
                "needs_user_input": False,
                "followup_candidates": [],
            },
            result_file=result_file,
        )
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["metadata"], {})
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_task_result([], result_file=result_file)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_task_result(
                {"task_id": "BLACK-1", "recorded_at": "now", "actor": "codex", "run_id": "demo", "status": "success", "needs_user_input": "no"},
                result_file=result_file,
            )
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_task_result(
                {
                    "recorded_at": "now",
                    "actor": "codex",
                    "run_id": "demo",
                    "status": "success",
                    "needs_user_input": False,
                },
                result_file=result_file,
            )

    def test_core_store_file_helpers_cover_json_and_atomic_round_trips(self) -> None:
        paths = self.init_demo_paths()

        installs_file = paths.control_dir / "tracked-installs.json"
        self.assertEqual(cli_tests.store_module.load_tracked_installs(paths), cli_tests.store_module.default_tracked_installs())
        installs_file.write_text("{broken", encoding="utf-8")
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.load_tracked_installs(paths)

        saved = cli_tests.store_module.save_tracked_installs(
            paths,
            {
                "repos": [
                    {"project_root": "/tmp/beta", "project_name": "Beta"},
                    {"project_root": "/tmp/alpha", "project_name": "Alpha"},
                    {"project_root": "/tmp/beta", "project_name": "Duplicate"},
                ]
            },
        )
        self.assertEqual(saved, installs_file)
        persisted = json.loads(installs_file.read_text(encoding="utf-8"))
        self.assertEqual([row["project_root"] for row in persisted["repos"]], ["/tmp/alpha", "/tmp/beta"])
        loaded_installs = cli_tests.store_module.load_tracked_installs(paths)
        self.assertEqual([row["project_root"] for row in loaded_installs["repos"]], ["/tmp/alpha", "/tmp/beta"])
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_tracked_installs([], installs_file=installs_file)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_tracked_installs({"repos": "oops"}, installs_file=installs_file)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_tracked_installs({"repos": [None]}, installs_file=installs_file)

        missing_state = self.root / "missing-state.json"
        self.assertEqual(cli_tests.store_module.load_state(missing_state), cli_tests.store_module.default_state())
        missing_state.write_text("{broken", encoding="utf-8")
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.load_state(missing_state)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_state([], state_file=missing_state)
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.normalize_state(
                {"approval_tasks": {}, "task_claims": []},
                state_file=missing_state,
            )
        normalized_state = cli_tests.store_module.normalize_state(
            {
                "approval_tasks": {"BLACK-1": {}},
                "task_claims": {"BLACK-1": {"status": "done"}},
            },
            state_file=missing_state,
        )
        self.assertEqual(normalized_state["approval_tasks"]["BLACK-1"]["status"], "pending")
        self.assertEqual(normalized_state["task_claims"]["BLACK-1"]["status"], "done")

        rows_file = self.root / "rows.jsonl"
        self.assertEqual(cli_tests.store_module.load_jsonl(rows_file), [])
        rows_file.write_text('{"one": 1}', encoding="utf-8")
        cli_tests.store_module.append_jsonl(rows_file, {"two": 2})
        self.assertEqual(cli_tests.store_module.load_jsonl(rows_file), [{"one": 1}, {"two": 2}])
        rows_file.write_text('{"ok": true}\n\n', encoding="utf-8")
        self.assertEqual(cli_tests.store_module.load_jsonl(rows_file), [{"ok": True}])
        rows_file.write_text("{broken\n", encoding="utf-8")
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.load_jsonl(rows_file)
        rows_file.write_text("[1, 2]\n", encoding="utf-8")
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.load_jsonl(rows_file)

        target = self.root / "atomic.txt"
        cli_tests.store_module.atomic_write_text(target, "hello\n")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")

        seen_temp: Path | None = None

        def before_replace(temp_path: Path) -> None:
            nonlocal seen_temp
            seen_temp = temp_path
            self.assertTrue(temp_path.exists())
            raise RuntimeError("stop before replace")

        with self.assertRaises(RuntimeError):
            cli_tests.store_module.atomic_write_text(target, "new\n", before_replace=before_replace)
        self.assertIsNotNone(seen_temp)
        self.assertFalse(seen_temp.exists())
        self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")

        state_file = self.root / "locked-state.json"
        with cli_tests.store_module.locked_state(state_file) as state:
            state["task_claims"]["BLACK-1"] = {"status": "done"}
        persisted_state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(persisted_state["task_claims"]["BLACK-1"]["status"], "done")

    def test_core_store_writer_helpers_cover_events_messages_and_results(self) -> None:
        paths = self.init_demo_paths()
        if paths.results_dir.exists():
            paths.results_dir.rmdir()
        self.assertEqual(cli_tests.store_module.load_task_results(paths), [])

        orphan_resolution = cli_tests.store_module.normalize_inbox_row(
            {
                "action": "resolve",
                "message_id": "missing",
                "at": "2026-04-08T09:55:00-07:00",
                "actor": "supervisor",
                "note": "ignore",
            },
            inbox_file=paths.inbox_file,
        )
        cli_tests.store_module.append_jsonl(paths.inbox_file, orphan_resolution)

        event = cli_tests.store_module.append_event(
            paths,
            event_type="claim",
            actor="codex",
            task_id="BLACK-1",
            payload={"status": "claimed"},
        )
        comment = cli_tests.store_module.record_comment(paths, actor="reviewer", body="Looks good", task_id="BLACK-1")
        message = cli_tests.store_module.send_message(
            paths,
            sender="supervisor",
            recipient="child",
            body="Do the task",
            kind="instruction",
            task_id="BLACK-1",
            reply_to="msg-0",
            tags=["core", "git-worktree"],
        )
        resolution = cli_tests.store_module.resolve_message(paths, message_id=message["message_id"], actor="child", note="done")
        result_path = cli_tests.store_module.record_task_result(
            paths,
            task_id="BLACK-1",
            actor="codex",
            status="success",
            what_changed=["Added direct store coverage."],
            validation=["make test-core"],
            residual=[],
            needs_user_input=False,
            followup_candidates=[],
            run_id="store-demo",
            metadata={"attempt": 1},
            task_shaping_telemetry={"elapsed_minutes": 5},
        )

        self.assertEqual(event["type"], "claim")
        self.assertEqual(comment["payload"]["body"], "Looks good")
        self.assertEqual(resolution["message_id"], message["message_id"])
        self.assertTrue(result_path.exists())
        self.assertEqual(len(cli_tests.store_module.load_events(paths, task_id="BLACK-1")), 4)
        self.assertEqual(len(cli_tests.store_module.load_events(paths, task_id="BLACK-1", limit=2)), 2)
        inbox_rows = cli_tests.store_module.load_inbox(paths, recipient="child", status="resolved", task_id="BLACK-1")
        self.assertEqual(len(inbox_rows), 1)
        self.assertEqual(inbox_rows[0]["reply_to"], "msg-0")
        self.assertEqual(inbox_rows[0]["resolution_note"], "done")
        result_rows = cli_tests.store_module.load_task_results(paths, task_id="BLACK-1")
        self.assertEqual(len(result_rows), 1)
        self.assertEqual(result_rows[0]["result_file"], str(result_path))

        bad_result = paths.results_dir / "BLACK-bad" / "broken.json"
        bad_result.parent.mkdir(parents=True, exist_ok=True)
        bad_result.write_text("{bad json}\n", encoding="utf-8")
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.load_task_results(paths)

        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.record_task_result(
                paths,
                task_id="BLACK-2",
                actor="codex",
                status="success",
                what_changed=[],
                validation=[],
                residual=[],
                needs_user_input=False,
                followup_candidates=[],
                metadata=[],
            )
        with self.assertRaises(cli_tests.store_module.StoreError):
            cli_tests.store_module.record_task_result(
                paths,
                task_id="BLACK-2",
                actor="codex",
                status="success",
                what_changed=[],
                validation=[],
                residual=[],
                needs_user_input=False,
                followup_candidates=[],
                task_shaping_telemetry=[],
            )

        entry: dict[str, object] = {"claim_expires_at": "soon"}
        cli_tests.store_module.claim_task_entry(entry, agent="codex", title="Title", summary={"bucket": "core"})
        self.assertEqual(entry["status"], "claimed")
        self.assertNotIn("claim_expires_at", entry)
        self.assertNotIn("claimed_pid", entry)

        cli_tests.store_module.claim_task_entry(
            entry,
            agent="codex",
            title="Title",
            summary={"bucket": "core"},
            claimed_pid=42,
        )
        self.assertEqual(entry["claimed_pid"], 42)
        self.assertEqual(entry["claimed_process_missing_scans"], 0)
