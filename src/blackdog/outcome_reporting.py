"""Outcome and cost reports derived solely from typed runtime and event evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
import math
from pathlib import PurePosixPath
from typing import Any

from blackdog.evidence import (
    LANDING_AUTHORITY,
    locked_evidence,
    terminal_artifact_tree,
    observe_report_binding,
    receipt_applicability,
)
from blackdog_core.evidence import (
    ASSESSMENT,
    DEFINITION,
    INTERVENTION,
    PHASE,
    VALIDATION_INTENT,
    VALIDATION_RESULT,
    Assessment,
    EvidenceError,
    EvidenceEvent,
    OutcomeDefinition,
    SetupMeasurement,
    digest,
    fields,
    sha,
    text,
)
from blackdog_core.profile import RepoProfile
from blackdog_core.state import RuntimeState, TaskAttemptRecord, parse_iso


def distribution(
    values: list[int], *, population: int | None, unit: str, source: str
) -> dict[str, Any]:
    ordered = sorted(values)
    size = len(ordered)
    median = (ordered[(size - 1) // 2] + ordered[size // 2]) / 2 if size else None
    return {
        "unit": unit,
        "source": source,
        "population": population,
        "samples": size,
        "missing": population - size if population is not None else None,
        "median": median,
        "p95": ordered[math.ceil(size * 0.95) - 1] if size else None,
        "percentile_method": "nearest_rank",
        "small_sample": size < 20,
    }


def _elapsed(attempt: TaskAttemptRecord) -> int | None:
    if attempt.ended_at is None:
        return None
    start, end = parse_iso(attempt.started_at), parse_iso(attempt.ended_at)
    if start is None or end is None or end < start:
        return None
    return int((end - start).total_seconds() * 1000)


def _artifact_tree(
    profile: RepoProfile, attempt: TaskAttemptRecord | None
) -> str | None:
    if attempt is None:
        return None
    try:
        return terminal_artifact_tree(profile, attempt)
    except EvidenceError:
        return None


def aggregate_quality(
    rows: list[dict[str, Any]], *, kind: str | None = None
) -> dict[str, Any]:
    sources = ("caller_declared", "reviewer_asserted")
    field = f"{kind}_assessment_coverage" if kind else "assessment_coverage"
    return {
        "defined_criteria": sum(r[field]["defined_criteria"] for r in rows),
        "required_criteria": sum(
            r[field]["required_criteria"] for r in rows
        ),
        "current_assessed_required_criteria": sum(
            r[field]["current_assessed_required_criteria"] for r in rows
        ),
        **{
            name: sum(r[field][name] for r in rows)
            for name in (
                "missing_required_assessments",
                "not_assessed_required_criteria",
                "stale_required_assessments",
                "unknown_required_assessments",
                "current_not_met_required_criteria",
            )
        },
        "current_required_by_provenance": {
            source: sum(
                r[field]["current_required_by_provenance"][source]
                for r in rows
            )
            for source in sources
        },
        "recorded_assessments_by_provenance": {
            source: sum(
                r[field]["recorded_assessments_by_provenance"][source]
                for r in rows
            )
            for source in sources
        },
        ("provenance_counts" if kind else "outcome_provenance_counts"): dict(
            Counter(
                r[f"{kind}_provenance" if kind else "criteria_provenance"]
                for r in rows
            )
        ),
        "reviewer_identity_authenticated": False,
    }


def _criterion_status(
    criteria: list[dict[str, Any]], events: list[EvidenceEvent], *, empty: str
) -> tuple[str, str, dict[str, Any]]:
    required = [c for c in criteria if c["required"]]
    current = [
        c for c in required
        if c["assessment"] is not None
        and c["result"] != "not_assessed"
        and c["artifact_applicability"] == "current"
    ]
    if not criteria:
        result = empty
    elif not required or len(current) != len(required):
        result = "not_assessed"
    else:
        result = "not_met" if any(c["result"] == "not_met" for c in current) else "met"
    provenance = Counter(c["assessment"]["provenance"] for c in current)
    ids = {c["id"] for c in criteria}
    recorded = Counter(
        e.data["assessment"]["provenance"] for e in events
        if e.kind == ASSESSMENT and e.data["assessment"]["criterion_id"] in ids
    )
    provenance_label = (
        ("not_defined" if empty == "not_defined" else "incomplete") if not criteria
        else "incomplete" if len(current) != len(required) or not required
        else next(iter(provenance)) if len(provenance) == 1 else "mixed"
    )
    return result, provenance_label, {
        "defined_criteria": len(criteria),
        "required_criteria": len(required),
        "current_assessed_required_criteria": len(current),
        "missing_required_assessments": sum(c["assessment"] is None for c in required),
        "not_assessed_required_criteria": sum(
            c["assessment"] is not None and c["result"] == "not_assessed"
            for c in required
        ),
        "stale_required_assessments": sum(
            c["assessment"] is not None and c["artifact_applicability"] == "stale"
            for c in required
        ),
        "unknown_required_assessments": sum(
            c["assessment"] is not None and c["artifact_applicability"] == "unknown"
            for c in required
        ),
        "current_not_met_required_criteria": sum(
            c["result"] == "not_met" for c in current
        ),
        "current_required_by_provenance": {
            source: provenance[source]
            for source in ("caller_declared", "reviewer_asserted")
        },
        "recorded_assessments_by_provenance": {
            source: recorded[source]
            for source in ("caller_declared", "reviewer_asserted")
        },
    }


def _attempt_context(attempt: TaskAttemptRecord) -> dict[str, Any]:
    """Project declarations, never infer historical execution from this host."""
    receipt = attempt.setup_receipt or {}
    host = receipt.get("execution_context")
    try:
        fields(host, "schema_version source host host_version")
        if (
            type(host["schema_version"]) is not int
            or host["schema_version"] != 1
            or host["source"] != "caller_declared"
        ):
            raise EvidenceError("unsupported execution context")
        text(host["host"], "host", limit=128)
        if host["host_version"] is not None:
            text(host["host_version"], "host_version", limit=128)
        valid_host = True
    except EvidenceError:
        valid_host = False
    guidance = receipt.get("guidance")
    try:
        fields(guidance, "schema_version selection_source documents")
        if (
            type(guidance["schema_version"]) is not int
            or guidance["schema_version"] != 1
            or guidance["selection_source"] != "host_declared"
            or not isinstance(guidance["documents"], list)
            or not 1 <= len(guidance["documents"]) <= 16
        ):
            raise EvidenceError("unsupported guidance context")
        seen: set[PurePosixPath] = set()
        for document in guidance["documents"]:
            fields(document, "path sha256")
            relative = PurePosixPath(text(document["path"], "guidance path"))
            if relative.is_absolute() or ".." in relative.parts or relative == PurePosixPath("."):
                raise EvidenceError("guidance path is not lexically contained")
            if relative in seen:
                raise EvidenceError("duplicate guidance path")
            seen.add(relative)
            sha(document["sha256"], "guidance document sha256")
        valid_guidance = True
    except EvidenceError:
        valid_guidance = False
    values = {
        "model": attempt.model,
        "reasoning_effort": attempt.reasoning_effort,
        "host": host.get("host") if valid_host else None,
        "host_version": host.get("host_version") if valid_host else None,
        "guidance": guidance if valid_guidance else None,
    }
    missing = [key for key, value in values.items() if value is None]
    return {
        "attempt_id": attempt.attempt_id,
        **values,
        "model_source": "attempt_record_caller_declared",
        "host_context_status": (
            "recorded" if valid_host
            else "not_recorded" if host is None else "invalid_or_unsupported"
        ),
        "guidance_status": (
            "recorded" if valid_guidance
            else "not_recorded" if guidance is None else "invalid_or_unsupported"
        ),
        "context_sha256": digest(values),
        "missing_fields": missing,
        "complete": not missing,
        "compliance_attested": False,
    }


def aggregate_context(rows: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = [a for row in rows for a in row["execution_context"]["attempts"]]
    return {
        "attempts": len(attempts),
        "complete_attempts": sum(a["complete"] for a in attempts),
        "missing_by_field": {
            key: sum(key in a["missing_fields"] for a in attempts)
            for key in ("model", "reasoning_effort", "host", "host_version", "guidance")
        },
        "source": "recorded_attempt_and_setup_receipt_declarations",
        "provider_data_read": False,
        "compliance_attested": False,
    }


def aggregate_validation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "invocations": sum(r["validation"]["invocations"] for r in rows),
        "completed": sum(r["validation"]["completed"] for r in rows),
        "indeterminate": sum(r["validation"]["indeterminate"] for r in rows),
        "historical_machine_passed_runs": sum(
            r["validation"]["historical_machine_passed_runs"] for r in rows
        ),
        "current_applicability": {
            status: sum(r["validation"]["current_applicability"][status] for r in rows)
            for status in ("current", "stale", "unknown")
        },
        "landing_authority": "none",
    }


def aggregate_observations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    interventions = [r["human_interventions"] for r in rows]
    regressions = [r["criterion_regressions"] for r in rows]
    intervention_tasks = sum(r["observations"] > 0 for r in interventions)
    assessed_tasks = sum(r["assessed_criteria"] > 0 for r in regressions)
    return {
        "human_interventions": {
            "value": (
                sum(r["value"] for r in interventions if r["value"] is not None)
                if intervention_tasks
                else None
            ),
            "unit": "count",
            "source": "caller_declared",
            "observations": sum(r["observations"] for r in interventions),
            "tasks_with_observations": intervention_tasks,
            "tasks_without_observations": len(rows) - intervention_tasks,
            "coverage": "recorded_observations_only",
        },
        "criterion_regressions": {
            "distinct_criteria": (
                sum(r["value"] for r in regressions if r["value"] is not None)
                if assessed_tasks
                else None
            ),
            "tasks_with_regressions": sum(bool(r["value"]) for r in regressions),
            "transition_observations": sum(
                r["transition_observations"] for r in regressions
            ),
            "defined_criteria": sum(r["criterion_population"] for r in regressions),
            "assessed_criteria": sum(r["assessed_criteria"] for r in regressions),
            "tasks_with_assessments": assessed_tasks,
            "tasks_without_assessments": len(rows) - assessed_tasks,
            "source": "caller_recorded_assessment_corrections",
            "coverage": "recorded_assessments_only",
        },
    }


def completion_distribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return distribution(
        [
            r["completion_elapsed_ms"]
            for r in rows
            if r["completion_elapsed_ms"] is not None
        ],
        population=len(rows),
        unit="ms",
        source="runtime_timestamps_wall_clock",
    )


def outcome_report(
    profile: RepoProfile,
    *,
    since: Any = None,
    until: Any = None,
    task_id: str | None = None,
    include_tasks: bool = True,
) -> dict[str, Any]:
    with locked_evidence(profile) as (state, _, events):
        snapshot, evidence = state, events
    return project_outcomes(
        profile,
        snapshot,
        evidence,
        since=since,
        until=until,
        task_id=task_id,
        include_tasks=include_tasks,
    )


def project_outcomes(
    profile: RepoProfile,
    state: RuntimeState,
    events: tuple[EvidenceEvent, ...],
    *,
    since: Any = None,
    until: Any = None,
    task_id: str | None = None,
    include_tasks: bool = True,
) -> dict[str, Any]:
    by_task: dict[str, list[EvidenceEvent]] = defaultdict(list)
    for event in events:
        by_task[event.task_id].append(event)
    rows: list[dict[str, Any]] = []
    cohorts: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for task in state.tasks:
        if task_id is not None and task.task_id != task_id:
            continue
        created = parse_iso(task.created_at)
        if since is not None and (created is None or created < since):
            continue
        if until is not None and (created is None or created >= until):
            continue
        task_events = by_task[task.task_id]
        definition_event = next((e for e in task_events if e.kind == DEFINITION), None)
        definition = (
            OutcomeDefinition.parse(definition_event.data) if definition_event else None
        )
        latest = task.attempts[-1] if task.attempts else None
        heads: dict[str, EvidenceEvent] = {}
        regressions = 0
        regressed_criteria: set[str] = set()
        for event in task_events:
            if event.kind != ASSESSMENT:
                continue
            assessment = Assessment.parse(event.data["assessment"])
            previous = heads.get(assessment.criterion_id)
            if (
                previous is not None
                and previous.data["assessment"]["result"] == "met"
                and assessment.result == "not_met"
            ):
                regressions += 1
                regressed_criteria.add(assessment.criterion_id)
            heads[assessment.criterion_id] = event
        tree = _artifact_tree(profile, latest) if heads else None
        criteria: list[dict[str, Any]] = []
        for criterion in definition.criteria if definition else ():
            event = heads.get(criterion.id)
            assessment = Assessment.parse(event.data["assessment"]) if event else None
            binding_status = (
                "unknown"
                if tree is None
                else (
                    "current"
                    if event
                    and latest
                    and event.attempt_id == latest.attempt_id
                    and event.data["source_tree"] == tree
                    else "stale"
                )
            )
            criteria.append(
                {
                    **asdict(criterion),
                    "assessment": assessment.to_dict() if assessment else None,
                    "assessment_event_id": event.event_id if event else None,
                    "source_tree": event.data["source_tree"] if event else None,
                    "artifact_applicability": binding_status if event else "unknown",
                    "result": assessment.result if assessment else "not_assessed",
                    "host_refs_authority": "caller_declared_unverified_locators",
                    "host_refs_fetched": False,
                    "reviewer_identity_authenticated": False,
                }
            )
        required = [c for c in criteria if c["required"]]
        criteria_result, criteria_provenance, assessment_coverage = _criterion_status(
            criteria, task_events, empty="not_assessed"
        )
        outcome, outcome_provenance, outcome_coverage = _criterion_status(
            [c for c in criteria if c["kind"] == "outcome"],
            task_events, empty="not_assessed",
        )
        compliance, compliance_provenance, compliance_coverage = _criterion_status(
            [c for c in criteria if c["kind"] == "compliance"],
            task_events, empty="not_defined",
        )
        contexts = [_attempt_context(attempt) for attempt in task.attempts]
        context_ids = sorted({context["context_sha256"] for context in contexts})
        execution_context = {
            "attempts": contexts,
            "cohort_sha256": digest(context_ids),
            "context_sha256": context_ids[0] if len(context_ids) == 1 else None,
            "complete": bool(contexts) and all(c["complete"] for c in contexts),
            "mixed": len(context_ids) > 1,
            "source": "recorded_attempt_and_setup_receipt_declarations",
            "compliance_attested": False,
        }
        intents = {e.event_id: e for e in task_events if e.kind == VALIDATION_INTENT}
        receipts = [e for e in task_events if e.kind == VALIDATION_RESULT]
        receipt_attempt_ids = {receipt.attempt_id for receipt in receipts}
        observed_bindings = {
            attempt.attempt_id: observe_report_binding(profile, attempt)
            for attempt in task.attempts
            if attempt.attempt_id in receipt_attempt_ids
        }
        applicability_rows = []
        for receipt in receipts:
            binding, reason = observed_bindings[receipt.attempt_id]
            applicability = receipt_applicability(
                intents[receipt.data["intent_event_id"]], receipt, binding
            )
            if (
                applicability["status"] == "unknown"
                and applicability["missing_reason"] == "current_workspace_unavailable"
                and reason is not None
            ):
                applicability["missing_reason"] = reason
            applicability_rows.append(
                {
                    "event_id": receipt.event_id,
                    "historical_all_passed": receipt.data["run"]["all_passed"],
                    "applicability": applicability,
                }
            )
        applicability_counts = Counter(
            row["applicability"]["status"] for row in applicability_rows
        )
        command_sets = sorted(
            {e.data["binding"]["command_set_sha256"] for e in intents.values()}
        )
        environment_ids = sorted(
            {
                e.data["binding"]["environment_sha256"]
                for e in intents.values()
                if e.data["binding"]["environment"]["runtime_identity"]["kind"]
                != "unknown"
            }
        )
        if any(
            e.data["binding"]["environment"]["runtime_identity"]["kind"] == "unknown"
            for e in intents.values()
        ):
            environment_ids = []
        environment_coverage = sorted(
            {e.data["binding"]["environment"]["coverage"] for e in intents.values()}
        )
        interventions = [e.data["value"] for e in task_events if e.kind == INTERVENTION]
        elapsed = [v for a in task.attempts if (v := _elapsed(a)) is not None]
        waits = []
        for prior, current in zip(task.attempts, task.attempts[1:]):
            end, start = parse_iso(prior.ended_at), parse_iso(current.started_at)
            if end is not None and start is not None and start >= end:
                waits.append(int((start - end).total_seconds() * 1000))
        setup = []
        for attempt in task.attempts:
            setup_receipt = attempt.setup_receipt or {}
            if "setup_measurement" in setup_receipt:
                timing = SetupMeasurement.parse(setup_receipt["setup_measurement"])
                if timing.value is not None:
                    setup.append(timing.value)
        validation_ms = [
            sum(c["elapsed_ms"] for c in e.data["run"]["results"]) for e in receipts
        ]
        phases = {}
        for phase in ("landing", "recovery"):
            values = [
                e.data["value"]
                for e in task_events
                if e.kind == PHASE and e.data["phase"] == phase
            ]
            phases[phase] = {
                **distribution(
                    values,
                    population=None,
                    unit="ms",
                    source="first_completed_mutating_invocation_for_phase",
                ),
                "coverage": "completed_mutating_invocations_only",
                "total_invocations": None,
                "failed_or_partial_invocations": None,
                "missing_reason": "unmeasured_invocation_count_unknown",
            }
        row = {
            "task_id": task.task_id,
            "task_status": task.status,
            "task_class": definition.task_class if definition else None,
            "definition_sha256": definition.sha256 if definition else None,
            "definition": definition.to_dict() if definition else None,
            "execution_status": latest.status if latest else None,
            "integration": {
                "status": "landed" if latest and latest.landed_commit else "not_landed",
                "source_commit": latest.commit if latest else None,
                "landed_commit": latest.landed_commit if latest else None,
                "target_branch": latest.target_branch if latest else None,
            },
            "outcome": outcome,
            "outcome_authority": "caller_recorded_assessments",
            "outcome_provenance": outcome_provenance,
            "compliance": compliance,
            "compliance_provenance": compliance_provenance,
            "compliance_authority": "caller_recorded_assessments",
            "criteria_result": criteria_result,
            "criteria_provenance": criteria_provenance,
            "assessment_coverage": assessment_coverage,
            "outcome_assessment_coverage": outcome_coverage,
            "compliance_assessment_coverage": compliance_coverage,
            "execution_context": execution_context,
            "criteria": criteria,
            "required_criteria": len(required),
            "assessed_required_criteria": sum(
                c["assessment"] is not None and c["result"] != "not_assessed"
                for c in required
            ),
            "attempts": len(task.attempts),
            "successor_attempts": max(0, len(task.attempts) - 1),
            "human_interventions": {
                "value": sum(interventions) if interventions else None,
                "unit": "count",
                "source": "caller_declared",
                "observations": len(interventions),
                "missing_reason": None if interventions else "not_recorded",
                "coverage": "recorded_observations_only",
            },
            "criterion_regressions": {
                "value": len(regressed_criteria) if heads else None,
                "transition_observations": regressions,
                "criterion_population": len(definition.criteria) if definition else 0,
                "assessed_criteria": len(heads),
                "source": "distinct_criteria_with_explicit_met_to_not_met_corrections",
                "coverage": "recorded_assessments_only",
            },
            "validation": {
                "invocations": len(intents),
                "completed": len(receipts),
                "indeterminate": len(intents) - len(receipts),
                "command_set_sha256": command_sets,
                "environment_sha256": environment_ids,
                "environment_coverage": environment_coverage,
                "caller_declared_rows": sum(len(a.validations) for a in task.attempts),
                "historical_machine_passed_runs": sum(
                    e.data["run"]["all_passed"] for e in receipts
                ),
                "current_applicability": {
                    status: applicability_counts[status]
                    for status in ("current", "stale", "unknown")
                },
                "receipt_applicability": applicability_rows,
                "applicability_coverage": "clean active workspaces reobserved once per attempt; dirty or terminal environments unknown",
                "landing_authority": "none",
            },
            "timing": {
                "attempt_elapsed": distribution(
                    elapsed,
                    population=len(task.attempts),
                    unit="ms",
                    source="runtime_timestamps_wall_clock",
                ),
                "inter_attempt_wait": distribution(
                    waits,
                    population=max(0, len(task.attempts) - 1),
                    unit="ms",
                    source="runtime_timestamp_gaps_not_human_wait",
                ),
                "setup": distribution(
                    setup,
                    population=len(task.attempts),
                    unit="ms",
                    source="monotonic_clock",
                ),
                "validation_commands": distribution(
                    validation_ms,
                    population=len(intents),
                    unit="ms",
                    source="monotonic_clock_sum_of_commands_per_invocation",
                ),
                "landing": phases["landing"],
                "recovery": phases["recovery"],
            },
        }
        # This is elapsed completion across attempts, not sum of overlapping
        # command/setup phases and not a claim about active working time.
        completion = None
        if (
            task.status in {"done", "canceled"}
            and task.attempts
            and latest
            and latest.ended_at
        ):
            first, last = parse_iso(task.attempts[0].started_at), parse_iso(
                latest.ended_at
            )
            if first is not None and last is not None and last >= first:
                completion = int((last - first).total_seconds() * 1000)
        row["completion_elapsed_ms"] = completion
        rows.append(row)
        key = (
            (
                definition.task_class,
                definition.sha256,
                command_sets[0] if len(command_sets) == 1 else None,
                environment_ids[0] if len(environment_ids) == 1 else None,
                environment_coverage[0] if len(environment_coverage) == 1 else None,
                execution_context["cohort_sha256"],
            )
            if definition
            else ("unknown", "unknown", None, None, None, execution_context["cohort_sha256"])
        )
        cohorts[key].append(row)
    cohort_rows = []
    for (
        task_class,
        definition_sha,
        command_set,
        environment_id,
        coverage,
        context_sha,
    ), members in sorted(cohorts.items(), key=lambda pair: str(pair[0])):
        eligible = [
            r for r in members
            if r["task_status"] == "done"
            and r["outcome"] == "met"
            and r["criteria_result"] == "met"
        ]
        status_outcome = defaultdict(list)
        for member in members:
            status_outcome[
                (member["task_status"], member["outcome"], member["compliance"])
            ].append(member)
        cohort_rows.append(
            {
                "task_class": task_class,
                "definition_sha256": definition_sha,
                "command_set_sha256": command_set,
                "environment_sha256": environment_id,
                "environment_coverage": coverage,
                "execution_context_cohort_sha256": context_sha,
                "execution_contexts": [
                    {key: value for key, value in context.items() if key != "attempt_id"}
                    for _, context in sorted({
                        context["context_sha256"]: context
                        for member in members
                        for context in member["execution_context"]["attempts"]
                    }.items())
                ],
                "comparable": definition_sha != "unknown"
                and command_set is not None
                and environment_id is not None
                and coverage is not None
                and all(
                    m["execution_context"]["complete"]
                    and not m["execution_context"]["mixed"]
                    for m in members
                ),
                "task_count": len(members),
                "attempt_count": sum(r["attempts"] for r in members),
                "outcomes": dict(Counter(r["outcome"] for r in members)),
                "compliance": dict(Counter(r["compliance"] for r in members)),
                "assessment_coverage": aggregate_quality(members),
                "outcome_assessment_coverage": aggregate_quality(members, kind="outcome"),
                "compliance_assessment_coverage": aggregate_quality(members, kind="compliance"),
                "execution_context_coverage": aggregate_context(members),
                "validation_coverage": aggregate_validation(members),
                "observations": aggregate_observations(members),
                "all_terminal_completion_elapsed": completion_distribution(members),
                "criteria_met_completion": {
                    "eligible_rule": (
                        "task_status=done and outcome=met and all required "
                        "outcome/compliance criteria currently met"
                    ),
                    "eligible_tasks": len(eligible),
                    "cohort_tasks": len(members),
                    "outcome_authority": "caller_recorded_assessments",
                    **completion_distribution(eligible),
                },
                "completion_by_status_and_outcome": [
                    {
                        "task_status": status,
                        "outcome": outcome,
                        "compliance": compliance,
                        "task_count": len(selected),
                        "completion_elapsed": completion_distribution(selected),
                    }
                    for (status, outcome, compliance), selected
                    in sorted(status_outcome.items())
                ],
            }
        )
    return {
        "schema_version": 1,
        "scope": "tasks_created_in_window_including_all_their_attempts_and_evidence",
        "task_count": len(rows),
        "definitions_missing": sum(r["definition"] is None for r in rows),
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
        "compliance": dict(Counter(r["compliance"] for r in rows)),
        "assessment_coverage": aggregate_quality(rows),
        "outcome_assessment_coverage": aggregate_quality(rows, kind="outcome"),
        "compliance_assessment_coverage": aggregate_quality(rows, kind="compliance"),
        "execution_context_coverage": aggregate_context(rows),
        "validation_coverage": aggregate_validation(rows),
        "observations": aggregate_observations(rows),
        "tasks": rows if include_tasks else [],
        "task_rows_included": include_tasks,
        "cohorts": cohort_rows,
        "landing_authority": LANDING_AUTHORITY,
        "reviewer_identity_authenticated": False,
        "provider_data_read": False,
    }
