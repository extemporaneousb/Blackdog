from __future__ import annotations

import hashlib
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Sequence

from blackdog_core.validation import (
    VALIDATION_COMMAND_STATUSES,
    ValidationCommandResult,
    ValidationRunResult,
)

_READ_CHUNK_BYTES = 64 * 1024
_TERMINATION_GRACE_SECONDS = 0.5


def _command_sha256(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _drain_ready_streams(
    selector: selectors.BaseSelector,
    byte_counts: list[int],
    *,
    timeout: float,
) -> bool:
    ready = selector.select(timeout)
    for key, _ in ready:
        try:
            chunk = os.read(key.fd, _READ_CHUNK_BYTES)
        except BlockingIOError:
            continue
        if chunk:
            byte_counts[key.data] += len(chunk)
        else:
            selector.unregister(key.fileobj)
            key.fileobj.close()
    return bool(ready)


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
            bufsize=0,
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
    byte_counts = [0, 0]
    timed_out = False
    stream_failed = False
    termination_succeeded = True
    lingering_process_group = False
    leader_exited = False
    deadline = started_ns / 1_000_000_000 + timeout_seconds
    try:
        with selectors.DefaultSelector() as selector:
            for stream_index, stream in enumerate((process.stdout, process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, stream_index)

            while True:
                if process.poll() is not None:
                    if not leader_exited:
                        lingering_process_group = _process_group_exists(process.pid)
                        leader_exited = True
                    if lingering_process_group or not selector.get_map():
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                # Poll the leader even when a descendant holds a silent pipe.
                _drain_ready_streams(selector, byte_counts, timeout=min(remaining, 0.05))

            if timed_out or lingering_process_group:
                if not leader_exited or lingering_process_group:
                    termination_succeeded = _terminate_process_group(process)
                # Count immediately available shutdown output without waiting for
                # detached descendants, whose pipes may never reach EOF.
                drain_deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
                while selector.get_map() and time.monotonic() < drain_deadline:
                    if not _drain_ready_streams(selector, byte_counts, timeout=0):
                        break
    except (OSError, ValueError):
        stream_failed = True
        if not leader_exited or lingering_process_group:
            termination_succeeded = _terminate_process_group(process)
    except BaseException:
        if not leader_exited or lingering_process_group:
            _terminate_process_group(process)
        raise
    finally:
        # Unbuffered, nonblocking streams have no reader lock to wait for here.
        process.stdout.close()
        process.stderr.close()

    elapsed_ms = max(0, (time.monotonic_ns() - started_ns) // 1_000_000)
    if lingering_process_group or stream_failed or not termination_succeeded:
        status = "execution_error"
    elif timed_out:
        status = "timed_out"
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
        stdout_bytes=byte_counts[0],
        stderr_bytes=byte_counts[1],
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
