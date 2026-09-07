from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import patch

from blackdog.evidence import (
    locked_evidence,
    observe_binding,
    read_document,
    record_outcome,
    source_tree,
    validate_task,
)
from blackdog.outcome_reporting import distribution, outcome_report
from blackdog_core.evidence import (
    ASSESSMENT,
    DEFINITION,
    INTERVENTION,
    VALIDATION_INTENT,
    VALIDATION_RESULT,
    Assessment,
    EvidenceError,
    OutcomeDefinition,
    digest,
    event_identity,
)
from blackdog_core.profile import load_profile
from blackdog_core.state import append_event_once, load_events, load_runtime_state
from blackdog_core.tasks import create_task, finish_task, start_task


class EvidenceTests(TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.workspace = Path(self.temp.name) / "task worktree"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.root / "blackdog.toml").write_text("""[project]
name = "Evidence test"
profile_version = 1
[paths]
control_dir = "@git-common/blackdog"
worktrees_dir = "../worktrees"
[taxonomy]
validation_commands = ["exit 0"]
doc_routing_defaults = []
""")
        (self.root / ".gitignore").write_text("counter\n")
        (self.root / "code.py").write_text("value = 1\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "initial")
        self.git("worktree", "add", "-qb", "task-branch", str(self.workspace))
        self.profile = load_profile(self.root)
        self.task = create_task(self.profile, title="Deliver a tested change")
        self.attempt = start_task(
            self.profile,
            task_id=self.task.task_id,
            actor="codex",
            worktree_path=str(self.workspace),
            workspace_mode="git-worktree",
            worktree_role="task",
            branch="task-branch",
            target_branch=self.git("branch", "--show-current"),
            start_commit=self.git("rev-parse", "HEAD"),
        )
        self.definition = {
            "schema_version": 1,
            "task_class": "bugfix",
            "objective": "Deliver corrected output",
            "criteria": [
                {
                    "id": "correct-output",
                    "description": "Output satisfies fixture",
                    "required": True,
                }
            ],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def git(self, *args: str, workspace: bool = False) -> str:
        return subprocess.run(
            ["git", "-C", str(self.workspace if workspace else self.root), *args],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    def record(self, kind: str, document: dict, **kwargs):
        return record_outcome(
            self.profile,
            task_id=self.task.task_id,
            attempt_id=self.attempt.attempt_id,
            actor="codex",
            kind=kind,
            document=document,
            **kwargs,
        )

    def validate(self, run_id="run-1"):
        return validate_task(
            self.profile,
            task_id=self.task.task_id,
            attempt_id=self.attempt.attempt_id,
            actor="codex",
            run_id=run_id,
        )

    def assessment(
        self, *, assessment_id="review-1", result="met", refs=(), supersedes=None
    ):
        return {
            "schema_version": 1,
            "assessment_id": assessment_id,
            "definition_sha256": digest(self.definition),
            "criterion_id": "correct-output",
            "result": result,
            "evaluator": "reviewer",
            "evaluator_kind": "human",
            "provenance": "reviewer_asserted",
            "evidence_refs": list(refs),
            "supersedes": supersedes,
        }

    def finish(self):
        self.git("add", "-A", workspace=True)
        if self.git("status", "--porcelain", workspace=True):
            self.git("commit", "-qm", "implemented", workspace=True)
        commit = self.git("rev-parse", "HEAD", workspace=True)
        finish_task(
            self.profile,
            task_id=self.task.task_id,
            attempt_id=self.attempt.attempt_id,
            actor="codex",
            status="success",
            summary="Implemented",
            commit=commit,
            landed_commit=commit,
        )

    def test_definition_is_immutable_and_exact_retry_is_noop(self):
        before = self.profile.paths.runtime_file.read_bytes()
        first = self.record(DEFINITION, self.definition)
        events = self.profile.paths.events_file.read_bytes()
        self.assertEqual(
            self.record(DEFINITION, self.definition)["event_id"], first["event_id"]
        )
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events)
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), before)
        with self.assertRaises(EvidenceError):
            self.record(
                DEFINITION, {**self.definition, "objective": "Changed criteria"}
            )

    def test_schema_rejects_vacuous_coercion_and_machine_provenance(self):
        for criteria in (
            [],
            [{"id": "x", "description": "x", "required": False}],
            [{"id": "x", "description": "x", "required": 1}],
        ):
            with self.subTest(criteria=criteria), self.assertRaises(EvidenceError):
                OutcomeDefinition.parse({**self.definition, "criteria": criteria})
        for bad in (True, 2, "1"):
            with self.subTest(version=bad), self.assertRaises(EvidenceError):
                OutcomeDefinition.parse({**self.definition, "schema_version": bad})
        with self.assertRaises(EvidenceError):
            Assessment.parse({**self.assessment(), "provenance": "machine_measured"})
        measurement = {
            "schema_version": 1,
            "measurement_id": "m1",
            "metric": "human_interventions",
            "unit": "count",
            "value": True,
            "provenance": "caller_declared",
            "reason": "reviewed",
        }
        with self.assertRaises(EvidenceError):
            self.record(INTERVENTION, measurement)

    def test_document_rejects_duplicate_keys_nonfinite_and_oversize(self):
        path = Path(self.temp.name) / "input.json"
        for content in ('{"x":1,"x":2}', '{"x":NaN}', '"' + "x" * 65536 + '"'):
            path.write_text(content)
            with self.subTest(content=content[:30]), self.assertRaises(EvidenceError):
                read_document(path)

    def test_dirty_git_content_binding_preserves_user_index(self):
        (self.workspace / "code.py").write_text("value = 2\n")
        self.git("add", "code.py", workspace=True)
        (self.workspace / "code.py").write_text("value = 3\n")
        (self.workspace / "new.txt").write_text("untracked\n")
        index_before = self.git("diff", "--cached", workspace=True)
        tree = source_tree(self.workspace)[1]
        self.assertEqual(index_before, self.git("diff", "--cached", workspace=True))
        self.git("add", "-A", workspace=True)
        self.assertEqual(tree, self.git("write-tree", workspace=True))

    def test_machine_validation_is_replayed_without_rerunning(self):
        before = self.profile.paths.runtime_file.read_bytes()
        first = self.validate()
        with patch(
            "blackdog.evidence.run_validation_commands",
            side_effect=AssertionError("rerun"),
        ):
            second = self.validate()
        self.assertTrue(second["replayed"])
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(second["applicability"]["status"], "current")
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), before)
        (self.workspace / "code.py").write_text("changed after validation\n")
        with patch(
            "blackdog.evidence.run_validation_commands",
            side_effect=AssertionError("rerun"),
        ):
            self.assertEqual(self.validate()["applicability"]["status"], "stale")

    def test_identical_tree_commit_preserves_validation_applicability(self):
        (self.workspace / "code.py").write_text("value = 2\n")
        first = self.validate()
        self.git("add", "-A", workspace=True)
        self.git("commit", "-qm", "same tested content", workspace=True)
        self.assertEqual(self.validate()["applicability"]["status"], "current")
        self.record(DEFINITION, self.definition)
        self.finish()
        self.record(ASSESSMENT, self.assessment(refs=[first["event_id"]]))
        self.assertEqual(outcome_report(self.profile)["tasks"][0]["outcome"], "met")

    def test_run_without_result_is_indeterminate_and_does_not_rerun(self):
        with patch(
            "blackdog.evidence.run_validation_commands", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.validate()
        with patch(
            "blackdog.evidence.run_validation_commands",
            side_effect=AssertionError("rerun"),
        ):
            retry = self.validate()
        self.assertEqual(retry["status"], "indeterminate")
        self.assertFalse(retry["rerun_performed"])
        self.assertTrue(self.validate("run-2")["receipt"]["run"]["all_passed"])

    def test_result_write_failure_retains_indeterminate_intent(self):
        from blackdog.evidence import _append

        def append(*args, **kwargs):
            if kwargs["kind"] == VALIDATION_RESULT:
                raise OSError("disk full")
            return _append(*args, **kwargs)

        with (
            patch("blackdog.evidence._append", side_effect=append),
            self.assertRaises(OSError),
        ):
            self.validate()
        with patch(
            "blackdog.evidence.run_validation_commands",
            side_effect=AssertionError("rerun"),
        ):
            self.assertEqual(self.validate()["status"], "indeterminate")

    def test_changed_inputs_during_run_cannot_support_met_assessment(self):
        self.record(DEFINITION, self.definition)
        config = self.workspace / "blackdog.toml"
        config.write_text(
            config.read_text().replace("exit 0", "printf changed > code.py")
        )
        run = self.validate()
        self.assertTrue(run["receipt"]["run"]["all_passed"])
        self.assertEqual(run["applicability"]["status"], "stale")
        with self.assertRaises(EvidenceError):
            self.record(ASSESSMENT, self.assessment(refs=[run["event_id"]]))

    def test_empty_config_never_records_success(self):
        config = self.workspace / "blackdog.toml"
        config.write_text(config.read_text().replace('["exit 0"]', "[]"))
        empty = replace(load_profile(self.workspace), validation_commands=())
        with (
            patch("blackdog.evidence.load_profile", return_value=empty),
            self.assertRaises(EvidenceError),
        ):
            self.validate()
        self.assertFalse(
            any(
                e["type"] == VALIDATION_INTENT
                for e in load_events(self.profile.paths.events_file)
            )
        )

    def test_terminal_review_and_correction_preserve_lifecycle(self):
        self.record(DEFINITION, self.definition)
        run = self.validate()
        self.finish()
        before = self.profile.paths.runtime_file.read_bytes()
        first = self.record(ASSESSMENT, self.assessment(refs=[run["event_id"]]))
        report = outcome_report(self.profile)["tasks"][0]
        self.assertEqual(report["outcome"], "met")
        self.assertEqual(report["integration"]["status"], "landed")
        self.assertFalse(report["criteria"][0]["reviewer_identity_authenticated"])
        self.record(
            ASSESSMENT,
            self.assessment(
                assessment_id="review-2", result="not_met", supersedes=first["event_id"]
            ),
        )
        report = outcome_report(self.profile)["tasks"][0]
        self.assertEqual(report["outcome"], "not_met")
        self.assertEqual(report["criterion_regressions"]["value"], 1)
        predecessor = report["criteria"][0]["assessment_event_id"]
        for index, result in enumerate(("met", "not_met"), start=3):
            corrected = self.record(
                ASSESSMENT,
                self.assessment(
                    assessment_id=f"review-{index}",
                    result=result,
                    supersedes=predecessor,
                ),
            )
            predecessor = corrected["event_id"]
        regressions = outcome_report(self.profile)["tasks"][0]["criterion_regressions"]
        self.assertEqual(regressions["value"], 1)
        self.assertEqual(regressions["transition_observations"], 2)
        self.assertEqual(regressions["criterion_population"], 1)
        measurement = {
            "schema_version": 1,
            "measurement_id": "intervention-1",
            "metric": "human_interventions",
            "unit": "count",
            "value": 2,
            "provenance": "caller_declared",
            "reason": "Explicit fixture observation",
        }
        self.record(INTERVENTION, measurement)
        self.record(INTERVENTION, measurement)
        aggregate = outcome_report(self.profile, include_tasks=False)
        observations = aggregate["observations"]
        self.assertEqual(observations["human_interventions"]["value"], 2)
        self.assertEqual(observations["human_interventions"]["observations"], 1)
        self.assertEqual(observations["criterion_regressions"]["distinct_criteria"], 1)
        self.assertEqual(
            observations["criterion_regressions"]["tasks_with_regressions"], 1
        )
        self.assertEqual(
            observations["criterion_regressions"]["transition_observations"], 2
        )
        self.assertEqual(aggregate["cohorts"][0]["observations"], observations)
        self.assertEqual(before, self.profile.paths.runtime_file.read_bytes())

    def test_competing_corrections_are_compare_and_swap(self):
        self.record(DEFINITION, self.definition)
        first = self.record(ASSESSMENT, self.assessment())

        def record(index):
            try:
                self.record(
                    ASSESSMENT,
                    self.assessment(
                        assessment_id=f"review-{index}", supersedes=first["event_id"]
                    ),
                )
                return True
            except EvidenceError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(record, (2, 3))), [False, True])

    def test_old_assessed_tree_does_not_accept_changed_final_artifact(self):
        self.record(DEFINITION, self.definition)
        self.record(ASSESSMENT, self.assessment())
        (self.workspace / "code.py").write_text("changed after assessment\n")
        self.finish()
        report = outcome_report(self.profile)["tasks"][0]
        self.assertEqual(report["outcome"], "not_assessed")
        self.assertEqual(report["criteria"][0]["artifact_applicability"], "stale")

    def test_history_is_missing_and_percentiles_include_denominators(self):
        report = outcome_report(self.profile)
        self.assertEqual(report["definitions_missing"], 1)
        self.assertEqual(report["tasks"][0]["outcome"], "not_assessed")
        self.assertIsNone(report["tasks"][0]["human_interventions"]["value"])
        d = distribution([10, 20, 30, 40], population=5, unit="ms", source="test")
        self.assertEqual(
            (d["samples"], d["missing"], d["median"], d["p95"]), (4, 1, 25, 40)
        )

    def test_reader_rejects_known_event_unknown_schema(self):
        append_event_once(
            self.profile.paths.events_file,
            event_id="corrupt",
            event_type=DEFINITION,
            payload={
                "schema_version": 99,
                "task_id": self.task.task_id,
                "attempt_id": self.attempt.attempt_id,
                "data": self.definition,
            },
        )
        with self.assertRaises(EvidenceError):
            outcome_report(self.profile)

    def test_canonical_event_reader_rejects_duplicate_object_keys(self):
        self.record(DEFINITION, self.definition)
        path = self.profile.paths.events_file
        content = path.read_text()
        content = content.replace(
            '"task_class": "bugfix"', '"task_class": "other", "task_class": "bugfix"'
        )
        path.write_text(content)
        from blackdog_core.state import StoreError

        with self.assertRaises(StoreError):
            outcome_report(self.profile)

    def test_foreign_actor_is_refused_before_execution(self):
        with patch(
            "blackdog.evidence.run_validation_commands",
            side_effect=AssertionError("executed"),
        ):
            with self.assertRaises(EvidenceError):
                validate_task(
                    self.profile,
                    task_id=self.task.task_id,
                    attempt_id=self.attempt.attempt_id,
                    actor="foreign",
                    run_id="x",
                )

    def test_binding_observation_does_not_block_other_tasks(self):
        from blackdog.evidence import observe_binding as observe

        entered, release = Event(), Event()

        def delayed(workspace):
            entered.set()
            if not release.wait(5):
                raise AssertionError("observation not released")
            return observe(workspace)

        with patch("blackdog.evidence.observe_binding", side_effect=delayed):
            with ThreadPoolExecutor(max_workers=2) as pool:
                validating = pool.submit(self.validate)
                self.assertTrue(entered.wait(3))
                other = pool.submit(create_task, self.profile, title="Independent task")
                try:
                    self.assertEqual(other.result(timeout=1).title, "Independent task")
                finally:
                    release.set()
                self.assertEqual(validating.result(timeout=5)["status"], "completed")

    def test_terminal_review_survives_source_object_loss_after_landing(self):
        from blackdog.evidence import _git
        from blackdog.wtam import land_task
        from blackdog_core.state import ValidationRecord

        self.record(DEFINITION, self.definition)
        (self.workspace / "code.py").write_text("value = 2\n")
        run = self.validate()
        landed = land_task(
            self.profile,
            task_id=self.task.task_id,
            actor="codex",
            summary="Deliver corrected output",
            validations=(ValidationRecord("tests", "passed"),),
            cwd=self.workspace,
        )
        self.assertEqual(landed.operation_status, "succeeded")
        terminal = load_runtime_state(self.profile.paths).tasks[0].attempts[-1]

        def missing_source(workspace, *args, **kwargs):
            if args == ("rev-parse", f"{terminal.commit}^{{tree}}"):
                raise EvidenceError("source object was collected")
            return _git(workspace, *args, **kwargs)

        with patch("blackdog.evidence._git", side_effect=missing_source):
            self.record(ASSESSMENT, self.assessment(refs=[run["event_id"]]))
            self.assertEqual(outcome_report(self.profile)["tasks"][0]["outcome"], "met")

    def test_measurement_store_failure_preserves_completed_lifecycle_result(self):
        from blackdog.wtam import land_task
        from blackdog_core.state import StoreError, ValidationRecord

        (self.workspace / "code.py").write_text("value = 2\n")
        with patch(
            "blackdog.measurement.locked_evidence",
            side_effect=StoreError("unreadable ledger"),
        ):
            landed = land_task(
                self.profile,
                task_id=self.task.task_id,
                actor="codex",
                summary="Deliver corrected output",
                validations=(ValidationRecord("tests", "passed"),),
                cwd=self.workspace,
            )
        self.assertEqual(landed.operation_status, "succeeded")
        self.assertEqual(landed.next_action.kind, "complete")
        self.assertEqual(
            landed["phase_measurement"]["missing_reason"], "evidence_write_failed"
        )
        self.assertEqual(load_runtime_state(self.profile.paths).tasks[0].status, "done")

    def test_validation_refuses_primary_and_invalid_start_lineage_before_commands(self):
        from blackdog_core.state import JsonRuntimeStore

        original = load_runtime_state(self.profile.paths)
        cases = (
            replace(
                self.attempt,
                worktree_path=str(self.root),
                branch=self.git("branch", "--show-current"),
            ),
            replace(self.attempt, start_commit="f" * 40),
            replace(self.attempt, worktree_role=None),
            replace(
                self.attempt, worktree_path=str(Path(self.temp.name) / "unregistered")
            ),
        )
        for invalid in cases:
            state = replace(
                original, tasks=(replace(original.tasks[0], attempts=(invalid,)),)
            )
            JsonRuntimeStore().save(self.profile.paths.runtime_file, state)
            with (
                self.subTest(attempt=invalid),
                patch(
                    "blackdog.evidence.run_validation_commands",
                    side_effect=AssertionError("executed"),
                ),
            ):
                with self.assertRaises(EvidenceError):
                    self.validate()
        JsonRuntimeStore().save(self.profile.paths.runtime_file, original)

    def test_different_runtime_descriptors_are_not_pooled_in_a_cohort(self):
        from blackdog.evidence import environment_descriptor

        self.record(DEFINITION, self.definition)
        self.validate()
        other_path = Path(self.temp.name) / "second worktree"
        self.git("worktree", "add", "-qb", "second-branch", str(other_path))
        other_task = create_task(self.profile, title="Second comparable task")
        other_attempt = start_task(
            self.profile,
            task_id=other_task.task_id,
            actor="codex",
            worktree_path=str(other_path),
            branch="second-branch",
            workspace_mode="git-worktree",
            worktree_role="task",
            target_branch=self.attempt.target_branch,
            start_commit=self.git("rev-parse", "HEAD"),
        )
        record_outcome(
            self.profile,
            task_id=other_task.task_id,
            attempt_id=other_attempt.attempt_id,
            actor="codex",
            kind=DEFINITION,
            document=self.definition,
        )
        descriptor = {
            **environment_descriptor(load_profile(other_path)),
            "python_version": "3.99.0",
        }
        with patch("blackdog.evidence.environment_descriptor", return_value=descriptor):
            validate_task(
                self.profile,
                task_id=other_task.task_id,
                attempt_id=other_attempt.attempt_id,
                actor="codex",
                run_id="other-run",
            )
        cohorts = outcome_report(self.profile)["cohorts"]
        self.assertEqual(len(cohorts), 2)
        self.assertEqual([c["task_count"] for c in cohorts], [1, 1])
        self.assertNotEqual(
            cohorts[0]["environment_sha256"], cohorts[1]["environment_sha256"]
        )

    def test_validation_parser_rejects_native_wrong_types(self):
        from blackdog_core.validation import ValidationCommandResult

        row = {
            "index": 0,
            "command_sha256": "1" * 64,
            "status": "passed",
            "returncode": 0,
            "elapsed_ms": 1,
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "output_retained": False,
        }
        for invalid in ({**row, "command_sha256": int("1" * 64)}, {**row, "status": 0}):
            with self.assertRaises(ValueError):
                ValidationCommandResult.from_dict(invalid)

    def test_aggregate_preserves_provenance_and_historical_applicability(self):
        self.record(DEFINITION, self.definition)
        self.validate()
        before = outcome_report(self.profile, include_tasks=False)
        self.assertEqual(
            before["validation_coverage"]["current_applicability"]["current"], 1
        )
        self.finish()
        first = self.record(
            ASSESSMENT, {**self.assessment(), "provenance": "caller_declared"}
        )
        self.record(
            ASSESSMENT,
            self.assessment(assessment_id="review-2", supersedes=first["event_id"]),
        )
        aggregate = outcome_report(self.profile, include_tasks=False)
        self.assertEqual(aggregate["tasks"], [])
        coverage = aggregate["assessment_coverage"]
        self.assertEqual(
            coverage["current_required_by_provenance"],
            {"caller_declared": 0, "reviewer_asserted": 1},
        )
        self.assertEqual(
            coverage["recorded_assessments_by_provenance"],
            {"caller_declared": 1, "reviewer_asserted": 1},
        )
        self.assertEqual(
            aggregate["validation_coverage"]["current_applicability"]["unknown"], 1
        )
        self.assertEqual(
            aggregate["validation_coverage"]["historical_machine_passed_runs"], 1
        )
        accepted = aggregate["cohorts"][0]["criteria_met_completion"]
        self.assertEqual((accepted["eligible_tasks"], accepted["cohort_tasks"]), (1, 1))

    def test_concurrent_same_run_has_one_effect_and_same_receipt(self):
        from blackdog.evidence import run_validation_commands

        with patch(
            "blackdog.evidence.run_validation_commands", wraps=run_validation_commands
        ) as run:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.validate(), range(2)))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(results[0]["event_id"], results[1]["event_id"])
        self.assertEqual(sorted(r["replayed"] for r in results), [False, True])

    def test_intent_failure_prevents_effect_and_post_receipt_failure_replays(self):
        from blackdog.evidence import _append

        def fail_intent(*args, **kwargs):
            raise OSError("cannot write intent")

        with (
            patch("blackdog.evidence._append", side_effect=fail_intent),
            patch(
                "blackdog.evidence.run_validation_commands",
                side_effect=AssertionError("executed"),
            ),
        ):
            with self.assertRaises(OSError):
                self.validate()

        def fail_after_receipt(*args, **kwargs):
            result = _append(*args, **kwargs)
            if kwargs["kind"] == VALIDATION_RESULT:
                raise OSError("interrupted after durable receipt")
            return result

        with (
            patch("blackdog.evidence._append", side_effect=fail_after_receipt),
            self.assertRaises(OSError),
        ):
            self.validate()
        before = self.profile.paths.events_file.read_bytes()
        with patch(
            "blackdog.evidence.run_validation_commands",
            side_effect=AssertionError("executed"),
        ):
            self.assertTrue(self.validate()["replayed"])
        self.assertEqual(before, self.profile.paths.events_file.read_bytes())

    def test_runtime_identity_is_release_bound_or_explicitly_unknown(self):
        from blackdog.evidence import runtime_descriptor
        from blackdog.runtime_distribution import RuntimeDistributionError

        with patch(
            "blackdog.evidence.runtime_identity",
            return_value={"kind": "release_sha256", "value": "a" * 64},
        ):
            self.assertEqual(
                runtime_descriptor(),
                {"kind": "release_sha256", "value": "a" * 64, "missing_reason": None},
            )
        with patch("blackdog.evidence.runtime_identity", side_effect=OSError):
            identity = runtime_descriptor()
            self.assertEqual(identity["kind"], "unknown")
            self.assertIsNone(identity["value"])
        with (
            patch("blackdog.evidence.runtime_identity", return_value=None),
            patch(
                "blackdog.evidence.release_bytes", side_effect=RuntimeDistributionError
            ),
        ):
            self.assertEqual(runtime_descriptor()["kind"], "unknown")

    def test_source_descriptor_reuses_bounded_distribution_projection(self):
        from blackdog.evidence import runtime_descriptor
        from blackdog.runtime_distribution import release_bytes

        entries = {
            "src/blackdog/__init__.py": '__version__ = "test"\n',
            "src/blackdog/runtime_distribution.py": "# Public fixture\n",
            "src/blackdog_core/__init__.py": "# Public fixture\n",
            "src/blackdog_cli/__init__.py": "# Public fixture\n",
            "src/blackdog_cli/main.py": "# Public fixture\n",
            "LICENSE": "Public fixture\n",
        }
        for relative, content in entries.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.git("add", "src", "LICENSE")
        package = self.root / "src" / "blackdog"
        with (
            patch("blackdog.evidence.runtime_identity", return_value=None),
            patch(
                "blackdog.evidence.release_bytes",
                side_effect=lambda: release_bytes(source_root=self.root),
            ),
        ):
            first = runtime_descriptor()
            self.assertEqual(first["kind"], "source_sha256")
            (package / "untracked.py").write_text("# Excluded fixture\n")
            (package / "loop").symlink_to(package, target_is_directory=True)
            self.assertEqual(first, runtime_descriptor())
            outside = Path(self.temp.name) / "outside.py"
            outside.write_text("# Outside fixture\n")
            tracked = package / "runtime_distribution.py"
            tracked.unlink()
            tracked.symlink_to(outside)
            read_bytes = Path.read_bytes

            def checked_read(path):
                self.assertNotEqual(path, outside)
                self.assertNotEqual(path, tracked)
                return read_bytes(path)

            with patch.object(Path, "read_bytes", checked_read):
                self.assertEqual(runtime_descriptor()["kind"], "unknown")
                tracked.unlink()
                os.mkfifo(tracked)
                self.assertEqual(runtime_descriptor()["kind"], "unknown")

    def test_unknown_runtime_cannot_qualify_receipts_or_cohorts(self):
        identity = {
            "kind": "unknown",
            "value": None,
            "missing_reason": "runtime_source_unavailable",
        }
        self.record(DEFINITION, self.definition)
        with patch("blackdog.evidence.runtime_descriptor", return_value=identity):
            run = self.validate()
            self.assertEqual(run["applicability"]["status"], "unknown")
            report = outcome_report(self.profile)
        self.assertFalse(report["cohorts"][0]["comparable"])
        self.assertIsNone(report["cohorts"][0]["environment_sha256"])
        with self.assertRaises(EvidenceError):
            self.record(ASSESSMENT, self.assessment(refs=[run["event_id"]]))

    def test_canceled_tasks_do_not_shorten_criteria_met_completion(self):
        from blackdog.outcome_reporting import project_outcomes

        self.record(DEFINITION, self.definition)
        self.validate()
        self.finish()
        self.record(ASSESSMENT, self.assessment())
        other = create_task(self.profile, title="Canceled comparable task")
        attempt = start_task(
            self.profile,
            task_id=other.task_id,
            actor="codex",
            worktree_path=str(self.workspace),
            branch="task-branch",
            workspace_mode="git-worktree",
            worktree_role="task",
            target_branch=self.attempt.target_branch,
            start_commit=self.git("rev-parse", "HEAD"),
        )
        record_outcome(
            self.profile,
            task_id=other.task_id,
            attempt_id=attempt.attempt_id,
            actor="codex",
            kind=DEFINITION,
            document=self.definition,
        )
        validate_task(
            self.profile,
            task_id=other.task_id,
            attempt_id=attempt.attempt_id,
            actor="codex",
            run_id="canceled-validation",
        )
        finish_task(
            self.profile,
            task_id=other.task_id,
            attempt_id=attempt.attempt_id,
            actor="codex",
            status="abandoned",
            summary="Canceled fixture",
        )
        with locked_evidence(self.profile) as (state, _, events):
            tasks = tuple(
                replace(
                    task,
                    attempts=(
                        replace(
                            task.attempts[0],
                            started_at="2026-01-01T00:00:00Z",
                            ended_at=(
                                "2026-01-01T00:00:10Z"
                                if task.task_id == self.task.task_id
                                else "2026-01-01T00:00:01Z"
                            ),
                        ),
                    ),
                )
                for task in state.tasks
            )
        report = project_outcomes(
            self.profile, replace(state, tasks=tasks), events, include_tasks=False
        )
        self.assertEqual(len(report["cohorts"]), 1)
        cohort = report["cohorts"][0]
        accepted = cohort["criteria_met_completion"]
        self.assertEqual((accepted["eligible_tasks"], accepted["cohort_tasks"]), (1, 2))
        self.assertEqual(accepted["median"], 10000)
        self.assertEqual(cohort["all_terminal_completion_elapsed"]["median"], 5500)
        self.assertTrue(accepted["small_sample"])
        self.assertEqual(len(cohort["completion_by_status_and_outcome"]), 2)

    def test_no_codex_stats_never_collects_provider_turns(self):
        from blackdog.stats import build_stats

        with patch(
            "blackdog.stats.collect_codex_turns",
            side_effect=AssertionError("provider scan"),
        ):
            report = build_stats(project_roots=(self.root,), no_codex=True)
        self.assertFalse(report.provider_data_read)
        self.assertIsNone(report.summary["codex_total_tokens"])
        self.assertEqual(report.to_dict()["provider_missing_reason"], "not_requested")
        self.assertEqual(report.outcome_evidence[0]["definitions_missing"], 1)
