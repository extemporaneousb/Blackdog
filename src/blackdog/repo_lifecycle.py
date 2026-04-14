from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess

from blackdog.contract import legacy_managed_skill_relative_path, managed_skill_name, managed_skill_relative_path
from blackdog.handlers import HandlerPlanSummary, execute_repo_handlers, plan_repo_handlers
from blackdog_core.profile import (
    RepoProfile,
    ConfigError,
    ensure_default_handlers_in_profile,
    load_profile,
    write_default_profile,
)


LEGACY_CONTROL_ARTIFACTS = (
    "backlog-index.html",
    "backlog-state.json",
    "backlog.md",
    "blackdog-backlog.html",
    "inbox.jsonl",
    "supervisor-runs",
    "task-results",
    "threads",
    "tracked-installs.json",
)
AGENTS_FILE_NAME = "AGENTS.md"
AGENTS_MANAGED_BEGIN = "<!-- BLACKDOG MANAGED CONTRACT:BEGIN -->"
AGENTS_MANAGED_END = "<!-- BLACKDOG MANAGED CONTRACT:END -->"


class RepoLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RepoLifecycleResult:
    action: str
    project_root: str
    source_root: str | None
    source_mode: str | None
    profile_path: str | None
    skill_path: str | None
    ve_path: str | None
    blackdog_path: str | None
    handlers: HandlerPlanSummary | None
    created: tuple[str, ...]
    updated: tuple[str, ...]
    removed: tuple[str, ...]
    preserved: tuple[str, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "project_root": self.project_root,
            "source_root": self.source_root,
            "source_mode": self.source_mode,
            "profile_path": self.profile_path,
            "skill_path": self.skill_path,
            "ve_path": self.ve_path,
            "blackdog_path": self.blackdog_path,
            "handlers": self.handlers.to_dict() if self.handlers is not None else None,
            "created": list(self.created),
            "updated": list(self.updated),
            "removed": list(self.removed),
            "preserved": list(self.preserved),
            "notes": list(self.notes),
        }


def _run_command(*args: str) -> None:
    completed = subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise RepoLifecycleError(f"{' '.join(args)} failed: {detail}")


