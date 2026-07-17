from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import threading
from typing import Any, Callable, Iterator
import unittest
from unittest.mock import patch

import blackdog.closing as closing
import blackdog.wtam as wtam
import blackdog_cli.main as cli
import blackdog_core.backlog as backlog
import blackdog_core.state as state
from blackdog_core.backlog import start_task
from blackdog_core.state import (
    ValidationRecord,
    append_event,
    create_prompt_receipt,
    load_events,
    load_runtime_state,
)
from tests.test_landing_transaction_faults import LandingRepo, _git_output, _run_git


BOUNDARIES = (
    "close_request",
    "core_request",
    "core_decision",
    "runtime",
    "task_release",
    "workset_release",
    "task_finish",
    "worktree_removal",
    "branch_deletion",
    "cleanup_event",
    "close_event",
)
SURFACES = ("task_close", "terminal_task_land")
EXPECTED_STAGES = {
    ("close_request", False): "conflict",
    ("close_request", True): "not_started",
    ("core_request", False): "not_started",
    ("core_request", True): "request_recorded",
    ("core_decision", False): "request_recorded",
    ("core_decision", True): "decision_recorded",
    ("runtime", False): "decision_recorded",
    ("runtime", True): "runtime_finalized",
    ("task_release", False): "runtime_finalized",
    ("task_release", True): "task_release_recorded",
    ("workset_release", False): "task_release_recorded",
    ("workset_release", True): "workset_release_recorded",
    ("task_finish", False): "workset_release_recorded",
    ("task_finish", True): "cleanup_pending",
    ("worktree_removal", False): "cleanup_pending",
    ("worktree_removal", True): "cleanup_pending",
    ("branch_deletion", False): "cleanup_pending",
    ("branch_deletion", True): "cleanup_pending",
    ("cleanup_event", False): "cleanup_pending",
    ("cleanup_event", True): "cleanup_finalized",
    ("close_event", False): "cleanup_finalized",
    ("close_event", True): "complete",
}
EXPECTED_PHASES = {
    "conflict": "preflight",
    "not_started": "close_request_recorded",
    "request_recorded": "close_core_request_recorded",
    "decision_recorded": "close_core_decision_recorded",
    "runtime_finalized": "close_runtime_finalized",
    "task_release_recorded": "close_task_release_recorded",
    "workset_release_recorded": "close_workset_release_recorded",
    "cleanup_pending": "close_cleanup_pending",
    "cleanup_finalized": "close_cleanup_finalized",
    "complete": "close_complete",
}


@dataclass(frozen=True)
class CloseSnapshot:
    planning: bytes
    runtime: bytes
    events: bytes
    main: str
    source_ref: str | None
    worktree_rows: str
    source_exists: bool


@dataclass(frozen=True)
class GitSourceSnapshot:
    main: str
    source_ref: str | None
    worktree_rows: str
    source_exists: bool


def _snapshot(repo: LandingRepo) -> CloseSnapshot:
    return CloseSnapshot(
        planning=repo.profile.paths.planning_file.read_bytes(),
        runtime=repo.profile.paths.runtime_file.read_bytes(),
        events=repo.profile.paths.events_file.read_bytes(),
        main=_git_output(repo.root, "rev-parse", "main"),
        source_ref=repo.source_ref(),
        worktree_rows=_git_output(repo.root, "worktree", "list", "--porcelain"),
        source_exists=repo.worktree.exists(),
    )


def _git_source_snapshot(repo: LandingRepo) -> GitSourceSnapshot:
    return GitSourceSnapshot(
        main=_git_output(repo.root, "rev-parse", "main"),
        source_ref=repo.source_ref(),
        worktree_rows=_git_output(repo.root, "worktree", "list", "--porcelain"),
        source_exists=repo.worktree.exists(),
    )


def _make_clean_repo(suffix: str) -> LandingRepo:
    repo = LandingRepo(suffix=suffix)
    for candidate in repo.worktree.glob("*.txt"):
        candidate.unlink()
    if _git_output(repo.worktree, "status", "--short"):
        repo.close()
        raise AssertionError("close transaction fixture must begin clean")
    return repo


def _invoke_surface(repo: LandingRepo, surface: str):
    if surface == "task_close":
        return repo.close_attempt()
    if surface == "terminal_task_land":
        return repo.land()
    raise AssertionError(surface)


def _request_for_result(repo: LandingRepo, result: Any) -> closing.CloseRequest | None:
    request_id = str(result.legacy_payload.get("close_request_id") or "")
    return closing.load_close_request_by_id(repo.profile, request_id) if request_id else None


def _replay_request(repo: LandingRepo, request: closing.CloseRequest):
    return wtam.close_task(
        repo.profile,
        workset_id=request.workset_id,
        task_id=request.task_id,
        actor=request.actor,
        status=request.status,
        summary=request.summary,
        validations=tuple(
            ValidationRecord(name=name, status=status)
            for name, status in request.validations
        ),
        residuals=request.residuals,
        followup_candidates=request.followup_candidates,
        note=request.note,
        cleanup=request.cleanup_requested,
        failure_class=request.failure_class,
        recovery_action=request.recovery_action,
        prompt_issue=request.prompt_issue,
        operator_issue=request.operator_issue,
        close_request_id=request.request_event_id,
    )


def _option_values(argv: tuple[str, ...], name: str) -> tuple[str, ...]:
    prefix = f"--{name}="
    return tuple(item[len(prefix) :] for item in argv if item.startswith(prefix))


def _replay_action(repo: LandingRepo, argv: tuple[str, ...]):
    def one(name: str) -> str | None:
        values = _option_values(argv, name)
        if len(values) > 1:
            raise AssertionError(f"duplicate --{name}")
        return values[0] if values else None

    validations = []
    for raw in _option_values(argv, "validation"):
        validation_name, separator, status = raw.partition("=")
        if not separator:
            raise AssertionError(f"invalid validation argument: {raw!r}")
        validations.append(ValidationRecord(name=validation_name, status=status))
    return wtam.close_task(
        repo.profile,
        workset_id=one("workset"),
        task_id=one("task"),
        actor=one("actor"),
        status=str(one("status")),
        summary=str(one("summary")),
        validations=tuple(validations),
        residuals=_option_values(argv, "residual"),
        followup_candidates=_option_values(argv, "followup"),
        note=one("note"),
        cleanup="--cleanup" in argv,
        failure_class=one("failure-class"),
        recovery_action=one("recovery-action"),
        prompt_issue="--prompt-issue" in argv,
        operator_issue="--operator-issue" in argv,
        close_request_id=one("close-request"),
    )


def _durable_request_before_core(
    repo: LandingRepo,
    *,
    surface: str = "task_close",
) -> tuple[Any, closing.CloseRequest]:
    with _inject_fault(repo, boundary="core_request", after=False) as probe:
        first = _invoke_surface(repo, surface)
    if not probe["tripped"]:
        raise AssertionError("core request fault did not trip")
    request = _request_for_result(repo, first)
    if request is None:
        raise AssertionError(f"durable close request missing: {first.to_dict()!r}")
    return first, request


