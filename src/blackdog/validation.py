from __future__ import annotations

import hashlib
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import BinaryIO, Sequence

from blackdog_core.validation import (
    VALIDATION_COMMAND_STATUSES,
    ValidationCommandResult,
    ValidationRunResult,
)

_READ_CHUNK_BYTES = 64 * 1024
_TERMINATION_GRACE_SECONDS = 0.5


def _command_sha256(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _drain_stream(
    stream: BinaryIO,
    byte_count: list[int],
    read_failed: list[bool],
) -> None:
    try:
        while True:
            chunk = stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            byte_count[0] += len(chunk)
    except (OSError, ValueError):
        read_failed[0] = True
    finally:
        try:
            stream.close()
        except OSError:
            read_failed[0] = True


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_process_group(process_group_id: int, signal_number: int) -> bool:
    try:
        os.killpg(process_group_id, signal_number)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> bool:
    process_group_id = process.pid
    signaled = _signal_process_group(process_group_id, signal.SIGTERM)
    deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass

    while _process_group_exists(process_group_id) and time.monotonic() < deadline:
        time.sleep(0.01)

    if _process_group_exists(process_group_id):
        signaled = _signal_process_group(process_group_id, signal.SIGKILL) and signaled
    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            signaled = False
        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            signaled = False
    return signaled


def _join_reader(thread: threading.Thread, stream: BinaryIO) -> bool:
    thread.join(timeout=_TERMINATION_GRACE_SECONDS)
    if not thread.is_alive():
        return True
    try:
        stream.close()
    except OSError:
        pass
    thread.join(timeout=_TERMINATION_GRACE_SECONDS)
    return not thread.is_alive()


def _run_validation_command(
    command: str,
    *,
    index: int,
    cwd: Path,
    timeout_seconds: float,
) -> ValidationCommandResult:
    started_ns = time.monotonic_ns()
    command_hash = _command_sha256(command)
    try:
        process = subprocess.Popen(
            ["/bin/sh", "-c", command],
            cwd=os.fspath(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return ValidationCommandResult(
            index=index,
            command_sha256=command_hash,
            status="execution_error",
            returncode=None,
            elapsed_ms=max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
            stdout_bytes=0,
            stderr_bytes=0,
        )

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_count = [0]
    stderr_count = [0]
    stdout_failed = [False]
    stderr_failed = [False]
    stdout_thread = threading.Thread(
        target=_drain_stream,
        args=(process.stdout, stdout_count, stdout_failed),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_stream,
        args=(process.stderr, stderr_count, stderr_failed),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    termination_succeeded = True
    lingering_process_group = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        termination_succeeded = _terminate_process_group(process)
    except BaseException:
        _terminate_process_group(process)
        _join_reader(stdout_thread, process.stdout)
        _join_reader(stderr_thread, process.stderr)
        raise
    else:
        lingering_process_group = _process_group_exists(process.pid)
        if lingering_process_group:
            termination_succeeded = _terminate_process_group(process)

    stdout_joined = _join_reader(stdout_thread, process.stdout)
    stderr_joined = _join_reader(stderr_thread, process.stderr)
    elapsed_ms = max(0, (time.monotonic_ns() - started_ns) // 1_000_000)
    stream_failed = (
        stdout_failed[0]
        or stderr_failed[0]
        or not stdout_joined
        or not stderr_joined
    )
    if lingering_process_group:
        status = "execution_error"
    elif timed_out and termination_succeeded and not stream_failed:
        status = "timed_out"
    elif stream_failed or (timed_out and not termination_succeeded):
        status = "execution_error"
    elif process.returncode == 0:
        status = "passed"
    else:
        status = "failed"
    return ValidationCommandResult(
        index=index,
        command_sha256=command_hash,
        status=status,
        returncode=process.returncode,
        elapsed_ms=elapsed_ms,
        stdout_bytes=stdout_count[0],
        stderr_bytes=stderr_count[0],
    )


def run_validation_commands(
    commands: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> ValidationRunResult:
    if timeout_seconds <= 0:
        raise ValueError("validation command timeout must be positive")
    normalized_commands = tuple(commands)
    if any(not isinstance(command, str) or not command.strip() for command in normalized_commands):
        raise ValueError("validation commands must be nonblank strings")

    results: list[ValidationCommandResult] = []
    for index, command in enumerate(normalized_commands):
        result = _run_validation_command(
            command,
            index=index,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
        results.append(result)
        if result.status != "passed":
            break
    return ValidationRunResult(
        command_count=len(normalized_commands),
        results=tuple(results),
    )


__all__ = [
    "VALIDATION_COMMAND_STATUSES",
    "ValidationCommandResult",
    "ValidationRunResult",
    "run_validation_commands",
]
