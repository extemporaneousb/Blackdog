"""Bounded best-effort product lifecycle observations.

This stream is deliberately descriptive.  It cannot activate, complete, or
otherwise change a Blackdog operation, and every public write boundary absorbs
its own failures after retaining bounded in-process missingness evidence.
"""

from __future__ import annotations

from collections import Counter, OrderedDict, deque
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from threading import Lock
from typing import IO, Iterable, Mapping, TypeVar

from blackdog.lifecycle import OPERATION_FAILURE_CODES
from blackdog_core.profile import RepoProfile, load_profile
from blackdog_core.state import ATTEMPT_STATUSES, TASK_STATUSES, now_iso

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on platforms without fcntl
    fcntl = None


OBSERVATION_SCHEMA_VERSION = 1
OBSERVATION_RELATIVE_PATH = Path("observability") / "lifecycle-v1.jsonl"
MAX_ARTIFACT_BYTES = 1_048_576
MAX_ROW_BYTES = 1_024
MAX_LABELS = 8
MAX_FAILURE_EVIDENCE = 16
MAX_PROCESS_PROJECTS = 256
MAX_HASH_INPUT_BYTES = 4_096

KNOWN_SURFACES = frozenset(
    {
        "prompt.preview",
        "prompt.tune",
        "repo.analyze",
        "repo.archive",
        "repo.bind",
        "repo.install",
        "repo.refresh",
        "repo.scaffold",
        "repo.table",
        "repo.unarchive",
        "repo.unbind",
        "repo.update",
        "stats.read",
        "task.begin",
        "task.cancel",
        "task.cleanup",
        "task.close",
        "task.land",
        "task.reconcile-landing",
        "task.recover",
        "task.reopen",
        "task.show",
        "worktree.cleanup",
        "worktree.close",
        "worktree.land",
        "worktree.preflight",
        "worktree.preview",
        "worktree.show",
        "worktree.start",
        "worktree.table",
    }
)
KNOWN_OUTCOMES = frozenset(
    {"success", "failed", "blocked", "partial", "abandoned", "unknown"}
)
KNOWN_REASONS = frozenset(
    {
        "none",
        "capacity",
        "contention",
        "io",
        "operator",
        "system",
        "validation",
        "unknown",
    }
)
ALLOWED_LABEL_VALUES = {
    "attempt_status": frozenset((*ATTEMPT_STATUSES, "unknown")),
    "failure_class": OPERATION_FAILURE_CODES,
    "mutation_phase": frozenset({"none", "pre_git", "post_git", "runtime", "unknown"}),
    "operation_status": frozenset(
        {"observed", "succeeded", "blocked", "closed", "partial", "unknown"}
    ),
    "operation_phase": frozenset({"completed", "requested", "unknown"}),
    "prompt_mode": frozenset({"raw", "skill", "tuned", "unknown"}),
    "prompt_role": frozenset({"execution", "request", "unknown"}),
    "result": frozenset({"applied", "completed", "deduped", "dry_run", "noop", "unknown"}),
    "retryability": frozenset({"retryable", "terminal", "unknown"}),
    "scope_source": frozenset({"discovery_roots", "explicit_project_roots", "registry", "unknown"}),
    "task_status": frozenset((*TASK_STATUSES, "unknown")),
}

