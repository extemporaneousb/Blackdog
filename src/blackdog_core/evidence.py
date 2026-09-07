"""Typed task evidence in the canonical append-only event ledger.

These observations describe outcomes. They never change lifecycle authority.
Readers reject unknown versions, broken references and competing corrections.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Mapping

from .state import StoreError, is_canonical_attempt_id, is_canonical_task_id
from .validation import ValidationRunResult

SCHEMA_VERSION = 1
MAX_DOCUMENT_BYTES = 65536
EVENT_PREFIX = "task.evidence."
DEFINITION = EVENT_PREFIX + "definition"
ASSESSMENT = EVENT_PREFIX + "assessment"
INTERVENTION = EVENT_PREFIX + "intervention"
VALIDATION_INTENT = EVENT_PREFIX + "validation-intent"
VALIDATION_RESULT = EVENT_PREFIX + "validation-result"
PHASE = EVENT_PREFIX + "phase"
EVENT_TYPES = frozenset(
    {DEFINITION, ASSESSMENT, INTERVENTION, VALIDATION_INTENT, VALIDATION_RESULT, PHASE}
)
_TOKEN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_GIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class EvidenceError(StoreError):
    """Evidence is malformed, conflicting, unowned or not applicable."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvidenceError("evidence must be canonical JSON") from exc


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def fields(value: Any, names: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(names.split()):
        raise EvidenceError(f"evidence requires exactly these fields: {names}")
    if len(canonical_json(value).encode()) > MAX_DOCUMENT_BYTES:
        raise EvidenceError("evidence document exceeds size limit")
    return value


def text(value: Any, name: str, *, limit: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > limit
    ):
        raise EvidenceError(
            f"{name} must be nonblank bounded text without surrounding whitespace"
        )
    if any(ord(c) < 32 for c in value):
        raise EvidenceError(f"{name} cannot contain control characters")
    return value


def token(value: Any, name: str) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise EvidenceError(f"{name} must be a bounded identifier")
    return value


def sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise EvidenceError(f"{name} must be a lowercase SHA-256")
    return value


def git_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or _GIT.fullmatch(value) is None:
        raise EvidenceError(f"{name} must be a Git object identity")
    return value


def choice(value: Any, name: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise EvidenceError(f"{name} must be one of {', '.join(sorted(allowed))}")
    return value


def integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise EvidenceError(f"{name} must be a nonnegative integer")
    return value


def version(value: Any) -> None:
    if type(value) is not int or value != SCHEMA_VERSION:
        raise EvidenceError("unsupported task evidence schema version")


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    description: str
    required: bool

    @classmethod
    def parse(cls, value: Any) -> Criterion:
        v = fields(value, "id description required")
        if type(v["required"]) is not bool:
            raise EvidenceError("criterion required must be boolean")
        return cls(
            id=token(v["id"], "criterion id"),
            description=text(v["description"], "description"),
            required=v["required"],
        )


@dataclass(frozen=True, slots=True)
class OutcomeDefinition:
    schema_version: int
    task_class: str
    objective: str
    criteria: tuple[Criterion, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["criteria"] = [asdict(c) for c in self.criteria]
        return result

    @property
    def sha256(self) -> str:
        return digest(self.to_dict())

    @classmethod
    def parse(cls, value: Any) -> OutcomeDefinition:
        v = fields(value, "schema_version task_class objective criteria")
        version(v["schema_version"])
        rows = v["criteria"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
            raise EvidenceError("definition requires between one and 64 criteria")
        criteria = tuple(Criterion.parse(row) for row in rows)
        if len({c.id for c in criteria}) != len(criteria) or not any(
            c.required for c in criteria
        ):
            raise EvidenceError(
                "criterion ids must be unique with at least one required criterion"
            )
        return cls(
            schema_version=1,
            task_class=token(v["task_class"], "task_class"),
            objective=text(v["objective"], "objective"),
            criteria=criteria,
        )


@dataclass(frozen=True, slots=True)
class Assessment:
    schema_version: int
    assessment_id: str
    definition_sha256: str
    criterion_id: str
    result: str
    evaluator: str
    evaluator_kind: str
    provenance: str
    evidence_refs: tuple[str, ...]
    supersedes: str | None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence_refs"] = list(self.evidence_refs)
        return result

    @classmethod
    def parse(cls, value: Any) -> Assessment:
        v = fields(
            value,
            "schema_version assessment_id definition_sha256 criterion_id result "
            "evaluator evaluator_kind provenance evidence_refs supersedes",
        )
        version(v["schema_version"])
        refs = v["evidence_refs"]
        if (
            not isinstance(refs, list)
            or len(refs) > 64
            or len(set(map(str, refs))) != len(refs)
        ):
            raise EvidenceError(
                "evidence_refs must be a bounded list of unique event identities"
            )
        return cls(
            schema_version=1,
            assessment_id=token(v["assessment_id"], "assessment_id"),
            definition_sha256=sha(v["definition_sha256"], "definition_sha256"),
            criterion_id=token(v["criterion_id"], "criterion_id"),
            result=choice(v["result"], "result", {"met", "not_met", "not_assessed"}),
            evaluator=text(v["evaluator"], "evaluator", limit=128),
            evaluator_kind=choice(
                v["evaluator_kind"], "evaluator_kind", {"agent", "human", "external"}
            ),
            provenance=choice(
                v["provenance"], "provenance", {"caller_declared", "reviewer_asserted"}
            ),
            evidence_refs=tuple(sha(r, "evidence reference") for r in refs),
            supersedes=(
                sha(v["supersedes"], "supersedes")
                if v["supersedes"] is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class Intervention:
    schema_version: int
    measurement_id: str
    metric: str
    unit: str
    value: int
    provenance: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def parse(cls, value: Any) -> Intervention:
        v = fields(
            value, "schema_version measurement_id metric unit value provenance reason"
        )
        version(v["schema_version"])
        return cls(
            schema_version=1,
            measurement_id=token(v["measurement_id"], "measurement_id"),
            metric=choice(v["metric"], "metric", {"human_interventions"}),
            unit=choice(v["unit"], "unit", {"count"}),
            value=integer(v["value"], "value"),
            provenance=choice(v["provenance"], "provenance", {"caller_declared"}),
            reason=text(v["reason"], "reason"),
        )


@dataclass(frozen=True, slots=True)
class SetupMeasurement:
    schema_version: int
    phase: str
    unit: str
    source: str
    value: int | None
    missing_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def parse(cls, value: Any) -> SetupMeasurement:
        row = fields(value, "schema_version phase unit source value missing_reason")
        version(row["schema_version"])
        amount = integer(row["value"], "value") if row["value"] is not None else None
        expected_reason = "not_recorded" if amount is None else None
        if row["missing_reason"] != expected_reason:
            raise EvidenceError("setup measurement missingness conflicts with value")
        return cls(
            schema_version=1,
            phase=choice(row["phase"], "phase", {"setup"}),
            unit=choice(row["unit"], "unit", {"ms"}),
            source=choice(row["source"], "source", {"monotonic_clock"}),
            value=amount,
            missing_reason=expected_reason,
        )


@dataclass(frozen=True, slots=True)
class PhaseMeasurement:
    measurement_id: str
    phase: str
    unit: str
    value: int
    source: str
    attribution: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def parse(cls, value: Any) -> PhaseMeasurement:
        v = fields(value, "measurement_id phase unit value source attribution")
        return cls(
            measurement_id=token(v["measurement_id"], "measurement_id"),
            phase=choice(
                v["phase"], "phase", {"landing", "recovery", "cleanup", "close"}
            ),
            unit=choice(v["unit"], "unit", {"ms"}),
            value=integer(v["value"], "value"),
            source=choice(v["source"], "source", {"monotonic_clock"}),
            attribution=choice(
                v["attribution"],
                "attribution",
                {"first_completed_mutating_invocation_for_phase"},
            ),
        )


@dataclass(frozen=True, slots=True)
class ValidationBinding:
    source_commit: str
    source_tree: str
    command_set_sha256: str
    command_sha256: tuple[str, ...]
    timeout_seconds: int
    environment: dict[str, Any]
    environment_sha256: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["command_sha256"] = list(self.command_sha256)
        return result

    @classmethod
    def parse(cls, value: Any) -> ValidationBinding:
        v = fields(
            value,
            "source_commit source_tree command_set_sha256 command_sha256 timeout_seconds environment environment_sha256",
        )
        commands = v["command_sha256"]
        if not isinstance(commands, list) or not 1 <= len(commands) <= 128:
            raise EvidenceError("validation binding requires one to 128 command hashes")
        commands = tuple(sha(c, "command_sha256") for c in commands)
        timeout = integer(v["timeout_seconds"], "timeout_seconds")
        if timeout == 0 or v["command_set_sha256"] != digest(
            {"commands": list(commands), "timeout_seconds": timeout}
        ):
            raise EvidenceError("validation command set digest or timeout is invalid")
        env = fields(
            v["environment"],
            "schema_version python_implementation python_version platform_system platform_machine handlers_sha256 runtime_identity coverage",
        )
        version(env["schema_version"])
        for name in (
            "python_implementation",
            "python_version",
            "platform_system",
            "platform_machine",
        ):
            text(env[name], name, limit=128)
        sha(env["handlers_sha256"], "handlers_sha256")
        identity = fields(env["runtime_identity"], "kind value missing_reason")
        kind = choice(
            identity["kind"],
            "runtime identity kind",
            {"source_sha256", "release_sha256", "unknown"},
        )
        if kind == "unknown":
            if (
                identity["value"] is not None
                or identity["missing_reason"] != "runtime_source_unavailable"
            ):
                raise EvidenceError(
                    "unknown runtime identity requires explicit missingness"
                )
        elif identity["missing_reason"] is not None:
            raise EvidenceError(
                "observed runtime identity cannot have a missing reason"
            )
        else:
            sha(identity["value"], "runtime identity value")
        choice(env["coverage"], "environment coverage", {"bounded_runtime_only"})
        if sha(v["environment_sha256"], "environment_sha256") != digest(env):
            raise EvidenceError("environment digest does not match descriptor")
        return cls(
            source_commit=git_id(v["source_commit"], "source_commit"),
            source_tree=git_id(v["source_tree"], "source_tree"),
            command_set_sha256=sha(v["command_set_sha256"], "command_set_sha256"),
            command_sha256=commands,
            timeout_seconds=timeout,
            environment=dict(env),
            environment_sha256=v["environment_sha256"],
        )


def matching_validation_inputs(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> bool:
    """Commit is lineage; identical trees retain the same observed applicability."""
    return all(
        left[key] == right[key]
        for key in ("source_tree", "command_set_sha256", "environment_sha256")
    )


def event_identity(kind: str, task_id: str, attempt_id: str, request_id: str) -> str:
    return digest(["blackdog.task-evidence/v1", kind, task_id, attempt_id, request_id])


@dataclass(frozen=True, slots=True)
class EvidenceEvent:
    event_id: str
    kind: str
    actor: str
    task_id: str
    attempt_id: str
    data: dict[str, Any]
    at: str


def parse_event(row: Mapping[str, Any]) -> EvidenceEvent:
    kind = row["type"]
    if kind not in EVENT_TYPES:
        raise EvidenceError("unsupported task evidence event type")
    p = fields(row["payload"], "schema_version task_id attempt_id data")
    version(p["schema_version"])
    if not is_canonical_task_id(p["task_id"]) or not is_canonical_attempt_id(
        p["attempt_id"]
    ):
        raise EvidenceError("evidence must bind canonical task and attempt identities")
    data = p["data"]
    if kind == DEFINITION:
        definition = OutcomeDefinition.parse(data)
        request_id = definition.sha256
    elif kind == ASSESSMENT:
        data = dict(fields(data, "assessment source_tree"))
        assessment = Assessment.parse(data["assessment"])
        git_id(data["source_tree"], "source_tree")
        request_id = assessment.assessment_id
    elif kind == INTERVENTION:
        request_id = Intervention.parse(data).measurement_id
    elif kind == PHASE:
        request_id = PhaseMeasurement.parse(data).measurement_id
    elif kind == VALIDATION_INTENT:
        data = dict(fields(data, "run_id binding"))
        request_id = token(data["run_id"], "run_id")
        ValidationBinding.parse(data["binding"])
    else:
        data = dict(fields(data, "run_id intent_event_id binding_after run"))
        request_id = token(data["run_id"], "run_id")
        sha(data["intent_event_id"], "intent_event_id")
        if data["binding_after"] is not None:
            ValidationBinding.parse(data["binding_after"])
        try:
            ValidationRunResult.from_dict(data["run"])
        except ValueError as exc:
            raise EvidenceError(str(exc)) from exc
    expected = event_identity(kind, p["task_id"], p["attempt_id"], request_id)
    if row["event_id"] != expected:
        raise EvidenceError(
            "task evidence event identity does not match immutable request"
        )
    if (
        kind in {VALIDATION_INTENT, VALIDATION_RESULT, PHASE}
        and row["actor"] != "blackdog"
    ):
        raise EvidenceError("machine validation evidence must be product-recorded")
    return EvidenceEvent(
        event_id=expected,
        kind=kind,
        actor=row["actor"],
        task_id=p["task_id"],
        attempt_id=p["attempt_id"],
        data=dict(data),
        at=row["at"],
    )


def read_evidence(
    events: tuple[dict[str, Any], ...], attempts: Mapping[str, str]
) -> tuple[EvidenceEvent, ...]:
    """Validate the entire stream before exposing any successful assessment."""
    result: list[EvidenceEvent] = []
    definitions: dict[str, OutcomeDefinition] = {}
    heads: dict[tuple[str, str], EvidenceEvent] = {}
    seen: dict[str, EvidenceEvent] = {}
    requests: set[tuple[str, str, str]] = set()
    for raw in events:
        if not str(raw.get("type", "")).startswith(EVENT_PREFIX):
            continue
        event = parse_event(raw)
        if attempts.get(event.attempt_id) != event.task_id:
            raise EvidenceError(
                "evidence references an unknown or foreign task attempt"
            )
        d = event.data
        if event.event_id in seen:
            raise EvidenceError("duplicate task evidence event identity")
        if event.kind == DEFINITION:
            if event.task_id in definitions:
                raise EvidenceError("task outcome definition is immutable")
            definitions[event.task_id] = OutcomeDefinition.parse(d)
        elif event.kind == ASSESSMENT:
            a = Assessment.parse(d["assessment"])
            definition = definitions.get(event.task_id)
            if (
                definition is None
                or a.definition_sha256 != definition.sha256
                or a.criterion_id not in {c.id for c in definition.criteria}
            ):
                raise EvidenceError(
                    "assessment does not reference its task definition and criterion"
                )
            key = (event.task_id, a.criterion_id)
            previous = heads.get(key)
            if a.supersedes != (previous.event_id if previous else None):
                raise EvidenceError(
                    "assessment supersedes must match the current criterion head"
                )
            for reference in a.evidence_refs:
                receipt = seen.get(reference)
                if (
                    receipt is None
                    or receipt.kind != VALIDATION_RESULT
                    or (receipt.task_id, receipt.attempt_id)
                    != (event.task_id, event.attempt_id)
                ):
                    raise EvidenceError(
                        "assessment references missing or foreign validation evidence"
                    )
                intent = seen[receipt.data["intent_event_id"]]
                if (
                    intent.data["binding"]["environment"]["runtime_identity"]["kind"]
                    == "unknown"
                ):
                    raise EvidenceError(
                        "assessment references validation with unobserved runtime identity"
                    )
                after = receipt.data["binding_after"]
                if (
                    after is None
                    or not matching_validation_inputs(after, intent.data["binding"])
                    or after["source_tree"] != d["source_tree"]
                ):
                    raise EvidenceError(
                        "assessment references validation of different or changed content"
                    )
                if (
                    a.result == "met"
                    and not ValidationRunResult.from_dict(
                        receipt.data["run"]
                    ).all_passed
                ):
                    raise EvidenceError("met assessment cannot cite failed validation")
            heads[key] = event
        elif event.kind == VALIDATION_RESULT:
            intent = seen.get(d["intent_event_id"])
            if (
                intent is None
                or intent.kind != VALIDATION_INTENT
                or (intent.task_id, intent.attempt_id, intent.data["run_id"])
                != (event.task_id, event.attempt_id, d["run_id"])
            ):
                raise EvidenceError(
                    "validation receipt references a missing or foreign invocation"
                )
            run = ValidationRunResult.from_dict(d["run"])
            command_hashes = intent.data["binding"]["command_sha256"]
            if run.command_count != len(command_hashes) or any(
                row.command_sha256 != command_hashes[row.index] for row in run.results
            ):
                raise EvidenceError(
                    "validation result does not match the intended command set"
                )
        if event.kind in {
            ASSESSMENT,
            INTERVENTION,
            VALIDATION_INTENT,
            VALIDATION_RESULT,
        }:
            request_id = (
                d["assessment"]["assessment_id"]
                if event.kind == ASSESSMENT
                else d.get("measurement_id", d.get("run_id"))
            )
            key = (event.task_id, event.kind, request_id)
            if key in requests:
                raise EvidenceError("evidence request identity reused across attempts")
            requests.add(key)
        seen[event.event_id] = event
        result.append(event)
    return tuple(result)
