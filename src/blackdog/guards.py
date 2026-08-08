"""Repository-owned policy guard execution.

Blackdog owns this protocol and its evidence shape. Target repositories own
every policy decision by choosing whether to configure guard commands and by
implementing those commands.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import subprocess
from typing import Any, Mapping, NoReturn

from blackdog_core.profile import GUARD_PHASE_TASK_BEGIN, GuardConfig, RepoProfile


GUARD_INPUT_SCHEMA_VERSION = 1
GUARD_RESULT_SCHEMA_VERSION = 1
GUARD_RECEIPT_SCHEMA_VERSION = 1
GUARD_RESULT_STATUSES = ("passed", "blocked")
MAX_GUARD_OUTPUT_BYTES = 16_384
MAX_GUARD_MESSAGE_CHARS = 512
MAX_GUARD_REQUIRED_INPUTS = 16
_IDENTIFIER_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_RESULT_KEYS = frozenset(
    {"schema_version", "status", "reason_code", "message", "required_inputs"}
)


class RepositoryGuardRefusal(RuntimeError):
    """A configured repository guard blocked or failed to evaluate."""

    def __init__(
        self,
        *,
        guard_id: str,
        action_id: str,
        reason_code: str,
        message: str,
        required_inputs: tuple[str, ...],
    ) -> None:
        self.guard_id = guard_id
        self.action_id = action_id
        self.reason_code = reason_code
        self.message = message
        self.required_inputs = required_inputs
        super().__init__(f"repository guard {guard_id!r}: {message}")


@dataclass(frozen=True)
class GuardTaskInput:
    actor: str
    prompt_mode: str
    execution_prompt_text: str
    execution_prompt_hash: str
    request_prompt_text: str
    request_prompt_hash: str


def evaluate_task_begin_guards(
    profile: RepoProfile,
    *,
    task: GuardTaskInput,
) -> tuple[dict[str, Any], ...]:
    receipts: list[dict[str, Any]] = []
    for guard in profile.guards:
        if not guard.enabled or guard.phase != GUARD_PHASE_TASK_BEGIN:
            continue
        receipts.append(_run_guard(profile, guard=guard, task=task))
    return tuple(receipts)


def _run_guard(
    profile: RepoProfile,
    *,
    guard: GuardConfig,
    task: GuardTaskInput,
) -> dict[str, Any]:
    config_sha256 = _guard_config_sha256(guard)
    input_payload = {
        "schema_version": GUARD_INPUT_SCHEMA_VERSION,
        "phase": GUARD_PHASE_TASK_BEGIN,
        "guard": {
            "id": guard.guard_id,
            "config_sha256": config_sha256,
        },
        "repository": {
            "project_name": profile.project_name,
            "project_root": str(profile.paths.project_root),
        },
        "task": {
            "actor": task.actor,
            "prompt_mode": task.prompt_mode,
            "execution_prompt": {
                "text": task.execution_prompt_text,
                "sha256": task.execution_prompt_hash,
            },
            "request": {
                "text": task.request_prompt_text,
                "sha256": task.request_prompt_hash,
            },
        },
    }
    try:
        completed = subprocess.run(
            list(guard.command),
            cwd=profile.paths.project_root,
            input=(
                json.dumps(input_payload, sort_keys=True, separators=(",", ":"))
                + "\n"
            ),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            timeout=guard.timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = "repository_guard_timeout"
        elif isinstance(exc, UnicodeError):
            reason = "repository_guard_invalid_utf8"
        else:
            reason = "repository_guard_unavailable"
        _raise_guard_failure(
            guard,
            reason_code=reason,
            message="the configured guard command could not complete",
        )
    if completed.returncode != 0:
        _raise_guard_failure(
            guard,
            reason_code="repository_guard_nonzero_exit",
            message=f"the configured guard command exited with status {completed.returncode}",
        )
    raw_output = completed.stdout.encode("utf-8")
    if len(raw_output) > MAX_GUARD_OUTPUT_BYTES:
        _raise_guard_failure(
            guard,
            reason_code="repository_guard_output_too_large",
            message="the configured guard result exceeded the output limit",
        )
    try:
        payload = _load_result_json(completed.stdout)
    except (json.JSONDecodeError, ValueError):
        _raise_guard_failure(
            guard,
            reason_code="repository_guard_invalid_json",
            message="the configured guard did not return one JSON object",
        )
    result = _validate_result(guard, payload)
    if result["status"] == "blocked":
        raise RepositoryGuardRefusal(
            guard_id=guard.guard_id,
            action_id="repository_guard_blocked",
            reason_code=str(result["reason_code"]),
            message=str(result["message"]),
            required_inputs=tuple(result["required_inputs"]),
        )
    return {
        "schema_version": GUARD_RECEIPT_SCHEMA_VERSION,
        "id": guard.guard_id,
        "phase": guard.phase,
        "config_sha256": config_sha256,
        "status": result["status"],
        "reason_code": result["reason_code"],
        "message": result["message"],
        "required_inputs": result["required_inputs"],
    }


def _validate_result(guard: GuardConfig, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RESULT_KEYS:
        _raise_guard_failure(
            guard,
            reason_code="repository_guard_invalid_result",
            message="the configured guard returned an invalid result object",
        )
    schema_version = value.get("schema_version")
    status = value.get("status")
    reason_code = value.get("reason_code")
    message = value.get("message")
    required_inputs = value.get("required_inputs")
    if type(schema_version) is not int or schema_version != GUARD_RESULT_SCHEMA_VERSION:
        _raise_guard_failure(
            guard,
            reason_code="repository_guard_invalid_result",
            message="the configured guard returned an unsupported result schema",
        )
    if status not in GUARD_RESULT_STATUSES:
        _raise_guard_failure(
            guard,
            reason_code="repository_guard_invalid_result",
            message="the configured guard returned an unsupported status",
        )
    if not isinstance(reason_code, str) or _IDENTIFIER_PATTERN.fullmatch(reason_code) is None:
        _raise_guard_failure(
            guard,
            reason_code="repository_guard_invalid_result",
            message="the configured guard returned an invalid reason code",
        )
    if (
        not isinstance(message, str)
        or not message.strip()
        or len(message) > MAX_GUARD_MESSAGE_CHARS
    ):
        _raise_guard_failure(
            guard,
            reason_code="repository_guard_invalid_result",
            message="the configured guard returned an invalid message",
        )
    if (
        not isinstance(required_inputs, list)
        or len(required_inputs) > MAX_GUARD_REQUIRED_INPUTS
        or any(
            not isinstance(item, str)
            or _IDENTIFIER_PATTERN.fullmatch(item) is None
            for item in required_inputs
        )
        or len(set(required_inputs)) != len(required_inputs)
        or (status == "passed" and required_inputs)
    ):
        _raise_guard_failure(
            guard,
            reason_code="repository_guard_invalid_result",
            message="the configured guard returned invalid required inputs",
        )
    return {
        "schema_version": schema_version,
        "status": status,
        "reason_code": reason_code,
        "message": message.strip(),
        "required_inputs": list(required_inputs),
    }


def _raise_guard_failure(
    guard: GuardConfig,
    *,
    reason_code: str,
    message: str,
) -> NoReturn:
    raise RepositoryGuardRefusal(
        guard_id=guard.guard_id,
        action_id="repository_guard_failed",
        reason_code=reason_code,
        message=message,
        required_inputs=("repository_guard",),
    )


def _load_result_json(text: str) -> Any:
    def reject_constant(value: str) -> NoReturn:
        raise ValueError(f"unsupported JSON constant: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        text,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )


def _guard_config_sha256(guard: GuardConfig) -> str:
    payload = {
        "schema_version": guard.schema_version,
        "id": guard.guard_id,
        "phase": guard.phase,
        "command": list(guard.command),
        "enabled": guard.enabled,
        "timeout_seconds": guard.timeout_seconds,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "GUARD_INPUT_SCHEMA_VERSION",
    "GUARD_RECEIPT_SCHEMA_VERSION",
    "GUARD_RESULT_SCHEMA_VERSION",
    "GuardTaskInput",
    "RepositoryGuardRefusal",
    "evaluate_task_begin_guards",
]