def _start_successor(repo: LandingRepo, *, suffix: str):
    runtime = load_runtime_state(repo.profile.paths)
    predecessor = next(
        row
        for workset in runtime.worksets
        if workset.workset_id == repo.workset_id
        for row in reversed(workset.attempts)
        if row.task_id == repo.task_id
    )
    if (
        predecessor.status == "in_progress"
        or predecessor.ended_at is None
        or predecessor.prompt_receipt is None
        or predecessor.user_prompt_receipt is None
    ):
        raise AssertionError("successor fixture requires a terminal predecessor with prompt lineage")
    execution_receipt = predecessor.prompt_receipt
    request_receipt = predecessor.user_prompt_receipt
    attempt_id = backlog.task_resume_attempt_id(
        workset_id=repo.workset_id,
        task_id=repo.task_id,
        predecessor_attempt_id=predecessor.attempt_id,
        actor=repo.actor,
        execution_prompt_hash=execution_receipt.prompt_hash,
        execution_prompt_mode=execution_receipt.mode,
        request_prompt_hash=request_receipt.prompt_hash,
        request_prompt_mode=request_receipt.mode,
    )
    branch = f"{repo.branch}-{suffix}"
    worktree = repo.base / "worktrees" / suffix
    _run_git(repo.root, "worktree", "add", "-b", branch, str(worktree), "main")
    attempt = start_task(
        repo.profile,
        workset_id=repo.workset_id,
        task_id=repo.task_id,
        actor=repo.actor,
        workspace_identity=f"successor-{suffix}",
        workspace_mode="git-worktree",
        worktree_role="task",
        worktree_path=str(worktree),
        branch=branch,
        target_branch="main",
        integration_branch="main",
        start_commit=_git_output(repo.root, "rev-parse", "main"),
        prompt_receipt=execution_receipt,
        user_prompt_receipt=request_receipt,
        attempt_id=attempt_id,
        expected_predecessor_attempt_id=predecessor.attempt_id,
        atomic_start_kind="resume",
        expected_task_actor=repo.actor,
        expected_execution_prompt_hash=execution_receipt.prompt_hash,
        expected_execution_prompt_mode=execution_receipt.mode,
        expected_request_prompt_hash=request_receipt.prompt_hash,
        expected_request_prompt_mode=request_receipt.mode,
        expected_task_updated_at=predecessor.ended_at,
    )
    return attempt, worktree, branch


