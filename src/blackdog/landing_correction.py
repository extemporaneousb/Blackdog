from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from blackdog.validation import ValidationRunResult
from blackdog_core.profile import RepoProfile
from blackdog_core.state import append_event_once, exclusive_file_lock, load_events


LANDING_CORRECTION_SCHEMA_VERSION = 1
LANDING_CORRECTION_EVENT_TYPE = "worktree.landing.correction"

PHASE_INTENT_RECORDED = "intent_recorded"
PHASE_REBASE_COMPLETED = "rebase_completed"
PHASE_VALIDATION_COMPLETED = "validation_completed"
PHASE_BLOCKED = "blocked"
PHASE_HANDED_TO_LANDING = "handed_to_landing"

LANDING_CORRECTION_PHASES = (
    PHASE_INTENT_RECORDED,
    PHASE_REBASE_COMPLETED,
    PHASE_VALIDATION_COMPLETED,
    PHASE_BLOCKED,
    PHASE_HANDED_TO_LANDING,
)
TERMINAL_LANDING_CORRECTION_PHASES = frozenset(
    {PHASE_BLOCKED, PHASE_HANDED_TO_LANDING}
)

_INTENT_KEYS = {
    "workset_id",
    "task_id",
    "attempt_id",
    "actor",
    "branch",
    "target_branch",
    "worktree_path",
    "source_head_commit",
    "target_commit",
    "source_tree_hash",
    "request_identity_hash",
    "validation_policy_hash",
    "generation",
    "resume_argv",
}
_EVENT_PAYLOAD_KEYS = {
    "schema_version",
    "correction_id",
    "workset_id",
    "task_id",
    "attempt_id",
    "phase",
    "data",
}
_REBASE_DATA_KEYS = {
    "source_head_commit",
    "target_commit",
    "source_tree_hash",
}
_VALIDATION_DATA_KEYS = {
    "source_head_commit",
    "target_commit",
    "source_tree_hash",
    "validation_policy_hash",
    "validation_evidence_hash",
    "validation_count",
    "all_passed",
    "validation",
}
_BLOCKED_DATA_KEYS = {"reason_code", "blocked_after", "validation"}
_HANDED_DATA_KEYS = {"landing_transaction_id", "landing_intent_event_id"}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class LandingCorrectionError(RuntimeError):
    """Landing correction evidence is invalid, ambiguous, or forked."""


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise LandingCorrectionError(
            f"landing correction evidence is not canonical JSON: {exc}"
        ) from exc
    return rendered.encode("utf-8")


def _strict_json_equal(left: Any, right: Any) -> bool:
    return _canonical_json_bytes(left) == _canonical_json_bytes(right)


def _require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LandingCorrectionError(f"{field} must be an object")
    return value


