from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import blackdog.landing as landing
import blackdog.wtam as wtam
import blackdog_core.backlog as backlog
import blackdog_core.state as state
from blackdog_core.backlog import start_task, upsert_workset
from blackdog_core.profile import DEFAULT_WORKTREES_DIR, load_profile, render_default_profile
from blackdog_core.state import ValidationRecord, append_event, create_prompt_receipt, load_events, load_runtime_state


PHASES_AFTER_INTENT = landing.LANDING_PHASES[1:]


def _run_git(root: Path, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_output(root: Path, *args: str) -> str:
    return _run_git(root, *args).stdout.strip()


def _ledger_intent(repo: "LandingRepo") -> landing.LandingIntent:
    head = _git_output(repo.root, "rev-parse", "HEAD")
    return landing.LandingIntent(
        workset_id=repo.workset_id,
        task_id=repo.task_id,
        attempt_id=repo.attempt.attempt_id,
        actor=repo.actor,
        branch=repo.branch,
        target_branch="main",
        worktree_path=str(repo.worktree),
        primary_worktree=str(repo.root),
        target_base_commit=head,
        source_head_commit=head,
        source_fingerprint="fixture-source-fingerprint",
        expected_source_tree_hash="fixture-source-tree",
        source_dirty=True,
        summary=repo.summary,
        note=None,
        validations=(("unit", "passed"),),
        residuals=("none",),
        followup_candidates=("none",),
        changed_paths=("fixture.txt",),
        cleanup=True,
        commit_message="fixture landing\n",
        temporary_worktree_path=str(repo.base / "ledger-temporary"),
    )


@dataclass(frozen=True)
class RepoSnapshot:
    main: str
    source_ref: str | None
    worktrees: str
    events: bytes
    runtime: bytes


class LandingRepo:
    """One isolated task attempt with a dirty branch-backed worktree."""

    def __init__(self, *, cleanup: bool = True, suffix: str = "fault") -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="blackdog-landing-fault-")
        self.base = Path(self._temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        _run_git(self.root, "init", "-b", "main")
        _run_git(self.root, "config", "user.email", "blackdog@example.com")
        _run_git(self.root, "config", "user.name", "Blackdog Test")
        (self.root / ".gitignore").write_text(".blackdog/\n", encoding="utf-8")
        profile_text = render_default_profile("Landing fault tests").replace(
            f'worktrees_dir = "{DEFAULT_WORKTREES_DIR}"',
            'worktrees_dir = "../worktrees"',
        )
        (self.root / "blackdog.toml").write_text(profile_text, encoding="utf-8")
        _run_git(self.root, "add", ".gitignore", "blackdog.toml")
        _run_git(self.root, "commit", "-m", "Initialize fixture")

        self.profile = load_profile(self.root)
        self.workset_id = f"landing-{suffix}"
        self.task_id = "LAND-1"
        self.actor = "codex"
        self.summary = f"land {suffix} change"
        self.cleanup = cleanup
        upsert_workset(
            self.profile,
            {
                "id": self.workset_id,
                "title": f"Landing {suffix}",
                "branch_intent": {
                    "target_branch": "main",
                    "integration_branch": "main",
                },
                "tasks": [
                    {
                        "id": self.task_id,
                        "title": f"Exercise {suffix}",
                        "intent": "prove transactional landing recovery",
                    }
                ],
            },
        )
        self.branch = f"codex/{self.workset_id.lower()}"
        self.worktree = self.base / "worktrees" / self.task_id.lower()
        self.worktree.parent.mkdir(parents=True, exist_ok=True)
        self.start_commit = _git_output(self.root, "rev-parse", "main")
        _run_git(
            self.root,
            "worktree",
            "add",
            "-b",
            self.branch,
            str(self.worktree),
            "main",
        )
        self.attempt = start_task(
            self.profile,
            workset_id=self.workset_id,
            task_id=self.task_id,
            actor=self.actor,
            workspace_identity=f"fixture-{suffix}",
            workspace_mode="git-worktree",
            worktree_role="task",
            worktree_path=str(self.worktree),
            branch=self.branch,
            target_branch="main",
            integration_branch="main",
            start_commit=self.start_commit,
            prompt_receipt=create_prompt_receipt(
                f"Exercise {suffix} landing.", source="unit-test"
            ),
        )
        (self.worktree / f"{suffix}.txt").write_text(
            f"{suffix}\n", encoding="utf-8"
        )

    def close(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(self.worktree)],
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
        self._temporary.cleanup()

    def land(self):
        return wtam.land_task(
            self.profile,
            workset_id=self.workset_id,
            task_id=self.task_id,
            actor=self.actor,
            summary=self.summary,
            validations=(ValidationRecord(name="unit", status="passed"),),
            residuals=("none",),
            followup_candidates=("none",),
            cleanup=self.cleanup,
        )

    def close_attempt(self):
        return wtam.close_task(
            self.profile,
            workset_id=self.workset_id,
            task_id=self.task_id,
            actor=self.actor,
            status="blocked",
            summary="block the interrupted landing",
            validations=(ValidationRecord(name="abort", status="passed"),),
            residuals=("retained source",),
            followup_candidates=("retry from retained source",),
            cleanup=True,
        )

    def transaction(self):
        return landing.load_landing_transaction(
            self.profile,
            workset_id=self.workset_id,
            task_id=self.task_id,
            attempt_id=self.attempt.attempt_id,
        )

    def events_for_attempt(self) -> list[dict[str, object]]:
        return [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("payload", {}).get("attempt_id") == self.attempt.attempt_id
        ]

    def source_ref(self) -> str | None:
        completed = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "--verify", self.branch],
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip() if completed.returncode == 0 else None

    def snapshot(self) -> RepoSnapshot:
        return RepoSnapshot(
            main=_git_output(self.root, "rev-parse", "main"),
            source_ref=self.source_ref(),
            worktrees=_git_output(self.root, "worktree", "list", "--porcelain"),
            events=self.profile.paths.events_file.read_bytes(),
            runtime=self.profile.paths.runtime_file.read_bytes(),
        )

    def latest_attempt(self):
        runtime = load_runtime_state(self.profile.paths)
        return next(
            row
            for workset in runtime.worksets
            if workset.workset_id == self.workset_id
            for row in workset.attempts
            if row.attempt_id == self.attempt.attempt_id
        )