OBSERVATION_ROW_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "surface",
        "operation_key_hash",
        "outcome",
        "reason",
        "labels",
        "unknown_label_count",
        "observation_id",
        "observed_at",
    }
)
_OPERATION_IDENTITY_FIELDS = (
    "workset_id",
    "task_id",
    "attempt_id",
    "action",
    "action_id",
    "operation_status",
    "task_status",
    "attempt_status",
    "status",
)
_MUTATION_PHASE_LABELS = {
    "none": "none",
    "preflight": "pre_git",
    "git_prepared": "pre_git",
    "workspace_started": "runtime",
    "workspace_adopted": "runtime",
    "proof_verified": "post_git",
    "runtime_finalized": "runtime",
    "runtime_and_event_finalized": "runtime",
    "event_finalized": "runtime",
    "event_finalization_partial": "runtime",
    "cleanup_event_finalization_pending": "runtime",
    "close_request_recorded": "pre_git",
    "close_core_request_recorded": "runtime",
    "close_core_decision_recorded": "runtime",
    "close_runtime_finalized": "runtime",
    "close_task_release_recorded": "runtime",
    "close_workset_release_recorded": "runtime",
    "close_task_finish_recorded": "runtime",
    "close_cleanup_pending": "post_git",
    "close_cleanup_finalized": "post_git",
    "close_event_pending": "runtime",
    "close_complete": "runtime",
    "runtime_and_cleanup_finalized": "runtime",
    "runtime_finalized_cleanup_pending": "runtime",
    "git_and_filesystem_finalized": "post_git",
    "git_and_filesystem_and_event_finalized": "post_git",
    "worktree_removed_branch_cleanup_pending": "post_git",
    "landing_intent_recorded": "pre_git",
    "landing_source_prepared": "post_git",
    "landing_canonical_commit_created": "post_git",
    "landing_target_updated": "post_git",
    "landing_temporary_cleanup_complete": "post_git",
    "landing_runtime_finalized": "runtime",
    "landing_land_event_recorded": "runtime",
    "landing_task_cleanup_complete": "post_git",
    "landing_complete": "runtime",
    "landing_abort_intent_recorded": "pre_git",
    "landing_abort_temporary_cleanup_complete": "post_git",
    "landing_abort_runtime_finalized": "runtime",
    "landing_abort_close_event_recorded": "runtime",
    "landing_abort_complete": "runtime",
    "landing_abort_superseded": "post_git",
}
_MISSING = object()
_RESULT_T = TypeVar("_RESULT_T")

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_PROCESS_LOCK = Lock()
_PROCESS_COUNTS: OrderedDict[str, Counter[str]] = OrderedDict()
_PROCESS_FAILURE_EVIDENCE: deque[dict[str, str]] = deque(maxlen=MAX_FAILURE_EVIDENCE)


class _ObservationContention(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ObservationWriteResult:
    observation_id: str
    status: str
    surface: str


@dataclass(frozen=True, slots=True)
class LifecycleObservationReport:
    project_id: str
    artifact_present: int
    artifact_missing: int
    observations: int
    duplicate_rows: int
    malformed_rows: int
    oversized_rows: int
    unknown_rows: int
    unknown_schema_rows: int
    unknown_surface_rows: int
    unknown_outcome_rows: int
    unknown_labels: int
    capacity_pressure: int
    truncated_artifact: int
    read_failures: int
    write_failures: int
    write_missing_targets: int
    surface_counts: dict[str, int]
    outcome_counts: dict[str, int]
    reason_counts: dict[str, int]
    label_counts: dict[str, dict[str, int]]
    write_failure_evidence: tuple[dict[str, str], ...]

    @property
    def stream_health(self) -> str:
        if self.artifact_missing:
            return "missing"
        incomplete = (
            self.malformed_rows
            + self.duplicate_rows
            + self.oversized_rows
            + self.unknown_rows
            + self.unknown_labels
            + self.capacity_pressure
            + self.truncated_artifact
            + self.read_failures
            + self.write_failures
            + self.write_missing_targets
        )
        return "degraded" if incomplete else "healthy"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["stream_health"] = self.stream_health
        payload["label_counts"] = {
            key: dict(sorted(counts.items()))
            for key, counts in sorted(self.label_counts.items())
        }
        payload["write_failure_evidence"] = [dict(row) for row in self.write_failure_evidence]
        return payload


def lifecycle_observation_path(profile: RepoProfile) -> Path:
    return profile.paths.control_dir / OBSERVATION_RELATIVE_PATH


def observe_lifecycle(
    profile: RepoProfile,
    *,
    surface: str,
    operation_key: object,
    outcome: str = "success",
    reason: str | None = None,
    labels: Mapping[str, object] | None = None,
) -> ObservationWriteResult:
    """Append one deduplicated observation without ever changing caller outcome."""

    try:
        return _observe_lifecycle(
            profile,
            surface=surface,
            operation_key=operation_key,
            outcome=outcome,
            reason=reason,
            labels=labels,
        )
    except Exception:
        project_id = hashlib.sha256(b"unknown-project").hexdigest()
        observation_id = hashlib.sha256(b"unavailable-observation").hexdigest()
        _best_effort_record_write_failure(project_id, "unknown", observation_id, "io")
        return ObservationWriteResult(observation_id, "failed", "unknown")


def _observe_lifecycle(
    profile: RepoProfile,
    *,
    surface: str,
    operation_key: object,
    outcome: str,
    reason: str | None,
    labels: Mapping[str, object] | None,
) -> ObservationWriteResult:
    project_id = _bounded_hash(str(profile.paths.project_root))
    normalized_surface = _normalize_surface(surface)
    normalized_outcome = outcome if outcome in KNOWN_OUTCOMES else "unknown"
    normalized_reason = reason if reason in KNOWN_REASONS else ("none" if reason is None else "unknown")
    normalized_labels, unknown_label_count = _normalize_labels(labels or {})
    identity = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "project_id": project_id,
        "surface": normalized_surface,
        "operation_key_hash": _bounded_hash(operation_key),
        "outcome": normalized_outcome,
        "reason": normalized_reason,
        "labels": normalized_labels,
        "unknown_label_count": unknown_label_count,
    }
    observation_id = _sha256_json(identity)
    row = {
        **identity,
        "observation_id": observation_id,
        "observed_at": now_iso(),
    }
    path = lifecycle_observation_path(profile)
    try:
        if not profile.paths.control_dir.is_dir():
            _best_effort_record_process_count(project_id, "write_missing_targets")
            return ObservationWriteResult(observation_id, "missing", normalized_surface)
        status = _append_observation_row(path, row)
        if status == "capacity":
            _best_effort_record_write_failure(
                project_id,
                normalized_surface,
                observation_id,
                "capacity",
            )
        return ObservationWriteResult(observation_id, status, normalized_surface)
    except _ObservationContention:
        _best_effort_record_write_failure(
            project_id,
            normalized_surface,
            observation_id,
            "contention",
        )
        return ObservationWriteResult(observation_id, "failed", normalized_surface)
    except Exception:
        _best_effort_record_write_failure(
            project_id,
            normalized_surface,
            observation_id,
            "io",
        )
        return ObservationWriteResult(observation_id, "failed", normalized_surface)


