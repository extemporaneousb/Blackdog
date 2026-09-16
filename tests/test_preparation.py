from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
import zipfile

from blackdog.handlers import execute_worktree_handlers, validate_existing_worktree_handlers
from blackdog.preparation import prepare_worktree
from blackdog.wtam import begin_task_worktree, land_task, show_task
from blackdog_core.profile import load_profile, render_default_profile
from blackdog_core.state import load_events, load_runtime_state, task_record
from blackdog_core.tasks import TaskError, create_task, record_task_setup, start_task
from tests.process_support import configure_test_git


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def tool(name: str, executable: str) -> dict[str, object]:
    version = subprocess.run([executable, "--version"], check=True, capture_output=True, text=True).stdout.strip()
    return {"name": name, "executable": executable, "version_args": ["--version"], "version": version}


def recipe_text(*, setup: list[list[str]], checks: list[list[str]], tracked: list[str], tools: list[dict[str, object]] | None = None, inputs: list[dict[str, object]] | None = None, timeout: int = 60) -> str:
    lines = [
        '[[handlers]]', 'id = "prepare"', 'kind = "worktree-preparation"', 'schema_version = 1',
        'revision = "fixture-v1"', f'timeout_seconds = {timeout}',
        f'tracked_inputs = {json.dumps(tracked)}', 'outputs = [".prepared", ".VE", "node_modules", "dist"]',
    ]
    for key, values in (
        ("tools", tools or [tool("python", "python3")]), ("inputs", inputs or []),
        ("setup", [{"name": f"setup-{i}", "argv": argv} for i, argv in enumerate(setup)]),
        ("checks", [{"name": f"check-{i}", "argv": argv} for i, argv in enumerate(checks)]),
    ):
        for row in values:
            lines.append(f"[[handlers.{key}]]")
            lines.extend(f"{name} = {json.dumps(value)}" for name, value in row.items())
    return "\n".join(lines) + "\n"


class PreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="blackdog-preparation-")
        self.base = Path(self.temporary.name)
        self.root = self.base / "primary with spaces"
        self.root.mkdir()
        git(self.root, "init", "-b", "main")
        configure_test_git(self.root)
        git(self.root, "config", "user.name", "Blackdog Test")
        git(self.root, "config", "user.email", "blackdog@example.com")
        self.write(".gitignore", ".prepared/\n.VE/\nnode_modules/\ndist/\nlocal-input/\n__pycache__/\n")
        self.write("requirements.lock", "fixture dependencies only\n")
        self.write("source.txt", "primary\n")
        self.worktree = self.base / "task with spaces"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")

    def configure(self, **kwargs):
        profile = render_default_profile("Preparation fixture").split("[[handlers]]", 1)[0]
        self.write("blackdog.toml", profile + recipe_text(**kwargs))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-m", "Reviewed fixture recipe")
        self.profile = load_profile(self.root)
        return self.profile

    def simple(self, *, setup: str | None = None, check: str | None = None, inputs=None, timeout=60):
        self.configure(
            setup=[["{python}", "-c", setup or "from pathlib import Path; Path('.prepared/result').write_text('ready')"]],
            checks=[["{python}", "-c", check or "from pathlib import Path; assert Path('.prepared/result').read_text() == 'ready'"]],
            tracked=["requirements.lock"], inputs=inputs, timeout=timeout,
        )
        self.checkout()

    def checkout(self):
        git(self.root, "worktree", "add", "-b", "task-fixture", str(self.worktree), "main")

    def run_setup(self):
        return execute_worktree_handlers(self.profile, worktree_path=self.worktree)

    def verify(self):
        return validate_existing_worktree_handlers(self.profile, worktree_path=self.worktree)

    def test_cold_setup_warm_verification_and_source_drift(self):
        self.simple(check="from pathlib import Path; assert Path('.prepared/result').read_text() == 'ready'; assert Path('source.txt').read_text().strip() in {'primary', 'task'}")
        first = self.run_setup()
        self.assertTrue(first.ready, first.remediation)
        self.assertEqual(first.preparation[0]["reuse"], "none")
        (self.worktree / "source.txt").write_text("task\n")
        second = self.verify()
        self.assertTrue(second.ready, second.remediation)
        self.assertEqual(second.preparation[0]["reuse"], "verified-worktree")
        self.assertNotEqual(first.preparation[0]["source"], second.preparation[0]["source"])
        self.assertEqual((self.root / "source.txt").read_text(), "primary\n")
        self.assertFalse((self.root / ".prepared").exists())

    def test_declared_input_is_copied_with_permissions_and_drift_blocks(self):
        data = b"fixture input, not a credential\n"
        self.write("local-input/value", data.decode())
        self.simple(inputs=[{"source": "local-input/value", "destination": ".prepared/input", "sha256": hashlib.sha256(data).hexdigest(), "mode": 0o600}])
        self.assertTrue(self.run_setup().ready)
        copied = self.worktree / ".prepared/input"
        self.assertEqual(copied.read_bytes(), data)
        self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
        (self.root / "local-input/value").write_text("changed")
        result = self.verify()
        self.assertFalse(result.ready)
        self.assertIn("digest differs", result.remediation)

    def test_missing_explicit_input_blocks_without_effects(self):
        self.simple(inputs=[{"source": "local-input/missing", "destination": ".prepared/input", "sha256": "0" * 64, "mode": 0o600}])
        result = self.run_setup()
        self.assertFalse(result.ready)
        self.assertFalse((self.worktree / ".prepared").exists())

    def test_changed_lockfile_output_tool_or_recipe_cannot_reuse(self):
        self.simple()
        self.assertTrue(self.run_setup().ready)
        lock = self.worktree / "requirements.lock"
        original = lock.read_text()
        lock.write_text("changed\n")
        self.assertIn("inputs", self.verify().remediation)
        lock.write_text(original)
        artifact = self.worktree / ".prepared/result"
        artifact.write_text("tampered")
        self.assertIn("outputs changed", self.verify().remediation)
        artifact.write_text("ready")
        config = self.profile.handlers[0]
        altered = replace(config, tools=(replace(config.tools[0], version="unsupported"),))
        result = prepare_worktree(self.profile, altered, worktree=self.worktree, execute=False)
        self.assertIn("version does not match", result.reason)
        recipe = self.worktree / "blackdog.toml"
        recipe.write_text(recipe.read_text().replace("fixture-v1", "fixture-v2"))
        self.assertIn("recipe_sha256", self.verify().remediation)

    def test_setup_failure_retains_intent_and_never_replays(self):
        self.simple(setup="from pathlib import Path; Path('.prepared/once').write_text('effect'); raise SystemExit(7)")
        result = self.run_setup()
        self.assertFalse(result.ready)
        self.assertIn("status 7", result.remediation)
        self.assertEqual((self.worktree / ".prepared/once").read_text(), "effect")
        self.assertIn("indeterminate", self.run_setup().remediation)

    def test_input_change_during_setup_cannot_publish_readiness(self):
        self.simple(setup="from pathlib import Path; Path('.prepared/result').write_text('ready'); Path('requirements.lock').write_text('changed')")
        result = self.run_setup()
        self.assertFalse(result.ready)
        self.assertIn("changed during preparation", result.remediation)
        self.assertFalse(Path(result.preparation[0]["evidence_path"]).exists())

    def test_timeout_is_indeterminate(self):
        self.simple(setup="import time; time.sleep(5)", timeout=1)
        started = time.monotonic()
        result = self.run_setup()
        self.assertFalse(result.ready)
        self.assertLess(time.monotonic() - started, 4)
        self.assertIn("timed out", result.remediation)
        self.assertIn("indeterminate", self.verify().remediation)

    def test_interrupted_effect_or_completion_publication_never_replays(self):
        from blackdog import preparation
        self.simple()
        original = preparation._run
        def interrupt_after_effect(argv, **kwargs):
            result = original(argv, **kwargs)
            if "write_text('ready')" in argv[-1]:
                raise KeyboardInterrupt()
            return result
        with patch.object(preparation, "_run", side_effect=interrupt_after_effect):
            interrupted = self.run_setup()
        self.assertFalse(interrupted.ready)
        self.assertIn("interrupted", interrupted.remediation)
        self.assertEqual((self.worktree / ".prepared/result").read_text(), "ready")
        self.assertIn("indeterminate", self.run_setup().remediation)

    def test_completion_publication_failure_blocks_existing_effects(self):
        from blackdog import preparation
        self.simple()
        write = preparation.atomic_write_text
        def fail_completion(path, text):
            if path.name == "completed.json":
                raise OSError("injected completion publication failure")
            write(path, text)
        with patch.object(preparation, "atomic_write_text", side_effect=fail_completion):
            result = self.run_setup()
        self.assertFalse(result.ready)
        self.assertIn("publication failure", result.remediation)
        self.assertIn("indeterminate", self.verify().remediation)

    def test_readiness_failure_after_source_edit_does_not_reuse_old_success(self):
        self.simple(check="from pathlib import Path; assert Path('source.txt').read_text().strip() == 'primary'")
        self.assertTrue(self.run_setup().ready)
        (self.worktree / "source.txt").write_text("different source\n")
        result = self.verify()
        self.assertFalse(result.ready)
        self.assertEqual(result.preparation[0]["status"], "blocked")
        self.assertIn("readiness/check-0", result.remediation)

    def test_commands_receive_no_inherited_private_environment(self):
        self.simple(check="import os; assert 'BLACKDOG_TEST_PRIVATE_VALUE' not in os.environ; assert 'PYTHONPATH' not in os.environ")
        with patch.dict(os.environ, {"BLACKDOG_TEST_PRIVATE_VALUE": "fixture-private-value", "PYTHONPATH": "unrelated-location"}):
            result = self.run_setup()
        self.assertTrue(result.ready, result.remediation)
        self.assertNotIn("fixture-private-value", json.dumps(result.to_dict()))

    def test_owned_output_symlink_and_unowned_existing_output_are_rejected(self):
        self.simple()
        (self.worktree / ".prepared").symlink_to(self.root)
        result = self.run_setup()
        self.assertFalse(result.ready)
        self.assertIn("escapes", result.remediation)
        (self.worktree / ".prepared").unlink()
        (self.worktree / ".prepared").mkdir()
        self.assertIn("already exist", self.run_setup().remediation)

    def test_output_symlink_cannot_attach_undeclared_ignored_file(self):
        self.simple(setup="from pathlib import Path; Path('.prepared/result').write_text('ready'); Path('.prepared/dependency').symlink_to('../local-input/dependency')")
        (self.worktree / "local-input").mkdir()
        dependency = self.worktree / "local-input/dependency"
        dependency.write_text("unqualified mutable bytes")
        result = self.run_setup()
        self.assertFalse(result.ready)
        self.assertIn("unqualified state", result.remediation)
        self.assertFalse(Path(result.preparation[0]["evidence_path"]).exists())
        dependency.write_text("changed unqualified bytes")
        self.assertFalse(self.verify().ready)

    def test_output_symlink_cannot_attach_directory_containing_tracked_and_ignored_files(self):
        self.write("source/visible.py", "VALUE = 1\n")
        self.simple(setup="from pathlib import Path; Path('.prepared/result').write_text('ready'); Path('.prepared/dependency').symlink_to('../source', target_is_directory=True)")
        hidden = self.worktree / "source/__pycache__"
        hidden.mkdir()
        (hidden / "ignored.py").write_text("VALUE = 2\n")
        result = self.run_setup()
        self.assertFalse(result.ready)
        self.assertIn("unqualified state", result.remediation)

    def test_output_symlinks_to_owned_outputs_and_exact_tracked_files_remain_qualified(self):
        self.simple(
            setup="from pathlib import Path; Path('.prepared/result').write_text('ready'); Path('.prepared/dependency').symlink_to('result'); Path('.prepared/source').symlink_to('../source.txt')",
            check="from pathlib import Path; assert Path('.prepared/dependency').read_text() == 'ready'; assert Path('.prepared/source').read_text().strip() == 'primary'",
        )
        self.assertTrue(self.run_setup().ready)
        self.assertTrue(self.verify().ready)
        (self.worktree / "source.txt").write_text("changed tracked source\n")
        self.assertFalse(self.verify().ready)

    def test_two_concurrent_requests_publish_once(self):
        self.simple(setup="from pathlib import Path; import time; time.sleep(.1); Path('.prepared/result').write_text('ready')")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_setup(), range(2)))
        self.assertTrue(all(result.ready for result in results), [r.remediation for r in results])
        self.assertEqual(sorted(result.preparation[0]["reuse"] for result in results), ["none", "verified-worktree"])

    def test_begin_failure_keeps_canonical_attempt_and_retry_blocks(self):
        self.configure(
            setup=[["{python}", "-c", "from pathlib import Path; Path('.prepared/effect').write_text('retained'); raise SystemExit(2)"]],
            checks=[["{python}", "-c", "pass"]], tracked=["requirements.lock"],
        )
        first = begin_task_worktree(self.profile, actor="codex", prompt="Prepare fixture", path=str(self.worktree), cwd=self.root)
        self.assertEqual(first.operation_status, "blocked")
        state = load_runtime_state(self.profile.paths)
        self.assertEqual(len(state.tasks), 1)
        attempt = state.tasks[0].attempts[0]
        self.assertEqual(attempt.status, "in_progress")
        self.assertEqual(attempt.setup_receipt["status"], "blocked")
        self.assertEqual(attempt.setup_receipt["preparation"][0]["attempt_id"], attempt.attempt_id)
        self.assertEqual((self.worktree / ".prepared/effect").read_text(), "retained")
        before = self.profile.paths.runtime_file.read_bytes(), self.profile.paths.events_file.read_bytes()
        shown = show_task(self.profile, task_id=state.tasks[0].task_id, cwd=self.root)
        self.assertEqual(shown.next_action.kind, "blocked")
        landed = land_task(self.profile, task_id=state.tasks[0].task_id, actor="codex", summary="Must remain blocked", cwd=self.root)
        self.assertEqual(landed.next_action.kind, "blocked")
        self.assertEqual(landed.next_action.action_id, "handler_setup_invalid")
        self.assertEqual(before, (self.profile.paths.runtime_file.read_bytes(), self.profile.paths.events_file.read_bytes()))
        replay = begin_task_worktree(self.profile, actor="codex", prompt="Prepare fixture", task_id=state.tasks[0].task_id, cwd=self.root)
        self.assertEqual(replay.operation_status, "blocked")
        self.assertIn("indeterminate", replay.next_action.reason_detail)

    def test_begin_success_exact_retry_noop_and_source_drift_updates_receipt(self):
        self.configure(
            setup=[["{python}", "-c", "from pathlib import Path; Path('.prepared/result').write_text('ready')"]],
            checks=[["{python}", "-c", "from pathlib import Path; assert Path('.prepared/result').read_text() == 'ready'"]],
            tracked=["requirements.lock"],
        )
        first = begin_task_worktree(self.profile, actor="codex", prompt="Prepare fixture", path=str(self.worktree), cwd=self.root)
        self.assertEqual(first.operation_status, "succeeded")
        task = load_runtime_state(self.profile.paths).tasks[0]
        before = (self.profile.paths.runtime_file.read_bytes(), self.profile.paths.events_file.read_bytes())
        second = begin_task_worktree(self.profile, actor="codex", prompt="Prepare fixture", task_id=task.task_id, cwd=self.root)
        self.assertEqual(second.operation_status, "succeeded")
        self.assertEqual(before, (self.profile.paths.runtime_file.read_bytes(), self.profile.paths.events_file.read_bytes()))
        (self.worktree / "source.txt").write_text("edited task\n")
        third = begin_task_worktree(self.profile, actor="codex", prompt="Prepare fixture", task_id=task.task_id, cwd=self.root)
        self.assertEqual(third.operation_status, "succeeded")
        latest = task_record(load_runtime_state(self.profile.paths), task.task_id).attempts[0]
        self.assertIsNotNone(latest.setup_receipt["setup_measurement"]["value"])
        self.assertNotEqual(task.attempts[0].setup_receipt["preparation"][0]["source"], latest.setup_receipt["preparation"][0]["source"])
        recipe = self.worktree / "blackdog.toml"
        recipe.write_text(recipe.read_text().replace('kind = "worktree-preparation"', 'enabled = false\nkind = "worktree-preparation"'))
        for _ in range(2):
            blocked = begin_task_worktree(self.profile, actor="codex", prompt="Prepare fixture", task_id=task.task_id, cwd=self.root)
            self.assertEqual(blocked.operation_status, "blocked")
            self.assertIn("removed or disabled", blocked.next_action.reason_detail)

    def test_setup_receipt_compare_and_set_and_event_failure_repair(self):
        self.configure(setup=[["{python}", "-c", "pass"]], checks=[["{python}", "-c", "pass"]], tracked=["requirements.lock"])
        task = create_task(self.profile, title="Setup evidence")
        pending = {"schema_version": 2, "status": "blocked", "preparation": []}
        attempt = start_task(self.profile, task_id=task.task_id, actor="codex", setup_receipt=pending)
        completed = {"schema_version": 2, "status": "ok", "preparation": [{"schema_version": 1, "status": "ready"}]}
        from blackdog_core import tasks
        append = tasks.append_event_once
        def fail_completed(*args, **kwargs):
            if kwargs["payload"].get("setup_receipt") == completed:
                raise OSError("injected event failure")
            return append(*args, **kwargs)
        with patch.object(tasks, "append_event_once", side_effect=fail_completed):
            with self.assertRaisesRegex(OSError, "injected"):
                record_task_setup(self.profile, task_id=task.task_id, attempt_id=attempt.attempt_id, actor="codex", expected_receipt=pending, setup_receipt=completed)
        self.assertEqual(task_record(load_runtime_state(self.profile.paths), task.task_id).attempts[0].setup_receipt, completed)
        for _ in range(2):
            record_task_setup(self.profile, task_id=task.task_id, attempt_id=attempt.attempt_id, actor="codex", expected_receipt=pending, setup_receipt=completed)
        events = [event for event in load_events(self.profile.paths.events_file) if event["type"] == "task.setup" and event["payload"]["setup_receipt"] == completed]
        self.assertEqual(len(events), 1)
        with self.assertRaisesRegex(TaskError, "changed during verification"):
            record_task_setup(self.profile, task_id=task.task_id, attempt_id=attempt.attempt_id, actor="codex", expected_receipt=pending, setup_receipt={**completed, "status": "blocked"})
        with self.assertRaisesRegex(TaskError, "actor"):
            record_task_setup(self.profile, task_id=task.task_id, attempt_id=attempt.attempt_id, actor="other", expected_receipt=completed, setup_receipt=completed)

    def test_start_event_failure_repairs_claim_before_pending_setup_blocks(self):
        from blackdog_core import tasks
        self.configure(setup=[["{python}", "-c", "pass"]], checks=[["{python}", "-c", "pass"]], tracked=["requirements.lock"])
        append = tasks.append_event_once
        def fail_start(*args, **kwargs):
            if kwargs.get("event_type") == "task.start":
                raise OSError("injected start publication failure")
            return append(*args, **kwargs)
        with patch.object(tasks, "append_event_once", side_effect=fail_start):
            first = begin_task_worktree(self.profile, actor="codex", prompt="Prepare fixture", path=str(self.worktree), cwd=self.root)
        self.assertEqual(first.operation_status, "partial")
        self.assertEqual(first.next_action.action_id, "retry_task_start_finalization")
        task = load_runtime_state(self.profile.paths).tasks[0]
        self.assertFalse((self.worktree / ".prepared").exists())
        second = begin_task_worktree(self.profile, actor="codex", prompt="Prepare fixture", task_id=task.task_id, cwd=self.root)
        self.assertEqual(second.operation_status, "blocked")
        self.assertEqual(sum(event["type"] == "task.start" for event in load_events(self.profile.paths.events_file)), 1)
        self.assertFalse((self.worktree / ".prepared").exists())

    def test_recipe_admission_failure_invalidates_canonical_readiness(self):
        self.configure(
            setup=[["{python}", "-c", "from pathlib import Path; Path('.prepared/result').write_text('ready')"]],
            checks=[["{python}", "-c", "pass"]], tracked=["requirements.lock"],
        )
        first = begin_task_worktree(self.profile, actor="codex", prompt="Prepare fixture", path=str(self.worktree), cwd=self.root)
        self.assertEqual(first.operation_status, "succeeded")
        task = load_runtime_state(self.profile.paths).tasks[0]
        recipe = self.worktree / "blackdog.toml"
        original = recipe.read_text()
        for bad_content in (None, "[malformed", original.replace('kind = "worktree-preparation"\nschema_version = 1', 'kind = "worktree-preparation"\nschema_version = 99')):
            with self.subTest(bad_content=bad_content):
                if bad_content is None:
                    recipe.unlink()
                else:
                    recipe.write_text(bad_content)
                blocked = begin_task_worktree(self.profile, actor="codex", prompt="Prepare fixture", task_id=task.task_id, cwd=self.root)
                self.assertEqual(blocked.operation_status, "blocked")
                current = task_record(load_runtime_state(self.profile.paths), task.task_id).attempts[0]
                self.assertEqual(current.setup_receipt["status"], "blocked")
                self.assertEqual(current.setup_receipt["preparation"][0]["handler_id"], "prepare")
                self.assertEqual(show_task(self.profile, task_id=task.task_id, cwd=self.root).next_action.kind, "blocked")
                self.assertEqual(land_task(self.profile, task_id=task.task_id, actor="codex", summary="Must block", cwd=self.root).next_action.kind, "blocked")
                recipe.write_text(original)
                restored = begin_task_worktree(self.profile, actor="codex", prompt="Prepare fixture", task_id=task.task_id, cwd=self.root)
                self.assertEqual(restored.operation_status, "succeeded")
    def python_fixture(self):
        self.write("pyproject.toml", '''\
            [build-system]
            requires = []
            build-backend = "fixture_backend"
            backend-path = ["."]
            [project]
            name = "prepared-sample"
            version = "1.0"
        ''')
        self.write("fixture_backend.py", '''\
            from pathlib import Path
            import zipfile
            def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
                name = "prepared_sample-1.0-py3-none-any.whl"
                info = "prepared_sample-1.0.dist-info/"
                files = {
                    "prepared_sample.pth": str(Path.cwd()) + "\\n",
                    info + "METADATA": "Metadata-Version: 2.1\\nName: prepared-sample\\nVersion: 1.0\\n",
                    info + "WHEEL": "Wheel-Version: 1.0\\nGenerator: fixture\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n",
                    info + "entry_points.txt": "[console_scripts]\\nprepared-sample = sample:main\\n",
                }
                files[info + "RECORD"] = "".join(key + ",,\\n" for key in files) + info + "RECORD,,\\n"
                with zipfile.ZipFile(Path(wheel_directory) / name, "w") as wheel:
                    for key, value in files.items():
                        wheel.writestr(key, value)
                return name
        ''')
        self.write("sample/__init__.py", '''\
            from pathlib import Path
            import fixture_dependency
            VALUE = "primary"
            def main():
                assert Path(__file__).resolve().is_relative_to(Path.cwd())
                assert fixture_dependency.VALUE == "qualified-local-wheel"
                print(VALUE)
        ''')
        self.write("check_python.py", '''\
            from pathlib import Path
            import subprocess
            import sample
            assert Path(sample.__file__).resolve().is_relative_to(Path.cwd())
            observed = subprocess.check_output([str(Path('.VE/bin/prepared-sample').absolute())], text=True).strip()
            assert observed == sample.VALUE
        ''')
        wheel_path = self.root / "fixture_dependency-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel_path, "w") as wheel:
            entries = {
                "fixture_dependency.py": 'VALUE = "qualified-local-wheel"\n',
                "fixture_dependency-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: fixture-dependency\nVersion: 1.0\n",
                "fixture_dependency-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            }
            entries["fixture_dependency-1.0.dist-info/RECORD"] = "".join(key + ",,\n" for key in entries) + "fixture_dependency-1.0.dist-info/RECORD,,\n"
            for name, contents in entries.items():
                wheel.writestr(name, contents)
        return [
            ["{python}", "-m", "venv", "--without-pip", ".VE"],
            ["{worktree}/.VE/bin/python", "-m", "ensurepip"],
            ["{worktree}/.VE/bin/python", "-m", "pip", "install", "--no-deps", "--no-build-isolation", "fixture_dependency-1.0-py3-none-any.whl", "-e", "."],
        ]

    def test_real_python_editable_and_console_script_use_task_source(self):
        setup = self.python_fixture()
        self.configure(setup=setup, checks=[["{worktree}/.VE/bin/python", "check_python.py"]], tracked=["pyproject.toml", "fixture_backend.py", "fixture_dependency-1.0-py3-none-any.whl"], timeout=120)
        self.checkout()
        sample = self.worktree / "sample/__init__.py"
        sample.write_text(sample.read_text().replace('VALUE = "primary"', 'VALUE = "task"'))
        result = self.run_setup()
        self.assertTrue(result.ready, result.remediation)
        self.assertTrue(self.verify().ready)
        sample.write_text(sample.read_text().replace('VALUE = "task"', 'VALUE = "edited-task"'))
        self.assertTrue(self.verify().ready)
        self.assertFalse((self.root / ".VE").exists())

    @unittest.skipUnless(shutil.which("node") and shutil.which("npm"), "Node and npm are required for the public mixed-language fixture")
    def test_locked_node_and_mixed_generated_output(self):
        setup = self.python_fixture()
        self.write("package.json", json.dumps({"name": "prepared-node", "version": "1.0.0", "private": True, "scripts": {"build": "node build.js", "test": "node test.js"}}))
        self.write("package-lock.json", json.dumps({"name": "prepared-node", "version": "1.0.0", "lockfileVersion": 3, "requires": True, "packages": {"": {"name": "prepared-node", "version": "1.0.0"}}}))
        self.write("generate.py", "import json, sample\nfrom pathlib import Path\nPath('.prepared/generated.json').write_text(json.dumps({'value': sample.VALUE}))\n")
        self.write("build.js", "const fs = require('node:fs'); const value = JSON.parse(fs.readFileSync('.prepared/generated.json')).value; fs.writeFileSync('dist/result.json', JSON.stringify({value}));\n")
        self.write("test.js", "const fs = require('node:fs'); const assert = require('node:assert'); assert.equal(JSON.parse(fs.readFileSync('dist/result.json')).value, JSON.parse(fs.readFileSync('.prepared/generated.json')).value);\n")
        self.configure(
            setup=[*setup, ["{npm}", "ci", "--ignore-scripts"], ["{worktree}/.VE/bin/python", "generate.py"], ["{npm}", "run", "build"]],
            checks=[["{worktree}/.VE/bin/python", "check_python.py"], ["{npm}", "test"]],
            tools=[tool("python", "python3"), tool("node", "node"), tool("npm", "npm"),
                   {"name": "shell", "executable": "sh", "version_args": ["-c", "printf posix-shell"], "version": "posix-shell"}],
            tracked=["pyproject.toml", "fixture_backend.py", "fixture_dependency-1.0-py3-none-any.whl", "package.json", "package-lock.json", "generate.py", "build.js"], timeout=120,
        )
        self.checkout()
        started = time.monotonic()
        first = self.run_setup()
        cold = time.monotonic() - started
        self.assertTrue(first.ready, first.remediation)
        started = time.monotonic()
        second = self.verify()
        warm = time.monotonic() - started
        self.assertTrue(second.ready, second.remediation)
        self.assertEqual(json.loads((self.worktree / "dist/result.json").read_text()), {"value": "primary"})
        print(f"mixed fixture preparation: cold={cold:.3f}s verified-worktree={warm:.3f}s (one sample; local wheel, no Node dependencies)")


if __name__ == "__main__":
    unittest.main()
