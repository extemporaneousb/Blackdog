"""Derived read-only views over canonical Blackdog runtime artifacts."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .backlog import (
    RuntimeArtifacts,
    build_plan_snapshot,
    build_runtime_snapshot as _build_legacy_runtime_snapshot,
    build_runtime_summary,
    build_workset_snapshot,
    load_runtime_artifacts,
    render_plan_text,
    summary_open_messages,
)
from .runtime_model import (
    RuntimeModel,
    load_runtime_model,
    project_runtime_model,
)
from .state import load_events, load_inbox, load_task_results


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def runtime_model_snapshot(model: RuntimeModel) -> dict[str, Any]:
    payload = _jsonable(model)
    if not isinstance(payload, dict):
        raise TypeError("runtime model payload must serialize to a dict")
    return payload


def build_runtime_snapshot(
    profile,
    snapshot,
    state,
    *,
    messages: list[dict[str, Any]] | None = None,
    results: list[dict[str, Any]] | None = None,
    allow_high_risk: bool = False,
) -> dict[str, Any]:
    message_rows = messages if messages is not None else load_inbox(profile.paths)
    result_rows = results if results is not None else load_task_results(profile.paths)
    base = _build_legacy_runtime_snapshot(
        profile,
        snapshot,
        state,
        messages=message_rows,
        results=result_rows,
        allow_high_risk=allow_high_risk,
    )
    model = project_runtime_model(
        profile,
        snapshot,
        state,
        events=load_events(profile.paths),
        inbox=message_rows,
        results=result_rows,
        allow_high_risk=allow_high_risk,
        execution_mode="snapshot",
    )
    base["runtime_model"] = runtime_model_snapshot(model)
    base["task_attempts"] = base["runtime_model"]["task_attempts"]
    base["wait_conditions"] = base["runtime_model"]["wait_conditions"]
    base["control_messages"] = base["runtime_model"]["control_messages"]
    base["workset_execution"] = base["runtime_model"]["workset_execution"]
    base["prompt_receipts"] = base["runtime_model"]["prompt_receipts"]
    return base


__all__ = [
    "RuntimeArtifacts",
    "RuntimeModel",
    "build_plan_snapshot",
    "build_runtime_snapshot",
    "build_runtime_summary",
    "build_workset_snapshot",
    "load_runtime_artifacts",
    "load_runtime_model",
    "render_plan_text",
    "runtime_model_snapshot",
    "summary_open_messages",
]
