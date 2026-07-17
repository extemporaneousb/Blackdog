from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import blackdog.landing as landing
import blackdog.wtam as wtam
from blackdog.handlers import HandlerPlanSummary
from blackdog_cli.main import main as cli_main
import blackdog_core.backlog as backlog
from blackdog_core.backlog import BacklogError, start_task, upsert_workset
from blackdog_core.profile import (
    DEFAULT_WORKTREES_DIR,
    load_profile,
    render_default_profile,
)
from blackdog_core.state import (
    ATTEMPT_STATUS_IN_PROGRESS,
    ATTEMPT_STATUS_SUCCESS,
    ValidationRecord,
    create_prompt_receipt,
    load_events,
    load_runtime_state,
    save_runtime_state,
    StoreError,
)


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_output(root: Path, *args: str) -> str:
    return _run_git(root, *args).stdout.strip()


class AdoptionCompletionRepo:
    """Real Git/runtime fixture with an abort-complete retained source."""

    def __init__(
        self,
        *,
        suffix: str,
        target_drift_before_abort: bool = False,
    ) -> None:
        self._temporary = tempfile.TemporaryDirectory(
            prefix=f"blackdog-adoption-completion-{suffix}-"
        )
        self.base = Path(self._temporary.name).resolve()
        self.root = self.base / "repo"
        self.root.mkdir()
        _run_git(self.root, "init", "-b", "main")
        _run_git(self.root, "config", "user.email", "blackdog@example.com")
        _run_git(self.root, "config", "user.name", "Blackdog Test")
        (self.root / ".gitignore").write_text(".blackdog/\n", encoding="utf-8")
        default_profile = render_default_profile(
            "Workspace adoption completion tests"
        ).split("\n[[handlers]]", 1)[0]
        profile_text = ("handlers = []\n\n" + default_profile).replace(
            f'worktrees_dir = "{DEFAULT_WORKTREES_DIR}"',
            'worktrees_dir = "../worktrees"',
        )
        (self.root / "blackdog.toml").write_text(profile_text, encoding="utf-8")
        _run_git(self.root, "add", ".gitignore", "blackdog.toml")
        _run_git(self.root, "commit", "-m", "Initialize adoption fixture")

        self.profile = load_profile(self.root)
        self.workset_id = f"adoption-{suffix}"
        self.task_id = "ADOPT-1"
        self.actor = "codex"
        self.summary = f"complete {suffix} adoption"
        upsert_workset(
            self.profile,
            {
                "id": self.workset_id,
                "title": f"Workspace adoption {suffix}",
                "branch_intent": {
                    "target_branch": "main",
                    "integration_branch": "main",
                },
                "tasks": [
                    {
                        "id": self.task_id,
                        "title": f"Exercise {suffix}",
                        "intent": "prove retained-workspace adoption completion",
                    }
                ],
            },
        )
        self.execution_text = "Continue the retained implementation exactly."
        self.request_text = "Finish the original retained-workspace request."
        self.execution_path = self.base / "execution-prompt.md"
        self.request_path = self.base / "request-prompt.md"
        self.execution_path.write_text(self.execution_text + "\n", encoding="utf-8")
        self.request_path.write_text(self.request_text + "\n", encoding="utf-8")
        self.execution_receipt = create_prompt_receipt(
            self.execution_text,
            source=str(self.execution_path),
            mode="raw",
        )
        self.request_receipt = create_prompt_receipt(
            self.request_text,
            source=str(self.request_path),
            mode="raw",
        )

        self.branch = f"codex/{self.workset_id}"
        self.worktree = self.base / "worktrees" / self.task_id.lower()
        self.worktree.parent.mkdir(parents=True)
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
        self.predecessor = start_task(
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
            prompt_receipt=self.execution_receipt,
            user_prompt_receipt=self.request_receipt,
            setup_receipt={
                "schema_version": 1,
                "status": "ok",
                "handler_fixture": {"id": "none", "status": "validated"},
            },
        )
        (self.worktree / f"{suffix}.txt").write_text(
            f"retained {suffix} work\n",
            encoding="utf-8",
        )
        self.pre_abort_target_commit: str | None = None
        if target_drift_before_abort:
            _run_git(self.worktree, "add", f"{suffix}.txt")
            _run_git(
                self.worktree,
                "commit",
                "-m",
                "Commit predecessor work before target drift",
            )
            self.pre_abort_target_commit = self.advance_target(
                filename="pre-abort-target-drift.txt"
            )
            _run_git(self.worktree, "rebase", "main")
        self._abort_landing()

    def close(self) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "worktree",
                "remove",
                "--force",
                str(self.worktree),
            ],
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

    def _abort_landing(self) -> None:
        blocker = wtam.StaleTaskBranchError(
            branch=self.branch,
            target_branch="main",
            branch_worktree=self.worktree,
        )
        with patch.object(wtam, "_update_landing_target", side_effect=blocker):
            interrupted = wtam.land_task(
                self.profile,
                workset_id=self.workset_id,
                task_id=self.task_id,
                actor=self.actor,
                summary=self.summary,
                validations=(ValidationRecord(name="unit", status="passed"),),
                residuals=("none",),
                followup_candidates=("none",),
                cleanup=True,
            )
        if interrupted.operation_status != "partial":
            raise RuntimeError(interrupted.to_dict())
        closed = wtam.close_task(
            self.profile,
            workset_id=self.workset_id,
            task_id=self.task_id,
            actor=self.actor,
            status="blocked",
            summary="Retain source after the interrupted landing.",
            validations=(ValidationRecord(name="abort", status="passed"),),
            residuals=("retained source",),
            followup_candidates=("adopt retained source",),
            cleanup=True,
        )
        if closed.operation_status != "succeeded":
            raise RuntimeError(closed.to_dict())
        transaction = self.transaction()
        if transaction is None or not transaction.abort_complete:
            raise RuntimeError("fixture did not reach abort_complete")

    def transaction(self) -> landing.LandingTransaction | None:
        return landing.load_landing_transaction(
            self.profile,
            workset_id=self.workset_id,
            task_id=self.task_id,
            attempt_id=self.predecessor.attempt_id,
        )

    @property
    def candidate(self) -> str:
        transaction = self.transaction()
        assert transaction is not None and transaction.abort_data is not None
        return str(transaction.abort_data["landed_commit"])

    @property
    def source_commit(self) -> str:
        transaction = self.transaction()
        assert transaction is not None and transaction.abort_data is not None
        return str(transaction.abort_data["source_commit"])

    def adoption_kwargs(self, *, target_commit: str | None = None) -> dict[str, object]:
        transaction = self.transaction()
        assert transaction is not None
        return {
            "profile": self.profile,
            "actor": self.actor,
            "prompt": self.execution_text,
            "prompt_source": str(self.execution_path),
            "user_prompt": self.request_text,
            "user_prompt_source": str(self.request_path),
            "prompt_mode": "raw",
            "expected_actor": self.actor,
            "expected_execution_prompt_hash": self.execution_receipt.prompt_hash,
            "expected_execution_prompt_mode": self.execution_receipt.mode,
            "expected_request_prompt_hash": self.request_receipt.prompt_hash,
            "expected_request_prompt_mode": self.request_receipt.mode,
            "adopt_aborted_landing_source": True,
            "expected_predecessor_attempt": self.predecessor.attempt_id,
            "expected_landing_transaction": transaction.transaction_id,
            "expected_source_commit": self.source_commit,
            "expected_source_tree": transaction.intent.expected_source_tree_hash,
            "expected_branch": self.branch,
            "expected_path": str(self.worktree),
            "expected_target_branch": "main",
            "expected_target_commit": target_commit
            or _git_output(self.root, "rev-parse", "main"),
            "workset_id": self.workset_id,
            "task_id": self.task_id,
            "cwd": self.root,
        }

    def adopt(self, **overrides: object):
        kwargs = self.adoption_kwargs()
        kwargs.update(overrides)
        return wtam.begin_task_worktree(**kwargs)

    def run_adoption_cli(self, argv: tuple[str, ...] | list[str]):
        args = list(argv)
        if args and args[0] not in {"task", "worktree"}:
            args = args[1:]
        args.append("--json")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            return_code = cli_main(args)
        payload = json.loads(stdout.getvalue()) if stdout.getvalue().strip() else None
        return return_code, payload, stderr.getvalue()

    def attempts(self):
        state = load_runtime_state(self.profile.paths)
        return tuple(
            attempt
            for workset in state.worksets
            if workset.workset_id == self.workset_id
            for attempt in workset.attempts
            if attempt.task_id == self.task_id
        )

    def successor(self):
        attempts = self.attempts()
        if len(attempts) != 2:
            raise AssertionError(f"expected predecessor and successor, got {attempts!r}")
        return attempts[-1]

    def events_for_successor(self) -> list[dict[str, object]]:
        successor = self.successor()
        return [
            event
            for event in load_events(self.profile.paths.events_file)
            if event.get("payload", {}).get("attempt_id") == successor.attempt_id
            or event.get("payload", {}).get("successor_attempt_id")
            == successor.attempt_id
        ]

    def advance_target(self, *, filename: str = "target-drift.txt") -> str:
        (self.root / filename).write_text("target-only drift\n", encoding="utf-8")
        _run_git(self.root, "add", filename)
        _run_git(self.root, "commit", "-m", f"Advance target with {filename}")
        return _git_output(self.root, "rev-parse", "main")

    def place_candidate_on_target(self) -> None:
        _run_git(self.root, "reset", "--hard", self.candidate)

    def merge_candidate_on_target(self) -> str:
        _run_git(
            self.root,
            "merge",
            "--no-ff",
            self.candidate,
            "-m",
            "Merge predecessor candidate",
        )
        return _git_output(self.root, "rev-parse", "main")