def _rewrite_event_payload(
    repo: LandingRepo,
    *,
    event_id: str,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    rows = [
        json.loads(line)
        for line in repo.profile.paths.events_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    matches = [row for row in rows if row.get("event_id") == event_id]
    if len(matches) != 1:
        raise AssertionError(f"expected one event {event_id!r}, got {len(matches)}")
    mutate(matches[0]["payload"])
    repo.profile.paths.events_file.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _rewrite_runtime_attempt(repo: LandingRepo, **updates: Any) -> None:
    payload = json.loads(repo.profile.paths.runtime_file.read_text(encoding="utf-8"))
    matches = [
        attempt
        for workset in payload["worksets"]
        if workset["id"] == repo.workset_id
        for attempt in workset["attempts"]
        if attempt["attempt_id"] == repo.attempt.attempt_id
    ]
    if len(matches) != 1:
        raise AssertionError("runtime fixture attempt is not unique")
    matches[0].update(updates)
    repo.profile.paths.runtime_file.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    load_runtime_state(repo.profile.paths)


@contextmanager
def _inject_fault(
    repo: LandingRepo,
    *,
    boundary: str,
    after: bool,
) -> Iterator[dict[str, bool]]:
    probe = {"tripped": False}

    def once(original: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if probe["tripped"]:
            return original(*args, **kwargs)
        probe["tripped"] = True
        if not after:
            raise OSError(f"fault before {boundary}")
        result = original(*args, **kwargs)
        raise OSError(f"fault after {boundary}")

    if boundary == "close_request":
        original = wtam.record_close_request

        def injected(*args: Any, **kwargs: Any) -> Any:
            return once(original, *args, **kwargs)

        with patch.object(wtam, "record_close_request", side_effect=injected):
            yield probe
        return

    core_types = {
        "core_request": "task.finalization.request",
        "core_decision": "task.finalization.decision",
        "task_release": "task.release",
        "workset_release": "workset.release",
        "task_finish": "task.finish",
    }
    if boundary in core_types:
        original = backlog.append_event_once
        target_type = core_types[boundary]

        def injected(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("event_type") == target_type and not probe["tripped"]:
                return once(original, *args, **kwargs)
            return original(*args, **kwargs)

        with patch.object(backlog, "append_event_once", side_effect=injected):
            yield probe
        return

    if boundary == "runtime":
        original = state.JsonRuntimeStore._save_unlocked

        def injected(store: Any, path: Path, runtime_state: Any) -> Any:
            if path == repo.profile.paths.runtime_file and not probe["tripped"]:
                return once(original, store, path, runtime_state)
            return original(store, path, runtime_state)

        with patch.object(
            state.JsonRuntimeStore,
            "_save_unlocked",
            autospec=True,
            side_effect=injected,
        ):
            yield probe
        return

    if boundary == "worktree_removal":
        original = wtam._run_git

        def injected(root: Path, *args: str, **kwargs: Any) -> Any:
            if args[:2] == ("worktree", "remove") and not probe["tripped"]:
                return once(original, root, *args, **kwargs)
            return original(root, *args, **kwargs)

        with patch.object(wtam, "_run_git", side_effect=injected):
            yield probe
        return

    if boundary == "branch_deletion":
        original = wtam._run_git_no_check

        def injected(root: Path, *args: str, **kwargs: Any) -> Any:
            if args and args[0] == "branch" and not probe["tripped"]:
                return once(original, root, *args, **kwargs)
            return original(root, *args, **kwargs)

        with patch.object(wtam, "_run_git_no_check", side_effect=injected):
            yield probe
        return

    if boundary == "cleanup_event":
        original = wtam.append_event_once

        def injected(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("event_type") == "worktree.cleanup" and not probe["tripped"]:
                return once(original, *args, **kwargs)
            return original(*args, **kwargs)

        with patch.object(wtam, "append_event_once", side_effect=injected):
            yield probe
        return

    if boundary == "close_event":
        original = wtam.record_close_event

        def injected(*args: Any, **kwargs: Any) -> Any:
            return once(original, *args, **kwargs)

        with patch.object(wtam, "record_close_event", side_effect=injected):
            yield probe
        return

    raise AssertionError(boundary)


class CloseTransactionFaultTests(unittest.TestCase):
    maxDiff = None

    def _assert_exact_retry_visible(
        self,
        repo: LandingRepo,
        result: Any,
        *,
        guarded: bool,
        recovery_pending: bool,
    ) -> tuple[str, ...]:
        self.assertIn(result.operation_status, {"blocked", "partial"}, result.to_dict())
        self.assertFalse(result.mutation_completed, result.to_dict())
        self.assertEqual(result.next_action.kind, "command", result.to_dict())
        self.assertEqual(
            result.next_action.action_id,
            "retry_task_close_finalization",
            result.to_dict(),
        )
        action = result.next_action.action
        self.assertIsNotNone(action)
        assert action is not None
        argv = action.argv
        request_id = result.legacy_payload.get("close_request_id")
        guards = _option_values(argv, "close-request")
        if guarded:
            self.assertIsInstance(request_id, str)
            self.assertEqual(guards, (request_id,))
            for observation_name, observation in (
                ("show", wtam.show_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                )),
                ("recover", wtam.recover_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                )),
            ):
                with self.subTest(observation=observation_name):
                    if recovery_pending:
                        self.assertEqual(
                            observation.next_action.action_id,
                            "retry_task_close_finalization",
                            observation.to_dict(),
                        )
                        self.assertIsNotNone(observation.next_action.action)
                        assert observation.next_action.action is not None
                        self.assertEqual(observation.next_action.action.argv, argv)
                    else:
                        self.assertNotEqual(
                            observation.next_action.action_id,
                            "retry_task_close_finalization",
                            observation.to_dict(),
                        )
        else:
            self.assertIsNone(request_id)
            self.assertEqual(guards, ())
            self.assertFalse(result.mutation_started, result.to_dict())
            for observation_name, observation in (
                ("show", wtam.show_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                )),
                ("recover", wtam.recover_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                )),
            ):
                with self.subTest(observation=observation_name):
                    self.assertNotEqual(
                        observation.next_action.action_id,
                        "retry_task_close_finalization",
                        observation.to_dict(),
                    )
                    self.assertTrue(observation.legacy_payload.get("active_attempt"))
        return argv

    def _assert_replay_converges_and_third_is_byte_noop(
        self,
        repo: LandingRepo,
        *,
        argv: tuple[str, ...],
    ) -> None:
        second = _replay_action(repo, argv)
        self.assertEqual(second.operation_status, "succeeded", second.to_dict())
        self.assertTrue(second.legacy_payload["close_transaction_complete"])
        before_third = _snapshot(repo)
        third = _replay_action(repo, argv)
        self.assertEqual(third.operation_status, "succeeded", third.to_dict())
        self.assertFalse(third.mutation_started, third.to_dict())
        self.assertFalse(third.mutation_completed, third.to_dict())
        self.assertEqual(third.mutation_phase, "close_complete")
        self.assertEqual(_snapshot(repo), before_third)

    def test_two_surfaces_repair_all_eleven_boundaries_before_and_after(self) -> None:
        """44 cases: two entry surfaces x eleven boundaries x before/after."""

        for surface in SURFACES:
            for boundary in BOUNDARIES:
                for after in (False, True):
                    suffix = f"close-{surface}-{boundary}-{'after' if after else 'before'}"
                    with self.subTest(surface=surface, boundary=boundary, after=after):
                        repo = _make_clean_repo(suffix.replace("_", "-").replace(".", "-"))
                        try:
                            with _inject_fault(repo, boundary=boundary, after=after) as probe:
                                first = _invoke_surface(repo, surface)
                            self.assertTrue(probe["tripped"], first.to_dict())
                            expected_stage = EXPECTED_STAGES[(boundary, after)]
                            self.assertEqual(
                                first.legacy_payload.get("close_transaction_stage"),
                                expected_stage,
                                first.to_dict(),
                            )
                            self.assertEqual(
                                first.mutation_phase,
                                EXPECTED_PHASES[expected_stage],
                                first.to_dict(),
                            )
                            guarded = boundary != "close_request" or after
                            argv = self._assert_exact_retry_visible(
                                repo,
                                first,
                                guarded=guarded,
                                recovery_pending=not (
                                    boundary == "close_event" and after
                                ),
                            )
                            self._assert_replay_converges_and_third_is_byte_noop(
                                repo,
                                argv=argv,
                            )
                        finally:
                            repo.close()

    def test_identical_concurrent_close_calls_converge_to_one_generation(self) -> None:
        repo = _make_clean_repo("close-concurrent")
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(lambda _index: repo.close_attempt(), range(2)))
            self.assertTrue(
                all(result.operation_status == "succeeded" for result in results),
                [result.to_dict() for result in results],
            )
            attempt_events = repo.events_for_attempt()
            for event_type in (
                "worktree.close.request",
                "task.finalization.request",
                "task.finalization.decision",
                "task.release",
                "workset.release",
                "task.finish",
                "worktree.cleanup",
                "worktree.close",
            ):
                self.assertEqual(
                    sum(row.get("type") == event_type for row in attempt_events),
                    1,
                    event_type,
                )
        finally:
            repo.close()

    def test_concurrent_different_semantics_choose_one_generation_and_block_loser(self) -> None:
        repo = _make_clean_repo("close-concurrent-conflict")
        try:
            barrier = threading.Barrier(2)
            local = threading.local()
            original = wtam.close_task_worktree

            def synchronized(*args: Any, **kwargs: Any):
                if not getattr(local, "entered", False):
                    local.entered = True
                    barrier.wait(timeout=10)
                return original(*args, **kwargs)

            def invoke(summary: str):
                return wtam.close_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    actor=repo.actor,
                    status="blocked",
                    summary=summary,
                    validations=(ValidationRecord(name="race", status="passed"),),
                    residuals=("retained source",),
                    followup_candidates=("inspect winner",),
                    cleanup=True,
                )

            with patch.object(
                wtam,
                "close_task_worktree",
                side_effect=synchronized,
            ), ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(
                    executor.map(invoke, ("race summary A", "race summary B"))
                )

            winners = [row for row in results if row.operation_status == "succeeded"]
            losers = [row for row in results if row.operation_status == "blocked"]
            self.assertEqual(len(winners), 1, [row.to_dict() for row in results])
            self.assertEqual(len(losers), 1, [row.to_dict() for row in results])
            loser = losers[0]
            self.assertFalse(loser.mutation_started, loser.to_dict())
            self.assertFalse(loser.mutation_completed, loser.to_dict())
            self.assertEqual(loser.next_action.kind, "blocked", loser.to_dict())
            self.assertEqual(loser.next_action.to_dict()["argv"], [])
            attempt_events = repo.events_for_attempt()
            self.assertEqual(
                sum(row.get("type") == "worktree.close.request" for row in attempt_events),
                1,
            )
            self.assertEqual(
                sum(row.get("type") == "worktree.close" for row in attempt_events),
                1,
            )
        finally:
            repo.close()

    def test_strict_close_payload_schema_rejects_bool_int_status_and_id_tamper(self) -> None:
        repo = _make_clean_repo("close-strict-request-payload")
        try:
            _first, request = _durable_request_before_core(repo)
            request_cases: dict[str, Callable[[dict[str, Any]], None]] = {
                "schema-version-bool": lambda payload: payload.__setitem__(
                    "schema_version", True
                ),
                "prompt-issue-one": lambda payload: payload.__setitem__(
                    "prompt_issue", 1
                ),
                "operator-issue-zero": lambda payload: payload.__setitem__(
                    "operator_issue", 0
                ),
                "projection-bool-int": lambda payload: payload[
                    "pre_close_projection"
                ].__setitem__("source_path_exists", 1),
                "invalid-validation-status": lambda payload: payload["validations"][
                    0
                ].__setitem__("status", "invalid"),
                "request-id": lambda payload: payload.__setitem__(
                    "close_request_id", "wrong-request-id"
                ),
                "cleanup-disposition-proof-pair": lambda payload: payload[
                    "pre_close_projection"
                ].update(
                    cleanup_disposition="retain_dirty",
                    cleanup_proof="no_ahead",
                ),
                "cleanup-eligibility-disposition": lambda payload: payload[
                    "pre_close_projection"
                ].__setitem__("cleanup_eligible", False),
            }
            for label, mutate in request_cases.items():
                with self.subTest(payload="request", case=label):
                    payload = deepcopy(request.to_dict())
                    mutate(payload)
                    with self.assertRaises(closing.CloseTransactionError):
                        closing.CloseRequest.from_dict(payload)
        finally:
            repo.close()

        repo = _make_clean_repo("close-strict-close-payload")
        try:
            completed = repo.close_attempt()
            request = _request_for_result(repo, completed)
            self.assertIsNotNone(request)
            assert request is not None
            close_row = next(
                row
                for row in repo.events_for_attempt()
                if row.get("event_id") == request.close_event_id
            )
            canonical = close_row["payload"]
            close_cases: dict[str, Callable[[dict[str, Any]], None]] = {
                "cleanup-bool-int": lambda payload: payload["cleanup"].__setitem__(
                    "retained", 0
                ),
                "core-bool-int": lambda payload: payload["core_finalization"].__setitem__(
                    "runtime_finalized", 1
                ),
                "cleanup-summary-bool-int": lambda payload: payload.__setitem__(
                    "cleanup_performed", 1
                ),
                "close-event-id": lambda payload: payload.__setitem__(
                    "close_event_id", "wrong-close-id"
                ),
                "cleanup-requested": lambda payload: payload["cleanup"].__setitem__(
                    "requested", False
                ),
                "cleanup-eligible": lambda payload: payload["cleanup"].__setitem__(
                    "eligible", False
                ),
                "cleanup-event-id": lambda payload: payload["cleanup"].__setitem__(
                    "event_id", "wrong-cleanup-id"
                ),
                "cleanup-retained": lambda payload: payload["cleanup"].__setitem__(
                    "retained", True
                ),
                "cleanup-performed": lambda payload: payload["cleanup"].__setitem__(
                    "performed", False
                ),
                "cleanup-worktree-removed": lambda payload: payload[
                    "cleanup"
                ].__setitem__("worktree_removed", False),
                "cleanup-branch-deleted": lambda payload: payload[
                    "cleanup"
                ].__setitem__("branch_deleted", False),
                "cleanup-proof": lambda payload: payload["cleanup"].__setitem__(
                    "proof", "dirty"
                ),
                "core-request-id": lambda payload: payload[
                    "core_finalization"
                ].__setitem__("request_event_id", "wrong-core-request"),
                "core-task-release-id": lambda payload: payload[
                    "core_finalization"
                ].__setitem__("task_release_event_id", "wrong-task-release"),
                "core-workset-release-id": lambda payload: payload[
                    "core_finalization"
                ].__setitem__("workset_release_event_id", "wrong-workset-release"),
                "core-task-finish-id": lambda payload: payload[
                    "core_finalization"
                ].__setitem__("task_finish_event_id", "wrong-task-finish"),
            }
            for label, mutate in close_cases.items():
                with self.subTest(payload="close", case=label):
                    payload = deepcopy(canonical)
                    mutate(payload)
                    with self.assertRaises(closing.CloseTransactionError):
                        closing.validate_close_event_payload(request, payload)

            _rewrite_event_payload(
                repo,
                event_id=request.close_event_id,
                mutate=lambda payload: payload["core_finalization"].__setitem__(
                    "task_finish_event_id", "wrong-finish-id"
                ),
            )
            before = _snapshot(repo)
            semantic_conflict = wtam.close_task(
                repo.profile,
                close_request_id=request.request_event_id,
            )
            self.assertEqual(semantic_conflict.operation_status, "blocked")
            self.assertFalse(semantic_conflict.mutation_started)
            self.assertFalse(semantic_conflict.mutation_completed)
            self.assertEqual(semantic_conflict.next_action.kind, "blocked")
            self.assertEqual(semantic_conflict.next_action.argv, ())
            self.assertEqual(_snapshot(repo), before)
            worktree_conflict = wtam.close_task_worktree(
                repo.profile,
                close_request_id=request.request_event_id,
            )
            self.assertTrue(worktree_conflict["close_transaction_blocked"])
            self.assertFalse(worktree_conflict["mutation_started"])
            self.assertEqual(worktree_conflict["next_action"]["kind"], "blocked")
            self.assertEqual(worktree_conflict["next_action"]["argv"], [])
            self.assertEqual(_snapshot(repo), before)
        finally:
            repo.close()

    def test_strict_close_ledger_rejects_envelope_and_duplicate_rows(self) -> None:
        envelope_mutations = {
            "actor": lambda row: row.__setitem__("actor", "other-actor"),
            "type": lambda row: row.__setitem__("type", "other.type"),
            "id": lambda row: row.__setitem__("event_id", "other-id"),
        }
        for label, mutate in envelope_mutations.items():
            with self.subTest(case=f"request-{label}"):
                repo = _make_clean_repo(f"close-request-envelope-{label}")
                try:
                    _first, request = _durable_request_before_core(repo)
                    rows = [
                        json.loads(line)
                        for line in repo.profile.paths.events_file.read_text(
                            encoding="utf-8"
                        ).splitlines()
                        if line.strip()
                    ]
                    row = next(
                        item for item in rows if item.get("event_id") == request.request_event_id
                    )
                    mutate(row)
                    repo.profile.paths.events_file.write_text(
                        "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
                        encoding="utf-8",
                    )
                    with self.assertRaises(closing.CloseTransactionError):
                        closing.close_requests_for_task(
                            repo.profile,
                            workset_id=repo.workset_id,
                            task_id=repo.task_id,
                        )
                finally:
                    repo.close()

        with self.subTest(case="duplicate-request"):
            repo = _make_clean_repo("close-duplicate-request")
            try:
                _first, request = _durable_request_before_core(repo)
                append_event(
                    repo.profile.paths.events_file,
                    event_id=request.request_event_id,
                    event_type=closing.CLOSE_REQUEST_EVENT_TYPE,
                    actor=request.actor,
                    payload=request.to_dict(),
                    durable=True,
                )
                with self.assertRaises(closing.CloseTransactionError):
                    closing.load_close_request_by_id(repo.profile, request.request_event_id)
            finally:
                repo.close()

        with self.subTest(case="duplicate-close"):
            repo = _make_clean_repo("close-duplicate-close")
            try:
                completed = repo.close_attempt()
                request = _request_for_result(repo, completed)
                self.assertIsNotNone(request)
                assert request is not None
                row = next(
                    item
                    for item in repo.events_for_attempt()
                    if item.get("event_id") == request.close_event_id
                )
                append_event(
                    repo.profile.paths.events_file,
                    event_id=request.close_event_id,
                    event_type=closing.CLOSE_EVENT_TYPE,
                    actor=request.actor,
                    payload=row["payload"],
                    durable=True,
                )
                with self.assertRaises(closing.CloseTransactionError):
                    closing.load_close_event(repo.profile, request)
            finally:
                repo.close()

    def test_invalid_initial_close_semantics_are_rejected_before_any_write(self) -> None:
        cases = {
            "status": {
                "status": "success",
                "failure_class": None,
                "validations": (ValidationRecord(name="valid", status="passed"),),
                "trusted": False,
            },
            "failure-class": {
                "status": "blocked",
                "failure_class": "not-a-class",
                "validations": (ValidationRecord(name="valid", status="passed"),),
                "trusted": True,
            },
            "validation-status": {
                "status": "blocked",
                "failure_class": None,
                "validations": (ValidationRecord(name="invalid", status="bogus"),),
                "trusted": False,
            },
        }
        for label, case in cases.items():
            with self.subTest(case=label):
                repo = _make_clean_repo(f"close-invalid-prewrite-{label}")
                try:
                    before = _snapshot(repo)
                    with self.assertRaises((closing.CloseTransactionError, backlog.BacklogError)):
                        wtam.close_task_worktree(
                            repo.profile,
                            workset_id=repo.workset_id,
                            task_id=repo.task_id,
                            actor=repo.actor,
                            status=str(case["status"]),
                            summary="Reject invalid close before write",
                            validations=case["validations"],
                            cleanup=True,
                            failure_class=case["failure_class"],
                            _trusted_failure_details=bool(case["trusted"]),
                        )
                    self.assertEqual(_snapshot(repo), before)
                    self.assertFalse(
                        any(
                            event.get("type") == closing.CLOSE_REQUEST_EVENT_TYPE
                            for event in repo.events_for_attempt()
                        )
                    )
                finally:
                    repo.close()

    def test_changed_guarded_semantics_conflict_without_writes(self) -> None:
        repo = _make_clean_repo("close-changed-guard")
        try:
            _first, request = _durable_request_before_core(repo)
            before = _snapshot(repo)
            result = wtam.close_task(
                repo.profile,
                workset_id=request.workset_id,
                task_id=request.task_id,
                actor=request.actor,
                status=request.status,
                summary=f"{request.summary} changed",
                validations=tuple(
                    ValidationRecord(name=name, status=status)
                    for name, status in request.validations
                ),
                residuals=request.residuals,
                followup_candidates=request.followup_candidates,
                note=request.note,
                cleanup=request.cleanup_requested,
                failure_class=request.failure_class,
                recovery_action=request.recovery_action,
                prompt_issue=request.prompt_issue,
                operator_issue=request.operator_issue,
                close_request_id=request.request_event_id,
            )
            self.assertEqual(result.operation_status, "blocked", result.to_dict())
            self.assertFalse(result.mutation_started, result.to_dict())
            self.assertEqual(result.next_action.kind, "blocked", result.to_dict())
            self.assertEqual(result.next_action.to_dict()["argv"], [])
            self.assertEqual(_snapshot(repo), before)
        finally:
            repo.close()

    def test_request_projection_drift_conflicts_before_core_mutation(self) -> None:
        def move_branch(repo: LandingRepo) -> None:
            _run_git(repo.worktree, "branch", "-m", f"{repo.branch}-moved")

        def move_path(repo: LandingRepo) -> None:
            moved = repo.worktree.with_name("moved-source")
            _run_git(repo.root, "worktree", "move", str(repo.worktree), str(moved))

        def move_head(repo: LandingRepo) -> None:
            (repo.worktree / "head-drift.txt").write_text("drift\n", encoding="utf-8")
            _run_git(repo.worktree, "add", "head-drift.txt")
            _run_git(repo.worktree, "commit", "-m", "Move close source HEAD")

        def replace_registration(repo: LandingRepo) -> None:
            _run_git(repo.root, "worktree", "remove", str(repo.worktree))
            _run_git(repo.root, "worktree", "add", "--detach", str(repo.worktree), "main")

        mutators = {
            "branch": move_branch,
            "path": move_path,
            "head": move_head,
            "registration": replace_registration,
        }
        for label, mutate in mutators.items():
            with self.subTest(drift=label):
                repo = _make_clean_repo(f"close-drift-{label}")
                try:
                    _first, request = _durable_request_before_core(repo)
                    mutate(repo)
                    before = _snapshot(repo)
                    result = _replay_request(repo, request)
                    self.assertEqual(result.operation_status, "blocked", result.to_dict())
                    self.assertFalse(result.mutation_started, result.to_dict())
                    self.assertEqual(result.next_action.kind, "blocked", result.to_dict())
                    self.assertEqual(result.next_action.to_dict()["argv"], [])
                    self.assertEqual(_snapshot(repo), before)
                    self.assertFalse(
                        any(
                            event.get("type") == "task.finalization.request"
                            for event in repo.events_for_attempt()
                        )
                    )
                finally:
                    repo.close()

    def test_cleanup_event_id_tamper_is_rejected_during_ledger_parse(self) -> None:
        repo = _make_clean_repo("close-cleanup-id-tamper")
        try:
            _first, request = _durable_request_before_core(repo)
            self.assertIsNotNone(request.cleanup_event_id)
            replacement = "a" * 64
            self.assertNotEqual(replacement, request.cleanup_event_id)
            _rewrite_event_payload(
                repo,
                event_id=request.request_event_id,
                mutate=lambda payload: payload.__setitem__("cleanup_event_id", replacement),
            )
            before = _snapshot(repo)
            with self.assertRaises(closing.CloseTransactionError):
                closing.load_close_request_by_id(repo.profile, request.request_event_id)
            self.assertEqual(_snapshot(repo), before)
        finally:
            repo.close()

    def test_wrong_type_and_orphan_v1_rows_conflict_but_legacy_close_is_compatible(self) -> None:
        with self.subTest(case="request-id-wrong-type"):
            repo = _make_clean_repo("close-request-id-collision")
            try:
                request_id = closing.close_request_event_id(
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    attempt_id=repo.attempt.attempt_id,
                )
                append_event(
                    repo.profile.paths.events_file,
                    event_id=request_id,
                    event_type="unrelated.event",
                    actor=repo.actor,
                    payload={"unrelated": True},
                    durable=True,
                )
                before = _snapshot(repo)
                with self.assertRaises(closing.CloseTransactionError):
                    closing.load_close_request(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        attempt_id=repo.attempt.attempt_id,
                    )
                self.assertEqual(_snapshot(repo), before)
            finally:
                repo.close()

        with self.subTest(case="close-id-wrong-type"):
            repo = _make_clean_repo("close-event-id-collision")
            try:
                _first, request = _durable_request_before_core(repo)
                append_event(
                    repo.profile.paths.events_file,
                    event_id=request.close_event_id,
                    event_type="unrelated.event",
                    actor=repo.actor,
                    payload={"unrelated": True},
                    durable=True,
                )
                before = _snapshot(repo)
                with self.assertRaises(closing.CloseTransactionError):
                    closing.load_close_event(repo.profile, request)
                self.assertEqual(_snapshot(repo), before)
            finally:
                repo.close()

        with self.subTest(case="orphan-v1-close"):
            repo = _make_clean_repo("close-orphan-v1")
            try:
                append_event(
                    repo.profile.paths.events_file,
                    event_type="worktree.close",
                    actor=repo.actor,
                    payload={
                        "schema_version": 1,
                        "close_request_id": "missing-request",
                    },
                    durable=True,
                )
                with self.assertRaises(closing.CloseTransactionError):
                    closing.close_requests_for_task(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                    )
            finally:
                repo.close()

        with self.subTest(case="legacy-close"):
            repo = _make_clean_repo("close-legacy-compatible")
            try:
                append_event(
                    repo.profile.paths.events_file,
                    event_type="worktree.close",
                    actor=repo.actor,
                    payload={
                        "workset_id": repo.workset_id,
                        "task_id": repo.task_id,
                        "attempt_id": repo.attempt.attempt_id,
                        "status": "blocked",
                    },
                    durable=True,
                )
                self.assertEqual(
                    closing.close_requests_for_task(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                    ),
                    (),
                )
            finally:
                repo.close()

    def test_completed_predecessor_guarded_replay_after_successor_is_full_noop(self) -> None:
        repo = _make_clean_repo("close-complete-successor")
        try:
            completed = repo.close_attempt()
            self.assertEqual(completed.operation_status, "succeeded", completed.to_dict())
            request = _request_for_result(repo, completed)
            self.assertIsNotNone(request)
            assert request is not None
            _start_successor(repo, suffix="successor-after-complete")
            before = _snapshot(repo)

            task_result = _replay_request(repo, request)
            self.assertEqual(task_result.operation_status, "succeeded", task_result.to_dict())
            self.assertFalse(task_result.mutation_started, task_result.to_dict())
            self.assertFalse(task_result.mutation_completed, task_result.to_dict())
            self.assertEqual(_snapshot(repo), before)

            worktree_result = wtam.close_task_worktree(
                repo.profile,
                workset_id=request.workset_id,
                task_id=request.task_id,
                actor=request.actor,
                status=request.status,
                summary=request.summary,
                validations=tuple(
                    ValidationRecord(name=name, status=status)
                    for name, status in request.validations
                ),
                residuals=request.residuals,
                followup_candidates=request.followup_candidates,
                note=request.note,
                cleanup=request.cleanup_requested,
                failure_class=request.failure_class,
                recovery_action=request.recovery_action,
                prompt_issue=request.prompt_issue,
                operator_issue=request.operator_issue,
                close_request_id=request.request_event_id,
            )
            self.assertTrue(worktree_result["close_transaction_complete"])
            self.assertFalse(worktree_result["mutation_started"])
            self.assertFalse(worktree_result["mutation_completed"])
            self.assertEqual(_snapshot(repo), before)
        finally:
            repo.close()

    def test_incomplete_predecessor_with_successor_conflicts_without_writes(self) -> None:
        repo = _make_clean_repo("close-incomplete-successor")
        try:
            with _inject_fault(repo, boundary="worktree_removal", after=False) as probe:
                first = repo.close_attempt()
            self.assertTrue(probe["tripped"])
            request = _request_for_result(repo, first)
            self.assertIsNotNone(request)
            assert request is not None
            _start_successor(repo, suffix="successor-before-cleanup")
            before = _snapshot(repo)
            for observation in (
                wtam.show_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                ),
                wtam.recover_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                ),
            ):
                self.assertEqual(observation.next_action.kind, "blocked", observation.to_dict())
                self.assertEqual(observation.next_action.to_dict()["argv"], [])
                self.assertFalse(observation.mutation_started, observation.to_dict())
                self.assertFalse(observation.mutation_completed, observation.to_dict())
                self.assertEqual(_snapshot(repo), before)
            result = _replay_request(repo, request)
            self.assertEqual(result.operation_status, "blocked", result.to_dict())
            self.assertFalse(result.mutation_started, result.to_dict())
            self.assertEqual(result.next_action.kind, "blocked", result.to_dict())
            self.assertEqual(result.next_action.to_dict()["argv"], [])
            self.assertEqual(_snapshot(repo), before)
        finally:
            repo.close()

    def test_incomplete_close_gates_same_task_competing_entrypoints(self) -> None:
        repo = _make_clean_repo("close-competing-gates")
        try:
            first, request = _durable_request_before_core(repo)
            expected_argv = first.next_action.action.argv
            operations = {
                "begin": lambda: wtam.begin_task_worktree(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    actor=repo.actor,
                    prompt="A competing begin must be gated.",
                    prompt_source="unit-test",
                ),
                "cancel": lambda: wtam.cancel_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    actor=repo.actor,
                    summary="A competing cancel must be gated.",
                ),
                "reopen": lambda: wtam.reopen_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    actor=repo.actor,
                    summary="A competing reopen must be gated.",
                ),
                "land": lambda: wtam.land_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    actor=repo.actor,
                    summary="A competing land must be gated.",
                    validations=(ValidationRecord(name="gate", status="passed"),),
                    cleanup=True,
                ),
                "cleanup": lambda: wtam.cleanup_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                ),
                "reconcile": lambda: wtam.reconcile_task_landing(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    attempt_id=repo.attempt.attempt_id,
                    landed_commit="0" * 40,
                    actor=repo.actor,
                    apply=True,
                    reason="A competing reconciliation must be gated.",
                ),
            }
            for operation, invoke in operations.items():
                with self.subTest(operation=operation):
                    before = _snapshot(repo)
                    result = invoke()
                    self.assertEqual(result.operation_status, "blocked", result.to_dict())
                    self.assertFalse(result.mutation_started, result.to_dict())
                    self.assertFalse(result.mutation_completed, result.to_dict())
                    self.assertEqual(
                        result.next_action.action_id,
                        "retry_task_close_finalization",
                        result.to_dict(),
                    )
                    self.assertIsNotNone(result.next_action.action)
                    assert result.next_action.action is not None
                    self.assertEqual(result.next_action.action.argv, expected_argv)
                    self.assertEqual(_snapshot(repo), before)

            loaded = closing.load_close_request_by_id(
                repo.profile,
                request.request_event_id,
            )
            self.assertEqual(loaded, request)
        finally:
            repo.close()

    def test_guard_only_task_and_worktree_close_hydrate_request_and_byte_noop(self) -> None:
        parser = cli._build_parser()
        for surface in ("task", "worktree"):
            with self.subTest(surface=surface, phase="parser"):
                parsed = parser.parse_args(
                    (
                        surface,
                        "close",
                        "--project-root=/tmp/repo",
                        "--close-request=request-id",
                    )
                )
                self.assertEqual(parsed.close_request, "request-id")
                for field in ("workset", "task", "actor", "status", "summary"):
                    self.assertIsNone(getattr(parsed, field), field)

        repo = _make_clean_repo("close-guard-only-task")
        try:
            _first, request = _durable_request_before_core(repo)
            completed = wtam.close_task(
                repo.profile,
                close_request_id=request.request_event_id,
            )
            self.assertEqual(completed.operation_status, "succeeded", completed.to_dict())
            self.assertTrue(completed.legacy_payload["close_transaction_complete"])
            before_third = _snapshot(repo)
            third = wtam.close_task(
                repo.profile,
                close_request_id=request.request_event_id,
            )
            self.assertEqual(third.operation_status, "succeeded", third.to_dict())
            self.assertFalse(third.mutation_started, third.to_dict())
            self.assertFalse(third.mutation_completed, third.to_dict())
            self.assertEqual(_snapshot(repo), before_third)
        finally:
            repo.close()

        repo = _make_clean_repo("close-guard-only-worktree")
        try:
            _first, request = _durable_request_before_core(repo)
            completed = wtam.close_task_worktree(
                repo.profile,
                close_request_id=request.request_event_id,
            )
            self.assertTrue(completed["close_transaction_complete"])
            before_third = _snapshot(repo)
            third = wtam.close_task_worktree(
                repo.profile,
                close_request_id=request.request_event_id,
            )
            self.assertTrue(third["close_transaction_complete"])
            self.assertFalse(third["mutation_started"])
            self.assertFalse(third["mutation_completed"])
            self.assertEqual(_snapshot(repo), before_third)
        finally:
            repo.close()

    def test_guarded_mixed_semantics_conflict_commandlessly_on_both_surfaces(self) -> None:
        for surface in ("task", "worktree"):
            with self.subTest(surface=surface):
                repo = _make_clean_repo(f"close-mixed-{surface}")
                try:
                    _first, request = _durable_request_before_core(repo)
                    before = _snapshot(repo)
                    if surface == "task":
                        result = wtam.close_task(
                            repo.profile,
                            close_request_id=request.request_event_id,
                            summary=f"{request.summary} changed",
                        )
                        payload = result.to_dict()
                        operation_status = result.operation_status
                        mutation_started = result.mutation_started
                        next_action = result.next_action.to_dict()
                    else:
                        payload = wtam.close_task_worktree(
                            repo.profile,
                            close_request_id=request.request_event_id,
                            summary=f"{request.summary} changed",
                        )
                        operation_status = (
                            "blocked" if payload.get("close_transaction_blocked") else "succeeded"
                        )
                        mutation_started = bool(payload.get("mutation_started"))
                        next_action = payload["next_action"]
                    self.assertEqual(operation_status, "blocked", payload)
                    self.assertFalse(mutation_started, payload)
                    self.assertEqual(next_action["kind"], "blocked", payload)
                    self.assertEqual(next_action["argv"], [], payload)
                    self.assertEqual(_snapshot(repo), before)
                finally:
                    repo.close()

    def test_cleanup_retention_matrix_completes_without_git_mutation(self) -> None:
        def dirty(repo: LandingRepo) -> None:
            # LandingRepo deliberately begins with one uncommitted file.
            self.assertTrue(_git_output(repo.worktree, "status", "--short"))

        def unlanded_ahead(repo: LandingRepo) -> None:
            _run_git(repo.worktree, "add", ".")
            _run_git(repo.worktree, "commit", "-m", "Unlanded close fixture")

        def primary_path(repo: LandingRepo) -> None:
            for candidate in repo.worktree.glob("*.txt"):
                candidate.unlink()
            _run_git(repo.root, "worktree", "remove", str(repo.worktree))
            _run_git(repo.root, "branch", "-D", repo.branch)
            _rewrite_runtime_attempt(
                repo,
                worktree_path=str(repo.root),
                branch="main",
                target_branch="main",
                integration_branch="main",
            )

        def detached(repo: LandingRepo) -> None:
            for candidate in repo.worktree.glob("*.txt"):
                candidate.unlink()
            _run_git(repo.root, "worktree", "remove", str(repo.worktree))
            _run_git(repo.root, "worktree", "add", "--detach", str(repo.worktree), "main")

        def alternate_registration(repo: LandingRepo) -> None:
            for candidate in repo.worktree.glob("*.txt"):
                candidate.unlink()
            _run_git(repo.root, "worktree", "remove", str(repo.worktree))
            _run_git(
                repo.root,
                "worktree",
                "add",
                "-b",
                f"{repo.branch}-alternate",
                str(repo.worktree),
                "main",
            )

        cases = {
            "dirty": (dirty, "retain_dirty", "dirty"),
            "unlanded_ahead": (unlanded_ahead, "retain_unlanded", "unproven"),
            "primary_path": (primary_path, "retain_unproven", "source_identity_unproven"),
            "detached": (detached, "retain_unproven", "source_identity_unproven"),
            "alternate_registration": (
                alternate_registration,
                "retain_unproven",
                "source_identity_unproven",
            ),
        }
        for label, (prepare, disposition, proof) in cases.items():
            surfaces = ("task", "worktree") if label in {"dirty", "primary_path"} else ("task",)
            for surface in surfaces:
                with self.subTest(case=label, surface=surface):
                    repo = LandingRepo(
                        suffix=f"close-retain-{label.replace('_', '-')}-{surface}"
                    )
                try:
                    prepare(repo)
                    before_git = _git_source_snapshot(repo)
                    if surface == "task":
                        result = repo.close_attempt()
                        self.assertEqual(
                            result.operation_status,
                            "partial",
                            result.to_dict(),
                        )
                        self.assertTrue(result.mutation_started, result.to_dict())
                        self.assertFalse(result.mutation_completed, result.to_dict())
                        self.assertEqual(
                            result.mutation_phase,
                            "runtime_finalized_cleanup_pending",
                            result.to_dict(),
                        )
                        payload = result.legacy_payload
                    else:
                        payload = wtam.close_task_worktree(
                            repo.profile,
                            workset_id=repo.workset_id,
                            task_id=repo.task_id,
                            actor=repo.actor,
                            status="blocked",
                            summary="block the interrupted landing",
                            validations=(
                                ValidationRecord(name="abort", status="passed"),
                            ),
                            residuals=("retained source",),
                            followup_candidates=("retry from retained source",),
                            cleanup=True,
                        )
                        self.assertEqual(payload["operation_status"], "partial", payload)
                        self.assertTrue(payload["mutation_started"], payload)
                        self.assertFalse(payload["mutation_completed"], payload)
                        self.assertEqual(
                            payload["mutation_phase"],
                            "runtime_finalized_cleanup_pending",
                            payload,
                        )
                    self.assertTrue(payload["close_transaction_complete"], payload)
                    self.assertFalse(payload["cleanup_performed"], payload)
                    self.assertIsNone(payload["cleanup_event_id"], payload)
                    self.assertTrue(payload["cleanup"]["retained"], payload)
                    self.assertFalse(payload["cleanup"]["performed"], payload)
                    self.assertEqual(payload["cleanup"]["proof"], proof, payload)
                    request = closing.load_close_request_by_id(
                        repo.profile,
                        str(payload["close_request_id"]),
                    )
                    self.assertIsNotNone(request)
                    assert request is not None
                    self.assertEqual(
                        request.pre_close_projection["cleanup_disposition"],
                        disposition,
                    )
                    self.assertEqual(_git_source_snapshot(repo), before_git)
                    self.assertEqual(
                        sum(
                            event.get("type") == "worktree.close"
                            for event in repo.events_for_attempt()
                        ),
                        1,
                    )
                    self.assertFalse(
                        any(
                            event.get("type") == "worktree.cleanup"
                            for event in repo.events_for_attempt()
                        )
                    )
                finally:
                    repo.close()

    def test_terminal_land_with_dirty_retained_cleanup_reports_partial(self) -> None:
        repo = LandingRepo(suffix="terminal-land-retained-dirty")
        try:
            with patch.object(
                wtam,
                "_build_landing_intent",
                side_effect=wtam.NoChangesToLandError(
                    branch=repo.branch,
                    target_branch="main",
                ),
            ):
                result = repo.land()

            payload = result.to_dict()
            self.assertEqual(result.operation_status, "partial", payload)
            self.assertTrue(result.mutation_started, payload)
            self.assertFalse(result.mutation_completed, payload)
            self.assertEqual(
                result.mutation_phase,
                "runtime_finalized_cleanup_pending",
                payload,
            )
            self.assertEqual(result["land_failure_disposition"], "closed", payload)
            self.assertTrue(result["close_transaction_complete"], payload)
            self.assertTrue(result["cleanup"]["retained"], payload)
            self.assertEqual(result["cleanup"]["proof"], "dirty", payload)
            self.assertNotEqual(
                result.next_action.action_id,
                "retry_task_close_finalization",
                payload,
            )
            rendered = wtam.render_land_text(result, surface="task")
            self.assertIn("[blackdog-task] operation status: partial", rendered)
            self.assertIn(
                "completed=no phase=runtime_finalized_cleanup_pending",
                rendered,
            )
            self.assertEqual(json.loads(json.dumps(payload))["operation_status"], "partial")

            request = closing.load_close_request_by_id(
                repo.profile,
                str(result["close_request_id"]),
            )
            self.assertIsNotNone(request)
            assert request is not None
            before_retry = _snapshot(repo)
            retried = _replay_request(repo, request)
            self.assertEqual(retried.operation_status, "partial", retried.to_dict())
            self.assertFalse(retried.mutation_started, retried.to_dict())
            self.assertFalse(retried.mutation_completed, retried.to_dict())
            self.assertEqual(_snapshot(repo), before_retry)
        finally:
            repo.close()

    def test_retained_source_drift_after_core_only_repairs_close_receipt(self) -> None:
        repo = LandingRepo(suffix="close-retained-post-core-drift")
        try:
            with _inject_fault(repo, boundary="close_event", after=False) as probe:
                first = repo.close_attempt()
            self.assertTrue(probe["tripped"])
            request = _request_for_result(repo, first)
            self.assertIsNotNone(request)
            assert request is not None
            self.assertIsNone(request.cleanup_event_id)
            self.assertEqual(
                request.pre_close_projection["cleanup_disposition"],
                "retain_dirty",
            )
            (repo.worktree / "post-core-drift.txt").write_text("later drift\n", encoding="utf-8")
            _run_git(repo.worktree, "add", "post-core-drift.txt")
            _run_git(repo.worktree, "commit", "-m", "Drift retained source after core")

            before = _snapshot(repo)
            before_rows = load_events(repo.profile.paths.events_file)
            repaired = wtam.close_task(
                repo.profile,
                close_request_id=request.request_event_id,
            )
            self.assertEqual(repaired.operation_status, "partial", repaired.to_dict())
            self.assertTrue(repaired.mutation_started, repaired.to_dict())
            self.assertFalse(repaired.mutation_completed, repaired.to_dict())
            self.assertEqual(
                repaired.mutation_phase,
                "runtime_finalized_cleanup_pending",
                repaired.to_dict(),
            )
            self.assertTrue(repaired.legacy_payload["close_transaction_complete"])
            after = _snapshot(repo)
            self.assertEqual(after.planning, before.planning)
            self.assertEqual(after.runtime, before.runtime)
            self.assertEqual(after.main, before.main)
            self.assertEqual(after.source_ref, before.source_ref)
            self.assertEqual(after.worktree_rows, before.worktree_rows)
            self.assertEqual(after.source_exists, before.source_exists)
            after_rows = load_events(repo.profile.paths.events_file)
            self.assertEqual(len(after_rows), len(before_rows) + 1)
            self.assertEqual(after_rows[-1]["event_id"], request.close_event_id)
            self.assertEqual(after_rows[-1]["type"], "worktree.close")

            before_third = _snapshot(repo)
            third = wtam.close_task(
                repo.profile,
                close_request_id=request.request_event_id,
            )
            self.assertEqual(third.operation_status, "partial", third.to_dict())
            self.assertFalse(third.mutation_started, third.to_dict())
            self.assertFalse(third.mutation_completed, third.to_dict())
            self.assertEqual(
                third.mutation_phase,
                "runtime_finalized_cleanup_pending",
                third.to_dict(),
            )
            self.assertEqual(_snapshot(repo), before_third)
        finally:
            repo.close()

    def test_hidden_task_and_worktree_close_parser_arguments_are_identical(self) -> None:
        parser = cli._build_parser()
        common = (
            "--project-root=/tmp/repo",
            "--workset=ws",
            "--task=T-1",
            "--actor=codex",
            "--status=blocked",
            "--summary=retry",
            "--close-request=request-id",
            "--failure-class=unknown",
            "--recovery-action=inspect",
            "--prompt-issue",
            "--operator-issue",
            "--cleanup",
        )
        task_args = parser.parse_args(("task", "close", *common))
        worktree_args = parser.parse_args(("worktree", "close", *common))
        for field in (
            "project_root",
            "workset",
            "task",
            "actor",
            "status",
            "summary",
            "close_request",
            "failure_class",
            "recovery_action",
            "prompt_issue",
            "operator_issue",
            "cleanup",
        ):
            self.assertEqual(getattr(task_args, field), getattr(worktree_args, field), field)


if __name__ == "__main__":
    unittest.main()
