from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess

from blackdog.handlers import HandlerPlanSummary, execute_repo_handlers, plan_repo_handlers
from blackdog_core.profile import (
    RepoProfile,
    ConfigError,
    ensure_default_handlers_in_profile,
    load_profile,
    write_default_profile,
)


MANAGED_SKILL_RELATIVE_PATH = Path(".codex") / "skills" / "blackdog" / "SKILL.md"
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


def render_repo_skill(profile: RepoProfile) -> str:
    docs = "\n".join(f"- `{item}`" for item in profile.doc_routing_defaults)
    return (
        "---\n"
        'name: blackdog\n'
        f'description: "Use the repo-local Blackdog CLI and contract for {profile.project_name}."\n'
        "---\n\n"
        f"# Blackdog: {profile.project_name}\n\n"
        "Use the repo-local Blackdog CLI instead of mutating control-root files by hand.\n"
        "The repo-local blackdog.toml handler blocks own env/runtime setup.\n\n"
        "## CLI Entry Point\n\n"
        "- `./.VE/bin/blackdog`\n\n"
        "## Shipped Workflow Families\n\n"
        "- repo lifecycle: `repo install`, `repo update`, `repo refresh`, `prompt preview`, `prompt tune`, `attempts summary`, `attempts table`\n"
        "- workset/task runtime: `workset put`, `summary`, `next --workset`, `snapshot`\n"
        "- WTAM kept-change execution: `worktree preflight`, `worktree preview`, `worktree start`, `worktree land`, `worktree cleanup`\n\n"
        "## Repo Lifecycle Flow\n\n"
        "1. `./.VE/bin/blackdog repo update --project-root .`\n"
        "2. `./.VE/bin/blackdog repo refresh --project-root .`\n"
        "3. `./.VE/bin/blackdog prompt preview --project-root . --prompt \"...\"`\n"
        "4. `./.VE/bin/blackdog prompt tune --project-root . --prompt \"...\"`\n"
        "5. review the routed docs below before editing\n\n"
        "## WTAM Flow\n\n"
        "1. `./.VE/bin/blackdog summary --project-root .`\n"
        "2. `./.VE/bin/blackdog next --project-root . --workset WORKSET`\n"
        "3. `./.VE/bin/blackdog worktree preflight --project-root .`\n"
        "4. `./.VE/bin/blackdog worktree preview --project-root . --workset WORKSET --task TASK --actor AGENT --prompt \"...\"`\n"
        "5. `./.VE/bin/blackdog worktree start --project-root . --workset WORKSET --task TASK --actor AGENT --prompt \"...\"`\n"
        "6. make kept changes only inside that task worktree\n"
        "7. `./.VE/bin/blackdog worktree land --project-root . --workset WORKSET --task TASK --actor AGENT`\n"
        "8. `./.VE/bin/blackdog worktree cleanup --project-root . --workset WORKSET --task TASK`\n\n"
        "## Docs To Review\n\n"
        f"{docs}\n"
    )


def _write_repo_skill(profile: RepoProfile, *, overwrite: bool) -> tuple[Path, bool]:
    skill_path = (profile.paths.project_root / MANAGED_SKILL_RELATIVE_PATH).resolve()
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

    skill_path, skill_changed = _write_repo_skill(profile, overwrite=False)
    if skill_changed:
        created.append(str(skill_path))
    else:
        preserved.append(str(skill_path))

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

    skill_path = (profile.paths.project_root / MANAGED_SKILL_RELATIVE_PATH).resolve()
    if skill_path.exists():
        preserved.append(str(skill_path))
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
    skill_path, skill_changed = _write_repo_skill(profile, overwrite=True)
    removed = list(_prune_legacy_control_artifacts(profile))
    preserved = [str(profile.paths.profile_file)]
    updated = [str(skill_path)] if skill_changed else []
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
