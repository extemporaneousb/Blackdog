from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import os
import shlex
import shutil

from .backlog import (
    render_initial_backlog,
    refresh_backlog_headers,
)
from .config import GIT_COMMON_TOKEN, _git_common_dir, load_profile, named_backlog_paths, write_default_profile, Profile
from .store import append_event, default_state, save_state
from .ui import build_ui_snapshot, render_static_html


class ScaffoldError(RuntimeError):
    pass


def _ensure_runtime_dirs(profile: Profile) -> None:
    profile.paths.backlog_dir.mkdir(parents=True, exist_ok=True)
    profile.paths.results_dir.mkdir(parents=True, exist_ok=True)
    profile.paths.supervisor_runs_dir.mkdir(parents=True, exist_ok=True)


def _profile_for_paths(profile: Profile, *, paths) -> Profile:
    return replace(profile, paths=paths)


def _ensure_baseline_agents_file(project_root: Path) -> None:
    agents_file = project_root / "AGENTS.md"
    if agents_file.exists():
        if agents_file.is_file():
            return
        raise ScaffoldError(f"AGENTS path exists but is not a file: {agents_file}")
    agents_file.write_text(
        "# AGENTS\n\n"
        "This repository was scaffolded with Blackdog.\n\n"
        "Update this contract for host-repo-specific requirements.\n\n"
        "Until this file is updated, follow a minimal standard:\n"
        "- Keep dependency-light changes when possible.\n"
        "- Use repository-local `./.VE/bin/blackdog`/`blackdog-skill` when present.\n"
        "- Keep implementation changes in branch-backed task worktrees.\n"
        "- Preserve this repository's existing operational contract.\n",
        encoding="utf-8",
    )


def scaffold_named_backlog(profile: Profile, name: str, *, force: bool = False) -> Path:
    paths = named_backlog_paths(profile, name)
    named_profile = _profile_for_paths(profile, paths=paths)
    if paths.backlog_dir.exists() and any(paths.backlog_dir.iterdir()) and not force:
        raise ScaffoldError(f"Refusing to overwrite {paths.backlog_dir}; pass --force to replace it")
    if paths.backlog_dir.exists() and force:
        shutil.rmtree(paths.backlog_dir)
    _ensure_runtime_dirs(named_profile)
    paths.backlog_file.write_text(render_initial_backlog(named_profile), encoding="utf-8")
    save_state(paths.state_file, default_state())
    paths.events_file.write_text("", encoding="utf-8")
    paths.inbox_file.write_text("", encoding="utf-8")
    append_event(paths, event_type="init", actor="blackdog", payload={"project_name": profile.project_name, "backlog_name": name})
    render_project_html(named_profile)
    return paths.backlog_dir


def remove_named_backlog(profile: Profile, name: str) -> Path:
    paths = named_backlog_paths(profile, name)
    if not paths.backlog_dir.exists():
        raise ScaffoldError(f"Named backlog does not exist: {paths.backlog_dir}")
    shutil.rmtree(paths.backlog_dir)
    return paths.backlog_dir


def reset_default_backlog(profile: Profile, *, purge_named: bool = False) -> Path:
    control_dir = profile.paths.control_dir
    if control_dir.exists():
        for child in list(control_dir.iterdir()):
            if child.name == profile.paths.profile_file.name:
                continue
            if child.name == ".DS_Store":
                child.unlink(missing_ok=True)
                continue
            if purge_named or child.name != "backlogs":
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    _ensure_runtime_dirs(profile)
    profile.paths.backlog_file.write_text(render_initial_backlog(profile), encoding="utf-8")
    save_state(profile.paths.state_file, default_state())
    profile.paths.events_file.write_text("", encoding="utf-8")
    profile.paths.inbox_file.write_text("", encoding="utf-8")
    append_event(profile.paths, event_type="init", actor="blackdog", payload={"project_name": profile.project_name})
    render_project_html(profile)
    return profile.paths.backlog_dir


