from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from typing import Any

from ..backlog import render_initial_backlog, refresh_backlog_headers
from ..config import (
    DEFAULT_SKILL_USAGE_HEURISTIC,
    GIT_COMMON_TOKEN,
    _git_common_dir,
    default_host_skill_name,
    load_profile,
    named_backlog_paths,
    write_default_profile,
    Profile,
)
from ..store import append_event, default_state, save_state
from .ui import build_ui_snapshot, render_static_html


class ScaffoldError(RuntimeError):
    pass


MANAGED_SKILL_MANIFEST = ".blackdog-managed.json"
MANAGED_SKILL_PREVIEW_SUFFIX = ".blackdog-new"


def _run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ScaffoldError(f"Command not found while scaffolding project: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        message = f"Command failed while scaffolding project: {shlex.join(command)}"
        if detail:
            message += f"\n{detail}"
        raise ScaffoldError(message) from exc


def _default_blackdog_source() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise ScaffoldError(
        "Cannot infer a local Blackdog source checkout from the current runtime; "
        "pass --blackdog-source PATH."
    )


def _ensure_new_project_root(project_root: Path) -> None:
    if project_root.exists():
        if not project_root.is_dir():
            raise ScaffoldError(f"Project root exists but is not a directory: {project_root}")
        if any(project_root.iterdir()):
            raise ScaffoldError(
                f"Refusing to create a new project in non-empty directory: {project_root}. "
                "Use `blackdog bootstrap` for an existing repo."
            )
        return
    project_root.mkdir(parents=True, exist_ok=True)


def _initialize_git_repo(project_root: Path) -> None:
    _run_command(["git", "init", str(project_root)])
    _run_command(["git", "-C", str(project_root), "symbolic-ref", "HEAD", "refs/heads/main"])
    git_env = os.environ.copy()
    git_env.update(
        {
            "GIT_AUTHOR_NAME": "Blackdog",
            "GIT_AUTHOR_EMAIL": "blackdog@example.com",
            "GIT_COMMITTER_NAME": "Blackdog",
            "GIT_COMMITTER_EMAIL": "blackdog@example.com",
        }
    )
    _run_command(
        ["git", "-C", str(project_root), "commit", "--allow-empty", "-m", "Initialize repository"],
        env=git_env,
    )


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
        "- Treat documented Blackdog CLI commands and artifact files as the integration surface; do not hand-edit backlog state.\n"
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


def create_project(
    project_root: Path,
    *,
    project_name: str,
    blackdog_source: Path | None = None,
    objectives: list[str] | None = None,
    push_objective: list[str] | None = None,
    non_negotiables: list[str] | None = None,
    evidence_requirements: list[str] | None = None,
    release_gates: list[str] | None = None,
) -> tuple[Profile, Path, Path, Path]:
    root = project_root.resolve()
    _ensure_new_project_root(root)
    _initialize_git_repo(root)

    venv_dir = root / ".VE"
    _run_command([str(Path(sys.executable).resolve()), "-m", "venv", str(venv_dir)], cwd=root)
    venv_python = venv_dir / "bin" / "python"
    if not venv_python.is_file():
        raise ScaffoldError(f"Expected virtualenv python at {venv_python}")

    source_root = blackdog_source.resolve() if blackdog_source is not None else _default_blackdog_source()
    if not (source_root / "pyproject.toml").is_file():
        raise ScaffoldError(f"Blackdog source path does not look like a project root: {source_root}")

    _run_command([str(venv_python), "-m", "pip", "install", "-e", str(source_root)], cwd=root)
    profile, skill_file = bootstrap_project(
        root,
        project_name=project_name,
        objectives=objectives,
        push_objective=push_objective,
        non_negotiables=non_negotiables,
        evidence_requirements=evidence_requirements,
        release_gates=release_gates,
    )
    return profile, skill_file, venv_dir, source_root


def refresh_project_skill(profile: Profile) -> Path:
    report = refresh_project_scaffold(profile, render_html=False)
    return Path(report["skill_file"])


def render_project_html(profile: Profile) -> Path:
    refresh_backlog_headers(profile)
    snapshot = build_ui_snapshot(profile)
    rendered_html = render_static_html(snapshot, profile.paths.html_file)
    for alias_path in legacy_html_aliases(profile):
        alias_path.write_text(rendered_html, encoding="utf-8")
    return profile.paths.html_file


def legacy_html_aliases(profile: Profile) -> list[Path]:
    legacy_path = profile.paths.html_file.with_name("backlog-index.html")
    if legacy_path.resolve() == profile.paths.html_file.resolve():
        return []
    return [legacy_path]


def _preferred_cli_command(project_root: Path, executable: str) -> str:
    candidate = project_root / ".VE" / "bin" / executable
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return shlex.quote(f"./.VE/bin/{executable}")
    return executable


def _host_skill_token(profile: Profile) -> str:
    token = profile.paths.skill_dir.name.strip()
    return token or default_host_skill_name(profile.project_name)


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


def _blackdog_managed_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _managed_skill_manifest_path(skill_dir: Path) -> Path:
    return skill_dir / MANAGED_SKILL_MANIFEST


def _load_managed_skill_manifest(skill_dir: Path) -> dict[str, dict[str, str]]:
    manifest_path = _managed_skill_manifest_path(skill_dir)
    if not manifest_path.exists():
        return {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    files = payload.get("files")
    if not isinstance(files, dict):
        return {}
    manifest: dict[str, dict[str, str]] = {}
    for relative_path, row in files.items():
        if not isinstance(relative_path, str) or not isinstance(row, dict):
            continue
        sha256 = row.get("sha256")
        if isinstance(sha256, str) and sha256:
            manifest[relative_path] = {"sha256": sha256}
    return manifest


def _save_managed_skill_manifest(skill_dir: Path, files: dict[str, dict[str, str]]) -> Path:
    manifest_path = _managed_skill_manifest_path(skill_dir)
    manifest_path.write_text(
        json.dumps({"schema_version": 1, "files": files}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def _project_skill_bundle(profile: Profile) -> dict[Path, str]:
    skill_dir = profile.paths.skill_dir
    agents_dir = skill_dir / "agents"
    references_dir = skill_dir / "references"
    agents_dir.mkdir(parents=True, exist_ok=True)
    references_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    agent_file = agents_dir / "openai.yaml"
    task_shaping_reference_file = references_dir / "task-shaping.md"
    skill_name = _host_skill_token(profile)
    display_name = f"Blackdog: {profile.project_name}"
    cli_command = _preferred_cli_command(profile.paths.project_root, "blackdog")
    skill_command = _preferred_cli_command(profile.paths.project_root, "blackdog-skill")
    workflow_guidance = (profile.pm_heuristics.get("skill_usage") or "").strip() or DEFAULT_SKILL_USAGE_HEURISTIC
    summary_focus = (profile.pm_heuristics.get("summary_focus") or "").strip()
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

## Blackdog Layer Contract

- `core` is the durable contract: `blackdog.toml` plus the canonical artifact files under the control root.
- `blackdog proper` is the shipped product surface: the `blackdog` and `blackdog-skill` CLIs, prompt/tune/report helpers, bootstrap/refresh flows, the generated project-local skill, the shipped static HTML board, and supervisor orchestration.
- Optional repo-specific skills, editor integrations, or wrappers should compose through documented CLI behavior and stable artifact/snapshot files rather than private Blackdog Python imports.
- Prefer CLI writes for backlog/runtime state transitions. Treat raw files as durable contracts to read and validate, not an ad hoc mutation surface.

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

- Host skill token: `{skill_name}`
- Skill metadata file: `{skill_metadata_path}`
- UI discovery file: `{skill_discovery_path}`
- Codex discovers this skill from `agents/openai.yaml` under `.codex/skills/<skill-name>/` in the opened repo.
- `agents/openai.yaml` should explicitly mention `${skill_name}` in `interface.default_prompt`.
- Open or refresh the repo in Codex after bootstrap so the skill appears in the available skill list.

## Repo-Specific Planning Guidance

- Workflow policy: {workflow_guidance}
"""
    if summary_focus:
        skill_text += f"\n- Summary focus: {summary_focus}\n"
    skill_text += f"""

## Host Project Creation

- When the user asks to create a brand-new Blackdog repo at a filesystem path, run `{cli_command} create-project --project-root /abs/path --project-name "Repo Name"` from this checkout.
- `create-project` creates the target directory, initializes git, bootstraps a repo-local `.VE`, installs Blackdog from the current checkout, and runs bootstrap so the new repo already has `blackdog.toml`, `AGENTS.md`, and `.codex/skills/{skill_name}/`.
- Use `{cli_command} bootstrap` instead when the target repo already exists or already has its own Python environment prepared.

## Repo Refresh

- Run `{cli_command} refresh` after updating the installed Blackdog package when you want to regenerate the project-local skill files and repo-branded HTML board.
- `refresh` keeps locally modified managed files in place and writes `*.blackdog-new` sidecars with the regenerated version when a managed file has diverged.
- From a Blackdog source checkout, run `blackdog update-repo /abs/path/to/host-repo` to reinstall Blackdog into that repo's `.VE` and then run the same refresh flow.

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

## Prompt Tuning

- Use `{cli_command} prompt --complexity low|medium|high "..."` when you want Blackdog to rewrite a request against this repo's local docs, validation defaults, and WTAM contract before turning it into backlog work.
- `prompt` is intended to help repo-local skills that build on top of Blackdog reuse the same contract and tuning guidance instead of re-explaining the repo from scratch.
- Use `{cli_command} tune --no-task` when you want direct tuning guidance without automatically seeding a backlog task.

## Task Shaping

- Treat a new user request as one candidate deliverable first. Default to one lane and one task unless there is a measured reason to split it.
- Consolidate serial slices that touch the same files, need the same validation, or must land together. Do not create separate tasks for analysis, implementation, cleanup, and verification of the same change.
- Split only when it buys real parallelism: disjoint write sets, independent validation, separate blockers, or clearly separable deliverables that can land independently.
- Before creating or reshaping tasks, estimate total elapsed task time, active edit time, touched paths, validation time, worktree spin-ups, and coordination handoffs. Minimize separate requests first, then add parallelism only when the saved wall-clock time exceeds the extra spin-up and coordination cost.
- When uncertain, under-split first. It is easier to split a live task later than to merge redundant lanes and half-finished work.
- Use [references/task-shaping.md](references/task-shaping.md) when adding, tuning, or restructuring work; it contains the measurement fields and consolidation rubric.

## Static Board

- `{html_path}` renders a wide control board with a `Backlog Control` panel, `Status` panel, paired objective/release-gate tables, `Execution Map`, and `Completed Tasks`.
- `blackdog snapshot` and the embedded HTML payload remain Blackdog-product surfaces, but machine-readable repo/header/plan/task facts flow through the neutral `core_export`; prefer that export for extensions instead of the surrounding board-only projection fields.
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
- Treat the documented CLI plus stable control-root artifacts as the supported integration contract for repo-local adapters and skills.
- Regenerate this skill after profile changes with `{cli_command} refresh` or `{skill_command} refresh backlog --project-root .`.

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
  default_prompt: "Use ${skill_name} to shape or execute repo-specific backlog work for {profile.project_name} through the local Blackdog contract."
"""
    return {
        skill_file: skill_text,
        agent_file: agent_text,
        task_shaping_reference_file: _task_shaping_reference_text(),
    }


def _write_managed_skill_bundle(
    skill_dir: Path,
    bundle: dict[Path, str],
    *,
    force: bool = False,
) -> dict[str, Any]:
    previous_manifest = _load_managed_skill_manifest(skill_dir)
    next_manifest: dict[str, dict[str, str]] = {}
    report: dict[str, Any] = {
        "created": [],
        "updated": [],
        "unchanged": [],
        "preserved_local": [],
    }
    for path in sorted(bundle, key=lambda item: str(item)):
        content = bundle[path]
        relative_path = path.relative_to(skill_dir).as_posix()
        preview_path = path.with_name(path.name + MANAGED_SKILL_PREVIEW_SUFFIX)
        existing_text: str | None = None
        if path.exists():
            try:
                existing_text = path.read_text(encoding="utf-8")
            except OSError:
                existing_text = None
        new_hash = _blackdog_managed_hash(content)
        if force:
            path.write_text(content, encoding="utf-8")
            preview_path.unlink(missing_ok=True)
            if existing_text is None:
                report["created"].append(str(path))
            elif existing_text == content:
                report["unchanged"].append(str(path))
            else:
                report["updated"].append(str(path))
            next_manifest[relative_path] = {"sha256": new_hash}
            continue
        if existing_text is None:
            path.write_text(content, encoding="utf-8")
            preview_path.unlink(missing_ok=True)
            report["created"].append(str(path))
            next_manifest[relative_path] = {"sha256": new_hash}
            continue
        if existing_text == content:
            preview_path.unlink(missing_ok=True)
            report["unchanged"].append(str(path))
            next_manifest[relative_path] = {"sha256": new_hash}
            continue
        existing_hash = _blackdog_managed_hash(existing_text)
        managed_row = previous_manifest.get(relative_path)
        if managed_row is not None and managed_row.get("sha256") == existing_hash:
            path.write_text(content, encoding="utf-8")
            preview_path.unlink(missing_ok=True)
            report["updated"].append(str(path))
            next_manifest[relative_path] = {"sha256": new_hash}
            continue
        preview_path.write_text(content, encoding="utf-8")
        report["preserved_local"].append({"path": str(path), "candidate": str(preview_path)})
        if managed_row is not None:
            next_manifest[relative_path] = managed_row
    manifest_path = _save_managed_skill_manifest(skill_dir, next_manifest)
    report["manifest_file"] = str(manifest_path)
    return report


def generate_project_skill(profile: Profile, *, force: bool = False) -> Path:
    bundle = _project_skill_bundle(profile)
    skill_file = profile.paths.skill_dir / "SKILL.md"
    if skill_file.exists() and not force:
        raise ScaffoldError(f"Refusing to overwrite {skill_file}; pass --force to replace it")
    _write_managed_skill_bundle(profile.paths.skill_dir, bundle, force=True)
    return skill_file


def refresh_project_scaffold(profile: Profile, *, render_html: bool = True) -> dict[str, Any]:
    _ensure_baseline_agents_file(profile.paths.project_root)
    bundle = _project_skill_bundle(profile)
    managed = _write_managed_skill_bundle(profile.paths.skill_dir, bundle, force=False)
    html_file = render_project_html(profile) if render_html else profile.paths.html_file
    return {
        "project_root": str(profile.paths.project_root),
        "profile": str(profile.paths.profile_file),
        "skill_file": str(profile.paths.skill_dir / "SKILL.md"),
        "html_file": str(html_file),
        "legacy_html_files": [str(path) for path in legacy_html_aliases(profile)],
        "managed": managed,
    }


def update_project_repo(project_root: Path, *, blackdog_source: Path | None = None) -> dict[str, Any]:
    root = project_root.resolve()
    venv_python = root / ".VE" / "bin" / "python"
    if not venv_python.is_file():
        raise ScaffoldError(f"Target repo is missing a repo-local virtualenv: {venv_python}")
    source_root = blackdog_source.resolve() if blackdog_source is not None else _default_blackdog_source()
    if not (source_root / "pyproject.toml").is_file():
        raise ScaffoldError(f"Blackdog source path does not look like a project root: {source_root}")
    _run_command([str(venv_python), "-m", "pip", "install", "-e", str(source_root)], cwd=root)
    profile_file = root / "blackdog.toml"
    if not profile_file.exists():
        raise ScaffoldError(f"Target repo is missing a Blackdog profile: {profile_file}")
    profile = load_profile(root)
    report = refresh_project_scaffold(profile)
    report["venv_python"] = str(venv_python)
    report["blackdog_source"] = str(source_root)
    return report
