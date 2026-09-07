"""Bounded timings for completed mutating task invocations.

Measurement failure is explicit in the returned result. It must not replace an
already-established lifecycle result or weaken its exact recovery capability.
"""

from __future__ import annotations

from dataclasses import replace
from functools import wraps
import time
from typing import Any, Callable

from blackdog.evidence import _append, _attempt, locked_evidence
from blackdog.lifecycle import OperationResult
from blackdog_core.evidence import (
    PHASE,
    PhaseMeasurement,
    event_identity,
)
from blackdog_core.profile import RepoProfile
from blackdog_core.state import StoreError


def measure_task_phase(phase: str) -> Callable:
    def decorate(
        operation: Callable[..., OperationResult],
    ) -> Callable[..., OperationResult]:
        @wraps(operation)
        def measured(
            profile: RepoProfile, *args: Any, **kwargs: Any
        ) -> OperationResult:
            started_ns = time.monotonic_ns()
            result = operation(profile, *args, **kwargs)
            elapsed_ms = (time.monotonic_ns() - started_ns) // 1_000_000
            payload = result.to_dict()
            task_id = payload.get("task_id")
            attempt_id = payload.get("attempt_id")
            if (
                not result.mutation_started
                or not result.mutation_completed
                or not task_id
                or not attempt_id
            ):
                return result
            measurement_id = f"{phase}.{result.mutation_phase}"
            observation = PhaseMeasurement(
                measurement_id,
                phase,
                "ms",
                elapsed_ms,
                "monotonic_clock",
                "first_completed_mutating_invocation_for_phase",
            )
            try:
                with locked_evidence(profile) as (state, rows, events):
                    _attempt(state, task_id, attempt_id)
                    event_id = event_identity(
                        PHASE, task_id, attempt_id, measurement_id
                    )
                    existing = next((e for e in events if e.event_id == event_id), None)
                    data = existing.data if existing else observation.to_dict()
                    recorded = _append(
                        profile,
                        state=state,
                        rows=rows,
                        kind=PHASE,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        actor="blackdog",
                        request_id=measurement_id,
                        data=data,
                    )
                    metric = {
                        "status": recorded["status"],
                        "event_id": event_id,
                        **data,
                    }
            except (StoreError, OSError):
                metric = {
                    "status": "missing",
                    "phase": phase,
                    "missing_reason": "evidence_write_failed",
                }
            return replace(
                result,
                legacy_payload={**result.legacy_payload, "phase_measurement": metric},
            )

        return measured

    return decorate
