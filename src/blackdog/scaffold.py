from __future__ import annotations

from pathlib import Path

from .backlog import (
    render_html,
    render_initial_backlog,
    build_view_model,
    load_backlog,
    refresh_backlog_headers,
    sync_state_for_backlog,
)
from .config import load_profile, write_default_profile, Profile
from .store import append_event, default_state, load_events, load_inbox, load_state, load_task_results, save_state


class ScaffoldError(RuntimeError):
    pass


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
    profile.paths.backlog_dir.mkdir(parents=True, exist_ok=True)
    profile.paths.results_dir.mkdir(parents=True, exist_ok=True)
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


def render_project_html(profile: Profile) -> Path:
    refresh_backlog_headers(profile)
    snapshot = load_backlog(profile.paths, profile)
    state = load_state(profile.paths.state_file)
    state = sync_state_for_backlog(state, snapshot)
    save_state(profile.paths.state_file, state)
    view = build_view_model(
        profile,
        snapshot,
        state,
        events=load_events(profile.paths, limit=20),
        messages=load_inbox(profile.paths),
        results=load_task_results(profile.paths),
    )
    render_html(view, profile.paths.html_file)
    return profile.paths.html_file


def generate_project_skill(profile: Profile, *, force: bool = False) -> Path:
    skill_dir = profile.paths.skill_dir
    agents_dir = skill_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    agent_file = agents_dir / "openai.yaml"
    if skill_file.exists() and not force:
        raise ScaffoldError(f"Refusing to overwrite {skill_file}; pass --force to replace it")
    project_slug = profile.project_name.lower().replace(" ", "-")
    skill_name = "blackdog-backlog" if project_slug == "blackdog" else f"{project_slug}-blackdog-backlog"
    display_name = "Blackdog Backlog" if project_slug == "blackdog" else f"{profile.project_name} Blackdog Backlog"
    validation_lines = "\n".join(f"  - `{command}`" for command in profile.validation_commands)
    skill_text = f"""---
name: {skill_name}
description: "Use the repo-versioned Blackdog backlog for {profile.project_name}. Trigger this skill when preparing, reviewing, claiming, completing, or reporting backlog work in this project, or when checking inbox messages and structured task results."
---

# {display_name}

Use the local Blackdog CLI instead of mutating backlog state by hand.

## Core Paths

- Profile: `{profile.paths.profile_file}`
- Backlog: `{profile.paths.backlog_file}`
- State: `{profile.paths.state_file}`
- Events: `{profile.paths.events_file}`
- Inbox: `{profile.paths.inbox_file}`
- Results: `{profile.paths.results_dir}`
- HTML view: `{profile.paths.html_file}`

## Standard Flow

1. Run `blackdog validate`.
2. Run `blackdog summary`.
3. Inspect runnable work with `blackdog next`.
4. Claim one task with `blackdog claim --agent <agent-name>`.
5. Record structured output with `blackdog result record ...`.
6. Complete or release the task through the CLI.
7. Check `blackdog inbox list --recipient <agent-name>` before claiming fresh work if the run may have pending instructions.

## Interaction Model

- Use `blackdog inbox send` for user, supervisor, or child-agent instructions/questions.
- Use `blackdog comment` for task-scoped narrative notes that belong in the event log.
- Use `blackdog result record` for structured `what_changed`, `validation`, `residual`, `needs_user_input`, and `followup_candidates`.
- Use `blackdog render` whenever you need a refreshed HTML control page.

## Repo Defaults

- Id prefix: `{profile.id_prefix}`
- Buckets: {", ".join(profile.buckets)}
- Domains: {", ".join(profile.domains)}
- Validation defaults:
{validation_lines}
"""
    agent_text = f"""interface:
  display_name: "{display_name}"
  short_description: "Repo-versioned backlog control via Blackdog"
  default_prompt: "Use the local Blackdog backlog for {profile.project_name} to review, claim, complete, and report work through the repo-versioned CLI."
"""
    skill_file.write_text(skill_text, encoding="utf-8")
    agent_file.write_text(agent_text, encoding="utf-8")
    return skill_file