def observe_lifecycle_for_project(
    project_root: Path,
    *,
    surface: str,
    operation_key: object,
    outcome: str = "success",
    reason: str | None = None,
    labels: Mapping[str, object] | None = None,
) -> ObservationWriteResult:
    """Best-effort convenience boundary for repo lifecycle CLI call sites."""

    project_id = hashlib.sha256(b"unknown-project").hexdigest()
    try:
        project_id = _bounded_hash(str(project_root.expanduser().resolve()))
        profile = load_profile(project_root, read_only=True)
    except Exception:
        normalized_surface = _normalize_surface(surface)
        observation_id = _sha256_json(
            {
                "schema_version": OBSERVATION_SCHEMA_VERSION,
                "project_id": project_id,
                "surface": normalized_surface,
                "operation_key_hash": _safe_bounded_hash(operation_key),
            }
        )
        _best_effort_record_process_count(project_id, "write_missing_targets")
        return ObservationWriteResult(observation_id, "missing", normalized_surface)
    return observe_lifecycle(
        profile,
        surface=surface,
        operation_key=operation_key,
        outcome=outcome,
        reason=reason,
        labels=labels,
    )


def observe_operation_result(
    profile: RepoProfile,
    result: _RESULT_T,
    *,
    surface: str | None = None,
) -> _RESULT_T:
    """Best-effort observation adapter that returns the caller's result unchanged."""

    try:
        if not isinstance(result, Mapping):
            return result
        operation = _mapping_text(result, "operation")
        resolved_surface = str(surface or operation or "unknown")
        identity: dict[str, str] = {
            "surface": _bounded_identity_value(resolved_surface),
            "operation": _bounded_identity_value(operation),
        }
        for key in _OPERATION_IDENTITY_FIELDS:
            identity[key] = _bounded_identity_value(_mapping_value(result, key))
        next_action = _mapping_value(result, "next_action")
        if isinstance(next_action, Mapping):
            identity["next_action_id"] = _bounded_identity_value(
                _mapping_value(next_action, "action_id")
            )
            identity["next_action_kind"] = _bounded_identity_value(
                _mapping_value(next_action, "kind")
            )

        outcome = _operation_outcome(result)
        result_label = _operation_result_label(result)
        labels: dict[str, object] = {
            "operation_phase": "completed",
            "result": result_label,
            "mutation_phase": _mutation_phase_label(
                _mapping_value(result, "mutation_phase")
            ),
        }
        for key in ("operation_status", "task_status", "attempt_status"):
            value = _mapping_value(result, key)
            if value is not None:
                labels[key] = value
        failure_class = _mapping_value(result, "failure_code")
        if failure_class is None:
            failure_class = _mapping_value(result, "failure_class")
        if failure_class is not None:
            labels["failure_class"] = failure_class
        retryability = _retryability_label(result)
        if retryability is not None:
            labels["retryability"] = retryability

        reason = {
            "blocked": "validation",
            "partial": "system",
            "failed": "system",
            "abandoned": "operator",
        }.get(outcome, "none")
        observe_lifecycle(
            profile,
            surface=resolved_surface,
            operation_key=_sha256_json(identity),
            outcome=outcome,
            reason=reason,
            labels=labels,
        )
    except Exception:
        pass
    return result


