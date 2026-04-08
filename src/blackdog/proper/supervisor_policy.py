from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path
from typing import Any

from ..backlog import TaskInfo
from ..config import Profile
from .worktree import WorktreeSpec, worktree_contract


DYNAMIC_REASONING_BASE_EFFORT = "high"
DYNAMIC_REASONING_COMPLEX_EFFORT = "xhigh"
CHILD_PROMPT_TEMPLATE_VERSION = 3


_CHILD_PROMPT_TEMPLATE = """
You are Blackdog child agent `{child_agent}` working on one Blackdog backlog task.

Current workspace for code changes: `{workspace}`
Central Blackdog project root for backlog state: `{project_root}`
Workspace mode: `{workspace_mode}`

Task id: `{task_id}`
Title: {task_title}
Objective: {objective}
Epic: {epic_title}
Lane: {lane_title}
Wave: {wave}
Priority: {priority}
Risk: {risk}
Domains: {domains}

Why it matters: {why}
Evidence: {evidence}
Safe first slice: {safe_first_slice}

Target paths:
{paths}

Docs to review:
{docs}

Checks to run if you change behavior:
{checks}

Required operating rules:
{workspace_baseline_rule}
{preserve_rule}
- Supervisor workspace mode for this run: `{workspace_mode}`.
{primary_cleanliness_rule}
{venv_rule}
- This branch-backed child run is already claimed and prepared. Skip manual startup and completion steps like `blackdog worktree preflight`, `blackdog claim`, and `blackdog complete`.
- Prefer Blackdog CLI output over direct reads of raw state files when checking claims, inbox state, results, or task status.
- Treat documented Blackdog CLI commands and stable artifact files as the integration contract; do not hand-edit backlog state or rely on private module imports when a CLI write path exists.
- Work only on `{task_id}`.
- Use the current directory for code edits.
- For Blackdog state commands, always target the central root with `--project-root {project_root}`.
- Before starting, read your inbox with `{protocol_command} inbox list`.
- Use the child protocol helper for protocol operations in this workspace:
  - `{protocol_command} result record --status success --what-changed "..."`
  - `{protocol_command} release --note "..."`
{branch_rules}
- If blocked, record a blocked or partial result and release the task with `{protocol_command} release --note "<reason>"`.
- Do not start unrelated tasks.
""".lstrip()

