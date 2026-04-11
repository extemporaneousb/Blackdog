from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

from tests import test_blackdog_cli as cli_tests
from tests.core_audit_support import CoreAuditTestCase


backlog = cli_tests.backlog_module
snapshot_api = cli_tests.snapshot_module


class CoreBacklogFlowTests(CoreAuditTestCase):
    def _init_profile(self) -> object:
        cli_tests.run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        profile = cli_tests.load_profile(self.root)
        initial = backlog.render_initial_backlog(
            profile,
            objectives=["OBJ-1: Harden core backlog flows", "Loose objective"],
            push_objective=["Keep core runtime state deterministic."],
            release_gates=["Direct core tests cover backlog planning and runtime flows."],
        )
        cli_tests.atomic_write_text(profile.paths.backlog_file, initial)
        return profile

    def _add_task(
        self,
        profile,
        *,
        title: str,
        paths: list[str],
        bucket: str = "core",
        priority: str = "P2",
        risk: str = "medium",
        effort: str = "M",
        objective: str = "OBJ-1",
        requires_approval: bool = False,
        approval_reason: str = "",
        epic_id: str | None = "epic-core",
        epic_title: str | None = "Core hardening",
        lane_id: str | None = "lane-core",
        lane_title: str | None = "Core lane",
        wave: int | None = 0,
        task_shaping: dict[str, object] | None = None,
        docs: list[str] | None = None,
        checks: list[str] | None = None,
        domains: list[str] | None = None,
        affected_paths: list[str] | None = None,
    ) -> dict[str, object]:
        return backlog.add_task(
            profile,
            title=title,
            bucket=bucket,
            priority=priority,
            risk=risk,
            effort=effort,
            why=f"{title} needs direct core coverage.",
            evidence=f"{title} currently depends on indirect coverage only.",
            safe_first_slice=f"Exercise {title} directly from the core test suite.",
            paths=paths,
            checks=checks or ["make test-core"],
            docs=docs or ["docs/FILE_FORMATS.md"],
            domains=domains or ["state"],
            packages=[],
            affected_paths=affected_paths or paths,
            task_shaping=task_shaping,
            objective=objective,
            requires_approval=requires_approval,
            approval_reason=approval_reason,
            epic_id=epic_id,
            epic_title=epic_title,
            lane_id=lane_id,
            lane_title=lane_title,
            wave=wave,
        )

    def _task_claim_entry(self, task, *, status: str, actor: str, when: str) -> dict[str, object]:
        entry = {
            "status": status,
            "title": task.title,
            "bucket": task.payload["bucket"],
            "paths": task.payload["paths"],
            "priority": task.payload["priority"],
            "risk": task.payload["risk"],
        }
        if status == "claimed":
            entry.update({"claimed_by": actor, "claimed_at": when})
        elif status == "done":
            entry.update({"completed_by": actor, "completed_at": when})
        return entry

    def test_core_audit_backlog_low_level_helpers_cover_validation_and_git_fallbacks(self) -> None:
        self.assertIsNone(backlog._coerce_optional_int(None, field="elapsed"))
        self.assertIsNone(backlog._coerce_optional_int(" ", field="elapsed"))
        self.assertEqual(backlog._coerce_optional_int(12.0, field="elapsed"), 12)
        with self.assertRaises(backlog.BacklogError):
            backlog._coerce_optional_int(True, field="elapsed")
        with self.assertRaises(backlog.BacklogError):
            backlog._coerce_optional_int(1.5, field="elapsed")
        with self.assertRaises(backlog.BacklogError):
            backlog._coerce_optional_int(object(), field="elapsed")

        self.assertEqual(
            backlog._coerce_task_shaping_touched_paths(" README.md ", fallback_paths=["docs/CLI.md"]),
            ["README.md"],
        )
        self.assertEqual(
            backlog._coerce_task_shaping_touched_paths(None, fallback_paths=[" docs/CLI.md ", "docs/CLI.md"]),
            ["docs/CLI.md"],
        )
        with self.assertRaises(backlog.BacklogError):
            backlog._coerce_task_shaping_touched_paths(3, fallback_paths=[])
        with self.assertRaises(backlog.BacklogError):
            backlog._coerce_task_shaping_touched_paths([1], fallback_paths=[])
        with self.assertRaises(backlog.BacklogError):
            backlog._coerce_task_shaping([], fallback_paths=["README.md"])

        self.assertIsNone(backlog._rounded_task_minutes(None))
        self.assertEqual(backlog._rounded_task_minutes(2), 5)

        with patch.object(
            backlog.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=["git"], returncode=1, stdout="", stderr="fatal"),
        ):
            with self.assertRaises(backlog.BacklogError):
                backlog.run_git(self.root, "status")

        with patch.object(backlog, "run_git", side_effect=[backlog.BacklogError("missing"), "feature/demo"]):
            self.assertEqual(backlog.current_branch(self.root), "feature/demo")
        with patch.object(backlog, "run_git", side_effect=[backlog.BacklogError("missing"), backlog.BacklogError("still-missing")]):
            self.assertEqual(backlog.current_branch(self.root), "unknown")
        with patch.object(backlog, "run_git", side_effect=backlog.BacklogError("missing")):
            self.assertEqual(backlog.current_commit(self.root), "uncommitted")

        self.assertEqual(backlog._extract_json_blocks("```json backlog-task\n\n```\n", "backlog-task"), [])
        with self.assertRaises(backlog.BacklogError):
            backlog._extract_json_blocks("```json backlog-task\n{\"id\": 1}\n", "backlog-task")
        with self.assertRaises(backlog.BacklogError):
            backlog._extract_json_blocks("```json backlog-task\nnot-json\n```\n", "backlog-task")
        with self.assertRaises(backlog.BacklogError):
            backlog._extract_json_blocks("```json backlog-task\n[1, 2]\n```\n", "backlog-task")

    def test_core_audit_backlog_parse_helpers_cover_payload_validation_headers_and_load_errors(self) -> None:
        profile = self._init_profile()

        valid_task = {
            "id": "DEMO-1234567890",
            "title": "Valid payload",
            "bucket": "core",
            "priority": "P1",
            "risk": "medium",
            "effort": "M",
            "paths": "src/blackdog_core/backlog.py",
            "checks": "make test-core",
            "docs": "docs/FILE_FORMATS.md",
            "domains": "state",
            "requires_approval": True,
            "approval_reason": "Touches durable state.",
            "safe_first_slice": "Add the direct tests first.",
            "task_shaping": {"estimated_elapsed_minutes": "15"},
        }
        backlog.validate_task_payload(valid_task, profile)
        self.assertEqual(valid_task["paths"], ["src/blackdog_core/backlog.py"])
        self.assertEqual(valid_task["checks"], ["make test-core"])
        self.assertEqual(valid_task["docs"], ["docs/FILE_FORMATS.md"])
        self.assertEqual(valid_task["domains"], ["state"])

        invalid_tasks = [
            ({k: v for k, v in valid_task.items() if k != "id"}, "missing"),
            ({**valid_task, "id": " "}, "empty id"),
            ({**valid_task, "bucket": "bogus"}, "bucket"),
            ({**valid_task, "priority": "P9"}, "priority"),
            ({**valid_task, "risk": "severe"}, "risk"),
            ({**valid_task, "effort": "XL"}, "effort"),
            ({**valid_task, "paths": [""]}, "paths"),
            ({**valid_task, "checks": [""]}, "checks"),
            ({**valid_task, "docs": [""]}, "docs"),
            ({**valid_task, "domains": {"state": True}}, "domains"),
            ({**valid_task, "approval_reason": ""}, "approval"),
            ({**valid_task, "safe_first_slice": ""}, "safe-first-slice"),
        ]
        for payload, label in invalid_tasks:
            with self.subTest(label=label):
                with self.assertRaises(backlog.BacklogError):
                    backlog.validate_task_payload(payload, profile)

        valid_plan = {
            "epics": [{"id": "epic-core", "title": "Core", "task_ids": ["DEMO-1234567890"]}],
            "lanes": [{"id": "lane-core", "title": "Core lane", "wave": 0, "task_ids": ["DEMO-1234567890"]}],
        }
        backlog.validate_plan_payload(valid_plan, task_ids={"DEMO-1234567890"})
        invalid_plans = [
            ([], "plan object"),
            ({"epics": [], "lanes": {}}, "lanes list"),
            ({"epics": {}, "lanes": []}, "epics list"),
            ({"epics": ["bad"], "lanes": []}, "epic object"),
            ({"epics": [{"id": "", "task_ids": []}], "lanes": []}, "epic id"),
            ({"epics": [{"id": "epic", "task_ids": ["DEMO-missing"]}], "lanes": []}, "epic unknown"),
            ({"epics": [], "lanes": ["bad"]}, "lane object"),
            ({"epics": [], "lanes": [{"id": "", "wave": 0, "task_ids": []}]}, "lane id"),
            ({"epics": [], "lanes": [{"id": "lane", "wave": "bad", "task_ids": []}]}, "lane wave"),
            ({"epics": [], "lanes": [{"id": "lane", "wave": 0, "task_ids": ["DEMO-missing"]}]}, "lane unknown"),
            (
                {
                    "epics": [],
                    "lanes": [
                        {"id": "lane-a", "wave": 0, "task_ids": ["DEMO-1234567890"]},
                        {"id": "lane-b", "wave": 1, "task_ids": ["DEMO-1234567890"]},
                    ],
                },
                "lane duplicate task",
            ),
        ]
        for payload, label in invalid_plans:
            with self.subTest(label=label):
                with self.assertRaises(backlog.BacklogError):
                    backlog.validate_plan_payload(payload, task_ids={"DEMO-1234567890"})

        rendered = backlog.render_initial_backlog(
            profile,
            objectives=["OBJ-7: Explicit objective"],
            push_objective=["Push the direct coverage gate forward."],
            non_negotiables=["Keep core deterministic."],
            evidence_requirements=["Update tests with behavior changes."],
            release_gates=["Core tests exercise backlog flows directly."],
        )
        self.assertIn("OBJ-7: Explicit objective", rendered)
        self.assertIn("Core tests exercise backlog flows directly.", rendered)

        original = profile.paths.backlog_file.read_text(encoding="utf-8")
        profile.paths.backlog_file.unlink()
        with self.assertRaises(backlog.BacklogError):
            backlog.load_backlog(profile.paths, profile)
        backlog.refresh_backlog_headers(profile)

        cli_tests.atomic_write_text(
            profile.paths.backlog_file,
            original + "\n" + backlog.render_backlog_plan_block({"epics": [], "lanes": []}),
        )
        with self.assertRaises(backlog.BacklogError):
            backlog.load_backlog(profile.paths, profile)

        cli_tests.atomic_write_text(profile.paths.backlog_file, rendered)
        with patch.object(backlog, "now_iso", return_value="2026-04-08T12:00:00-07:00"), patch.object(
            backlog, "current_branch", return_value="feature/direct-core"
        ), patch.object(backlog, "current_commit", return_value="abc123def456"):
            backlog.refresh_backlog_headers(profile)
        refreshed = profile.paths.backlog_file.read_text(encoding="utf-8")
        self.assertIn("Target branch: `feature/direct-core`", refreshed)
        self.assertIn("Target commit: `abc123def456`", refreshed)

        self.assertTrue(backlog._replace_header("Project: `Old`\n", "Project", "New").startswith("Project: `New`"))
        self.assertTrue(backlog._replace_header("No header here\n", "Project", "New").startswith("Project: `New`"))
        self.assertIsNone(backlog._parse_runtime_iso(None))
        self.assertIsNone(backlog._parse_runtime_iso("not-an-iso"))
        self.assertIsNotNone(backlog._parse_runtime_iso("2026-04-08T12:00:00-07:00"))
        self.assertIsNone(backlog._rounded_minutes(None))
        self.assertEqual(backlog._rounded_minutes(125), 2)
        self.assertIsNone(backlog._runtime_int("bad"))
        self.assertEqual(backlog._runtime_int("15"), 15)
        self.assertFalse(backlog._result_has_actual_task_telemetry({"task_shaping_telemetry": {"estimate_delta_minutes": 5}}))
        self.assertTrue(backlog._result_has_actual_task_telemetry({"task_shaping_telemetry": {"actual_task_minutes": 25}}))
        self.assertEqual(backlog._unique_ordered([" docs/CLI.md ", "", "docs/CLI.md", "docs/FILE_FORMATS.md"]), ["docs/CLI.md", "docs/FILE_FORMATS.md"])

    def test_core_audit_backlog_status_views_and_exports_cover_planned_runtime_states(self) -> None:
        profile = self._init_profile()
        self._add_task(profile, title="Ready task", paths=["src/blackdog_core/backlog.py"], lane_id="lane-ready", lane_title="Ready", wave=0)
        self._add_task(profile, title="Claimed task", paths=["src/blackdog_core/profile.py"], lane_id="lane-claimed", lane_title="Claimed", wave=0)
        self._add_task(
            profile,
            title="Approval task",
            paths=["src/blackdog_core/state.py"],
            lane_id="lane-approval",
            lane_title="Approval",
            wave=0,
            requires_approval=True,
            approval_reason="Needs review.",
        )
        self._add_task(
            profile,
            title="High risk task",
            paths=["src/blackdog_core/backlog.py", "docs/FILE_FORMATS.md"],
            lane_id="lane-high",
            lane_title="High",
            wave=0,
            risk="high",
            effort="L",
        )
        self._add_task(profile, title="Lower wave blocker", paths=["src/blackdog_core/state.py"], lane_id="lane-blocker", lane_title="Blocker", wave=0)
        self._add_task(profile, title="Lower wave waiting", paths=["src/blackdog_core/profile.py"], lane_id="lane-later", lane_title="Later", wave=1)
        self._add_task(profile, title="Predecessor first", paths=["src/blackdog_core/backlog.py"], lane_id="lane-seq", lane_title="Sequence", wave=0)
        self._add_task(profile, title="Predecessor second", paths=["src/blackdog_core/backlog.py"], lane_id="lane-seq", lane_title="Sequence", wave=0)
        self._add_task(profile, title="Done task", paths=["src/blackdog_core/state.py"], lane_id="lane-done", lane_title="Done", wave=0)

        snapshot = backlog.load_backlog(profile.paths, profile)
        task_ids = {task.title: task.id for task in snapshot.tasks.values()}
        now = datetime.now().astimezone()
        state = {
            "schema_version": 1,
            "approval_tasks": {task_ids["Approval task"]: {"status": "pending", "title": "Approval task"}},
            "task_claims": {
                task_ids["Claimed task"]: {
                    **self._task_claim_entry(
                        snapshot.tasks[task_ids["Claimed task"]],
                        status="claimed",
                        actor="",
                        when=(now - timedelta(minutes=5)).isoformat(timespec="seconds"),
                    ),
                    "claimed_by": "",
                },
                task_ids["Done task"]: self._task_claim_entry(
                    snapshot.tasks[task_ids["Done task"]],
                    status="done",
                    actor="codex",
                    when=(now - timedelta(minutes=30)).isoformat(timespec="seconds"),
                ),
            },
        }

        approval_task = snapshot.tasks[task_ids["Approval task"]]
        claimed_task = snapshot.tasks[task_ids["Claimed task"]]
        done_task = snapshot.tasks[task_ids["Done task"]]
        high_risk_task = snapshot.tasks[task_ids["High risk task"]]
        waiting_wave_task = snapshot.tasks[task_ids["Lower wave waiting"]]
        waiting_predecessor_task = snapshot.tasks[task_ids["Predecessor second"]]
        ready_task = snapshot.tasks[task_ids["Ready task"]]

        self.assertEqual(backlog.active_claim_owner(claimed_task.id, state), "another-agent")
        self.assertEqual(backlog.blocking_reason(done_task, snapshot, state, allow_high_risk=False), "already done")
        self.assertEqual(backlog.blocking_reason(claimed_task, snapshot, state, allow_high_risk=False), "claimed by another-agent")
        self.assertEqual(backlog.blocking_reason(approval_task, snapshot, state, allow_high_risk=False), "approval required")
        self.assertEqual(
            backlog.blocking_reason(waiting_wave_task, snapshot, state, allow_high_risk=False),
            f"waiting for lower-wave task {task_ids['Ready task']}",
        )
        self.assertEqual(
            backlog.blocking_reason(waiting_predecessor_task, snapshot, state, allow_high_risk=False),
            f"waiting for predecessor {task_ids['Predecessor first']}",
        )
        self.assertEqual(backlog.blocking_reason(high_risk_task, snapshot, state, allow_high_risk=False), "high-risk item")
        self.assertIsNone(backlog.blocking_reason(high_risk_task, snapshot, state, allow_high_risk=True))

        self.assertEqual(backlog.classify_task_status(done_task, snapshot, state, allow_high_risk=False)[0], "done")
        self.assertEqual(backlog.classify_task_status(claimed_task, snapshot, state, allow_high_risk=False)[0], "claimed")
        self.assertEqual(backlog.classify_task_status(approval_task, snapshot, state, allow_high_risk=False)[0], "approval")
        self.assertEqual(backlog.classify_task_status(high_risk_task, snapshot, state, allow_high_risk=False)[0], "high-risk")
        self.assertEqual(backlog.classify_task_status(waiting_predecessor_task, snapshot, state, allow_high_risk=False)[0], "waiting")
        self.assertEqual(backlog.classify_task_status(ready_task, snapshot, state, allow_high_risk=False), ("ready", "claimable now"))

        planned_next = backlog.next_runnable_tasks(snapshot, state, allow_high_risk=False, limit=8)
        planned_titles = [task.title for task in planned_next]
        self.assertIn("Ready task", planned_titles)
        self.assertIn("Lower wave blocker", planned_titles)
        self.assertIn("Predecessor first", planned_titles)
        self.assertNotIn("High risk task", planned_titles)
        self.assertIn("High risk task", [task.title for task in backlog.next_runnable_tasks(snapshot, state, allow_high_risk=True, limit=8)])

        messages = [
            {
                "message_id": "keep",
                "status": "open",
                "task_id": task_ids["Ready task"],
                "sender": "operator",
                "recipient": "worker",
                "kind": "note",
                "body": "Keep this one visible.",
                "tags": [],
            },
            {
                "message_id": "done-hidden",
                "status": "open",
                "task_id": task_ids["Done task"],
                "sender": "operator",
                "recipient": "worker",
                "kind": "note",
                "body": "Done tasks should disappear.",
                "tags": [],
            },
            {
                "message_id": "supervisor-hidden",
                "status": "open",
                "task_id": "",
                "sender": "blackdog",
                "recipient": "supervisor/child-01",
                "kind": "instruction",
                "body": "Supervisor traffic should disappear.",
                "tags": ["supervisor-run"],
            },
            {
                "message_id": "unknown-hidden",
                "status": "open",
                "task_id": "DEMO-missing",
                "sender": "operator",
                "recipient": "worker",
                "kind": "note",
                "body": "Unknown tasks should disappear.",
                "tags": [],
            },
        ]
        results = [
            {
                "task_id": done_task.id,
                "status": "success",
                "recorded_at": "2026-04-08T11:30:00-07:00",
                "actor": "codex",
            },
            {
                "task_id": ready_task.id,
                "status": "partial",
                "recorded_at": "2026-04-08T11:20:00-07:00",
                "actor": "codex",
            },
        ]
        events = [
            {"event_id": "evt-1", "type": "claim", "at": "2026-04-08T11:00:00-07:00", "actor": "codex", "task_id": ready_task.id, "payload": {}},
            {"event_id": "evt-2", "type": "complete", "at": "2026-04-08T11:30:00-07:00", "actor": "codex", "task_id": done_task.id, "payload": {}},
        ]

        view = snapshot_api.build_runtime_summary(
            profile,
            snapshot,
            state,
            events=events,
            messages=messages,
            results=results,
            allow_high_risk=False,
        )
        self.assertEqual(view["counts"]["ready"], 3)
        self.assertEqual(view["counts"]["claimed"], 1)
        self.assertEqual(view["counts"]["done"], 1)
        self.assertEqual(view["counts"]["approval"], 1)
        self.assertEqual(view["counts"]["high-risk"], 1)
        self.assertGreaterEqual(view["counts"]["waiting"], 2)
        self.assertEqual(view["open_messages"][0]["message_id"], "keep")
        self.assertEqual(view["next_rows"][0]["title"], "Ready task")
        self.assertEqual(view["objectives"][0]["id"], "OBJ-1")
        self.assertIn("Ready", {lane["title"] for lane in view["lanes"]} | {row["lane_titles"][0] for row in view["objective_rows"] if row["lane_titles"]})

        filtered_messages = backlog.summary_open_messages(snapshot, state, messages)
        self.assertEqual([row["message_id"] for row in filtered_messages], ["keep"])

        plan_view = snapshot_api.build_plan_snapshot(profile, snapshot, state, allow_high_risk=False)
        self.assertEqual(plan_view["counts"]["tasks"], len(snapshot.tasks))
        self.assertGreaterEqual(plan_view["counts"]["lanes"], 1)
        self.assertIn("Wave 0", backlog.render_plan_text(plan_view))

        export = snapshot_api.build_runtime_snapshot(
            profile,
            snapshot,
            state,
            messages=messages,
            results=results,
            allow_high_risk=False,
        )
        exported_ready = next(row for row in export["tasks"] if row["id"] == ready_task.id)
        exported_done = next(row for row in export["tasks"] if row["id"] == done_task.id)
        self.assertEqual(exported_ready["objective_title"], "Harden core backlog flows")
        self.assertEqual(exported_done["latest_result_status"], "success")
        self.assertEqual(export["counts"]["ready"], 3)
        self.assertIn("Ready task", backlog.render_summary_text(view))

        focused_next = backlog.next_runnable_tasks(
            snapshot,
            state,
            allow_high_risk=False,
            limit=8,
            focus_task_ids=[task_ids["Predecessor second"]],
        )
        self.assertEqual([task.id for task in focused_next], [task_ids["Predecessor first"]])

        focused_view = snapshot_api.build_runtime_summary(
            profile,
            snapshot,
            state,
            events=events,
            messages=messages,
            results=results,
            allow_high_risk=False,
            focus_task_ids=[task_ids["Predecessor second"]],
        )
        self.assertEqual(focused_view["workset"]["visibility"], "focused")
        self.assertEqual(focused_view["workset"]["scope"]["kind"], "task_ids")
        self.assertEqual(focused_view["workset"]["scope"]["task_ids"], [task_ids["Predecessor second"]])
        self.assertEqual(
            focused_view["workset"]["task_ids"],
            [task_ids["Predecessor first"], task_ids["Predecessor second"]],
        )
        self.assertEqual(focused_view["total"], 2)
        self.assertEqual([row["id"] for row in focused_view["next_rows"]], [task_ids["Predecessor first"]])
        self.assertIn("Focus:", backlog.render_summary_text(focused_view))

        focused_export = snapshot_api.build_runtime_snapshot(
            profile,
            snapshot,
            state,
            messages=messages,
            results=results,
            allow_high_risk=False,
            focus_task_ids=[task_ids["Predecessor second"]],
        )
        self.assertEqual(
            focused_export["workset"]["task_ids"],
            [task_ids["Predecessor first"], task_ids["Predecessor second"]],
        )
        self.assertEqual(
            [row["id"] for row in focused_export["tasks"]],
            [task_ids["Predecessor first"], task_ids["Predecessor second"]],
        )
        self.assertEqual(
            [row["task_id"] for row in focused_export["runtime_model"]["task_states"]],
            [task_ids["Predecessor first"], task_ids["Predecessor second"]],
        )

    def test_core_audit_backlog_unplanned_queue_and_unlisted_objectives_are_modeled_directly(self) -> None:
        task_a = backlog.BacklogTask(
            payload={
                "id": "DEMO-a",
                "title": "Highest priority",
                "bucket": "core",
                "priority": "P1",
                "risk": "low",
                "effort": "S",
                "paths": ["a.py"],
                "checks": [],
                "docs": [],
                "domains": [],
                "requires_approval": False,
                "approval_reason": "",
                "safe_first_slice": "A",
                "task_shaping": backlog._coerce_task_shaping(None, fallback_paths=["a.py"]),
                "objective": "ADHOC",
            },
            narrative=backlog.TaskNarrative("why", "evidence", ("a.py",)),
            epic_title=None,
            lane_id=None,
            lane_title=None,
            wave=None,
            lane_order=None,
            lane_position=None,
            predecessor_ids=(),
        )
        task_b = backlog.BacklogTask(
            payload={
                "id": "DEMO-b",
                "title": "Lower priority",
                "bucket": "core",
                "priority": "P2",
                "risk": "medium",
                "effort": "M",
                "paths": ["b.py"],
                "checks": [],
                "docs": [],
                "domains": [],
                "requires_approval": False,
                "approval_reason": "",
                "safe_first_slice": "B",
                "task_shaping": backlog._coerce_task_shaping(None, fallback_paths=["b.py"]),
                "objective": "ADHOC",
            },
            narrative=backlog.TaskNarrative("why", "evidence", ("b.py",)),
            epic_title=None,
            lane_id=None,
            lane_title=None,
            wave=None,
            lane_order=None,
            lane_position=None,
            predecessor_ids=(),
        )
        snapshot = backlog.BacklogSnapshot(
            raw_text="# Demo\n",
            headers={},
            sections={},
            tasks={task_a.id: task_a, task_b.id: task_b},
            plan={"epics": [], "lanes": []},
        )
        state = {"task_claims": {}, "approval_tasks": {}}
        ready = backlog.next_runnable_tasks(snapshot, state, allow_high_risk=False, limit=2)
        self.assertEqual([task.id for task in ready], ["DEMO-a", "DEMO-b"])

        profile = self._init_profile()
        view = snapshot_api.build_runtime_summary(
            profile,
            snapshot,
            state,
            events=[],
            messages=[],
            results=[],
            allow_high_risk=False,
        )
        self.assertEqual(view["objective_rows"][0]["id"], "ADHOC")
        self.assertEqual(view["objective_rows"][0]["remaining"], 2)
        self.assertEqual(view["lanes"][0]["title"], "Unplanned")

    def test_core_audit_backlog_reconcile_and_strict_runtime_validation_cover_direct_runtime_paths(self) -> None:
        profile = self._init_profile()
        self._add_task(
            profile,
            title="Strict runtime task",
            paths=["src/blackdog_core/backlog.py"],
            lane_id="lane-runtime",
            lane_title="Runtime",
            wave=0,
            requires_approval=True,
            approval_reason="Runtime semantics changed.",
        )
        snapshot = backlog.load_backlog(profile.paths, profile)
        task = next(iter(snapshot.tasks.values()))

        reconciled, report = backlog.reconcile_state_for_backlog(
            {"schema_version": 1, "approval_tasks": [], "task_claims": "bad"},
            snapshot,
        )
        self.assertIsInstance(reconciled["approval_tasks"], dict)
        self.assertIsInstance(reconciled["task_claims"], dict)
        self.assertTrue(report["state_reconciled"])

        result_dir = self.runtime_paths().results_dir / task.id
        result_dir.mkdir(parents=True, exist_ok=True)
        good_result = result_dir / "20260408-120000-good.json"
        good_result.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": task.id,
                    "recorded_at": "2026-04-08T12:00:00-07:00",
                    "actor": "codex",
                    "run_id": "good",
                    "status": "success",
                    "what_changed": ["Recorded valid evidence."],
                    "validation": ["unit"],
                    "residual": [],
                    "needs_user_input": False,
                    "followup_candidates": [],
                    "metadata": {},
                    "task_shaping_telemetry": {},
                    "result_file": str(good_result),
                }
            )
            + "\n",
            encoding="utf-8",
        )
        wrong_dir = self.runtime_paths().results_dir / "OTHER-task"
        wrong_dir.mkdir(parents=True, exist_ok=True)
        wrong_result = wrong_dir / "20260408-120000-wrong.json"
        wrong_result.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": task.id,
                    "recorded_at": "2026-04-08T12:05:00-07:00",
                    "actor": "codex",
                    "run_id": "wrong",
                    "status": "failed",
                    "what_changed": ["Wrong directory."],
                    "validation": [],
                    "residual": [],
                    "needs_user_input": False,
                    "followup_candidates": [],
                    "metadata": {},
                    "task_shaping_telemetry": {},
                    "result_file": str(wrong_result),
                }
            )
            + "\n",
            encoding="utf-8",
        )

        direct_report = backlog._strict_runtime_validation(
            snapshot,
            messages=[
                {"message_id": "msg-unknown", "status": "open", "task_id": "DEMO-missing"},
            ],
            events=[
                {"event_id": "evt-a", "type": "task_result", "at": "2026-04-08T12:00:00-07:00", "actor": "codex", "task_id": task.id, "payload": {}},
                {
                    "event_id": "evt-b",
                    "type": "task_result",
                    "at": "2026-04-08T12:01:00-07:00",
                    "actor": "codex",
                    "task_id": task.id,
                    "payload": {"run_id": "missing", "result_file": str(result_dir / "missing.json")},
                },
                {
                    "event_id": "evt-c",
                    "type": "task_result",
                    "at": "2026-04-08T12:02:00-07:00",
                    "actor": "codex",
                    "task_id": task.id,
                    "payload": {"run_id": "wrong", "result_file": str(wrong_result)},
                },
                {
                    "event_id": "evt-d",
                    "type": "task_result",
                    "at": "2026-04-08T12:03:00-07:00",
                    "actor": "codex",
                    "task_id": task.id,
                    "payload": {"run_id": "good", "result_file": str(good_result)},
                },
            ],
            results=[
                {"task_id": "DEMO-missing", "run_id": "ghost", "result_file": str(good_result)},
                {"task_id": task.id, "run_id": "wrong", "result_file": str(wrong_result)},
                {"task_id": task.id, "run_id": "missing", "result_file": str(result_dir / "missing.json")},
                {"task_id": task.id, "run_id": "noevent", "result_file": str(good_result)},
            ],
        )
        self.assertGreaterEqual(direct_report["issue_count"], 6)
        self.assertIn("result_missing_task_result_event", direct_report["issue_count_by_kind"])
        self.assertIn("task_result_event_missing_fields", direct_report["issue_count_by_kind"])
        self.assertIn("task_result_event_missing_file", direct_report["issue_count_by_kind"])
        self.assertIn("task_result_event_task_mismatch", direct_report["issue_count_by_kind"])
        self.assertIn("inbox_unknown_task", direct_report["issue_count_by_kind"])
        self.assertIn("Strict validation failed", backlog._strict_validation_error(direct_report))

        runtime = snapshot_api.load_runtime_artifacts(profile, event_limit=1, strict_validate=False)
        self.assertEqual(runtime.backlog.tasks[task.id].title, "Strict runtime task")
        self.assertIsNone(runtime.strict_validation)

        cli_tests.send_message(
            profile.paths,
            sender="operator",
            recipient="worker",
            body="Unknown task should fail strict validation.",
            kind="note",
            task_id="DEMO-missing",
        )
        with self.assertRaises(backlog.BacklogError):
            snapshot_api.load_runtime_artifacts(profile, strict_validate=True)

    def test_core_audit_backlog_runtime_calibration_and_prompt_tuning_cover_direct_core_helpers(self) -> None:
        profile = self._init_profile()
        self._add_task(
            profile,
            title="Small completed task",
            paths=["src/blackdog_core/backlog.py"],
            effort="S",
            task_shaping={"estimated_elapsed_minutes": 20, "estimated_active_minutes": 15, "estimated_validation_minutes": 5},
            lane_id="lane-small",
            lane_title="Small",
            wave=0,
        )
        self._add_task(
            profile,
            title="Medium completed task",
            paths=["src/blackdog_core/state.py", "docs/FILE_FORMATS.md"],
            effort="M",
            task_shaping={"estimated_elapsed_minutes": 75, "estimated_active_minutes": 45, "estimated_validation_minutes": 10},
            docs=["docs/FILE_FORMATS.md", "docs/CLI.md"],
            checks=["make test-core", "make coverage-core"],
            domains=["state", "results", "events"],
            lane_id="lane-medium",
            lane_title="Medium",
            wave=1,
        )
        self._add_task(
            profile,
            title="Active task",
            paths=["src/blackdog_core/profile.py"],
            effort="L",
            task_shaping={"estimated_elapsed_minutes": 120, "estimated_active_minutes": 80, "estimated_validation_minutes": 20},
            lane_id="lane-active",
            lane_title="Active",
            wave=2,
        )
        snapshot = backlog.load_backlog(profile.paths, profile)
        task_ids = {task.title: task.id for task in snapshot.tasks.values()}

        now = datetime.now().astimezone()
        state = {
            "schema_version": 1,
            "approval_tasks": {},
            "task_claims": {
                task_ids["Small completed task"]: self._task_claim_entry(
                    snapshot.tasks[task_ids["Small completed task"]],
                    status="done",
                    actor="alpha",
                    when=(now - timedelta(hours=2)).isoformat(timespec="seconds"),
                ),
                task_ids["Medium completed task"]: self._task_claim_entry(
                    snapshot.tasks[task_ids["Medium completed task"]],
                    status="done",
                    actor="beta",
                    when=(now - timedelta(hours=1)).isoformat(timespec="seconds"),
                ),
                task_ids["Active task"]: self._task_claim_entry(
                    snapshot.tasks[task_ids["Active task"]],
                    status="claimed",
                    actor="gamma",
                    when=(now - timedelta(minutes=30)).isoformat(timespec="seconds"),
                ),
            },
        }
        cli_tests.save_state(profile.paths.state_file, state)

        def append_event(task_title: str, event_type: str, *, at_offset_minutes: int, actor: str = "codex", payload: dict[str, object] | None = None) -> None:
            cli_tests.append_jsonl(
                profile.paths.events_file,
                {
                    "event_id": f"{task_title}-{event_type}-{at_offset_minutes}",
                    "type": event_type,
                    "at": (now + timedelta(minutes=at_offset_minutes)).isoformat(timespec="seconds"),
                    "actor": actor,
                    "task_id": task_ids[task_title],
                    "payload": payload or {},
                },
            )

        append_event("Small completed task", "claim", at_offset_minutes=-120, actor="alpha")
        append_event("Small completed task", "worktree_start", at_offset_minutes=-119, payload={"branch": "agent/small", "target_branch": "main"})
        append_event("Small completed task", "child_launch", at_offset_minutes=-118, payload={"run_id": "run-small", "workspace": "/tmp/small", "target_branch": "main"})
        append_event("Small completed task", "complete", at_offset_minutes=-60, actor="alpha")
        append_event("Medium completed task", "claim", at_offset_minutes=-90, actor="beta")
        append_event("Medium completed task", "release", at_offset_minutes=-75, actor="beta")
        append_event("Medium completed task", "claim", at_offset_minutes=-70, actor="delta")
        append_event("Medium completed task", "child_launch_failed", at_offset_minutes=-69, payload={"run_id": "run-medium-1", "branch": "agent/medium"})
        append_event("Medium completed task", "child_finish", at_offset_minutes=-68, payload={"run_id": "run-medium-1", "workspace": "/tmp/medium", "land_error": "blocked"})
        append_event("Medium completed task", "worktree_land", at_offset_minutes=-67, payload={"land_error": "still blocked"})
        append_event("Medium completed task", "complete", at_offset_minutes=-20, actor="delta")

        cli_tests.record_task_result(
            profile.paths,
            task_id=task_ids["Small completed task"],
            actor="alpha",
            status="success",
            what_changed=["Recorded the small-task baseline."],
            validation=["unit"],
            residual=[],
            needs_user_input=False,
            followup_candidates=[],
            run_id="small",
            task_shaping_telemetry={"estimated_elapsed_minutes": 20},
        )
        cli_tests.record_task_result(
            profile.paths,
            task_id=task_ids["Medium completed task"],
            actor="delta",
            status="success",
            what_changed=["Recorded the medium-task baseline."],
            validation=["unit", "coverage"],
            residual=[],
            needs_user_input=False,
            followup_candidates=[],
            run_id="medium",
            task_shaping_telemetry={"estimated_elapsed_minutes": 75, "actual_task_minutes": 85, "actual_active_minutes": 70},
        )

        changed_file = self.root / "runtime-delta.txt"
        changed_file.write_text("delta\n", encoding="utf-8")

        events = cli_tests.load_events(profile.paths)
        results = cli_tests.load_task_results(profile.paths)
        runtime_rows = backlog._task_runtime_rows(
            snapshot=snapshot,
            state=cli_tests.load_state(profile.paths.state_file),
            events=events,
            results=results,
        )
        medium_runtime = runtime_rows[task_ids["Medium completed task"]]
        active_runtime = runtime_rows[task_ids["Active task"]]
        self.assertEqual(medium_runtime["claim_count"], 2)
        self.assertEqual(medium_runtime["actual_reclaim_count"], 1)
        self.assertGreaterEqual(medium_runtime["landing_failures"], 2)
        self.assertIsNotNone(active_runtime["active_task_seconds"])
        self.assertEqual(backlog._runtime_target_branch(task_id=task_ids["Small completed task"], events=events, fallback="fallback"), "main")
        self.assertEqual(backlog._runtime_target_branch(task_id="DEMO-missing", events=events, fallback="fallback"), "fallback")
        changed_paths = backlog._runtime_changed_paths(self.root, target_branch="missing-branch")
        self.assertIn("runtime-delta.txt", changed_paths)

        calibration = backlog._build_task_shaping_calibration(
            snapshot=snapshot,
            state=cli_tests.load_state(profile.paths.state_file),
            events=events,
            results=results,
        )
        seeded = backlog._seed_task_shaping_from_calibration(
            None,
            effort="M",
            risk="high",
            paths=["a.py", "b.py", "c.py"],
            checks=["check-a", "check-b"],
            docs=["doc-a", "doc-b", "doc-c"],
            domains=["state", "results", "events"],
            calibration=calibration,
        )
        self.assertEqual(seeded["estimate_basis_effort"], "M")
        self.assertGreaterEqual(int(seeded["estimated_elapsed_minutes"]), 75)
        self.assertGreaterEqual(int(seeded["estimated_validation_minutes"]), 10)

        enriched = backlog.enrich_result_task_shaping_telemetry(
            profile,
            task_id=task_ids["Medium completed task"],
            task_shaping_telemetry={"custom": "value"},
            cwd=self.root,
        )
        self.assertEqual(enriched["custom"], "value")
        self.assertIn("actual_task_minutes", enriched)
        self.assertIn("changed_paths", enriched)
        self.assertIn("estimate_accuracy_ratio", enriched)

        context_metrics = backlog._task_context_metrics(snapshot.tasks[task_ids["Medium completed task"]], medium_runtime)
        self.assertGreaterEqual(context_metrics["context_packet_score"], 1)
        self.assertGreaterEqual(context_metrics["document_routing_value_score"], 1)

        analysis = backlog.build_tune_analysis(profile)
        self.assertIn("recommendation", analysis)
        self.assertIn("categories", analysis)
        profiles = backlog.build_prompt_profiles(profile, analysis=analysis)
        self.assertEqual(set(profiles), {"low", "medium", "high"})
        improved = backlog.build_prompt_improvement(
            profile,
            prompt_text="Tighten the next tuning slice.",
            complexity="medium",
            analysis=analysis,
        )
        self.assertIn("Calibrated task-shaping defaults by effort", improved["improved_prompt"])
        with self.assertRaises(backlog.BacklogError):
            backlog.build_prompt_improvement(profile, prompt_text="x", complexity="bogus", analysis=analysis)
        with self.assertRaises(backlog.BacklogError):
            backlog.build_prompt_improvement(profile, prompt_text="   ", complexity="low", analysis=analysis)

        tune_payload = backlog._tune_task_payload(profile)
        self.assertEqual(tune_payload["objective"], "TUNING")
        first_seed, created = backlog.seed_tune_task(profile)
        second_seed, created_again = backlog.seed_tune_task(profile)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first_seed["id"], second_seed["id"])

    def test_core_audit_backlog_plan_mutation_helpers_cover_direct_task_updates_and_sweeps(self) -> None:
        profile = self._init_profile()
        keep = self._add_task(profile, title="Keep task", paths=["src/blackdog_core/backlog.py"], lane_id="lane-a", lane_title="Lane A", wave=0)
        done = self._add_task(profile, title="Done then sweep", paths=["src/blackdog_core/state.py"], lane_id="lane-b", lane_title="Lane B", wave=2)
        remove = self._add_task(profile, title="Remove task", paths=["docs/CLI.md"], lane_id="lane-c", lane_title="Lane C", wave=3)

        snapshot = backlog.load_backlog(profile.paths, profile)
        keep_id = keep["id"]
        done_id = done["id"]
        remove_id = remove["id"]
        plan = json.loads(json.dumps(snapshot.plan))

        self.assertIsNotNone(
            backlog._plan_entry_for_task({"epics": plan["epics"], "lanes": ["bad", *plan["lanes"]]}, collection="lanes", task_id=keep_id)
        )
        self.assertIsNone(backlog._plan_entry_for_task(plan, collection="lanes", task_id="DEMO-missing"))

        no_plan_text = re.sub(r"```json backlog-plan\n.*?\n```\n?", "", snapshot.raw_text, flags=re.S)
        inserted = backlog._replace_plan_block(no_plan_text, {"epics": [], "lanes": []})
        self.assertIn("```json backlog-plan", inserted)
        replaced = backlog._replace_plan_block(snapshot.raw_text, {"epics": [], "lanes": []})
        self.assertIn("```json backlog-plan", replaced)
        with self.assertRaises(backlog.BacklogError):
            backlog._replace_plan_block("## No lane plan here\n", {"epics": [], "lanes": []})

        ensured_epic = backlog._ensure_plan_entry({"epics": [], "lanes": []}, kind="epic", entry_id="epic-x", title="Epic X")
        self.assertEqual(ensured_epic["id"], "epic-x")
        existing_lane = backlog._ensure_plan_entry({"epics": [], "lanes": [{"id": "lane-x", "title": "", "task_ids": []}]}, kind="lane", entry_id="lane-x", title="Lane X", wave=4)
        self.assertEqual(existing_lane["title"], "Lane X")
        self.assertEqual(existing_lane["wave"], 4)

        pruned = backlog._prune_empty_plan_entries(
            {
                "epics": [{"id": "epic-empty", "task_ids": []}, {"id": "epic-keep", "task_ids": [keep_id]}, "bad"],
                "lanes": [{"id": "lane-empty", "task_ids": []}, {"id": "lane-keep", "task_ids": [keep_id]}, "bad"],
            }
        )
        self.assertEqual([entry["id"] for entry in pruned["epics"]], ["epic-keep"])
        self.assertEqual([entry["id"] for entry in pruned["lanes"]], ["lane-keep"])

        removed_text = backlog._remove_task_section(snapshot.raw_text, remove_id)
        self.assertNotIn(f"### {remove_id} -", removed_text)
        with self.assertRaises(backlog.BacklogError):
            backlog._remove_task_section(snapshot.raw_text, "DEMO-missing")
        replaced_text = backlog._replace_task_section(
            snapshot.raw_text,
            keep_id,
            backlog.render_task_section(
                {**snapshot.tasks[keep_id].payload, "title": "Updated keep title"},
                why="Updated why",
                evidence="Updated evidence",
                affected_paths=["src/blackdog_core/backlog.py"],
            ),
        )
        self.assertIn("Updated keep title", replaced_text)
        with self.assertRaises(backlog.BacklogError):
            backlog._replace_task_section(snapshot.raw_text, "DEMO-missing", "replacement")

        updated = backlog.update_task(
            profile,
            task_id=keep_id,
            title="Keep task updated",
            objective="OBJ-2",
            docs=["docs/CLI.md"],
            domains=["state", "results"],
            task_shaping={"estimated_validation_minutes": 25},
            lane_id="lane-z",
            lane_title="Lane Z",
            wave=5,
        )
        self.assertEqual(updated["title"], "Keep task updated")
        self.assertEqual(updated["objective"], "OBJ-2")
        self.assertEqual(updated["task_shaping"]["estimated_validation_minutes"], 25)

        with self.assertRaises(backlog.BacklogError):
            backlog.update_task(profile, task_id="DEMO-missing", title="Missing")

        snapshot = backlog.load_backlog(profile.paths, profile)
        blocked_state = {
            "schema_version": 1,
            "approval_tasks": {},
            "task_claims": {
                keep_id: self._task_claim_entry(
                    snapshot.tasks[keep_id],
                    status="claimed",
                    actor="codex",
                    when=datetime.now().astimezone().isoformat(timespec="seconds"),
                )
            },
        }
        cli_tests.save_state(profile.paths.state_file, blocked_state)
        with self.assertRaises(backlog.BacklogError):
            backlog.update_task(profile, task_id=keep_id, title="Blocked by claim")

        cli_tests.save_state(profile.paths.state_file, {"schema_version": 1, "approval_tasks": {}, "task_claims": {}})
        cli_tests.record_task_result(
            profile.paths,
            task_id=remove_id,
            actor="codex",
            status="success",
            what_changed=["Recorded result before remove."],
            validation=["unit"],
            residual=[],
            needs_user_input=False,
            followup_candidates=[],
            run_id="remove-block",
        )
        with self.assertRaises(backlog.BacklogError):
            backlog.remove_task(profile, task_id=remove_id)

        removable = self._add_task(profile, title="Fresh removable", paths=["README.md"], lane_id="lane-r", lane_title="Lane R", wave=6)
        removed_payload = backlog.remove_task(profile, task_id=removable["id"])
        self.assertEqual(removed_payload["id"], removable["id"])

        snapshot = backlog.load_backlog(profile.paths, profile)
        cli_tests.save_state(
            profile.paths.state_file,
            {
                "schema_version": 1,
                "approval_tasks": {},
                "task_claims": {
                    done_id: self._task_claim_entry(
                        snapshot.tasks[done_id],
                        status="done",
                        actor="codex",
                        when=datetime.now().astimezone().isoformat(timespec="seconds"),
                    )
                },
            },
        )
        compacted_plan, compact_meta = backlog.compact_active_plan(snapshot, cli_tests.load_state(profile.paths.state_file))
        self.assertIn(done_id, compact_meta["removed_task_ids"])
        self.assertIn("lane-b", compact_meta["removed_lane_ids"])
        self.assertEqual(compact_meta["wave_map"], {"3": 0, "5": 1})
        empty_compacted_plan, empty_compact_meta = backlog.compact_active_plan(
            replace(
                snapshot,
                plan={
                    "epics": ["bad", {"id": "epic-done", "title": "Done Epic", "task_ids": [done_id]}],
                    "lanes": ["bad", {"id": "lane-done", "title": "Done Lane", "wave": 4, "task_ids": [done_id]}],
                },
            ),
            cli_tests.load_state(profile.paths.state_file),
        )
        self.assertEqual(empty_compacted_plan, {"epics": [], "lanes": []})
        self.assertIn("epic-done", empty_compact_meta["removed_epic_ids"])
        sweep = backlog.sweep_completed_tasks(profile)
        self.assertTrue(sweep["changed"])
        self.assertIn(done_id, sweep["removed_task_ids"])

    def test_core_audit_backlog_empty_render_edges_and_runtime_branches_cover_remaining_helpers(self) -> None:
        profile = self._init_profile()
        snapshot = backlog.load_backlog(profile.paths, profile)

        reset_state, _ = backlog.reconcile_state_for_backlog("bad-state", snapshot)
        self.assertEqual(reset_state["approval_tasks"], {})
        self.assertEqual(reset_state["task_claims"], {})

        payload = self._add_task(
            profile,
            title="Approval object reset",
            paths=["src/blackdog_core/backlog.py"],
            requires_approval=True,
            approval_reason="Reset the approval row.",
            lane_id="lane-reset",
            lane_title="Reset",
            wave=0,
        )
        snapshot = backlog.load_backlog(profile.paths, profile)
        repaired, _ = backlog.reconcile_state_for_backlog(
            {"schema_version": 1, "approval_tasks": {payload["id"]: "bad"}, "task_claims": {}},
            snapshot,
        )
        self.assertIsInstance(repaired["approval_tasks"][payload["id"]], dict)

        self.assertEqual(
            backlog._section_items(["```json", "1. numbered", "- dashed", " ", "```"]),
            ["numbered", "dashed"],
        )
        latest = backlog._latest_result_index(
            [
                {"task_id": "", "status": "skip"},
                {"task_id": payload["id"], "status": "first", "recorded_at": "1", "actor": "a"},
                {"task_id": payload["id"], "status": "second", "recorded_at": "2", "actor": "b"},
            ]
        )
        self.assertEqual(latest[payload["id"]]["status"], "first")

        empty_plan_view = {"project_name": "Demo", "counts": {"epics": 0, "lanes": 0, "waves": 0, "tasks": 0}, "waves": [], "epics": [], "lanes": []}
        self.assertIn("- No waves defined.", backlog.render_plan_text(empty_plan_view))
        self.assertIn("- No epics defined.", backlog.render_plan_text(empty_plan_view))
        self.assertIn("- No lanes defined.", backlog.render_plan_text(empty_plan_view))
        empty_summary_view = {
            "project_name": "Demo",
            "total": 0,
            "counts": {"ready": 0, "claimed": 0, "done": 0, "approval": 0, "waiting": 0, "high-risk": 0},
            "next_rows": [],
            "open_messages": [],
        }
        self.assertIn("- No runnable tasks.", backlog.render_summary_text(empty_summary_view))
        self.assertEqual(backlog.next_runnable_tasks(snapshot, {"task_claims": {}, "approval_tasks": {}}, allow_high_risk=False, limit=3), [])
        self.assertEqual(backlog.next_runnable_tasks(snapshot, {"task_claims": {payload["id"]: {"status": "done"}}}, allow_high_risk=False, limit=3), [])
        self.assertEqual(
            backlog.summary_open_messages(
                snapshot,
                {"task_claims": {}, "approval_tasks": {}},
                [
                    {"status": "resolved", "task_id": payload["id"], "sender": "user", "recipient": "agent", "tags": []},
                    {"status": "open", "task_id": payload["id"], "sender": "user", "recipient": "agent", "tags": ["supervisor-run"]},
                    {"status": "open", "task_id": payload["id"], "sender": "user", "recipient": "agent", "tags": []},
                ],
            ),
            [{"status": "open", "task_id": payload["id"], "sender": "user", "recipient": "agent", "tags": []}],
        )

        weird_error = backlog._strict_validation_error(
            {
                "issues": [
                    {"kind": "file-only", "result_file": "/tmp/result.json"},
                    {"kind": "run-only", "run_id": "run-1"},
                    {"kind": "unknown"},
                ]
            }
        )
        self.assertIn("/tmp/result.json", weird_error)
        self.assertIn("run-1", weird_error)
        self.assertIn("unknown", weird_error)

        real_exists = Path.exists
        call_count = {"value": 0}

        def fake_exists(path: Path) -> bool:
            if path == profile.paths.backlog_file:
                call_count["value"] += 1
                return call_count["value"] == 1
            return real_exists(path)

        with patch.object(backlog, "locked_path", return_value=nullcontext()), patch.object(Path, "exists", new=fake_exists):
            backlog.refresh_backlog_headers(profile)

        result_rows = backlog._task_runtime_rows(
            snapshot=snapshot,
            state={"task_claims": {payload["id"]: "bad"}},
            events=[
                {
                    "event_id": "worktree-path",
                    "type": "worktree_start",
                    "at": "2026-04-08T12:00:00-07:00",
                    "actor": "codex",
                    "task_id": payload["id"],
                    "payload": {"worktree_path": "/tmp/worktree"},
                }
            ],
            results=[
                {"task_id": "", "status": "skip"},
                {"task_id": payload["id"], "task_shaping_telemetry": {"estimated_active_minutes": 9}},
            ],
        )
        self.assertEqual(result_rows[payload["id"]]["estimated_active_minutes"], 9)
        self.assertEqual(result_rows[payload["id"]]["actual_worktrees_used"], 1)

        with patch.object(backlog, "_rounded_task_minutes", side_effect=lambda value: None if value in {5, 15, 20, 25, 30, 55, 90, 105, 180} else value):
            calibration = backlog._build_task_shaping_calibration(
                snapshot=backlog.BacklogSnapshot(raw_text="", headers={}, sections={}, tasks={}, plan={"epics": [], "lanes": []}),
                state={"task_claims": {}},
                events=[],
                results=[],
            )
        self.assertEqual(calibration["by_effort"]["S"]["seeded_elapsed_minutes"], 30)
        self.assertEqual(calibration["by_effort"]["M"]["seeded_active_minutes"], 55)
        self.assertGreaterEqual(calibration["by_effort"]["L"]["seeded_validation_minutes"], 25)

        shaped = self._add_task(
            profile,
            title="Calibration fallback",
            paths=["src/blackdog_core/backlog.py"],
            effort="S",
            lane_id="lane-shape",
            lane_title="Shape",
            wave=1,
            task_shaping={"estimated_elapsed_minutes": 42, "estimated_active_minutes": 21},
        )
        snapshot = backlog.load_backlog(profile.paths, profile)
        with patch.object(
            backlog,
            "_task_runtime_rows",
            return_value={
                shaped["id"]: {
                    "actual_task_minutes": None,
                    "estimated_elapsed_minutes": None,
                    "estimated_active_minutes": None,
                }
            },
        ):
            fallback_calibration = backlog._build_task_shaping_calibration(
                snapshot=snapshot,
                state={"task_claims": {shaped["id"]: {"status": "done"}}},
                events=[],
                results=[],
            )
        self.assertEqual(fallback_calibration["by_effort"]["S"]["estimate_sample_size"], 1)
        self.assertEqual(fallback_calibration["by_effort"]["S"]["median_estimated_elapsed_minutes"], 42)
        self.assertEqual(fallback_calibration["by_effort"]["S"]["median_estimated_active_minutes"], 21)

        elapsed_only = self._add_task(
            profile,
            title="Elapsed-only context",
            paths=["src/blackdog_core/backlog.py"],
            effort="M",
            lane_id="lane-context",
            lane_title="Context",
            wave=2,
            task_shaping={"estimated_elapsed_minutes": 36},
        )
        snapshot = backlog.load_backlog(profile.paths, profile)
        context_metrics = backlog._task_context_metrics(
            replace(
                snapshot.tasks[elapsed_only["id"]],
                payload={**snapshot.tasks[elapsed_only["id"]].payload, "task_shaping": {"estimated_elapsed_minutes": 36}},
            ),
            {
                "actual_touched_path_count": 2,
                "estimated_active_minutes": None,
                "estimated_elapsed_minutes": None,
                "actual_task_minutes": 40,
                "actual_reclaim_count": 0,
                "actual_retry_count": 0,
                "landing_failures": 0,
            },
        )
        self.assertEqual(context_metrics["context_efficiency_ratio"], 2.0)
        self.assertGreaterEqual(context_metrics["document_routing_value_score"], 1)

    def test_core_audit_backlog_tune_branch_selection_and_path_fallbacks_cover_remaining_analysis_paths(self) -> None:
        profile = self._init_profile()
        self._add_task(profile, title="Tune A", paths=["a.py"], effort="S", lane_id="lane-ta", lane_title="Tune A", wave=0)
        self._add_task(profile, title="Tune B", paths=["b.py"], effort="M", lane_id="lane-tb", lane_title="Tune B", wave=1)
        self._add_task(profile, title="Tune C", paths=["c.py"], effort="L", lane_id="lane-tc", lane_title="Tune C", wave=2)
        self._add_task(profile, title="Tune D", paths=["d.py"], effort="M", lane_id="lane-td", lane_title="Tune D", wave=3)
        self._add_task(profile, title="Tune E", paths=["e.py"], effort="S", lane_id="lane-te", lane_title="Tune E", wave=4)
        snapshot = backlog.load_backlog(profile.paths, profile)
        task_ids = {task.title: task.id for task in snapshot.tasks.values()}
        state = {
            "task_claims": {
                task_ids["Tune A"]: {"status": "done"},
                task_ids["Tune B"]: {"status": "done"},
                task_ids["Tune C"]: {"status": "done"},
                task_ids["Tune D"]: {"status": "done"},
                task_ids["Tune E"]: {"status": "done"},
            }
        }
        profile_outside = replace(profile, paths=replace(profile.paths, project_root=Path("/tmp/blackdog-outside-root")))

        def run_analysis(runtime_rows: dict[str, dict[str, object]], *, results: list[dict[str, object]] | None = None) -> dict[str, object]:
            with patch.object(backlog, "locked_path", return_value=nullcontext()), patch.object(
                backlog, "load_backlog", return_value=snapshot
            ), patch.object(backlog, "load_state", return_value=state), patch.object(
                backlog, "sync_state_for_backlog", side_effect=lambda current, _snapshot: current
            ), patch.object(backlog, "load_events", return_value=[]), patch.object(
                backlog, "load_task_results", return_value=results or []
            ), patch.object(
                backlog, "_task_runtime_rows", return_value=runtime_rows
            ), patch.object(
                backlog,
                "_build_task_shaping_calibration",
                return_value={"default_active_ratio": 0.62, "by_effort": {"S": {}, "M": {}, "L": {}}},
            ):
                return backlog.build_tune_analysis(profile)

        not_ready = run_analysis(
            {
                task_ids["Tune A"]: {"actual_task_seconds": None, "actual_task_minutes": None, "estimated_active_minutes": None, "estimated_elapsed_minutes": None, "actual_retry_count": 1, "actual_reclaim_count": 0, "landing_failures": 0}
            }
        )
        self.assertEqual(not_ready["recommendation"]["focus"], "task_time_calibration")

        mean_error = run_analysis(
            {
                task_ids["Tune A"]: {"actual_task_seconds": 600, "actual_task_minutes": 40, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune B"]: {"actual_task_seconds": 700, "actual_task_minutes": 45, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune C"]: {"actual_task_seconds": 800, "actual_task_minutes": 50, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune D"]: {"actual_task_seconds": 900, "actual_task_minutes": 55, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune E"]: {"actual_task_seconds": 1000, "actual_task_minutes": 60, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
            }
        )
        self.assertEqual(mean_error["recommendation"]["focus"], "task_time_calibration")

        landing = run_analysis(
            {
                task_ids["Tune A"]: {"actual_task_seconds": 600, "actual_task_minutes": 12, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 1},
                task_ids["Tune B"]: {"actual_task_seconds": 700, "actual_task_minutes": 11, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune C"]: {"actual_task_seconds": 800, "actual_task_minutes": 9, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune D"]: {"actual_task_seconds": 900, "actual_task_minutes": 10, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune E"]: {"actual_task_seconds": 1000, "actual_task_minutes": 10, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
            }
        )
        self.assertEqual(landing["recommendation"]["focus"], "landing_failures")

        retry = run_analysis(
            {
                task_ids["Tune A"]: {"actual_task_seconds": 600, "actual_task_minutes": 12, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 2, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune B"]: {"actual_task_seconds": 700, "actual_task_minutes": 11, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune C"]: {"actual_task_seconds": 800, "actual_task_minutes": 9, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune D"]: {"actual_task_seconds": 900, "actual_task_minutes": 10, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune E"]: {"actual_task_seconds": 1000, "actual_task_minutes": 10, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
            }
        )
        self.assertEqual(retry["recommendation"]["focus"], "retry_pressure")

        healthy = run_analysis(
            {
                task_ids["Tune A"]: {"actual_task_seconds": 600, "actual_task_minutes": 11, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune B"]: {"actual_task_seconds": 700, "actual_task_minutes": 10, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune C"]: {"actual_task_seconds": 800, "actual_task_minutes": 9, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune D"]: {"actual_task_seconds": 900, "actual_task_minutes": 10, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
                task_ids["Tune E"]: {"actual_task_seconds": 1000, "actual_task_minutes": 10, "estimated_active_minutes": 10, "estimated_elapsed_minutes": 10, "actual_retry_count": 0, "actual_reclaim_count": 0, "landing_failures": 0},
            }
        )
        self.assertEqual(healthy["recommendation"]["focus"], "backlog_health")

        fallback_analysis = {
            "recommendation": {"focus": "backlog_health", "summary": "Stable enough."},
            "categories": {},
            "calibration": {
                "by_effort": {
                    "S": {"seeded_elapsed_minutes": None, "seeded_active_minutes": None, "seeded_validation_minutes": None, "completed_sample_size": 0},
                    "M": {"seeded_elapsed_minutes": 60, "seeded_active_minutes": 30, "seeded_validation_minutes": 10, "completed_sample_size": 1},
                }
            },
        }
        fallback_profiles = backlog.build_prompt_profiles(profile_outside, analysis=fallback_analysis)
        self.assertTrue(fallback_profiles["low"]["routed_docs"])
        fallback_improvement = backlog.build_prompt_improvement(
            profile_outside,
            prompt_text="Review the tuning contract.",
            complexity="medium",
            analysis=fallback_analysis,
        )
        self.assertIn("M: elapsed 60m", fallback_improvement["improved_prompt"])
        self.assertNotIn("S: elapsed", fallback_improvement["improved_prompt"])

        with patch.object(backlog, "build_tune_analysis", return_value={**fallback_analysis, "tasks_with_recorded_compute": 3, "estimated_time_samples": 3, "retry_total": 0, "landing_failures": 0, "results_with_actual_task_telemetry": 3, "result_files": 3, "coverage_gaps": []}):
            tune_payload = backlog._tune_task_payload(profile_outside)
        self.assertIn("Compare recorded task time against the current estimate contract", tune_payload["safe_first_slice"])

        existing_id = backlog.make_task_id(
            profile,
            bucket="skills",
            title="Auto-tune runtime contract and backlog health",
            paths=tune_payload["paths"],
        )
        existing_snapshot = replace(snapshot, tasks={**snapshot.tasks, existing_id: next(iter(snapshot.tasks.values()))})
        with patch.object(backlog, "locked_path", return_value=nullcontext()), patch.object(
            backlog, "_tune_task_payload", return_value=tune_payload
        ), patch.object(
            backlog, "load_backlog", side_effect=[snapshot, existing_snapshot]
        ), patch.object(
            backlog, "add_task", side_effect=backlog.BacklogError("race")
        ):
            payload, created = backlog.seed_tune_task(profile)
        self.assertFalse(created)
        self.assertEqual(payload["id"], next(iter(existing_snapshot.tasks.values())).id)

        with patch.object(backlog, "locked_path", return_value=nullcontext()), patch.object(
            backlog, "_tune_task_payload", return_value=tune_payload
        ), patch.object(
            backlog, "load_backlog", side_effect=[snapshot, snapshot]
        ), patch.object(
            backlog, "add_task", side_effect=backlog.BacklogError("still-failing")
        ):
            with self.assertRaises(backlog.BacklogError):
                backlog.seed_tune_task(profile)

    def test_core_audit_backlog_remaining_mutation_error_paths_cover_direct_add_update_remove_edges(self) -> None:
        profile = self._init_profile()
        task = self._add_task(profile, title="Mutable task", paths=["src/blackdog_core/backlog.py"], lane_id="lane-m", lane_title="Mutable", wave=0)
        with self.assertRaises(backlog.BacklogError):
            self._add_task(profile, title="Mutable task", paths=["src/blackdog_core/backlog.py"], lane_id="lane-m", lane_title="Mutable", wave=0)

        malformed_snapshot = backlog.BacklogSnapshot(
            raw_text="# Demo\n## Lane Plan",
            headers={},
            sections={},
            tasks={},
            plan={"epics": None, "lanes": None},
        )
        with patch.object(backlog, "locked_path", return_value=nullcontext()), patch.object(
            backlog, "load_backlog", return_value=malformed_snapshot
        ), patch.object(backlog, "load_state", return_value={}), patch.object(
            backlog, "load_events", return_value=[]
        ), patch.object(
            backlog, "load_task_results", return_value=[]
        ), patch.object(
            backlog, "_apply_runtime_headers", return_value=malformed_snapshot.raw_text
        ), patch.object(
            backlog, "render_task_section", return_value="section"
        ), patch.object(
            backlog, "atomic_write_text"
        ):
            added = backlog.add_task(
                profile,
                title="Patched add",
                bucket="core",
                priority="P1",
                risk="medium",
                effort="S",
                why="why",
                evidence="evidence",
                safe_first_slice="slice",
                paths=["a.py"],
                checks=[],
                docs=[],
                domains=[],
                packages=[],
                affected_paths=["a.py"],
                task_shaping=None,
                objective="OBJ-1",
                requires_approval=False,
                approval_reason="",
                epic_id=None,
                epic_title=None,
                lane_id=None,
                lane_title=None,
                wave=None,
            )
        self.assertEqual(added["title"], "Patched add")

        snapshot = backlog.load_backlog(profile.paths, profile)
        task_id = task["id"]
        cli_tests.record_task_result(
            profile.paths,
            task_id=task_id,
            actor="codex",
            status="success",
            what_changed=["Result blocks edit."],
            validation=["unit"],
            residual=[],
            needs_user_input=False,
            followup_candidates=[],
            run_id="update-block",
        )
        with self.assertRaises(backlog.BacklogError):
            backlog.update_task(profile, task_id=task_id, title="blocked by result")

        fresh = self._add_task(profile, title="Another mutable task", paths=["src/blackdog_core/state.py"], lane_id="lane-n", lane_title="Mutable N", wave=1)
        updated = backlog.update_task(
            profile,
            task_id=fresh["id"],
            bucket="docs",
            priority="P1",
            risk="low",
            effort="S",
            safe_first_slice="new slice",
            paths=["docs/CLI.md"],
            checks=["make coverage-core"],
            packages=["pkg-a"],
            requires_approval=True,
            approval_reason="Needs approval now.",
            affected_paths=["docs/CLI.md", "docs/FILE_FORMATS.md"],
        )
        self.assertEqual(updated["bucket"], "docs")
        self.assertEqual(updated["priority"], "P1")
        self.assertEqual(updated["risk"], "low")
        self.assertEqual(updated["effort"], "S")
        self.assertEqual(updated["paths"], ["docs/CLI.md"])
        self.assertEqual(updated["checks"], ["make coverage-core"])
        self.assertEqual(updated["packages"], ["pkg-a"])
        self.assertTrue(updated["requires_approval"])

        patched_snapshot = replace(
            backlog.load_backlog(profile.paths, profile),
            plan={
                "epics": [
                    "bad",
                    {"id": "epic-patched", "title": "Patched Epic", "task_ids": [fresh["id"]]},
                ],
                "lanes": [
                    "bad",
                    {"id": "lane-patched", "title": "Patched Lane", "wave": 1, "task_ids": [fresh["id"]]},
                ],
            },
        )
        with patch.object(backlog, "locked_path", return_value=nullcontext()), patch.object(
            backlog, "load_backlog", return_value=patched_snapshot
        ), patch.object(
            backlog, "load_state", return_value={"schema_version": 1, "approval_tasks": {}, "task_claims": {}}
        ), patch.object(
            backlog, "load_task_results", return_value=[]
        ), patch.object(
            backlog, "atomic_write_text"
        ):
            patched = backlog.update_task(profile, task_id=fresh["id"], title="Patched updated title")
        self.assertEqual(patched["title"], "Patched updated title")

        with self.assertRaises(backlog.BacklogError):
            backlog.remove_task(profile, task_id="DEMO-missing")

        snapshot = backlog.load_backlog(profile.paths, profile)
        claimed_id = fresh["id"]
        cli_tests.save_state(
            profile.paths.state_file,
            {
                "schema_version": 1,
                "approval_tasks": {},
                "task_claims": {
                    claimed_id: self._task_claim_entry(
                        snapshot.tasks[claimed_id],
                        status="claimed",
                        actor="codex",
                        when=datetime.now().astimezone().isoformat(timespec="seconds"),
                    )
                },
            },
        )
        with self.assertRaises(backlog.BacklogError):
            backlog.remove_task(profile, task_id=claimed_id)

        cli_tests.save_state(
            profile.paths.state_file,
            {
                "schema_version": 1,
                "approval_tasks": {},
                "task_claims": {
                    claimed_id: self._task_claim_entry(
                        snapshot.tasks[claimed_id],
                        status="done",
                        actor="codex",
                        when=datetime.now().astimezone().isoformat(timespec="seconds"),
                    )
                },
            },
        )
        with self.assertRaises(backlog.BacklogError):
            backlog.remove_task(profile, task_id=claimed_id)