def read_lifecycle_observability(profile: RepoProfile) -> LifecycleObservationReport:
    """Read bounded observation health; malformed or unreadable rows never escape."""

    try:
        return _read_lifecycle_observability(profile)
    except Exception:
        try:
            project_id = _safe_bounded_hash(str(profile.paths.project_root))
        except Exception:
            project_id = hashlib.sha256(b"unknown-project").hexdigest()
        process_counts, failure_evidence = _best_effort_process_health_for_project(
            project_id
        )
        return LifecycleObservationReport(
            project_id=project_id,
            artifact_present=0,
            artifact_missing=0,
            observations=0,
            duplicate_rows=0,
            malformed_rows=0,
            oversized_rows=0,
            unknown_rows=0,
            unknown_schema_rows=0,
            unknown_surface_rows=0,
            unknown_outcome_rows=0,
            unknown_labels=0,
            capacity_pressure=0,
            truncated_artifact=0,
            read_failures=1,
            write_failures=process_counts["write_failures"],
            write_missing_targets=process_counts["write_missing_targets"],
            surface_counts={},
            outcome_counts={},
            reason_counts={},
            label_counts={},
            write_failure_evidence=failure_evidence,
        )


def _read_lifecycle_observability(profile: RepoProfile) -> LifecycleObservationReport:
    """Implementation for the total public observation reader."""

    project_id = _bounded_hash(str(profile.paths.project_root))
    path = lifecycle_observation_path(profile)
    counts: Counter[str] = Counter()
    surface_counts: Counter[str] = Counter()
    outcome_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    label_counts: dict[str, Counter[str]] = {}
    seen_ids: set[str] = set()
    read_succeeded = False
    try:
        artifact_present = path.is_file()
    except Exception:
        counts["read_failures"] = 1
        artifact_present = False
    if not artifact_present and not counts["read_failures"]:
        counts["artifact_missing"] = 1
    elif artifact_present:
        counts["artifact_present"] = 1
        try:
            with _open_observation_file(path, "rb") as handle:
                data = handle.read(MAX_ARTIFACT_BYTES + 1)
            read_succeeded = True
        except Exception:
            counts["read_failures"] = 1
            data = b""
        persisted_bytes = len(data)
        if read_succeeded and (
            persisted_bytes > MAX_ARTIFACT_BYTES
            or MAX_ARTIFACT_BYTES - persisted_bytes < MAX_ROW_BYTES + 1
        ):
            counts["capacity_pressure"] = 1
        if persisted_bytes > MAX_ARTIFACT_BYTES:
            counts["truncated_artifact"] = 1
            data = data[:MAX_ARTIFACT_BYTES]
        for raw_line in data.splitlines():
            if len(raw_line) > MAX_ROW_BYTES:
                counts["oversized_rows"] += 1
                continue
            try:
                row = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                counts["malformed_rows"] += 1
                continue
            if not isinstance(row, dict):
                counts["malformed_rows"] += 1
                continue
            schema_version = row.get("schema_version")
            if not isinstance(schema_version, int) or isinstance(schema_version, bool):
                counts["malformed_rows"] += 1
                continue
            if schema_version != OBSERVATION_SCHEMA_VERSION:
                counts["unknown_schema_rows"] += 1
                counts["unknown_rows"] += 1
                continue
            if not _observation_row_shape_is_valid(row):
                counts["malformed_rows"] += 1
                continue
            observation_id = row["observation_id"]
            if row.get("project_id") != project_id:
                counts["unknown_rows"] += 1
                continue
            surface = row["surface"]
            outcome = row["outcome"]
            reason = row["reason"]
            unknown = False
            if surface not in KNOWN_SURFACES:
                counts["unknown_surface_rows"] += 1
                unknown = True
            if outcome not in KNOWN_OUTCOMES:
                counts["unknown_outcome_rows"] += 1
                unknown = True
            if reason not in KNOWN_REASONS or not _labels_are_known(row):
                unknown = True
            if unknown:
                counts["unknown_rows"] += 1
                continue
            if not _observation_identity_is_valid(row):
                counts["malformed_rows"] += 1
                continue
            if observation_id in seen_ids:
                counts["duplicate_rows"] += 1
                continue
            seen_ids.add(observation_id)
            counts["observations"] += 1
            counts["unknown_labels"] += int(row["unknown_label_count"])
            surface_counts[surface] += 1
            outcome_counts[outcome] += 1
            reason_counts[reason] += 1
            for key, value in row["labels"].items():
                label_counts.setdefault(key, Counter())[value] += 1

    process_counts, failure_evidence = _best_effort_process_health_for_project(project_id)
    return LifecycleObservationReport(
        project_id=project_id,
        artifact_present=counts["artifact_present"],
        artifact_missing=counts["artifact_missing"],
        observations=counts["observations"],
        duplicate_rows=counts["duplicate_rows"],
        malformed_rows=counts["malformed_rows"],
        oversized_rows=counts["oversized_rows"],
        unknown_rows=counts["unknown_rows"],
        unknown_schema_rows=counts["unknown_schema_rows"],
        unknown_surface_rows=counts["unknown_surface_rows"],
        unknown_outcome_rows=counts["unknown_outcome_rows"],
        unknown_labels=counts["unknown_labels"],
        capacity_pressure=counts["capacity_pressure"],
        truncated_artifact=counts["truncated_artifact"],
        read_failures=counts["read_failures"] + process_counts["read_failures"],
        write_failures=process_counts["write_failures"],
        write_missing_targets=process_counts["write_missing_targets"],
        surface_counts=dict(sorted(surface_counts.items())),
        outcome_counts=dict(sorted(outcome_counts.items())),
        reason_counts=dict(sorted(reason_counts.items())),
        label_counts={
            key: dict(sorted(values.items()))
            for key, values in sorted(label_counts.items())
        },
        write_failure_evidence=failure_evidence,
    )