CHILD_PROMPT_TEMPLATE_HASH = hashlib.sha256(_CHILD_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()


def build_child_prompt(
    profile: Profile,
    task: TaskInfo,
    *,
    child_agent: str,
    workspace_mode: str,
    workspace: Path,
    worktree_spec: WorktreeSpec | None = None,
    protocol_command: Path,
) -> str:
    if worktree_spec is None:
        raise RuntimeError("Blackdog only supports branch-backed task worktrees for child runs")
    contract = worktree_contract(profile, workspace=workspace, workspace_mode=workspace_mode)
    docs = "\n".join(f"- {item}" for item in task.payload.get("docs", [])) or "- No routed docs."
    checks = "\n".join(f"- {item}" for item in task.payload.get("checks", [])) or "- No validation commands."
    paths = "\n".join(f"- {item}" for item in task.payload.get("paths", [])) or "- No specific paths."
    domains = ", ".join(str(item) for item in task.payload.get("domains", [])) or "none"
    workspace_baseline_rule = "- This branch-backed worktree was created from the primary worktree branch. Treat committed repo state as the baseline for this task."
    preserve_rule = "- Keep your changes isolated to the task branch and target paths unless the task requires broader edits."
    primary_cleanliness_rule = (
        f"- Primary-worktree landing gate: currently dirty ({', '.join(contract['primary_dirty_paths'])}). "
        f"The supervisor cannot land `{worktree_spec.branch}` into `{contract['target_branch']}` until the primary checkout is clean."
        if contract["primary_dirty_paths"]
        else f"- Primary-worktree landing gate: `{contract['primary_worktree']}` must stay clean for the supervisor to land changes into `{contract['target_branch']}`."
    )
    venv_rule = (
        f"- `{contract['ve_expectation']}` Preferred CLI for this workspace: `{contract['workspace_blackdog_path']}`."
        if contract["workspace_has_local_blackdog"]
        else f"- `{contract['ve_expectation']}` This workspace does not currently have `{contract['workspace_blackdog_path']}`, so use the child protocol helper at `{protocol_command}`."
    )
    branch_rules = textwrap.dedent(
        f"""
        - This is a branch-backed task worktree on branch `{worktree_spec.branch}` targeting `{worktree_spec.target_branch}`.
        - Commit your code changes on that task branch before you exit if you want the supervisor to land them.
        - Do not land, merge, or delete the branch yourself. The supervisor will land `{worktree_spec.branch}` through the primary worktree and then clean it up.
        - Do not run `{protocol_command} complete` for this task from a branch-backed child run; the supervisor will complete it after a successful land.
        """
    ).strip()
    return textwrap.dedent(
        _CHILD_PROMPT_TEMPLATE.format(
            child_agent=child_agent,
            workspace=workspace,
            project_root=profile.paths.project_root,
            workspace_mode=contract["workspace_mode"],
            task_id=task.id,
            task_title=task.title,
            objective=task.payload.get("objective") or "unassigned",
            epic_title=task.epic_title or "Unplanned",
            lane_title=task.lane_title or "Unplanned",
            wave=task.wave if task.wave is not None else "unplanned",
            priority=task.payload.get("priority"),
            risk=task.payload.get("risk"),
            domains=domains,
            why=task.narrative.why or "See backlog task entry.",
            evidence=task.narrative.evidence or "See backlog task entry.",
            safe_first_slice=task.payload.get("safe_first_slice"),
            paths=paths,
            docs=docs,
            checks=checks,
            workspace_baseline_rule=workspace_baseline_rule,
            preserve_rule=preserve_rule,
            primary_cleanliness_rule=primary_cleanliness_rule,
            venv_rule=venv_rule,
            branch_rules=branch_rules,
            protocol_command=protocol_command,
        )
    ).strip()


def apply_launch_overrides(
    command: list[str],
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> list[str]:
    updated: list[str] = []
    saw_model = False
    saw_reasoning = False
    index = 0
    while index < len(command):
        token = command[index]
        next_token = command[index + 1] if index + 1 < len(command) else None
        if token in {"-m", "--model"} and next_token is not None:
            updated.extend([token, model if model is not None else next_token])
            saw_model = True
            index += 2
            continue
        if token == "--effort" and next_token is not None:
            updated.extend([token, reasoning_effort if reasoning_effort is not None else next_token])
            saw_reasoning = True
            index += 2
            continue
        if token == "-c" and next_token is not None and "=" in next_token:
            key, _, value = next_token.partition("=")
            if key == "model":
                updated.extend([token, f"{key}={model if model is not None else value}"])
                saw_model = True
                index += 2
                continue
            if key == "model_reasoning_effort":
                updated.extend([token, f"{key}={reasoning_effort if reasoning_effort is not None else value}"])
                saw_reasoning = True
                index += 2
                continue
        updated.append(token)
        index += 1
    if model is not None and not saw_model:
        updated.extend(["-m", model])
    if reasoning_effort is not None and not saw_reasoning:
        updated.extend(["-c", f"model_reasoning_effort={reasoning_effort}"])
    return updated


def _dynamic_reasoning_effort_for_task(profile: Profile, task: TaskInfo) -> str:
    base_effort = profile.supervisor_reasoning_effort or DYNAMIC_REASONING_BASE_EFFORT
    if str(task.payload.get("risk") or "") == "high" or str(task.payload.get("effort") or "") == "L":
        return DYNAMIC_REASONING_COMPLEX_EFFORT
    return base_effort


def resolved_task_launch_overrides(
    profile: Profile,
    task: TaskInfo,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, str | None]:
    resolved_reasoning_effort = reasoning_effort
    if resolved_reasoning_effort is None:
        if profile.supervisor_dynamic_reasoning:
            resolved_reasoning_effort = _dynamic_reasoning_effort_for_task(profile, task)
        else:
            resolved_reasoning_effort = profile.supervisor_reasoning_effort
    return {
        "model": model if model is not None else profile.supervisor_model,
        "reasoning_effort": resolved_reasoning_effort,
    }


def launch_settings_view(launch_command: tuple[str, ...] | list[str], *, strategy: str) -> dict[str, Any]:
    command = list(launch_command)
    model = None
    reasoning_effort = None
    config_overrides: dict[str, str] = {}
    index = 0
    while index < len(command):
        token = command[index]
        next_token = command[index + 1] if index + 1 < len(command) else None
        if token in {"-m", "--model"} and next_token is not None:
            model = next_token
            index += 2
            continue
        if token == "--effort" and next_token is not None:
            reasoning_effort = next_token
            index += 2
            continue
        if token == "-c" and next_token is not None and "=" in next_token:
            key, _, value = next_token.partition("=")
            config_overrides[key] = value
            if key == "model":
                model = value
            elif key == "model_reasoning_effort":
                reasoning_effort = value
            index += 2
            continue
        index += 1
    return {
        "command": command,
        "strategy": strategy,
        "launcher": command[0] if command else None,
        "mode": command[1] if len(command) > 1 else None,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "config_overrides": config_overrides,
    }


def launch_defaults_view(profile: Profile, settings: dict[str, Any]) -> dict[str, Any]:
    view = dict(settings)
    view["model"] = profile.supervisor_model or view.get("model")
    view["reasoning_effort"] = profile.supervisor_reasoning_effort or view.get("reasoning_effort")
    view["dynamic_reasoning"] = profile.supervisor_dynamic_reasoning
    if profile.supervisor_dynamic_reasoning:
        base_effort = profile.supervisor_reasoning_effort or DYNAMIC_REASONING_BASE_EFFORT
        view["dynamic_reasoning_summary"] = (
            f"{base_effort} by default; {DYNAMIC_REASONING_COMPLEX_EFFORT} for high-risk or L tasks"
        )
    return view


def launch_settings_reasoning_label(settings: dict[str, Any]) -> str:
    if settings.get("dynamic_reasoning"):
        return str(
            settings.get("dynamic_reasoning_summary")
            or f"{DYNAMIC_REASONING_BASE_EFFORT}/{DYNAMIC_REASONING_COMPLEX_EFFORT} (dynamic)"
        )
    return str(settings.get("reasoning_effort") or "default")


def build_child_launch_telemetry(
    launch_command: tuple[str, ...],
    launch_command_strategy: str,
    prompt: str,
) -> dict[str, Any]:
    return {
        "launch_command": list(launch_command),
        "launch_command_strategy": launch_command_strategy,
        "launch_settings": launch_settings_view(launch_command, strategy=launch_command_strategy),
        "prompt_template_version": CHILD_PROMPT_TEMPLATE_VERSION,
        "prompt_template_hash": CHILD_PROMPT_TEMPLATE_HASH,
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