@contextmanager
def landing_repo(*, cleanup: bool = True, suffix: str = "fault"):
    repo = LandingRepo(cleanup=cleanup, suffix=suffix)
    try:
        yield repo
    finally:
        repo.close()


class LandingTransactionFaultTests(unittest.TestCase):
    maxDiff = None

    def _assert_resume_action(self, repo: LandingRepo, result) -> None:
        transaction = repo.transaction()
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(result.operation_status, "partial")
        self.assertEqual(result.next_action.kind, "command")
        self.assertEqual(result.next_action.action_id, "resume_landing_transaction")
        assert result.next_action.action is not None
        self.assertEqual(
            result.next_action.action.argv,
            transaction.intent.task_land_argv(
                executable=result.next_action.action.argv[0],
                project_root=Path(transaction.intent.primary_worktree),
            ),
        )

    def _interrupt_landing_at_phase(
        self,
        repo: LandingRepo,
        *,
        phase: str,
        after_append: bool = False,
    ):
        original = wtam.record_landing_phase
        tripped = False

        def injected(*args, **kwargs):
            nonlocal tripped
            if kwargs.get("phase") == phase and not tripped:
                tripped = True
                if after_append:
                    original(*args, **kwargs)
                raise OSError(f"fault {'after' if after_append else 'before'} {phase}")
            return original(*args, **kwargs)

        with patch.object(wtam, "record_landing_phase", side_effect=injected):
            result = repo.land()
        self.assertTrue(tripped, phase)
        return result

    def _canonical_partial(self, repo: LandingRepo):
        blocker = wtam.StaleTaskBranchError(
            branch=repo.branch,
            target_branch="main",
            branch_worktree=repo.worktree,
        )
        with patch.object(wtam, "_update_landing_target", side_effect=blocker):
            result = repo.land()
        transaction = repo.transaction()
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertEqual(
            transaction.phases,
            ("intent_recorded", "source_prepared", "canonical_commit_created"),
        )
        self.assertEqual(result.operation_status, "partial")
        return transaction

    def _assert_converges_then_noops(self, repo: LandingRepo) -> None:
        second = repo.land()
        self.assertEqual(second.operation_status, "succeeded", second.to_dict())
        transaction = repo.transaction()
        self.assertIsNotNone(transaction)
        assert transaction is not None
        self.assertTrue(transaction.complete)
        before = repo.snapshot()
        third = repo.land()
        self.assertEqual(third.operation_status, "succeeded", third.to_dict())
        self.assertFalse(third.mutation_started, third.to_dict())
        self.assertFalse(third.mutation_completed, third.to_dict())
        self.assertEqual(third.mutation_phase, "landing_complete")
        self.assertEqual(repo.snapshot(), before)

    def test_fixture_lands_and_exact_retry_is_a_noop(self) -> None:
        with landing_repo(suffix="smoke") as repo:
            self._assert_converges_then_noops(repo)

    def test_ledger_rejects_schema_order_duplicate_and_type_corruption(self) -> None:
        def mutate_schema(rows, index):
            rows[index]["payload"]["unexpected"] = "field"

        def mutate_order(rows, index):
            rows[index - 1], rows[index] = rows[index], rows[index - 1]

        def mutate_duplicate(rows, index):
            rows.insert(index + 1, json.loads(json.dumps(rows[index])))

        def mutate_schema_type(rows, index):
            rows[index]["payload"]["schema_version"] = True

        def mutate_data_type(rows, index):
            rows[index]["payload"]["data"] = []

        cases = {
            "envelope-extra-field": mutate_schema,
            "out-of-order-phase": mutate_order,
            "duplicate-phase": mutate_duplicate,
            "boolean-schema-version": mutate_schema_type,
            "non-object-phase-data": mutate_data_type,
        }
        for suffix, mutator in cases.items():
            with self.subTest(case=suffix), landing_repo(suffix=f"ledger-{suffix}") as repo:
                intent = _ledger_intent(repo)
                landing.record_landing_phase(
                    repo.profile,
                    intent=intent,
                    phase="intent_recorded",
                    data=intent.to_dict(),
                )
                landing.record_landing_phase(
                    repo.profile,
                    intent=intent,
                    phase="source_prepared",
                    data={"source_commit": "candidate"},
                )
                rows = [
                    json.loads(line)
                    for line in repo.profile.paths.events_file.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                index = next(
                    i
                    for i, row in enumerate(rows)
                    if row.get("event_id")
                    == landing.landing_phase_event_id(intent.transaction_id, "source_prepared")
                )
                mutator(rows, index)
                repo.profile.paths.events_file.write_text(
                    "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                    encoding="utf-8",
                )
                with self.assertRaises(landing.LandingTransactionError):
                    repo.transaction()

        with self.subTest(case="intent-bool-coercion"), landing_repo(
            suffix="ledger-bool"
        ) as repo:
            intent = _ledger_intent(repo)
            landing.record_landing_phase(
                repo.profile,
                intent=intent,
                phase="intent_recorded",
                data=intent.to_dict(),
            )
            rows = [
                json.loads(line)
                for line in repo.profile.paths.events_file.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            row = next(
                row
                for row in rows
                if row.get("event_id")
                == landing.landing_phase_event_id(intent.transaction_id, "intent_recorded")
            )
            row["payload"]["data"]["cleanup"] = 1
            repo.profile.paths.events_file.write_text(
                "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                landing.LandingTransactionError, "must be booleans"
            ):
                repo.transaction()

    def test_phase_and_land_event_append_once_exact_retry_is_byte_noop(self) -> None:
        with landing_repo(suffix="append-once") as repo:
            intent = _ledger_intent(repo)
            self.assertTrue(
                landing.record_landing_phase(
                    repo.profile,
                    intent=intent,
                    phase="intent_recorded",
                    data=intent.to_dict(),
                )
            )
            self.assertTrue(
                landing.record_landing_phase(
                    repo.profile,
                    intent=intent,
                    phase="source_prepared",
                    data={"source_commit": "candidate"},
                )
            )
            phase_bytes = repo.profile.paths.events_file.read_bytes()
            self.assertFalse(
                landing.record_landing_phase(
                    repo.profile,
                    intent=intent,
                    phase="source_prepared",
                    data={"source_commit": "candidate"},
                )
            )
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), phase_bytes)
            with self.assertRaises(state.StoreError):
                landing.record_landing_phase(
                    repo.profile,
                    intent=intent,
                    phase="source_prepared",
                    data={"source_commit": "conflict"},
                )
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), phase_bytes)

            payload = {
                "workset_id": intent.workset_id,
                "task_id": intent.task_id,
                "attempt_id": intent.attempt_id,
                "status": "success",
            }
            self.assertTrue(
                landing.append_worktree_land_once(
                    repo.profile, intent=intent, payload=payload
                )
            )
            event_bytes = repo.profile.paths.events_file.read_bytes()
            self.assertFalse(
                landing.append_worktree_land_once(
                    repo.profile, intent=intent, payload=payload
                )
            )
            self.assertTrue(
                landing.exact_worktree_land_event(
                    repo.profile, intent=intent, payload=payload
                )
            )
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), event_bytes)
            with self.assertRaises(state.StoreError):
                landing.append_worktree_land_once(
                    repo.profile,
                    intent=intent,
                    payload={**payload, "status": "failed"},
                )
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), event_bytes)

    def test_each_durable_landing_side_effect_recovers_from_missing_phase_evidence(self) -> None:
        expected_previous = {
            "source_prepared": "intent_recorded",
            "canonical_commit_created": "source_prepared",
            "target_updated": "canonical_commit_created",
            "temporary_cleanup_complete": "target_updated",
            "runtime_finalized": "temporary_cleanup_complete",
            "land_event_recorded": "runtime_finalized",
            "task_cleanup_complete": "land_event_recorded",
            "complete": "task_cleanup_complete",
        }
        for phase in PHASES_AFTER_INTENT:
            with self.subTest(phase=phase), landing_repo(suffix=f"before-{phase}") as repo:
                first = self._interrupt_landing_at_phase(repo, phase=phase)
                self._assert_resume_action(repo, first)
                self.assertTrue(first.mutation_started, first.to_dict())
                self.assertFalse(first.mutation_completed, first.to_dict())
                self.assertEqual(
                    first.mutation_phase,
                    f"landing_{expected_previous[phase]}",
                    first.to_dict(),
                )
                self._assert_converges_then_noops(repo)

    def test_durable_phase_append_faults_report_exact_truth_and_converge(self) -> None:
        for phase in PHASES_AFTER_INTENT:
            with self.subTest(phase=phase), landing_repo(suffix=f"after-{phase}") as repo:
                first = self._interrupt_landing_at_phase(
                    repo, phase=phase, after_append=True
                )
                if phase == "complete":
                    self.assertEqual(first.next_action.kind, "complete", first.to_dict())
                else:
                    self._assert_resume_action(repo, first)
                    self.assertFalse(first.mutation_completed, first.to_dict())
                self.assertTrue(first.mutation_started, first.to_dict())
                self.assertEqual(first.mutation_phase, f"landing_{phase}")
                self._assert_converges_then_noops(repo)
                if phase == "complete":
                    self.assertEqual(first.operation_status, "succeeded", first.to_dict())
                    self.assertTrue(first.mutation_completed, first.to_dict())

    def test_runtime_request_decision_write_and_owned_event_faults_converge(self) -> None:
        event_types = (
            "task.finalization.request",
            "task.finalization.decision",
            "task.release",
            "workset.release",
            "task.finish",
        )
        for event_type in event_types:
            with self.subTest(boundary=event_type), landing_repo(
                suffix=f"runtime-{event_type.replace('.', '-')}"
            ) as repo:
                original = backlog.append_event_once
                tripped = False

                def after_event(*args, **kwargs):
                    nonlocal tripped
                    changed = original(*args, **kwargs)
                    if kwargs.get("event_type") == event_type and not tripped:
                        tripped = True
                        raise OSError(f"fault after {event_type}")
                    return changed

                with patch.object(backlog, "append_event_once", side_effect=after_event):
                    first = repo.land()
                self.assertTrue(tripped)
                self._assert_resume_action(repo, first)
                self.assertEqual(
                    first.mutation_phase,
                    "landing_temporary_cleanup_complete",
                    first.to_dict(),
                )
                self.assertTrue(
                    any(
                        event.get("type") == event_type
                        for event in repo.events_for_attempt()
                    )
                )
                self._assert_converges_then_noops(repo)

        with self.subTest(boundary="runtime-write"), landing_repo(
            suffix="runtime-write"
        ) as repo:
            original_save = state.JsonRuntimeStore._save_unlocked
            tripped = False

            def after_runtime_write(store, path, runtime_state):
                nonlocal tripped
                original_save(store, path, runtime_state)
                if path == repo.profile.paths.runtime_file and not tripped:
                    tripped = True
                    raise OSError("fault after runtime write")

            with patch.object(
                state.JsonRuntimeStore,
                "_save_unlocked",
                autospec=True,
                side_effect=after_runtime_write,
            ):
                first = repo.land()
            self.assertTrue(tripped)
            self._assert_resume_action(repo, first)
            self.assertEqual(first.mutation_phase, "landing_temporary_cleanup_complete")
            self.assertEqual(repo.latest_attempt().status, "success")
            self._assert_converges_then_noops(repo)

    def test_worktree_land_and_source_cleanup_event_faults_converge(self) -> None:
        with self.subTest(boundary="worktree.land"), landing_repo(
            suffix="worktree-land-event"
        ) as repo:
            original = wtam.append_worktree_land_once
            tripped = False

            def after_land_event(*args, **kwargs):
                nonlocal tripped
                changed = original(*args, **kwargs)
                if not tripped:
                    tripped = True
                    raise OSError("fault after worktree.land")
                return changed

            with patch.object(
                wtam, "append_worktree_land_once", side_effect=after_land_event
            ):
                first = repo.land()
            self.assertTrue(tripped)
            self._assert_resume_action(repo, first)
            self.assertEqual(first.mutation_phase, "landing_runtime_finalized")
            self.assertEqual(
                sum(event.get("type") == "worktree.land" for event in repo.events_for_attempt()),
                1,
            )
            self._assert_converges_then_noops(repo)

        with self.subTest(boundary="source-filesystem-cleanup"), landing_repo(
            suffix="source-cleanup-filesystem"
        ) as repo:
            original = wtam.append_event_once
            tripped = False

            def before_cleanup_event(*args, **kwargs):
                nonlocal tripped
                if kwargs.get("event_type") == "worktree.cleanup" and not tripped:
                    tripped = True
                    raise OSError("fault before source cleanup event")
                return original(*args, **kwargs)

            with patch.object(wtam, "append_event_once", side_effect=before_cleanup_event):
                first = repo.land()
            self.assertTrue(tripped)
            self._assert_resume_action(repo, first)
            self.assertEqual(first.mutation_phase, "landing_land_event_recorded")
            self.assertFalse(repo.worktree.exists())
            self.assertIsNone(repo.source_ref())
            self.assertFalse(
                any(
                    event.get("type") == "worktree.cleanup"
                    for event in repo.events_for_attempt()
                )
            )
            self._assert_converges_then_noops(repo)

        with self.subTest(boundary="source-cleanup-event"), landing_repo(
            suffix="source-cleanup-event"
        ) as repo:
            original = wtam.append_event_once
            tripped = False

            def after_cleanup_event(*args, **kwargs):
                nonlocal tripped
                changed = original(*args, **kwargs)
                if kwargs.get("event_type") == "worktree.cleanup" and not tripped:
                    tripped = True
                    raise OSError("fault after source cleanup event")
                return changed

            with patch.object(wtam, "append_event_once", side_effect=after_cleanup_event):
                first = repo.land()
            self.assertTrue(tripped)
            self._assert_resume_action(repo, first)
            self.assertEqual(first.mutation_phase, "landing_land_event_recorded")
            self.assertEqual(
                sum(
                    event.get("type") == "worktree.cleanup"
                    for event in repo.events_for_attempt()
                ),
                1,
            )
            self._assert_converges_then_noops(repo)

    def test_keep_worktree_then_supported_cleanup_and_exact_retries_are_noops(self) -> None:
        with landing_repo(cleanup=False, suffix="keep-worktree") as repo:
            landed = repo.land()
            self.assertEqual(landed.operation_status, "succeeded", landed.to_dict())
            self.assertTrue(repo.worktree.exists())
            self.assertIsNotNone(repo.source_ref())
            landed_snapshot = repo.snapshot()
            repeated_land = repo.land()
            self.assertEqual(repeated_land.operation_status, "succeeded")
            self.assertFalse(repeated_land.mutation_started)
            self.assertEqual(repo.snapshot(), landed_snapshot)

            cleanup = wtam.cleanup_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
            )
            self.assertEqual(cleanup.operation_status, "succeeded", cleanup.to_dict())
            self.assertTrue(cleanup.mutation_started)
            self.assertTrue(cleanup.mutation_completed)
            self.assertFalse(repo.worktree.exists())
            self.assertIsNone(repo.source_ref())
            cleanup_snapshot = repo.snapshot()

            repeated_cleanup = wtam.cleanup_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
            )
            self.assertEqual(repeated_cleanup.operation_status, "succeeded")
            self.assertFalse(repeated_cleanup.mutation_started)
            self.assertFalse(repeated_cleanup.mutation_completed)
            self.assertEqual(repo.snapshot(), cleanup_snapshot)

    def test_concurrent_land_calls_serialize_to_one_commit_and_one_event(self) -> None:
        with landing_repo(suffix="concurrent-land") as repo:
            barrier = threading.Barrier(2)

            def run_land():
                barrier.wait(timeout=10)
                return repo.land()

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(run_land) for _ in range(2)]
                results = [future.result(timeout=30) for future in futures]

            self.assertEqual(
                [result.operation_status for result in results],
                ["succeeded", "succeeded"],
            )
            transaction = repo.transaction()
            self.assertIsNotNone(transaction)
            assert transaction is not None
            self.assertTrue(transaction.complete)
            self.assertEqual(
                _git_output(repo.root, "rev-list", "--count", f"{repo.start_commit}..main"),
                "1",
            )
            self.assertEqual(
                sum(
                    event.get("type") == "worktree.land"
                    for event in repo.events_for_attempt()
                ),
                1,
            )
            self.assertEqual(sum(result.mutation_started for result in results), 1)
            self.assertEqual(
                sum(
                    event.get("event_id")
                    == landing.landing_phase_event_id(
                        transaction.transaction_id, "complete"
                    )
                    for event in repo.events_for_attempt()
                ),
                1,
            )

    def test_concurrent_land_then_close_serializes_without_competing_terminal_mutation(self) -> None:
        with landing_repo(suffix="land-close") as repo:
            entered = threading.Event()
            release = threading.Event()
            original = wtam._run_landing_transaction
            tripped = False

            def paused_land(*args, **kwargs):
                nonlocal tripped
                if not tripped:
                    tripped = True
                    entered.set()
                    self.assertTrue(release.wait(timeout=10))
                return original(*args, **kwargs)

            with patch.object(
                wtam, "_run_landing_transaction", side_effect=paused_land
            ), ThreadPoolExecutor(max_workers=2) as executor:
                land_future = executor.submit(repo.land)
                self.assertTrue(entered.wait(timeout=10))
                close_future = executor.submit(repo.close_attempt)
                release.set()
                landed = land_future.result(timeout=30)
                with self.assertRaisesRegex(
                    backlog.BacklogError, "No active WTAM attempt"
                ):
                    close_future.result(timeout=30)

            self.assertEqual(landed.operation_status, "succeeded", landed.to_dict())
            self.assertEqual(repo.latest_attempt().status, "success")
            transaction = repo.transaction()
            self.assertIsNotNone(transaction)
            assert transaction is not None
            self.assertTrue(transaction.complete)
            self.assertFalse(transaction.abort_requested)
            self.assertEqual(
                sum(
                    event.get("type") == "worktree.land"
                    for event in repo.events_for_attempt()
                ),
                1,
            )
            self.assertFalse(
                any(
                    event.get("type") == "worktree.close"
                    for event in repo.events_for_attempt()
                )
            )

    def _interrupt_abort_after_request(self, repo: LandingRepo):
        original = backlog.append_event_once
        tripped = False

        def after_request(*args, **kwargs):
            nonlocal tripped
            changed = original(*args, **kwargs)
            if (
                kwargs.get("event_type") == "task.finalization.request"
                and not tripped
            ):
                tripped = True
                raise OSError("fault after abort finalization request")
            return changed

        with patch.object(backlog, "append_event_once", side_effect=after_request):
            result = repo.close_attempt()
        self.assertTrue(tripped)
        return result

    def test_abort_finalization_request_is_the_point_of_no_return(self) -> None:
        with landing_repo(suffix="abort-point-of-no-return") as repo:
            transaction = self._canonical_partial(repo)
            first = self._interrupt_abort_after_request(repo)
            self.assertEqual(first.operation_status, "blocked", first.to_dict())
            self.assertTrue(first.mutation_started, first.to_dict())
            self.assertFalse(first.mutation_completed, first.to_dict())
            self.assertEqual(first.next_action.action_id, "resume_landing_abort")
            transaction = repo.transaction()
            self.assertIsNotNone(transaction)
            assert transaction is not None and transaction.abort_data is not None
            self.assertTrue(transaction.abort_cleanup_complete)
            self.assertFalse(transaction.abort_runtime_finalized)
            candidate = transaction.abort_data["landed_commit"]
            self.assertIsInstance(candidate, str)
            _run_git(repo.root, "merge", "--ff-only", str(candidate))

            second = repo.close_attempt()
            self.assertEqual(second.operation_status, "succeeded", second.to_dict())
            self.assertTrue(second.mutation_started, second.to_dict())
            self.assertTrue(second.mutation_completed, second.to_dict())
            transaction = repo.transaction()
            self.assertIsNotNone(transaction)
            assert transaction is not None
            self.assertTrue(transaction.abort_complete)
            self.assertFalse(transaction.abort_superseded)
            self.assertEqual(repo.latest_attempt().status, "blocked")
            self.assertTrue(repo.worktree.exists())
            before = repo.snapshot()

            third = repo.close_attempt()
            self.assertEqual(third.operation_status, "succeeded", third.to_dict())
            self.assertFalse(third.mutation_started, third.to_dict())
            self.assertFalse(third.mutation_completed, third.to_dict())
            self.assertEqual(repo.snapshot(), before)

    def test_nonterminal_transaction_guards_all_competing_mutators(self) -> None:
        with landing_repo(suffix="nonterminal-guards") as repo:
            transaction = self._canonical_partial(repo)
            self._interrupt_abort_after_request(repo)
            transaction = repo.transaction()
            self.assertIsNotNone(transaction)
            assert transaction is not None and transaction.abort_data is not None
            self.assertFalse(transaction.terminal)
            candidate = transaction.abort_data["landed_commit"]
            self.assertIsInstance(candidate, str)
            before_events = repo.profile.paths.events_file.read_bytes()
            before_runtime = repo.profile.paths.runtime_file.read_bytes()
            before_main = _git_output(repo.root, "rev-parse", "main")

            guarded = (
                wtam.cancel_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    actor=repo.actor,
                    summary="must not cancel",
                ),
                wtam.reopen_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    actor=repo.actor,
                    summary="must not reopen",
                ),
                wtam.begin_task_worktree(
                    repo.profile,
                    actor=repo.actor,
                    prompt="Exercise nonterminal-guards landing.",
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    cwd=repo.root,
                ),
            )
            self.assertEqual(
                tuple(result.operation for result in guarded),
                ("task.cancel", "task.reopen", "task.begin"),
            )
            for result in guarded:
                self.assertEqual(result.operation_status, "blocked", result.to_dict())
                self.assertFalse(result.mutation_started)
                self.assertFalse(result.mutation_completed)
                self.assertEqual(result.mutation_phase, "none")
                self.assertEqual(result.next_action.action_id, "resume_landing_abort")
                self.assertIsNotNone(result.next_action.action)

            cleanup = wtam.cleanup_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
            )
            self.assertEqual(cleanup.operation_status, "blocked", cleanup.to_dict())
            self.assertFalse(cleanup.mutation_started)
            self.assertEqual(cleanup.next_action.action_id, "resume_landing_abort")

            reconcile = wtam.reconcile_task_landing(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=repo.attempt.attempt_id,
                landed_commit=str(candidate),
                actor=repo.actor,
                apply=True,
                reason="must not bypass native transaction",
            )
            self.assertEqual(reconcile.operation_status, "blocked", reconcile.to_dict())
            self.assertFalse(reconcile.mutation_started)
            self.assertEqual(reconcile.next_action.action_id, "resume_landing_abort")

            self.assertEqual(repo.profile.paths.events_file.read_bytes(), before_events)
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), before_runtime)
            self.assertEqual(_git_output(repo.root, "rev-parse", "main"), before_main)

    def test_abort_event_only_retry_reports_mutation_truth(self) -> None:
        with landing_repo(suffix="abort-event-only") as repo:
            self._canonical_partial(repo)
            with patch.object(
                wtam,
                "_append_landing_abort_close_event_once",
                side_effect=OSError("fault before abort close event"),
            ):
                first = repo.close_attempt()
            self.assertEqual(first.operation_status, "blocked", first.to_dict())
            transaction = repo.transaction()
            self.assertIsNotNone(transaction)
            assert transaction is not None
            self.assertTrue(transaction.abort_runtime_finalized)
            self.assertFalse(transaction.abort_close_event_recorded)
            before_runtime = repo.profile.paths.runtime_file.read_bytes()
            before_events = repo.profile.paths.events_file.read_bytes()

            second = repo.close_attempt()
            self.assertEqual(second.operation_status, "succeeded", second.to_dict())
            self.assertTrue(second.mutation_started, second.to_dict())
            self.assertTrue(second.mutation_completed, second.to_dict())
            self.assertEqual(second.mutation_phase, "landing_abort_complete")
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), before_runtime)
            self.assertNotEqual(repo.profile.paths.events_file.read_bytes(), before_events)
            before = repo.snapshot()

            third = repo.close_attempt()
            self.assertEqual(third.operation_status, "succeeded", third.to_dict())
            self.assertFalse(third.mutation_started, third.to_dict())
            self.assertFalse(third.mutation_completed, third.to_dict())
            self.assertEqual(repo.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