def aggregate_lifecycle_observability(
    reports: Iterable[LifecycleObservationReport],
) -> dict[str, object]:
    rows = tuple(reports)
    numeric_fields = (
        "artifact_present",
        "artifact_missing",
        "observations",
        "duplicate_rows",
        "malformed_rows",
        "oversized_rows",
        "unknown_rows",
        "unknown_schema_rows",
        "unknown_surface_rows",
        "unknown_outcome_rows",
        "unknown_labels",
        "capacity_pressure",
        "truncated_artifact",
        "read_failures",
        "write_failures",
        "write_missing_targets",
    )
    payload: dict[str, object] = {
        "repos_considered": len(rows),
        **{field: sum(int(getattr(row, field)) for row in rows) for field in numeric_fields},
    }
    surfaces: Counter[str] = Counter()
    outcomes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    labels: dict[str, Counter[str]] = {}
    for row in rows:
        surfaces.update(row.surface_counts)
        outcomes.update(row.outcome_counts)
        reasons.update(row.reason_counts)
        for key, values in row.label_counts.items():
            labels.setdefault(key, Counter()).update(values)
    payload["surface_counts"] = dict(sorted(surfaces.items()))
    payload["outcome_counts"] = dict(sorted(outcomes.items()))
    payload["reason_counts"] = dict(sorted(reasons.items()))
    payload["label_counts"] = {
        key: dict(sorted(values.items()))
        for key, values in sorted(labels.items())
    }
    if not rows or all(row.stream_health == "missing" for row in rows):
        payload["stream_health"] = "missing"
    elif any(row.stream_health != "healthy" for row in rows):
        payload["stream_health"] = "degraded"
    else:
        payload["stream_health"] = "healthy"
    return payload


