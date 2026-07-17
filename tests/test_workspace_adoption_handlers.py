from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import blackdog.handlers as handlers
import blackdog.wtam as wtam
from blackdog_core.profile import load_profile, render_default_profile
from blackdog_core.state import (
    append_event,
    create_prompt_receipt,
    default_runtime_state,
    save_runtime_state,
)
from tests.core_audit_support import REPO_ROOT


def _run(*args: str) -> None:
    subprocess.run(
        list(args),
        check=True,
        capture_output=True,
        text=True,
    )


def _artifact_state(path: Path) -> tuple[object, ...]:
    if path.is_symlink():
        return ("symlink", os.readlink(path))
    if path.is_file():
        return (
            "file",
            path.stat().st_mode & 0o777,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    if path.is_dir():
        return ("directory", path.stat().st_mode & 0o777)
    return ("missing",)


class WorkspaceAdoptionHandlerTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(
            prefix="blackdog-workspace-adoption-handlers-"
        )
        cls.base = Path(cls._temporary.name).resolve()
        cls.root = cls.base / "self-blackdog"
        shutil.copytree(
            REPO_ROOT,
            cls.root,
            ignore=shutil.ignore_patterns(
                ".git",
                ".VE",
                ".blackdog",
                "__pycache__",
                "*.pyc",
            ),
        )
        editable_source = cls.root / "fixture-editable" / "src" / "fixture_editable"
        editable_source.mkdir(parents=True)
        (editable_source / "__init__.py").write_text(
            'VALUE = "task-worktree"\n',
            encoding="utf-8",
        )
        _run("git", "init", "-b", "main", str(cls.root))
        _run(
            "git",
            "-C",
            str(cls.root),
            "config",
            "user.email",
            "blackdog@example.com",
        )
        _run(
            "git",
            "-C",
            str(cls.root),
            "config",
            "user.name",
            "Blackdog Test",
        )
        _run("git", "-C", str(cls.root), "add", ".")
        _run(
            "git",
            "-C",
            str(cls.root),
            "commit",
            "-m",
            "Create handler validation fixture",
        )

        _run(sys.executable, "-m", "venv", str(cls.root / ".VE"))
        cls.root_site_packages = next(
            (cls.root / ".VE" / "lib").glob("python*/site-packages")
        )
        cls.root_editable = (
            cls.root_site_packages / "__editable__.fixture_editable-0.1.pth"
        )
        cls.root_editable.write_text(
            str(cls.root / "fixture-editable" / "src") + "\n",
            encoding="utf-8",
        )
        cls.root_tool = cls.root / ".VE" / "bin" / "adoption-fixture-tool"
        cls.root_tool.write_text("#!/bin/sh\necho fixture\n", encoding="utf-8")
        cls.root_tool.chmod(0o755)

        cls.worktree = cls.base / "worktrees" / "handler-task"
        cls.worktree.parent.mkdir(parents=True)
        _run(
            "git",
            "-C",
            str(cls.root),
            "worktree",
            "add",
            "-b",
            "codex/adoption-handler-fixture",
            str(cls.worktree),
            "main",
        )
        cls.profile = load_profile(cls.root)
        setup = handlers.execute_worktree_handlers(
            cls.profile,
            worktree_path=cls.worktree,
        )
        if not setup.ready:
            raise RuntimeError(f"handler fixture setup failed: {setup.to_dict()}")

        cls.worktree_site_packages = next(
            (cls.worktree / ".VE" / "lib").glob("python*/site-packages")
        )
        cls.root_overlay = (
            cls.worktree_site_packages / "blackdog-root-overlay.pth"
        )
        cls.editable_overlay = (
            cls.worktree_site_packages / "blackdog-worktree-editables.pth"
        )
        cls.source_overlay = (
            cls.worktree_site_packages / "blackdog-worktree-source.pth"
        )
        cls.launcher = cls.worktree / ".VE" / "bin" / "blackdog"
        cls.worktree_python = cls.worktree / ".VE" / "bin" / "python"
        cls.fallback = (
            cls.worktree / ".VE" / "bin" / cls.root_tool.name
        )
        cls.wrong_origin_overlay = (
            cls.worktree_site_packages / "00-wrong-blackdog-origin.pth"
        )
        cls.prompt_path = cls.base / "adoption-prompt.md"
        cls.prompt_path.write_text(
            "Adopt the retained workspace without repairing its setup.\n",
            encoding="utf-8",
        )

        save_runtime_state(cls.profile.paths, default_runtime_state())
        append_event(
            cls.profile.paths.events_file,
            event_type="fixture.ready",
            actor="test",
            payload={"fixture": "workspace-adoption-handlers"},
        )

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(cls.root),
                "worktree",
                "remove",
                "--force",
                str(cls.worktree),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        cls._temporary.cleanup()

    def _setup_snapshot(self) -> dict[str, tuple[object, ...]]:
        expected = {
            self.root_editable,
            self.root_tool,
            self.root / ".VE" / "bin" / "python",
            self.root_overlay,
            self.editable_overlay,
            self.source_overlay,
            self.launcher,
            self.worktree_python,
            self.fallback,
            self.wrong_origin_overlay,
        }
        for directory in (
            self.root / ".VE" / "bin",
            self.worktree / ".VE" / "bin",
        ):
            expected.update(directory.iterdir())
        expected.update(self.root_site_packages.glob("*.pth"))
        expected.update(self.worktree_site_packages.glob("*.pth"))
        return {
            str(path.relative_to(self.base)): _artifact_state(path)
            for path in sorted(expected)
        }

    @contextmanager
    def _missing(self, path: Path):
        if path.is_symlink():
            saved = ("symlink", os.readlink(path), None)
        else:
            saved = (
                "file",
                path.read_bytes(),
                path.stat().st_mode & 0o777,
            )
        path.unlink()
        try:
            yield
        finally:
            if saved[0] == "symlink":
                path.symlink_to(saved[1])
            else:
                path.write_bytes(saved[1])
                path.chmod(saved[2])

    @contextmanager
    def _corrupt_text(self, path: Path, text: str):
        original = path.read_bytes()
        mode = path.stat().st_mode & 0o777
        path.write_text(text, encoding="utf-8")
        try:
            yield
        finally:
            path.write_bytes(original)
            path.chmod(mode)

    @contextmanager
    def _wrong_import_origin(self):
        self.assertFalse(self.wrong_origin_overlay.exists())
        self.wrong_origin_overlay.write_text(
            str(REPO_ROOT / "src") + "\n",
            encoding="utf-8",
        )
        try:
            yield
        finally:
            self.wrong_origin_overlay.unlink(missing_ok=True)

    @contextmanager
    def _without_test_runner_pythonpath(self):
        original = os.environ.pop("PYTHONPATH", None)
        try:
            yield
        finally:
            if original is not None:
                os.environ["PYTHONPATH"] = original

    def _assert_bytes_and_setup_unchanged(
        self,
        *,
        runtime_before: bytes,
        events_before: bytes,
        setup_before: dict[str, tuple[object, ...]],
    ) -> None:
        self.assertEqual(
            self.profile.paths.runtime_file.read_bytes(),
            runtime_before,
        )
        self.assertEqual(
            self.profile.paths.events_file.read_bytes(),
            events_before,
        )
        self.assertEqual(self._setup_snapshot(), setup_before)

    def _assert_adoption_rejected_read_only(self, *, action: str) -> None:
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        setup_before = self._setup_snapshot()

        with self._without_test_runner_pythonpath():
            summary = handlers.validate_existing_worktree_handlers(
                self.profile,
                worktree_path=self.worktree,
            )
        self.assertFalse(summary.ready, summary.to_dict())
        matching = [row for row in summary.actions if row.action == action]
        self.assertEqual(len(matching), 1, summary.to_dict())
        self.assertEqual(matching[0].status, handlers.HANDLER_STATUS_BLOCKED)

        prompt_receipt = create_prompt_receipt(
            self.prompt_path.read_text(encoding="utf-8"),
            source=str(self.prompt_path),
        )
        predecessor = SimpleNamespace(
            attempt_id="HANDLER-1-predecessor",
            task_id="HANDLER-1",
            actor="codex",
            note=None,
            prompt_receipt=prompt_receipt,
            user_prompt_receipt=prompt_receipt,
            setup_receipt={"fixture": "healthy-before-corruption"},
        )
        transaction = SimpleNamespace(transaction_id="handler-adoption-transaction")
        proof = {
            "worktree_path": str(self.worktree),
            "branch": "codex/adoption-handler-fixture",
            "target_branch": "main",
            "target_commit_at_adoption": "target-commit",
            "canonical_candidate": "candidate-commit",
        }
        with self._without_test_runner_pythonpath():
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        wtam,
                        "_require_workset_and_task",
                        return_value=(SimpleNamespace(), SimpleNamespace()),
                    )
                )
                runtime_token = object()
                stack.enter_context(
                    patch.object(wtam, "load_runtime_state", return_value=runtime_token)
                )
                stack.enter_context(
                    patch.object(wtam, "find_task_attempt", return_value=predecessor)
                )
                stack.enter_context(
                    patch.object(
                        wtam,
                        "load_landing_transaction",
                        return_value=transaction,
                    )
                )
                stack.enter_context(
                    patch.object(
                        wtam,
                        "_task_attempts_in_append_order",
                        return_value=(predecessor,),
                    )
                )
                stack.enter_context(
                    patch.object(
                        wtam,
                        "_prove_aborted_landing_source_adoption",
                        return_value=proof,
                    )
                )
                stack.enter_context(
                    patch.object(wtam, "_bounded_skill_provenance", return_value=None)
                )
                with self.assertRaisesRegex(wtam.WorktreeError, action):
                    wtam._adopt_aborted_landing_source_worktree(
                        self.profile,
                        workset_id="handler-workset",
                        task_id="HANDLER-1",
                        actor="codex",
                        incoming_execution=prompt_receipt,
                        incoming_request=prompt_receipt,
                        current_skill_provenance=None,
                        expected_actor="codex",
                        expected_execution_prompt_hash=prompt_receipt.prompt_hash,
                        expected_execution_prompt_mode=prompt_receipt.mode,
                        expected_request_prompt_hash=prompt_receipt.prompt_hash,
                        expected_request_prompt_mode=prompt_receipt.mode,
                        expected_predecessor_attempt=predecessor.attempt_id,
                        expected_landing_transaction=transaction.transaction_id,
                        expected_source_commit="source-commit",
                        expected_source_tree="source-tree",
                        expected_branch="codex/adoption-handler-fixture",
                        expected_path=str(self.worktree),
                        expected_target_branch="main",
                        expected_target_commit="target-commit",
                        cwd=self.root,
                        note=None,
                    )

        self._assert_bytes_and_setup_unchanged(
            runtime_before=runtime_before,
            events_before=events_before,
            setup_before=setup_before,
        )

    def test_healthy_retained_handler_setup_is_ready_and_read_only(self) -> None:
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        setup_before = self._setup_snapshot()

        with self._without_test_runner_pythonpath():
            summary = handlers.validate_existing_worktree_handlers(
                self.profile,
                worktree_path=self.worktree,
            )

        self.assertTrue(summary.ready, summary.to_dict())
        self.assertTrue(summary.actions)
        self.assertTrue(
            all(
                action.status == handlers.HANDLER_STATUS_VALIDATED
                for action in summary.actions
            ),
            summary.to_dict(),
        )
        self._assert_bytes_and_setup_unchanged(
            runtime_before=runtime_before,
            events_before=events_before,
            setup_before=setup_before,
        )

    def test_missing_worktree_editable_overlay_blocks_adoption_read_only(self) -> None:
        with self._missing(self.editable_overlay):
            self._assert_adoption_rejected_read_only(
                action="validate-worktree-editables"
            )

    def test_missing_owned_root_bin_fallback_blocks_adoption_read_only(self) -> None:
        with self._missing(self.fallback):
            self._assert_adoption_rejected_read_only(
                action="validate-root-bin-fallback"
            )

    def test_corrupt_root_overlay_blocks_adoption_read_only(self) -> None:
        with self._corrupt_text(self.root_overlay, "/wrong/root/site-packages\n"):
            self._assert_adoption_rejected_read_only(action="validate-root-overlay")

    def test_corrupt_blackdog_launcher_blocks_adoption_read_only(self) -> None:
        with self._corrupt_text(self.launcher, "#!/bin/sh\nexit 99\n"):
            self._assert_adoption_rejected_read_only(
                action="validate-blackdog-launcher"
            )

    def test_missing_worktree_source_overlay_blocks_adoption_read_only(self) -> None:
        with self._missing(self.source_overlay):
            self._assert_adoption_rejected_read_only(
                action="validate-worktree-source-overlay"
            )

    def test_missing_worktree_python_blocks_adoption_read_only(self) -> None:
        with self._missing(self.worktree_python):
            self._assert_adoption_rejected_read_only(
                action="validate-worktree-python"
            )

    def test_wrong_blackdog_import_origin_blocks_adoption_read_only(self) -> None:
        with self._wrong_import_origin():
            self._assert_adoption_rejected_read_only(
                action="validate-blackdog-import-origin"
            )


class ConsumerWorkspaceAdoptionHandlerTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary = tempfile.TemporaryDirectory(
            prefix="blackdog-consumer-adoption-handlers-"
        )
        cls.base = Path(cls._temporary.name).resolve()
        cls.root = cls.base / "consumer-repo"
        cls.root.mkdir()
        _run("git", "init", "-b", "main", str(cls.root))
        _run(
            "git",
            "-C",
            str(cls.root),
            "config",
            "user.email",
            "blackdog@example.com",
        )
        _run(
            "git",
            "-C",
            str(cls.root),
            "config",
            "user.name",
            "Blackdog Consumer Test",
        )
        (cls.root / ".gitignore").write_text(".VE/\n", encoding="utf-8")
        (cls.root / "blackdog.toml").write_text(
            render_default_profile("Consumer Handler Fixture"),
            encoding="utf-8",
        )
        _run("git", "-C", str(cls.root), "add", ".")
        _run(
            "git",
            "-C",
            str(cls.root),
            "commit",
            "-m",
            "Initialize consumer fixture",
        )
        cls.profile = load_profile(cls.root)
        _run(sys.executable, "-m", "venv", str(cls.root / ".VE"))

        cls.managed_source = cls.profile.paths.control_dir / "source" / "blackdog"
        cls.managed_source.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            REPO_ROOT,
            cls.managed_source,
            ignore=shutil.ignore_patterns(
                ".git",
                ".VE",
                ".blackdog",
                "__pycache__",
                "*.pyc",
            ),
        )
        _run("git", "init", "-b", "main", str(cls.managed_source))
        _run(
            "git",
            "-C",
            str(cls.managed_source),
            "config",
            "user.email",
            "blackdog@example.com",
        )
        _run(
            "git",
            "-C",
            str(cls.managed_source),
            "config",
            "user.name",
            "Managed Blackdog Source",
        )
        _run("git", "-C", str(cls.managed_source), "add", ".")
        _run(
            "git",
            "-C",
            str(cls.managed_source),
            "commit",
            "-m",
            "Create external managed Blackdog checkout",
        )

        cls.worktree = cls.base / "worktrees" / "consumer-task"
        cls.worktree.parent.mkdir(parents=True)
        _run(
            "git",
            "-C",
            str(cls.root),
            "worktree",
            "add",
            "-b",
            "codex/consumer-handler-fixture",
            str(cls.worktree),
            "main",
        )
        with patch.object(handlers, "_current_blackdog_source_root", return_value=None):
            setup = handlers.execute_worktree_handlers(
                cls.profile,
                worktree_path=cls.worktree,
            )
        if not setup.ready:
            raise RuntimeError(f"consumer handler setup failed: {setup.to_dict()}")
        if setup.source_mode != "managed-checkout":
            raise RuntimeError(f"consumer fixture did not use managed source: {setup.to_dict()}")

        cls.worktree_python = cls.worktree / ".VE" / "bin" / "python"
        cls.launcher = cls.worktree / ".VE" / "bin" / "blackdog"
        cls.worktree_site_packages = next(
            (cls.worktree / ".VE" / "lib").glob("python*/site-packages")
        )
        cls.root_overlay = (
            cls.worktree_site_packages / "blackdog-root-overlay.pth"
        )
        cls.prompt_path = cls.base / "consumer-adoption-prompt.md"
        cls.prompt_path.write_text(
            "Adopt the consumer workspace only with its exact managed launcher.\n",
            encoding="utf-8",
        )
        save_runtime_state(cls.profile.paths, default_runtime_state())
        append_event(
            cls.profile.paths.events_file,
            event_type="fixture.ready",
            actor="test",
            payload={"fixture": "consumer-workspace-adoption-handlers"},
        )

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(cls.root),
                "worktree",
                "remove",
                "--force",
                str(cls.worktree),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        cls._temporary.cleanup()

    def _setup_snapshot(self) -> dict[str, tuple[object, ...]]:
        paths = {
            self.root / ".VE" / "bin" / "python",
            self.worktree_python,
            self.launcher,
            self.root_overlay,
        }
        for directory in (
            self.root / ".VE" / "bin",
            self.worktree / ".VE" / "bin",
        ):
            paths.update(directory.iterdir())
        paths.update(self.worktree_site_packages.glob("*.pth"))
        return {
            str(path.relative_to(self.base)): _artifact_state(path)
            for path in sorted(paths)
        }

    @contextmanager
    def _corrupt_launcher(self):
        original = self.launcher.read_bytes()
        mode = stat.S_IMODE(self.launcher.stat().st_mode)
        self.launcher.write_text("#!/bin/sh\nexit 91\n", encoding="utf-8")
        try:
            yield
        finally:
            self.launcher.write_bytes(original)
            self.launcher.chmod(mode)

    def _managed_validation(self):
        with patch.object(handlers, "_current_blackdog_source_root", return_value=None):
            return handlers.validate_existing_worktree_handlers(
                self.profile,
                worktree_path=self.worktree,
            )

    def _assert_corrupt_launcher_rejected_before_mutation(self) -> None:
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        setup_before = self._setup_snapshot()
        summary = self._managed_validation()
        self.assertFalse(summary.ready, summary.to_dict())
        launcher_actions = [
            action
            for action in summary.actions
            if action.action == "validate-blackdog-launcher"
        ]
        self.assertEqual(len(launcher_actions), 1, summary.to_dict())
        self.assertEqual(
            launcher_actions[0].status,
            handlers.HANDLER_STATUS_BLOCKED,
        )

        prompt_receipt = create_prompt_receipt(
            self.prompt_path.read_text(encoding="utf-8"),
            source=str(self.prompt_path),
        )
        predecessor = SimpleNamespace(
            attempt_id="CONSUMER-1-predecessor",
            task_id="CONSUMER-1",
            actor="codex",
            note=None,
            prompt_receipt=prompt_receipt,
            user_prompt_receipt=prompt_receipt,
            setup_receipt={"fixture": "managed-consumer"},
        )
        transaction = SimpleNamespace(
            transaction_id="consumer-handler-adoption-transaction"
        )
        proof = {
            "worktree_path": str(self.worktree),
            "branch": "codex/consumer-handler-fixture",
            "target_branch": "main",
            "target_commit_at_adoption": "target-commit",
            "canonical_candidate": "candidate-commit",
        }
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(handlers, "_current_blackdog_source_root", return_value=None)
            )
            stack.enter_context(
                patch.object(
                    wtam,
                    "_require_workset_and_task",
                    return_value=(SimpleNamespace(), SimpleNamespace()),
                )
            )
            stack.enter_context(
                patch.object(wtam, "load_runtime_state", return_value=object())
            )
            stack.enter_context(
                patch.object(wtam, "find_task_attempt", return_value=predecessor)
            )
            stack.enter_context(
                patch.object(
                    wtam,
                    "load_landing_transaction",
                    return_value=transaction,
                )
            )
            stack.enter_context(
                patch.object(
                    wtam,
                    "_task_attempts_in_append_order",
                    return_value=(predecessor,),
                )
            )
            stack.enter_context(
                patch.object(
                    wtam,
                    "_prove_aborted_landing_source_adoption",
                    return_value=proof,
                )
            )
            stack.enter_context(
                patch.object(wtam, "_bounded_skill_provenance", return_value=None)
            )
            with self.assertRaisesRegex(
                wtam.WorktreeError,
                "validate-blackdog-launcher",
            ):
                wtam._adopt_aborted_landing_source_worktree(
                    self.profile,
                    workset_id="consumer-workset",
                    task_id="CONSUMER-1",
                    actor="codex",
                    incoming_execution=prompt_receipt,
                    incoming_request=prompt_receipt,
                    current_skill_provenance=None,
                    expected_actor="codex",
                    expected_execution_prompt_hash=prompt_receipt.prompt_hash,
                    expected_execution_prompt_mode=prompt_receipt.mode,
                    expected_request_prompt_hash=prompt_receipt.prompt_hash,
                    expected_request_prompt_mode=prompt_receipt.mode,
                    expected_predecessor_attempt=predecessor.attempt_id,
                    expected_landing_transaction=transaction.transaction_id,
                    expected_source_commit="source-commit",
                    expected_source_tree="source-tree",
                    expected_branch="codex/consumer-handler-fixture",
                    expected_path=str(self.worktree),
                    expected_target_branch="main",
                    expected_target_commit="target-commit",
                    cwd=self.root,
                    note=None,
                )

        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)
        self.assertEqual(self._setup_snapshot(), setup_before)

    def test_managed_consumer_launcher_and_import_origins_are_exact(self) -> None:
        runtime_before = self.profile.paths.runtime_file.read_bytes()
        events_before = self.profile.paths.events_file.read_bytes()
        setup_before = self._setup_snapshot()
        expected_launcher = handlers._blackdog_launcher_text(
            self.worktree_python,
            source_root=self.managed_source,
        )

        self.assertEqual(self.launcher.read_bytes(), expected_launcher.encode("utf-8"))
        self.assertEqual(stat.S_IMODE(self.launcher.stat().st_mode), 0o755)
        summary = self._managed_validation()
        self.assertTrue(summary.ready, summary.to_dict())
        self.assertEqual(summary.source_mode, "managed-checkout")

        completed = subprocess.run(
            [
                str(self.launcher),
                "summary",
                "--project-root",
                str(self.root),
                "--json",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        launcher_payload = json.loads(completed.stdout)
        self.assertEqual(
            launcher_payload["project_name"],
            "Consumer Handler Fixture",
        )

        inherited_pythonpath = os.environ.get("PYTHONPATH")
        launcher_pythonpath = str(self.managed_source / "src")
        if inherited_pythonpath:
            launcher_pythonpath += os.pathsep + inherited_pythonpath
        environment = dict(os.environ)
        environment["PYTHONPATH"] = launcher_pythonpath
        origins = subprocess.run(
            [
                str(self.worktree_python),
                "-c",
                (
                    "import pathlib, blackdog, blackdog_cli; "
                    "print(pathlib.Path(blackdog.__file__).resolve()); "
                    "print(pathlib.Path(blackdog_cli.__file__).resolve())"
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        ).stdout.splitlines()
        expected_import_root = (self.managed_source / "src").resolve()
        self.assertEqual(len(origins), 2)
        self.assertTrue(
            all(
                Path(origin).resolve().is_relative_to(expected_import_root)
                for origin in origins
            ),
            origins,
        )
        self.assertEqual(self.profile.paths.runtime_file.read_bytes(), runtime_before)
        self.assertEqual(self.profile.paths.events_file.read_bytes(), events_before)
        self.assertEqual(self._setup_snapshot(), setup_before)

    def test_corrupt_managed_consumer_launcher_blocks_before_mutation(self) -> None:
        with self._corrupt_launcher():
            self._assert_corrupt_launcher_rejected_before_mutation()


if __name__ == "__main__":
    unittest.main()