def scaffold_project(
    project_root: Path,
    *,
    project_name: str,
    force: bool = False,
    objectives: list[str] | None = None,
    push_objective: list[str] | None = None,
    non_negotiables: list[str] | None = None,
    evidence_requirements: list[str] | None = None,
    release_gates: list[str] | None = None,
) -> Profile:
    root = project_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    write_default_profile(root, project_name, force=force)
    profile = load_profile(root)
    _ensure_runtime_dirs(profile)
    if profile.paths.backlog_file.exists() and not force:
        raise ScaffoldError(f"Refusing to overwrite {profile.paths.backlog_file}; pass --force to replace it")
    profile.paths.backlog_file.write_text(
        render_initial_backlog(
            profile,
            objectives=objectives,
            push_objective=push_objective,
            non_negotiables=non_negotiables,
            evidence_requirements=evidence_requirements,
            release_gates=release_gates,
        ),
        encoding="utf-8",
    )
    save_state(profile.paths.state_file, default_state())
    profile.paths.events_file.write_text("", encoding="utf-8")
    profile.paths.inbox_file.write_text("", encoding="utf-8")
    append_event(profile.paths, event_type="init", actor="blackdog", payload={"project_name": project_name})
    render_project_html(profile)
    return profile


def bootstrap_project(
    project_root: Path,
    *,
    project_name: str,
    force: bool = False,
    objectives: list[str] | None = None,
    push_objective: list[str] | None = None,
    non_negotiables: list[str] | None = None,
    evidence_requirements: list[str] | None = None,
    release_gates: list[str] | None = None,
) -> tuple[Profile, Path]:
    root = project_root.resolve()
    profile_file = root / "blackdog.toml"
    if force or not profile_file.exists():
        profile = scaffold_project(
            root,
            project_name=project_name,
            force=force,
            objectives=objectives,
            push_objective=push_objective,
            non_negotiables=non_negotiables,
            evidence_requirements=evidence_requirements,
            release_gates=release_gates,
        )
    else:
        profile = load_profile(root)
        _ensure_runtime_dirs(profile)
        missing = [
            str(path)
            for path in (
                profile.paths.backlog_file,
                profile.paths.state_file,
                profile.paths.events_file,
                profile.paths.inbox_file,
            )
            if not path.exists()
        ]
        if missing:
            raise ScaffoldError(
                "Existing Blackdog profile is missing required backlog artifacts: "
                + ", ".join(missing)
                + ". Use --force to rebuild the scaffold."
            )

    _ensure_baseline_agents_file(root)

    skill_file = profile.paths.skill_dir / "SKILL.md"
    if force or not skill_file.exists():
        skill_file = generate_project_skill(profile, force=force)
    render_project_html(profile)
    return profile, skill_file


def refresh_project_skill(profile: Profile) -> Path:
    return generate_project_skill(profile, force=True)


def render_project_html(profile: Profile) -> Path:
    refresh_backlog_headers(profile)
    snapshot = build_ui_snapshot(profile)
    render_static_html(snapshot, profile.paths.html_file)
    return profile.paths.html_file


def _preferred_cli_command(project_root: Path, executable: str) -> str:
    candidate = project_root / ".VE" / "bin" / executable
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return shlex.quote(f"./.VE/bin/{executable}")
    return executable


def _display_path(project_root: Path, path: Path, *, git_common_dir: Path | None = None) -> str:
    resolved_root = project_root.resolve()
    resolved_path = path.resolve()
    if git_common_dir is not None:
        try:
            relative_to_git_common = resolved_path.relative_to(git_common_dir)
        except ValueError:
            pass
        else:
            relative_text = relative_to_git_common.as_posix()
            return GIT_COMMON_TOKEN if not relative_text else f"{GIT_COMMON_TOKEN}/{relative_text}"
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError:
        return str(resolved_path)
    return relative.as_posix() or "."


def _task_shaping_reference_text() -> str:
    return """# Task Shaping

Use this reference when turning a user prompt into backlog tasks or when restructuring an existing plan.

## Objective

- Minimize the number of separate agent requests, worktrees, and handoffs while still minimizing end-to-end turnaround time.
- Prefer fewer, larger tasks that own a coherent deliverable and can land with one validation story.

## Measure First

Estimate these values before deciding whether to split work:

- `estimated_elapsed_minutes`: total wall-clock time to land and validate the deliverable.
- `estimated_active_minutes`: expected edit and reasoning time.
- `estimated_touched_paths`: approximate number of files or directories likely to change.
- `estimated_validation_minutes`: time to run the required checks.
- `estimated_worktrees`: how many branch-backed worktrees or child launches the plan would require.
- `estimated_handoffs`: how many times context would move between agents or tasks.
- `parallelizable_groups`: how many truly independent write scopes could run at the same time.

If the CLI has nowhere to store these fields, keep them in working notes and only record the meaningful constraints in task `why`, `evidence`, or comments.

## Consolidate by Default

Use one task when most of the following are true:

- the work shares the same touched paths or validation commands;
- one step depends directly on the previous step;
- the deliverable only makes sense as one landed change;
- worktree spin-up cost is material relative to the expected edit time; or
- handoff cost is likely to exceed any parallel speedup.

Use one lane for one cohesive deliverable. Do not create separate lane or task pairs for research, implementation, cleanup, and validation when they are parts of the same serial change.

## Split Only for Parallelism or Blocking

Split a task only when every child slice has:

- a disjoint or lightly coupled write set;
- an independently meaningful landed outcome;
- validation that can run mostly independently; and
- a believable wall-clock win from parallel execution.

Simple rule: split only when `parallel time saved > extra worktree spin-up + extra coordination + duplicated validation`.

## Tuning Loop

After completion, compare the estimates with actuals:

- actual elapsed time;
- actual changed path count;
- actual validation time;
- number of worktrees or agents used; and
- number of times work had to be merged, re-claimed, or re-scoped.

If consolidation repeatedly produces tasks that are too large, split later at the boundary that appeared in the real work. If parallel slices repeatedly collide in the same files or validations, merge them earlier next time.
"""