def _mapping_value(
    payload: Mapping[str, object],
    key: str,
    default: object | None = None,
) -> object | None:
    try:
        return payload.get(key, default)
    except Exception:
        return default


def _normalize_surface(value: object) -> str:
    return value if isinstance(value, str) and value in KNOWN_SURFACES else "unknown"


def _mapping_text(payload: Mapping[str, object], key: str) -> str:
    value = _mapping_value(payload, key)
    if not isinstance(value, str):
        return ""
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) > 128:
        return "unknown"
    return value


def _bounded_identity_value(value: object) -> str:
    return _safe_bounded_hash("unset" if value is None else value)


def _operation_outcome(result: Mapping[str, object]) -> str:
    mutation_started = _mapping_value(result, "mutation_started") is True
    mutation_completed = _mapping_value(result, "mutation_completed") is True
    if mutation_started and not mutation_completed:
        return "partial"
    operation_status = _mapping_text(result, "operation_status").lower()
    if operation_status == "closed":
        return _closed_operation_outcome(result)
    operation_outcomes = {
        "observed": "success",
        "succeeded": "success",
        "success": "success",
        "completed": "success",
        "blocked": "blocked",
        "partial": "partial",
        "failed": "failed",
        "failure": "failed",
        "abandoned": "abandoned",
        "canceled": "abandoned",
        "cancelled": "abandoned",
    }
    if operation_status in operation_outcomes:
        return operation_outcomes[operation_status]
    status = _mapping_text(result, "status").lower()
    if status == "closed":
        return _closed_operation_outcome(result)
    return operation_outcomes.get(status, "unknown")


def _closed_operation_outcome(result: Mapping[str, object]) -> str:
    attempt_status = _mapping_text(result, "attempt_status").lower()
    if attempt_status == "failed":
        return "failed"
    if attempt_status == "abandoned":
        return "abandoned"
    return "blocked"


def _operation_result_label(result: Mapping[str, object]) -> str:
    explicit_result = _mapping_text(result, "result")
    dry_run = _mapping_value(result, "dry_run")
    apply = _mapping_value(result, "apply", _MISSING)
    changed = _mapping_value(result, "changed", _MISSING)
    mutation_started = _mapping_value(result, "mutation_started")
    mutation_completed = _mapping_value(result, "mutation_completed")
    if mutation_started is True and mutation_completed is True:
        return "applied"
    if mutation_started is True and mutation_completed is not True:
        return "unknown"
    if mutation_completed is True:
        return "unknown"
    if mutation_started is False and mutation_completed is False:
        if dry_run is True or apply is False or explicit_result == "dry_run":
            return "dry_run"
        if explicit_result in {"completed", "deduped", "noop"}:
            return explicit_result
        return "noop"
    if mutation_started is False or mutation_completed is False:
        return "unknown"
    if explicit_result in ALLOWED_LABEL_VALUES["result"]:
        return explicit_result
    if dry_run is True or apply is False:
        return "dry_run"
    if apply is True or changed is True:
        return "applied"
    if changed is False:
        return "noop"
    return "completed"