def _require_exact_keys(
    value: Mapping[str, Any],
    *,
    keys: set[str],
    field: str,
) -> None:
    actual = set(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise LandingCorrectionError(
            f"{field} has invalid fields: missing={missing!r}, extra={extra!r}"
        )


def _require_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LandingCorrectionError(f"{field} must be a non-empty string")
    return value


def _require_hash(value: Any, *, field: str) -> str:
    text = _require_text(value, field=field)
    if _HASH_RE.fullmatch(text) is None:
        raise LandingCorrectionError(
            f"{field} must be a lowercase 64-character hexadecimal digest"
        )
    return text


def _require_git_oid(value: Any, *, field: str) -> str:
    text = _require_text(value, field=field)
    if _GIT_OID_RE.fullmatch(text) is None:
        raise LandingCorrectionError(
            f"{field} must be a lowercase 40- or 64-character Git object id"
        )
    return text


def _require_strict_int(value: Any, *, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise LandingCorrectionError(
            f"{field} must be an integer greater than or equal to {minimum}"
        )
    return value


@dataclass(frozen=True, slots=True)
class LandingCorrectionIntent:
    workset_id: str
    task_id: str
    attempt_id: str
    actor: str
    branch: str
    target_branch: str
    worktree_path: str
    source_head_commit: str
    target_commit: str
    source_tree_hash: str
    request_identity_hash: str
    validation_policy_hash: str
    generation: int = 1
    resume_argv: tuple[str, ...] = ("blackdog", "task", "land")

    def __post_init__(self) -> None:
        for field_name in (
            "workset_id",
            "task_id",
            "attempt_id",
            "actor",
            "branch",
            "target_branch",
            "worktree_path",
        ):
            _require_text(getattr(self, field_name), field=field_name)
        _require_git_oid(self.source_head_commit, field="source_head_commit")
        _require_git_oid(self.target_commit, field="target_commit")
        _require_hash(self.source_tree_hash, field="source_tree_hash")
        _require_hash(self.request_identity_hash, field="request_identity_hash")
        _require_hash(self.validation_policy_hash, field="validation_policy_hash")
        _require_strict_int(self.generation, field="generation", minimum=1)
        if not self.resume_argv or any(
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            for argument in self.resume_argv
        ):
            raise LandingCorrectionError(
                "resume_argv must contain nonempty NUL-free strings"
            )

    @property
    def correction_id(self) -> str:
        return landing_correction_id(
            workset_id=self.workset_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
            actor=self.actor,
            branch=self.branch,
            target_branch=self.target_branch,
            worktree_path=self.worktree_path,
            source_head_commit=self.source_head_commit,
            target_commit=self.target_commit,
            source_tree_hash=self.source_tree_hash,
            request_identity_hash=self.request_identity_hash,
            validation_policy_hash=self.validation_policy_hash,
            generation=self.generation,
            resume_argv=self.resume_argv,
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
            "source_head_commit": self.source_head_commit,
            "target_commit": self.target_commit,
            "source_tree_hash": self.source_tree_hash,
            "request_identity_hash": self.request_identity_hash,
            "validation_policy_hash": self.validation_policy_hash,
            "generation": self.generation,
            "resume_argv": list(self.resume_argv),
        }

    @classmethod
    def from_dict(cls, value: Any) -> LandingCorrectionIntent:
        row = _require_mapping(value, field="landing correction intent")
        _require_exact_keys(
            row,
            keys=_INTENT_KEYS,
            field="landing correction intent",
        )
        return cls(
            workset_id=_require_text(row["workset_id"], field="workset_id"),
            task_id=_require_text(row["task_id"], field="task_id"),
            attempt_id=_require_text(row["attempt_id"], field="attempt_id"),
            actor=_require_text(row["actor"], field="actor"),
            branch=_require_text(row["branch"], field="branch"),
            target_branch=_require_text(
                row["target_branch"], field="target_branch"
            ),
            worktree_path=_require_text(row["worktree_path"], field="worktree_path"),
            source_head_commit=_require_git_oid(
                row["source_head_commit"], field="source_head_commit"
            ),
            target_commit=_require_git_oid(
                row["target_commit"], field="target_commit"
            ),
            source_tree_hash=_require_hash(
                row["source_tree_hash"], field="source_tree_hash"
            ),
            request_identity_hash=_require_hash(
                row["request_identity_hash"], field="request_identity_hash"
            ),
            validation_policy_hash=_require_hash(
                row["validation_policy_hash"], field="validation_policy_hash"
            ),
            generation=_require_strict_int(
                row["generation"],
                field="generation",
                minimum=1,
            ),
            resume_argv=tuple(
                _require_text(argument, field="resume_argv argument")
                for argument in (
                    row["resume_argv"]
                    if isinstance(row["resume_argv"], list)
                    else ()
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class LandingCorrection:
    correction_id: str
    intent: LandingCorrectionIntent
    phases: tuple[str, ...]
    phase_data: Mapping[str, Mapping[str, Any]]
    event_positions: tuple[int, ...]

    @property
    def last_phase(self) -> str:
        return self.phases[-1]

    @property
    def terminal(self) -> bool:
        return self.last_phase in TERMINAL_LANDING_CORRECTION_PHASES

    @property
    def active(self) -> bool:
        return not self.terminal

    @property
    def blocked(self) -> bool:
        return self.last_phase == PHASE_BLOCKED

    @property
    def handed_to_landing(self) -> bool:
        return self.last_phase == PHASE_HANDED_TO_LANDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": LANDING_CORRECTION_SCHEMA_VERSION,
            "correction_id": self.correction_id,
            "intent": self.intent.to_dict(),
            "phases": list(self.phases),
            "phase_data": {
                phase: dict(self.phase_data[phase]) for phase in self.phases
            },
            "active": self.active,
            "terminal": self.terminal,
        }


@dataclass(frozen=True, slots=True)
class LandingCorrectionSelection:
    corrections: tuple[LandingCorrection, ...]
    active: LandingCorrection | None
    latest_terminal: LandingCorrection | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "corrections": [correction.to_dict() for correction in self.corrections],
            "active_correction_id": (
                self.active.correction_id if self.active is not None else None
            ),
            "latest_terminal_correction_id": (
                self.latest_terminal.correction_id
                if self.latest_terminal is not None
                else None
            ),
        }


def landing_correction_id(
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    actor: str,
    branch: str,
    target_branch: str,
    worktree_path: str,
    source_head_commit: str,
    target_commit: str,
    source_tree_hash: str,
    request_identity_hash: str,
    validation_policy_hash: str,
    generation: int,
    resume_argv: Sequence[str],
) -> str:
    material = {
        "namespace": "blackdog.worktree.landing.correction/v1",
        "workset_id": workset_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "actor": actor,
        "branch": branch,
        "target_branch": target_branch,
        "worktree_path": worktree_path,
        "source_head_commit": source_head_commit,
        "target_commit": target_commit,
        "source_tree_hash": source_tree_hash,
        "request_identity_hash": request_identity_hash,
        "validation_policy_hash": validation_policy_hash,
        "generation": generation,
        "resume_argv": list(resume_argv),
    }
    return hashlib.sha256(_canonical_json_bytes(material)).hexdigest()


def landing_correction_phase_event_id(correction_id: str, phase: str) -> str:
    correction_id = _require_hash(correction_id, field="correction_id")
    if phase not in LANDING_CORRECTION_PHASES:
        raise LandingCorrectionError(f"unsupported landing correction phase {phase!r}")
    material = (
        "blackdog.worktree.landing.correction.phase/v1"
        f"\0{correction_id}\0{phase}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _events_file(profile: RepoProfile) -> Path:
    return profile.paths.events_file


def _canonical_phase_data(phase: str, value: Any) -> dict[str, Any]:
    row = _require_mapping(value, field=f"{phase} data")
    if phase == PHASE_INTENT_RECORDED:
        intent = LandingCorrectionIntent.from_dict(row)
        return intent.to_dict()
    if phase == PHASE_REBASE_COMPLETED:
        _require_exact_keys(row, keys=_REBASE_DATA_KEYS, field=f"{phase} data")
        return {
            "source_head_commit": _require_git_oid(
                row["source_head_commit"], field="source_head_commit"
            ),
            "target_commit": _require_git_oid(
                row["target_commit"], field="target_commit"
            ),
            "source_tree_hash": _require_hash(
                row["source_tree_hash"], field="source_tree_hash"
            ),
        }
    if phase == PHASE_VALIDATION_COMPLETED:
        _require_exact_keys(
            row,
            keys=_VALIDATION_DATA_KEYS,
            field=f"{phase} data",
        )
        try:
            validation = ValidationRunResult.from_dict(row["validation"])
        except ValueError as exc:
            raise LandingCorrectionError(
                f"validation evidence is invalid: {exc}"
            ) from exc
        all_passed = row["all_passed"]
        if all_passed is not True or not validation.all_passed:
            raise LandingCorrectionError(
                "validation_completed requires all_passed=true"
            )
        validation_count = _require_strict_int(
            row["validation_count"],
            field="validation_count",
            minimum=1,
        )
        if validation_count != validation.completed_count:
            raise LandingCorrectionError(
                "validation_count does not match validation result rows"
            )
        validation_evidence_hash = _require_hash(
            row["validation_evidence_hash"],
            field="validation_evidence_hash",
        )
        expected_validation_hash = hashlib.sha256(
            _canonical_json_bytes(validation.to_dict())
        ).hexdigest()
        if validation_evidence_hash != expected_validation_hash:
            raise LandingCorrectionError(
                "validation_evidence_hash does not match validation result rows"
            )
        return {
            "source_head_commit": _require_git_oid(
                row["source_head_commit"], field="source_head_commit"
            ),
            "target_commit": _require_git_oid(
                row["target_commit"], field="target_commit"
            ),
            "source_tree_hash": _require_hash(
                row["source_tree_hash"], field="source_tree_hash"
            ),
            "validation_policy_hash": _require_hash(
                row["validation_policy_hash"], field="validation_policy_hash"
            ),
            "validation_evidence_hash": validation_evidence_hash,
            "validation_count": validation_count,
            "all_passed": True,
            "validation": validation.to_dict(),
        }
    if phase == PHASE_BLOCKED:
        _require_exact_keys(row, keys=_BLOCKED_DATA_KEYS, field=f"{phase} data")
        reason_code = _require_text(row["reason_code"], field="reason_code")
        if _REASON_CODE_RE.fullmatch(reason_code) is None:
            raise LandingCorrectionError(
                "reason_code must be a lowercase stable identifier"
            )
        blocked_after = _require_text(row["blocked_after"], field="blocked_after")
        if blocked_after not in {
            PHASE_INTENT_RECORDED,
            PHASE_REBASE_COMPLETED,
            PHASE_VALIDATION_COMPLETED,
        }:
            raise LandingCorrectionError(
                f"blocked_after has invalid phase {blocked_after!r}"
            )
        validation_payload = row["validation"]
        validation = None
        if validation_payload is not None:
            try:
                validation = ValidationRunResult.from_dict(validation_payload)
            except ValueError as exc:
                raise LandingCorrectionError(
                    f"blocked validation evidence is invalid: {exc}"
                ) from exc
            if (
                validation.all_passed
                and reason_code != "post_rebase_validation_failed"
            ):
                raise LandingCorrectionError(
                    "blocked validation evidence cannot report all_passed=true"
                )
        if reason_code == "post_rebase_validation_failed" and validation is None:
            raise LandingCorrectionError(
                "post-rebase validation blocker requires validation evidence"
            )
        if reason_code != "post_rebase_validation_failed" and validation is not None:
            raise LandingCorrectionError(
                "non-validation blocker cannot carry validation evidence"
            )
        return {
            "reason_code": reason_code,
            "blocked_after": blocked_after,
            "validation": (
                validation.to_dict() if validation is not None else None
            ),
        }
    if phase == PHASE_HANDED_TO_LANDING:
        _require_exact_keys(row, keys=_HANDED_DATA_KEYS, field=f"{phase} data")
        return {
            "landing_transaction_id": _require_hash(
                row["landing_transaction_id"], field="landing_transaction_id"
            ),
            "landing_intent_event_id": _require_hash(
                row["landing_intent_event_id"], field="landing_intent_event_id"
            ),
        }
    raise LandingCorrectionError(f"unsupported landing correction phase {phase!r}")


def _validate_phase_order(
    phases: Sequence[str],
    phase_data: Mapping[str, Mapping[str, Any]],
) -> None:
    phase_tuple = tuple(phases)
    valid_active = {
        (PHASE_INTENT_RECORDED,),
        (PHASE_INTENT_RECORDED, PHASE_REBASE_COMPLETED),
        (PHASE_INTENT_RECORDED, PHASE_VALIDATION_COMPLETED),
        (
            PHASE_INTENT_RECORDED,
            PHASE_REBASE_COMPLETED,
            PHASE_VALIDATION_COMPLETED,
        ),
    }
    valid_handed = {
        (
            PHASE_INTENT_RECORDED,
            PHASE_VALIDATION_COMPLETED,
            PHASE_HANDED_TO_LANDING,
        ),
        (
            PHASE_INTENT_RECORDED,
            PHASE_REBASE_COMPLETED,
            PHASE_VALIDATION_COMPLETED,
            PHASE_HANDED_TO_LANDING,
        ),
    }
    valid_blocked_prefixes = valid_active
    if phase_tuple in valid_active or phase_tuple in valid_handed:
        return
    if (
        phase_tuple
        and phase_tuple[-1] == PHASE_BLOCKED
        and phase_tuple[:-1] in valid_blocked_prefixes
    ):
        blocked_after = phase_data[PHASE_BLOCKED]["blocked_after"]
        if blocked_after != phase_tuple[-2]:
            raise LandingCorrectionError(
                "blocked evidence does not name the immediately preceding phase"
            )
        return
    raise LandingCorrectionError(
        f"landing correction phases are not a valid ordered prefix: {phase_tuple!r}"
    )


def _validate_phase_coherence(
    *,
    intent: LandingCorrectionIntent,
    phases: Sequence[str],
    phase_data: Mapping[str, Mapping[str, Any]],
) -> None:
    expected_source = intent.source_head_commit
    expected_target = intent.target_commit
    expected_tree = intent.source_tree_hash
    if PHASE_REBASE_COMPLETED in phases:
        rebase = phase_data[PHASE_REBASE_COMPLETED]
        expected_source = str(rebase["source_head_commit"])
        expected_target = str(rebase["target_commit"])
        expected_tree = str(rebase["source_tree_hash"])
    if PHASE_VALIDATION_COMPLETED in phases:
        validation = phase_data[PHASE_VALIDATION_COMPLETED]
        observed = (
            validation["source_head_commit"],
            validation["target_commit"],
            validation["source_tree_hash"],
        )
        expected = (expected_source, expected_target, expected_tree)
        if observed != expected:
            raise LandingCorrectionError(
                "validation evidence does not describe the exact corrected tree"
            )
        if validation["validation_policy_hash"] != intent.validation_policy_hash:
            raise LandingCorrectionError(
                "validation evidence does not match the intent policy"
            )


def _matching_attempt_payload(
    event: Mapping[str, Any],
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
) -> bool:
    if event.get("type") != LANDING_CORRECTION_EVENT_TYPE:
        return False
    payload = event.get("payload")
    return (
        isinstance(payload, Mapping)
        and payload.get("workset_id") == workset_id
        and payload.get("task_id") == task_id
        and payload.get("attempt_id") == attempt_id
    )


def _selection_from_events(
    events: Sequence[Mapping[str, Any]],
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
) -> LandingCorrectionSelection:
    rows_by_correction: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for position, event in enumerate(events):
        if not _matching_attempt_payload(
            event,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
        ):
            continue
        payload = _require_mapping(
            event.get("payload"), field="landing correction event payload"
        )
        _require_exact_keys(
            payload,
            keys=_EVENT_PAYLOAD_KEYS,
            field="landing correction event payload",
        )
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != LANDING_CORRECTION_SCHEMA_VERSION
        ):
            raise LandingCorrectionError(
                "landing correction event has unsupported schema_version"
            )
        correction_id = _require_hash(
            payload["correction_id"], field="correction_id"
        )
        phase = _require_text(payload["phase"], field="phase")
        if phase not in LANDING_CORRECTION_PHASES:
            raise LandingCorrectionError(
                f"landing correction event has unsupported phase {phase!r}"
            )
        rows_by_correction.setdefault(correction_id, []).append((position, event))

    corrections: list[LandingCorrection] = []
    for correction_id, rows in rows_by_correction.items():
        rows.sort(key=lambda row: row[0])
        phases: list[str] = []
        phase_data: dict[str, Mapping[str, Any]] = {}
        positions: list[int] = []
        intent: LandingCorrectionIntent | None = None
        for position, event in rows:
            payload = _require_mapping(
                event.get("payload"), field="landing correction event payload"
            )
            phase = str(payload["phase"])
            if phase in phase_data:
                raise LandingCorrectionError(
                    f"duplicate landing correction phase {phase!r} "
                    f"for {correction_id}"
                )
            canonical_data = _canonical_phase_data(phase, payload["data"])
            if not _strict_json_equal(canonical_data, payload["data"]):
                raise LandingCorrectionError(
                    f"landing correction phase {phase!r} is not canonical"
                )
            if phase == PHASE_INTENT_RECORDED:
                intent = LandingCorrectionIntent.from_dict(canonical_data)
                if intent.correction_id != correction_id:
                    raise LandingCorrectionError(
                        "landing correction intent does not match correction_id"
                    )
            phases.append(phase)
            phase_data[phase] = canonical_data
            positions.append(position)

        if intent is None:
            raise LandingCorrectionError(
                f"landing correction {correction_id} has no intent_recorded phase"
            )
        for _, event in rows:
            payload = _require_mapping(
                event.get("payload"), field="landing correction event payload"
            )
            phase = str(payload["phase"])
            if payload["correction_id"] != correction_id:
                raise LandingCorrectionError("landing correction id fork")
            if (
                payload["workset_id"] != intent.workset_id
                or payload["task_id"] != intent.task_id
                or payload["attempt_id"] != intent.attempt_id
            ):
                raise LandingCorrectionError("landing correction identity fork")
            if event.get("actor") != intent.actor:
                raise LandingCorrectionError("landing correction actor fork")
            expected_event_id = landing_correction_phase_event_id(
                correction_id, phase
            )
            if event.get("event_id") != expected_event_id:
                raise LandingCorrectionError(
                    f"landing correction phase {phase!r} has invalid event_id"
                )

        _validate_phase_order(phases, phase_data)
        _validate_phase_coherence(
            intent=intent,
            phases=phases,
            phase_data=phase_data,
        )
        corrections.append(
            LandingCorrection(
                correction_id=correction_id,
                intent=intent,
                phases=tuple(phases),
                phase_data=dict(phase_data),
                event_positions=tuple(positions),
            )
        )

    corrections.sort(key=lambda correction: correction.event_positions[0])
    generations = tuple(correction.intent.generation for correction in corrections)
    if generations != tuple(range(1, len(corrections) + 1)):
        raise LandingCorrectionError(
            "landing correction generations must be contiguous and append ordered"
        )
    active = [correction for correction in corrections if correction.active]
    if len(active) > 1:
        raise LandingCorrectionError(
            "attempt has multiple active landing correction generations"
        )
    active_correction = active[0] if active else None
    if (
        active_correction is not None
        and corrections
        and active_correction is not corrections[-1]
    ):
        raise LandingCorrectionError(
            "active landing correction generation is not the latest generation"
        )
    terminals = [correction for correction in corrections if correction.terminal]
    latest_terminal = (
        max(terminals, key=lambda correction: correction.event_positions[-1])
        if terminals
        else None
    )
    return LandingCorrectionSelection(
        corrections=tuple(corrections),
        active=active_correction,
        latest_terminal=latest_terminal,
    )


def load_landing_correction_selection(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
) -> LandingCorrectionSelection:
    events_file = _events_file(profile)
    with exclusive_file_lock(events_file):
        events = load_events(events_file)
        return _selection_from_events(
            events,
            workset_id=workset_id,
            task_id=task_id,
            attempt_id=attempt_id,
        )


def load_landing_correction(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
    correction_id: str,
) -> LandingCorrection | None:
    correction_id = _require_hash(correction_id, field="correction_id")
    selection = load_landing_correction_selection(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    return next(
        (
            correction
            for correction in selection.corrections
            if correction.correction_id == correction_id
        ),
        None,
    )


def _event_payload(
    *,
    intent: LandingCorrectionIntent,
    phase: str,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": LANDING_CORRECTION_SCHEMA_VERSION,
        "correction_id": intent.correction_id,
        "workset_id": intent.workset_id,
        "task_id": intent.task_id,
        "attempt_id": intent.attempt_id,
        "phase": phase,
        "data": dict(data),
    }


def record_landing_correction_phase(
    profile: RepoProfile,
    *,
    intent: LandingCorrectionIntent,
    phase: str,
    data: Mapping[str, Any],
) -> bool:
    if phase not in LANDING_CORRECTION_PHASES:
        raise LandingCorrectionError(f"unsupported landing correction phase {phase!r}")
    canonical_data = _canonical_phase_data(phase, data)
    if phase == PHASE_INTENT_RECORDED and not _strict_json_equal(
        canonical_data, intent.to_dict()
    ):
        raise LandingCorrectionError(
            "intent_recorded data does not match the supplied intent"
        )

    events_file = _events_file(profile)
    with exclusive_file_lock(events_file):
        events = load_events(events_file)
        selection = _selection_from_events(
            events,
            workset_id=intent.workset_id,
            task_id=intent.task_id,
            attempt_id=intent.attempt_id,
        )
        existing = next(
            (
                correction
                for correction in selection.corrections
                if correction.correction_id == intent.correction_id
            ),
            None,
        )
        if existing is None:
            if phase != PHASE_INTENT_RECORDED:
                raise LandingCorrectionError(
                    "landing correction phase cannot precede intent_recorded"
                )
            if selection.active is not None:
                raise LandingCorrectionError(
                    "attempt already has an active landing correction generation"
                )
            prospective_phases = (PHASE_INTENT_RECORDED,)
            prospective_data = {PHASE_INTENT_RECORDED: canonical_data}
        else:
            if existing.intent != intent:
                raise LandingCorrectionError("landing correction intent fork")
            if phase in existing.phase_data:
                if not _strict_json_equal(existing.phase_data[phase], canonical_data):
                    raise LandingCorrectionError(
                        f"landing correction phase {phase!r} conflicts with "
                        "recorded evidence"
                    )
                prospective_phases = existing.phases
                prospective_data = existing.phase_data
            else:
                if existing.terminal:
                    raise LandingCorrectionError(
                        "landing correction is already terminal"
                    )
                prospective_phases = (*existing.phases, phase)
                prospective_data = {**existing.phase_data, phase: canonical_data}

        _validate_phase_order(prospective_phases, prospective_data)
        _validate_phase_coherence(
            intent=intent,
            phases=prospective_phases,
            phase_data=prospective_data,
        )
        return append_event_once(
            events_file,
            event_id=landing_correction_phase_event_id(
                intent.correction_id, phase
            ),
            event_type=LANDING_CORRECTION_EVENT_TYPE,
            actor=intent.actor,
            payload=_event_payload(
                intent=intent,
                phase=phase,
                data=canonical_data,
            ),
        )


def record_landing_correction_intent(
    profile: RepoProfile,
    *,
    intent: LandingCorrectionIntent,
) -> bool:
    return record_landing_correction_phase(
        profile,
        intent=intent,
        phase=PHASE_INTENT_RECORDED,
        data=intent.to_dict(),
    )


def record_landing_correction_rebase_completed(
    profile: RepoProfile,
    *,
    intent: LandingCorrectionIntent,
    source_head_commit: str,
    target_commit: str,
    source_tree_hash: str,
) -> bool:
    return record_landing_correction_phase(
        profile,
        intent=intent,
        phase=PHASE_REBASE_COMPLETED,
        data={
            "source_head_commit": source_head_commit,
            "target_commit": target_commit,
            "source_tree_hash": source_tree_hash,
        },
    )


def record_landing_correction_validation_completed(
    profile: RepoProfile,
    *,
    intent: LandingCorrectionIntent,
    source_head_commit: str,
    target_commit: str,
    source_tree_hash: str,
    validation_evidence_hash: str,
    validation: ValidationRunResult,
) -> bool:
    return record_landing_correction_phase(
        profile,
        intent=intent,
        phase=PHASE_VALIDATION_COMPLETED,
        data={
            "source_head_commit": source_head_commit,
            "target_commit": target_commit,
            "source_tree_hash": source_tree_hash,
            "validation_policy_hash": intent.validation_policy_hash,
            "validation_evidence_hash": validation_evidence_hash,
            "validation_count": validation.completed_count,
            "all_passed": True,
            "validation": validation.to_dict(),
        },
    )


def record_landing_correction_blocked(
    profile: RepoProfile,
    *,
    intent: LandingCorrectionIntent,
    reason_code: str,
    validation: ValidationRunResult | None = None,
) -> bool:
    selection = load_landing_correction_selection(
        profile,
        workset_id=intent.workset_id,
        task_id=intent.task_id,
        attempt_id=intent.attempt_id,
    )
    active = selection.active
    existing = next(
        (
            correction
            for correction in selection.corrections
            if correction.correction_id == intent.correction_id
        ),
        None,
    )
    if existing is not None and existing.blocked:
        blocked_after = str(
            existing.phase_data[PHASE_BLOCKED]["blocked_after"]
        )
    elif active is not None and active.correction_id == intent.correction_id:
        blocked_after = active.last_phase
    else:
        raise LandingCorrectionError(
            "only the active landing correction can be blocked"
        )
    return record_landing_correction_phase(
        profile,
        intent=intent,
        phase=PHASE_BLOCKED,
        data={
            "reason_code": reason_code,
            "blocked_after": blocked_after,
            "validation": validation.to_dict() if validation is not None else None,
        },
    )


def record_landing_correction_handed_to_landing(
    profile: RepoProfile,
    *,
    intent: LandingCorrectionIntent,
    landing_transaction_id: str,
    landing_intent_event_id: str,
) -> bool:
    return record_landing_correction_phase(
        profile,
        intent=intent,
        phase=PHASE_HANDED_TO_LANDING,
        data={
            "landing_transaction_id": landing_transaction_id,
            "landing_intent_event_id": landing_intent_event_id,
        },
    )
