"""Owned outcome admission and machine validation without lifecycle mutation."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
from typing import Any, Iterator

from blackdog.git_worktrees import inspect_task_worktree
from blackdog.runtime_distribution import (
    RuntimeDistributionError,
    release_bytes,
    runtime_identity,
)
from blackdog.landing import attempt_lifecycle_lock
from blackdog.validation import run_validation_commands
from blackdog_core.evidence import (
    ASSESSMENT,
    DEFINITION,
    INTERVENTION,
    VALIDATION_INTENT,
    VALIDATION_RESULT,
    MAX_DOCUMENT_BYTES,
    Assessment,
    EvidenceError,
    EvidenceEvent,
    Intervention,
    OutcomeDefinition,
    ValidationBinding,
    canonical_json,
    digest,
    event_identity,
    read_evidence,
    matching_validation_inputs,
    token,
)
from blackdog_core.profile import ConfigError, RepoProfile, load_profile
from blackdog.lifecycle import WorktreeError
from blackdog_core.state import (
    RuntimeState,
    TaskAttemptRecord,
    append_event_once,
    exclusive_file_lock,
    load_events,
    load_runtime_state,
    now_iso,
    task_record,
)

LANDING_AUTHORITY = "none: evidence observations do not authorize landing; repository guards and supplied validations remain authoritative"


def read_document(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        raw = handle.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise EvidenceError("evidence input exceeds size limit")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceError("evidence input contains duplicate keys")
            result[key] = value
        return result

    def invalid_number(value: str) -> None:
        raise EvidenceError("evidence input contains a nonfinite number")

    try:
        value = json.loads(
            raw, object_pairs_hook=unique_pairs, parse_constant=invalid_number
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("evidence input must be UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise EvidenceError("evidence input must be an object")
    return value


def _attempt(
    state: RuntimeState,
    task_id: str,
    attempt_id: str,
    *,
    actor: str | None = None,
    active: bool = False,
) -> TaskAttemptRecord:
    task = task_record(state, task_id)
    attempt = (
        next((a for a in task.attempts if a.attempt_id == attempt_id), None)
        if task
        else None
    )
    if attempt is None:
        raise EvidenceError(
            "evidence requires an existing attempt owned by the selected task"
        )
    if actor is not None and actor != attempt.actor:
        raise EvidenceError("evidence actor must match the attempt owner")
    if active and attempt.status != "in_progress":
        raise EvidenceError("new evidence requires the task's active attempt")
    return attempt


def _attempt_owners(state: RuntimeState) -> dict[str, str]:
    return {a.attempt_id: task.task_id for task in state.tasks for a in task.attempts}


@contextmanager
def locked_evidence(
    profile: RepoProfile,
) -> Iterator[
    tuple[RuntimeState, tuple[dict[str, Any], ...], tuple[EvidenceEvent, ...]]
]:
    # The existing core lifecycle order is runtime, then events. Never invert it.
    with exclusive_file_lock(profile.paths.runtime_file):
        with exclusive_file_lock(profile.paths.events_file):
            state = load_runtime_state(profile.paths)
            rows = load_events(profile.paths.events_file)
            yield state, rows, read_evidence(rows, _attempt_owners(state))


def _append(
    profile: RepoProfile,
    *,
    state: RuntimeState,
    rows: tuple[dict[str, Any], ...],
    kind: str,
    task_id: str,
    attempt_id: str,
    actor: str,
    request_id: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    event_id = event_identity(kind, task_id, attempt_id, request_id)
    payload = {
        "schema_version": 1,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "data": data,
    }
    candidate = {
        "event_id": event_id,
        "type": kind,
        "actor": actor,
        "at": now_iso(),
        "payload": payload,
    }
    existing = next((row for row in rows if row["event_id"] == event_id), None)
    if existing is None:
        read_evidence((*rows, candidate), _attempt_owners(state))
    elif any(
        canonical_json(existing[key]) != canonical_json(candidate[key])
        for key in ("type", "actor", "payload")
    ):
        raise EvidenceError(
            "evidence request identity conflicts with its immutable recorded content"
        )
    changed = append_event_once(
        profile.paths.events_file,
        event_id=event_id,
        event_type=kind,
        actor=actor,
        payload=payload,
    )
    return {
        "status": "recorded" if changed else "unchanged",
        "event_id": event_id,
        "landing_authority": LANDING_AUTHORITY,
    }


def _git(workspace: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args], env=env, capture_output=True, text=True
    )
    if result.returncode:
        raise EvidenceError("unable to observe validation Git content")
    return result.stdout.strip()


def source_tree(workspace: Path) -> tuple[str, str]:
    """Observe the effective tracked/untracked tree using a private Git index.

    Git's ordinary ignore rules apply. The user's index is never changed. The
    resulting object identity also matches the tree a normal add-all commits.
    """
    head = _git(workspace, "rev-parse", "HEAD^{commit}")
    if _git(workspace, "ls-files", "--unmerged"):
        raise EvidenceError("cannot validate an unresolved Git index")
    with tempfile.TemporaryDirectory(prefix="blackdog-validation-index-") as directory:
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(Path(directory) / "index")
        _git(workspace, "read-tree", head, env=environment)
        _git(workspace, "add", "-A", env=environment)
        tree = _git(workspace, "write-tree", env=environment)
    if _git(workspace, "rev-parse", "HEAD^{commit}") != head:
        raise EvidenceError("Git HEAD changed during validation binding")
    return head, tree


def runtime_descriptor() -> dict[str, Any]:
    try:
        identity = runtime_identity()
        if identity is not None:
            return {**identity, "missing_reason": None}
        # Use the release builder's indexed/manifest inclusion and containment
        # checks. This identifies its deterministic distribution projection;
        # it does not claim coverage of arbitrary importable, untracked code.
        return {
            "kind": "source_sha256",
            "value": hashlib.sha256(release_bytes()).hexdigest(),
            "missing_reason": None,
        }
    except (OSError, RuntimeDistributionError):
        return {
            "kind": "unknown",
            "value": None,
            "missing_reason": "runtime_source_unavailable",
        }


def environment_descriptor(profile: RepoProfile) -> dict[str, Any]:
    # Hash only explicit repository configuration and the shipped distribution.
    # Do not inspect environment variables, provider data, or private content.
    return {
        "schema_version": 1,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "handlers_sha256": digest([asdict(handler) for handler in profile.handlers]),
        "runtime_identity": runtime_descriptor(),
        "coverage": "bounded_runtime_only",
    }


def command_fingerprint(profile: RepoProfile) -> tuple[tuple[str, ...], str]:
    hashes = tuple(
        hashlib.sha256(command.encode()).hexdigest()
        for command in profile.validation_commands
    )
    return hashes, digest(
        {
            "commands": list(hashes),
            "timeout_seconds": profile.landing.validation_timeout_seconds,
        }
    )


def observe_binding(workspace: Path) -> ValidationBinding:
    profile = load_profile(workspace)
    commit, tree = source_tree(workspace)
    environment = environment_descriptor(profile)
    commands, command_set = command_fingerprint(profile)
    return ValidationBinding(
        source_commit=commit,
        source_tree=tree,
        command_set_sha256=command_set,
        command_sha256=commands,
        timeout_seconds=profile.landing.validation_timeout_seconds,
        environment=environment,
        environment_sha256=digest(environment),
    )


def _workspace(profile: RepoProfile, attempt: TaskAttemptRecord) -> Path:
    proof = inspect_task_worktree(profile, attempt)
    if not proof.valid or proof.path is None:
        raise EvidenceError(
            "validation workspace failed canonical task ownership proof"
        )
    path = Path(proof.path)
    # The shared proof establishes registration, role, branch HEAD and start
    # ancestry; additionally retain the same-repository boundary here.
    expected = _git(
        profile.paths.project_root,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
    )
    observed = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if Path(expected).resolve() != Path(observed).resolve():
        raise EvidenceError("attempt workspace belongs to another repository")
    return path


def terminal_artifact_tree(profile: RepoProfile, attempt: TaskAttemptRecord) -> str:
    if attempt.commit:
        try:
            return _git(
                profile.paths.project_root, "rev-parse", f"{attempt.commit}^{{tree}}"
            )
        except EvidenceError:
            pass
    # Squash landing can make the source commit collectible after branch
    # cleanup. The existing typed landing transaction retains its exact tree.
    if attempt.landed_commit and attempt.task_id:
        from blackdog.landing import load_landing_transaction

        transaction = load_landing_transaction(
            profile, task_id=attempt.task_id, attempt_id=attempt.attempt_id
        )
        if transaction is not None:
            intent = transaction.intent
            tree = _git(
                profile.paths.project_root,
                "rev-parse",
                f"{attempt.landed_commit}^{{tree}}",
            )
            canonical = transaction.data_for("canonical_commit_created").get(
                "canonical_commit"
            )
            if (
                intent.source_head_commit == attempt.commit
                and intent.expected_source_tree_hash == tree
                and canonical == attempt.landed_commit
                and transaction.target_updated
            ):
                return tree
    raise EvidenceError(
        "terminal assessment requires verifiable source or canonical landing tree evidence"
    )


def observe_report_binding(
    profile: RepoProfile,
    attempt: TaskAttemptRecord,
) -> tuple[ValidationBinding | None, str | None]:
    """Observe only a clean active workspace; reports never stage Git content."""
    if attempt.status != "in_progress":
        return None, "terminal_environment_not_reobserved"
    try:
        workspace = _workspace(profile, attempt)
        if _git(workspace, "status", "--porcelain", "--untracked-files=normal"):
            return None, "dirty_workspace_not_reobserved"
        current_profile = load_profile(workspace)
        commit = _git(workspace, "rev-parse", "HEAD^{commit}")
        tree = _git(workspace, "rev-parse", f"{commit}^{{tree}}")
        environment = environment_descriptor(current_profile)
        commands, command_set = command_fingerprint(current_profile)
        if _git(workspace, "rev-parse", "HEAD^{commit}") != commit or _git(
            workspace, "status", "--porcelain", "--untracked-files=normal"
        ):
            return None, "workspace_changed_during_observation"
        return (
            ValidationBinding(
                source_commit=commit,
                source_tree=tree,
                command_set_sha256=command_set,
                command_sha256=commands,
                timeout_seconds=current_profile.landing.validation_timeout_seconds,
                environment=environment,
                environment_sha256=digest(environment),
            ),
            None,
        )
    except (EvidenceError, ConfigError, WorktreeError, OSError):
        return None, "current_binding_unavailable"


def record_outcome(
    profile: RepoProfile,
    *,
    task_id: str,
    attempt_id: str,
    actor: str,
    kind: str,
    document: dict[str, Any],
) -> dict[str, Any]:
    if kind == DEFINITION:
        parsed = OutcomeDefinition.parse(document)
        request_id, data = parsed.sha256, parsed.to_dict()
    elif kind == ASSESSMENT:
        parsed = Assessment.parse(document)
        request_id, data = parsed.assessment_id, {"assessment": parsed.to_dict()}
    elif kind == INTERVENTION:
        parsed = Intervention.parse(document)
        request_id, data = parsed.measurement_id, parsed.to_dict()
    else:
        raise EvidenceError(
            "caller may record only definitions, assessments and declared interventions"
        )
    with attempt_lifecycle_lock(profile, task_id=task_id, attempt_id=attempt_id):
        with locked_evidence(profile) as (state, _, events):
            attempt = _attempt(state, task_id, attempt_id, actor=actor)
            event_id = event_identity(kind, task_id, attempt_id, request_id)
            existing = next((e for e in events if e.event_id == event_id), None)
            if existing is None and kind == DEFINITION:
                _attempt(state, task_id, attempt_id, actor=actor, active=True)
        # Potentially expensive Git/filter execution holds only this attempt's
        # lock. Other tasks may continue using the shared runtime and ledger.
        if kind == ASSESSMENT:
            if existing:
                data["source_tree"] = existing.data["source_tree"]
            elif attempt.status == "in_progress":
                data["source_tree"] = source_tree(_workspace(profile, attempt))[1]
            else:
                data["source_tree"] = terminal_artifact_tree(profile, attempt)
        with locked_evidence(profile) as (state, rows, _):
            if _attempt(state, task_id, attempt_id, actor=actor) != attempt:
                raise EvidenceError(
                    "attempt changed while outcome evidence was observed"
                )
            result = _append(
                profile,
                state=state,
                rows=rows,
                kind=kind,
                task_id=task_id,
                attempt_id=attempt_id,
                actor=actor,
                request_id=request_id,
                data=data,
            )
    if kind == DEFINITION:
        result["definition_sha256"] = request_id
    result["reviewer_identity_authenticated"] = False
    return result


def receipt_applicability(
    intent: EvidenceEvent, receipt: EvidenceEvent, current: ValidationBinding | None
) -> dict[str, Any]:
    before = intent.data["binding"]
    after = receipt.data["binding_after"]
    if after is None:
        status, reason = "unknown", "post_run_binding_unavailable"
    elif (
        before["environment"]["runtime_identity"]["kind"] == "unknown"
        or after["environment"]["runtime_identity"]["kind"] == "unknown"
    ):
        status, reason = "unknown", "runtime_identity_unavailable"
    elif not matching_validation_inputs(before, after):
        status, reason = "stale", "observed_inputs_changed_during_run"
    elif current is None:
        status, reason = "unknown", "current_workspace_unavailable"
    elif current.environment["runtime_identity"]["kind"] == "unknown":
        status, reason = "unknown", "runtime_identity_unavailable"
    elif not matching_validation_inputs(current.to_dict(), before):
        status, reason = "stale", "current_observed_inputs_differ"
    else:
        status, reason = "current", None
    return {
        "status": status,
        "missing_reason": reason,
        "scope": "observed Git tree, configured commands and bounded runtime descriptor",
        "unobserved": [
            "ambient environment values",
            "external dependencies and services",
        ],
        "landing_authority": "none",
    }


def validate_task(
    profile: RepoProfile,
    *,
    task_id: str,
    attempt_id: str,
    actor: str,
    run_id: str,
) -> dict[str, Any]:
    token(run_id, "run_id")
    with attempt_lifecycle_lock(profile, task_id=task_id, attempt_id=attempt_id):
        with locked_evidence(profile) as (state, _, events):
            attempt = _attempt(state, task_id, attempt_id, actor=actor)
            intent_id = event_identity(VALIDATION_INTENT, task_id, attempt_id, run_id)
            intent = next((e for e in events if e.event_id == intent_id), None)
            receipt = next(
                (
                    e
                    for e in events
                    if e.kind == VALIDATION_RESULT
                    and e.data["intent_event_id"] == intent_id
                ),
                None,
            )
            if intent is None:
                _attempt(state, task_id, attempt_id, actor=actor, active=True)
        if receipt is not None:
            try:
                current = observe_binding(_workspace(profile, attempt))
            except (EvidenceError, ConfigError, WorktreeError, OSError):
                current = None
            return {
                "status": "completed",
                "replayed": True,
                "event_id": receipt.event_id,
                "receipt": receipt.data,
                "applicability": receipt_applicability(intent, receipt, current),
                "landing_authority": LANDING_AUTHORITY,
            }
        if intent is not None:
            return {
                "status": "indeterminate",
                "run_id": run_id,
                "intent_event_id": intent_id,
                "missing_reason": "invocation_started_without_durable_result",
                "rerun_performed": False,
                "landing_authority": LANDING_AUTHORITY,
            }
        workspace = _workspace(profile, attempt)
        run_profile = load_profile(workspace)
        if not run_profile.validation_commands:
            raise EvidenceError(
                "task validation requires at least one configured command"
            )
        binding = observe_binding(workspace)
        if binding.command_set_sha256 != command_fingerprint(run_profile)[1]:
            raise EvidenceError("validation configuration changed during binding")
        with locked_evidence(profile) as (state, rows, _):
            if (
                _attempt(state, task_id, attempt_id, actor=actor, active=True)
                != attempt
            ):
                raise EvidenceError(
                    "attempt changed while validation inputs were observed"
                )
            _append(
                profile,
                state=state,
                rows=rows,
                kind=VALIDATION_INTENT,
                task_id=task_id,
                attempt_id=attempt_id,
                actor="blackdog",
                request_id=run_id,
                data={"run_id": run_id, "binding": binding.to_dict()},
            )
        # Intent is durable before configured validation commands. Interrupted invocations
        # remain indeterminate; the same identity never implicitly runs twice.
        run = run_validation_commands(
            run_profile.validation_commands,
            cwd=workspace,
            timeout_seconds=run_profile.landing.validation_timeout_seconds,
        )
        try:
            after = observe_binding(_workspace(profile, attempt))
        except (EvidenceError, ConfigError, WorktreeError, OSError):
            after = None
        with locked_evidence(profile) as (state, rows, events):
            _attempt(state, task_id, attempt_id, actor=actor)
            data = {
                "run_id": run_id,
                "intent_event_id": intent_id,
                "binding_after": after.to_dict() if after else None,
                "run": run.to_dict(),
            }
            result = _append(
                profile,
                state=state,
                rows=rows,
                kind=VALIDATION_RESULT,
                task_id=task_id,
                attempt_id=attempt_id,
                actor="blackdog",
                request_id=run_id,
                data=data,
            )
            intent = next(e for e in events if e.event_id == intent_id)
            receipt = EvidenceEvent(
                event_id=result["event_id"],
                kind=VALIDATION_RESULT,
                actor="blackdog",
                task_id=task_id,
                attempt_id=attempt_id,
                data=data,
                at=now_iso(),
            )
        return {
            "status": "completed",
            "replayed": False,
            "event_id": result["event_id"],
            "receipt": data,
            "applicability": receipt_applicability(intent, receipt, after),
            "landing_authority": LANDING_AUTHORITY,
        }
