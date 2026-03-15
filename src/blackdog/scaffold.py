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
from .config import load_profile, named_backlog_paths, write_default_profile, Profile
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
    candidate = (project_root / ".VE" / "bin" / executable).resolve()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return shlex.quote(str(candidate))
    return executable


def generate_project_skill(profile: Profile, *, force: bool = False) -> Path:
    skill_dir = profile.paths.skill_dir
    agents_dir = skill_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    agent_file = agents_dir / "openai.yaml"
    if skill_file.exists() and not force:
        raise ScaffoldError(f"Refusing to overwrite {skill_file}; pass --force to replace it")
    skill_name = "blackdog"
    display_name = "Blackdog"
    cli_command = _preferred_cli_command(profile.paths.project_root, "blackdog")
    skill_command = _preferred_cli_command(profile.paths.project_root, "blackdog-skill")
    validation_lines = "\n".join(f"  - `{command}`" for command in profile.validation_commands)
    doc_routing_lines = "\n".join(f"  - `{path}`" for path in profile.doc_routing_defaults)
    skill_text = f"""---
name: {skill_name}
description: "Use the project-local Blackdog backlog contract for {profile.project_name}. Trigger this skill when reviewing, claiming, completing, supervising, or reporting backlog work in this repo, or when checking inbox messages and structured task results."
---

# {display_name}

Use the local Blackdog CLI instead of mutating backlog state by hand.

## CLI Entry Points

- Blackdog CLI: `{cli_command}`
- Skill refresh CLI: `{skill_command}`

## Core Paths

- Profile: `{profile.paths.profile_file}`
- Control root: `{profile.paths.control_dir}`
- Backlog: `{profile.paths.backlog_file}`
- State: `{profile.paths.state_file}`
- Events: `{profile.paths.events_file}`
- Inbox: `{profile.paths.inbox_file}`
- Results: `{profile.paths.results_dir}`
- HTML view: `{profile.paths.html_file}`

## Standard Flow

1. Run `{cli_command} validate`.
2. Run `{cli_command} summary`.
3. Inspect runnable work with `{cli_command} next`.
4. Before any repo edit you intend to keep, run `{cli_command} worktree preflight`. If it reports `primary worktree: yes`, do not edit in that checkout; create or enter a branch-backed task worktree with `{cli_command} worktree start --id TASK` first. Analysis-only work can stay in the current checkout.
5. Claim one task with `{cli_command} claim --agent <agent-name>`, then record structured output with `{cli_command} result record ...`.
6. Complete or release the task through the CLI for direct work.
7. Use `{cli_command} supervise run` or `{cli_command} supervise loop` when you want Blackdog to launch child agents instead of editing directly.
8. Check `{cli_command} inbox list --recipient <agent-name>` before claiming fresh work if the run may have pending instructions.
9. Open `{profile.paths.html_file}` directly when you want the static task index; `blackdog render` refreshes it and supervisor cycles rewrite it after each loop pass.

## Docs to Review

Review these repo docs before editing when they apply:
{doc_routing_lines}

Keep `blackdog.toml` `[taxonomy].doc_routing_defaults` aligned with the repo's required review set, then regenerate this skill after routing changes.

## Supervisor Model

- The coordinating agent stays in the primary worktree.
- Child agents launched by `blackdog supervise ...` run in branch-backed task worktrees and land through the primary worktree after successful commits.
- Blackdog uses branch-backed task worktrees for kept implementation changes.
- `pause` and `stop` messages are checked between loop cycles. They do not interrupt an already-running child claim.

## Repo Contract

- Commit `blackdog.toml` and this project-local skill if the repo wants a shared Blackdog operating contract.
- Do not check in mutable runtime files from `{profile.paths.control_dir}`.
- Regenerate this skill after profile changes with `{skill_command} refresh backlog --project-root {profile.paths.project_root}`.

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
  default_prompt: "Use Blackdog's project-local profile, skill, and shared control root for {profile.project_name} to review, claim, supervise, and report backlog work through the repo CLI."
"""
    skill_file.write_text(skill_text, encoding="utf-8")
    agent_file.write_text(agent_text, encoding="utf-8")
    return skill_file
