from __future__ import annotations

import json
import subprocess
import sys

from tests import test_blackdog_cli as cli_tests
from tests.core_audit_support import CoreAuditTestCase


class CoreBacklogAuditTests(CoreAuditTestCase):
    def test_core_audit_backlog_task_shaping_normalizes_and_rejects_invalid_values(self) -> None:
        shaped = cli_tests.backlog_module._coerce_task_shaping(
            {
                "estimated_elapsed_minutes": "30",
                "estimated_touched_paths": [" docs/FILE_FORMATS.md ", "", "tests/test_core_backlog.py", "docs/FILE_FORMATS.md"],
                "estimated_worktrees": "2",
                "parallelizable_groups": 1,
            },
            fallback_paths=["pyproject.toml"],
        )
        self.assertEqual(shaped["estimated_elapsed_minutes"], 30)
        self.assertEqual(
            shaped["estimated_touched_paths"],
            ["docs/FILE_FORMATS.md", "tests/test_core_backlog.py"],
        )
        self.assertEqual(shaped["estimated_worktrees"], 2)
        self.assertEqual(shaped["parallelizable_groups"], 1)

        with self.assertRaises(cli_tests.backlog_module.BacklogError):
            cli_tests.backlog_module._coerce_task_shaping(
                {"estimated_validation_minutes": -5},
                fallback_paths=["pyproject.toml"],
            )

    def test_core_audit_backlog_reconcile_prunes_orphans_and_promotes_done_approval(self) -> None:
        cli_tests.run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        profile = cli_tests.load_profile(self.root)
        cli_tests.backlog_module.add_task(
            profile,
            title="Reconcile canonical state",
            bucket="core",
            priority="P1",
            risk="medium",
            effort="M",
            why="Core state should reconcile approval and claim semantics from one pass.",
            evidence="The previous sync logic only seeded approval rows and left stale runtime entries behind.",
            safe_first_slice="Prune orphans and promote completed approval rows during backlog sync.",
            paths=["src/blackdog_core/state.py"],
            checks=[],
            docs=["docs/FILE_FORMATS.md"],
            domains=["state"],
            packages=[],
            affected_paths=["src/blackdog_core/state.py"],
            task_shaping=None,
            objective="Core hardening",
            requires_approval=True,
            approval_reason="This task changes durable runtime semantics.",
            epic_id="core-hardening",
            epic_title="Core hardening",
            lane_id="hardening-audit",
            lane_title="Hardening audit",
            wave=0,
        )
        snapshot = cli_tests.load_backlog(profile.paths, profile)
        task_id = next(iter(snapshot.tasks))
        reconciled, report = cli_tests.backlog_module.reconcile_state_for_backlog(
            {
                "schema_version": 1,
                "approval_tasks": {
                    task_id: {"status": "pending"},
                    "BLACK-orphan": {"status": "pending"},
                },
                "task_claims": {
                    task_id: {
                        "status": "done",
                        "claimed_by": "codex",
                        "claimed_pid": 1234,
                        "claimed_process_missing_scans": 4,
                    },
                    "BLACK-orphan": {"status": "claimed", "claimed_by": "stale-agent"},
                },
            },
            snapshot,
        )
        self.assertTrue(report["state_reconciled"])
        self.assertEqual(report["pruned_approval_rows"], 1)
        self.assertEqual(report["pruned_claim_rows"], 1)
        self.assertEqual(report["promoted_done_approvals"], 1)
        self.assertEqual(report["updated_claim_rows"], 1)
        self.assertEqual(report["claim_runtime_fields_dropped"], 1)
        self.assertNotIn("BLACK-orphan", reconciled["approval_tasks"])
        self.assertNotIn("BLACK-orphan", reconciled["task_claims"])
        self.assertEqual(reconciled["approval_tasks"][task_id]["status"], "done")
        self.assertEqual(reconciled["approval_tasks"][task_id]["title"], snapshot.tasks[task_id].title)
        self.assertEqual(
            reconciled["approval_tasks"][task_id]["approval_reason"],
            "This task changes durable runtime semantics.",
        )
        self.assertNotIn("claimed_pid", reconciled["task_claims"][task_id])
        self.assertNotIn("claimed_process_missing_scans", reconciled["task_claims"][task_id])

    def test_core_audit_validate_reports_reconcile_and_strict_sections(self) -> None:
        cli_tests.run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        cli_tests.run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Validate strict runtime",
            "--bucket",
            "core",
            "--why",
            "Validate should report the canonical reconcile pass and strict artifact checks.",
            "--evidence",
            "Core validation previously only returned a small counter payload.",
            "--safe-first-slice",
            "Record one canonical result and surface the strict validation counts.",
            "--path",
            "src/blackdog_core/backlog.py",
            "--requires-approval",
            "--approval-reason",
            "This task changes durable runtime semantics.",
            "--wave",
            "0",
        )
        profile = cli_tests.load_profile(self.root)
        snapshot = cli_tests.load_backlog(profile.paths, profile)
        task_id = next(iter(snapshot.tasks))
        cli_tests.record_task_result(
            self.runtime_paths(),
            task_id=task_id,
            actor="codex",
            status="success",
            what_changed=["Recorded one canonical result row."],
            validation=["unit"],
            residual=[],
            needs_user_input=False,
            followup_candidates=[],
            run_id="core-validate",
        )
        payload = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog_cli.main", "validate", "--project-root", str(self.root)],
                check=True,
                capture_output=True,
                text=True,
                env=cli_tests.cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(payload["tasks"], 1)
        self.assertEqual(payload["results"], 1)
        self.assertIn("reconcile", payload)
        self.assertIn("strict_validation", payload)
        self.assertEqual(payload["strict_validation"]["issue_count"], 0)
        self.assertEqual(payload["strict_validation"]["task_result_events"], 1)
        self.assertGreaterEqual(payload["reconcile"]["seeded_approval_rows"], 1)

    def test_core_audit_validate_rejects_result_without_task_result_event(self) -> None:
        cli_tests.run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        cli_tests.run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Validate missing result event",
            "--bucket",
            "core",
            "--why",
            "Strict validate should fail when result evidence loses its matching task_result event.",
            "--evidence",
            "The append-only result contract requires result files and task_result events to stay paired.",
            "--safe-first-slice",
            "Write a valid result file without the matching event and run validate.",
            "--path",
            "src/blackdog_core/state.py",
            "--wave",
            "0",
        )
        profile = cli_tests.load_profile(self.root)
        snapshot = cli_tests.load_backlog(profile.paths, profile)
        task_id = next(iter(snapshot.tasks))
        result_dir = self.runtime_paths().results_dir / task_id
        result_dir.mkdir(parents=True, exist_ok=True)
        (result_dir / "20260408-120000-core-validate.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "task_id": task_id,
                    "recorded_at": "2026-04-08T12:00:00-07:00",
                    "actor": "codex",
                    "run_id": "core-validate",
                    "status": "success",
                    "what_changed": ["Recorded evidence without the matching task_result event."],
                    "validation": ["unit"],
                    "residual": [],
                    "needs_user_input": False,
                    "followup_candidates": [],
                    "metadata": {},
                    "task_shaping_telemetry": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [sys.executable, "-m", "blackdog_cli.main", "validate", "--project-root", str(self.root)],
            check=False,
            capture_output=True,
            text=True,
            env=cli_tests.cli_env(),
            cwd=self.root,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("result_missing_task_result_event", completed.stderr)
