"""Strict immutable command validation observations, independent of execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

VALIDATION_COMMAND_STATUSES = frozenset(
    {"passed", "failed", "timed_out", "execution_error"}
)


@dataclass(frozen=True, slots=True)
class ValidationCommandResult:
    index: int
    command_sha256: str
    status: str
    returncode: int | None
    elapsed_ms: int
    stdout_bytes: int
    stderr_bytes: int
    output_retained: bool = False

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("validation command index must be nonnegative")
        if len(self.command_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.command_sha256
        ):
            raise ValueError("validation command SHA-256 must be lowercase hexadecimal")
        if self.status not in VALIDATION_COMMAND_STATUSES:
            raise ValueError(f"unsupported validation command status: {self.status}")
        if self.status == "passed" and self.returncode != 0:
            raise ValueError("passed validation command must have returncode 0")
        if self.status == "failed" and (
            self.returncode is None or self.returncode == 0
        ):
            raise ValueError("failed validation command must have nonzero returncode")
        if self.elapsed_ms < 0 or self.stdout_bytes < 0 or self.stderr_bytes < 0:
            raise ValueError("validation command counters must be nonnegative")
        if self.output_retained:
            raise ValueError("validation command output retention is not supported")

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "command_sha256": self.command_sha256,
            "status": self.status,
            "returncode": self.returncode,
            "elapsed_ms": self.elapsed_ms,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "output_retained": False,
        }

    @classmethod
    def from_dict(cls, value: Any) -> ValidationCommandResult:
        if not isinstance(value, Mapping):
            raise ValueError("validation command result must be an object")
        expected_keys = {
            "index",
            "command_sha256",
            "status",
            "returncode",
            "elapsed_ms",
            "stdout_bytes",
            "stderr_bytes",
            "output_retained",
        }
        if set(value) != expected_keys:
            raise ValueError("validation command result has invalid fields")
        for field in ("index", "elapsed_ms", "stdout_bytes", "stderr_bytes"):
            if not isinstance(value[field], int) or isinstance(value[field], bool):
                raise ValueError(
                    f"validation command result {field} must be an integer"
                )
        returncode = value["returncode"]
        if returncode is not None and (
            not isinstance(returncode, int) or isinstance(returncode, bool)
        ):
            raise ValueError(
                "validation command result returncode must be an integer or null"
            )
        if value["output_retained"] is not False:
            raise ValueError("validation command output retention is not supported")
        if not isinstance(value["command_sha256"], str) or not isinstance(
            value["status"], str
        ):
            raise ValueError("validation command hash and status must be strings")
        return cls(
            index=value["index"],
            command_sha256=value["command_sha256"],
            status=value["status"],
            returncode=returncode,
            elapsed_ms=value["elapsed_ms"],
            stdout_bytes=value["stdout_bytes"],
            stderr_bytes=value["stderr_bytes"],
            output_retained=False,
        )


@dataclass(frozen=True, slots=True)
class ValidationRunResult:
    command_count: int
    results: tuple[ValidationCommandResult, ...]

    def __post_init__(self) -> None:
        if self.command_count < 0:
            raise ValueError("validation command count must be nonnegative")
        if len(self.results) > self.command_count:
            raise ValueError("validation results cannot exceed configured commands")
        if tuple(result.index for result in self.results) != tuple(
            range(len(self.results))
        ):
            raise ValueError("validation command result indexes must be contiguous")
        if any(result.status != "passed" for result in self.results[:-1]):
            raise ValueError(
                "validation execution must stop after the first non-passed result"
            )

    @property
    def completed_count(self) -> int:
        return len(self.results)

    @property
    def all_passed(self) -> bool:
        return self.completed_count == self.command_count and all(
            result.status == "passed" for result in self.results
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "command_count": self.command_count,
            "completed_count": self.completed_count,
            "all_passed": self.all_passed,
            "results": [result.to_dict() for result in self.results],
        }

    @classmethod
    def from_dict(cls, value: Any) -> ValidationRunResult:
        if not isinstance(value, Mapping):
            raise ValueError("validation run result must be an object")
        expected_keys = {
            "command_count",
            "completed_count",
            "all_passed",
            "results",
        }
        if set(value) != expected_keys:
            raise ValueError("validation run result has invalid fields")
        command_count = value["command_count"]
        completed_count = value["completed_count"]
        if (
            not isinstance(command_count, int)
            or isinstance(command_count, bool)
            or not isinstance(completed_count, int)
            or isinstance(completed_count, bool)
        ):
            raise ValueError("validation run counts must be integers")
        if not isinstance(value["all_passed"], bool):
            raise ValueError("validation run all_passed must be a boolean")
        rows = value["results"]
        if not isinstance(rows, list):
            raise ValueError("validation run results must be a list")
        result = cls(
            command_count=command_count,
            results=tuple(ValidationCommandResult.from_dict(row) for row in rows),
        )
        if completed_count != result.completed_count:
            raise ValueError("validation completed_count does not match result rows")
        if value["all_passed"] != result.all_passed:
            raise ValueError("validation all_passed does not match result rows")
        return result
