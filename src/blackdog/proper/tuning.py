from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..core import backlog as core_backlog
from ..core.config import Profile


PROMPT_COMPLEXITY_PROFILES = {
    "low": {
        "label": "Low",
        "summary": "Keep the prompt lean and execution-biased for bounded work.",
        "doc_limit": 2,
        "include_why": False,
        "include_evidence": False,
        "include_task_shaping": False,
        "include_validation": True,
        "include_prompt_tuning": False,
    },
    "medium": {
        "label": "Medium",
        "summary": "Carry enough repo context to avoid avoidable retries without bloating the prompt.",
        "doc_limit": 4,
        "include_why": True,
        "include_evidence": True,
        "include_task_shaping": True,
        "include_validation": True,
        "include_prompt_tuning": True,
    },
    "high": {
        "label": "High",
        "summary": "Front-load repo policy, routed docs, validation, and tuning context for multi-surface work.",
        "doc_limit": 8,
        "include_why": True,
        "include_evidence": True,
        "include_task_shaping": True,
        "include_validation": True,
        "include_prompt_tuning": True,
    },
}


def _unique_ordered(items: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in items:
        item = str(raw).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def build_prompt_profiles(profile: Profile, *, analysis: dict[str, Any]) -> dict[str, Any]:
    try:
        skill_doc_paths = [
            str((profile.paths.skill_dir / "SKILL.md").resolve().relative_to(profile.paths.project_root.resolve())),
            str(
                (profile.paths.skill_dir / "agents" / "openai.yaml")
                .resolve()
                .relative_to(profile.paths.project_root.resolve())
            ),
        ]
    except ValueError:
        skill_doc_paths = [
            str(profile.paths.skill_dir / "SKILL.md"),
            str(profile.paths.skill_dir / "agents" / "openai.yaml"),
        ]
    base_docs = _unique_ordered(
        [
            "AGENTS.md",
            *profile.doc_routing_defaults,
            *skill_doc_paths,
            "docs/INTEGRATION.md",
        ]
    )
    validation_commands = list(profile.validation_commands)
    focus = analysis["recommendation"]["focus"]
    focus_summary = analysis["recommendation"]["summary"]
    calibration = analysis.get("calibration", {})
    by_effort = calibration.get("by_effort", {})
    calibrated_defaults = {
        effort: {
            "estimated_elapsed_minutes": stats.get("seeded_elapsed_minutes"),
            "estimated_active_minutes": stats.get("seeded_active_minutes"),
            "estimated_validation_minutes": stats.get("seeded_validation_minutes"),
            "sample_size": stats.get("completed_sample_size", 0),
        }
        for effort, stats in by_effort.items()
    }
    profiles: dict[str, Any] = {}
    for name, config in PROMPT_COMPLEXITY_PROFILES.items():
        routed_docs = base_docs[: config["doc_limit"]]
        sections = ["Goal", "Repo contract", "Target paths"]
        if config["include_why"]:
            sections.append("Why it matters")
        if config["include_evidence"]:
            sections.append("Evidence")
        if config["include_task_shaping"]:
            sections.append("Task-shaping expectations")
        if config["include_validation"]:
            sections.append("Validation")
        if config["include_prompt_tuning"]:
            sections.append("Prompt-tuning focus")
        profiles[name] = {
            "complexity": name,
            "label": config["label"],
            "summary": config["summary"],
            "routed_docs": routed_docs,
            "validation_commands": validation_commands if config["include_validation"] else [],
            "recommended_sections": sections,
            "focus": {
                "category": focus,
                "summary": focus_summary,
            },
            "prompt_strategy": {
                "keep_context_compact": bool(name == "low"),
                "require_explicit_estimates": focus in {"task_shaping_coverage", "task_time_calibration"},
                "require_result_runtime_capture": True,
                "prefer_repo_routed_docs": True,
            },
            "calibrated_task_shape_defaults": calibrated_defaults,
            "context_budget": {
                "doc_limit": config["doc_limit"],
                "include_task_shaping": config["include_task_shaping"],
                "include_validation": config["include_validation"],
            },
        }
    return profiles


def build_prompt_improvement(profile: Profile, *, prompt_text: str, complexity: str, analysis: dict[str, Any]) -> dict[str, Any]:
    if complexity not in PROMPT_COMPLEXITY_PROFILES:
        raise core_backlog.BacklogError(f"Unsupported prompt complexity: {complexity}")
    prompt = prompt_text.strip()
    if not prompt:
        raise core_backlog.BacklogError("Prompt text is required for prompt tuning.")
    profiles = build_prompt_profiles(profile, analysis=analysis)
    selected = profiles[complexity]
    lines = [
        f"You are working in the {profile.project_name} repo under the local Blackdog contract.",
        "Use the repo-local Blackdog CLI when available and keep kept implementation changes in a branch-backed task worktree.",
        "",
        f"Complexity profile: {selected['label']}",
        selected["summary"],
        "",
        "Repo-specific operating rules:",
        "- Review the routed docs before kept edits.",
        "- If implementation is needed, start with `blackdog worktree preflight` and move to a task worktree before editing.",
        "- Prefer the local backlog/runtime contract over ad hoc coordination.",
    ]
    if selected["routed_docs"]:
        lines.extend(["", "Routed docs:"])
        lines.extend(f"- {item}" for item in selected["routed_docs"])
    if selected["validation_commands"]:
        lines.extend(["", "Validation defaults:"])
        lines.extend(f"- {item}" for item in selected["validation_commands"])
    calibrated_defaults = selected.get("calibrated_task_shape_defaults") or {}
    if calibrated_defaults:
        lines.extend(["", "Calibrated task-shaping defaults by effort:"])
        for effort in ("S", "M", "L"):
            defaults = calibrated_defaults.get(effort) or {}
            elapsed = defaults.get("estimated_elapsed_minutes")
            active = defaults.get("estimated_active_minutes")
            validation = defaults.get("estimated_validation_minutes")
            sample_size = defaults.get("sample_size")
            if elapsed is None or active is None:
                continue
            lines.append(
                f"- {effort}: elapsed {elapsed}m, active {active}m, validation {validation}m, sample size {sample_size}"
            )
    lines.extend(["", "Required prompt sections:"])
    lines.extend(f"- {item}" for item in selected["recommended_sections"])
    lines.extend(
        [
            "",
            "Prompt-tuning focus:",
            f"- {selected['focus']['category']}: {selected['focus']['summary']}",
            f"- Require explicit estimate and runtime capture: {selected['prompt_strategy']['require_explicit_estimates']}",
            "",
            "User request:",
            prompt,
        ]
    )
    improved_prompt = "\n".join(lines).strip()
    return {
        "project_name": profile.project_name,
        "complexity": complexity,
        "original_prompt": prompt,
        "improved_prompt": improved_prompt,
        "prompt_profile": selected,
        "tune_focus": analysis["recommendation"],
        "tuning_categories": analysis["categories"],
        "context_budget": {
            "doc_limit": selected["context_budget"]["doc_limit"],
            "validation_count": len(selected["validation_commands"]),
            "routed_doc_count": len(selected["routed_docs"]),
            "packet_bytes": len(improved_prompt.encode("utf-8")),
        },
    }


def build_tune_analysis(profile: Profile) -> dict[str, Any]:
    with core_backlog.locked_path(profile.paths.backlog_file):
        snapshot = core_backlog.load_backlog(profile.paths, profile)
    state = core_backlog.sync_state_for_backlog(core_backlog.load_state(profile.paths.state_file), snapshot)
    events = core_backlog.load_events(profile.paths)
    results = core_backlog.load_task_results(profile.paths)
    runtime_rows = core_backlog._task_runtime_rows(snapshot=snapshot, state=state, events=events, results=results)
    calibration = core_backlog._build_task_shaping_calibration(
        snapshot=snapshot,
        state=state,
        events=events,
        results=results,
    )

    result_files = len(results)
    tasks_with_recorded_compute = sum(1 for row in runtime_rows.values() if row.get("actual_task_seconds") is not None)
    completed_tasks_with_recorded_compute = sum(
        1
        for task_id, row in runtime_rows.items()
        if row.get("actual_task_seconds") is not None and core_backlog.task_done(task_id, state)
    )
    results_with_actual_task_telemetry = sum(1 for row in results if core_backlog._result_has_actual_task_telemetry(row))
    total_task_minutes = sum(int(row.get("actual_task_minutes") or 0) for row in runtime_rows.values())
    completed_task_minutes = [
        int(row.get("actual_task_minutes") or 0)
        for task_id, row in runtime_rows.items()
        if row.get("actual_task_minutes") is not None and core_backlog.task_done(task_id, state)
    ]
    average_completed_task_minutes = (
        round(sum(completed_task_minutes) / len(completed_task_minutes)) if completed_task_minutes else None
    )

    sample_rows: list[dict[str, Any]] = []
    for task in snapshot.tasks.values():
        runtime = runtime_rows.get(task.id)
        if runtime is None:
            continue
        actual_minutes = runtime.get("actual_task_minutes")
        estimate_minutes = runtime.get("estimated_active_minutes")
        if estimate_minutes is None:
            estimate_minutes = runtime.get("estimated_elapsed_minutes")
        if actual_minutes is None or estimate_minutes is None:
            continue
        delta_minutes = int(actual_minutes) - int(estimate_minutes)
        sample_rows.append(
            {
                "task_id": task.id,
                "title": task.title,
                "estimate_minutes": int(estimate_minutes),
                "actual_minutes": int(actual_minutes),
                "delta_minutes": delta_minutes,
            }
        )

    mean_absolute_error = (
        round(sum(abs(row["delta_minutes"]) for row in sample_rows) / len(sample_rows), 2) if sample_rows else None
    )
    underestimated_tasks = sum(1 for row in sample_rows if row["delta_minutes"] > 0)
    overestimated_tasks = sum(1 for row in sample_rows if row["delta_minutes"] < 0)
    retry_total = sum(int(row.get("actual_retry_count") or 0) for row in runtime_rows.values())
    reclaim_total = sum(int(row.get("actual_reclaim_count") or 0) for row in runtime_rows.values())
    landing_failures = sum(int(row.get("landing_failures") or 0) for row in runtime_rows.values())
    context_rows = [
        core_backlog._task_context_metrics(task, runtime_rows.get(task.id, core_backlog._task_runtime_row(task.id)))
        for task in snapshot.tasks.values()
    ]
    completed_context_rows = [
        core_backlog._task_context_metrics(task, runtime_rows.get(task.id, core_backlog._task_runtime_row(task.id)))
        for task in snapshot.tasks.values()
        if core_backlog.task_done(task.id, state)
    ]

    def _average(values: list[int | float | None]) -> float | None:
        filtered = [float(value) for value in values if value is not None]
        if not filtered:
            return None
        return round(sum(filtered) / len(filtered), 2)

    coverage_gaps: list[str] = []
    if tasks_with_recorded_compute and len(sample_rows) < max(5, tasks_with_recorded_compute // 4):
        coverage_gaps.append("Most completed tasks still lack both an estimate snapshot and a comparable actual task-time sample.")
    if result_files and results_with_actual_task_telemetry < max(5, result_files // 4):
        coverage_gaps.append("Most structured results still lack explicit actual task-time telemetry fields.")
    calibration_ready = len(sample_rows) >= max(3, tasks_with_recorded_compute // 5) if tasks_with_recorded_compute else False

    enough_data = bool(tasks_with_recorded_compute or retry_total or landing_failures)
    if not enough_data:
        recommendation = {
            "focus": "collect_task_time_history",
            "summary": "Collect claim-derived task-time history before attempting backlog tuning.",
        }
    elif coverage_gaps:
        recommendation = {
            "focus": "task_shaping_coverage",
            "summary": "Increase estimate and actual task-time coverage so tune can compare more completed work with the same contract.",
        }
    elif not calibration_ready:
        recommendation = {
            "focus": "task_time_calibration",
            "summary": "Coverage is improving, but Blackdog still needs more comparable estimate-vs-actual samples before tightening prompt defaults.",
        }
    elif mean_absolute_error is not None and mean_absolute_error >= 10:
        recommendation = {
            "focus": "task_time_calibration",
            "summary": "Recorded task time is diverging from stored estimates; recalibrate active-minute defaults against the task history.",
        }
    elif landing_failures:
        recommendation = {
            "focus": "landing_failures",
            "summary": "Tune the WTAM flow around repeated landing failures before expanding more parallel work.",
        }
    elif retry_total:
        recommendation = {
            "focus": "retry_pressure",
            "summary": "Tune task boundaries and queue sequencing to reduce repeat attempts on the same task ids.",
        }
    else:
        recommendation = {
            "focus": "backlog_health",
            "summary": "Task-time history is stable enough to review for the next smaller calibration win.",
        }

    categories = {
        "time": {
            "tasks_with_recorded_compute": tasks_with_recorded_compute,
            "completed_tasks_with_recorded_compute": completed_tasks_with_recorded_compute,
            "estimated_time_samples": len(sample_rows),
            "total_task_minutes": total_task_minutes,
            "average_completed_task_minutes": average_completed_task_minutes,
            "timing_mean_absolute_error_minutes": mean_absolute_error,
            "underestimated_tasks": underestimated_tasks,
            "overestimated_tasks": overestimated_tasks,
        },
        "missteps": {
            "retry_total": retry_total,
            "reclaim_total": reclaim_total,
            "landing_failures": landing_failures,
            "tasks_with_missteps": sum(
                1
                for row in runtime_rows.values()
                if int(row.get("actual_retry_count") or 0)
                or int(row.get("actual_reclaim_count") or 0)
                or int(row.get("landing_failures") or 0)
            ),
        },
        "document_use_value": {
            "completed_tasks_with_routed_docs": sum(
                1 for row in completed_context_rows if int(row.get("context_doc_count") or 0) > 0
            ),
            "average_routed_doc_count": _average([row.get("context_doc_count") for row in completed_context_rows]),
            "positive_proxy_tasks": sum(
                1 for row in completed_context_rows if int(row.get("document_routing_value_score") or 0) >= 2
            ),
            "average_document_routing_value_score": _average(
                [row.get("document_routing_value_score") for row in completed_context_rows]
            ),
        },
        "context_efficiency": {
            "tasks_with_context_metrics": len(context_rows),
            "tasks_with_estimate_context": sum(
                1 for row in context_rows if int(row.get("context_estimate_field_count") or 0) > 0
            ),
            "average_context_packet_score": _average([row.get("context_packet_score") for row in context_rows]),
            "average_context_packet_bytes": _average([row.get("context_packet_bytes") for row in context_rows]),
            "average_context_efficiency_ratio": _average(
                [row.get("context_efficiency_ratio") for row in completed_context_rows]
            ),
        },
        "calibration": {
            "calibration_ready": calibration_ready,
            "default_active_ratio": calibration.get("default_active_ratio"),
            "effort_profiles": calibration.get("by_effort", {}),
        },
    }

    return {
        "enough_data": enough_data,
        "result_files": result_files,
        "tasks_with_recorded_compute": tasks_with_recorded_compute,
        "completed_tasks_with_recorded_compute": completed_tasks_with_recorded_compute,
        "results_with_actual_task_telemetry": results_with_actual_task_telemetry,
        "estimated_time_samples": len(sample_rows),
        "total_task_minutes": total_task_minutes,
        "average_completed_task_minutes": average_completed_task_minutes,
        "timing_mean_absolute_error_minutes": mean_absolute_error,
        "underestimated_tasks": underestimated_tasks,
        "overestimated_tasks": overestimated_tasks,
        "retry_total": retry_total,
        "reclaim_total": reclaim_total,
        "landing_failures": landing_failures,
        "calibration_ready": calibration_ready,
        "calibration": calibration,
        "coverage_gaps": coverage_gaps,
        "categories": categories,
        "recommendation": recommendation,
        "top_timing_outliers": sorted(sample_rows, key=lambda row: abs(row["delta_minutes"]), reverse=True)[:3],
    }


def _tune_task_payload(
    profile: Profile,
    *,
    analysis_builder: Callable[[Profile], dict[str, Any]] = build_tune_analysis,
) -> dict[str, Any]:
    analysis = analysis_builder(profile)
    paths = _unique_ordered(
        [
            str(profile.paths.backlog_file),
            str(profile.paths.state_file),
            str(profile.paths.events_file),
            str(profile.paths.inbox_file),
            str(profile.paths.results_dir),
            str(profile.paths.profile_file),
            str(profile.paths.html_file),
            str(profile.paths.skill_dir / "SKILL.md"),
            str(profile.paths.skill_dir / "agents/openai.yaml"),
        ]
    )
    try:
        skill_docs = [
            str((profile.paths.skill_dir / "SKILL.md").resolve().relative_to(profile.paths.project_root.resolve())),
            str(
                (profile.paths.skill_dir / "agents" / "openai.yaml")
                .resolve()
                .relative_to(profile.paths.project_root.resolve())
            ),
        ]
    except ValueError:
        skill_docs = [
            str(profile.paths.skill_dir / "SKILL.md"),
            str(profile.paths.skill_dir / "agents" / "openai.yaml"),
        ]
    docs = _unique_ordered(
        [
            "AGENTS.md",
            *skill_docs,
            "blackdog.toml",
            "docs/CLI.md",
            "docs/FILE_FORMATS.md",
            "docs/INTEGRATION.md",
            *profile.doc_routing_defaults,
        ]
    )
    evidence_bits = [
        f"{analysis['tasks_with_recorded_compute']} tasks with recorded task time",
        f"{analysis['estimated_time_samples']} estimate-vs-actual timing samples",
        f"{analysis['retry_total']} retries",
        f"{analysis['landing_failures']} landing failures",
        f"{analysis['results_with_actual_task_telemetry']}/{analysis['result_files']} results with explicit actual telemetry",
    ]
    safe_first_slice = [
        (
            "1. Review the recorded runtime history for "
            f"{analysis['tasks_with_recorded_compute']} task(s) with task-time data "
            f"and {analysis['estimated_time_samples']} comparable estimate samples."
        ),
    ]
    if analysis["coverage_gaps"]:
        safe_first_slice.append(
            "2. Confirm the current coverage gaps and make sure new result rows preserve both estimate snapshots and actual task time."
        )
    else:
        safe_first_slice.append(
            "2. Compare recorded task time against the current estimate contract and identify the largest recurrent mismatch."
        )
    safe_first_slice.append(
        "3. Draft one concrete tuning task focused on "
        f"{analysis['recommendation']['focus'].replace('_', ' ')}: {analysis['recommendation']['summary']}"
    )
    return {
        "title": "Auto-tune runtime contract and backlog health",
        "bucket": "skills",
        "priority": "P1",
        "risk": "low",
        "effort": "S",
        "paths": paths,
        "checks": list(profile.validation_commands),
        "docs": docs,
        "domains": list(profile.domains),
        "packages": [],
        "objective": "TUNING",
        "why": (
            "Analyze the recorded task-time history, runtime outcomes, and the local profile/skill contract, "
            "then propose the next highest-confidence tuning slice."
        ),
        "evidence": (
            "Runtime history currently shows "
            + ", ".join(evidence_bits)
            + ". Use this task to turn those signals into one concrete repo-local tuning task."
        ),
        "safe_first_slice": "\n".join(safe_first_slice),
        "requires_approval": False,
        "approval_reason": "",
        "epic_title": "Self-tuning queue",
        "epic_id": "epic-blackdog-tune",
        "lane_title": "Self-tuning lane",
        "lane_id": "lane-blackdog-tune",
        "wave": 0,
    }


def seed_tune_task(
    profile: Profile,
    *,
    tune_task_payload_builder: Callable[[Profile], dict[str, Any]] = _tune_task_payload,
) -> tuple[dict[str, Any], bool]:
    template = tune_task_payload_builder(profile)
    target_task_id = core_backlog.make_task_id(
        profile,
        bucket=template["bucket"],
        title=template["title"],
        paths=template["paths"],
    )
    with core_backlog.locked_path(profile.paths.backlog_file):
        snapshot = core_backlog.load_backlog(profile.paths, profile)
        existing = snapshot.tasks.get(target_task_id)
        if existing is not None:
            return existing.payload, False

    try:
        payload = core_backlog.add_task(
            profile,
            title=template["title"],
            bucket=template["bucket"],
            priority=template["priority"],
            risk=template["risk"],
            effort=template["effort"],
            why=template["why"],
            evidence=template["evidence"],
            safe_first_slice=template["safe_first_slice"],
            paths=template["paths"],
            checks=template["checks"],
            docs=template["docs"],
            domains=template["domains"],
            packages=template["packages"],
            affected_paths=template["paths"],
            task_shaping=None,
            objective=template["objective"],
            requires_approval=template["requires_approval"],
            approval_reason=template["approval_reason"],
            epic_id=template["epic_id"],
            epic_title=template["epic_title"],
            lane_id=template["lane_id"],
            lane_title=template["lane_title"],
            wave=template["wave"],
        )
    except core_backlog.BacklogError:
        with core_backlog.locked_path(profile.paths.backlog_file):
            snapshot = core_backlog.load_backlog(profile.paths, profile)
            existing = snapshot.tasks.get(target_task_id)
            if existing is not None:
                return existing.payload, False
        raise

    return payload, True