def _mutation_phase_label(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    return _MUTATION_PHASE_LABELS.get(value, "unknown")


def _retryability_label(result: Mapping[str, object]) -> str | None:
    value = _mapping_value(result, "retryability")
    if isinstance(value, str) and value in ALLOWED_LABEL_VALUES["retryability"]:
        return value
    retryable = _mapping_value(result, "retryable")
    if retryable is True:
        return "retryable"
    if retryable is False:
        return "terminal"
    return None


def _normalize_labels(labels: Mapping[str, object]) -> tuple[dict[str, str], int]:
    normalized: dict[str, str] = {}
    unknown = 0
    for raw_key, raw_value in labels.items():
        key = str(raw_key)
        allowed = ALLOWED_LABEL_VALUES.get(key)
        if allowed is None or len(normalized) >= MAX_LABELS:
            unknown += 1
            continue
        value = str(raw_value)
        normalized[key] = value if value in allowed else "unknown"
        if value not in allowed:
            unknown += 1
    return dict(sorted(normalized.items())), min(unknown, 255)


def _observation_row_shape_is_valid(row: Mapping[str, object]) -> bool:
    if set(row) != OBSERVATION_ROW_FIELDS:
        return False
    if not _labels_shape_is_valid(row):
        return False
    string_fields = (
        "project_id",
        "surface",
        "operation_key_hash",
        "outcome",
        "reason",
        "observation_id",
        "observed_at",
    )
    if any(not isinstance(row.get(field), str) for field in string_fields):
        return False
    return bool(
        _HEX_64.fullmatch(row["project_id"])
        and _HEX_64.fullmatch(row["operation_key_hash"])
        and _HEX_64.fullmatch(row["observation_id"])
        and row["observed_at"]
        and len(row["observed_at"]) <= 64
    )


def _labels_shape_is_valid(row: Mapping[str, object]) -> bool:
    labels = row.get("labels")
    unknown_label_count = row.get("unknown_label_count")
    if (
        not isinstance(labels, dict)
        or len(labels) > MAX_LABELS
        or not isinstance(unknown_label_count, int)
        or isinstance(unknown_label_count, bool)
        or not 0 <= unknown_label_count <= 255
    ):
        return False
    return all(isinstance(key, str) and isinstance(value, str) for key, value in labels.items())


def _labels_are_known(row: Mapping[str, object]) -> bool:
    labels = row["labels"]
    return all(
        key in ALLOWED_LABEL_VALUES
        and value in ALLOWED_LABEL_VALUES[key]
        for key, value in labels.items()
    )


def _observation_identity_is_valid(row: Mapping[str, object]) -> bool:
    identity = {
        "schema_version": row.get("schema_version"),
        "project_id": row.get("project_id"),
        "surface": row.get("surface"),
        "operation_key_hash": row.get("operation_key_hash"),
        "outcome": row.get("outcome"),
        "reason": row.get("reason"),
        "labels": row.get("labels"),
        "unknown_label_count": row.get("unknown_label_count"),
    }
    return row.get("observation_id") == _sha256_json(identity)


def _bounded_hash(value: object) -> str:
    raw = str(value).encode("utf-8", errors="replace")
    if len(raw) > MAX_HASH_INPUT_BYTES:
        raw = raw[:MAX_HASH_INPUT_BYTES] + f":bytes={len(raw)}".encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _safe_bounded_hash(value: object) -> str:
    try:
        return _bounded_hash(value)
    except Exception:
        return hashlib.sha256(b"unavailable-value").hexdigest()


def _sha256_json(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _append_observation_row(path: Path, row: Mapping[str, object]) -> str:
    _ensure_observation_parent(path)
    with _observation_lock(path):
        line = _serialize_observation(row)
        current_size = path.stat().st_size if path.exists() else 0
        if current_size + len(line.encode("utf-8")) + 1 > MAX_ARTIFACT_BYTES:
            return "capacity"
        if path.exists() and _observation_id_exists(path, str(row["observation_id"])):
            return "deduped"
        with _open_observation_file(path, "a") as handle:
            _write_text(handle, line + "\n")
            handle.flush()
            _fsync(handle)
    return "written"


def _ensure_observation_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _observation_lock(path: Path) -> AbstractContextManager[None]:
    return _nonblocking_observation_lock(path)


@contextmanager
def _nonblocking_observation_lock(path: Path):
    lock_path = path.with_name(f"{path.name}.lock")
    if fcntl is not None:
        with lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise _ObservationContention("observation stream is busy") from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    lock_dir = path.with_name(f"{path.name}.lockdir")
    try:
        lock_dir.mkdir()
    except FileExistsError as exc:
        raise _ObservationContention("observation stream is busy") from exc
    try:
        yield
    finally:
        try:
            lock_dir.rmdir()
        except OSError:
            pass


def _open_observation_file(path: Path, mode: str) -> IO:
    if "b" in mode:
        return path.open(mode)
    return path.open(mode, encoding="utf-8")


def _write_text(handle: IO, text: str) -> None:
    handle.write(text)


def _fsync(handle: IO) -> None:
    os.fsync(handle.fileno())


def _serialize_observation(row: Mapping[str, object]) -> str:
    line = json.dumps(row, sort_keys=True, separators=(",", ":"))
    if len(line.encode("utf-8")) > MAX_ROW_BYTES:
        raise ValueError("lifecycle observation exceeds row bound")
    return line


def _observation_id_exists(path: Path, observation_id: str) -> bool:
    with _open_observation_file(path, "rb") as handle:
        data = handle.read(MAX_ARTIFACT_BYTES)
    for raw_line in data.splitlines():
        if len(raw_line) > MAX_ROW_BYTES:
            continue
        try:
            row = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(row, dict) and row.get("observation_id") == observation_id:
            return True
    return False


def _record_process_count(project_id: str, key: str) -> None:
    with _PROCESS_LOCK:
        _process_counts_for_write(project_id)[key] += 1


def _best_effort_record_process_count(project_id: str, key: str) -> None:
    try:
        _record_process_count(project_id, key)
    except Exception:
        pass


def _record_write_failure(project_id: str, surface: str, observation_id: str, reason: str) -> None:
    with _PROCESS_LOCK:
        _process_counts_for_write(project_id)["write_failures"] += 1
        _PROCESS_FAILURE_EVIDENCE.append(
            {
                "project_id": project_id,
                "surface": surface if surface in KNOWN_SURFACES else "unknown",
                "observation_id": observation_id,
                "reason": reason if reason in {"capacity", "contention", "io"} else "unknown",
            }
        )


def _best_effort_record_write_failure(
    project_id: str,
    surface: str,
    observation_id: str,
    reason: str,
) -> None:
    try:
        _record_write_failure(project_id, surface, observation_id, reason)
    except Exception:
        pass


def _process_health_for_project(project_id: str) -> tuple[Counter[str], tuple[dict[str, str], ...]]:
    with _PROCESS_LOCK:
        counts = Counter(_PROCESS_COUNTS.get(project_id, {}))
        evidence = tuple(
            {key: value for key, value in row.items() if key != "project_id"}
            for row in _PROCESS_FAILURE_EVIDENCE
            if row["project_id"] == project_id
        )
        if evidence and not counts["write_failures"]:
            counts["write_failures"] = len(evidence)
    return counts, evidence


def _best_effort_process_health_for_project(
    project_id: str,
) -> tuple[Counter[str], tuple[dict[str, str], ...]]:
    try:
        return _process_health_for_project(project_id)
    except Exception:
        return Counter({"read_failures": 1}), ()


def _process_counts_for_write(project_id: str) -> Counter[str]:
    counts = _PROCESS_COUNTS.get(project_id)
    if counts is not None:
        _PROCESS_COUNTS.move_to_end(project_id)
        return counts
    if len(_PROCESS_COUNTS) >= MAX_PROCESS_PROJECTS:
        evicted_project_id, _ = _PROCESS_COUNTS.popitem(last=False)
        retained_evidence = tuple(
            row
            for row in _PROCESS_FAILURE_EVIDENCE
            if row["project_id"] != evicted_project_id
        )
        _PROCESS_FAILURE_EVIDENCE.clear()
        _PROCESS_FAILURE_EVIDENCE.extend(retained_evidence)
    counts = Counter()
    _PROCESS_COUNTS[project_id] = counts
    return counts


def _reset_process_health_for_tests() -> None:
    with _PROCESS_LOCK:
        _PROCESS_COUNTS.clear()
        _PROCESS_FAILURE_EVIDENCE.clear()


__all__ = [
    "ALLOWED_LABEL_VALUES",
    "KNOWN_OUTCOMES",
    "KNOWN_REASONS",
    "KNOWN_SURFACES",
    "LifecycleObservationReport",
    "MAX_ARTIFACT_BYTES",
    "MAX_FAILURE_EVIDENCE",
    "MAX_LABELS",
    "MAX_PROCESS_PROJECTS",
    "MAX_ROW_BYTES",
    "OBSERVATION_RELATIVE_PATH",
    "OBSERVATION_SCHEMA_VERSION",
    "ObservationWriteResult",
    "aggregate_lifecycle_observability",
    "lifecycle_observation_path",
    "observe_lifecycle",
    "observe_lifecycle_for_project",
    "observe_operation_result",
    "read_lifecycle_observability",
]
