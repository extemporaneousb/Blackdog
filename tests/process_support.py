"""Small process helpers for isolated integration fixtures."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
from typing import Mapping, Sequence


def configure_test_git(root: Path) -> None:
    """Keep personal signing and hooks settings out of a synthetic repository."""
    hooks = root / ".git" / "empty-test-hooks"
    hooks.mkdir()
    for key, value in (
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
        ("core.hooksPath", str(hooks)),
    ):
        subprocess.run(
            ["git", "-C", str(root), "config", "--local", key, value],
            check=True, capture_output=True, text=True, timeout=10,
        )


def run_cli(
    argv: Sequence[str], *, cwd: Path | None = None,
    env: Mapping[str, str] | None = None, timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    """Bound test commands and retain diagnostics without inherited pipe waits."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
            start_new_session=True,
        )
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Only signal the process group created for this test invocation.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            finally:
                # The leader may have exited while a child ignored SIGTERM.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)
        stdout.seek(0)
        stderr.seek(0)
        output = stdout.read().decode("utf-8", errors="replace")
        errors = stderr.read().decode("utf-8", errors="replace")
    if timed_out:
        raise AssertionError(
            f"CLI command exceeded {timeout:g}s: {shlex.join(argv)}\n"
            f"stdout:\n{output}\nstderr:\n{errors}"
        )
    return subprocess.CompletedProcess(argv, process.returncode, output, errors)