def generate_project_skill(profile: Profile, *, force: bool = False) -> Path:
    skill_dir = profile.paths.skill_dir
    agents_dir = skill_dir / "agents"
    references_dir = skill_dir / "references"
    agents_dir.mkdir(parents=True, exist_ok=True)
    references_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    agent_file = agents_dir / "openai.yaml"
    task_shaping_reference_file = references_dir / "task-shaping.md"
    if skill_file.exists() and not force:
        raise ScaffoldError(f"Refusing to overwrite {skill_file}; pass --force to replace it")
    skill_name = "blackdog"
    display_name = "Blackdog"
    cli_command = _preferred_cli_command(profile.paths.project_root, "blackdog")
    skill_command = _preferred_cli_command(profile.paths.project_root, "blackdog-skill")
    git_common_dir = _git_common_dir(profile.paths.project_root)
    profile_path = _display_path(profile.paths.project_root, profile.paths.profile_file, git_common_dir=git_common_dir)
    control_root = _display_path(profile.paths.project_root, profile.paths.control_dir, git_common_dir=git_common_dir)
    backlog_path = _display_path(profile.paths.project_root, profile.paths.backlog_file, git_common_dir=git_common_dir)
    state_path = _display_path(profile.paths.project_root, profile.paths.state_file, git_common_dir=git_common_dir)
    events_path = _display_path(profile.paths.project_root, profile.paths.events_file, git_common_dir=git_common_dir)
    inbox_path = _display_path(profile.paths.project_root, profile.paths.inbox_file, git_common_dir=git_common_dir)
    results_path = _display_path(profile.paths.project_root, profile.paths.results_dir, git_common_dir=git_common_dir)
    html_path = _display_path(profile.paths.project_root, profile.paths.html_file, git_common_dir=git_common_dir)
    coverage_output = _display_path(
        profile.paths.project_root,
        profile.paths.project_root / "coverage" / "latest.json",
        git_common_dir=git_common_dir,
    )
    skill_metadata_path = _display_path(
        profile.paths.project_root,
        profile.paths.skill_dir / "SKILL.md",
        git_common_dir=git_common_dir,
    )
    skill_discovery_path = _display_path(
        profile.paths.project_root,
        profile.paths.skill_dir / "agents" / "openai.yaml",
        git_common_dir=git_common_dir,
    )
    validation_lines = "\n".join(f"  - `{command}`" for command in profile.validation_commands)
    doc_routing_lines = "\n".join(f"  - `{path}`" for path in profile.doc_routing_defaults)
    skill_text = f"""---
name: {skill_name}
description: "Use the project-local Blackdog backlog contract for {profile.project_name}. Trigger this skill when shaping a user request into measurable backlog tasks, reviewing, adding, claiming, completing, supervising, or reporting backlog work in this repo, or when checking inbox messages and structured task results."
---

# {display_name}

Use the local Blackdog CLI instead of mutating backlog state by hand.

## CLI Entry Points

- Blackdog CLI: `{cli_command}`
- Skill refresh CLI: `{skill_command}`

## Core Paths

- Profile: `{profile_path}`
- Control root: `{control_root}`
- Backlog: `{backlog_path}`
- State: `{state_path}`
- Events: `{events_path}`
- Inbox: `{inbox_path}`
- Results: `{results_path}`
- HTML view: `{html_path}`

## Codex Skill Discovery

- Skill metadata file: `{skill_metadata_path}`
- UI discovery file: `{skill_discovery_path}`
- Codex discovers this skill from `agents/openai.yaml` under `.codex/skills/<skill-name>/` in the opened repo.
- Open or refresh the repo in Codex after bootstrap so the skill appears in the available skill list.

## Standard Flow

1. Run `{cli_command} validate`.
2. Run `{cli_command} summary`.
3. Inspect runnable work with `{cli_command} next`.
4. Before any repo edit you intend to keep, run `{cli_command} worktree preflight`. If it reports `primary worktree: yes`, do not edit in that checkout; create or enter a branch-backed task worktree with `{cli_command} worktree start --id TASK` first. Analysis-only work can stay in the current checkout.
5. Run `{cli_command} coverage --output {coverage_output}` to collect shipping-surface validation coverage evidence before large surface edits.
6. Claim one task with `{cli_command} claim --agent <agent-name>`, then record structured output with `{cli_command} result record ...`.
7. Complete or release the task through the CLI for direct work.
8. Use `{cli_command} supervise run` when you want Blackdog to launch child agents instead of editing directly.
9. Check `{cli_command} inbox list --recipient <agent-name>` before claiming fresh work if the run may have pending instructions.
10. Open `{html_path}` directly when you want the static backlog board; `blackdog render` refreshes it and active supervisor runs rerender it after task-state changes, including run exit after landed updates.

## Task Shaping

- Treat a new user request as one candidate deliverable first. Default to one lane and one task unless there is a measured reason to split it.
- Consolidate serial slices that touch the same files, need the same validation, or must land together. Do not create separate tasks for analysis, implementation, cleanup, and verification of the same change.
- Split only when it buys real parallelism: disjoint write sets, independent validation, separate blockers, or clearly separable deliverables that can land independently.
- Before creating or reshaping tasks, estimate total elapsed task time, active edit time, touched paths, validation time, worktree spin-ups, and coordination handoffs. Minimize separate requests first, then add parallelism only when the saved wall-clock time exceeds the extra spin-up and coordination cost.
- When uncertain, under-split first. It is easier to split a live task later than to merge redundant lanes and half-finished work.
- Use [references/task-shaping.md](references/task-shaping.md) when adding, tuning, or restructuring work; it contains the measurement fields and consolidation rubric.

## Static Board

- `{html_path}` renders a wide control board with a `Backlog Control` panel, `Status` panel, paired objective/release-gate tables, `Execution Map`, and `Completed Tasks`.
- The control panel shows the current push copy, branch/commit/run/time-on-task summary, progress bar, and plain artifact links.
- The release-gates panel stays beside the objective table and shows explicit or inferred passed checks without making the rows interactive.
- The execution map keeps only live lanes and waves visible, carries the `Inbox JSON` link, and removes search/filter chrome.
- Objective rows are summary-only, while execution-map and completed-task cards open the task reader popout. Completed history is grouped by sweep when run metadata exists.

## Docs to Review

Review these repo docs before editing when they apply:
{doc_routing_lines}

Keep `blackdog.toml` `[taxonomy].doc_routing_defaults` aligned with the repo's required review set, then regenerate this skill after routing changes.

## Supervisor Model

- The coordinating agent stays in the primary worktree.
- Child agents launched by `blackdog supervise ...` run in branch-backed task worktrees and land through the primary worktree after successful commits.
- Blackdog uses branch-backed task worktrees for kept implementation changes.
- `stop` messages are checked while a supervisor run is active. They prevent new launches, but they do not interrupt an already-running child claim.
- Tasks completed during the active run stay visible in the execution map until the next run starts and performs its opening sweep.

## Repo Contract

- Commit `blackdog.toml` and this project-local skill if the repo wants a shared Blackdog operating contract.
- Do not check in mutable runtime files from `{control_root}`.
- Regenerate this skill after profile changes with `{skill_command} refresh backlog --project-root .`.

## Repo Defaults

- Id prefix: `{profile.id_prefix}`
- Buckets: {", ".join(profile.buckets)}
- Domains: {", ".join(profile.domains)}
- Validation defaults:
{validation_lines}
"""
    agent_text = f"""interface:
  display_name: "{display_name}"
  short_description: "Project-local backlog control via Blackdog"
  default_prompt: "Use Blackdog's project-local profile, skill, and shared control root for {profile.project_name} to shape this request into measurable, consolidated backlog work and then review, claim, supervise, or report it through the repo CLI."
"""
    skill_file.write_text(skill_text, encoding="utf-8")
    agent_file.write_text(agent_text, encoding="utf-8")
    task_shaping_reference_file.write_text(_task_shaping_reference_text(), encoding="utf-8")
    return skill_file
