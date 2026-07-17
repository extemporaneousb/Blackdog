from __future__ import annotations

from contextlib import chdir, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import blackdog.wtam as wtam
import blackdog_core.backlog as backlog
from blackdog.observability import read_lifecycle_observability
from blackdog.repo_lifecycle import install_repo
from blackdog_cli.main import main as blackdog_main
from blackdog_core.profile import DEFAULT_WORKTREES_DIR, load_profile, render_default_profile
from blackdog_core.state import (
    ValidationRecord,
    load_events,
    load_runtime_state,
    save_runtime_state,
    task_claim_index,
    workset_claim,
)
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class StartTransactionFaultTests(CoreAuditTestCase):
    @contextmanager
    def start_repo(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        self.init_git_repo(root)
        worktrees = root.parent / f".worktrees-{root.name}"
        profile_text = render_default_profile("Start transaction faults").replace(
            f'worktrees_dir = "{DEFAULT_WORKTREES_DIR}"',
            f'worktrees_dir = "{worktrees}"',
        )
        (root / "blackdog.toml").write_text(profile_text, encoding="utf-8")
        install_repo(root, source_root=str(REPO_ROOT))
        subprocess.run(
            ["git", "-C", str(root), "add", "blackdog.toml", "AGENTS.md", ".codex/skills"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", "Configure Blackdog"],
            check=True,
            capture_output=True,
            text=True,
        )
        prompt_file = load_profile(root).paths.control_dir / "test-inputs" / "prompt.md"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text("Exercise deterministic task-start repair.\n", encoding="utf-8")
        try:
            yield root, load_profile(root), prompt_file
        finally:
            completed = subprocess.run(
                ["git", "-C", str(root), "worktree", "list", "--porcelain"],
                check=False,
                capture_output=True,
                text=True,
            )
            for line in completed.stdout.splitlines():
                if not line.startswith("worktree "):
                    continue
                candidate = Path(line.removeprefix("worktree ")).resolve()
                if candidate == root.resolve():
                    continue
                subprocess.run(
                    ["git", "-C", str(root), "worktree", "remove", "--force", str(candidate)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            shutil.rmtree(worktrees, ignore_errors=True)
            temporary.cleanup()

    def begin(self, profile, prompt_file: Path, **kwargs):
        actor = kwargs.pop("actor", "codex")
        return wtam.begin_task_worktree(
            profile,
            actor=actor,
            prompt=prompt_file.read_text(encoding="utf-8"),
            prompt_source=str(prompt_file),
            prompt_mode="raw",
            cwd=profile.paths.project_root,
            **kwargs,
        )

    def run_cli(self, *args: str, cwd: Path) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with chdir(cwd), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = blackdog_main(list(args))
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def only_task(self, profile) -> tuple[str, str]:
        planning = backlog.load_planning_state(profile.paths)
        self.assertEqual(len(planning.worksets), 1)
        workset = planning.worksets[0]
        self.assertEqual(len(workset.tasks), 1)
        return workset.workset_id, workset.tasks[0].task_id

    def durable_snapshot(self, root: Path, profile) -> tuple[object, ...]:
        control_files: list[tuple[object, ...]] = []
        if profile.paths.control_dir.exists():
            for candidate in sorted(profile.paths.control_dir.rglob("*")):
                relative = candidate.relative_to(profile.paths.control_dir)
                if relative.parts and relative.parts[0] == "locks":
                    continue
                metadata = candidate.lstat()
                if candidate.is_symlink():
                    content: object = ("link", candidate.readlink().as_posix())
                elif candidate.is_file():
                    content = ("file", candidate.read_bytes())
                else:
                    content = ("dir",)
                control_files.append(
                    (
                        relative.as_posix(),
                        metadata.st_mode,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        content,
                    )
                )

        def git(*args: str) -> tuple[int, str, str]:
            completed = subprocess.run(
                ["git", "-C", str(root), *args],
                check=False,
                capture_output=True,
                text=True,
            )
            return completed.returncode, completed.stdout, completed.stderr

        worktree_rows = git("worktree", "list", "--porcelain")
        registered_status: list[tuple[str, tuple[int, str, str], tuple[int, str, str]]] = []
        for line in worktree_rows[1].splitlines():
            if not line.startswith("worktree "):
                continue
            path = line.removeprefix("worktree ")
            registered_status.append(
                (
                    path,
                    (
                        lambda result: (result.returncode, result.stdout, result.stderr)
                    )(
                        subprocess.run(
                            ["git", "-C", path, "status", "--porcelain=v1", "--untracked-files=all"],
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                    ),
                    (
                        lambda result: (result.returncode, result.stdout, result.stderr)
                    )(
                        subprocess.run(
                            ["git", "-C", path, "rev-parse", "HEAD"],
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                    ),
                )
            )
        return (
            profile.paths.planning_file.read_bytes(),
            profile.paths.runtime_file.read_bytes()
            if profile.paths.runtime_file.exists()
            else None,
            profile.paths.events_file.read_bytes()
            if profile.paths.events_file.exists()
            else None,
            tuple(control_files),
            git("show-ref"),
            worktree_rows,
            tuple(registered_status),
        )

    def core_git_snapshot(self, root: Path, profile) -> tuple[object, ...]:
        def git(*args: str) -> tuple[int, str, str]:
            completed = subprocess.run(
                ["git", "-C", str(root), *args],
                check=False,
                capture_output=True,
                text=True,
            )
            return completed.returncode, completed.stdout, completed.stderr

        return (
            profile.paths.planning_file.read_bytes(),
            profile.paths.runtime_file.read_bytes(),
            profile.paths.events_file.read_bytes(),
            git("worktree", "list", "--porcelain"),
            git("show-ref"),
        )

    def reserve_without_product_event(self, profile, prompt_file: Path):
        original = wtam.append_event_once
        tripped = False

        def fail_once(*args, **kwargs):
            nonlocal tripped
            if kwargs.get("event_type") == "worktree.start" and not tripped:
                tripped = True
                raise OSError("reserve without product start event")
            return original(*args, **kwargs)

        with patch.object(wtam, "append_event_once", side_effect=fail_once):
            partial = self.begin(profile, prompt_file)
        self.assertTrue(tripped)
        self.assertEqual(partial.operation_status, "partial")
        self.assertEqual(partial.next_action.action_id, "repair_task_start_evidence")
        workset_id, task_id = self.only_task(profile)
        attempt = load_runtime_state(profile.paths).worksets[0].attempts[-1]
        return workset_id, task_id, attempt, partial

    def assert_one_start_event_each(self, profile, attempt_id: str) -> None:
        events = load_events(profile.paths.events_file)
        for event_type in ("task.claim", "task.start", "worktree.start"):
            event_id = (
                wtam._ordinary_resume_start_event_id(attempt_id)
                if event_type == "worktree.start"
                and any(
                    attempt.attempt_id == attempt_id
                    and isinstance(attempt.setup_receipt, dict)
                    and isinstance(attempt.setup_receipt.get("atomic_start"), dict)
                    and attempt.setup_receipt["atomic_start"].get("start_kind") == "resume"
                    for runtime_workset in load_runtime_state(profile.paths).worksets
                    for attempt in runtime_workset.attempts
                )
                else wtam._initial_start_event_id(attempt_id)
                if event_type == "worktree.start"
                else backlog.task_start_event_id(
                    attempt_id=attempt_id,
                    event_type=event_type,
                )
            )
            self.assertEqual(
                sum(event.get("event_id") == event_id for event in events),
                1,
                (event_type, events),
            )
        workset_event_id = backlog.task_start_event_id(
            attempt_id=attempt_id,
            event_type="workset.claim",
        )
        self.assertEqual(
            sum(event.get("event_id") == workset_event_id for event in events),
            1,
            events,
        )
        runtime_state = load_runtime_state(profile.paths)
        attempts = [
            attempt
            for runtime_workset in runtime_state.worksets
            for attempt in runtime_workset.attempts
            if attempt.attempt_id == attempt_id
        ]
        self.assertEqual(len(attempts), 1)
        attempt = attempts[0]
        claim = task_claim_index(runtime_state, runtime_state.worksets[0].workset_id).get(
            attempt.task_id
        )
        self.assertIsNotNone(claim)
        self.assertEqual(claim.attempt_id, attempt_id)
        self.assertIsNotNone(workset_claim(runtime_state, runtime_state.worksets[0].workset_id))

    def close_failed(
        self,
        profile,
        *,
        workset_id: str,
        task_id: str,
        actor: str = "codex",
    ) -> None:
        wtam.close_task_worktree(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor=actor,
            status="failed",
            summary="Close start-transaction fixture",
            cleanup=True,
        )

    def reserve_ordinary_without_product_event(self, profile, prompt_file: Path):
        started = self.begin(profile, prompt_file)
        workset_id = started["workset_id"]
        task_id = started["task_id"]
        self.close_failed(profile, workset_id=workset_id, task_id=task_id)
        original = wtam.append_event_once
        tripped = False

        def fail_once(*args, **kwargs):
            nonlocal tripped
            if kwargs.get("event_type") == "worktree.start" and not tripped:
                tripped = True
                raise OSError("reserve ordinary successor without product start event")
            return original(*args, **kwargs)

        with patch.object(wtam, "append_event_once", side_effect=fail_once):
            partial = self.begin(
                profile,
                prompt_file,
                workset_id=workset_id,
                task_id=task_id,
            )
        self.assertTrue(tripped)
        self.assertEqual(partial.operation_status, "partial")
        self.assertEqual(partial.next_action.action_id, "repair_task_start_evidence")
        attempt = load_runtime_state(profile.paths).worksets[0].attempts[-1]
        self.assertEqual(attempt.setup_receipt["atomic_start"]["start_kind"], "resume")
        return workset_id, task_id, attempt, partial

    def test_initial_start_repairs_every_core_event_boundary_and_byte_noops(self) -> None:
        for target_type in ("workset.claim", "task.claim", "task.start"):
            for fault_after_append in (False, True):
                with self.subTest(target_type=target_type, after=fault_after_append), self.start_repo() as (
                    _root,
                    profile,
                    prompt_file,
                ):
                    original = backlog.append_event_once
                    tripped = False

                    def fail_once(*args, **kwargs):
                        nonlocal tripped
                        if kwargs.get("event_type") == target_type and not tripped:
                            tripped = True
                            if fault_after_append:
                                original(*args, **kwargs)
                            raise OSError(f"fault at initial {target_type}")
                        return original(*args, **kwargs)

                    with patch.object(backlog, "append_event_once", side_effect=fail_once):
                        partial = self.begin(profile, prompt_file)
                    self.assertEqual(partial.operation_status, "partial")
                    self.assertTrue(partial.mutation_started)
                    self.assertFalse(partial.mutation_completed)
                    self.assertEqual(
                        partial.next_action.action_id,
                        "repair_task_start_evidence",
                    )
                    self.assertIn(f"fault at initial {target_type}", partial["error"])
                    self.assertTrue(tripped)
                    workset_id, task_id = self.only_task(profile)
                    attempt = load_runtime_state(profile.paths).worksets[0].attempts[0]
                    self.assertTrue(Path(str(attempt.worktree_path)).is_dir())
                    shown = wtam.show_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                    )
                    self.assertEqual(
                        shown.next_action.action_id,
                        "repair_task_start_evidence",
                        shown.to_dict(),
                    )
                    repaired = self.begin(
                        profile,
                        prompt_file,
                        workset_id=workset_id,
                        task_id=task_id,
                    )
                    self.assertEqual(repaired.operation_status, "succeeded")
                    self.assert_one_start_event_each(profile, attempt.attempt_id)
                    runtime_before = profile.paths.runtime_file.read_bytes()
                    events_before = profile.paths.events_file.read_bytes()
                    runtime_stat_before = profile.paths.runtime_file.stat()
                    events_stat_before = profile.paths.events_file.stat()
                    exact = self.begin(
                        profile,
                        prompt_file,
                        workset_id=workset_id,
                        task_id=task_id,
                    )
                    self.assertFalse(exact.mutation_started, exact.to_dict())
                    self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
                    self.assertEqual(profile.paths.events_file.read_bytes(), events_before)
                    runtime_stat_after = profile.paths.runtime_file.stat()
                    events_stat_after = profile.paths.events_file.stat()
                    self.assertEqual(runtime_stat_after.st_ino, runtime_stat_before.st_ino)
                    self.assertEqual(runtime_stat_after.st_mtime_ns, runtime_stat_before.st_mtime_ns)
                    self.assertEqual(events_stat_after.st_ino, events_stat_before.st_ino)
                    self.assertEqual(events_stat_after.st_mtime_ns, events_stat_before.st_mtime_ns)
                    self.close_failed(profile, workset_id=workset_id, task_id=task_id)

    def test_initial_product_event_fault_repairs_existing_or_missing_workspace(self) -> None:
        for fault_after_append in (False, True):
            for remove_workspace in (False, True):
                with self.subTest(after=fault_after_append, missing=remove_workspace), self.start_repo() as (
                    root,
                    profile,
                    prompt_file,
                ):
                    original = wtam.append_event_once
                    tripped = False

                    def fail_once(*args, **kwargs):
                        nonlocal tripped
                        if kwargs.get("event_type") == "worktree.start" and not tripped:
                            tripped = True
                            if fault_after_append:
                                original(*args, **kwargs)
                            raise OSError("fault at initial product start")
                        return original(*args, **kwargs)

                    with patch.object(wtam, "append_event_once", side_effect=fail_once):
                        partial = self.begin(profile, prompt_file)
                    self.assertEqual(partial.operation_status, "partial")
                    self.assertTrue(partial.mutation_started)
                    self.assertEqual(
                        partial.mutation_completed,
                        fault_after_append,
                    )
                    if not fault_after_append:
                        self.assertEqual(
                            partial.next_action.action_id,
                            "repair_task_start_evidence",
                        )
                    workset_id, task_id = self.only_task(profile)
                    attempt = load_runtime_state(profile.paths).worksets[0].attempts[0]
                    workspace = Path(str(attempt.worktree_path))
                    if remove_workspace:
                        subprocess.run(
                            ["git", "-C", str(root), "worktree", "remove", "--force", str(workspace)],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                        subprocess.run(
                            ["git", "-C", str(root), "branch", "-D", str(attempt.branch)],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                    repaired = self.begin(
                        profile,
                        prompt_file,
                        workset_id=workset_id,
                        task_id=task_id,
                    )
                    self.assertEqual(repaired.operation_status, "succeeded")
                    self.assertTrue(workspace.is_dir())
                    self.assert_one_start_event_each(profile, attempt.attempt_id)
                    self.close_failed(profile, workset_id=workset_id, task_id=task_id)

    def test_resume_repairs_core_and_product_faults_preserves_base_ref_and_noops(self) -> None:
        for fault_owner, fault_after_append in (
            ("core", False),
            ("core", True),
            ("product", False),
            ("product", True),
        ):
            with self.subTest(owner=fault_owner, after=fault_after_append), self.start_repo() as (
                root,
                profile,
                prompt_file,
            ):
                started = self.begin(profile, prompt_file)
                workset_id = started["workset_id"]
                task_id = started["task_id"]
                self.close_failed(profile, workset_id=workset_id, task_id=task_id)
                subprocess.run(
                    ["git", "-C", str(root), "branch", "resume-base", "main"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                module = backlog if fault_owner == "core" else wtam
                original = module.append_event_once
                target_type = "task.start" if fault_owner == "core" else "worktree.start"
                tripped = False

                def fail_once(*args, **kwargs):
                    nonlocal tripped
                    if kwargs.get("event_type") == target_type and not tripped:
                        tripped = True
                        if fault_after_append:
                            original(*args, **kwargs)
                        raise OSError(f"fault at resume {fault_owner}")
                    return original(*args, **kwargs)

                with patch.object(module, "append_event_once", side_effect=fail_once):
                    partial = self.begin(
                        profile,
                        prompt_file,
                        workset_id=workset_id,
                        task_id=task_id,
                        from_ref="resume-base",
                    )
                self.assertEqual(partial.operation_status, "partial")
                self.assertTrue(partial.mutation_started)
                start_completed = fault_owner == "product" and fault_after_append
                self.assertEqual(partial.mutation_completed, start_completed)
                self.assertEqual(
                    partial.next_action.action_id,
                    (
                        "inspect_pristine_active_task"
                        if start_completed
                        else "repair_task_start_evidence"
                    ),
                )
                successor = load_runtime_state(profile.paths).worksets[0].attempts[-1]
                self.assertEqual(
                    successor.setup_receipt["worktree_start"]["base_ref"],
                    "resume-base",
                )
                repaired = self.begin(
                    profile,
                    prompt_file,
                    workset_id=workset_id,
                    task_id=task_id,
                )
                self.assertEqual(repaired.operation_status, "succeeded")
                product = next(
                    event
                    for event in load_events(profile.paths.events_file)
                    if event.get("type") == "worktree.start"
                    and event.get("payload", {}).get("attempt_id") == successor.attempt_id
                )
                self.assertEqual(product["payload"]["base_ref"], "resume-base")
                runtime_before = profile.paths.runtime_file.read_bytes()
                events_before = profile.paths.events_file.read_bytes()
                exact = self.begin(
                    profile,
                    prompt_file,
                    workset_id=workset_id,
                    task_id=task_id,
                )
                self.assertFalse(exact.mutation_started, exact.to_dict())
                self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
                self.assertEqual(profile.paths.events_file.read_bytes(), events_before)
                self.close_failed(profile, workset_id=workset_id, task_id=task_id)

    def test_repair_failure_after_same_attempt_mutation_returns_typed_partial(self) -> None:
        with self.start_repo() as (_root, profile, prompt_file):
            original_core = backlog.append_event_once
            tripped_core = False

            def fail_core_once(*args, **kwargs):
                nonlocal tripped_core
                if kwargs.get("event_type") == "task.start" and not tripped_core:
                    tripped_core = True
                    raise OSError("first start event fault")
                return original_core(*args, **kwargs)

            with patch.object(backlog, "append_event_once", side_effect=fail_core_once):
                first_partial = self.begin(profile, prompt_file)
            self.assertEqual(first_partial.operation_status, "partial")
            workset_id, task_id = self.only_task(profile)
            attempt_id = first_partial["worktree"]["attempt_id"]
            events_before = profile.paths.events_file.read_bytes()

            original_product = wtam.append_event_once
            tripped_product = False

            def fail_product_once(*args, **kwargs):
                nonlocal tripped_product
                if kwargs.get("event_type") == "worktree.start" and not tripped_product:
                    tripped_product = True
                    raise OSError("repair product event fault")
                return original_product(*args, **kwargs)

            with patch.object(wtam, "append_event_once", side_effect=fail_product_once):
                repair_partial = self.begin(
                    profile,
                    prompt_file,
                    workset_id=workset_id,
                    task_id=task_id,
                )
            self.assertEqual(repair_partial.operation_status, "partial")
            self.assertTrue(repair_partial.mutation_started)
            self.assertFalse(repair_partial.mutation_completed)
            self.assertEqual(repair_partial["worktree"]["attempt_id"], attempt_id)
            self.assertEqual(
                repair_partial.next_action.action_id,
                "repair_task_start_evidence",
            )
            self.assertNotEqual(profile.paths.events_file.read_bytes(), events_before)

            repaired = self.begin(
                profile,
                prompt_file,
                workset_id=workset_id,
                task_id=task_id,
            )
            self.assertEqual(repaired.operation_status, "succeeded")
            self.close_failed(profile, workset_id=workset_id, task_id=task_id)

    def test_core_rejects_tampered_workset_claim_ownership_without_writes(self) -> None:
        with self.start_repo() as (_root, profile, prompt_file):
            started = self.begin(profile, prompt_file)
            workset_id = started["workset_id"]
            task_id = started["task_id"]
            self.close_failed(profile, workset_id=workset_id, task_id=task_id)

            original = backlog.append_event_once
            tripped = False

            def fail_once(*args, **kwargs):
                nonlocal tripped
                if kwargs.get("event_type") == "task.start" and not tripped:
                    tripped = True
                    raise OSError("reserve ordinary successor")
                return original(*args, **kwargs)

            with patch.object(backlog, "append_event_once", side_effect=fail_once):
                partial = self.begin(
                    profile,
                    prompt_file,
                    workset_id=workset_id,
                    task_id=task_id,
                )
            self.assertEqual(partial.operation_status, "partial")
            runtime = load_runtime_state(profile.paths)
            runtime_workset = runtime.worksets[0]
            successor = runtime_workset.attempts[-1]
            setup = dict(successor.setup_receipt or {})
            atomic = dict(setup["atomic_start"])
            self.assertIs(atomic["workset_claim_created"], True)
            atomic["workset_claim_created"] = False
            setup["atomic_start"] = atomic
            tampered_successor = replace(successor, setup_receipt=setup)
            tampered_runtime_workset = replace(
                runtime_workset,
                attempts=(*runtime_workset.attempts[:-1], tampered_successor),
            )
            save_runtime_state(
                profile.paths,
                replace(runtime, worksets=(tampered_runtime_workset,)),
            )
            runtime_before = profile.paths.runtime_file.read_bytes()
            events_before = profile.paths.events_file.read_bytes()
            runtime_stat_before = profile.paths.runtime_file.stat()
            events_stat_before = profile.paths.events_file.stat()

            with self.assertRaisesRegex(
                backlog.BacklogError,
                "workset-claim ownership",
            ):
                backlog.start_task(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor=tampered_successor.actor,
                    execution_model=tampered_successor.execution_model,
                    workspace_identity=tampered_successor.workspace_identity,
                    workspace_mode=tampered_successor.workspace_mode,
                    worktree_role=tampered_successor.worktree_role,
                    worktree_path=tampered_successor.worktree_path,
                    branch=tampered_successor.branch,
                    target_branch=tampered_successor.target_branch,
                    integration_branch=tampered_successor.integration_branch,
                    start_commit=tampered_successor.start_commit,
                    model=tampered_successor.model,
                    reasoning_effort=tampered_successor.reasoning_effort,
                    codex_session=tampered_successor.codex_session,
                    prompt_receipt=tampered_successor.prompt_receipt,
                    user_prompt_receipt=tampered_successor.user_prompt_receipt,
                    note=tampered_successor.note,
                    setup_receipt=tampered_successor.setup_receipt,
                    attempt_id=tampered_successor.attempt_id,
                    expected_predecessor_attempt_id=atomic[
                        "expected_predecessor_attempt_id"
                    ],
                    atomic_start_kind="resume",
                    expected_task_actor=atomic["expected_task_actor"],
                    expected_execution_prompt_hash=atomic[
                        "expected_execution_prompt_hash"
                    ],
                    expected_execution_prompt_mode=atomic[
                        "expected_execution_prompt_mode"
                    ],
                    expected_request_prompt_hash=atomic[
                        "expected_request_prompt_hash"
                    ],
                    expected_request_prompt_mode=atomic[
                        "expected_request_prompt_mode"
                    ],
                    expected_task_updated_at=atomic["expected_task_updated_at"],
                )
            self.assertEqual(profile.paths.runtime_file.read_bytes(), runtime_before)
            self.assertEqual(profile.paths.events_file.read_bytes(), events_before)
            self.assertEqual(profile.paths.runtime_file.stat().st_ino, runtime_stat_before.st_ino)
            self.assertEqual(
                profile.paths.runtime_file.stat().st_mtime_ns,
                runtime_stat_before.st_mtime_ns,
            )
            self.assertEqual(profile.paths.events_file.stat().st_ino, events_stat_before.st_ino)
            self.assertEqual(
                profile.paths.events_file.stat().st_mtime_ns,
                events_stat_before.st_mtime_ns,
            )

    def test_pre_reservation_git_failure_returns_typed_retained_envelope(self) -> None:
        with self.start_repo() as (root, profile, prompt_file):
            request_file = profile.paths.control_dir / "test-inputs" / "request.md"
            request_file.write_text(prompt_file.read_text(encoding="utf-8"), encoding="utf-8")
            custom_path = root.parent / f"{root.name}-explicit-retry-worktree"
            custom_branch = "agent/explicit-retry-branch"
            custom_base = "explicit-retry-base"
            custom_model = "start-transaction-test-model"
            custom_reasoning = "high"
            custom_note = "preserve every start override"
            subprocess.run(
                ["git", "-C", str(root), "branch", custom_base, "main"],
                check=True,
                capture_output=True,
                text=True,
            )
            original = wtam._run_git_no_check

            def fail_worktree_add(repo_root, *args, **kwargs):
                if args[:2] == ("worktree", "add"):
                    return subprocess.CompletedProcess(
                        ["git", "-C", str(repo_root), *args],
                        1,
                        stdout="",
                        stderr="injected worktree add failure",
                    )
                return original(repo_root, *args, **kwargs)

            with patch.object(wtam, "_run_git_no_check", side_effect=fail_worktree_add):
                partial = self.begin(
                    profile,
                    prompt_file,
                    user_prompt=request_file.read_text(encoding="utf-8"),
                    user_prompt_source=str(request_file),
                    branch=custom_branch,
                    from_ref=custom_base,
                    path=str(custom_path),
                    model=custom_model,
                    reasoning_effort=custom_reasoning,
                    note=custom_note,
                )
            self.assertEqual(partial.operation_status, "partial")
            self.assertTrue(partial.mutation_started)
            self.assertFalse(partial.mutation_completed)
            self.assertEqual(partial.mutation_phase, "preflight")
            self.assertIsNone(partial.attempt_status)
            self.assertIsNone(partial["worktree"])
            self.assertEqual(partial.next_action.action_id, "retry_reserved_task_begin")
            self.assertEqual(partial.next_action.kind, "command")
            self.assertIn("--workset=", partial.next_action.action.command)
            self.assertIn("--task=", partial.next_action.action.command)
            self.assertIn("--execution-prompt-file=", partial.next_action.action.command)
            retry_argv = partial.next_action.action.argv
            self.assertTrue(any(arg.startswith("--request-file=") for arg in retry_argv))
            for expected in (
                f"--branch={custom_branch}",
                f"--from={custom_base}",
                f"--path={custom_path}",
                f"--model={custom_model}",
                f"--reasoning-effort={custom_reasoning}",
                f"--note={custom_note}",
            ):
                self.assertIn(expected, retry_argv)
            workset_id, task_id = self.only_task(profile)
            self.assertEqual(partial["workset_id"], workset_id)
            self.assertEqual(partial["task_id"], task_id)
            self.assertTrue(
                (
                    profile.paths.control_dir
                    / partial["execution_prompt_replay_artifact_path"]
                ).is_file()
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(root), "worktree", "list", "--porcelain"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.count("worktree "),
                1,
            )
            repaired = subprocess.run(
                list(retry_argv),
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(repaired.returncode, 0, repaired.stderr or repaired.stdout)
            attempt = load_runtime_state(profile.paths).worksets[0].attempts[-1]
            self.assertEqual(attempt.branch, custom_branch)
            self.assertEqual(Path(str(attempt.worktree_path)), custom_path.resolve())
            self.assertEqual(attempt.model, custom_model)
            self.assertEqual(attempt.reasoning_effort, custom_reasoning)
            self.assertEqual(attempt.note, custom_note)
            self.assertEqual(
                attempt.setup_receipt["worktree_start"]["base_ref"],
                custom_base,
            )
            self.assertEqual(attempt.prompt_receipt.prompt_hash, partial["execution_prompt_hash"])
            self.assertEqual(attempt.prompt_receipt.mode, "raw")
            self.assertEqual(attempt.user_prompt_receipt.prompt_hash, partial["user_prompt_hash"])
            self.assertEqual(attempt.user_prompt_receipt.mode, "raw")
            self.assertEqual(
                attempt.prompt_receipt.replay_artifact_path,
                partial["execution_prompt_replay_artifact_path"],
            )
            self.assertEqual(
                attempt.user_prompt_receipt.replay_artifact_path,
                partial["user_prompt_replay_artifact_path"],
            )
            self.close_failed(profile, workset_id=workset_id, task_id=task_id)

    def test_auto_envelope_reservation_repairs_each_durable_boundary(self) -> None:
        for fault_boundary in ("planning_saved", "runtime_saved"):
            with self.subTest(boundary=fault_boundary), self.start_repo() as (
                root,
                profile,
                prompt_file,
            ):
                if fault_boundary == "planning_saved":
                    original = backlog.save_planning_state
                    tripped = False

                    def fail_after_planning_save(*args, **kwargs):
                        nonlocal tripped
                        result = original(*args, **kwargs)
                        if not tripped:
                            tripped = True
                            raise OSError("fault after auto-envelope planning save")
                        return result

                    fault = patch.object(
                        backlog,
                        "save_planning_state",
                        side_effect=fail_after_planning_save,
                    )
                else:
                    original = backlog.append_event_once
                    tripped = False

                    def fail_before_workset_event(*args, **kwargs):
                        nonlocal tripped
                        if kwargs.get("event_type") == "workset.put" and not tripped:
                            tripped = True
                            raise OSError("fault after auto-envelope runtime save")
                        return original(*args, **kwargs)

                    fault = patch.object(
                        backlog,
                        "append_event_once",
                        side_effect=fail_before_workset_event,
                    )

                with fault:
                    partial = self.begin(profile, prompt_file)
                self.assertEqual(partial.operation_status, "partial", partial.to_dict())
                self.assertTrue(partial.mutation_started)
                self.assertFalse(partial.mutation_completed)
                self.assertEqual(partial.mutation_phase, "preflight")
                self.assertEqual(
                    partial.next_action.action_id,
                    "retry_reserved_task_begin",
                )
                workset_id = partial["workset_id"]
                task_id = partial["task_id"]
                planning = backlog.load_planning_state(profile.paths)
                self.assertEqual(len(planning.worksets), 1)
                self.assertEqual(len(planning.worksets[0].tasks), 1)
                self.assertEqual(planning.worksets[0].workset_id, workset_id)
                self.assertEqual(planning.worksets[0].tasks[0].task_id, task_id)

                retry_argv = list(partial.next_action.action.argv)
                repaired = subprocess.run(
                    retry_argv,
                    cwd=root,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(repaired.returncode, 0, repaired.stderr or repaired.stdout)
                runtime = load_runtime_state(profile.paths)
                self.assertEqual(len(runtime.worksets), 1)
                self.assertEqual(len(runtime.worksets[0].attempts), 1)
                envelope_event_id = wtam._task_begin_workset_event_id(workset_id)
                self.assertEqual(
                    sum(
                        event.get("event_id") == envelope_event_id
                        for event in load_events(profile.paths.events_file)
                    ),
                    1,
                )

                stores_before = (
                    profile.paths.planning_file.read_bytes(),
                    profile.paths.runtime_file.read_bytes(),
                    profile.paths.events_file.read_bytes(),
                )
                git_before = subprocess.run(
                    ["git", "-C", str(root), "worktree", "list", "--porcelain"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                refs_before = subprocess.run(
                    ["git", "-C", str(root), "show-ref"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                exact = subprocess.run(
                    retry_argv,
                    cwd=root,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(exact.returncode, 0, exact.stderr or exact.stdout)
                self.assertEqual(
                    (
                        profile.paths.planning_file.read_bytes(),
                        profile.paths.runtime_file.read_bytes(),
                        profile.paths.events_file.read_bytes(),
                    ),
                    stores_before,
                )
                self.assertEqual(
                    subprocess.run(
                        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout,
                    git_before,
                )
                self.assertEqual(
                    subprocess.run(
                        ["git", "-C", str(root), "show-ref"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout,
                    refs_before,
                )
                self.close_failed(profile, workset_id=workset_id, task_id=task_id)

    def test_unreserved_exact_git_workspace_converges_after_start_faults(self) -> None:
        expected_states = {
            "after_worktree_add": "workspace",
            "handler_cleanup_workspace": "workspace",
            "handler_cleanup_branch": "branch",
            "handler_cleanup_absent": "absent",
            "handler_cleanup_unregistered_path": "conflict",
            "handler_cleanup_dirty_workspace": "conflict",
            "handler_cleanup_moved_branch": "conflict",
            "handler_cleanup_mismatched_registration": "conflict",
        }
        for fault_kind, expected_state in expected_states.items():
            with self.subTest(fault=fault_kind), self.start_repo() as (
                root,
                profile,
                prompt_file,
            ):
                original_git = wtam._run_git_no_check
                tripped = False
                alternate_registration = root.parent / f"{root.name}-alternate-registration"

                def git_fault(repo_root, *args, **kwargs):
                    nonlocal tripped
                    if (
                        fault_kind == "after_worktree_add"
                        and args[:2] == ("worktree", "add")
                        and not tripped
                    ):
                        tripped = True
                        completed = original_git(repo_root, *args, **kwargs)
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                        return subprocess.CompletedProcess(
                            completed.args,
                            1,
                            stdout=completed.stdout,
                            stderr="injected failure after exact worktree add",
                        )
                    if args[:3] == ("worktree", "remove", "--force"):
                        if fault_kind in {
                            "handler_cleanup_workspace",
                            "handler_cleanup_dirty_workspace",
                        }:
                            return subprocess.CompletedProcess(
                                ["git", "-C", str(repo_root), *args],
                                1,
                                stdout="",
                                stderr="injected cleanup refusal",
                            )
                        removed_branch = None
                        if fault_kind == "handler_cleanup_mismatched_registration":
                            removed_branch = subprocess.run(
                                [
                                    "git",
                                    "-C",
                                    str(args[-1]),
                                    "branch",
                                    "--show-current",
                                ],
                                check=True,
                                capture_output=True,
                                text=True,
                            ).stdout.strip()
                        completed = original_git(repo_root, *args, **kwargs)
                        if fault_kind == "handler_cleanup_unregistered_path":
                            retained_path = Path(str(args[-1]))
                            retained_path.mkdir(parents=True, exist_ok=True)
                            (retained_path / "unregistered-residual.txt").write_text(
                                "cleanup returned after unregistering this path\n",
                                encoding="utf-8",
                            )
                            return subprocess.CompletedProcess(
                                completed.args,
                                1,
                                stdout=completed.stdout,
                                stderr="injected failure after unregistering the task path",
                            )
                        if fault_kind == "handler_cleanup_mismatched_registration":
                            self.assertTrue(removed_branch)
                            self.assertEqual(completed.returncode, 0, completed.stderr)
                            added = original_git(
                                repo_root,
                                "worktree",
                                "add",
                                str(alternate_registration),
                                str(removed_branch),
                            )
                            self.assertEqual(added.returncode, 0, added.stderr)
                            return subprocess.CompletedProcess(
                                completed.args,
                                1,
                                stdout=completed.stdout,
                                stderr="injected failure after moving the branch registration",
                            )
                        return completed
                    if args[:2] == ("branch", "-D"):
                        if fault_kind in {
                            "handler_cleanup_workspace",
                            "handler_cleanup_dirty_workspace",
                            "handler_cleanup_branch",
                        }:
                            return subprocess.CompletedProcess(
                                ["git", "-C", str(repo_root), *args],
                                1,
                                stdout="",
                                stderr="injected branch cleanup refusal",
                            )
                        if fault_kind == "handler_cleanup_moved_branch":
                            branch_name = str(args[-1])
                            base = subprocess.run(
                                ["git", "-C", str(repo_root), "rev-parse", branch_name],
                                check=True,
                                capture_output=True,
                                text=True,
                            ).stdout.strip()
                            tree = subprocess.run(
                                ["git", "-C", str(repo_root), "rev-parse", f"{base}^{{tree}}"],
                                check=True,
                                capture_output=True,
                                text=True,
                            ).stdout.strip()
                            moved = subprocess.run(
                                [
                                    "git",
                                    "-C",
                                    str(repo_root),
                                    "commit-tree",
                                    tree,
                                    "-p",
                                    base,
                                    "-m",
                                    "injected retained branch movement",
                                ],
                                check=True,
                                capture_output=True,
                                text=True,
                            ).stdout.strip()
                            subprocess.run(
                                [
                                    "git",
                                    "-C",
                                    str(repo_root),
                                    "update-ref",
                                    f"refs/heads/{branch_name}",
                                    moved,
                                ],
                                check=True,
                                capture_output=True,
                                text=True,
                            )
                            return subprocess.CompletedProcess(
                                ["git", "-C", str(repo_root), *args],
                                1,
                                stdout="",
                                stderr="injected failure after moving the retained branch",
                            )
                    return original_git(repo_root, *args, **kwargs)

                def handler_fault(_profile, *, worktree_path, **_kwargs):
                    if fault_kind == "handler_cleanup_dirty_workspace":
                        (Path(worktree_path) / "dirty-residual.txt").write_text(
                            "handler mutation retained before cleanup failure\n",
                            encoding="utf-8",
                        )
                    raise OSError("injected handler failure")

                with patch.object(wtam, "_run_git_no_check", side_effect=git_fault), patch.object(
                    wtam,
                    "execute_worktree_handlers",
                    side_effect=handler_fault,
                ):
                    partial = self.begin(profile, prompt_file)

                workset_id = partial["workset_id"]
                task_id = partial["task_id"]
                planning = backlog.load_planning_state(profile.paths)
                self.assertEqual(len(planning.worksets), 1)
                workset = planning.worksets[0]
                task = workset.tasks[0]
                branch = wtam.default_task_branch(workset_id, task)
                workspace = wtam.default_task_worktree_path(
                    profile,
                    workset_id=workset_id,
                    task=task,
                ).resolve()
                self.assertEqual(len(load_runtime_state(profile.paths).worksets[0].attempts), 0)
                self.assertEqual(partial["retained_workspace_state"], expected_state)

                if expected_state == "conflict":
                    self.assertEqual(partial.operation_status, "blocked", partial.to_dict())
                    self.assertTrue(partial.mutation_started)
                    self.assertFalse(partial.mutation_completed)
                    self.assertEqual(
                        partial.next_action.action_id,
                        "task_start_workspace_proof_required",
                    )
                    self.assertEqual(partial.next_action.kind, "blocked")
                    self.assertIsNone(partial.next_action.action)
                    self.assertEqual(partial["recommended_commands"], [])
                    before_retry = (
                        profile.paths.planning_file.read_bytes(),
                        profile.paths.runtime_file.read_bytes(),
                        profile.paths.events_file.read_bytes(),
                        subprocess.run(
                            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
                            check=True,
                            capture_output=True,
                            text=True,
                        ).stdout,
                        subprocess.run(
                            ["git", "-C", str(root), "show-ref"],
                            check=True,
                            capture_output=True,
                            text=True,
                        ).stdout,
                    )
                    exact_retry = self.begin(
                        profile,
                        prompt_file,
                        workset_id=workset_id,
                        task_id=task_id,
                    )
                    self.assertEqual(exact_retry.operation_status, "blocked")
                    self.assertFalse(exact_retry.mutation_started)
                    self.assertFalse(exact_retry.mutation_completed)
                    self.assertEqual(exact_retry.mutation_phase, "none")
                    self.assertEqual(
                        exact_retry.next_action.action_id,
                        "task_start_workspace_proof_required",
                    )
                    self.assertIsNone(exact_retry.next_action.action)
                    self.assertEqual(
                        (
                            profile.paths.planning_file.read_bytes(),
                            profile.paths.runtime_file.read_bytes(),
                            profile.paths.events_file.read_bytes(),
                            subprocess.run(
                                ["git", "-C", str(root), "worktree", "list", "--porcelain"],
                                check=True,
                                capture_output=True,
                                text=True,
                            ).stdout,
                            subprocess.run(
                                ["git", "-C", str(root), "show-ref"],
                                check=True,
                                capture_output=True,
                                text=True,
                            ).stdout,
                        ),
                        before_retry,
                    )
                    continue

                self.assertEqual(partial.operation_status, "partial", partial.to_dict())
                self.assertEqual(
                    partial.next_action.action_id,
                    "retry_reserved_task_begin",
                )
                registered = subprocess.run(
                    ["git", "-C", str(root), "worktree", "list", "--porcelain"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                self.assertEqual(
                    registered.count(f"worktree {workspace}"),
                    1 if expected_state == "workspace" else 0,
                )
                self.assertEqual(
                    registered.count(f"branch refs/heads/{branch}"),
                    1 if expected_state == "workspace" else 0,
                )

                repaired = subprocess.run(
                    list(partial.next_action.action.argv),
                    cwd=root,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(repaired.returncode, 0, repaired.stderr or repaired.stdout)
                runtime = load_runtime_state(profile.paths)
                self.assertEqual(len(runtime.worksets), 1)
                self.assertEqual(len(runtime.worksets[0].attempts), 1)
                attempt = runtime.worksets[0].attempts[0]
                self.assertEqual(attempt.branch, branch)
                self.assertEqual(Path(str(attempt.worktree_path)), workspace)
                registered = subprocess.run(
                    ["git", "-C", str(root), "worktree", "list", "--porcelain"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
                self.assertEqual(registered.count(f"worktree {workspace}"), 1)
                self.assertEqual(registered.count(f"branch refs/heads/{branch}"), 1)
                self.close_failed(profile, workset_id=workset_id, task_id=task_id)

    def test_concurrent_distinct_auto_envelopes_preserve_both_reservations(self) -> None:
        with self.start_repo() as (root, profile, prompt_file):
            second_prompt = profile.paths.control_dir / "test-inputs" / "second-prompt.md"
            second_prompt.write_text(
                "Exercise a distinct concurrent task-begin reservation.\n",
                encoding="utf-8",
            )
            barrier = threading.Barrier(2)
            original_reserve = wtam._reserve_auto_task_envelope
            original_upsert = wtam.upsert_workset
            original_git = wtam._run_git_no_check
            results: list[object] = []
            errors: list[BaseException] = []
            result_lock = threading.Lock()

            class LostUpdatePlanningStore:
                """Force the historical load/load/save/save window when it is unlocked."""

                def __init__(self) -> None:
                    self.delegate = backlog.JsonPlanningStore()
                    self.lock = threading.Lock()
                    self.second_load = threading.Event()
                    self.load_count = 0
                    self.save_count = 0
                    self.overlap_before_first_save = False

                def load(self, path):
                    snapshot = self.delegate.load(path)
                    with self.lock:
                        self.load_count += 1
                        index = self.load_count
                        if index == 2:
                            self.overlap_before_first_save = self.save_count == 0
                            self.second_load.set()
                    if index == 1:
                        # With no planning lock, the peer reaches its stale load and
                        # releases this wait. With the lock, it cannot enter until
                        # this transaction saves and releases the file lock.
                        self.second_load.wait(timeout=0.75)
                    return snapshot

                def save(self, path, state):
                    self.delegate.save(path, state)
                    with self.lock:
                        self.save_count += 1

            planning_probe = LostUpdatePlanningStore()

            def synchronized_reserve(*args, **kwargs):
                barrier.wait(timeout=10)
                return original_reserve(*args, **kwargs)

            def probed_upsert(*args, **kwargs):
                return original_upsert(
                    *args,
                    planning_store=planning_probe,
                    **kwargs,
                )

            def fail_worktree_add(repo_root, *args, **kwargs):
                if args[:2] == ("worktree", "add"):
                    return subprocess.CompletedProcess(
                        ["git", "-C", str(repo_root), *args],
                        1,
                        stdout="",
                        stderr="hold both begins after envelope reservation",
                    )
                return original_git(repo_root, *args, **kwargs)

            def run_begin(path: Path) -> None:
                try:
                    result = self.begin(profile, path)
                    with result_lock:
                        results.append(result)
                except BaseException as exc:  # pragma: no cover - asserted below
                    with result_lock:
                        errors.append(exc)

            with patch.object(
                wtam,
                "_reserve_auto_task_envelope",
                side_effect=synchronized_reserve,
            ), patch.object(
                wtam,
                "upsert_workset",
                side_effect=probed_upsert,
            ), patch.object(wtam, "_run_git_no_check", side_effect=fail_worktree_add):
                threads = [
                    threading.Thread(target=run_begin, args=(path,))
                    for path in (prompt_file, second_prompt)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
                self.assertTrue(all(not thread.is_alive() for thread in threads))

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(planning_probe.load_count, 2)
            self.assertEqual(planning_probe.save_count, 2)
            self.assertFalse(
                planning_probe.overlap_before_first_save,
                "planning reservations must serialize load-through-save",
            )
            self.assertTrue(all(result.operation_status == "partial" for result in results))
            workset_ids = {result["workset_id"] for result in results}
            self.assertEqual(len(workset_ids), 2)
            planning = backlog.load_planning_state(profile.paths)
            self.assertEqual({workset.workset_id for workset in planning.worksets}, workset_ids)
            self.assertTrue(all(len(workset.tasks) == 1 for workset in planning.worksets))
            runtime = load_runtime_state(profile.paths)
            self.assertEqual({workset.workset_id for workset in runtime.worksets}, workset_ids)
            self.assertTrue(all(not workset.attempts for workset in runtime.worksets))
            events = load_events(profile.paths.events_file)
            for workset_id in workset_ids:
                self.assertEqual(
                    sum(
                        event.get("event_id")
                        == wtam._task_begin_workset_event_id(workset_id)
                        for event in events
                    ),
                    1,
                )

            for result in results:
                repaired = subprocess.run(
                    list(result.next_action.action.argv),
                    cwd=root,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(repaired.returncode, 0, repaired.stderr or repaired.stdout)
            runtime = load_runtime_state(profile.paths)
            self.assertEqual({workset.workset_id for workset in runtime.worksets}, workset_ids)
            self.assertTrue(all(len(workset.attempts) == 1 for workset in runtime.worksets))
            for result in results:
                self.close_failed(
                    profile,
                    workset_id=result["workset_id"],
                    task_id=result["task_id"],
                )

    def test_start_repair_conflicts_are_zero_mutation(self) -> None:
        conflict_kinds = (
            "handler_receipt",
            "noncanonical_primary",
            "path_override",
            "branch_tip",
            "registration",
            "event_payload",
        )
        for conflict_kind in conflict_kinds:
            with self.subTest(conflict=conflict_kind), self.start_repo() as (
                root,
                profile,
                prompt_file,
            ):
                workset_id, task_id, attempt, _partial = self.reserve_without_product_event(
                    profile,
                    prompt_file,
                )
                retry_kwargs: dict[str, str] = {}
                alias: Path | None = None
                removed_handler_ve: Path | None = None
                if conflict_kind in {"handler_receipt", "noncanonical_primary"}:
                    runtime = load_runtime_state(profile.paths)
                    runtime_workset = runtime.worksets[0]
                    setup = dict(attempt.setup_receipt or {})
                    if conflict_kind == "handler_receipt":
                        probes = [dict(probe) for probe in setup["probes"]]
                        handler_probe = next(
                            probe for probe in probes if probe.get("handler_id") is not None
                        )
                        handler_probe["action"] = "tampered-handler-action"
                        setup["probes"] = probes
                        removed_handler_ve = Path(str(attempt.worktree_path)) / ".VE"
                        shutil.rmtree(removed_handler_ve)
                    else:
                        alias = root.parent / f"{root.name}-primary-alias"
                        alias.symlink_to(root.resolve(), target_is_directory=True)
                        start_receipt = dict(setup["worktree_start"])
                        start_receipt["primary_worktree"] = str(alias.absolute())
                        setup["worktree_start"] = start_receipt
                    tampered = replace(attempt, setup_receipt=setup)
                    save_runtime_state(
                        profile.paths,
                        replace(
                            runtime,
                            worksets=(
                                replace(
                                    runtime_workset,
                                    attempts=(*runtime_workset.attempts[:-1], tampered),
                                ),
                            ),
                        ),
                    )
                elif conflict_kind == "path_override":
                    retry_kwargs["path"] = str(root.parent / "different-task-path")
                elif conflict_kind in {"branch_tip", "registration"}:
                    workspace = Path(str(attempt.worktree_path))
                    subprocess.run(
                        ["git", "-C", str(root), "worktree", "remove", "--force", str(workspace)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    if conflict_kind == "branch_tip":
                        tree = subprocess.run(
                            ["git", "-C", str(root), "rev-parse", f"{attempt.start_commit}^{{tree}}"],
                            check=True,
                            capture_output=True,
                            text=True,
                        ).stdout.strip()
                        moved = subprocess.run(
                            [
                                "git",
                                "-C",
                                str(root),
                                "commit-tree",
                                tree,
                                "-p",
                                str(attempt.start_commit),
                                "-m",
                                "conflicting branch tip",
                            ],
                            check=True,
                            capture_output=True,
                            text=True,
                        ).stdout.strip()
                        subprocess.run(
                            ["git", "-C", str(root), "update-ref", f"refs/heads/{attempt.branch}", moved],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                    else:
                        alternate = root.parent / f"{root.name}-alternate-registration"
                        subprocess.run(
                            ["git", "-C", str(root), "worktree", "add", str(alternate), str(attempt.branch)],
                            check=True,
                            capture_output=True,
                            text=True,
                        )
                else:
                    wtam.append_event_once(
                        profile.paths.events_file,
                        event_id=wtam._initial_start_event_id(attempt.attempt_id),
                        event_type="worktree.start",
                        actor=attempt.actor,
                        payload={"conflict": True},
                    )

                before = self.durable_snapshot(root, profile)

                def retry() -> None:
                    blocked = self.begin(
                        profile,
                        prompt_file,
                        workset_id=workset_id,
                        task_id=task_id,
                        **retry_kwargs,
                    )
                    self.assertEqual(blocked.operation_status, "blocked", blocked.to_dict())
                    self.assertFalse(blocked.mutation_started)
                    self.assertFalse(blocked.mutation_completed)
                    self.assertEqual(blocked.mutation_phase, "none")
                    self.assertEqual(
                        blocked.next_action.action_id,
                        "task_start_proof_required",
                    )
                    self.assertIsNone(blocked.next_action.action)
                    self.assertEqual(blocked["recommended_commands"], [])

                if conflict_kind == "handler_receipt":
                    with patch.object(
                        wtam,
                        "execute_worktree_handlers",
                        side_effect=AssertionError("handler mutation ran before conflict rejection"),
                    ):
                        retry()
                else:
                    retry()
                self.assertEqual(self.durable_snapshot(root, profile), before)
                if removed_handler_ve is not None:
                    self.assertFalse(removed_handler_ve.exists())
                if alias is not None:
                    alias.unlink()

    def test_ordinary_resume_recreates_missing_workspace_exactly(self) -> None:
        with self.start_repo() as (root, profile, prompt_file):
            workset_id, task_id, successor, _partial = (
                self.reserve_ordinary_without_product_event(profile, prompt_file)
            )
            workspace = Path(str(successor.worktree_path))
            subprocess.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(workspace)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "branch", "-D", str(successor.branch)],
                check=True,
                capture_output=True,
                text=True,
            )
            repaired = self.begin(
                profile,
                prompt_file,
                workset_id=workset_id,
                task_id=task_id,
            )
            self.assertEqual(repaired.operation_status, "succeeded")
            self.assertTrue(repaired.mutation_started)
            self.assertEqual(repaired["worktree"]["workspace_action"], "repaired")
            self.assertTrue(workspace.is_dir())
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(workspace), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                successor.start_commit,
            )
            self.assert_one_start_event_each(profile, successor.attempt_id)
            runtime_before = profile.paths.runtime_file.stat()
            events_before = profile.paths.events_file.stat()
            exact = self.begin(
                profile,
                prompt_file,
                workset_id=workset_id,
                task_id=task_id,
            )
            self.assertFalse(exact.mutation_started)
            self.assertEqual(exact["worktree"]["workspace_action"], "reused")
            self.assertEqual(profile.paths.runtime_file.stat().st_ino, runtime_before.st_ino)
            self.assertEqual(
                profile.paths.runtime_file.stat().st_mtime_ns,
                runtime_before.st_mtime_ns,
            )
            self.assertEqual(profile.paths.events_file.stat().st_ino, events_before.st_ino)
            self.assertEqual(
                profile.paths.events_file.stat().st_mtime_ns,
                events_before.st_mtime_ns,
            )
            self.close_failed(profile, workset_id=workset_id, task_id=task_id)

    def test_ordinary_resume_conflicts_are_zero_mutation(self) -> None:
        for conflict_kind in (
            "atomic_receipt",
            "handler_receipt",
            "path_override",
            "registration",
            "event_payload",
        ):
            with self.subTest(conflict=conflict_kind), self.start_repo() as (
                root,
                profile,
                prompt_file,
            ):
                workset_id, task_id, successor, _partial = (
                    self.reserve_ordinary_without_product_event(profile, prompt_file)
                )
                retry_kwargs: dict[str, str] = {}
                removed_handler_ve: Path | None = None
                if conflict_kind in {"atomic_receipt", "handler_receipt"}:
                    runtime = load_runtime_state(profile.paths)
                    runtime_workset = runtime.worksets[0]
                    setup = dict(successor.setup_receipt or {})
                    if conflict_kind == "atomic_receipt":
                        atomic = dict(setup["atomic_start"])
                        self.assertIs(atomic["workset_claim_created"], True)
                        atomic["workset_claim_created"] = False
                        setup["atomic_start"] = atomic
                    else:
                        probes = [dict(probe) for probe in setup["probes"]]
                        next(
                            probe for probe in probes if probe.get("handler_id") is not None
                        )["action"] = "tampered-handler-action"
                        setup["probes"] = probes
                        removed_handler_ve = Path(str(successor.worktree_path)) / ".VE"
                        shutil.rmtree(removed_handler_ve)
                    tampered = replace(successor, setup_receipt=setup)
                    save_runtime_state(
                        profile.paths,
                        replace(
                            runtime,
                            worksets=(
                                replace(
                                    runtime_workset,
                                    attempts=(*runtime_workset.attempts[:-1], tampered),
                                ),
                            ),
                        ),
                    )
                elif conflict_kind == "path_override":
                    retry_kwargs["path"] = str(root.parent / "wrong-ordinary-resume-path")
                elif conflict_kind == "registration":
                    workspace = Path(str(successor.worktree_path))
                    subprocess.run(
                        ["git", "-C", str(root), "worktree", "remove", "--force", str(workspace)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    alternate = root.parent / f"{root.name}-ordinary-alternate"
                    subprocess.run(
                        ["git", "-C", str(root), "worktree", "add", str(alternate), str(successor.branch)],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                else:
                    wtam.append_event_once(
                        profile.paths.events_file,
                        event_id=wtam._ordinary_resume_start_event_id(successor.attempt_id),
                        event_type="worktree.start",
                        actor=successor.actor,
                        payload={"conflict": True},
                    )

                before = self.durable_snapshot(root, profile)

                def retry() -> None:
                    blocked = self.begin(
                        profile,
                        prompt_file,
                        workset_id=workset_id,
                        task_id=task_id,
                        **retry_kwargs,
                    )
                    self.assertEqual(blocked.operation_status, "blocked", blocked.to_dict())
                    self.assertFalse(blocked.mutation_started)
                    self.assertFalse(blocked.mutation_completed)
                    self.assertEqual(blocked.mutation_phase, "none")
                    self.assertEqual(
                        blocked.next_action.action_id,
                        "task_start_proof_required",
                    )
                    self.assertIsNone(blocked.next_action.action)
                    self.assertEqual(blocked["recommended_commands"], [])

                if conflict_kind == "handler_receipt":
                    with patch.object(
                        wtam,
                        "execute_worktree_handlers",
                        side_effect=AssertionError("handler mutation ran before conflict rejection"),
                    ):
                        retry()
                else:
                    retry()
                self.assertEqual(self.durable_snapshot(root, profile), before)
                if removed_handler_ve is not None:
                    self.assertFalse(removed_handler_ve.exists())

    def test_two_resume_cycles_select_only_the_current_transition(self) -> None:
        with self.start_repo() as (_root, profile, prompt_file):
            initial = self.begin(profile, prompt_file)
            workset_id = initial["workset_id"]
            task_id = initial["task_id"]
            self.close_failed(profile, workset_id=workset_id, task_id=task_id)
            initial_terminal = load_runtime_state(profile.paths).worksets[0].attempts[-1]
            with patch.object(backlog, "now_iso", return_value=initial_terminal.ended_at):
                wtam.cancel_task(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor="cycle-actor",
                    summary="Cancel the failed first cycle before reopening",
                )
                wtam.reopen_task(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    actor="cycle-actor",
                    summary="First cycle reopen",
                )
            reopen_event = next(
                event
                for event in reversed(load_events(profile.paths.events_file))
                if event.get("type") == "task.reopen"
            )
            old_generation = reopen_event["payload"]["updated_at"]
            self.assertNotEqual(old_generation, initial_terminal.ended_at)
            first_resume = self.begin(
                profile,
                prompt_file,
                workset_id=workset_id,
                task_id=task_id,
                actor="cycle-actor",
            )
            first_resume_id = first_resume["worktree"]["attempt_id"]
            self.close_failed(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                actor="cycle-actor",
            )
            terminal_first_resume = load_runtime_state(profile.paths).worksets[0].attempts[-1]
            self.assertEqual(terminal_first_resume.attempt_id, first_resume_id)
            second_resume = self.begin(
                profile,
                prompt_file,
                workset_id=workset_id,
                task_id=task_id,
                actor="cycle-actor",
            )
            runtime_attempts = load_runtime_state(profile.paths).worksets[0].attempts
            self.assertEqual(len(runtime_attempts), 3)
            successor = runtime_attempts[-1]
            atomic = successor.setup_receipt["atomic_start"]
            self.assertEqual(
                atomic["expected_predecessor_attempt_id"],
                terminal_first_resume.attempt_id,
            )
            self.assertEqual(
                atomic["expected_task_updated_at"],
                terminal_first_resume.ended_at,
            )
            self.assertNotEqual(atomic["expected_task_updated_at"], old_generation)
            self.assertEqual(second_resume["worktree"]["attempt_id"], successor.attempt_id)
            self.close_failed(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                actor="cycle-actor",
            )

    def test_duplicate_predecessor_terminal_boundary_is_zero_mutation(self) -> None:
        with self.start_repo() as (root, profile, prompt_file):
            initial = self.begin(profile, prompt_file)
            workset_id = initial["workset_id"]
            task_id = initial["task_id"]
            backlog.finish_task(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=initial["worktree"]["attempt_id"],
                actor="codex",
                status="failed",
                summary="Create a legacy terminal predecessor without a close ledger.",
            )
            finish = next(
                event
                for event in reversed(load_events(profile.paths.events_file))
                if event.get("type") == "task.finish"
                and event.get("payload", {}).get("attempt_id")
                == initial["worktree"]["attempt_id"]
            )
            backlog.append_event(
                profile.paths.events_file,
                event_type="task.finish",
                actor=str(finish["actor"]),
                payload=dict(finish["payload"]),
            )
            before = self.durable_snapshot(root, profile)
            blocked = self.begin(
                profile,
                prompt_file,
                workset_id=workset_id,
                task_id=task_id,
            )
            self.assertEqual(blocked.operation_status, "blocked", blocked.to_dict())
            self.assertFalse(blocked.mutation_started)
            self.assertEqual(
                blocked.next_action.action_id,
                "task_start_proof_required",
            )
            self.assertEqual(
                blocked.next_action.reason_code,
                "task_start_evidence_conflict",
            )
            self.assertEqual(
                len(load_runtime_state(profile.paths).worksets[0].attempts),
                1,
            )
            self.assertEqual(self.durable_snapshot(root, profile), before)

    def test_legacy_missing_terminal_boundary_uses_bounded_fallback(self) -> None:
        with self.start_repo() as (root, profile, prompt_file):
            initial = self.begin(profile, prompt_file)
            workset_id = initial["workset_id"]
            task_id = initial["task_id"]
            initial_attempt_id = initial["worktree"]["attempt_id"]
            backlog.finish_task(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                attempt_id=initial_attempt_id,
                actor="codex",
                status="failed",
                summary="Create legacy terminal state before bounded fallback.",
            )
            retained_events = [
                event
                for event in load_events(profile.paths.events_file)
                if not (
                    event.get("type") == "task.finish"
                    and event.get("payload", {}).get("attempt_id") == initial_attempt_id
                )
            ]
            profile.paths.events_file.write_text(
                "".join(
                    json.dumps(event, sort_keys=True) + "\n"
                    for event in retained_events
                ),
                encoding="utf-8",
            )
            predecessor = load_runtime_state(profile.paths).worksets[0].attempts[-1]
            before = self.durable_snapshot(root, profile)
            self.assertEqual(
                backlog.resume_predecessor_identity(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    predecessor=predecessor,
                ),
                (predecessor.actor, predecessor.ended_at),
            )
            self.assertEqual(self.durable_snapshot(root, profile), before)
            resumed = self.begin(
                profile,
                prompt_file,
                workset_id=workset_id,
                task_id=task_id,
            )
            self.assertEqual(resumed.operation_status, "succeeded")
            self.assertTrue(resumed.mutation_completed)
            self.close_failed(profile, workset_id=workset_id, task_id=task_id)

    def test_terminal_operations_wait_for_exact_start_repair(self) -> None:
        for operation in ("close", "land", "cancel"):
            with self.subTest(operation=operation), self.start_repo() as (
                root,
                profile,
                prompt_file,
            ):
                workset_id, task_id, successor, _partial = (
                    self.reserve_ordinary_without_product_event(profile, prompt_file)
                )
                before = self.core_git_snapshot(root, profile)
                observations_before = read_lifecycle_observability(profile).observations
                if operation == "close":
                    blocked = wtam.close_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="codex",
                        status="failed",
                        summary="Must not close before start repair",
                        cleanup=True,
                        cwd=root,
                    )
                elif operation == "land":
                    blocked = wtam.land_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="codex",
                        summary="Must not land before start repair",
                        validations=(ValidationRecord(name="unit", status="passed"),),
                        cleanup=True,
                        cwd=root,
                    )
                else:
                    blocked = wtam.cancel_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="codex",
                        summary="Must not cancel before start repair",
                    )
                self.assertEqual(blocked.operation, f"task.{operation}")
                self.assertEqual(blocked.operation_status, "blocked", blocked.to_dict())
                self.assertFalse(blocked.mutation_started)
                self.assertFalse(blocked.mutation_completed)
                self.assertEqual(blocked.mutation_phase, "none")
                self.assertEqual(
                    blocked.next_action.action_id,
                    "repair_task_start_evidence",
                )
                self.assertIsNotNone(blocked.next_action.action)
                self.assertEqual(self.core_git_snapshot(root, profile), before)
                observations_after = read_lifecycle_observability(profile).observations
                self.assertEqual(observations_after, observations_before + 1)

                common = (
                    "--project-root",
                    str(root),
                    "--workset",
                    workset_id,
                    "--task",
                    task_id,
                    "--actor",
                    "codex",
                )
                if operation == "close":
                    cli_argv = (
                        "task",
                        "close",
                        *common,
                        "--status",
                        "failed",
                        "--summary",
                        "Must not close before start repair",
                    )
                    result_key = "closure"
                elif operation == "land":
                    cli_argv = (
                        "task",
                        "land",
                        *common,
                        "--summary",
                        "Must not land before start repair",
                        "--validation",
                        "unit=passed",
                    )
                    result_key = "landing"
                else:
                    cli_argv = (
                        "task",
                        "cancel",
                        *common,
                        "--summary",
                        "Must not cancel before start repair",
                    )
                    result_key = "task_state"

                exit_code, stdout, stderr = self.run_cli(
                    *cli_argv,
                    "--json",
                    cwd=root,
                )
                self.assertEqual(exit_code, 1)
                self.assertEqual(stderr, "")
                cli_blocked = json.loads(stdout)[result_key]
                self.assertEqual(cli_blocked["operation_status"], "blocked")
                self.assertFalse(cli_blocked["mutation_started"])
                self.assertEqual(
                    cli_blocked["next_action"],
                    blocked.next_action.to_dict(),
                )
                self.assertEqual(self.core_git_snapshot(root, profile), before)
                self.assertEqual(
                    read_lifecycle_observability(profile).observations,
                    observations_after,
                )

                exit_code, stdout, stderr = self.run_cli(*cli_argv, cwd=root)
                self.assertEqual(exit_code, 1)
                self.assertEqual(stderr, "")
                self.assertIn("operation status: blocked", stdout)
                self.assertIn("next action: repair_task_start_evidence", stdout)
                self.assertIn(
                    f"next command: {blocked.next_action.rendered_command}",
                    stdout,
                )
                self.assertEqual(self.core_git_snapshot(root, profile), before)
                self.assertEqual(
                    read_lifecycle_observability(profile).observations,
                    observations_after,
                )
                if operation == "land":
                    with patch(
                        "blackdog.observability.observe_lifecycle",
                        side_effect=OSError("telemetry unavailable"),
                    ):
                        fail_open = wtam.land_task(
                            profile,
                            workset_id=workset_id,
                            task_id=task_id,
                            actor="codex",
                            summary="Telemetry failure must not change the blocker",
                            validations=(
                                ValidationRecord(name="unit", status="passed"),
                            ),
                            cleanup=True,
                            cwd=root,
                        )
                    self.assertEqual(fail_open.operation_status, "blocked")
                    self.assertEqual(
                        fail_open.next_action.action_id,
                        "repair_task_start_evidence",
                    )
                    self.assertEqual(self.core_git_snapshot(root, profile), before)
                repaired = self.begin(
                    profile,
                    prompt_file,
                    workset_id=workset_id,
                    task_id=task_id,
                )
                self.assertEqual(repaired.operation_status, "succeeded")
                if operation == "close":
                    self.close_failed(profile, workset_id=workset_id, task_id=task_id)
                    self.assertEqual(
                        load_runtime_state(profile.paths).worksets[0].attempts[-1].status,
                        "failed",
                    )
                elif operation == "land":
                    workspace = Path(str(successor.worktree_path))
                    (workspace / "landed.txt").write_text("landed\n", encoding="utf-8")
                    landed = wtam.land_task(
                        profile,
                        workset_id=workset_id,
                        task_id=task_id,
                        actor="codex",
                        summary="Land after exact start repair",
                        validations=(ValidationRecord(name="unit", status="passed"),),
                        cleanup=True,
                        cwd=root,
                    )
                    self.assertEqual(landed["status"], "success")
                else:
                    self.close_failed(profile, workset_id=workset_id, task_id=task_id)
                    self.assertEqual(
                        load_runtime_state(profile.paths).worksets[0].attempts[-1].status,
                        "failed",
                    )

    def test_concurrent_exact_retries_share_one_successor_and_workspace(self) -> None:
        with self.start_repo() as (root, profile, prompt_file):
            workset_id, task_id, successor, _partial = (
                self.reserve_ordinary_without_product_event(profile, prompt_file)
            )
            workspace = Path(str(successor.worktree_path))
            subprocess.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(workspace)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "branch", "-D", str(successor.branch)],
                check=True,
                capture_output=True,
                text=True,
            )
            barrier = threading.Barrier(3)
            results: list[object] = []
            errors: list[BaseException] = []

            def retry() -> None:
                try:
                    barrier.wait(timeout=10)
                    results.append(
                        self.begin(
                            profile,
                            prompt_file,
                            workset_id=workset_id,
                            task_id=task_id,
                        )
                    )
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=retry) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=10)
            for thread in threads:
                thread.join(timeout=90)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertTrue(all(result.operation_status == "succeeded" for result in results))
            self.assertEqual(
                sorted(result["worktree"]["workspace_action"] for result in results),
                ["repaired", "reused"],
            )
            runtime_state = load_runtime_state(profile.paths)
            runtime_workset = runtime_state.worksets[0]
            self.assertEqual(
                sum(attempt.attempt_id == successor.attempt_id for attempt in runtime_workset.attempts),
                1,
            )
            self.assertEqual(len(runtime_workset.task_claims), 1)
            self.assert_one_start_event_each(profile, successor.attempt_id)
            worktree_listing = subprocess.run(
                ["git", "-C", str(root), "worktree", "list", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(worktree_listing.count(f"worktree {workspace}\n"), 1)
            self.assertTrue(workspace.is_dir())
            self.close_failed(profile, workset_id=workset_id, task_id=task_id)


if __name__ == "__main__":
    import unittest

    unittest.main()
