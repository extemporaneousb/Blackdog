"""Durable product-layer landing transactions for WTAM attempts."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from blackdog_core.profile import RepoProfile
from blackdog_core.state import append_event_once, exclusive_file_lock, load_events


LANDING_EVENT_SCHEMA_VERSION = 1
LANDING_PHASE_EVENT_TYPE = "worktree.landing.phase"
LANDING_ABORT_EVENT_TYPE = "worktree.landing.abort"
LANDING_ABORT_CLEANUP_EVENT_TYPE = "worktree.landing.abort-cleanup"
LANDING_ABORT_SUPERSEDED_EVENT_TYPE = "worktree.landing.abort-superseded"
LANDING_ABORT_RUNTIME_EVENT_TYPE = "worktree.landing.abort-runtime-finalized"
LANDING_ABORT_CLOSE_EVENT_TYPE = "worktree.landing.abort-close-event-recorded"
LANDING_ABORT_COMPLETE_EVENT_TYPE = "worktree.landing.abort-complete"
LANDING_PHASES = (
    "intent_recorded",
    "source_prepared",
    "canonical_commit_created",
    "target_updated",
    "temporary_cleanup_complete",
    "runtime_finalized",
    "land_event_recorded",
    "task_cleanup_complete",
    "complete",
)


class LandingTransactionError(RuntimeError):
    """The append-only landing ledger is corrupt or semantically inconsistent."""


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LandingTransactionError("landing evidence is not canonical JSON") from exc


def strict_json_equal(left: object, right: object) -> bool:
    """Compare JSON evidence without Python's bool/int/float equality coercions."""
    return _canonical_json_bytes(left) == _canonical_json_bytes(right)


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise LandingTransactionError(
            f"landing transaction {field} must be a string"
        )
    resolved = value.strip()
    if not resolved:
        raise LandingTransactionError(f"landing transaction {field} must be nonempty")
    return resolved


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LandingTransactionError("landing transaction optional text must be a string or null")
    resolved = value.strip()
    return resolved or None


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise LandingTransactionError(f"landing transaction {field} must be a list")
    if any(not isinstance(item, str) for item in value):
        raise LandingTransactionError(
            f"landing transaction {field} items must be strings"
        )
    result = tuple(value)
    if any(not item.strip() for item in result):
        raise LandingTransactionError(f"landing transaction {field} contains an empty value")
    return result


