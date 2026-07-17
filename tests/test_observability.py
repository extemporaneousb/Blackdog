from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import time
from unittest.mock import patch

import blackdog.observability as observability
from blackdog.lifecycle import (
    MUTATION_PHASES,
    OPERATION_FAILURE_CODES,
    NextAction,
    OperationResult,
)
from blackdog.observability import (
    ALLOWED_LABEL_VALUES,
    MAX_FAILURE_EVIDENCE,
    MAX_PROCESS_PROJECTS,
    MAX_ROW_BYTES,
    lifecycle_observation_path,
    observe_lifecycle,
    observe_lifecycle_for_project,
    observe_operation_result,
    read_lifecycle_observability,
)
from blackdog.prompting import preview_prompt
from blackdog.stats import build_stats, render_stats_text
from blackdog_core.state import (
    ATTEMPT_STATUS_ABANDONED,
    ATTEMPT_STATUS_BLOCKED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_SUCCESS,
    FAILURE_CLASS_NO_CHANGES,
)
from blackdog_cli.main import _observe_failed_product_cli, _observe_repo_cli_result
from tests.core_audit_support import CoreAuditTestCase


class LifecycleObservabilityTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile("Observability Demo")
        self.profile = self.load_test_profile()
        observability._reset_process_health_for_tests()

    def tearDown(self) -> None:
        observability._reset_process_health_for_tests()
        super().tearDown()

    def test_stable_ids_dedupe_and_never_serialize_prose_or_unbounded_labels(self) -> None:
        secret = "SECRET_SENTINEL_prompt_request_summary_note_error_path_command"
        labels = {
            "prompt_role": "request",
            "prompt_mode": "raw",
            "summary": secret,
            "note": secret,
            "error": secret,
            "path": secret,
            "command": secret,
        }
        with patch("blackdog.observability.now_iso", side_effect=["2026-07-16T12:00:00+00:00", "later"]):
            first = observe_lifecycle(
                self.profile,
                surface="prompt.preview",
                operation_key=secret,
                labels=labels,
            )
            second = observe_lifecycle(
                self.profile,
                surface="prompt.preview",
                operation_key=secret,
                labels=labels,
            )

        self.assertEqual(first.status, "written")
        self.assertEqual(second.status, "deduped")
        self.assertEqual(first.observation_id, second.observation_id)
        path = lifecycle_observation_path(self.profile)
        lines = path.read_bytes().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertLessEqual(len(lines[0]), MAX_ROW_BYTES)
        self.assertNotIn(secret.encode(), lines[0])
        row = json.loads(lines[0])
        self.assertEqual(row["labels"], {"prompt_mode": "raw", "prompt_role": "request"})
        self.assertEqual(row["unknown_label_count"], 5)
        report = read_lifecycle_observability(self.profile)
        self.assertEqual(report.unknown_labels, 5)
        self.assertEqual(report.stream_health, "degraded")

    def test_project_convenience_boundary_writes_without_affecting_its_caller(self) -> None:
        result = observe_lifecycle_for_project(
            self.root,
            surface="repo.refresh",
            operation_key="refresh|completed",
            labels={"operation_phase": "completed", "result": "completed"},
        )

        self.assertEqual(result.status, "written")
        report = read_lifecycle_observability(self.profile)
        self.assertEqual(report.surface_counts, {"repo.refresh": 1})

    def test_operation_result_adapter_is_private_stable_and_semantically_sensitive(self) -> None:
        secret = "SECRET_SENTINEL_result_prose_path_command"
        base = {
            "operation": "task.land",
            "operation_status": "blocked",
            "workset_id": "workset-1",
            "task_id": "TASK-1",
            "attempt_id": "attempt-1",
            "action_id": "land-active-task",
            "task_status": "blocked",
            "attempt_status": "failed",
            "status": "failed",
            "mutation_started": True,
            "mutation_completed": False,
            "mutation_phase": "git_prepared",
            "failure_code": "missing_worktree",
            "retryability": "retryable",
            "next_action": {
                "action_id": "repair-task",
                "kind": "command",
                "display": secret,
                "command": secret,
                "argv": [secret],
            },
            "prompt": secret,
            "request": secret,
            "summary": secret,
            "note": secret,
            "error": secret,
            "reason": secret,
            "reason_detail": secret,
            "display": secret,
            "project_root": secret,
            "path": secret,
            "worktree_path": secret,
            "branch": secret,
            "target_branch": secret,
            "command": secret,
            "argv": [secret],
            "changed_paths": [secret],
            "residuals": [secret],
            "followup_candidates": [secret],
        }

        self.assertIs(observe_operation_result(self.profile, base), base)
        same_semantics = {
            **base,
            "summary": f"{secret}-changed",
            "path": f"{secret}-changed",
            "command": f"{secret}-changed",
        }
        self.assertIs(observe_operation_result(self.profile, same_semantics), same_semantics)
        changed_action = {**base, "action_id": "close-active-task"}
        self.assertIs(observe_operation_result(self.profile, changed_action), changed_action)
        changed_outcome = {
            **base,
            "operation_status": "succeeded",
            "mutation_completed": True,
            "mutation_phase": "runtime_finalized",
            "failure_code": None,
            "retryability": "terminal",
        }
        self.assertIs(observe_operation_result(self.profile, changed_outcome), changed_outcome)

        raw = lifecycle_observation_path(self.profile).read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)
        rows = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row["observation_id"] for row in rows}), 3)
        report = read_lifecycle_observability(self.profile)
        self.assertEqual(report.outcome_counts, {"partial": 2, "success": 1})
        self.assertEqual(report.label_counts["mutation_phase"], {"pre_git": 2, "runtime": 1})
        self.assertEqual(
            report.label_counts["failure_class"],
            {"missing_worktree": 2},
        )

    def test_closed_task_land_operation_result_is_non_success_while_task_close_succeeds(self) -> None:
        blocked_action = NextAction.terminal(
            action_id="closed_land_requires_new_attempt",
            kind="blocked",
            disposition="blocked",
            reason_code="no_changes",
            reason_detail="The no-change landing closed without landing work.",
            display="Start or close the next task action",
        )
        land_result = OperationResult(
            operation="task.land",
            operation_status="closed",
            task_status="blocked",
            attempt_status=ATTEMPT_STATUS_BLOCKED,
            disposition=blocked_action.disposition,
            mutation_started=True,
            mutation_completed=True,
            mutation_phase="runtime_finalized",
            failure_code=FAILURE_CLASS_NO_CHANGES,
            next_action=blocked_action,
            legacy_payload={
                "workset_id": "workset-1",
                "task_id": "TASK-1",
                "attempt_id": "attempt-1",
                "land_failure_disposition": "closed",
            },
        )
        self.assertIs(observe_operation_result(self.profile, land_result), land_result)

        complete_action = NextAction.terminal(
            action_id="task_close_complete",
            kind="complete",
            disposition="complete",
            reason_code="task_closed",
            reason_detail="The requested task close completed.",
            display="Task close complete",
        )
        close_result = OperationResult(
            operation="task.close",
            operation_status="succeeded",
            task_status="blocked",
            attempt_status=ATTEMPT_STATUS_FAILED,
            disposition=complete_action.disposition,
            mutation_started=True,
            mutation_completed=True,
            mutation_phase="runtime_finalized",
            failure_code=None,
            next_action=complete_action,
            legacy_payload={"workset_id": "workset-1", "task_id": "TASK-1"},
        )
        self.assertIs(observe_operation_result(self.profile, close_result), close_result)

        report = read_lifecycle_observability(self.profile)
        self.assertEqual(report.outcome_counts, {"blocked": 1, "success": 1})
        self.assertEqual(
            report.surface_counts,
            {"task.close": 1, "task.land": 1},
        )

    def test_closed_operation_outcome_truth_table(self) -> None:
        cases = (
            ({"operation_status": "closed"}, "blocked"),
            (
                {
                    "operation_status": "closed",
                    "attempt_status": ATTEMPT_STATUS_BLOCKED,
                },
                "blocked",
            ),
            (
                {
                    "operation_status": "closed",
                    "attempt_status": ATTEMPT_STATUS_FAILED,
                },
                "failed",
            ),
            (
                {
                    "operation_status": "closed",
                    "attempt_status": ATTEMPT_STATUS_ABANDONED,
                },
                "abandoned",
            ),
            (
                {
                    "operation_status": "closed",
                    "attempt_status": ATTEMPT_STATUS_SUCCESS,
                },
                "blocked",
            ),
            (
                {
                    "status": "closed",
                    "attempt_status": ATTEMPT_STATUS_FAILED,
                },
                "failed",
            ),
            (
                {
                    "operation_status": "succeeded",
                    "attempt_status": ATTEMPT_STATUS_FAILED,
                },
                "success",
            ),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(observability._operation_outcome(payload), expected)

    def test_operation_result_adapter_is_fail_open_for_io_capacity_and_contention(self) -> None:
        def payload(action_id: str) -> dict[str, object]:
            return {
                "operation": "task.show",
                "operation_status": "observed",
                "workset_id": "workset-1",
                "task_id": "TASK-1",
                "attempt_id": "attempt-1",
                "action_id": action_id,
                "task_status": "in_progress",
                "attempt_status": "in_progress",
                "mutation_started": False,
                "mutation_completed": False,
                "mutation_phase": "none",
            }

        io_result = payload("io")
        with patch("blackdog.observability._append_observation_row", side_effect=OSError("secret")):
            self.assertIs(observe_operation_result(self.profile, io_result), io_result)

        capacity_result = payload("capacity")
        with patch("blackdog.observability.MAX_ARTIFACT_BYTES", 0):
            self.assertIs(
                observe_operation_result(self.profile, capacity_result),
                capacity_result,
            )

        contention_result = payload("contention")
        path = lifecycle_observation_path(self.profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        with observability._nonblocking_observation_lock(path):
            self.assertIs(
                observe_operation_result(self.profile, contention_result),
                contention_result,
            )

        report = read_lifecycle_observability(self.profile)
        self.assertEqual(report.write_failures, 3)

    def test_nested_observation_failures_never_escape_product_boundaries(self) -> None:
        with (
            patch(
                "blackdog.observability._append_observation_row",
                side_effect=OSError("primary write failure"),
            ),
            patch(
                "blackdog.observability._record_write_failure",
                side_effect=OSError("secondary health failure"),
            ),
        ):
            preview = preview_prompt(self.profile, request="nested prompt failure")
            self.assertEqual(len(preview.prompt_hash), 64)

        with (
            patch(
                "blackdog.observability._observe_lifecycle",
                side_effect=OSError("outer observation failure"),
            ),
            patch(
                "blackdog.observability._record_write_failure",
                side_effect=OSError("outer health failure"),
            ),
        ):
            failed = observe_lifecycle(
                self.profile,
                surface="task.show",
                operation_key="outer-failure",
            )
            self.assertEqual(failed.status, "failed")

        with (
            patch(
                "blackdog.observability.load_profile",
                side_effect=OSError("missing profile"),
            ),
            patch(
                "blackdog.observability._record_process_count",
                side_effect=OSError("missing-target health failure"),
            ),
        ):
            missing = observe_lifecycle_for_project(
                self.root,
                surface="repo.refresh",
                operation_key="missing-profile",
            )
            self.assertEqual(missing.status, "missing")

        written = observe_lifecycle(
            self.profile,
            surface="task.show",
            operation_key="valid-before-process-health-read-failure",
        )
        self.assertEqual(written.status, "written")
        with patch(
            "blackdog.observability._process_health_for_project",
            side_effect=OSError("stats health failure"),
        ):
            stats = build_stats(project_roots=(self.root,))
            self.assertEqual(stats.lifecycle_observability["repos_considered"], 1)
            self.assertGreaterEqual(stats.lifecycle_observability["read_failures"], 1)
            self.assertEqual(stats.lifecycle_observability["stream_health"], "degraded")
            stats_text = render_stats_text(stats)
            self.assertIn("stream_health=degraded", stats_text)
            self.assertIn("read_failures=1", stats_text)

    def test_mutation_phase_mapping_is_exhaustive_for_owner_contract(self) -> None:
        self.assertEqual(set(observability._MUTATION_PHASE_LABELS), set(MUTATION_PHASES))
        self.assertTrue(
            set(observability._MUTATION_PHASE_LABELS.values()).issubset(
                ALLOWED_LABEL_VALUES["mutation_phase"]
            )
        )
        self.assertEqual(
            observability._MUTATION_PHASE_LABELS[
                "worktree_removed_branch_cleanup_pending"
            ],
            "post_git",
        )

    def test_operation_result_adapter_distinguishes_dry_run_applied_and_noop(self) -> None:
        def payload(
            action_id: str,
            *,
            apply: bool,
            mutation_started: bool,
            mutation_completed: bool,
        ) -> dict[str, object]:
            return {
                "operation": "task.reconcile-landing",
                "operation_status": "observed" if not apply else "succeeded",
                "workset_id": "workset-1",
                "task_id": "TASK-1",
                "attempt_id": "attempt-1",
                "action_id": action_id,
                "apply": apply,
                "mutation_started": mutation_started,
                "mutation_completed": mutation_completed,
                "mutation_phase": "runtime_finalized" if mutation_started else "none",
            }

        observe_operation_result(
            self.profile,
            payload(
                "preview-reconciliation",
                apply=False,
                mutation_started=False,
                mutation_completed=False,
            ),
        )
        observe_operation_result(
            self.profile,
            payload(
                "apply-reconciliation",
                apply=True,
                mutation_started=True,
                mutation_completed=True,
            ),
        )
        observe_operation_result(
            self.profile,
            payload(
                "noop-reconciliation",
                apply=True,
                mutation_started=False,
                mutation_completed=False,
            ),
        )
        contradictory_applied = payload(
            "contradictory-applied",
            apply=False,
            mutation_started=True,
            mutation_completed=True,
        )
        contradictory_applied["dry_run"] = True
        contradictory_applied["result"] = "dry_run"
        observe_operation_result(self.profile, contradictory_applied)
        contradictory_partial = payload(
            "contradictory-partial",
            apply=False,
            mutation_started=True,
            mutation_completed=False,
        )
        contradictory_partial["dry_run"] = True
        contradictory_partial["result"] = "dry_run"
        observe_operation_result(self.profile, contradictory_partial)
        inconsistent_booleans = payload(
            "inconsistent-booleans",
            apply=False,
            mutation_started=False,
            mutation_completed=True,
        )
        inconsistent_booleans["dry_run"] = True
        observe_operation_result(self.profile, inconsistent_booleans)

        report = read_lifecycle_observability(self.profile)
        self.assertEqual(
            report.label_counts["result"],
            {"applied": 2, "dry_run": 1, "noop": 1, "unknown": 2},
        )
        self.assertEqual(report.outcome_counts["partial"], 1)
        self.assertEqual(report.surface_counts, {"task.reconcile-landing": 6})
        stats_text = render_stats_text(build_stats(project_roots=(self.root,)))
        self.assertIn("partial=1", stats_text)
        for outcome in ("success", "failed", "blocked", "abandoned", "unknown"):
            self.assertIn(f"{outcome}=", stats_text)

    def test_cli_failure_observation_uses_only_bounded_taxonomy(self) -> None:
        secret = "SECRET_SENTINEL_failure_exception"
        args = argparse.Namespace(
            command="prompt",
            prompt_command="tune",
            project_root=str(self.root),
        )

        _observe_failed_product_cli(args, ValueError(secret))

        report = read_lifecycle_observability(self.profile)
        self.assertEqual(report.outcome_counts, {"failed": 1})
        self.assertEqual(report.reason_counts, {"validation": 1})
        self.assertEqual(report.label_counts["operation_phase"], {"completed": 1})
        self.assertNotIn(secret, lifecycle_observation_path(self.profile).read_text(encoding="utf-8"))

        with patch(
            "blackdog_cli.main.observe_lifecycle_for_project",
            side_effect=RuntimeError(secret),
        ):
            _observe_repo_cli_result(
                self.root,
                surface="repo.refresh",
                action="refresh",
            )

    def test_stats_reports_preexisting_stream_health_before_stamping_its_own_read(self) -> None:
        first = build_stats(project_roots=(self.root,))

        self.assertEqual(first.lifecycle_observability["stream_health"], "missing")
        self.assertEqual(first.lifecycle_observability["observations"], 0)
        self.assertNotIn("coverage", first.lifecycle_observability)
        self.assertTrue(lifecycle_observation_path(self.profile).is_file())

        second = build_stats(project_roots=(self.root,))

        self.assertEqual(second.lifecycle_observability["stream_health"], "healthy")
        self.assertEqual(second.lifecycle_observability["observations"], 1)
        self.assertEqual(second.lifecycle_observability["surface_counts"], {"stats.read": 1})
        self.assertNotIn("coverage", second.lifecycle_observability)

    def test_every_write_boundary_failure_leaves_prompt_operation_successful(self) -> None:
        failures = (
            "_ensure_observation_parent",
            "_observation_lock",
            "_open_observation_file",
            "_serialize_observation",
            "_write_text",
            "_fsync",
        )
        for helper in failures:
            with self.subTest(helper=helper), patch(
                f"blackdog.observability.{helper}",
                side_effect=OSError(f"SECRET_SENTINEL_{helper}"),
            ):
                preview = preview_prompt(self.profile, request=f"safe request {helper}")
                self.assertEqual(len(preview.prompt_hash), 64)

        report = read_lifecycle_observability(self.profile)
        self.assertEqual(report.write_failures, len(failures))
        self.assertLessEqual(len(report.write_failure_evidence), MAX_FAILURE_EVIDENCE)
        serialized_evidence = json.dumps(report.to_dict(), sort_keys=True)
        self.assertNotIn("SECRET_SENTINEL", serialized_evidence)

    def test_lock_contention_returns_immediately_without_affecting_prompt(self) -> None:
        path = lifecycle_observation_path(self.profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        with observability._nonblocking_observation_lock(path):
            started = time.monotonic()
            preview = preview_prompt(self.profile, request="contention request")
            elapsed = time.monotonic() - started

        self.assertEqual(len(preview.prompt_hash), 64)
        self.assertLess(elapsed, 0.5)
        report = read_lifecycle_observability(self.profile)
        self.assertEqual(report.write_failures, 1)
        self.assertEqual(report.write_failure_evidence[0]["reason"], "contention")

    def test_concurrent_writers_leave_only_valid_bounded_jsonl(self) -> None:
        def write(index: int):
            return observe_lifecycle(
                self.profile,
                surface="repo.table",
                operation_key=f"operation-{index}",
                labels={"operation_phase": "completed"},
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(pool.map(write, range(40)))

        self.assertEqual(len(results), 40)
        path = lifecycle_observation_path(self.profile)
        for raw_line in path.read_bytes().splitlines():
            self.assertLessEqual(len(raw_line), MAX_ROW_BYTES)
            self.assertIsInstance(json.loads(raw_line), dict)
        report = read_lifecycle_observability(self.profile)
        self.assertEqual(report.malformed_rows, 0)
        self.assertGreater(report.observations, 0)
        self.assertEqual(report.observations + report.write_failures, 40)

    def test_reader_bounds_malformed_duplicate_old_future_unknown_and_oversized_rows(self) -> None:
        written = observe_lifecycle(
            self.profile,
            surface="repo.table",
            operation_key="reader-fixture",
            labels={"operation_phase": "completed"},
        )
        self.assertEqual(written.status, "written")
        path = lifecycle_observation_path(self.profile)
        valid_line = path.read_text(encoding="utf-8").strip()
        valid = json.loads(valid_line)
        old = {**valid, "schema_version": 0, "observation_id": hashlib.sha256(b"old").hexdigest()}
        future = {**valid, "schema_version": 2, "observation_id": hashlib.sha256(b"future").hexdigest()}
        unknown = {
            **valid,
            "surface": "SECRET_SENTINEL_unknown_surface",
            "outcome": "SECRET_SENTINEL_unknown_outcome",
            "observation_id": hashlib.sha256(b"unknown").hexdigest(),
        }
        extra_field = {
            **valid,
            "summary": "SECRET_SENTINEL_extra_field",
            "observation_id": hashlib.sha256(b"extra").hexdigest(),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(valid_line + "\n")
            handle.write("not-json\n")
            handle.write(json.dumps(old) + "\n")
            handle.write(json.dumps(future) + "\n")
            handle.write(json.dumps(unknown) + "\n")
            handle.write(json.dumps(extra_field) + "\n")
            handle.write(json.dumps({"oversized": "x" * (MAX_ROW_BYTES + 1)}) + "\n")

        report = read_lifecycle_observability(self.profile)
        self.assertEqual(report.observations, 1)
        self.assertEqual(report.duplicate_rows, 1)
        self.assertEqual(report.malformed_rows, 2)
        self.assertEqual(report.unknown_schema_rows, 2)
        self.assertEqual(report.unknown_surface_rows, 1)
        self.assertEqual(report.unknown_outcome_rows, 1)
        self.assertEqual(report.oversized_rows, 1)
        self.assertEqual(report.stream_health, "degraded")
        self.assertNotIn(
            "SECRET_SENTINEL_extra_field",
            json.dumps(report.to_dict(), sort_keys=True),
        )

        with patch("blackdog.observability.MAX_ARTIFACT_BYTES", 64):
            truncated = read_lifecycle_observability(self.profile)
        self.assertEqual(truncated.truncated_artifact, 1)
        self.assertEqual(truncated.stream_health, "degraded")

    def test_missing_permission_capacity_and_malformed_stream_keep_stats_available(self) -> None:
        path = lifecycle_observation_path(self.profile)
        self.assertFalse(path.exists())
        missing = read_lifecycle_observability(self.profile)
        self.assertEqual(missing.artifact_missing, 1)
        self.assertEqual(missing.stream_health, "missing")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("malformed\n", encoding="utf-8")
        result = build_stats(project_roots=(self.root,))
        self.assertEqual(result.lifecycle_observability["malformed_rows"], 1)
        self.assertEqual(result.lifecycle_observability["stream_health"], "degraded")

        with patch("blackdog.observability._open_observation_file", side_effect=PermissionError("secret path")):
            result = build_stats(project_roots=(self.root,))
        self.assertGreaterEqual(result.lifecycle_observability["read_failures"], 1)
        self.assertGreaterEqual(read_lifecycle_observability(self.profile).write_failures, 1)

        original = path.read_bytes()
        with patch("blackdog.observability.MAX_ARTIFACT_BYTES", len(original)):
            capacity = observe_lifecycle(self.profile, surface="stats.read", operation_key="capacity")
        self.assertEqual(capacity.status, "capacity")
        self.assertEqual(path.read_bytes(), original)

    def test_duplicate_only_stream_degrades_stats_health(self) -> None:
        observe_lifecycle(
            self.profile,
            surface="repo.table",
            operation_key="duplicate-only",
            labels={"operation_phase": "completed"},
        )
        path = lifecycle_observation_path(self.profile)
        line = path.read_text(encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)

        report = read_lifecycle_observability(self.profile)
        self.assertEqual(report.duplicate_rows, 1)
        self.assertEqual(report.malformed_rows, 0)
        self.assertEqual(report.stream_health, "degraded")
        stats = build_stats(project_roots=(self.root,))
        self.assertEqual(stats.lifecycle_observability["duplicate_rows"], 1)
        self.assertEqual(stats.lifecycle_observability["stream_health"], "degraded")

    def test_capacity_pressure_survives_process_health_reset_and_degrades_stats(self) -> None:
        written = observe_lifecycle(
            self.profile,
            surface="repo.table",
            operation_key="capacity-fixture",
            labels={"operation_phase": "completed"},
        )
        self.assertEqual(written.status, "written")
        path = lifecycle_observation_path(self.profile)
        persisted_size = path.stat().st_size

        with patch("blackdog.observability.MAX_ARTIFACT_BYTES", persisted_size):
            refused = observe_lifecycle(
                self.profile,
                surface="stats.read",
                operation_key="refused-at-capacity",
            )
            self.assertEqual(refused.status, "capacity")
            observability._reset_process_health_for_tests()

            report = read_lifecycle_observability(self.profile)
            self.assertEqual(report.write_failures, 0)
            self.assertEqual(report.capacity_pressure, 1)
            self.assertEqual(report.truncated_artifact, 0)
            self.assertEqual(report.stream_health, "degraded")

            stats = build_stats(project_roots=(self.root,))
            self.assertEqual(stats.lifecycle_observability["capacity_pressure"], 1)
            self.assertEqual(stats.lifecycle_observability["stream_health"], "degraded")
            self.assertIn("capacity_pressure=1", render_stats_text(stats))

    def test_failure_evidence_and_taxonomy_are_bounded_and_contract_owned(self) -> None:
        self.assertEqual(ALLOWED_LABEL_VALUES["failure_class"], OPERATION_FAILURE_CODES)
        with patch("blackdog.observability._append_observation_row", side_effect=OSError("SECRET")):
            for index in range(MAX_FAILURE_EVIDENCE + 10):
                observe_lifecycle(
                    self.profile,
                    surface="task.land",
                    operation_key=f"failure-{index}",
                    labels={"failure_class": "missing_worktree"},
                )
        report = read_lifecycle_observability(self.profile)
        self.assertEqual(report.write_failures, MAX_FAILURE_EVIDENCE + 10)
        self.assertEqual(len(report.write_failure_evidence), MAX_FAILURE_EVIDENCE)
        self.assertNotIn("SECRET", json.dumps(report.to_dict(), sort_keys=True))

    def test_process_local_health_is_bounded_by_recent_project_identity(self) -> None:
        for index in range(MAX_PROCESS_PROJECTS + 25):
            project_id = hashlib.sha256(f"project-{index}".encode()).hexdigest()
            observability._record_process_count(project_id, "write_missing_targets")

        self.assertEqual(len(observability._PROCESS_COUNTS), MAX_PROCESS_PROJECTS)

    def test_process_health_eviction_removes_evidence_and_defends_against_orphans(self) -> None:
        observe_lifecycle(
            self.profile,
            surface="task.show",
            operation_key="healthy-artifact",
        )
        project_id = observability._bounded_hash(str(self.profile.paths.project_root))
        observability._record_write_failure(
            project_id,
            "task.show",
            hashlib.sha256(b"evicted-failure").hexdigest(),
            "io",
        )
        for index in range(MAX_PROCESS_PROJECTS):
            other_project_id = hashlib.sha256(f"other-project-{index}".encode()).hexdigest()
            observability._record_process_count(
                other_project_id,
                "write_missing_targets",
            )

        self.assertNotIn(project_id, observability._PROCESS_COUNTS)
        counts, evidence = observability._process_health_for_project(project_id)
        self.assertEqual(counts["write_failures"], 0)
        self.assertEqual(evidence, ())
        self.assertEqual(read_lifecycle_observability(self.profile).stream_health, "healthy")

        with observability._PROCESS_LOCK:
            observability._PROCESS_FAILURE_EVIDENCE.append(
                {
                    "project_id": project_id,
                    "surface": "task.show",
                    "observation_id": hashlib.sha256(b"orphaned-failure").hexdigest(),
                    "reason": "io",
                }
            )
        orphaned = read_lifecycle_observability(self.profile)
        self.assertEqual(orphaned.write_failures, 1)
        self.assertEqual(len(orphaned.write_failure_evidence), 1)
        self.assertEqual(orphaned.stream_health, "degraded")
