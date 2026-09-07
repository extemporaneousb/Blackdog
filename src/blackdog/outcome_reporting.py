"""Outcome and cost reports derived solely from typed runtime and event evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
import math
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


def aggregate_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sources = ("caller_declared", "reviewer_asserted")
    return {
        "required_criteria": sum(
            r["assessment_coverage"]["required_criteria"] for r in rows
        ),
        "current_assessed_required_criteria": sum(
            r["assessment_coverage"]["current_assessed_required_criteria"] for r in rows
        ),
        "current_required_by_provenance": {
            source: sum(
                r["assessment_coverage"]["current_required_by_provenance"][source]
                for r in rows
            )
            for source in sources
        },
        "recorded_assessments_by_provenance": {
            source: sum(
                r["assessment_coverage"]["recorded_assessments_by_provenance"][source]
                for r in rows
            )
            for source in sources
        },
        "outcome_provenance_counts": dict(
            Counter(r["outcome_provenance"] for r in rows)
        ),
        "reviewer_identity_authenticated": False,
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
    cohorts: dict[
        tuple[str, str, str | None, str | None, str | None], list[dict[str, Any]]
    ] = defaultdict(list)
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
                    "reviewer_identity_authenticated": False,
                }
            )
        required = [c for c in criteria if c["required"]]
        if not required or any(
            c["assessment"] is None
            or c["artifact_applicability"] != "current"
            or c["result"] == "not_assessed"
            for c in required
        ):
            outcome = "not_assessed"
        elif any(c["result"] == "not_met" for c in required):
            outcome = "not_met"
        else:
            outcome = "met"
        current_required = [
            c
            for c in required
            if c["assessment"] is not None
            and c["result"] != "not_assessed"
            and c["artifact_applicability"] == "current"
        ]
        provenance = Counter(c["assessment"]["provenance"] for c in current_required)
        recorded_provenance = Counter(
            e.data["assessment"]["provenance"]
            for e in task_events
            if e.kind == ASSESSMENT
        )
        outcome_provenance = (
            "incomplete"
            if len(current_required) != len(required) or not required
            else next(iter(provenance)) if len(provenance) == 1 else "mixed"
        )
        assessment_coverage = {
            "required_criteria": len(required),
            "current_assessed_required_criteria": len(current_required),
            "current_required_by_provenance": {
                source: provenance[source]
                for source in ("caller_declared", "reviewer_asserted")
            },
            "recorded_assessments_by_provenance": {
                source: recorded_provenance[source]
                for source in ("caller_declared", "reviewer_asserted")
            },
        }
        intents = {e.event_id: e for e in task_events if e.kind == VALIDATION_INTENT}
        receipts = [e for e in task_events if e.kind == VALIDATION_RESULT]
        observed_bindings = {
            attempt.attempt_id: observe_report_binding(profile, attempt)
            for attempt in task.attempts
            if any(receipt.attempt_id == attempt.attempt_id for receipt in receipts)
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
            "assessment_coverage": assessment_coverage,
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
            )
            if definition
            else ("unknown", "unknown", None, None, None)
        )
        cohorts[key].append(row)
    cohort_rows = []
    for (
        task_class,
        definition_sha,
        command_set,
        environment_id,
        coverage,
    ), members in sorted(cohorts.items(), key=lambda pair: str(pair[0])):
        eligible = [
            r for r in members if r["task_status"] == "done" and r["outcome"] == "met"
        ]
        status_outcome = defaultdict(list)
        for member in members:
            status_outcome[(member["task_status"], member["outcome"])].append(member)
        cohort_rows.append(
            {
                "task_class": task_class,
                "definition_sha256": definition_sha,
                "command_set_sha256": command_set,
                "environment_sha256": environment_id,
                "environment_coverage": coverage,
                "comparable": definition_sha != "unknown"
                and command_set is not None
                and environment_id is not None
                and coverage is not None,
                "task_count": len(members),
                "attempt_count": sum(r["attempts"] for r in members),
                "outcomes": dict(Counter(r["outcome"] for r in members)),
                "assessment_coverage": aggregate_quality(members),
                "validation_coverage": aggregate_validation(members),
                "observations": aggregate_observations(members),
                "all_terminal_completion_elapsed": completion_distribution(members),
                "criteria_met_completion": {
                    "eligible_rule": "task_status=done and outcome=met",
                    "eligible_tasks": len(eligible),
                    "cohort_tasks": len(members),
                    "outcome_authority": "caller_recorded_assessments",
                    **completion_distribution(eligible),
                },
                "completion_by_status_and_outcome": [
                    {
                        "task_status": status,
                        "outcome": outcome,
                        "task_count": len(selected),
                        "completion_elapsed": completion_distribution(selected),
                    }
                    for (status, outcome), selected in sorted(status_outcome.items())
                ],
            }
        )
    return {
        "schema_version": 1,
        "scope": "tasks_created_in_window_including_all_their_attempts_and_evidence",
        "task_count": len(rows),
        "definitions_missing": sum(r["definition"] is None for r in rows),
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
        "assessment_coverage": aggregate_quality(rows),
        "validation_coverage": aggregate_validation(rows),
        "observations": aggregate_observations(rows),
        "tasks": rows if include_tasks else [],
        "task_rows_included": include_tasks,
        "cohorts": cohort_rows,
        "landing_authority": LANDING_AUTHORITY,
        "reviewer_identity_authenticated": False,
        "provider_data_read": False,
    }
