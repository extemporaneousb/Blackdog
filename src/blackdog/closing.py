"""Durable product-layer close transactions for WTAM attempts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from typing import Any

from blackdog.landing import strict_json_equal
from blackdog_core.backlog import (
    task_finalization_owned_event_id,
    task_finalization_request_event_id,
)
from blackdog_core.profile import RepoProfile
from blackdog_core.state import (
    FAILURE_CLASSES,
    FAILURE_CLASS_ABANDONED,
    VALIDATION_STATUSES,
    append_event_once,
    exclusive_file_lock,
    load_events,
)


CLOSE_REQUEST_EVENT_TYPE = "worktree.close.request"
CLOSE_EVENT_TYPE = "worktree.close"
CLOSE_SCHEMA_VERSION = 1
CLOSE_STATUSES = frozenset({"blocked", "failed", "abandoned"})
_CLEANUP_DISPOSITION_PROOFS = {
    "retain_not_requested": frozenset({"not_requested"}),
    "retain_unproven": frozenset({"source_identity_unproven"}),
    "retain_dirty": frozenset({"dirty"}),
    "retain_unlanded": frozenset({"unproven"}),
    "remove": frozenset({"no_ahead", "contained", "patch_equivalent"}),
    "already_absent": frozenset({"exact_source_absent"}),
}

_PROJECTION_KEYS = frozenset(
    {
        "recorded_branch",
        "recorded_target_branch",
        "recorded_worktree_path",
        "resolved_source_path",
        "source_path_exists",
        "source_is_worktree",
        "source_registration",
        "source_head_commit",
        "branch_state",
        "branch_commit",
        "changed_paths",
        "worktree_dirty",
        "cleanup_eligible",
        "cleanup_disposition",
        "cleanup_proof",
        "cleanup_reason",
    }
)
_REGISTRATION_KEYS = frozenset(
    {"registered", "path", "branch", "head", "detached"}
)
_REQUEST_KEYS = frozenset(
    {
        "schema_version",
        "close_request_id",
        "finalization_id",
        "close_event_id",
        "cleanup_event_id",
        "workset_id",
        "task_id",
        "attempt_id",
        "actor",
        "status",
        "summary",
        "validations",
        "residuals",
        "followup_candidates",
        "note",
        "failure_class",
        "recovery_action",
        "prompt_issue",
        "operator_issue",
        "cleanup_requested",
        "pre_close_projection",
    }
)
_CORE_EVIDENCE_KEYS = frozenset(
    {
        "request_event_id",
        "decision_event_id",
        "task_release_event_id",
        "workset_release_event_id",
        "task_finish_event_id",
        "runtime_finalized",
    }
)
_CLEANUP_EVIDENCE_KEYS = frozenset(
    {
        "requested",
        "eligible",
        "event_id",
        "performed",
        "worktree_removed",
        "branch_deleted",
        "retained",
        "reason",
        "proof",
    }
)
_CLOSE_EVENT_KEYS = frozenset(
    {
        "schema_version",
        "close_request_id",
        "finalization_id",
        "close_event_id",
        "workset_id",
        "task_id",
        "attempt_id",
        "actor",
        "status",
        "summary",
        "branch",
        "target_branch",
        "worktree_path",
        "changed_paths",
        "commit",
        "cleanup_requested",
        "cleanup_performed",
        "cleanup_reason",
        "failure_class",
        "recovery_action",
        "prompt_issue",
        "operator_issue",
        "core_finalization",
        "cleanup",
    }
)


class CloseTransactionError(RuntimeError):
    """The append-only close ledger is corrupt or conflicts with its request."""


def _digest(namespace: str, *parts: str) -> str:
    return hashlib.sha256("\0".join((namespace, *parts)).encode("utf-8")).hexdigest()


def close_request_event_id(*, workset_id: str, task_id: str, attempt_id: str) -> str:
    return _digest(
        "blackdog.worktree.close.request/v1",
        workset_id,
        task_id,
        attempt_id,
    )


def close_finalization_id(request_event_id: str) -> str:
    return _digest("blackdog.worktree.close.finalization/v1", request_event_id)


def worktree_close_event_id(request_event_id: str) -> str:
    return _digest("blackdog.worktree.close.event/v1", request_event_id)


def worktree_cleanup_event_id(
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str | None,
    branch: str | None,
    worktree_path: str,
) -> str:
    identity_material = "\0".join(
        (workset_id, task_id, attempt_id or "", branch or "", worktree_path)
    )
    return f"worktree-cleanup-{hashlib.sha256(identity_material.encode('utf-8')).hexdigest()}"


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CloseTransactionError(f"close transaction {field} must be canonical nonempty text")
    return value


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value.strip() != value:
        raise CloseTransactionError(
            f"close transaction {field} must be null or canonical nonempty text"
        )
    return value


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CloseTransactionError(f"close transaction {field} must be a list")
    return tuple(_required_text(item, field=field) for item in value)


def _validation_tuple(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise CloseTransactionError("close transaction validations must be a list")
    rows: list[tuple[str, str]] = []
    for row in value:
        if not isinstance(row, Mapping) or set(row) != {"name", "status"}:
            raise CloseTransactionError(
                "close transaction validation rows must contain exactly name and status"
            )
        rows.append(
            (
                _required_text(row.get("name"), field="validation name"),
                _required_text(row.get("status"), field="validation status"),
            )
        )
        if rows[-1][1] not in VALIDATION_STATUSES:
            raise CloseTransactionError(
                "close transaction validation status is unsupported"
            )
    return tuple(rows)


def _validate_registration(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _REGISTRATION_KEYS:
        raise CloseTransactionError("close transaction source registration is invalid")
    if type(value.get("registered")) is not bool or type(value.get("detached")) is not bool:
        raise CloseTransactionError("close transaction source registration booleans are invalid")
    result = {
        "registered": bool(value["registered"]),
        "path": _optional_text(value.get("path"), field="registration path"),
        "branch": _optional_text(value.get("branch"), field="registration branch"),
        "head": _optional_text(value.get("head"), field="registration head"),
        "detached": bool(value["detached"]),
    }
    if result["registered"] and result["path"] is None:
        raise CloseTransactionError("registered close source is missing its path")
    if result["registered"] and result["head"] is None:
        raise CloseTransactionError("registered close source is missing its HEAD")
    if result["registered"] and not result["detached"] and result["branch"] is None:
        raise CloseTransactionError("attached close source is missing its branch")
    if result["detached"] and result["branch"] is not None:
        raise CloseTransactionError("detached close source unexpectedly has a branch")
    if not result["registered"] and any(
        result[key] is not None for key in ("path", "branch", "head")
    ):
        raise CloseTransactionError(
            "unregistered close source carries registration identity"
        )
    if not result["registered"] and result["detached"]:
        raise CloseTransactionError(
            "unregistered close source cannot be marked detached"
        )
    return result


def _validate_projection(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PROJECTION_KEYS:
        raise CloseTransactionError("close transaction pre-close projection has conflicting fields")
    for key in (
        "source_path_exists",
        "source_is_worktree",
        "worktree_dirty",
        "cleanup_eligible",
    ):
        if type(value.get(key)) is not bool:
            raise CloseTransactionError(f"close transaction {key} must be a boolean")
    branch_state = _required_text(value.get("branch_state"), field="branch_state")
    if branch_state not in {"exists", "missing"}:
        raise CloseTransactionError("close transaction branch_state is unsupported")
    result = {
        "recorded_branch": _optional_text(value.get("recorded_branch"), field="recorded branch"),
        "recorded_target_branch": _optional_text(
            value.get("recorded_target_branch"), field="recorded target branch"
        ),
        "recorded_worktree_path": _optional_text(
            value.get("recorded_worktree_path"), field="recorded worktree path"
        ),
        "resolved_source_path": _optional_text(
            value.get("resolved_source_path"), field="resolved source path"
        ),
        "source_path_exists": bool(value["source_path_exists"]),
        "source_is_worktree": bool(value["source_is_worktree"]),
        "source_registration": _validate_registration(value.get("source_registration")),
        "source_head_commit": _optional_text(
            value.get("source_head_commit"), field="source HEAD"
        ),
        "branch_state": branch_state,
        "branch_commit": _optional_text(value.get("branch_commit"), field="branch commit"),
        "changed_paths": list(_string_tuple(value.get("changed_paths"), field="changed paths")),
        "worktree_dirty": bool(value["worktree_dirty"]),
        "cleanup_eligible": bool(value["cleanup_eligible"]),
        "cleanup_disposition": _required_text(
            value.get("cleanup_disposition"), field="cleanup disposition"
        ),
        "cleanup_proof": _required_text(value.get("cleanup_proof"), field="cleanup proof"),
        "cleanup_reason": _required_text(value.get("cleanup_reason"), field="cleanup reason"),
    }
    if branch_state == "exists" and result["branch_commit"] is None:
        raise CloseTransactionError("existing close branch is missing its commit")
    if branch_state == "missing" and result["branch_commit"] is not None:
        raise CloseTransactionError("missing close branch unexpectedly has a commit")
    if result["source_path_exists"] and result["resolved_source_path"] is None:
        raise CloseTransactionError("existing close source is missing its path")
    if result["source_is_worktree"] and not result["source_path_exists"]:
        raise CloseTransactionError("close worktree source does not exist")
    if result["worktree_dirty"] and not result["source_is_worktree"]:
        raise CloseTransactionError("dirty close source is not a worktree")
    if (
        result["source_registration"]["registered"]
        and result["source_registration"]["path"]
        != result["resolved_source_path"]
    ):
        raise CloseTransactionError(
            "close source registration path conflicts with the resolved source"
        )
    disposition = result["cleanup_disposition"]
    proof = result["cleanup_proof"]
    allowed_proofs = _CLEANUP_DISPOSITION_PROOFS.get(disposition)
    if allowed_proofs is None or proof not in allowed_proofs:
        raise CloseTransactionError(
            "close cleanup disposition and proof are not a supported pair"
        )
    eligible = result["cleanup_eligible"]
    if eligible != (disposition in {"remove", "already_absent"}):
        raise CloseTransactionError(
            "close cleanup eligibility conflicts with its disposition"
        )
    registration = result["source_registration"]
    if disposition == "remove":
        expected_branch_ref = (
            f"refs/heads/{result['recorded_branch']}"
            if result["recorded_branch"] is not None
            else None
        )
        if not (
            result["source_path_exists"]
            and result["source_is_worktree"]
            and result["resolved_source_path"] is not None
            and result["recorded_worktree_path"] == result["resolved_source_path"]
            and registration["registered"]
            and registration["path"] == result["resolved_source_path"]
            and registration["branch"] == expected_branch_ref
            and not registration["detached"]
            and result["branch_state"] == "exists"
            and registration["head"] == result["source_head_commit"]
            and result["branch_commit"] == result["source_head_commit"]
            and not result["worktree_dirty"]
        ):
            raise CloseTransactionError(
                "removable close source lacks exact clean ownership proof"
            )
    elif disposition == "already_absent":
        if not (
            result["recorded_worktree_path"] is not None
            and result["resolved_source_path"] == result["recorded_worktree_path"]
            and not result["source_path_exists"]
            and not result["source_is_worktree"]
            and not registration["registered"]
            and result["branch_state"] == "missing"
            and not result["worktree_dirty"]
        ):
            raise CloseTransactionError(
                "already-absent close cleanup lacks exact absence proof"
            )
    elif disposition == "retain_dirty" and not result["worktree_dirty"]:
        raise CloseTransactionError(
            "dirty close retention lacks a dirty source projection"
        )
    elif disposition == "retain_unlanded" and not (
        result["source_path_exists"]
        and result["source_is_worktree"]
        and registration["registered"]
        and not registration["detached"]
        and result["branch_state"] == "exists"
        and not result["worktree_dirty"]
    ):
        raise CloseTransactionError(
            "unlanded close retention lacks an exact clean source projection"
        )
    return result


@dataclass(frozen=True, slots=True)
class CloseRequest:
    workset_id: str
    task_id: str
    attempt_id: str
    actor: str
    status: str
    summary: str
    validations: tuple[tuple[str, str], ...]
    residuals: tuple[str, ...]
    followup_candidates: tuple[str, ...]
    note: str | None
    failure_class: str | None
    recovery_action: str | None
    prompt_issue: bool
    operator_issue: bool
    cleanup_requested: bool
    cleanup_event_id: str | None
    pre_close_projection: Mapping[str, Any]

    @property
    def request_event_id(self) -> str:
        return close_request_event_id(
            workset_id=self.workset_id,
            task_id=self.task_id,
            attempt_id=self.attempt_id,
        )

    @property
    def finalization_id(self) -> str:
        return close_finalization_id(self.request_event_id)

    @property
    def close_event_id(self) -> str:
        return worktree_close_event_id(self.request_event_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CLOSE_SCHEMA_VERSION,
            "close_request_id": self.request_event_id,
            "finalization_id": self.finalization_id,
            "close_event_id": self.close_event_id,
            "cleanup_event_id": self.cleanup_event_id,
            "workset_id": self.workset_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "actor": self.actor,
            "status": self.status,
            "summary": self.summary,
            "validations": [
                {"name": name, "status": status}
                for name, status in self.validations
            ],
            "residuals": list(self.residuals),
            "followup_candidates": list(self.followup_candidates),
            "note": self.note,
            "failure_class": self.failure_class,
            "recovery_action": self.recovery_action,
            "prompt_issue": self.prompt_issue,
            "operator_issue": self.operator_issue,
            "cleanup_requested": self.cleanup_requested,
            "pre_close_projection": dict(self.pre_close_projection),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CloseRequest":
        if set(payload) != _REQUEST_KEYS:
            raise CloseTransactionError("close request has conflicting fields")
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
            raise CloseTransactionError("close request schema version is unsupported")
        for key in ("prompt_issue", "operator_issue", "cleanup_requested"):
            if type(payload.get(key)) is not bool:
                raise CloseTransactionError(f"close request {key} must be a boolean")
        status = _required_text(payload.get("status"), field="status")
        if status not in CLOSE_STATUSES:
            raise CloseTransactionError("close request status is unsupported")
        result = cls(
            workset_id=_required_text(payload.get("workset_id"), field="workset_id"),
            task_id=_required_text(payload.get("task_id"), field="task_id"),
            attempt_id=_required_text(payload.get("attempt_id"), field="attempt_id"),
            actor=_required_text(payload.get("actor"), field="actor"),
            status=status,
            summary=_required_text(payload.get("summary"), field="summary"),
            validations=_validation_tuple(payload.get("validations")),
            residuals=_string_tuple(payload.get("residuals"), field="residuals"),
            followup_candidates=_string_tuple(
                payload.get("followup_candidates"), field="followup candidates"
            ),
            note=_optional_text(payload.get("note"), field="note"),
            failure_class=_optional_text(payload.get("failure_class"), field="failure class"),
            recovery_action=_optional_text(
                payload.get("recovery_action"), field="recovery action"
            ),
            prompt_issue=bool(payload["prompt_issue"]),
            operator_issue=bool(payload["operator_issue"]),
            cleanup_requested=bool(payload["cleanup_requested"]),
            cleanup_event_id=_optional_text(
                payload.get("cleanup_event_id"), field="cleanup event id"
            ),
            pre_close_projection=_validate_projection(payload.get("pre_close_projection")),
        )
        if result.failure_class not in FAILURE_CLASSES:
            raise CloseTransactionError("close request failure class is unsupported")
        if result.status == "abandoned" and (
            result.failure_class != FAILURE_CLASS_ABANDONED
            or not result.operator_issue
        ):
            raise CloseTransactionError(
                "abandoned close request has invalid derived failure fields"
            )
        cleanup_owns_event = bool(
            result.cleanup_requested
            and result.pre_close_projection["cleanup_eligible"]
        )
        if cleanup_owns_event != (result.cleanup_event_id is not None):
            raise CloseTransactionError(
                "close request cleanup event identity conflicts with cleanup ownership"
            )
        if result.cleanup_event_id is not None:
            source_path = result.pre_close_projection["resolved_source_path"]
            if not isinstance(source_path, str):
                raise CloseTransactionError(
                    "eligible close cleanup is missing its exact source path"
                )
            expected_cleanup_id = worktree_cleanup_event_id(
                workset_id=result.workset_id,
                task_id=result.task_id,
                attempt_id=result.attempt_id,
                branch=result.pre_close_projection["recorded_branch"],
                worktree_path=source_path,
            )
            if result.cleanup_event_id != expected_cleanup_id:
                raise CloseTransactionError(
                    "close request cleanup event identity is not deterministic"
                )
        disposition = str(result.pre_close_projection["cleanup_disposition"])
        if not result.cleanup_requested and disposition != "retain_not_requested":
            raise CloseTransactionError(
                "close request without cleanup has an invalid cleanup disposition"
            )
        if result.cleanup_requested and result.pre_close_projection["cleanup_eligible"]:
            if disposition not in {"remove", "already_absent"}:
                raise CloseTransactionError(
                    "eligible close cleanup has an invalid disposition"
                )
        elif result.cleanup_requested and not disposition.startswith("retain_"):
            raise CloseTransactionError(
                "ineligible close cleanup lacks explicit negative ownership"
            )
        expected = result.to_dict()
        if not strict_json_equal(payload, expected):
            raise CloseTransactionError("close request is not canonical")
        return result


def _scan_close_ledger(
    profile: RepoProfile,
    *,
    include_close_events: bool = True,
) -> tuple[dict[str, CloseRequest], dict[str, dict[str, Any]]]:
    """Parse every recognizable v1 close row before target filtering.

    Legacy ``worktree.close`` rows have no v1 schema/request marker and remain
    readable by older reporting paths. A row that claims any v1 identity is
    never silently downgraded to legacy evidence.
    """

    with exclusive_file_lock(profile.paths.events_file):
        events = load_events(profile.paths.events_file)
    requests: dict[str, CloseRequest] = {}
    for event in events:
        payload = event.get("payload")
        request_type = event.get("type") == CLOSE_REQUEST_EVENT_TYPE
        recognizable = bool(
            isinstance(payload, Mapping)
            and (
                set(payload) == _REQUEST_KEYS
                or "pre_close_projection" in payload
            )
        )
        if request_type and not isinstance(payload, Mapping):
            raise CloseTransactionError("worktree.close.request payload is not an object")
        if not request_type and not recognizable:
            continue
        if not isinstance(payload, Mapping):
            raise CloseTransactionError("recognizable close request payload is invalid")
        request = CloseRequest.from_dict(payload)
        request_id = request.request_event_id
        if (
            event.get("event_id") != request_id
            or event.get("type") != CLOSE_REQUEST_EVENT_TYPE
            or event.get("actor") != request.actor
        ):
            raise CloseTransactionError(
                "close request has a conflicting event identity, type, or actor"
            )
        if request_id in requests:
            raise CloseTransactionError("close request occurs more than once")
        requests[request_id] = request

    for event in events:
        request = requests.get(str(event.get("event_id") or ""))
        if request is None:
            continue
        if (
            event.get("type") != CLOSE_REQUEST_EVENT_TYPE
            or event.get("actor") != request.actor
            or not isinstance(event.get("payload"), Mapping)
            or not strict_json_equal(event["payload"], request.to_dict())
        ):
            raise CloseTransactionError(
                "close request event identity collides with a conflicting row"
            )

    if not include_close_events:
        return requests, {}

    close_events: dict[str, dict[str, Any]] = {}
    close_ids = {
        request.close_event_id: request for request in requests.values()
    }
    for event in events:
        payload = event.get("payload")
        close_type = event.get("type") == CLOSE_EVENT_TYPE
        if close_type and not isinstance(payload, Mapping):
            raise CloseTransactionError("worktree.close payload is not an object")
        recognizable = bool(
            isinstance(payload, Mapping)
            and (
                set(payload) == _CLOSE_EVENT_KEYS
                or "core_finalization" in payload
                or (
                    close_type
                    and any(
                        marker in payload
                        for marker in (
                            "schema_version",
                            "close_request_id",
                            "close_event_id",
                            "core_finalization",
                        )
                    )
                )
            )
        ) or event.get("event_id") in close_ids
        if not recognizable:
            # A pre-v1 worktree.close row has no close_request_id/schema marker.
            # It is intentionally outside this transaction ledger.
            continue
        if not isinstance(payload, Mapping):
            raise CloseTransactionError("recognizable worktree.close payload is invalid")
        request_id = payload.get("close_request_id")
        if not isinstance(request_id, str) or request_id not in requests:
            raise CloseTransactionError(
                "worktree.close is orphaned from its deterministic close request"
            )
        request = requests[request_id]
        canonical = validate_close_event_payload(request, payload)
        if (
            not close_type
            or event.get("event_id") != request.close_event_id
            or event.get("actor") != request.actor
        ):
            raise CloseTransactionError(
                "worktree.close has a conflicting event identity, type, or actor"
            )
        if request_id in close_events:
            raise CloseTransactionError("worktree.close occurs more than once")
        close_events[request_id] = canonical
    return requests, close_events


def load_close_request(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    attempt_id: str,
) -> CloseRequest | None:
    expected_id = close_request_event_id(
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
    )
    requests, _close_events = _scan_close_ledger(profile)
    request = requests.get(expected_id)
    if request is None:
        with exclusive_file_lock(profile.paths.events_file):
            occupied = any(
                event.get("event_id") == expected_id
                for event in load_events(profile.paths.events_file)
            )
        if occupied:
            raise CloseTransactionError(
                "deterministic close request identity is occupied by a conflicting row"
            )
    if request is not None and (
        request.workset_id != workset_id
        or request.task_id != task_id
        or request.attempt_id != attempt_id
    ):
        raise CloseTransactionError("parsed close request does not match requested target")
    return request


def load_close_request_by_id(
    profile: RepoProfile,
    request_event_id: str,
) -> CloseRequest | None:
    requests, _close_events = _scan_close_ledger(profile)
    request = requests.get(request_event_id)
    if request is None:
        with exclusive_file_lock(profile.paths.events_file):
            occupied = any(
                event.get("event_id") == request_event_id
                for event in load_events(profile.paths.events_file)
            )
        if occupied:
            raise CloseTransactionError(
                "guarded close request identity is occupied by a conflicting row"
            )
    return request


def load_close_request_record_by_id(
    profile: RepoProfile,
    request_event_id: str,
) -> CloseRequest | None:
    """Load one strict request without interpreting its later close receipt.

    Public product surfaces use this narrow read only to recover immutable
    request identity when the later close row itself is corrupt.  The product
    operation can then return a typed, commandless evidence conflict instead
    of leaking a parser exception.  It never relaxes request-ledger checks.
    """

    requests, _close_events = _scan_close_ledger(
        profile,
        include_close_events=False,
    )
    request = requests.get(request_event_id)
    if request is None:
        with exclusive_file_lock(profile.paths.events_file):
            occupied = any(
                event.get("event_id") == request_event_id
                for event in load_events(profile.paths.events_file)
            )
        if occupied:
            raise CloseTransactionError(
                "guarded close request identity is occupied by a conflicting row"
            )
    return request


def record_close_request(profile: RepoProfile, request: CloseRequest) -> bool:
    changed = append_event_once(
        profile.paths.events_file,
        event_id=request.request_event_id,
        event_type=CLOSE_REQUEST_EVENT_TYPE,
        actor=request.actor,
        payload=request.to_dict(),
    )
    loaded = load_close_request(
        profile,
        workset_id=request.workset_id,
        task_id=request.task_id,
        attempt_id=request.attempt_id,
    )
    if loaded != request:
        raise CloseTransactionError("close request was not durably recorded")
    return changed


def _validate_core_evidence(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CORE_EVIDENCE_KEYS:
        raise CloseTransactionError("close core-finalization evidence is invalid")
    if type(value.get("runtime_finalized")) is not bool:
        raise CloseTransactionError("close runtime-finalized evidence must be boolean")
    result = {
        key: _optional_text(value.get(key), field=key)
        for key in _CORE_EVIDENCE_KEYS
        if key != "runtime_finalized"
    }
    result["runtime_finalized"] = bool(value["runtime_finalized"])
    return result


def _validate_cleanup_evidence(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CLEANUP_EVIDENCE_KEYS:
        raise CloseTransactionError("close cleanup evidence is invalid")
    for key in (
        "requested",
        "eligible",
        "performed",
        "worktree_removed",
        "branch_deleted",
        "retained",
    ):
        if type(value.get(key)) is not bool:
            raise CloseTransactionError(f"close cleanup evidence {key} must be boolean")
    return {
        "requested": bool(value["requested"]),
        "eligible": bool(value["eligible"]),
        "event_id": _optional_text(value.get("event_id"), field="cleanup event id"),
        "performed": bool(value["performed"]),
        "worktree_removed": bool(value["worktree_removed"]),
        "branch_deleted": bool(value["branch_deleted"]),
        "retained": bool(value["retained"]),
        "reason": _required_text(value.get("reason"), field="cleanup reason"),
        "proof": _required_text(value.get("proof"), field="cleanup proof"),
    }


def validate_close_event_payload(
    request: CloseRequest,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if set(payload) != _CLOSE_EVENT_KEYS:
        raise CloseTransactionError("worktree.close has conflicting fields")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise CloseTransactionError("worktree.close schema version is unsupported")
    expected = {
        "close_request_id": request.request_event_id,
        "finalization_id": request.finalization_id,
        "close_event_id": request.close_event_id,
        "workset_id": request.workset_id,
        "task_id": request.task_id,
        "attempt_id": request.attempt_id,
        "actor": request.actor,
        "status": request.status,
        "summary": request.summary,
        "branch": request.pre_close_projection["recorded_branch"],
        "target_branch": request.pre_close_projection["recorded_target_branch"],
        "worktree_path": request.pre_close_projection["resolved_source_path"],
        "changed_paths": list(request.pre_close_projection["changed_paths"]),
        "commit": request.pre_close_projection["source_head_commit"],
        "cleanup_requested": request.cleanup_requested,
        "failure_class": request.failure_class,
        "recovery_action": request.recovery_action,
        "prompt_issue": request.prompt_issue,
        "operator_issue": request.operator_issue,
    }
    for key, value in expected.items():
        if not strict_json_equal(payload.get(key), value):
            raise CloseTransactionError(f"worktree.close conflicts on {key}")
    for key in ("cleanup_performed", "prompt_issue", "operator_issue"):
        if type(payload.get(key)) is not bool:
            raise CloseTransactionError(f"worktree.close {key} must be boolean")
    cleanup_reason = _required_text(payload.get("cleanup_reason"), field="cleanup reason")
    core = _validate_core_evidence(payload.get("core_finalization"))
    cleanup = _validate_cleanup_evidence(payload.get("cleanup"))
    expected_core_request_id = task_finalization_request_event_id(
        workset_id=request.workset_id,
        task_id=request.task_id,
        attempt_id=request.attempt_id,
    )
    if core["request_event_id"] != expected_core_request_id:
        raise CloseTransactionError(
            "worktree.close core request identity is not deterministic"
        )
    decision_event_id = core["decision_event_id"]
    if decision_event_id is None:
        raise CloseTransactionError("worktree.close lacks its core decision proof")
    expected_task_release_id = task_finalization_owned_event_id(
        decision_event_id=decision_event_id,
        event_type="task.release",
    )
    expected_task_finish_id = task_finalization_owned_event_id(
        decision_event_id=decision_event_id,
        event_type="task.finish",
    )
    expected_workset_release_id = task_finalization_owned_event_id(
        decision_event_id=decision_event_id,
        event_type="workset.release",
    )
    if core["task_release_event_id"] != expected_task_release_id:
        raise CloseTransactionError(
            "worktree.close task-release identity is not decision-owned"
        )
    if core["task_finish_event_id"] != expected_task_finish_id:
        raise CloseTransactionError(
            "worktree.close task-finish identity is not decision-owned"
        )
    if core["workset_release_event_id"] not in {
        None,
        expected_workset_release_id,
    }:
        raise CloseTransactionError(
            "worktree.close workset-release identity is not decision-owned"
        )
    if not core["runtime_finalized"]:
        raise CloseTransactionError("worktree.close lacks complete core finalization proof")
    if cleanup["performed"] != payload["cleanup_performed"]:
        raise CloseTransactionError("worktree.close cleanup summary conflicts with evidence")
    if cleanup["reason"] != cleanup_reason:
        raise CloseTransactionError("worktree.close cleanup reason conflicts with evidence")
    projection = request.pre_close_projection
    event_recorded = request.cleanup_event_id is not None
    expected_cleanup = {
        "requested": request.cleanup_requested,
        "eligible": bool(projection["cleanup_eligible"]),
        "event_id": request.cleanup_event_id,
        "performed": bool(
            event_recorded and projection["cleanup_disposition"] == "remove"
        ),
        "worktree_removed": bool(
            event_recorded
            and projection["cleanup_disposition"] == "remove"
            and projection["source_path_exists"]
        ),
        "branch_deleted": bool(
            event_recorded
            and projection["cleanup_disposition"] == "remove"
            and projection["branch_state"] == "exists"
        ),
        "retained": not event_recorded,
        "reason": projection["cleanup_reason"],
        "proof": projection["cleanup_proof"],
    }
    if not strict_json_equal(cleanup, expected_cleanup):
        raise CloseTransactionError(
            "worktree.close cleanup evidence conflicts with its immutable request"
        )
    return {
        **dict(payload),
        "core_finalization": core,
        "cleanup": cleanup,
    }


def load_close_event(
    profile: RepoProfile,
    request: CloseRequest,
) -> dict[str, Any] | None:
    requests, close_events = _scan_close_ledger(profile)
    loaded = requests.get(request.request_event_id)
    if loaded != request:
        raise CloseTransactionError("worktree.close request is missing or changed")
    return close_events.get(request.request_event_id)


def record_close_event(
    profile: RepoProfile,
    *,
    request: CloseRequest,
    payload: Mapping[str, Any],
) -> bool:
    canonical = validate_close_event_payload(request, payload)
    changed = append_event_once(
        profile.paths.events_file,
        event_id=request.close_event_id,
        event_type=CLOSE_EVENT_TYPE,
        actor=request.actor,
        payload=canonical,
    )
    loaded = load_close_event(profile, request)
    if loaded is None or not strict_json_equal(loaded, canonical):
        raise CloseTransactionError("worktree.close was not durably recorded")
    return changed


def close_requests_for_task(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
) -> tuple[CloseRequest, ...]:
    requests, _close_events = _scan_close_ledger(profile)
    return tuple(
        request
        for _request_id, request in sorted(requests.items())
        if request.workset_id == workset_id and request.task_id == task_id
    )


__all__ = [
    "CLOSE_EVENT_TYPE",
    "CLOSE_REQUEST_EVENT_TYPE",
    "CloseRequest",
    "CloseTransactionError",
    "close_finalization_id",
    "close_request_event_id",
    "close_requests_for_task",
    "load_close_event",
    "load_close_request",
    "load_close_request_by_id",
    "load_close_request_record_by_id",
    "record_close_event",
    "record_close_request",
    "validate_close_event_payload",
    "worktree_close_event_id",
    "worktree_cleanup_event_id",
]