def _run_git(project_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise RepoLifecycleError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _resolve_repo_root(project_root: Path) -> Path:
    try:
        return Path(_run_git(project_root.resolve(), "rev-parse", "--show-toplevel")).resolve()
    except RepoLifecycleError as exc:
        raise RepoLifecycleError(f"{project_root.resolve()} is not inside a git repo") from exc


def _managed_skill_path(profile: RepoProfile) -> Path:
    return (profile.paths.project_root / managed_skill_relative_path(profile)).resolve()


def _legacy_managed_skill_path(profile: RepoProfile) -> Path:
    return (profile.paths.project_root / legacy_managed_skill_relative_path()).resolve()


def _repo_agents_path(profile: RepoProfile) -> Path:
    return (profile.paths.project_root / AGENTS_FILE_NAME).resolve()


def _remove_if_empty(path: Path, *, stop_at: Path) -> None:
    current = path.resolve()
    limit = stop_at.resolve()
    while current != limit and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _migrate_legacy_skill_path(profile: RepoProfile) -> tuple[str, ...]:
    managed_skill = _managed_skill_path(profile)
    skills_root = (profile.paths.project_root / ".codex" / "skills").resolve()
    obsolete_paths = {
        _legacy_managed_skill_path(profile),
        (profile.paths.project_root / managed_skill_relative_path(profile.paths.project_root)).resolve(),
    }
    removed: list[str] = []
    for candidate in obsolete_paths:
        if candidate == managed_skill or not candidate.exists():
            continue
        candidate.unlink()
        _remove_if_empty(candidate.parent, stop_at=skills_root)
        removed.append(str(candidate))
    return tuple(sorted(removed))


def _render_repo_agents_contract(profile: RepoProfile) -> str:
    routed_docs = tuple(item for item in profile.doc_routing_defaults if item != AGENTS_FILE_NAME)
    lines = [
        AGENTS_MANAGED_BEGIN,
        "## Blackdog Contract",
        "",
        "This section is managed by `blackdog repo install` and `blackdog repo refresh`.",
        "Keep repo-specific requirements outside this block.",
        "",
        "- Use the repo-local `./.VE/bin/blackdog` when it exists instead of mutating Blackdog control files by hand.",
        "- `blackdog.toml` is the machine-readable source of truth for handler setup and routed docs.",
        "- Before any repo edit you intend to keep, run `./.VE/bin/blackdog worktree preflight --project-root .`.",
        "- If preflight reports `primary worktree: yes`, do not keep implementation edits in that checkout; start or enter a branch-backed task worktree first.",
        "- Analysis-only work may stay in the current checkout.",
        "- `.VE/` is unversioned and bound to one worktree path; create one per worktree and do not copy virtualenvs between worktrees.",
        "- Prefer `./.VE/bin/blackdog task begin --project-root . --actor AGENT --prompt \"...\" --prompt-mode raw` for the normal same-thread path.",
        "- Use `./.VE/bin/blackdog worktree preview --project-root . ...` before `worktree start` when you need to inspect the WTAM plan first.",
    ]
    if routed_docs:
        lines.extend(("", "Review these routed docs before editing when they apply:"))
        lines.extend(f"- `{item}`" for item in routed_docs)
    if profile.validation_commands:
        lines.extend(("", "Run the narrowest relevant validation after changes. Repo defaults:"))
        lines.extend(f"- `{command}`" for command in profile.validation_commands)
    lines.extend(("", AGENTS_MANAGED_END))
    return "\n".join(lines).rstrip() + "\n"


def _render_repo_agents_file(profile: RepoProfile, existing_text: str | None = None) -> str:
    contract = _render_repo_agents_contract(profile).rstrip()
    if existing_text is None or not existing_text.strip():
        return (
            "# AGENTS\n\n"
            "Keep repo-specific requirements outside the managed Blackdog section below.\n\n"
            f"{contract}\n"
        )
    start = existing_text.find(AGENTS_MANAGED_BEGIN)
    end = existing_text.find(AGENTS_MANAGED_END)
    if start != -1 and end != -1 and end > start:
        end += len(AGENTS_MANAGED_END)
        prefix = existing_text[:start].rstrip()
        suffix = existing_text[end:].lstrip("\n")
        parts = [part for part in (prefix, contract, suffix) if part]
        return "\n\n".join(parts).rstrip() + "\n"
    return existing_text.rstrip() + "\n\n" + contract + "\n"


def _write_repo_agents(profile: RepoProfile) -> tuple[Path, str]:
    agents_path = _repo_agents_path(profile)
    existing_text = agents_path.read_text(encoding="utf-8") if agents_path.exists() else None
    rendered = _render_repo_agents_file(profile, existing_text=existing_text)
    if existing_text == rendered:
        return agents_path, "preserved"
    agents_path.parent.mkdir(parents=True, exist_ok=True)
    agents_path.write_text(rendered, encoding="utf-8")
    return agents_path, "created" if existing_text is None else "updated"


def render_repo_skill(profile: RepoProfile) -> str:
    docs = "\n".join(f"- `{item}`" for item in profile.doc_routing_defaults)
    skill_name = managed_skill_name(profile)
    return (
        "---\n"
        f"name: {skill_name}\n"
        f'description: "Use the repo-local Blackdog CLI and contract for {profile.project_name}."\n'
        "---\n\n"
        f"# Repo Skill: {profile.project_name}\n\n"
        "Use the repo-local Blackdog CLI instead of mutating control-root files by hand.\n"
        "`blackdog.toml` is the machine-readable source of truth for handler setup and routed docs.\n"
        "Keep hard repo rules in `AGENTS.md` and the routed docs below; this skill is the generated Blackdog summary.\n\n"
        "## CLI Entry Point\n\n"
        "- `./.VE/bin/blackdog`\n\n"
        "## Shipped Workflow Families\n\n"
        "- repo lifecycle: `repo install`, `repo update`, `repo refresh`, `prompt preview`, `prompt tune`, `attempts summary`, `attempts table`\n"
        "- workset/task runtime: `workset put`, `summary`, `next --workset`, `snapshot`\n"
        "- same-thread task execution: `task begin`, `task show`, `task land`, `task close`, `task cleanup`\n"
        "- WTAM kept-change execution: `worktree preflight`, `worktree preview`, `worktree start`, `worktree show`, `worktree land`, `worktree close`, `worktree cleanup`\n\n"
        "## Repo Lifecycle Flow\n\n"
        "1. `./.VE/bin/blackdog repo update --project-root .`\n"
        "2. `./.VE/bin/blackdog repo refresh --project-root .`\n"
        "3. `./.VE/bin/blackdog prompt preview --project-root . --prompt \"...\"`\n"
        "4. `./.VE/bin/blackdog prompt tune --project-root . --prompt \"...\"`\n"
        "5. review the routed docs below before editing\n\n"
        "## Same-Thread Task Flow\n\n"
        "1. `./.VE/bin/blackdog task begin --project-root . --actor AGENT --prompt \"...\" --prompt-mode raw`\n"
        "2. make kept changes only inside the returned task worktree\n"
        "3. `./.VE/bin/blackdog task land --project-root . --summary \"...\"`\n"
        "4. if recovery is needed from that task worktree, use `./.VE/bin/blackdog task show --project-root .` or `./.VE/bin/blackdog task close --project-root . --status blocked|failed|abandoned --summary \"...\"`\n"
        "5. if the task workspace was retained, use `./.VE/bin/blackdog task cleanup --project-root .`\n\n"
        "## Explicit Planned-Task Flow\n\n"
        "1. `./.VE/bin/blackdog summary --project-root .`\n"
        "2. `./.VE/bin/blackdog next --project-root . --workset WORKSET`\n"
        "3. `./.VE/bin/blackdog worktree preflight --project-root .`\n"
        "4. `./.VE/bin/blackdog worktree preview --project-root . --workset WORKSET --task TASK --actor AGENT --prompt \"...\"`\n"
        "5. `./.VE/bin/blackdog worktree start --project-root . --workset WORKSET --task TASK --actor AGENT --prompt \"...\"`\n"
        "6. make kept changes only inside that task worktree\n"
        "7. `./.VE/bin/blackdog worktree land --project-root . --workset WORKSET --task TASK --actor AGENT --summary \"...\"`\n\n"
        "## Docs To Review\n\n"
        f"{docs}\n"
    )


def _write_repo_skill(profile: RepoProfile, *, overwrite: bool) -> tuple[Path, bool]:
    skill_path = _managed_skill_path(profile)
    if skill_path.exists() and not overwrite:
        return skill_path, False
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(render_repo_skill(profile), encoding="utf-8")
    return skill_path, True


def _prune_legacy_control_artifacts(profile: RepoProfile) -> tuple[str, ...]:
    removed: list[str] = []
    for name in LEGACY_CONTROL_ARTIFACTS:
        path = (profile.paths.control_dir / name).resolve()
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(str(path))
    return tuple(removed)


def _require_profile(project_root: Path) -> RepoProfile:
    profile_path = (project_root / "blackdog.toml").resolve()
    if not profile_path.exists():
        raise RepoLifecycleError(f"{profile_path} is missing; run `blackdog repo install` first")
    try:
        return load_profile(project_root)
    except ConfigError as exc:
        raise RepoLifecycleError(str(exc)) from exc


def _apply_handler_actions(
    summary: HandlerPlanSummary,
    *,
    created: list[str],
    updated: list[str],
    preserved: list[str],
    notes: list[str],
) -> None:
    for action in summary.actions:
        if action.target_path is None:
            continue
        if action.status == "created":
            created.append(action.target_path)
        elif action.status == "updated":
            updated.append(action.target_path)
        elif action.status in {"preserved", "validated"}:
            preserved.append(action.target_path)
        elif action.status == "blocked":
            notes.append(action.message)
    if summary.remediation:
        notes.append(summary.remediation)


def install_repo(
    project_root: Path,
    *,
    project_name: str | None = None,
    source_root: str | None = None,
) -> RepoLifecycleResult:
    repo_root = _resolve_repo_root(project_root)
    created: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    preserved: list[str] = []
    notes: list[str] = []

    profile_path = (repo_root / "blackdog.toml").resolve()
    if not profile_path.exists():
        write_default_profile(repo_root, project_name or repo_root.name)
        created.append(str(profile_path))
    else:
        preserved.append(str(profile_path))
        if project_name:
            notes.append("ignored --project-name because blackdog.toml already exists")

    if ensure_default_handlers_in_profile(profile_path):
        updated.append(str(profile_path))
    profile = load_profile(repo_root)
    handler_summary = execute_repo_handlers(
        profile,
        operation="repo-install",
        source_root=source_root,
        update_managed_source=False,
    )
    _apply_handler_actions(
        handler_summary,
        created=created,
        updated=updated,
        preserved=preserved,
        notes=notes,
    )

    agents_path, agents_status = _write_repo_agents(profile)
    if agents_status == "created":
        created.append(str(agents_path))
    elif agents_status == "updated":
        updated.append(str(agents_path))
    else:
        preserved.append(str(agents_path))

    skill_path, skill_changed = _write_repo_skill(profile, overwrite=False)
    if skill_changed:
        created.append(str(skill_path))
    else:
        preserved.append(str(skill_path))
    legacy_skill = _legacy_managed_skill_path(profile)
    if legacy_skill.exists() and legacy_skill != skill_path:
        preserved.append(str(legacy_skill))
        notes.append("legacy repo skill path still exists; run `blackdog repo refresh` to migrate it to the repo-slug skill path")

    return RepoLifecycleResult(
        action="install",
        project_root=str(profile.paths.project_root),
        source_root=handler_summary.source_root,
        source_mode=handler_summary.source_mode,
        profile_path=str(profile.paths.profile_file),
        skill_path=str(skill_path),
        ve_path=handler_summary.root_ve_path,
        blackdog_path=handler_summary.blackdog_path,
        handlers=handler_summary,
        created=tuple(dict.fromkeys(created)),
        updated=tuple(dict.fromkeys(updated)),
        removed=tuple(dict.fromkeys(removed)),
        preserved=tuple(dict.fromkeys(preserved)),
        notes=tuple(notes),
    )


def update_repo(
    project_root: Path,
    *,
    source_root: str | None = None,
) -> RepoLifecycleResult:
    repo_root = _resolve_repo_root(project_root)
    profile = _require_profile(repo_root)
    created: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    preserved: list[str] = [str(profile.paths.profile_file)]
    notes: list[str] = []
    if not profile.handlers_explicit:
        notes.append("profile uses synthesized default handlers; run `blackdog repo install` to pin handler blocks explicitly")
    handler_summary = execute_repo_handlers(
        profile,
        operation="repo-update",
        source_root=source_root,
        update_managed_source=True,
    )
    _apply_handler_actions(
        handler_summary,
        created=created,
        updated=updated,
        preserved=preserved,
        notes=notes,
    )

    skill_path = _managed_skill_path(profile)
    if skill_path.exists():
        preserved.append(str(skill_path))
    else:
        legacy_skill = _legacy_managed_skill_path(profile)
        if legacy_skill.exists() and legacy_skill != skill_path:
            preserved.append(str(legacy_skill))
            notes.append("repo skill is still at the legacy blackdog path; run `blackdog repo refresh` to migrate it")
        else:
            notes.append("repo skill is missing; run `blackdog repo refresh` to regenerate it")

    return RepoLifecycleResult(
        action="update",
        project_root=str(profile.paths.project_root),
        source_root=handler_summary.source_root,
        source_mode=handler_summary.source_mode,
        profile_path=str(profile.paths.profile_file),
        skill_path=str(skill_path),
        ve_path=handler_summary.root_ve_path,
        blackdog_path=handler_summary.blackdog_path,
        handlers=handler_summary,
        created=tuple(dict.fromkeys(created)),
        updated=tuple(dict.fromkeys(updated)),
        removed=tuple(dict.fromkeys(removed)),
        preserved=tuple(dict.fromkeys(preserved)),
        notes=tuple(notes),
    )


def refresh_repo(project_root: Path) -> RepoLifecycleResult:
    repo_root = _resolve_repo_root(project_root)
    profile = _require_profile(repo_root)
    handler_summary = plan_repo_handlers(profile, operation="repo-refresh")
    agents_path, agents_status = _write_repo_agents(profile)
    skill_path, skill_changed = _write_repo_skill(profile, overwrite=True)
    removed = list(_prune_legacy_control_artifacts(profile))
    removed.extend(_migrate_legacy_skill_path(profile))
    preserved = [str(profile.paths.profile_file)]
    updated = []
    if agents_status == "created":
        updated.append(str(agents_path))
    elif agents_status == "updated":
        updated.append(str(agents_path))
    else:
        preserved.append(str(agents_path))
    if skill_changed:
        updated.append(str(skill_path))
    notes: list[str] = []
    _apply_handler_actions(
        handler_summary,
        created=[],
        updated=[],
        preserved=preserved,
        notes=notes,
    )
    return RepoLifecycleResult(
        action="refresh",
        project_root=str(profile.paths.project_root),
        source_root=handler_summary.source_root,
        source_mode=handler_summary.source_mode,
        profile_path=str(profile.paths.profile_file),
        skill_path=str(skill_path),
        ve_path=handler_summary.root_ve_path,
        blackdog_path=handler_summary.blackdog_path,
        handlers=handler_summary,
        created=(),
        updated=tuple(updated),
        removed=tuple(removed),
        preserved=tuple(preserved),
        notes=tuple(notes),
    )


def render_repo_lifecycle_text(result: RepoLifecycleResult) -> str:
    lines = [
        f"[blackdog-repo] action: {result.action}",
        f"[blackdog-repo] project root: {result.project_root}",
    ]
    if result.source_root:
        lines.append(f"[blackdog-repo] source root: {result.source_root}")
    if result.source_mode:
        lines.append(f"[blackdog-repo] source mode: {result.source_mode}")
    if result.profile_path:
        lines.append(f"[blackdog-repo] profile: {result.profile_path}")
    if result.skill_path:
        lines.append(f"[blackdog-repo] skill: {result.skill_path}")
    if result.blackdog_path:
        lines.append(f"[blackdog-repo] launcher: {result.blackdog_path}")
    if result.handlers is not None:
        if result.handlers.runtime_mode:
            lines.append(f"[blackdog-repo] runtime mode: {result.handlers.runtime_mode}")
        if result.handlers.script_policy:
            lines.append(f"[blackdog-repo] script policy: {result.handlers.script_policy}")
        for action in result.handlers.actions:
            target = f" -> {action.target_path}" if action.target_path else ""
            timing = "" if action.elapsed_ms is None else f" [{action.elapsed_ms}ms]"
            lines.append(
                f"[blackdog-repo] handler {action.handler_id}: {action.action} {action.status}{target}{timing} ({action.message})"
            )
    if result.created:
        lines.append(f"[blackdog-repo] created: {', '.join(result.created)}")
    if result.updated:
        lines.append(f"[blackdog-repo] updated: {', '.join(result.updated)}")
    if result.removed:
        lines.append(f"[blackdog-repo] removed: {', '.join(result.removed)}")
    if result.preserved:
        lines.append(f"[blackdog-repo] preserved: {', '.join(result.preserved)}")
    for note in result.notes:
        lines.append(f"[blackdog-repo] note: {note}")
    return "\n".join(lines) + "\n"


__all__ = [
    "RepoLifecycleError",
    "RepoLifecycleResult",
    "install_repo",
    "refresh_repo",
    "render_repo_lifecycle_text",
    "render_repo_skill",
    "update_repo",
]