@contextmanager
def adoption_repo(*, suffix: str, target_drift_before_abort: bool = False):
    repo = AdoptionCompletionRepo(
        suffix=suffix,
        target_drift_before_abort=target_drift_before_abort,
    )
    try:
        yield repo
    finally:
        repo.close()


def _replace_workset_runtime(state, *, workset_id: str, transform):
    return replace(
        state,
        worksets=tuple(
            transform(workset) if workset.workset_id == workset_id else workset
            for workset in state.worksets
        ),
    )


def _tamper_event_payload(
    events_file: Path,
    *,
    event_type: str,
    successor_attempt_id: str,
    mutate,
) -> str:
    rows = [
        json.loads(line)
        for line in events_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    matches = [
        row
        for row in rows
        if row.get("type") == event_type
        and isinstance(row.get("payload"), dict)
        and (
            row["payload"].get("successor_attempt_id") == successor_attempt_id
            or row["payload"].get("attempt_id") == successor_attempt_id
        )
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one {event_type} for {successor_attempt_id}, got {matches!r}"
        )
    mutate(matches[0]["payload"])
    events_file.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return str(matches[0]["event_id"])


class WorkspaceAdoptionCompletionTests(unittest.TestCase):
    maxDiff = None

    def test_pre_reservation_target_drift_returns_fresh_read_only_route(self) -> None:
        with adoption_repo(suffix="pre-reservation-drift") as repo:
            stale_kwargs = repo.adoption_kwargs()
            stale_target = str(stale_kwargs["expected_target_commit"])
            current_target = repo.advance_target()
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            events_before = repo.profile.paths.events_file.read_bytes()

            result = wtam.begin_task_worktree(**stale_kwargs)

            self.assertEqual(result.operation_status, "blocked", result.to_dict())
            self.assertFalse(result.mutation_started, result.to_dict())
            self.assertFalse(result.mutation_completed, result.to_dict())
            self.assertEqual(result.mutation_phase, "none")
            self.assertEqual(repo.attempts(), (repo.attempts()[0],))
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)
            self.assertEqual(result.next_action.action_id, "adopt_aborted_landing_source")
            self.assertEqual(result.next_action.kind, "command")
            assert result.next_action.action is not None
            argv = result.next_action.action.argv
            self.assertIn(f"--expected-target-commit={current_target}", argv)
            self.assertNotIn(f"--expected-target-commit={stale_target}", argv)
            self.assertIn("--adopt-aborted-landing-source", argv)

            cli_runtime_before = repo.profile.paths.runtime_file.read_bytes()
            cli_events_before = repo.profile.paths.events_file.read_bytes()
            stale_argv = [
                "task",
                "begin",
                f"--project-root={repo.root}",
                f"--workset={repo.workset_id}",
                f"--task={repo.task_id}",
                f"--actor={repo.actor}",
                f"--execution-prompt-file={repo.execution_path}",
                f"--request-file={repo.request_path}",
                "--prompt-mode=raw",
                f"--expected-actor={repo.actor}",
                f"--expected-execution-prompt-hash={repo.execution_receipt.prompt_hash}",
                "--expected-execution-prompt-mode=raw",
                f"--expected-request-prompt-hash={repo.request_receipt.prompt_hash}",
                "--expected-request-prompt-mode=raw",
                "--adopt-aborted-landing-source",
                f"--expected-predecessor-attempt={repo.predecessor.attempt_id}",
                f"--expected-landing-transaction={repo.transaction().transaction_id}",
                f"--expected-source-commit={repo.source_commit}",
                f"--expected-source-tree={repo.transaction().intent.expected_source_tree_hash}",
                f"--expected-branch={repo.branch}",
                f"--expected-path={repo.worktree}",
                "--expected-target-branch=main",
                f"--expected-target-commit={stale_target}",
            ]
            return_code, cli_payload, cli_error = repo.run_adoption_cli(stale_argv)
            self.assertEqual(return_code, 1, (cli_payload, cli_error))
            self.assertEqual(cli_error, "")
            self.assertEqual(cli_payload["task"]["operation_status"], "blocked")
            self.assertFalse(cli_payload["task"]["mutation_started"])
            fresh_cli_argv = cli_payload["task"]["next_action"]["argv"]
            self.assertIn(f"--expected-target-commit={current_target}", fresh_cli_argv)
            self.assertEqual(
                repo.profile.paths.runtime_file.read_bytes(),
                cli_runtime_before,
            )
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), cli_events_before)

            return_code, success_payload, cli_error = repo.run_adoption_cli(fresh_cli_argv)
            self.assertEqual(return_code, 0, (success_payload, cli_error))
            self.assertEqual(success_payload["task"]["operation_status"], "succeeded")
            runtime_after_success = repo.profile.paths.runtime_file.read_bytes()
            events_after_success = repo.profile.paths.events_file.read_bytes()
            return_code, retry_payload, cli_error = repo.run_adoption_cli(fresh_cli_argv)
            self.assertEqual(return_code, 0, (retry_payload, cli_error))
            self.assertFalse(retry_payload["task"]["mutation_started"])
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_after_success)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_after_success)

    def test_pre_reservation_candidate_arrival_routes_predecessor_reconcile(self) -> None:
        with adoption_repo(suffix="pre-reservation-candidate") as repo:
            stale_kwargs = repo.adoption_kwargs()
            repo.place_candidate_on_target()
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            events_before = repo.profile.paths.events_file.read_bytes()

            result = wtam.begin_task_worktree(**stale_kwargs)

            self.assertEqual(result.operation_status, "blocked", result.to_dict())
            self.assertFalse(result.mutation_started, result.to_dict())
            self.assertEqual(len(repo.attempts()), 1)
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)
            self.assertEqual(
                result.next_action.action_id,
                "verify_late_landing_reconciliation",
            )
            self.assertEqual(result.next_action.kind, "command")
            assert result.next_action.action is not None
            argv = result.next_action.action.argv
            self.assertIn(f"--attempt={repo.predecessor.attempt_id}", argv)
            self.assertIn(f"--landed-commit={repo.candidate}", argv)
            self.assertNotIn("--apply", argv)

    def test_start_faults_repair_core_and_worktree_events_then_byte_noop(self) -> None:
        with adoption_repo(suffix="start-fault-repair") as repo:
            kwargs = repo.adoption_kwargs()
            original_core_append = backlog.append_event_once
            core_tripped = False

            def fail_first_core_event(*args, **call_kwargs):
                nonlocal core_tripped
                if call_kwargs.get("event_type") == "task.claim" and not core_tripped:
                    core_tripped = True
                    raise OSError("fault after successor runtime reservation")
                return original_core_append(*args, **call_kwargs)

            with patch.object(
                backlog,
                "append_event_once",
                side_effect=fail_first_core_event,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "fault after successor runtime reservation",
                ):
                    wtam.begin_task_worktree(**kwargs)
            self.assertTrue(core_tripped)
            self.assertEqual(len(repo.attempts()), 2)
            recovery = wtam.show_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                cwd=repo.root,
            )
            self.assertEqual(
                recovery.next_action.action_id,
                "adopt_aborted_landing_source",
                recovery.to_dict(),
            )
            assert recovery.next_action.action is not None
            recovery_argv = recovery.next_action.action.argv
            self.assertIn("--adopt-aborted-landing-source", recovery_argv)
            self.assertIn(
                f"--expected-predecessor-attempt={repo.predecessor.attempt_id}",
                recovery_argv,
            )
            self.assertNotIn("rebase", recovery_argv)
            self.assertNotIn("reconcile-landing", recovery_argv)

            original_product_append = wtam.append_event_once
            worktree_tripped = False

            def fail_worktree_start(*args, **call_kwargs):
                nonlocal worktree_tripped
                if (
                    call_kwargs.get("event_type") == "worktree.start"
                    and not worktree_tripped
                ):
                    worktree_tripped = True
                    raise OSError("fault after core start evidence")
                return original_product_append(*args, **call_kwargs)

            with patch.object(
                wtam,
                "append_event_once",
                side_effect=fail_worktree_start,
            ):
                with self.assertRaisesRegex(OSError, "fault after core start evidence"):
                    wtam.begin_task_worktree(**kwargs)
            self.assertTrue(worktree_tripped)

            repaired = wtam.begin_task_worktree(**kwargs)
            self.assertEqual(repaired.operation_status, "succeeded", repaired.to_dict())
            self.assertTrue(repaired.mutation_started, repaired.to_dict())
            successor = repo.successor()
            owned = repo.events_for_successor()
            for event_type in ("task.claim", "task.start", "worktree.start"):
                self.assertEqual(
                    sum(event.get("type") == event_type for event in owned),
                    1,
                    (event_type, owned),
                )
            all_events = load_events(repo.profile.paths.events_file)
            expected_start_event_ids = {
                "workset.claim": backlog.task_start_event_id(
                    attempt_id=successor.attempt_id,
                    event_type="workset.claim",
                ),
                "task.claim": backlog.task_start_event_id(
                    attempt_id=successor.attempt_id,
                    event_type="task.claim",
                ),
                "task.start": backlog.task_start_event_id(
                    attempt_id=successor.attempt_id,
                    event_type="task.start",
                ),
                "worktree.start": wtam._workspace_adoption_start_event_id(
                    successor.attempt_id
                ),
            }
            for event_type, event_id in expected_start_event_ids.items():
                self.assertEqual(
                    sum(
                        event.get("type") == event_type
                        and event.get("event_id") == event_id
                        for event in all_events
                    ),
                    1,
                    (event_type, event_id, all_events),
                )
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            events_before = repo.profile.paths.events_file.read_bytes()

            exact_retry = wtam.begin_task_worktree(**kwargs)

            self.assertEqual(exact_retry.operation_status, "succeeded")
            self.assertFalse(exact_retry.mutation_started, exact_retry.to_dict())
            self.assertEqual(exact_retry["worktree"]["attempt_id"], successor.attempt_id)
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)

    def test_missing_start_evidence_blocks_all_terminal_mutation_routes(self) -> None:
        for route in ("reconcile", "land", "close", "cancel"):
            with self.subTest(route=route), adoption_repo(
                suffix=f"start-evidence-{route}-guard"
            ) as repo:
                kwargs = repo.adoption_kwargs()
                original_append = backlog.append_event_once
                tripped = False

                def fail_task_claim(*args, **call_kwargs):
                    nonlocal tripped
                    if call_kwargs.get("event_type") == "task.claim" and not tripped:
                        tripped = True
                        raise OSError("leave adoption start evidence incomplete")
                    return original_append(*args, **call_kwargs)

                with patch.object(
                    backlog,
                    "append_event_once",
                    side_effect=fail_task_claim,
                ):
                    with self.assertRaisesRegex(
                        OSError,
                        "leave adoption start evidence incomplete",
                    ):
                        wtam.begin_task_worktree(**kwargs)
                self.assertTrue(tripped)
                successor = repo.successor()

                def assert_repair_route() -> None:
                    shown = wtam.show_task(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        cwd=repo.root,
                    )
                    self.assertEqual(
                        shown.next_action.action_id,
                        "adopt_aborted_landing_source",
                        shown.to_dict(),
                    )
                    assert shown.next_action.action is not None
                    self.assertIn(
                        "--adopt-aborted-landing-source",
                        shown.next_action.action.argv,
                    )

                def call_route():
                    if route == "reconcile":
                        return wtam.reconcile_task_landing(
                            repo.profile,
                            workset_id=repo.workset_id,
                            task_id=repo.task_id,
                            attempt_id=successor.attempt_id,
                            landed_commit=repo.candidate,
                            actor=repo.actor,
                            apply=True,
                        )
                    if route == "land":
                        return wtam.land_task(
                            repo.profile,
                            workset_id=repo.workset_id,
                            task_id=repo.task_id,
                            actor=repo.actor,
                            summary="Must not land before adoption start evidence converges.",
                            validations=(
                                ValidationRecord(name="guard", status="passed"),
                            ),
                            cleanup=True,
                        )
                    if route == "close":
                        return wtam.close_task(
                            repo.profile,
                            workset_id=repo.workset_id,
                            task_id=repo.task_id,
                            actor=repo.actor,
                            status="blocked",
                            summary="Must not close before adoption start evidence converges.",
                            cleanup=False,
                        )
                    return wtam.cancel_task(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        actor=repo.actor,
                        summary="Must not cancel before adoption start evidence converges.",
                    )

                assert_repair_route()
                main_before = _git_output(repo.root, "rev-parse", "main")
                source_before = _git_output(repo.root, "rev-parse", repo.branch)
                status_before = _git_output(repo.worktree, "status", "--porcelain=v1")
                runtime_before = repo.profile.paths.runtime_file.read_bytes()
                events_before = repo.profile.paths.events_file.read_bytes()
                result = call_route()
                self.assertEqual(result.operation_status, "blocked", result.to_dict())
                self.assertFalse(result.mutation_started)
                self.assertFalse(result.mutation_completed)
                self.assertEqual(result.mutation_phase, "none")
                self.assertEqual(
                    result.next_action.action_id,
                    "adopt_aborted_landing_source",
                    result.to_dict(),
                )
                self.assertIsNotNone(result.next_action.action)
                self.assertIn(
                    "--adopt-aborted-landing-source",
                    result.next_action.action.argv,
                )
                self.assertEqual(_git_output(repo.root, "rev-parse", "main"), main_before)
                self.assertEqual(_git_output(repo.root, "rev-parse", repo.branch), source_before)
                self.assertEqual(
                    _git_output(repo.worktree, "status", "--porcelain=v1"),
                    status_before,
                )
                self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
                self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)
                self.assertEqual(repo.successor().status, ATTEMPT_STATUS_IN_PROGRESS)
                assert_repair_route()

                repaired = wtam.begin_task_worktree(**kwargs)
                self.assertEqual(
                    repaired.operation_status,
                    "succeeded",
                    repaired.to_dict(),
                )

    def test_adoption_retry_rejects_receipt_successor_claim_and_setup_tampering(self) -> None:
        with adoption_repo(suffix="tamper-rejection") as repo:
            repo.adopt()
            pristine = load_runtime_state(repo.profile.paths)

            def replace_attempt(state, mutate):
                return _replace_workset_runtime(
                    state,
                    workset_id=repo.workset_id,
                    transform=lambda workset: replace(
                        workset,
                        attempts=tuple(
                            mutate(attempt)
                            if attempt.attempt_id == repo.successor().attempt_id
                            else attempt
                            for attempt in workset.attempts
                        ),
                    ),
                )

            cases = []

            receipt_setup = copy.deepcopy(repo.successor().setup_receipt)
            assert receipt_setup is not None
            receipt_setup["workspace_adoption"].pop("source_tree_hash")
            cases.append(
                (
                    "receipt",
                    replace_attempt(pristine, lambda row: replace(row, setup_receipt=receipt_setup)),
                )
            )
            cases.append(
                (
                    "successor",
                    replace_attempt(pristine, lambda row: replace(row, actor="intruder")),
                )
            )
            cases.append(
                (
                    "successor-prompt-source",
                    replace_attempt(
                        pristine,
                        lambda row: replace(
                            row,
                            prompt_receipt=replace(
                                row.prompt_receipt,
                                source=str(repo.base / "tampered-prompt.md"),
                            ),
                        ),
                    ),
                )
            )
            cases.append(
                (
                    "successor-model",
                    replace_attempt(pristine, lambda row: replace(row, model="tampered-model")),
                )
            )
            cases.append(
                (
                    "successor-note",
                    replace_attempt(pristine, lambda row: replace(row, note="tampered-note")),
                )
            )
            cases.append(
                (
                    "claim",
                    _replace_workset_runtime(
                        pristine,
                        workset_id=repo.workset_id,
                        transform=lambda workset: replace(
                            workset,
                            task_claims=tuple(
                                replace(claim, actor="intruder")
                                if claim.task_id == repo.task_id
                                else claim
                                for claim in workset.task_claims
                            ),
                        ),
                    ),
                )
            )
            cases.append(
                (
                    "workset-claim",
                    _replace_workset_runtime(
                        pristine,
                        workset_id=repo.workset_id,
                        transform=lambda workset: replace(
                            workset,
                            workset_claim=replace(
                                workset.workset_claim,
                                actor="intruder",
                            ),
                        ),
                    ),
                )
            )
            cases.append(
                (
                    "task-runtime",
                    _replace_workset_runtime(
                        pristine,
                        workset_id=repo.workset_id,
                        transform=lambda workset: replace(
                            workset,
                            task_states=tuple(
                                replace(
                                    record,
                                    updated_at="2000-01-01T00:00:00+00:00",
                                    note="tampered-runtime-note",
                                )
                                if record.task_id == repo.task_id
                                else record
                                for record in workset.task_states
                            ),
                        ),
                    ),
                )
            )
            handler_setup = copy.deepcopy(repo.successor().setup_receipt)
            assert handler_setup is not None
            handler_setup["status"] = "tampered"
            cases.append(
                (
                    "setup-handler",
                    replace_attempt(pristine, lambda row: replace(row, setup_receipt=handler_setup)),
                )
            )

            predecessor_setup_state = _replace_workset_runtime(
                pristine,
                workset_id=repo.workset_id,
                transform=lambda workset: replace(
                    workset,
                    attempts=tuple(
                        replace(
                            attempt,
                            setup_receipt={
                                **dict(attempt.setup_receipt or {}),
                                "handler_fixture": "tampered",
                            },
                        )
                        if attempt.attempt_id == repo.predecessor.attempt_id
                        else attempt
                        for attempt in workset.attempts
                    ),
                ),
            )
            cases.append(("predecessor-setup", predecessor_setup_state))

            for name, tampered in cases:
                with self.subTest(case=name):
                    save_runtime_state(repo.profile.paths, tampered)
                    events_before = repo.profile.paths.events_file.read_bytes()
                    shown = wtam.show_task(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        cwd=repo.root,
                    )
                    self.assertEqual(
                        shown.next_action.action_id,
                        "workspace_adoption_proof_required",
                        shown.to_dict(),
                    )
                    if shown.next_action.action is not None:
                        self.assertNotIn("rebase", shown.next_action.action.argv)
                        self.assertNotIn("reconcile-landing", shown.next_action.action.argv)
                    with self.assertRaises(
                        (
                            BacklogError,
                            landing.LandingTransactionError,
                            StoreError,
                            wtam.WorktreeError,
                        )
                    ):
                        repo.adopt()
                    self.assertEqual(
                        repo.profile.paths.events_file.read_bytes(),
                        events_before,
                    )
                    save_runtime_state(repo.profile.paths, pristine)

            retry = repo.adopt()
            self.assertEqual(retry.operation_status, "succeeded", retry.to_dict())
            self.assertFalse(retry.mutation_started, retry.to_dict())

    def test_adoption_prompt_identity_allows_legacy_predecessor_and_verifies_artifacts(self) -> None:
        with adoption_repo(suffix="prompt-artifact-lineage") as repo:
            self.assertIsNone(repo.predecessor.prompt_receipt.replay_artifact_path)
            self.assertIsNone(repo.predecessor.user_prompt_receipt.replay_artifact_path)

            adopted = repo.adopt()
            self.assertEqual(adopted.operation_status, "succeeded", adopted.to_dict())
            successor = repo.successor()
            self.assertEqual(
                (
                    successor.prompt_receipt.prompt_hash,
                    successor.prompt_receipt.source,
                    successor.prompt_receipt.mode,
                ),
                (
                    repo.predecessor.prompt_receipt.prompt_hash,
                    repo.predecessor.prompt_receipt.source,
                    repo.predecessor.prompt_receipt.mode,
                ),
            )
            self.assertIsNotNone(successor.prompt_receipt.replay_artifact_path)
            artifact = (
                repo.profile.paths.control_dir
                / successor.prompt_receipt.replay_artifact_path
            )
            original = artifact.read_bytes()

            for failure, expected_code in (
                ("missing", "prompt_artifact_missing"),
                ("tampered", "prompt_artifact_hash_mismatch"),
            ):
                with self.subTest(failure=failure):
                    try:
                        if failure == "missing":
                            artifact.unlink()
                        else:
                            artifact.write_text("tampered replay content", encoding="utf-8")
                        runtime_before = repo.profile.paths.runtime_file.read_bytes()
                        events_before = repo.profile.paths.events_file.read_bytes()

                        shown = wtam.show_task(
                            repo.profile,
                            workset_id=repo.workset_id,
                            task_id=repo.task_id,
                            cwd=repo.root,
                        )
                        self.assertEqual(
                            shown.next_action.action_id,
                            "workspace_adoption_proof_required",
                            shown.to_dict(),
                        )
                        self.assertEqual(shown.next_action.kind, "blocked", shown.to_dict())
                        self.assertEqual(shown.next_action.argv, ())
                        self.assertEqual(
                            shown["workspace_adoption_issue_code"],
                            "active_workspace_adoption_proof_failed",
                            shown.to_dict(),
                        )
                        self.assertIn(expected_code, shown["workspace_adoption_issue_detail"])

                        blocked = wtam.land_task(
                            repo.profile,
                            workset_id=repo.workset_id,
                            task_id=repo.task_id,
                            actor=repo.actor,
                            summary=repo.summary,
                            validations=(
                                ValidationRecord(name="unit", status="passed"),
                            ),
                            cleanup=False,
                        )
                        self.assertEqual(blocked.operation_status, "blocked", blocked.to_dict())
                        self.assertFalse(blocked.mutation_started, blocked.to_dict())
                        self.assertFalse(blocked.mutation_completed, blocked.to_dict())
                        self.assertEqual(blocked.mutation_phase, "none", blocked.to_dict())
                        self.assertEqual(
                            blocked.next_action.action_id,
                            "workspace_adoption_proof_required",
                            blocked.to_dict(),
                        )
                        self.assertEqual(blocked.next_action.kind, "blocked", blocked.to_dict())
                        self.assertEqual(blocked.next_action.argv, ())
                        self.assertEqual(
                            blocked["workspace_adoption_issue_code"],
                            "active_workspace_adoption_proof_failed",
                            blocked.to_dict(),
                        )
                        self.assertIn(
                            expected_code,
                            blocked["workspace_adoption_issue_detail"],
                        )
                        self.assertEqual(
                            repo.profile.paths.runtime_file.read_bytes(),
                            runtime_before,
                        )
                        self.assertEqual(
                            repo.profile.paths.events_file.read_bytes(),
                            events_before,
                        )
                    finally:
                        artifact.write_bytes(original)
                        artifact.chmod(0o600)

    def test_later_handler_drift_does_not_override_exact_start_evidence(self) -> None:
        with adoption_repo(suffix="post-start-handler-drift") as repo:
            adopted = repo.adopt()
            self.assertEqual(adopted.operation_status, "succeeded", adopted.to_dict())
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            events_before = repo.profile.paths.events_file.read_bytes()
            later_drift = HandlerPlanSummary(
                ready=False,
                actions=(),
                remediation="simulated handler drift after exact durable start",
            )

            with patch.object(
                wtam,
                "validate_existing_worktree_handlers",
                return_value=later_drift,
            ):
                shown = wtam.show_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    cwd=repo.root,
                )

            self.assertNotEqual(
                shown.next_action.action_id,
                "workspace_adoption_proof_required",
                shown.to_dict(),
            )
            self.assertTrue(shown["active_workspace_adoption"], shown.to_dict())
            self.assertIsNone(shown["workspace_adoption_issue_code"], shown.to_dict())
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)

    def test_two_adopters_converge_and_cleanup_waiter_cannot_delete_successor(self) -> None:
        with adoption_repo(suffix="two-adopters") as repo:
            kwargs = repo.adoption_kwargs()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _index: wtam.begin_task_worktree(**kwargs), range(2)))
            self.assertEqual(len(repo.attempts()), 2)
            self.assertEqual(
                sorted(result.mutation_started for result in results),
                [False, True],
            )
            self.assertTrue(all(result.operation_status == "succeeded" for result in results))
            owned = repo.events_for_successor()
            self.assertEqual(sum(row.get("type") == "worktree.start" for row in owned), 1)

        with adoption_repo(suffix="adoption-cleanup-race") as repo:
            kwargs = repo.adoption_kwargs()
            entered = threading.Event()
            release = threading.Event()
            original_start = wtam.start_task

            def held_start(*args, **call_kwargs):
                entered.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("cleanup race did not release adoption")
                return original_start(*args, **call_kwargs)

            with patch.object(wtam, "start_task", side_effect=held_start):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    adoption_future = pool.submit(wtam.begin_task_worktree, **kwargs)
                    self.assertTrue(entered.wait(timeout=10))
                    cleanup_future = pool.submit(
                        wtam.cleanup_task_worktree,
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        path=str(repo.worktree),
                        branch=repo.branch,
                    )
                    time.sleep(0.1)
                    release.set()
                    adopted = adoption_future.result(timeout=10)
                    with self.assertRaisesRegex(
                        wtam.WorktreeError,
                        "active attempts must be landed or closed",
                    ):
                        cleanup_future.result(timeout=10)
            self.assertEqual(adopted.operation_status, "succeeded", adopted.to_dict())
            self.assertTrue(repo.worktree.exists())
            self.assertEqual(repo.successor().status, ATTEMPT_STATUS_IN_PROGRESS)

        with adoption_repo(suffix="cleanup-first-race") as repo:
            guarded_kwargs = repo.adoption_kwargs()
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            events_before = repo.profile.paths.events_file.read_bytes()
            git_before = {
                "main": _git_output(repo.root, "rev-parse", "main"),
                "branch": _git_output(repo.root, "rev-parse", repo.branch),
                "worktree_head": _git_output(repo.worktree, "rev-parse", "HEAD"),
                "worktrees": _git_output(repo.root, "worktree", "list", "--porcelain"),
                "source_status": _git_output(repo.worktree, "status", "--porcelain"),
            }
            with self.assertRaisesRegex(
                wtam.WorktreeError,
                "refusing cleanup|not proven landed",
            ):
                wtam.cleanup_task_worktree(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    path=str(repo.worktree),
                    branch=repo.branch,
                )
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)
            self.assertTrue(repo.worktree.exists())
            self.assertEqual(
                {
                    "main": _git_output(repo.root, "rev-parse", "main"),
                    "branch": _git_output(repo.root, "rev-parse", repo.branch),
                    "worktree_head": _git_output(repo.worktree, "rev-parse", "HEAD"),
                    "worktrees": _git_output(
                        repo.root,
                        "worktree",
                        "list",
                        "--porcelain",
                    ),
                    "source_status": _git_output(
                        repo.worktree,
                        "status",
                        "--porcelain",
                    ),
                },
                git_before,
            )

            adopted = wtam.begin_task_worktree(**guarded_kwargs)
            self.assertEqual(adopted.operation_status, "succeeded", adopted.to_dict())
            self.assertTrue(adopted.mutation_started, adopted.to_dict())
            self.assertEqual(len(repo.attempts()), 2)
            self.assertEqual(repo.successor().status, ATTEMPT_STATUS_IN_PROGRESS)
            self.assertTrue(repo.worktree.exists())

    def test_candidate_completion_fault_repairs_events_cleanup_and_third_noops(self) -> None:
        with adoption_repo(suffix="candidate-completion-repair") as repo:
            repo.adopt()
            successor = repo.successor()
            repo.place_candidate_on_target()
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            events_before = repo.profile.paths.events_file.read_bytes()

            dry_run = wtam.reconcile_task_landing(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=successor.attempt_id,
                landed_commit=repo.candidate,
                actor=repo.actor,
                apply=False,
                reason="Prove candidate arrival",
            )
            self.assertEqual(dry_run.operation_status, "observed", dry_run.to_dict())
            self.assertFalse(dry_run.mutation_started, dry_run.to_dict())
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)
            self.assertEqual(dry_run.next_action.action_id, "apply_adopted_successor_completion")
            assert dry_run.next_action.action is not None
            self.assertIn("--apply", dry_run.next_action.action.argv)

            original_append = wtam.append_event_once
            tripped = False

            def fail_completion_marker(*args, **call_kwargs):
                nonlocal tripped
                if (
                    call_kwargs.get("event_type") == "worktree.adoption.complete"
                    and not tripped
                ):
                    tripped = True
                    raise OSError("fault after runtime and synthetic land evidence")
                return original_append(*args, **call_kwargs)

            with patch.object(wtam, "append_event_once", side_effect=fail_completion_marker):
                with self.assertRaisesRegex(
                    OSError,
                    "fault after runtime and synthetic land evidence",
                ):
                    wtam.reconcile_task_landing(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        attempt_id=successor.attempt_id,
                        landed_commit=repo.candidate,
                        actor=repo.actor,
                        apply=True,
                        reason="Apply candidate arrival",
                    )
            self.assertTrue(tripped)
            self.assertEqual(repo.successor().status, ATTEMPT_STATUS_SUCCESS)
            self.assertTrue(repo.worktree.exists())
            repair_route = wtam.show_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                cwd=repo.root,
            )
            self.assertEqual(
                repair_route.next_action.action_id,
                "repair_adoption_completion",
                repair_route.to_dict(),
            )
            assert repair_route.next_action.action is not None
            self.assertIn("reconcile-landing", repair_route.next_action.action.argv)
            self.assertIn("--apply", repair_route.next_action.action.argv)

            repaired = wtam.reconcile_task_landing(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=successor.attempt_id,
                landed_commit=repo.candidate,
                actor=repo.actor,
                apply=True,
                reason="Apply candidate arrival",
            )
            self.assertEqual(repaired.operation_status, "succeeded", repaired.to_dict())
            self.assertFalse(repo.worktree.exists())
            owned = repo.events_for_successor()
            self.assertEqual(sum(row.get("type") == "worktree.land" for row in owned), 1)
            self.assertEqual(
                sum(row.get("type") == "worktree.adoption.complete" for row in owned),
                1,
            )
            self.assertEqual(
                sum(
                    row.get("type") == "worktree.adoption.completion.intent"
                    for row in owned
                ),
                1,
            )
            runtime_after = repo.profile.paths.runtime_file.read_bytes()
            events_after = repo.profile.paths.events_file.read_bytes()

            third = wtam.reconcile_task_landing(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=successor.attempt_id,
                landed_commit=repo.candidate,
                actor=repo.actor,
                apply=True,
                reason="Apply candidate arrival",
            )
            self.assertEqual(third.operation_status, "succeeded", third.to_dict())
            self.assertFalse(third.mutation_started, third.to_dict())
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_after)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_after)

        with adoption_repo(suffix="special-cleanup-event-fault") as repo:
            repo.adopt()
            successor = repo.successor()
            repo.place_candidate_on_target()
            original_append = wtam.append_event_once
            tripped = False

            def fail_cleanup_event(*args, **call_kwargs):
                nonlocal tripped
                if call_kwargs.get("event_type") == "worktree.cleanup" and not tripped:
                    tripped = True
                    raise OSError("fault after cleanup filesystem mutation")
                return original_append(*args, **call_kwargs)

            with patch.object(wtam, "append_event_once", side_effect=fail_cleanup_event):
                partial = wtam.reconcile_task_landing(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    attempt_id=successor.attempt_id,
                    landed_commit=repo.candidate,
                    actor=repo.actor,
                    apply=True,
                    reason="Apply candidate before cleanup event fault",
                )
            self.assertTrue(tripped)
            self.assertEqual(partial.operation_status, "partial", partial.to_dict())
            self.assertTrue(partial.mutation_started, partial.to_dict())
            self.assertFalse(partial.mutation_completed, partial.to_dict())
            self.assertFalse(repo.worktree.exists())
            branch_probe = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo.root),
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{repo.branch}",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(branch_probe.returncode, 0, branch_probe.stdout)
            owned_before_retry = repo.events_for_successor()
            self.assertEqual(
                sum(
                    row.get("type") == "worktree.adoption.completion.intent"
                    for row in owned_before_retry
                ),
                1,
            )
            self.assertEqual(
                sum(row.get("type") == "worktree.land" for row in owned_before_retry),
                1,
            )
            self.assertEqual(
                sum(
                    row.get("type") == "worktree.adoption.complete"
                    for row in owned_before_retry
                ),
                1,
            )
            self.assertEqual(
                sum(row.get("type") == "worktree.cleanup" for row in owned_before_retry),
                0,
            )

            repaired = wtam.reconcile_task_landing(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=successor.attempt_id,
                landed_commit=repo.candidate,
                actor=repo.actor,
                apply=True,
                reason="Repair cleanup event finalization",
            )
            self.assertEqual(repaired.operation_status, "succeeded", repaired.to_dict())
            owned_after_retry = repo.events_for_successor()
            for event_type in (
                "worktree.adoption.completion.intent",
                "worktree.land",
                "worktree.adoption.complete",
                "worktree.cleanup",
            ):
                self.assertEqual(
                    sum(row.get("type") == event_type for row in owned_after_retry),
                    1,
                    event_type,
                )

    def test_completion_intent_survives_runtime_fault_and_later_target_removal(self) -> None:
        with adoption_repo(suffix="completion-intent-runtime-fault") as repo:
            repo.adopt()
            successor = repo.successor()
            repo.place_candidate_on_target()
            original_finish = wtam.finish_task
            tripped = False

            def fail_after_intent(*args, **call_kwargs):
                nonlocal tripped
                if not tripped:
                    tripped = True
                    raise KeyboardInterrupt("fault after durable completion intent")
                return original_finish(*args, **call_kwargs)

            with patch.object(wtam, "finish_task", side_effect=fail_after_intent):
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "fault after durable completion intent",
                ):
                    wtam.reconcile_task_landing(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        attempt_id=successor.attempt_id,
                        landed_commit=repo.candidate,
                        actor=repo.actor,
                        apply=True,
                        reason="Bind completion before runtime",
                    )
            self.assertTrue(tripped)
            self.assertEqual(repo.successor().status, ATTEMPT_STATUS_IN_PROGRESS)
            intent_rows = [
                row
                for row in repo.events_for_successor()
                if row.get("type") == "worktree.adoption.completion.intent"
            ]
            self.assertEqual(len(intent_rows), 1, repo.events_for_successor())
            self.assertEqual(
                intent_rows[0]["payload"]["landed_commit"],
                repo.candidate,
            )
            shown = wtam.show_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                cwd=repo.root,
            )
            self.assertEqual(
                shown.next_action.action_id,
                "apply_adopted_successor_completion",
                shown.to_dict(),
            )
            assert shown.next_action.action is not None
            self.assertIn("--apply", shown.next_action.action.argv)

            _run_git(repo.root, "reset", "--hard", repo.start_commit)
            after_target_removal = wtam.show_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                cwd=repo.root,
            )
            self.assertEqual(
                after_target_removal.next_action.action_id,
                "apply_adopted_successor_completion",
                after_target_removal.to_dict(),
            )
            assert after_target_removal.next_action.action is not None
            self.assertIn("reconcile-landing", after_target_removal.next_action.action.argv)
            self.assertIn("--apply", after_target_removal.next_action.action.argv)
            self.assertNotIn("rebase", after_target_removal.next_action.action.argv)
            stores_before_competing_retry = (
                repo.profile.paths.runtime_file.read_bytes(),
                repo.profile.paths.events_file.read_bytes(),
            )
            for apply, landed_commit in (
                (False, repo.candidate),
                (True, repo.start_commit),
            ):
                with self.subTest(
                    competing_completion_apply=apply,
                    competing_completion_commit=landed_commit,
                ):
                    competing = wtam.reconcile_task_landing(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        attempt_id=successor.attempt_id,
                        landed_commit=landed_commit,
                        actor=repo.actor,
                        apply=apply,
                    )
                    self.assertEqual(
                        competing.operation_status,
                        "blocked",
                        competing.to_dict(),
                    )
                    self.assertFalse(competing.mutation_started, competing.to_dict())
                    self.assertFalse(competing.mutation_completed, competing.to_dict())
                    self.assertEqual(
                        competing.next_action.action_id,
                        "apply_adopted_successor_completion",
                        competing.to_dict(),
                    )
                    self.assertEqual(
                        (
                            repo.profile.paths.runtime_file.read_bytes(),
                            repo.profile.paths.events_file.read_bytes(),
                        ),
                        stores_before_competing_retry,
                    )
            repaired = wtam.reconcile_task_landing(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=successor.attempt_id,
                landed_commit=repo.candidate,
                actor=repo.actor,
                apply=True,
                reason="Bind completion before runtime",
            )
            self.assertEqual(repaired.operation_status, "succeeded", repaired.to_dict())
            self.assertEqual(repo.successor().status, ATTEMPT_STATUS_SUCCESS)
            owned = repo.events_for_successor()
            self.assertEqual(
                sum(
                    row.get("type") == "worktree.adoption.completion.intent"
                    for row in owned
                ),
                1,
            )
            self.assertEqual(sum(row.get("type") == "worktree.land" for row in owned), 1)
            self.assertEqual(
                sum(row.get("type") == "worktree.adoption.complete" for row in owned),
                1,
            )
            runtime_after = repo.profile.paths.runtime_file.read_bytes()
            events_after = repo.profile.paths.events_file.read_bytes()

            third = wtam.reconcile_task_landing(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=successor.attempt_id,
                landed_commit=repo.candidate,
                actor=repo.actor,
                apply=True,
                reason="Bind completion before runtime",
            )
            self.assertEqual(third.operation_status, "succeeded", third.to_dict())
            self.assertFalse(third.mutation_started, third.to_dict())
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_after)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_after)

    def test_special_completion_intent_semantic_tampering_blocks_read_only(self) -> None:
        with adoption_repo(suffix="special-completion-intent-tamper") as repo:
            repo.adopt()
            successor = repo.successor()
            repo.place_candidate_on_target()
            original_finish = wtam.finish_task
            tripped = False

            def stop_after_intent(*args, **call_kwargs):
                nonlocal tripped
                if not tripped:
                    tripped = True
                    raise KeyboardInterrupt("retain special completion intent for tampering")
                return original_finish(*args, **call_kwargs)

            with patch.object(wtam, "finish_task", side_effect=stop_after_intent):
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "retain special completion intent for tampering",
                ):
                    wtam.reconcile_task_landing(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        attempt_id=successor.attempt_id,
                        landed_commit=repo.candidate,
                        actor=repo.actor,
                        apply=True,
                    )
            self.assertTrue(tripped)
            clean_events = repo.profile.paths.events_file.read_bytes()
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            clean_intent = next(
                row["payload"]
                for row in repo.events_for_successor()
                if row.get("type") == "worktree.adoption.completion.intent"
            )
            self.assertIsNone(clean_intent["native_target_updated_commit"])
            mutations = {
                "cleanup_requested": lambda payload: payload.__setitem__(
                    "cleanup_requested",
                    not payload["cleanup_requested"],
                ),
                "changed_paths": lambda payload: payload.__setitem__(
                    "changed_paths",
                    [*payload["changed_paths"], "forged-path.txt"],
                ),
                "source_commit": lambda payload: payload.__setitem__(
                    "source_commit",
                    repo.start_commit,
                ),
                "source_tree_hash": lambda payload: payload.__setitem__(
                    "source_tree_hash",
                    "0" * 64,
                ),
                "target_commit_at_completion": lambda payload: payload.__setitem__(
                    "target_commit_at_completion",
                    repo.start_commit,
                ),
                "native_target_updated_commit": lambda payload: payload.__setitem__(
                    "native_target_updated_commit",
                    repo.candidate,
                ),
                "source_attribution": lambda payload: payload[
                    "source_attribution"
                ].__setitem__("patch_equivalent", False),
            }
            for name, mutate in mutations.items():
                with self.subTest(field=name):
                    repo.profile.paths.events_file.write_bytes(clean_events)
                    _tamper_event_payload(
                        repo.profile.paths.events_file,
                        event_type="worktree.adoption.completion.intent",
                        successor_attempt_id=successor.attempt_id,
                        mutate=mutate,
                    )
                    tampered_events = repo.profile.paths.events_file.read_bytes()
                    shown = wtam.show_task(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        cwd=repo.root,
                    )
                    self.assertEqual(
                        shown.next_action.action_id,
                        "workspace_adoption_proof_required",
                        shown.to_dict(),
                    )
                    self.assertEqual(shown.next_action.kind, "blocked", shown.to_dict())
                    self.assertEqual(shown.next_action.argv, (), shown.to_dict())
                    self.assertEqual(
                        shown["workspace_adoption_issue_code"],
                        "active_workspace_adoption_proof_failed",
                        shown.to_dict(),
                    )
                    self.assertTrue(
                        shown["workspace_adoption_issue_detail"],
                        shown.to_dict(),
                    )

                    blocked = wtam.reconcile_task_landing(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        attempt_id=successor.attempt_id,
                        landed_commit=repo.candidate,
                        actor=repo.actor,
                        apply=True,
                    )
                    self.assertEqual(blocked.operation_status, "blocked", blocked.to_dict())
                    self.assertFalse(blocked.mutation_started, blocked.to_dict())
                    self.assertFalse(blocked.mutation_completed, blocked.to_dict())
                    self.assertEqual(blocked.mutation_phase, "none", blocked.to_dict())
                    self.assertEqual(
                        blocked.next_action.action_id,
                        "workspace_adoption_proof_required",
                        blocked.to_dict(),
                    )
                    self.assertEqual(blocked.next_action.kind, "blocked", blocked.to_dict())
                    self.assertEqual(blocked.next_action.argv, (), blocked.to_dict())
                    self.assertEqual(
                        blocked["workspace_adoption_issue_code"],
                        "active_workspace_adoption_proof_failed",
                        blocked.to_dict(),
                    )
                    self.assertEqual(
                        blocked["workspace_adoption_issue_detail"],
                        shown["workspace_adoption_issue_detail"],
                        blocked.to_dict(),
                    )
                    self.assertEqual(
                        repo.profile.paths.runtime_file.read_bytes(),
                        runtime_before,
                    )
                    self.assertEqual(
                        repo.profile.paths.events_file.read_bytes(),
                        tampered_events,
                    )
                    self.assertTrue(repo.worktree.exists())
            repo.profile.paths.events_file.write_bytes(clean_events)

    def test_candidate_removed_before_completion_intent_is_read_only_blocked(self) -> None:
        with adoption_repo(suffix="candidate-removed") as repo:
            repo.adopt()
            successor = repo.successor()
            repo.place_candidate_on_target()
            dry_run = wtam.reconcile_task_landing(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=successor.attempt_id,
                landed_commit=repo.candidate,
                actor=repo.actor,
                apply=False,
            )
            self.assertEqual(dry_run.operation_status, "observed", dry_run.to_dict())
            _run_git(repo.root, "reset", "--hard", repo.start_commit)
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            events_before = repo.profile.paths.events_file.read_bytes()

            with self.assertRaisesRegex(
                wtam.WorktreeError,
                "target does not contain",
            ):
                wtam.reconcile_task_landing(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    attempt_id=successor.attempt_id,
                    landed_commit=repo.candidate,
                    actor=repo.actor,
                    apply=True,
                )
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)
            self.assertFalse(
                any(
                    row.get("type") == "worktree.adoption.completion.intent"
                    for row in repo.events_for_successor()
                )
            )

        with adoption_repo(suffix="candidate-removed-at-intent-append") as repo:
            repo.adopt()
            successor = repo.successor()
            repo.place_candidate_on_target()
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            events_before = repo.profile.paths.events_file.read_bytes()
            original_append_intent = wtam._append_workspace_adoption_completion_intent
            tripped = False

            def remove_target_before_intent_append(*args, **call_kwargs):
                nonlocal tripped
                if not tripped:
                    tripped = True
                    _run_git(repo.root, "reset", "--hard", repo.start_commit)
                return original_append_intent(*args, **call_kwargs)

            with patch.object(
                wtam,
                "_append_workspace_adoption_completion_intent",
                side_effect=remove_target_before_intent_append,
            ):
                with self.assertRaises(
                    (landing.LandingTransactionError, wtam.WorktreeError)
                ):
                    wtam.reconcile_task_landing(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        attempt_id=successor.attempt_id,
                        landed_commit=repo.candidate,
                        actor=repo.actor,
                        apply=True,
                    )
            self.assertTrue(tripped)
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)
            self.assertEqual(repo.successor().status, ATTEMPT_STATUS_IN_PROGRESS)
            self.assertTrue(repo.worktree.exists())
            owned = repo.events_for_successor()
            self.assertFalse(
                any(
                    row.get("type") == "worktree.adoption.completion.intent"
                    for row in owned
                )
            )
            self.assertFalse(any(row.get("type") == "worktree.land" for row in owned))
            self.assertFalse(
                any(row.get("type") == "worktree.adoption.complete" for row in owned)
            )
            self.assertFalse(any(row.get("type") == "worktree.cleanup" for row in owned))

    def test_candidate_after_patch_equivalent_rebase_passes_but_extra_commit_blocks(self) -> None:
        with adoption_repo(suffix="candidate-after-rebase") as repo:
            repo.adopt()
            successor = repo.successor()
            repo.advance_target(filename="concurrent-target.txt")
            recovery = wtam.inspect_task_worktree(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
            )
            self.assertEqual(recovery["workspace_adoption_relation"], "diverged")
            self.assertEqual(
                recovery["workspace_adoption_rebase_argv"],
                ["git", "-C", str(repo.worktree), "rebase", "main"],
            )
            _run_git(repo.worktree, "rebase", "main")
            rebased_head = _git_output(repo.worktree, "rev-parse", "HEAD")
            self.assertNotEqual(rebased_head, repo.source_commit)
            repo.merge_candidate_on_target()

            dry_run = wtam.reconcile_task_landing(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=successor.attempt_id,
                landed_commit=repo.candidate,
                actor=repo.actor,
                apply=False,
            )
            self.assertEqual(dry_run.operation_status, "observed", dry_run.to_dict())
            self.assertTrue(dry_run["proof"]["successor_only_work_absent"])

        with adoption_repo(suffix="extra-successor-commit") as repo:
            repo.adopt()
            successor = repo.successor()
            (repo.worktree / "successor-only.txt").write_text(
                "not present on target\n",
                encoding="utf-8",
            )
            _run_git(repo.worktree, "add", "successor-only.txt")
            _run_git(repo.worktree, "commit", "-m", "Add successor-only work")
            repo.place_candidate_on_target()
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            events_before = repo.profile.paths.events_file.read_bytes()

            with self.assertRaisesRegex(
                wtam.WorktreeError,
                "successor.*not proven present|successor-only",
            ):
                wtam.reconcile_task_landing(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    attempt_id=successor.attempt_id,
                    landed_commit=repo.candidate,
                    actor=repo.actor,
                    apply=False,
                )
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)

        with adoption_repo(suffix="contained-extra-successor-commit") as repo:
            repo.adopt()
            successor = repo.successor()
            (repo.worktree / "contained-successor-only.txt").write_text(
                "target contains this, but the special route must not claim it\n",
                encoding="utf-8",
            )
            _run_git(repo.worktree, "add", "contained-successor-only.txt")
            _run_git(repo.worktree, "commit", "-m", "Add contained successor-only work")
            successor_head = _git_output(repo.worktree, "rev-parse", "HEAD")
            repo.place_candidate_on_target()
            _run_git(
                repo.root,
                "merge",
                "--no-ff",
                successor_head,
                "-m",
                "Merge successor-only work for attribution test",
            )
            self.assertEqual(
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo.root),
                        "merge-base",
                        "--is-ancestor",
                        successor_head,
                        "main",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                ).returncode,
                0,
            )
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            events_before = repo.profile.paths.events_file.read_bytes()
            with self.assertRaisesRegex(
                wtam.WorktreeError,
                "successor|patch-equivalent|special completion",
            ):
                wtam.reconcile_task_landing(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    attempt_id=successor.attempt_id,
                    landed_commit=repo.candidate,
                    actor=repo.actor,
                    apply=False,
                )
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)

    def test_landing_target_base_excludes_pre_abort_target_drift_from_patch(self) -> None:
        with adoption_repo(
            suffix="pre-abort-target-drift-attribution",
            target_drift_before_abort=True,
        ) as repo:
            transaction = repo.transaction()
            assert transaction is not None
            self.assertNotEqual(
                repo.predecessor.start_commit,
                transaction.intent.target_base_commit,
            )
            self.assertEqual(
                transaction.intent.target_base_commit,
                repo.pre_abort_target_commit,
            )
            self.assertNotIn(
                "pre-abort-target-drift.txt",
                transaction.intent.changed_paths,
            )
            repo.adopt()
            successor = repo.successor()
            repo.advance_target(filename="post-adoption-target-drift.txt")
            _run_git(repo.worktree, "rebase", "main")
            repo.merge_candidate_on_target()
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            events_before = repo.profile.paths.events_file.read_bytes()

            dry_run = wtam.reconcile_task_landing(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=successor.attempt_id,
                landed_commit=repo.candidate,
                actor=repo.actor,
                apply=False,
            )
            self.assertEqual(dry_run.operation_status, "observed", dry_run.to_dict())
            attribution = dry_run["proof"]["source_attribution"]
            self.assertEqual(attribution["mode"], "patch_equivalent_rebase")
            self.assertEqual(
                attribution["original_range_base"],
                transaction.intent.target_base_commit,
            )
            self.assertNotEqual(
                attribution["original_range_base"],
                repo.predecessor.start_commit,
            )
            self.assertNotIn(
                "pre-abort-target-drift.txt",
                attribution["changed_paths"],
            )
            self.assertNotIn(
                "post-adoption-target-drift.txt",
                attribution["changed_paths"],
            )
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(repo.profile.paths.events_file.read_bytes(), events_before)
            self.assertEqual(repo.successor().status, ATTEMPT_STATUS_IN_PROGRESS)
            self.assertFalse(
                any(
                    row.get("type") == "worktree.adoption.completion.intent"
                    for row in repo.events_for_successor()
                )
            )

    def test_normal_successor_land_emits_marker_and_repairs_runtime_before_marker(self) -> None:
        with adoption_repo(suffix="normal-successor-land") as repo:
            repo.adopt()
            successor = repo.successor()
            successor_only = repo.worktree / "normal-successor-only.txt"
            successor_only.write_text("owned by the successor\n", encoding="utf-8")
            result = wtam.land_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                actor=repo.actor,
                summary="Land the adopted successor normally.",
                validations=(ValidationRecord(name="unit", status="passed"),),
                residuals=("none",),
                followup_candidates=("none",),
                cleanup=True,
            )
            self.assertEqual(result.operation_status, "succeeded", result.to_dict())
            owned = repo.events_for_successor()
            native_land = [row for row in owned if row.get("type") == "worktree.land"]
            completion = [
                row for row in owned if row.get("type") == "worktree.adoption.complete"
            ]
            self.assertEqual(len(native_land), 1, owned)
            self.assertEqual(len(completion), 1, owned)
            self.assertIn("normal-successor-only.txt", native_land[0]["payload"]["changed_paths"])
            self.assertEqual(
                completion[0]["payload"]["changed_paths"],
                native_land[0]["payload"]["changed_paths"],
            )
            self.assertEqual(
                completion[0]["payload"]["land_event_id"],
                native_land[0]["event_id"],
            )
            self.assertEqual(completion[0]["payload"]["completion_route"], "successor_landing")
            self.assertFalse(repo.worktree.exists())

        with adoption_repo(suffix="normal-intent-before-runtime") as repo:
            repo.adopt()
            original_finish = wtam.finish_task
            tripped = False

            def fail_before_native_runtime(*args, **call_kwargs):
                nonlocal tripped
                if not tripped:
                    tripped = True
                    raise KeyboardInterrupt("fault before native runtime finalization")
                return original_finish(*args, **call_kwargs)

            def land_before_runtime_fixture():
                return wtam.land_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    actor=repo.actor,
                    summary="Prove normal completion intent precedes native runtime.",
                    validations=(ValidationRecord(name="unit", status="passed"),),
                    residuals=("none",),
                    followup_candidates=("none",),
                    cleanup=True,
                )

            with patch.object(wtam, "finish_task", side_effect=fail_before_native_runtime):
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "fault before native runtime finalization",
                ):
                    land_before_runtime_fixture()
            self.assertTrue(tripped)
            self.assertEqual(repo.successor().status, ATTEMPT_STATUS_IN_PROGRESS)
            owned = repo.events_for_successor()
            self.assertEqual(
                sum(
                    row.get("type") == "worktree.adoption.completion.intent"
                    for row in owned
                ),
                1,
            )
            self.assertFalse(any(row.get("type") == "worktree.land" for row in owned))
            self.assertFalse(
                any(row.get("type") == "worktree.adoption.complete" for row in owned)
            )
            self.assertFalse(any(row.get("type") == "worktree.cleanup" for row in owned))
            self.assertTrue(repo.worktree.exists())

            stores_before_competing_retry = (
                repo.profile.paths.runtime_file.read_bytes(),
                repo.profile.paths.events_file.read_bytes(),
            )
            competing = wtam.land_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                actor=repo.actor,
                summary="A competing completion request must not own this transaction.",
                validations=(ValidationRecord(name="unit", status="passed"),),
                residuals=("none",),
                followup_candidates=("none",),
                cleanup=True,
            )
            self.assertEqual(competing.operation_status, "blocked", competing.to_dict())
            self.assertFalse(competing.mutation_started, competing.to_dict())
            self.assertFalse(competing.mutation_completed, competing.to_dict())
            self.assertIsNotNone(competing.next_action.action, competing.to_dict())
            assert competing.next_action.action is not None
            self.assertEqual(
                competing.next_action.action.argv[1:3],
                ("task", "land"),
                competing.to_dict(),
            )
            self.assertEqual(
                (
                    repo.profile.paths.runtime_file.read_bytes(),
                    repo.profile.paths.events_file.read_bytes(),
                ),
                stores_before_competing_retry,
            )

            repaired = land_before_runtime_fixture()
            self.assertEqual(repaired.operation_status, "succeeded", repaired.to_dict())
            owned = repo.events_for_successor()
            self.assertEqual(sum(row.get("type") == "worktree.land" for row in owned), 1)
            self.assertEqual(
                sum(row.get("type") == "worktree.adoption.complete" for row in owned),
                1,
            )
            self.assertFalse(repo.worktree.exists())

        with adoption_repo(suffix="normal-land-marker-repair") as repo:
            repo.adopt()
            successor = repo.successor()
            original_append = wtam.append_event_once
            tripped = False

            def fail_marker(*args, **call_kwargs):
                nonlocal tripped
                if (
                    call_kwargs.get("event_type") == "worktree.adoption.complete"
                    and not tripped
                ):
                    tripped = True
                    raise KeyboardInterrupt("fault before normal adoption marker")
                return original_append(*args, **call_kwargs)

            with patch.object(wtam, "append_event_once", side_effect=fail_marker):
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "fault before normal adoption marker",
                ):
                    wtam.land_task(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        actor=repo.actor,
                        summary="Land and repair the normal adoption marker.",
                        validations=(ValidationRecord(name="unit", status="passed"),),
                        residuals=("none",),
                        followup_candidates=("none",),
                        cleanup=True,
                    )
            self.assertTrue(tripped)
            self.assertEqual(repo.successor().status, ATTEMPT_STATUS_SUCCESS)
            self.assertTrue(repo.worktree.exists())
            self.assertEqual(
                sum(
                    row.get("type") == "worktree.adoption.completion.intent"
                    for row in repo.events_for_successor()
                ),
                1,
            )
            self.assertEqual(
                sum(
                    row.get("type") == "worktree.land"
                    for row in repo.events_for_successor()
                ),
                1,
            )
            self.assertFalse(
                any(
                    row.get("type") == "worktree.adoption.complete"
                    for row in repo.events_for_successor()
                )
            )
            completion_intent = next(
                row["payload"]
                for row in repo.events_for_successor()
                if row.get("type") == "worktree.adoption.completion.intent"
            )
            native_transaction = landing.load_landing_transaction(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=successor.attempt_id,
            )
            assert native_transaction is not None
            landed_target = _git_output(repo.root, "rev-parse", "main")
            self.assertEqual(
                completion_intent["native_target_updated_commit"],
                native_transaction.data_for("target_updated")["target_commit"],
            )
            self.assertEqual(
                completion_intent["target_commit_at_completion"],
                landed_target,
            )
            _run_git(repo.root, "reset", "--hard", repo.start_commit)
            self.assertNotEqual(
                _git_output(repo.root, "rev-parse", "main"),
                completion_intent["target_commit_at_completion"],
            )
            recovery = wtam.show_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                cwd=repo.root,
            )
            self.assertEqual(
                recovery.next_action.action_id,
                "repair_adoption_completion",
                recovery.to_dict(),
            )
            assert recovery.next_action.action is not None
            self.assertEqual(
                recovery.next_action.action.argv[1:3],
                ("task", "land"),
            )

            repaired = wtam.land_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                actor=repo.actor,
                summary="Land and repair the normal adoption marker.",
                validations=(ValidationRecord(name="unit", status="passed"),),
                residuals=("none",),
                followup_candidates=("none",),
                cleanup=True,
            )
            self.assertEqual(repaired.operation_status, "succeeded", repaired.to_dict())
            self.assertEqual(
                sum(
                    row.get("type") == "worktree.adoption.complete"
                    for row in repo.events_for_successor()
                ),
                1,
            )
            self.assertFalse(repo.worktree.exists())
            self.assertEqual(
                _git_output(repo.root, "rev-parse", "main"),
                repo.start_commit,
            )

    def test_normal_completion_intent_and_native_land_tampering_blocks_repair(self) -> None:
        with adoption_repo(suffix="normal-completion-intent-tamper") as repo:
            repo.adopt()
            successor = repo.successor()
            summary = "Land normally, then prove completion tamper rejection."

            def land():
                return wtam.land_task(
                    repo.profile,
                    workset_id=repo.workset_id,
                    task_id=repo.task_id,
                    actor=repo.actor,
                    summary=summary,
                    validations=(ValidationRecord(name="unit", status="passed"),),
                    residuals=("none",),
                    followup_candidates=("none",),
                    cleanup=False,
                )

            original_append = wtam.append_event_once
            tripped = False

            def stop_before_marker(*args, **call_kwargs):
                nonlocal tripped
                if (
                    call_kwargs.get("event_type") == "worktree.adoption.complete"
                    and not tripped
                ):
                    tripped = True
                    raise KeyboardInterrupt("retain normal completion intent for tampering")
                return original_append(*args, **call_kwargs)

            with patch.object(wtam, "append_event_once", side_effect=stop_before_marker):
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "retain normal completion intent for tampering",
                ):
                    land()
            self.assertTrue(tripped)
            self.assertEqual(repo.successor().status, ATTEMPT_STATUS_SUCCESS)
            clean_events = repo.profile.paths.events_file.read_bytes()
            runtime_before = repo.profile.paths.runtime_file.read_bytes()
            intent = next(
                row["payload"]
                for row in repo.events_for_successor()
                if row.get("type") == "worktree.adoption.completion.intent"
            )
            self.assertTrue(intent["native_landing_transaction_id"])
            self.assertTrue(intent["native_land_event_id"])
            native_transaction = landing.load_landing_transaction(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                attempt_id=successor.attempt_id,
            )
            assert native_transaction is not None
            self.assertEqual(
                intent["native_target_updated_commit"],
                native_transaction.data_for("target_updated")["target_commit"],
            )
            self.assertEqual(
                intent["target_commit_at_completion"],
                _git_output(repo.root, "rev-parse", "main"),
            )

            intent_mutations = {
                "cleanup_requested": lambda payload: payload.__setitem__(
                    "cleanup_requested",
                    not payload["cleanup_requested"],
                ),
                "changed_paths": lambda payload: payload.__setitem__(
                    "changed_paths",
                    [*payload["changed_paths"], "forged-native-path.txt"],
                ),
                "source_tree_hash": lambda payload: payload.__setitem__(
                    "source_tree_hash",
                    "f" * 64,
                ),
                "native_transaction": lambda payload: payload.__setitem__(
                    "native_landing_transaction_id",
                    "0" * 64,
                ),
                "native_event": lambda payload: payload.__setitem__(
                    "native_land_event_id",
                    "1" * 64,
                ),
                "native_target_updated_commit": lambda payload: payload.__setitem__(
                    "native_target_updated_commit",
                    repo.start_commit,
                ),
                "target_commit_at_completion": lambda payload: payload.__setitem__(
                    "target_commit_at_completion",
                    repo.start_commit,
                ),
                "source_attribution": lambda payload: payload[
                    "source_attribution"
                ].__setitem__("mode", "forged_valid_string"),
            }
            for name, mutate in intent_mutations.items():
                with self.subTest(intent_field=name):
                    repo.profile.paths.events_file.write_bytes(clean_events)
                    _tamper_event_payload(
                        repo.profile.paths.events_file,
                        event_type="worktree.adoption.completion.intent",
                        successor_attempt_id=successor.attempt_id,
                        mutate=mutate,
                    )
                    tampered_events = repo.profile.paths.events_file.read_bytes()
                    git_before_show = (
                        _git_output(repo.root, "rev-parse", "main"),
                        _git_output(repo.root, "rev-parse", repo.branch),
                        _git_output(repo.worktree, "rev-parse", "HEAD"),
                        _git_output(repo.root, "worktree", "list", "--porcelain"),
                        _git_output(repo.worktree, "status", "--porcelain"),
                    )
                    shown = wtam.show_task(
                        repo.profile,
                        workset_id=repo.workset_id,
                        task_id=repo.task_id,
                        cwd=repo.root,
                    )
                    self.assertEqual(
                        shown.next_action.action_id,
                        "workspace_adoption_proof_required",
                        shown.to_dict(),
                    )
                    self.assertEqual(shown.next_action.kind, "blocked", shown.to_dict())
                    self.assertEqual(
                        shown.next_action.disposition,
                        "proof_required",
                        shown.to_dict(),
                    )
                    self.assertEqual(
                        shown.next_action.reason_code,
                        "terminal_workspace_adoption_proof_failed",
                        shown.to_dict(),
                    )
                    self.assertNotEqual(
                        shown.next_action.action_id,
                        "resume_landing_transaction",
                        shown.to_dict(),
                    )
                    self.assertEqual(shown.next_action.argv, (), shown.to_dict())
                    self.assertEqual(
                        (
                            _git_output(repo.root, "rev-parse", "main"),
                            _git_output(repo.root, "rev-parse", repo.branch),
                            _git_output(repo.worktree, "rev-parse", "HEAD"),
                            _git_output(
                                repo.root,
                                "worktree",
                                "list",
                                "--porcelain",
                            ),
                            _git_output(
                                repo.worktree,
                                "status",
                                "--porcelain",
                            ),
                        ),
                        git_before_show,
                    )
                    self.assertEqual(
                        repo.profile.paths.runtime_file.read_bytes(),
                        runtime_before,
                    )
                    self.assertEqual(
                        repo.profile.paths.events_file.read_bytes(),
                        tampered_events,
                    )
                    try:
                        result = land()
                    except (
                        BacklogError,
                        landing.LandingTransactionError,
                        StoreError,
                        wtam.WorktreeError,
                    ):
                        pass
                    else:
                        self.assertNotEqual(result.operation_status, "succeeded", result.to_dict())
                        self.assertFalse(result.mutation_started, result.to_dict())
                    self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
                    self.assertEqual(repo.profile.paths.events_file.read_bytes(), tampered_events)
                    self.assertTrue(repo.worktree.exists())

            repo.profile.paths.events_file.write_bytes(clean_events)
            _tamper_event_payload(
                repo.profile.paths.events_file,
                event_type="worktree.land",
                successor_attempt_id=successor.attempt_id,
                mutate=lambda payload: payload.__setitem__(
                    "transaction_id",
                    "2" * 64,
                ),
            )
            tampered_native_event = repo.profile.paths.events_file.read_bytes()
            shown = wtam.show_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                cwd=repo.root,
            )
            self.assertEqual(
                shown.next_action.action_id,
                "workspace_adoption_proof_required",
                shown.to_dict(),
            )
            self.assertEqual(shown.next_action.kind, "blocked", shown.to_dict())
            self.assertEqual(
                shown.next_action.disposition,
                "proof_required",
                shown.to_dict(),
            )
            self.assertEqual(
                shown.next_action.reason_code,
                "terminal_workspace_adoption_proof_failed",
                shown.to_dict(),
            )
            self.assertNotEqual(
                shown.next_action.action_id,
                "resume_landing_transaction",
                shown.to_dict(),
            )
            self.assertEqual(shown.next_action.argv, (), shown.to_dict())
            try:
                result = land()
            except (
                BacklogError,
                landing.LandingTransactionError,
                StoreError,
                wtam.WorktreeError,
            ):
                pass
            else:
                self.assertNotEqual(result.operation_status, "succeeded", result.to_dict())
                self.assertFalse(result.mutation_started, result.to_dict())
            self.assertEqual(repo.profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(
                repo.profile.paths.events_file.read_bytes(),
                tampered_native_event,
            )
            self.assertTrue(repo.worktree.exists())

            repo.profile.paths.events_file.write_bytes(clean_events)
            repaired = land()
            self.assertEqual(repaired.operation_status, "succeeded", repaired.to_dict())
            self.assertEqual(
                sum(
                    row.get("type") == "worktree.adoption.complete"
                    for row in repo.events_for_successor()
                ),
                1,
            )

    def test_full_abort_adopt_rebase_normal_land_cleans_and_completes(self) -> None:
        with adoption_repo(suffix="full-normal-rebase-land") as repo:
            repo.adopt()
            successor = repo.successor()
            repo.advance_target(filename="full-flow-target.txt")
            shown = wtam.show_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                cwd=repo.root,
            )
            self.assertEqual(shown.next_action.action_id, "rebase_adopted_workspace")
            assert shown.next_action.action is not None
            self.assertEqual(
                shown.next_action.action.argv,
                ("git", "-C", str(repo.worktree), "rebase", "main"),
            )
            _run_git(repo.worktree, "rebase", "main")

            landed = wtam.land_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                actor=repo.actor,
                summary="Finish the full adopted rebase flow.",
                validations=(ValidationRecord(name="integration", status="passed"),),
                residuals=("none",),
                followup_candidates=("none",),
                cleanup=True,
            )
            self.assertEqual(landed.operation_status, "succeeded", landed.to_dict())
            self.assertEqual(repo.successor().status, ATTEMPT_STATUS_SUCCESS)
            self.assertFalse(repo.worktree.exists())
            self.assertTrue((repo.root / "full-flow-target.txt").is_file())
            self.assertTrue((repo.root / "full-normal-rebase-land.txt").is_file())
            owned = repo.events_for_successor()
            self.assertEqual(sum(row.get("type") == "worktree.land" for row in owned), 1)
            self.assertEqual(
                sum(row.get("type") == "worktree.adoption.complete" for row in owned),
                1,
            )
            final_show = wtam.show_task(
                repo.profile,
                workset_id=repo.workset_id,
                task_id=repo.task_id,
                cwd=repo.root,
            )
            self.assertEqual(final_show.next_action.kind, "complete", final_show.to_dict())


if __name__ == "__main__":
    unittest.main()