def _validation_tuple(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise LandingTransactionError("landing transaction validations must be a list")
    result: list[tuple[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise LandingTransactionError("landing transaction validation rows must be objects")
        result.append(
            (
                _required_text(item.get("name"), field="validation name"),
                _required_text(item.get("status"), field="validation status"),
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class LandingIntent:
    workset_id: str
    task_id: str
    attempt_id: str
    actor: str
    branch: str
    target_branch: str
    worktree_path: str
    primary_worktree: str
    target_base_commit: str
    source_head_commit: str
    source_fingerprint: str
    expected_source_tree_hash: str
    source_dirty: bool
    summary: str
    note: str | None
    validations: tuple[tuple[str, str], ...]
    residuals: tuple[str, ...]
    followup_candidates: tuple[str, ...]
    changed_paths: tuple[str, ...]
    cleanup: bool
    commit_message: str
    temporary_worktree_path: str

    @property
    def transaction_id(self) -> str:
        return landing_transaction_id(
            workset_id=self.workset_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workset_id": self.workset_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "actor": self.actor,
            "branch": self.branch,
            "target_branch": self.target_branch,
            "worktree_path": self.worktree_path,
            "primary_worktree": self.primary_worktree,
            "target_base_commit": self.target_base_commit,
            "source_head_commit": self.source_head_commit,
            "source_fingerprint": self.source_fingerprint,
            "expected_source_tree_hash": self.expected_source_tree_hash,
            "source_dirty": self.source_dirty,
            "summary": self.summary,
            "note": self.note,
            "validations": [
                {"name": name, "status": status} for name, status in self.validations
            ],
            "residuals": list(self.residuals),
            "followup_candidates": list(self.followup_candidates),
            "changed_paths": list(self.changed_paths),
            "cleanup": self.cleanup,
            "commit_message": self.commit_message,
            "temporary_worktree_path": self.temporary_worktree_path,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LandingIntent":
        source_dirty = payload.get("source_dirty")
        cleanup = payload.get("cleanup")
        if not isinstance(source_dirty, bool) or not isinstance(cleanup, bool):
            raise LandingTransactionError(
                "landing transaction source_dirty and cleanup values must be booleans"
            )
        return cls(
            workset_id=_required_text(payload.get("workset_id"), field="workset_id"),
            task_id=_required_text(payload.get("task_id"), field="task_id"),
            attempt_id=_required_text(payload.get("attempt_id"), field="attempt_id"),
            actor=_required_text(payload.get("actor"), field="actor"),
            branch=_required_text(payload.get("branch"), field="branch"),
            target_branch=_required_text(payload.get("target_branch"), field="target_branch"),
            worktree_path=_required_text(payload.get("worktree_path"), field="worktree_path"),
            primary_worktree=_required_text(
                payload.get("primary_worktree"), field="primary_worktree"
            ),
            target_base_commit=_required_text(
                payload.get("target_base_commit"), field="target_base_commit"
            ),
            source_head_commit=_required_text(
                payload.get("source_head_commit"), field="source_head_commit"
            ),
            source_fingerprint=_required_text(
                payload.get("source_fingerprint"), field="source_fingerprint"
            ),
            expected_source_tree_hash=_required_text(
                payload.get("expected_source_tree_hash"),
                field="expected_source_tree_hash",
            ),
            source_dirty=source_dirty,
            summary=_required_text(payload.get("summary"), field="summary"),
            note=_optional_text(payload.get("note")),
            validations=_validation_tuple(payload.get("validations")),
            residuals=_string_tuple(payload.get("residuals"), field="residuals"),
            followup_candidates=_string_tuple(
                payload.get("followup_candidates"), field="followup_candidates"
            ),
            changed_paths=_string_tuple(
                payload.get("changed_paths"), field="changed_paths"
            ),
            cleanup=cleanup,
            commit_message=(
                _required_text(payload.get("commit_message"), field="commit_message")
                .rstrip("\n")
                + "\n"
            ),
            temporary_worktree_path=_required_text(
                payload.get("temporary_worktree_path"), field="temporary_worktree_path"
            ),
        )

    def request_identity(self) -> dict[str, Any]:
        """Return only caller-controlled values that must match on retry."""
        return {
            "actor": self.actor,
            "summary": self.summary,
            "note": self.note,
            "validations": self.validations,
            "residuals": self.residuals,
            "followup_candidates": self.followup_candidates,
            "cleanup": self.cleanup,
        }

    def task_land_argv(self, *, executable: str, project_root: Path) -> tuple[str, ...]:
        argv = [
            executable,
            "task",
            "land",
            f"--project-root={project_root}",
            f"--workset={self.workset_id}",
            f"--task={self.task_id}",
            f"--actor={self.actor}",
            f"--summary={self.summary}",
        ]
        argv.extend(f"--validation={name}={status}" for name, status in self.validations)
        argv.extend(f"--residual={item}" for item in self.residuals)
        argv.extend(f"--followup={item}" for item in self.followup_candidates)
        if self.note is not None:
            argv.append(f"--note={self.note}")
        if not self.cleanup:
            argv.append("--keep-worktree")
        return tuple(argv)


@dataclass(frozen=True, slots=True)
class LandingProof:
    source_commit: str
    landed_commit: str
    target_commit: str
    target_branch: str
    changed_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_commit": self.source_commit,
            "landed_commit": self.landed_commit,
            "target_commit": self.target_commit,
            "target_branch": self.target_branch,
            "changed_paths": list(self.changed_paths),
        }


@dataclass(frozen=True, slots=True)
class LandingTransaction:
    transaction_id: str
    intent: LandingIntent
    phases: tuple[str, ...]
    phase_data: Mapping[str, Mapping[str, Any]]
    abort_data: Mapping[str, Any] | None = None
    abort_cleanup_data: Mapping[str, Any] | None = None
    abort_superseded_data: Mapping[str, Any] | None = None
    abort_runtime_data: Mapping[str, Any] | None = None
    abort_close_data: Mapping[str, Any] | None = None
    abort_complete_data: Mapping[str, Any] | None = None

    @property
    def last_phase(self) -> str:
        return self.phases[-1]

    @property
    def next_phase(self) -> str | None:
        if self.aborted:
            return None
        if len(self.phases) == len(LANDING_PHASES):
            return None
        return LANDING_PHASES[len(self.phases)]

    @property
    def complete(self) -> bool:
        return self.phases == LANDING_PHASES

    @property
    def terminal(self) -> bool:
        return self.complete or self.abort_complete

    @property
    def outcome(self) -> str:
        if self.complete:
            return "landed_complete"
        if self.abort_complete:
            return "abort_complete"
        if self.aborted:
            return "abort_in_progress"
        return "landing_in_progress"

    @property
    def aborted(self) -> bool:
        return self.abort_data is not None and self.abort_superseded_data is None

    @property
    def abort_requested(self) -> bool:
        return self.abort_data is not None

    @property
    def abort_superseded(self) -> bool:
        return self.abort_superseded_data is not None

    @property
    def abort_cleanup_complete(self) -> bool:
        return self.abort_cleanup_data is not None

    @property
    def abort_runtime_finalized(self) -> bool:
        return self.abort_runtime_data is not None

    @property
    def abort_close_event_recorded(self) -> bool:
        return self.abort_close_data is not None

    @property
    def abort_complete(self) -> bool:
        return self.abort_complete_data is not None

    @property
    def target_updated(self) -> bool:
        return "target_updated" in self.phases

    def data_for(self, phase: str) -> Mapping[str, Any]:
        try:
            return self.phase_data[phase]
        except KeyError as exc:
            raise LandingTransactionError(
                f"landing transaction {self.transaction_id} is missing phase data for {phase}"
            ) from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "phases": list(self.phases),
            "last_phase": self.last_phase,
            "next_phase": self.next_phase,
            "complete": self.complete,
            "terminal": self.terminal,
            "outcome": self.outcome,
            "target_updated": self.target_updated,
            "aborted": self.aborted,
            "abort_cleanup_complete": self.abort_cleanup_complete,
            "abort_requested": self.abort_requested,
            "abort_superseded": self.abort_superseded,
            "abort_runtime_finalized": self.abort_runtime_finalized,
            "abort_close_event_recorded": self.abort_close_event_recorded,
            "abort_complete": self.abort_complete,
            "abort": dict(self.abort_data) if self.abort_data is not None else None,
            "abort_cleanup": (
                dict(self.abort_cleanup_data)
                if self.abort_cleanup_data is not None
                else None
            ),
            "abort_supersession": (
                dict(self.abort_superseded_data)
                if self.abort_superseded_data is not None
                else None
            ),
            "abort_runtime": (
                dict(self.abort_runtime_data)
                if self.abort_runtime_data is not None
                else None
            ),
            "abort_close": (
                dict(self.abort_close_data)
                if self.abort_close_data is not None
                else None
            ),
            "abort_completion": (
                dict(self.abort_complete_data)
                if self.abort_complete_data is not None
                else None
            ),
            "intent": self.intent.to_dict(),
        }


def landing_transaction_id(*, workset_id: str, task_id: str, attempt_id: str) -> str:
    material = "\0".join(
        (
            "blackdog.worktree.landing.transaction/v1",
            _required_text(workset_id, field="workset_id"),
            _required_text(task_id, field="task_id"),
            _required_text(attempt_id, field="attempt_id"),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def landing_phase_event_id(transaction_id: str, phase: str) -> str:
    if phase not in LANDING_PHASES:
        raise LandingTransactionError(f"unknown landing phase: {phase!r}")
    material = "\0".join(
        ("blackdog.worktree.landing.phase/v1", transaction_id, phase)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def worktree_land_event_id(transaction_id: str) -> str:
    return hashlib.sha256(
        f"blackdog.worktree.land/v1\0{transaction_id}".encode("utf-8")
    ).hexdigest()


def landing_abort_event_id(transaction_id: str) -> str:
    return hashlib.sha256(
        f"blackdog.worktree.landing.abort/v1\0{transaction_id}".encode("utf-8")
    ).hexdigest()


def landing_abort_cleanup_event_id(transaction_id: str) -> str:
    return hashlib.sha256(
        f"blackdog.worktree.landing.abort-cleanup/v1\0{transaction_id}".encode("utf-8")
    ).hexdigest()


def _landing_abort_stage_event_id(transaction_id: str, stage: str) -> str:
    return hashlib.sha256(
        f"blackdog.worktree.landing.{stage}/v1\0{transaction_id}".encode("utf-8")
    ).hexdigest()


def landing_abort_superseded_event_id(transaction_id: str) -> str:
    return _landing_abort_stage_event_id(transaction_id, "abort-superseded")


def landing_abort_runtime_event_id(transaction_id: str) -> str:
    return _landing_abort_stage_event_id(transaction_id, "abort-runtime-finalized")


def landing_abort_close_event_id(transaction_id: str) -> str:
    return _landing_abort_stage_event_id(transaction_id, "abort-close-event-recorded")


def landing_abort_complete_event_id(transaction_id: str) -> str:
    return _landing_abort_stage_event_id(transaction_id, "abort-complete")


def _phase_payload(
    intent: LandingIntent,
    *,
    phase: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": LANDING_EVENT_SCHEMA_VERSION,
        "transaction_id": intent.transaction_id,
        "workset_id": intent.workset_id,
        "task_id": intent.task_id,
        "attempt_id": intent.attempt_id,
        "phase": phase,
        "data": dict(data),
    }


def load_landing_transaction(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
) -> LandingTransaction | None:
    transaction_id = landing_transaction_id(
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    with exclusive_file_lock(profile.paths.events_file):
        events = load_events(profile.paths.events_file)
    rows: list[tuple[str, Mapping[str, Any]]] = []
    row_actors: list[str] = []
    abort_row: Mapping[str, Any] | None = None
    abort_actor: str | None = None
    abort_cleanup_row: Mapping[str, Any] | None = None
    abort_cleanup_actor: str | None = None
    abort_superseded_row: Mapping[str, Any] | None = None
    abort_superseded_actor: str | None = None
    abort_runtime_row: Mapping[str, Any] | None = None
    abort_runtime_actor: str | None = None
    abort_close_row: Mapping[str, Any] | None = None
    abort_close_actor: str | None = None
    abort_complete_row: Mapping[str, Any] | None = None
    abort_complete_actor: str | None = None
    phase_positions: list[int] = []
    abort_positions: dict[str, int] = {}
    seen_phases: set[str] = set()
    canonical_ids = {
        landing_phase_event_id(transaction_id, phase): phase for phase in LANDING_PHASES
    }
    abort_ids = {
        landing_abort_event_id(transaction_id): LANDING_ABORT_EVENT_TYPE,
        landing_abort_cleanup_event_id(transaction_id): LANDING_ABORT_CLEANUP_EVENT_TYPE,
        landing_abort_superseded_event_id(transaction_id): LANDING_ABORT_SUPERSEDED_EVENT_TYPE,
        landing_abort_runtime_event_id(transaction_id): LANDING_ABORT_RUNTIME_EVENT_TYPE,
        landing_abort_close_event_id(transaction_id): LANDING_ABORT_CLOSE_EVENT_TYPE,
        landing_abort_complete_event_id(transaction_id): LANDING_ABORT_COMPLETE_EVENT_TYPE,
    }
    for event_index, event in enumerate(events):
        payload = event.get("payload")
        event_id = str(event.get("event_id") or "")
        expected_abort_type = abort_ids.get(event_id)
        if expected_abort_type is not None and (
            event.get("type") != expected_abort_type
            or not isinstance(payload, Mapping)
            or payload.get("transaction_id") != transaction_id
        ):
            raise LandingTransactionError(
                f"landing transaction {transaction_id} abort identity is occupied by conflicting content"
            )
        if event.get("type") in {
            LANDING_ABORT_EVENT_TYPE,
            LANDING_ABORT_CLEANUP_EVENT_TYPE,
            LANDING_ABORT_SUPERSEDED_EVENT_TYPE,
            LANDING_ABORT_RUNTIME_EVENT_TYPE,
            LANDING_ABORT_CLOSE_EVENT_TYPE,
            LANDING_ABORT_COMPLETE_EVENT_TYPE,
        } and isinstance(payload, Mapping) and payload.get("transaction_id") == transaction_id:
            expected_type = str(event.get("type"))
            expected_id = (
                landing_abort_event_id(transaction_id)
                if expected_type == LANDING_ABORT_EVENT_TYPE
                else landing_abort_cleanup_event_id(transaction_id)
                if expected_type == LANDING_ABORT_CLEANUP_EVENT_TYPE
                else landing_abort_superseded_event_id(transaction_id)
                if expected_type == LANDING_ABORT_SUPERSEDED_EVENT_TYPE
                else landing_abort_runtime_event_id(transaction_id)
                if expected_type == LANDING_ABORT_RUNTIME_EVENT_TYPE
                else landing_abort_close_event_id(transaction_id)
                if expected_type == LANDING_ABORT_CLOSE_EVENT_TYPE
                else landing_abort_complete_event_id(transaction_id)
            )
            if event.get("event_id") != expected_id:
                raise LandingTransactionError(
                    f"landing transaction {transaction_id} abort row has a noncanonical event id"
                )
            if set(payload) != {
                "schema_version",
                "transaction_id",
                "workset_id",
                "task_id",
                "attempt_id",
                "data",
            }:
                raise LandingTransactionError(
                    f"landing transaction {transaction_id} abort envelope has conflicting fields"
                )
            if (
                type(payload.get("schema_version")) is not int
                or payload.get("schema_version") != LANDING_EVENT_SCHEMA_VERSION
                or payload.get("workset_id") != workset_id
                or payload.get("task_id") != task_id
                or payload.get("attempt_id") != attempt_id
                or not isinstance(payload.get("data"), Mapping)
            ):
                raise LandingTransactionError(
                    f"landing transaction {transaction_id} abort envelope is invalid"
                )
            event_actor = event.get("actor")
            if not isinstance(event_actor, str) or not event_actor.strip():
                raise LandingTransactionError(
                    f"landing transaction {transaction_id} abort actor is invalid"
                )
            if expected_type == LANDING_ABORT_EVENT_TYPE:
                if abort_row is not None:
                    raise LandingTransactionError(
                        f"landing transaction {transaction_id} abort occurs more than once"
                    )
                abort_row = dict(payload["data"])
                abort_actor = event_actor
            elif expected_type == LANDING_ABORT_CLEANUP_EVENT_TYPE:
                if abort_cleanup_row is not None:
                    raise LandingTransactionError(
                        f"landing transaction {transaction_id} abort cleanup occurs more than once"
                    )
                abort_cleanup_row = dict(payload["data"])
                abort_cleanup_actor = event_actor
            elif expected_type == LANDING_ABORT_SUPERSEDED_EVENT_TYPE:
                if abort_superseded_row is not None:
                    raise LandingTransactionError(
                        f"landing transaction {transaction_id} abort supersession occurs more than once"
                    )
                abort_superseded_row = dict(payload["data"])
                abort_superseded_actor = event_actor
            elif expected_type == LANDING_ABORT_RUNTIME_EVENT_TYPE:
                if abort_runtime_row is not None:
                    raise LandingTransactionError(
                        f"landing transaction {transaction_id} abort runtime phase occurs more than once"
                    )
                abort_runtime_row = dict(payload["data"])
                abort_runtime_actor = event_actor
            elif expected_type == LANDING_ABORT_CLOSE_EVENT_TYPE:
                if abort_close_row is not None:
                    raise LandingTransactionError(
                        f"landing transaction {transaction_id} abort close phase occurs more than once"
                    )
                abort_close_row = dict(payload["data"])
                abort_close_actor = event_actor
            else:
                if abort_complete_row is not None:
                    raise LandingTransactionError(
                        f"landing transaction {transaction_id} abort completion occurs more than once"
                    )
                abort_complete_row = dict(payload["data"])
                abort_complete_actor = event_actor
            abort_positions[expected_type] = event_index
            continue
        canonical_phase = canonical_ids.get(str(event.get("event_id") or ""))
        if canonical_phase is not None and (
            event.get("type") != LANDING_PHASE_EVENT_TYPE
            or not isinstance(payload, Mapping)
            or payload.get("transaction_id") != transaction_id
            or payload.get("phase") != canonical_phase
        ):
            raise LandingTransactionError(
                f"landing transaction {transaction_id} canonical phase id for "
                f"{canonical_phase!r} is occupied by conflicting content"
            )
        if event.get("type") != LANDING_PHASE_EVENT_TYPE or not isinstance(payload, Mapping):
            continue
        if payload.get("transaction_id") != transaction_id:
            continue
        expected_payload_keys = {
            "schema_version",
            "transaction_id",
            "workset_id",
            "task_id",
            "attempt_id",
            "phase",
            "data",
        }
        if set(payload) != expected_payload_keys:
            raise LandingTransactionError(
                f"landing transaction {transaction_id} phase envelope has conflicting fields"
            )
        if (
            type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != LANDING_EVENT_SCHEMA_VERSION
        ):
            raise LandingTransactionError(
                f"landing transaction {transaction_id} has unsupported phase schema"
            )
        common = {
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt_id,
        }
        mismatches = [key for key, value in common.items() if payload.get(key) != value]
        if mismatches:
            raise LandingTransactionError(
                f"landing transaction {transaction_id} identity conflicts on: {', '.join(mismatches)}"
            )
        phase_value = payload.get("phase")
        if not isinstance(phase_value, str):
            raise LandingTransactionError(
                f"landing transaction {transaction_id} phase must be a string"
            )
        phase = phase_value
        if phase not in LANDING_PHASES:
            raise LandingTransactionError(
                f"landing transaction {transaction_id} has unknown phase {phase!r}"
            )
        expected_event_id = landing_phase_event_id(transaction_id, phase)
        if event.get("event_id") != expected_event_id:
            raise LandingTransactionError(
                f"landing transaction {transaction_id} phase {phase!r} has a noncanonical event id"
            )
        if phase in seen_phases:
            raise LandingTransactionError(
                f"landing transaction {transaction_id} phase {phase!r} occurs more than once"
            )
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise LandingTransactionError(
                f"landing transaction {transaction_id} phase {phase!r} data must be an object"
            )
        seen_phases.add(phase)
        rows.append((phase, data))
        phase_positions.append(event_index)
        event_actor = event.get("actor")
        if not isinstance(event_actor, str) or not event_actor.strip():
            raise LandingTransactionError(
                f"landing transaction {transaction_id} phase actor must be a nonempty string"
            )
        row_actors.append(event_actor)
    if not rows and abort_positions:
        raise LandingTransactionError(
            f"landing transaction {transaction_id} has abort evidence without immutable intent"
        )
    if not rows:
        return None
    phases = tuple(phase for phase, _data in rows)
    expected_prefix = LANDING_PHASES[: len(phases)]
    if phases != expected_prefix:
        raise LandingTransactionError(
            f"landing transaction {transaction_id} phases are not a strict prefix: {phases!r}"
        )
    intent = LandingIntent.from_dict(rows[0][1])
    if not strict_json_equal(intent.to_dict(), dict(rows[0][1])):
        raise LandingTransactionError(
            f"landing transaction {transaction_id} intent data is not canonical"
        )
    if intent.transaction_id != transaction_id:
        raise LandingTransactionError(
            f"landing transaction {transaction_id} intent identity does not match its event envelope"
        )
    if any(actor != intent.actor for actor in row_actors):
        raise LandingTransactionError(
            f"landing transaction {transaction_id} phase actor does not match immutable intent actor"
        )
    if abort_row is not None:
        if abort_actor != intent.actor:
            raise LandingTransactionError(
                f"landing transaction {transaction_id} abort actor conflicts with intent"
            )
        if (
            abort_superseded_row is None
            and ("target_updated" in phases or phases == LANDING_PHASES)
        ):
            raise LandingTransactionError(
                f"landing transaction {transaction_id} cannot be both aborted and target-updated"
            )
    abort_owned_actors = (
        abort_cleanup_actor,
        abort_superseded_actor,
        abort_runtime_actor,
        abort_close_actor,
        abort_complete_actor,
    )
    if any(actor is not None and actor != intent.actor for actor in abort_owned_actors):
        raise LandingTransactionError(
            f"landing transaction {transaction_id} abort phase actor conflicts with intent"
        )
    if abort_row is not None:
        abort_position = abort_positions[LANDING_ABORT_EVENT_TYPE]
        if not phase_positions or phase_positions[0] > abort_position:
            raise LandingTransactionError(
                f"landing transaction {transaction_id} abort precedes immutable intent"
            )
        supersede_position = abort_positions.get(LANDING_ABORT_SUPERSEDED_EVENT_TYPE)
        for position in phase_positions:
            if position <= abort_position:
                continue
            if supersede_position is None or position <= supersede_position:
                raise LandingTransactionError(
                    f"landing transaction {transaction_id} phase occurs after abort without supersession"
                )
    ordered_abort_types = (
        LANDING_ABORT_EVENT_TYPE,
        LANDING_ABORT_CLEANUP_EVENT_TYPE,
        LANDING_ABORT_RUNTIME_EVENT_TYPE,
        LANDING_ABORT_CLOSE_EVENT_TYPE,
        LANDING_ABORT_COMPLETE_EVENT_TYPE,
    )
    prior_position: int | None = None
    gap = False
    for event_type in ordered_abort_types:
        position = abort_positions.get(event_type)
        if position is None:
            gap = prior_position is not None
            continue
        if abort_row is None or gap or (prior_position is not None and position <= prior_position):
            raise LandingTransactionError(
                f"landing transaction {transaction_id} abort phases are reordered or have gaps"
            )
        prior_position = position
    if abort_superseded_row is not None:
        supersede_position = abort_positions[LANDING_ABORT_SUPERSEDED_EVENT_TYPE]
        abort_position = abort_positions.get(LANDING_ABORT_EVENT_TYPE)
        cleanup_position = abort_positions.get(LANDING_ABORT_CLEANUP_EVENT_TYPE)
        if (
            abort_position is None
            or cleanup_position is None
            or not (abort_position < cleanup_position < supersede_position)
        ):
            raise LandingTransactionError(
                f"landing transaction {transaction_id} abort supersession is reordered"
            )
        if any(
            row is not None
            for row in (abort_runtime_row, abort_close_row, abort_complete_row)
        ):
            raise LandingTransactionError(
                f"landing transaction {transaction_id} superseded abort also has terminal abort phases"
            )
    return LandingTransaction(
        transaction_id=transaction_id,
        intent=intent,
        phases=phases,
        phase_data={phase: dict(data) for phase, data in rows},
        abort_data=abort_row,
        abort_cleanup_data=abort_cleanup_row,
        abort_superseded_data=abort_superseded_row,
        abort_runtime_data=abort_runtime_row,
        abort_close_data=abort_close_row,
        abort_complete_data=abort_complete_row,
    )


def record_landing_phase(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    phase: str,
    data: Mapping[str, Any],
) -> bool:
    if phase not in LANDING_PHASES:
        raise LandingTransactionError(f"unknown landing phase: {phase!r}")
    current = load_landing_transaction(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
    )
    if current is not None and current.aborted:
        raise LandingTransactionError(
            f"landing transaction {intent.transaction_id} is durably aborted"
        )
    if current is None and phase != "intent_recorded":
        raise LandingTransactionError(
            f"landing transaction {intent.transaction_id} must record immutable intent first"
        )
    if current is not None and phase not in current.phases and current.next_phase != phase:
        raise LandingTransactionError(
            f"landing transaction {intent.transaction_id} cannot record {phase!r}; "
            f"next phase is {current.next_phase!r}"
        )
    resolved_data = intent.to_dict() if phase == "intent_recorded" else dict(data)
    return append_event_once(
        profile.paths.events_file,
        event_id=landing_phase_event_id(intent.transaction_id, phase),
        event_type=LANDING_PHASE_EVENT_TYPE,
        actor=intent.actor,
        payload=_phase_payload(intent, phase=phase, data=resolved_data),
    )


def _abort_payload(intent: LandingIntent, *, data: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": LANDING_EVENT_SCHEMA_VERSION,
        "transaction_id": intent.transaction_id,
        "workset_id": intent.workset_id,
        "task_id": intent.task_id,
        "attempt_id": intent.attempt_id,
        "data": dict(data),
    }


def record_landing_abort(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    data: Mapping[str, Any],
) -> bool:
    current = load_landing_transaction(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
    )
    if current is None:
        raise LandingTransactionError("cannot abort a landing transaction without intent")
    if current.complete or current.target_updated:
        raise LandingTransactionError("cannot abort a target-updated landing transaction")
    return append_event_once(
        profile.paths.events_file,
        event_id=landing_abort_event_id(intent.transaction_id),
        event_type=LANDING_ABORT_EVENT_TYPE,
        actor=intent.actor,
        payload=_abort_payload(intent, data=data),
    )


def record_landing_abort_cleanup(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    data: Mapping[str, Any],
) -> bool:
    current = load_landing_transaction(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
    )
    if current is None or not current.aborted:
        raise LandingTransactionError("cannot record abort cleanup before durable abort")
    return append_event_once(
        profile.paths.events_file,
        event_id=landing_abort_cleanup_event_id(intent.transaction_id),
        event_type=LANDING_ABORT_CLEANUP_EVENT_TYPE,
        actor=intent.actor,
        payload=_abort_payload(intent, data=data),
    )


def _record_abort_stage(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    event_id: str,
    event_type: str,
    data: Mapping[str, Any],
) -> bool:
    return append_event_once(
        profile.paths.events_file,
        event_id=event_id,
        event_type=event_type,
        actor=intent.actor,
        payload=_abort_payload(intent, data=data),
    )


def record_landing_abort_superseded(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    data: Mapping[str, Any],
) -> bool:
    current = load_landing_transaction(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
    )
    if current is None or not current.aborted or current.abort_runtime_finalized:
        raise LandingTransactionError("abort supersession requires an unfinalized abort intent")
    return _record_abort_stage(
        profile,
        intent=intent,
        event_id=landing_abort_superseded_event_id(intent.transaction_id),
        event_type=LANDING_ABORT_SUPERSEDED_EVENT_TYPE,
        data=data,
    )


def record_landing_abort_runtime(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    data: Mapping[str, Any],
) -> bool:
    current = load_landing_transaction(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
    )
    if current is None or not current.aborted or not current.abort_cleanup_complete:
        raise LandingTransactionError("abort runtime finalization requires durable abort cleanup")
    return _record_abort_stage(
        profile,
        intent=intent,
        event_id=landing_abort_runtime_event_id(intent.transaction_id),
        event_type=LANDING_ABORT_RUNTIME_EVENT_TYPE,
        data=data,
    )


def record_landing_abort_close_event(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    data: Mapping[str, Any],
) -> bool:
    current = load_landing_transaction(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
    )
    if current is None or not current.abort_runtime_finalized:
        raise LandingTransactionError("abort close event requires runtime finalization")
    return _record_abort_stage(
        profile,
        intent=intent,
        event_id=landing_abort_close_event_id(intent.transaction_id),
        event_type=LANDING_ABORT_CLOSE_EVENT_TYPE,
        data=data,
    )


def record_landing_abort_complete(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    data: Mapping[str, Any],
) -> bool:
    current = load_landing_transaction(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
    )
    if current is None or not current.abort_close_event_recorded:
        raise LandingTransactionError("abort completion requires close event evidence")
    return _record_abort_stage(
        profile,
        intent=intent,
        event_id=landing_abort_complete_event_id(intent.transaction_id),
        event_type=LANDING_ABORT_COMPLETE_EVENT_TYPE,
        data=data,
    )


def append_worktree_land_once(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    payload: Mapping[str, Any],
) -> bool:
    resolved_payload = {**dict(payload), "transaction_id": intent.transaction_id}
    return append_event_once(
        profile.paths.events_file,
        event_id=worktree_land_event_id(intent.transaction_id),
        event_type="worktree.land",
        actor=intent.actor,
        payload=resolved_payload,
    )


def exact_worktree_land_event(
    profile: RepoProfile,
    *,
    intent: LandingIntent,
    payload: Mapping[str, Any],
) -> bool:
    expected_id = worktree_land_event_id(intent.transaction_id)
    expected_payload = {**dict(payload), "transaction_id": intent.transaction_id}
    with exclusive_file_lock(profile.paths.events_file):
        matches = [
            event
            for event in load_events(profile.paths.events_file)
            if event.get("event_id") == expected_id
        ]
    return len(matches) == 1 and (
        matches[0].get("type") == "worktree.land"
        and matches[0].get("actor") == intent.actor
        and strict_json_equal(matches[0].get("payload"), expected_payload)
    )


@contextmanager
def attempt_lifecycle_lock(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
) -> Iterator[None]:
    transaction_id = landing_transaction_id(
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    protected_path = (
        profile.paths.control_dir / "locks" / f"attempt-{transaction_id}"
    ).resolve(strict=False)
    with exclusive_file_lock(protected_path):
        yield


__all__ = [
    "LANDING_EVENT_SCHEMA_VERSION",
    "LANDING_ABORT_EVENT_TYPE",
    "LANDING_ABORT_CLEANUP_EVENT_TYPE",
    "LANDING_ABORT_SUPERSEDED_EVENT_TYPE",
    "LANDING_ABORT_RUNTIME_EVENT_TYPE",
    "LANDING_ABORT_CLOSE_EVENT_TYPE",
    "LANDING_ABORT_COMPLETE_EVENT_TYPE",
    "LANDING_PHASE_EVENT_TYPE",
    "LANDING_PHASES",
    "LandingIntent",
    "LandingProof",
    "LandingTransaction",
    "LandingTransactionError",
    "append_worktree_land_once",
    "attempt_lifecycle_lock",
    "exact_worktree_land_event",
    "landing_phase_event_id",
    "landing_abort_event_id",
    "landing_abort_cleanup_event_id",
    "landing_abort_superseded_event_id",
    "landing_abort_runtime_event_id",
    "landing_abort_close_event_id",
    "landing_abort_complete_event_id",
    "landing_transaction_id",
    "load_landing_transaction",
    "record_landing_phase",
    "record_landing_abort",
    "record_landing_abort_cleanup",
    "record_landing_abort_superseded",
    "record_landing_abort_runtime",
    "record_landing_abort_close_event",
    "record_landing_abort_complete",
    "strict_json_equal",
    "worktree_land_event_id",
]
