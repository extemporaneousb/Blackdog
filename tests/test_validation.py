from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest

from blackdog.validation import ValidationRunResult, run_validation_commands


class ValidationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="blackdog validation workspace "
        )
        self.workspace = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_runs_shell_command_in_exact_cwd_with_environment_assignment_and_quoting(
        self,
    ) -> None:
        cwd_proof = self.workspace / "cwd proof"
        code = (
            "import os, pathlib, sys; "
            "pathlib.Path('cwd proof').write_text('exact cwd'); "
            "sys.exit(0 if os.environ.get('VALIDATION_VALUE') == 'value with spaces' else 9)"
        )
        command = (
            "VALIDATION_VALUE='value with spaces' "
            f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        )

        result = run_validation_commands(
            (command,),
            cwd=self.workspace,
            timeout_seconds=5,
        )

        self.assertTrue(result.all_passed)
        self.assertEqual(result.completed_count, 1)
        command_result = result.results[0]
        self.assertEqual(command_result.status, "passed")
        self.assertEqual(command_result.returncode, 0)
        self.assertEqual(cwd_proof.read_text(encoding="utf-8"), "exact cwd")
        self.assertEqual(
            command_result.command_sha256,
            hashlib.sha256(command.encode("utf-8")).hexdigest(),
        )
        self.assertFalse(command_result.output_retained)

    def test_failure_counts_output_and_stops_before_the_next_command(self) -> None:
        marker = self.workspace / "must-not-run"
        failing_code = (
            "import sys; "
            "sys.stdout.buffer.write(b'secret stdout\\n'); "
            "sys.stderr.buffer.write(b'secret stderr\\n'); "
            "sys.exit(7)"
        )
        marker_code = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
        first_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(failing_code)}"
        second_command = f"{shlex.quote(sys.executable)} -c {shlex.quote(marker_code)}"

        result = run_validation_commands(
            (first_command, second_command),
            cwd=self.workspace,
            timeout_seconds=5,
        )

        self.assertFalse(result.all_passed)
        self.assertEqual(result.command_count, 2)
        self.assertEqual(result.completed_count, 1)
        self.assertFalse(marker.exists())
        command_result = result.results[0]
        self.assertEqual(command_result.status, "failed")
        self.assertEqual(command_result.returncode, 7)
        self.assertEqual(command_result.stdout_bytes, len(b"secret stdout\n"))
        self.assertEqual(command_result.stderr_bytes, len(b"secret stderr\n"))
        serialized = json.dumps(result.to_dict(), sort_keys=True)
        self.assertNotIn("secret stdout", serialized)
        self.assertNotIn("secret stderr", serialized)
        self.assertNotIn(first_command, serialized)
        self.assertFalse(command_result.to_dict()["output_retained"])

    def test_timeout_terminates_the_validation_process_group(self) -> None:
        ready_path = self.workspace / "child-ready"
        terminated_path = self.workspace / "child-terminated"
        child_pid_path = self.workspace / "child-pid"
        child_code = (
            "import os, pathlib, signal, time; "
            f"ready = pathlib.Path({str(ready_path)!r}); "
            f"terminated = pathlib.Path({str(terminated_path)!r}); "
            f"pid_path = pathlib.Path({str(child_pid_path)!r}); "
            "pid_path.write_text(str(os.getpid())); "
            "signal.signal(signal.SIGTERM, "
            "lambda signum, frame: (terminated.write_text('terminated'), "
            "exit(0))); "
            "ready.write_text('ready'); "
            "time.sleep(60)"
        )
        parent_code = (
            "import pathlib, subprocess, sys, time; "
            f"ready = pathlib.Path({str(ready_path)!r}); "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            "deadline = time.monotonic() + 5; "
            "\nwhile not ready.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
            "time.sleep(60)"
        )
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_code)}"

        result = run_validation_commands(
            (command,),
            cwd=self.workspace,
            timeout_seconds=1,
        )

        self.assertFalse(result.all_passed)
        self.assertEqual(result.results[0].status, "timed_out")
        self.assertTrue(ready_path.exists())
        deadline = time.monotonic() + 2
        while not terminated_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(terminated_path.exists())
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        process_exists = True
        deadline = time.monotonic() + 2
        while process_exists and time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                process_exists = False
            else:
                time.sleep(0.01)
        self.assertFalse(process_exists)

    def test_success_counts_large_stdout_and_stderr_without_retaining_output(self) -> None:
        stdout_size = 200_003
        stderr_size = 150_007
        code = (
            "import sys; "
            f"sys.stdout.buffer.write(b'x' * {stdout_size}); "
            f"sys.stderr.buffer.write(b'y' * {stderr_size})"
        )
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

        result = run_validation_commands(
            (command,),
            cwd=self.workspace,
            timeout_seconds=5,
        )

        self.assertTrue(result.all_passed)
        command_result = result.results[0]
        self.assertEqual(command_result.stdout_bytes, stdout_size)
        self.assertEqual(command_result.stderr_bytes, stderr_size)
        self.assertFalse(command_result.output_retained)
        self.assertEqual(
            set(command_result.to_dict()),
            {
                "index",
                "command_sha256",
                "status",
                "returncode",
                "elapsed_ms",
                "stdout_bytes",
                "stderr_bytes",
                "output_retained",
            },
        )

    def test_successful_shell_with_lingering_descendant_fails_and_kills_group(
        self,
    ) -> None:
        child_pid_path = self.workspace / "background-child-pid"
        command = (
            "sleep 30 >/dev/null 2>&1 & "
            f"echo $! > {shlex.quote(child_pid_path.name)}"
        )

        result = run_validation_commands(
            (command,),
            cwd=self.workspace,
            timeout_seconds=2,
        )

        self.assertFalse(result.all_passed)
        self.assertEqual(result.results[0].status, "execution_error")
        child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())
        process_exists = True
        deadline = time.monotonic() + 2
        while process_exists and time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                process_exists = False
            else:
                time.sleep(0.01)
        self.assertFalse(process_exists)

    def test_spawn_failure_is_typed_without_retaining_error_text(self) -> None:
        missing_cwd = self.workspace / "missing"
        command = "printf 'must not be retained'"

        result = run_validation_commands(
            (command,),
            cwd=missing_cwd,
            timeout_seconds=5,
        )

        self.assertFalse(result.all_passed)
        command_result = result.results[0]
        self.assertEqual(command_result.status, "execution_error")
        self.assertIsNone(command_result.returncode)
        self.assertEqual(command_result.stdout_bytes, 0)
        self.assertEqual(command_result.stderr_bytes, 0)
        self.assertNotIn(command, json.dumps(result.to_dict(), sort_keys=True))

    def test_typed_result_round_trips_without_command_or_output(self) -> None:
        command = "printf 'sensitive value'"
        result = run_validation_commands(
            (command,),
            cwd=self.workspace,
            timeout_seconds=5,
        )

        restored = ValidationRunResult.from_dict(result.to_dict())

        self.assertEqual(restored, result)
        serialized = json.dumps(restored.to_dict(), sort_keys=True)
        self.assertNotIn(command, serialized)
        self.assertNotIn("sensitive value", serialized)

    def test_typed_result_rejects_mismatched_summary(self) -> None:
        result = run_validation_commands(
            ("true",),
            cwd=self.workspace,
            timeout_seconds=5,
        ).to_dict()
        result["all_passed"] = False

        with self.assertRaisesRegex(ValueError, "all_passed"):
            ValidationRunResult.from_dict(result)

    def test_typed_result_rejects_status_returncode_conflict(self) -> None:
        result = run_validation_commands(
            ("true",),
            cwd=self.workspace,
            timeout_seconds=5,
        ).to_dict()
        result["results"][0]["returncode"] = 7

        with self.assertRaisesRegex(ValueError, "returncode 0"):
            ValidationRunResult.from_dict(result)


if __name__ == "__main__":
    unittest.main()
