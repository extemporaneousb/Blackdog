from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import tomllib

from blackdog.contract import (
    LEGACY_MANAGED_SKILL_NAME,
    legacy_managed_skill_relative_path,
    managed_skill_name,
    managed_skill_relative_path,
)
from blackdog.handlers import HandlerPlanSummary, execute_repo_handlers, plan_repo_handlers
from blackdog_core.profile import (
    RepoProfile,
    ConfigError,
    suggest_default_doc_routing,
    ensure_default_handlers_in_profile,
    load_profile,
    write_default_profile,
)


LEGACY_BACKLOG_CONTROL_ARTIFACTS = (
    "backlog-index.html",
    "backlog-state.json",
    "backlog.md",
    "blackdog-backlog.html",
    "inbox.jsonl",
    "task-results",
    "threads",
    "tracked-installs.json",
)
REMOVED_ORCHESTRATION_CONTROL_ARTIFACTS = ("supervisor-runs",)
AGENTS_FILE_NAME = "AGENTS.md"
AGENTS_MANAGED_BEGIN = "<!-- BLACKDOG MANAGED CONTRACT:BEGIN -->"
AGENTS_MANAGED_END = "<!-- BLACKDOG MANAGED CONTRACT:END -->"
_ENTRYPOINT_DOCS = (
    AGENTS_FILE_NAME,
    "docs/AGENT_START.md",
    "docs/AGENT_WORKFLOW.md",
    "README.md",
)
_SKIPPED_SCAN_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".VE",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    "coverage",
}


class RepoLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RepoConversionFinding:
    code: str
    severity: str
    message: str
    paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "paths": list(self.paths),
        }


@dataclass(frozen=True, slots=True)
class RepoConversionStep:
    phase: str
    summary: str
    details: str | None = None
    paths: tuple[str, ...] = ()
    command: str | None = None
    managed_by_blackdog: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "summary": self.summary,
            "details": self.details,
            "paths": list(self.paths),
            "command": self.command,
            "managed_by_blackdog": self.managed_by_blackdog,
        }


@dataclass(frozen=True, slots=True)
class RepoSkillSurface:
    name: str
    skill_path: str
    discovery_path: str | None
    managed: bool
    delegates_to_managed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "skill_path": self.skill_path,
            "discovery_path": self.discovery_path,
            "managed": self.managed,
            "delegates_to_managed": self.delegates_to_managed,
        }


@dataclass(frozen=True, slots=True)
class RepoConversionAnalysis:
    action: str
    project_root: str
    repo_root: str
    project_name: str
    in_git_repo: bool
    conversion_status: str
    profile_exists: bool
    profile_path: str | None
    profile_error: str | None
    current_doc_routing: tuple[str, ...]
    suggested_doc_routing: tuple[str, ...]
    agents_path: str
    managed_agents_block_present: bool
    ve_path: str
    ve_exists: bool
    blackdog_path: str
    blackdog_launcher_exists: bool
    managed_skill_path: str
    managed_skill_exists: bool
    legacy_skill_path: str
    legacy_skill_exists: bool
    entrypoint_docs: tuple[str, ...]
    agent_docs: tuple[str, ...]
    package_agent_docs: tuple[str, ...]
    skills: tuple[RepoSkillSurface, ...]
    findings: tuple[RepoConversionFinding, ...]
    proposed_steps: tuple[RepoConversionStep, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "project_root": self.project_root,
            "repo_root": self.repo_root,
            "project_name": self.project_name,
            "in_git_repo": self.in_git_repo,
            "conversion_status": self.conversion_status,
            "profile_exists": self.profile_exists,
            "profile_path": self.profile_path,
            "profile_error": self.profile_error,
            "current_doc_routing": list(self.current_doc_routing),
            "suggested_doc_routing": list(self.suggested_doc_routing),
            "agents_path": self.agents_path,
            "managed_agents_block_present": self.managed_agents_block_present,
            "ve_path": self.ve_path,
            "ve_exists": self.ve_exists,
            "blackdog_path": self.blackdog_path,
            "blackdog_launcher_exists": self.blackdog_launcher_exists,
            "managed_skill_path": self.managed_skill_path,
            "managed_skill_exists": self.managed_skill_exists,
            "legacy_skill_path": self.legacy_skill_path,
            "legacy_skill_exists": self.legacy_skill_exists,
            "entrypoint_docs": list(self.entrypoint_docs),
            "agent_docs": list(self.agent_docs),
            "package_agent_docs": list(self.package_agent_docs),
            "skills": [item.to_dict() for item in self.skills],
            "findings": [item.to_dict() for item in self.findings],
            "proposed_steps": [item.to_dict() for item in self.proposed_steps],
            "notes": list(self.notes),
        }


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


@dataclass(frozen=True, slots=True)
class RepoScaffoldResult:
    action: str
    dry_run: bool
    target_root: str
    project_name: str
    like_root: str | None
    source_root: str | None
    planned_seed_files: tuple[str, ...]
    created: tuple[str, ...]
    preserved: tuple[str, ...]
    initialized_git: bool
    install_result: RepoLifecycleResult | None
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "dry_run": self.dry_run,
            "target_root": self.target_root,
            "project_name": self.project_name,
            "like_root": self.like_root,
            "source_root": self.source_root,
            "planned_seed_files": list(self.planned_seed_files),
            "created": list(self.created),
            "preserved": list(self.preserved),
            "initialized_git": self.initialized_git,
            "install_result": self.install_result.to_dict() if self.install_result is not None else None,
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


def _resolve_repo_root_or_none(project_root: Path) -> Path | None:
    try:
        return _resolve_repo_root(project_root)
    except RepoLifecycleError:
        return None


def _is_git_repo_root(project_root: Path) -> bool:
    if not project_root.exists():
        return False
    try:
        return _resolve_repo_root(project_root) == project_root.resolve()
    except RepoLifecycleError:
        return False


def _looks_like_blackdog_source_checkout(root: Path) -> bool:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    if not (root / "src" / "blackdog_cli" / "main.py").is_file():
        return False
    try:
        payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return str((payload.get("project") or {}).get("name") or "") == "blackdog"


def _repo_visible_lifecycle_paths(
    project_root: Path,
    *,
    created: list[str],
    updated: list[str],
    removed: list[str],
) -> tuple[str, ...]:
    visible: list[str] = []
    for raw_path in (*created, *updated, *removed):
        path = Path(raw_path)
        try:
            relative = path.resolve().relative_to(project_root.resolve())
        except ValueError:
            continue
        if not relative.parts or relative.parts[0] in {".VE", ".git"}:
            continue
        visible.append(str(path))
    return tuple(dict.fromkeys(visible))


def _append_repo_visible_dirty_note(
    notes: list[str],
    project_root: Path,
    *,
    created: list[str],
    updated: list[str],
    removed: list[str],
) -> None:
    changed = _repo_visible_lifecycle_paths(project_root, created=created, updated=updated, removed=removed)
    if not changed:
        return
    notes.append(
        "repo lifecycle changed managed worktree files; the primary checkout stays dirty until "
        "these changes are committed, landed, reverted, or explicitly reported; run `git status --short` before finishing"
    )


def _current_blackdog_source_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    if _looks_like_blackdog_source_checkout(candidate):
        return candidate
    return None


def _stable_blackdog_source_root() -> Path | None:
    candidate = _current_blackdog_source_root()
    if candidate is None:
        return None
    if (candidate / ".git").is_dir():
        return candidate
    try:
        output = _run_git(candidate, "worktree", "list", "--porcelain")
    except RepoLifecycleError:
        return None
    for line in output.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line.split(" ", 1)[1]).resolve()
        if _looks_like_blackdog_source_checkout(path) and (path / ".git").is_dir():
            return path
    return None


def _iter_repo_relative_files(repo_root: Path) -> tuple[str, ...]:
    discovered: list[str] = []
    for current_root, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(name for name in dirnames if name not in _SKIPPED_SCAN_DIRS)
        root_path = Path(current_root)
        for filename in sorted(filenames):
            candidate = root_path / filename
            discovered.append(candidate.relative_to(repo_root).as_posix())
    return tuple(discovered)


def _find_agent_docs(repo_root: Path) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    agent_docs = [
        relative_path
        for relative_path in _iter_repo_relative_files(repo_root)
        if Path(relative_path).name == AGENTS_FILE_NAME
        or (Path(relative_path).name.startswith("AGENT") and Path(relative_path).suffix == ".md")
    ]
    entrypoint_docs = tuple(path for path in _ENTRYPOINT_DOCS if (repo_root / path).is_file())
    package_agent_docs = tuple(
        path
        for path in agent_docs
        if Path(path).name == AGENTS_FILE_NAME and path != AGENTS_FILE_NAME
    )
    return tuple(agent_docs), entrypoint_docs, package_agent_docs


def _merge_suggested_doc_routing(repo_root: Path, current: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    for candidate in ("AGENTS.md", *current, *suggest_default_doc_routing(repo_root)):
        if candidate != "AGENTS.md" and not (repo_root / candidate).is_file():
            continue
        if candidate not in merged:
            merged.append(candidate)
    return tuple(merged)


def _find_skill_surfaces(
    repo_root: Path,
    *,
    managed_name: str,
) -> tuple[RepoSkillSurface, ...]:
    skills_root = repo_root / ".codex" / "skills"
    if not skills_root.is_dir():
        return ()
    managed_token = f"${managed_name}"
    surfaces: list[RepoSkillSurface] = []
    for child in sorted(item for item in skills_root.iterdir() if item.is_dir()):
        skill_path = child / "SKILL.md"
        if not skill_path.is_file():
            continue
        discovery_path = child / "agents" / "openai.yaml"
        skill_text = skill_path.read_text(encoding="utf-8")
        discovery_text = discovery_path.read_text(encoding="utf-8") if discovery_path.is_file() else ""
        surfaces.append(
            RepoSkillSurface(
                name=child.name,
                skill_path=str(skill_path.resolve()),
                discovery_path=str(discovery_path.resolve()) if discovery_path.is_file() else None,
                managed=child.name == managed_name,
                delegates_to_managed=managed_token in skill_text or managed_token in discovery_text,
            )
        )
    return tuple(surfaces)


def _shell_command(*parts: str) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def _recommended_install_command(repo_root: Path) -> str:
    parts = ["./.VE/bin/blackdog", "repo", "install", "--project-root", str(repo_root)]
    source_root = _stable_blackdog_source_root()
    if source_root is not None and source_root != repo_root:
        parts.extend(["--source-root", str(source_root)])
    return _shell_command(*parts)


def _recommended_refresh_command(repo_root: Path) -> str:
    return _shell_command("./.VE/bin/blackdog", "repo", "refresh", "--project-root", str(repo_root))


def _recommended_scaffold_install_command(repo_root: Path, *, source_root: str | None) -> str:
    parts = ["./.VE/bin/blackdog", "repo", "install", "--project-root", str(repo_root)]
    if source_root:
        parts.extend(["--source-root", str(Path(source_root).resolve())])
    else:
        stable_source = _stable_blackdog_source_root()
        if stable_source is not None and stable_source != repo_root:
            parts.extend(["--source-root", str(stable_source)])
    return _shell_command(*parts)


def _managed_skill_path(profile: RepoProfile) -> Path:
    return (profile.paths.project_root / managed_skill_relative_path(profile)).resolve()


def _managed_skill_metadata_path(profile: RepoProfile) -> Path:
    return _managed_skill_path(profile).parent / "agents" / "openai.yaml"


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


def _yaml_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass(frozen=True, slots=True)
class RepoSkillWriteResult:
    skill_path: Path
    metadata_path: Path
    changed: tuple[Path, ...]
    removed: tuple[Path, ...]


def _prune_obsolete_managed_skill_dirs(profile: RepoProfile) -> tuple[str, ...]:
    managed_skill = _managed_skill_path(profile)
    skills_root = (profile.paths.project_root / ".codex" / "skills").resolve()
    removed: list[str] = []
    if not skills_root.is_dir():
        return ()
    for child in sorted(item for item in skills_root.iterdir() if item.is_dir()):
        if child.resolve() == managed_skill.parent.resolve():
            continue
        marker = child / ".blackdog-managed.json"
        skill_path = child / "SKILL.md"
        should_remove = (
            child.name == LEGACY_MANAGED_SKILL_NAME
            or marker.exists()
            or (child.name.endswith("-supervisor") and skill_path.is_file())
        )
        if not should_remove:
            continue
        shutil.rmtree(child)
        removed.append(str(child))
    _remove_if_empty(skills_root, stop_at=profile.paths.project_root)
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
        "- Normal repo-skill implementation uses `./.VE/bin/blackdog task begin --project-root . --actor AGENT --prompt-file EXECUTION_PROMPT --prompt-mode skill --user-prompt-file USER_PROMPT`.",
        "- Abandoned work is canceled by default; use `task reopen` only when the work should re-enter the normal queue.",
        "- Use `./.VE/bin/blackdog worktree preview --project-root . ...` before `worktree start` when you need to inspect the WTAM plan first.",
        "- Do not launch an external browser, use macOS `open`, use `xdg-open`, or run headed Playwright/browser sessions for agent verification unless the user explicitly asks for a user-visible browser. Prefer Codex in-app browser tools or headless evidence.",
        "- After `repo install`, `repo update`, or `repo refresh`, run `git status --short`; commit or land managed repo changes, or report the checkout as intentionally dirty before finishing.",
        "- Before finishing implementation work, re-check branch and dirty state. Do not leave uncommitted changes from your work; if committing or landing, make sure the result is on the primary `main` branch unless the user explicitly requested another branch.",
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
    blackdog_source_skill = _looks_like_blackdog_source_checkout(profile.paths.project_root)
    scaffold_workflow = ""
    repo_lifecycle_surface = "`repo analyze`, `repo bind`, `repo table`, `repo install`, `repo update`, `repo refresh`, `repo archive`, `repo unarchive`, `repo unbind`, `attempts summary`, `attempts table`, `codex coverage`, `codex history`"
    if blackdog_source_skill:
        scaffold_workflow = (
            f"- `${skill_name} scaffold project <description>`: ask only for missing durable choices such as target path, project name, exemplar repo, validation commands, routed docs, local project access, and app/runtime needs; preview with `./.VE/bin/blackdog repo scaffold --target-root TARGET --like EXEMPLAR --project-name NAME --dry-run`, then apply without adding scaffold logic to the generated project skill.\n"
        )
        repo_lifecycle_surface = "`repo analyze`, `repo bind`, `repo table`, `repo install`, `repo scaffold`, `repo update`, `repo refresh`, `repo archive`, `repo unarchive`, `repo unbind`, `attempts summary`, `attempts table`, `codex coverage`, `codex history`"
    return (
        "---\n"
        f"name: {skill_name}\n"
        f'description: "Repo-local AI development workflow for {profile.project_name}, backed by Blackdog."\n'
        "---\n\n"
        f"# Repo Skill: {profile.project_name}\n\n"
        "Use this repo-local skill for normal development requests. The skill is backed by the repo-local Blackdog CLI, but users do not need to name Blackdog workflow primitives in ordinary requests.\n"
        "`blackdog.toml` is the machine-readable source of truth for handler setup and routed docs.\n\n"
        "## User Workflows\n\n"
        f"- `$blackdog install or update in this repo`: before this repo-local skill exists, analyze the repo, then run `./.VE/bin/blackdog repo install --project-root .` when missing or `./.VE/bin/blackdog repo update --project-root .` followed by `./.VE/bin/blackdog repo refresh --project-root .` when already installed; finish with `git status --short` and commit or land managed repo changes, or report the checkout as intentionally dirty.\n"
        f"{scaffold_workflow}"
        f"- `${skill_name} do <task-description>`: build a concise execution prompt from the request and routed docs, run `./.VE/bin/blackdog task begin --project-root . --actor AGENT --prompt-file EXECUTION_PROMPT --prompt-mode skill --user-prompt-file USER_PROMPT`, make changes only in the returned task workspace, validate, then land with `./.VE/bin/blackdog task land --project-root . --summary \"...\"`.\n"
        "- For multi-agent work, use the active Codex thread directly and keep Blackdog focused on the task execution and attempt history it can record through the normal `do` flow.\n\n"
        "## Internal CLI Surface\n\n"
        f"- repo lifecycle: {repo_lifecycle_surface}\n"
        "- task execution: `task begin`, `task show`, `task recover`, `task land`, `task close`, `task cancel`, `task reopen`, `task cleanup`\n"
        "- status and evidence: `summary`, `snapshot`, `attempts summary`, `attempts table`, `codex coverage`, `codex history`\n"
        "- abandoned work is canceled by default; use `task reopen` only when it should return to normal execution\n\n"
        "## Operator Guardrails\n\n"
        "- Do not launch an external browser, use macOS `open`, use `xdg-open`, or run headed Playwright/browser sessions for agent verification unless the user explicitly asks for a user-visible browser; prefer Codex in-app browser tools or headless evidence.\n"
        "- After `repo install`, `repo update`, or `repo refresh`, run `git status --short`; commit or land managed repo changes, or report the checkout as intentionally dirty before finishing.\n"
        "- Before finishing implementation work, re-check branch and dirty state. Do not leave uncommitted changes from your work; if committing or landing, make sure the result is on the primary `main` branch unless the user explicitly requested another branch.\n\n"
        "## Docs To Review\n\n"
        f"{docs}\n"
    )


def render_repo_skill_metadata(profile: RepoProfile) -> str:
    skill_name = managed_skill_name(profile)
    return (
        "interface:\n"
        f"  display_name: {_yaml_quote(f'{profile.project_name} Development')}\n"
        f"  short_description: {_yaml_quote('Repo-local development overlay')}\n"
        f"  default_prompt: {_yaml_quote(f'Use ${skill_name} do <task-description> for repo work.')}\n"
    )


def _prune_managed_skill_auxiliary_files(profile: RepoProfile) -> tuple[Path, ...]:
    skill_dir = _managed_skill_path(profile).parent
    if not skill_dir.is_dir():
        return ()
    allowed = {_managed_skill_path(profile).resolve(), _managed_skill_metadata_path(profile).resolve()}
    removed: list[Path] = []
    for path in sorted((item for item in skill_dir.rglob("*") if item.is_file()), reverse=True):
        if path.resolve() in allowed:
            continue
        path.unlink()
        removed.append(path)
    for path in sorted((item for item in skill_dir.rglob("*") if item.is_dir()), reverse=True):
        if path == skill_dir:
            continue
        try:
            path.rmdir()
        except OSError:
            pass
    return tuple(removed)


def _write_repo_skill(profile: RepoProfile, *, overwrite: bool) -> RepoSkillWriteResult:
    skill_path = _managed_skill_path(profile)
    metadata_path = _managed_skill_metadata_path(profile)
    if skill_path.exists() and not overwrite:
        return RepoSkillWriteResult(skill_path=skill_path, metadata_path=metadata_path, changed=(), removed=())
    changed: list[Path] = []
    rendered_skill = render_repo_skill(profile)
    rendered_metadata = render_repo_skill_metadata(profile)
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    if not skill_path.exists() or skill_path.read_text(encoding="utf-8") != rendered_skill:
        skill_path.write_text(rendered_skill, encoding="utf-8")
        changed.append(skill_path)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    if not metadata_path.exists() or metadata_path.read_text(encoding="utf-8") != rendered_metadata:
        metadata_path.write_text(rendered_metadata, encoding="utf-8")
        changed.append(metadata_path)
    removed = _prune_managed_skill_auxiliary_files(profile)
    return RepoSkillWriteResult(skill_path=skill_path, metadata_path=metadata_path, changed=tuple(changed), removed=removed)


def _prune_repo_refresh_cleanup_artifacts(profile: RepoProfile) -> tuple[str, ...]:
    removed: list[str] = []
    for name in (*LEGACY_BACKLOG_CONTROL_ARTIFACTS, *REMOVED_ORCHESTRATION_CONTROL_ARTIFACTS):
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


def _scaffold_like_root(like_root: str | None) -> Path | None:
    if not like_root:
        return None
    candidate = Path(like_root).resolve()
    if not candidate.exists():
        raise RepoLifecycleError(f"scaffold exemplar does not exist: {candidate}")
    if not candidate.is_dir():
        raise RepoLifecycleError(f"scaffold exemplar must be a directory: {candidate}")
    return _resolve_repo_root_or_none(candidate) or candidate


def _scaffold_seed_candidates(like_root: Path | None) -> tuple[str, ...]:
    if like_root is None:
        return ()
    candidates: list[str] = []
    for relative_path in ("AGENTS.md", "README.md", ".gitignore", *suggest_default_doc_routing(like_root)):
        if relative_path in candidates:
            continue
        source_path = like_root / relative_path
        if source_path.is_file():
            candidates.append(relative_path)
    return tuple(candidates)


def _copy_scaffold_seed_files(
    *,
    like_root: Path | None,
    target_root: Path,
    relative_paths: tuple[str, ...],
    created: list[str],
    preserved: list[str],
) -> None:
    if like_root is None:
        return
    for relative_path in relative_paths:
        source_path = like_root / relative_path
        target_path = target_root / relative_path
        if target_path.exists():
            preserved.append(str(target_path.resolve()))
            continue
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        created.append(str(target_path.resolve()))


def scaffold_repo(
    *,
    target_root: Path,
    project_name: str | None = None,
    like_root: str | None = None,
    source_root: str | None = None,
    dry_run: bool = False,
) -> RepoScaffoldResult:
    target = target_root.resolve()
    if target.exists() and not target.is_dir():
        raise RepoLifecycleError(f"scaffold target must be a directory: {target}")
    if target.exists() and any(target.iterdir()) and not _is_git_repo_root(target):
        raise RepoLifecycleError(
            f"refusing to scaffold into non-empty non-repo directory: {target}; use `blackdog repo install` for existing repos"
        )

    exemplar = _scaffold_like_root(like_root)
    seed_files = _scaffold_seed_candidates(exemplar)
    resolved_project_name = project_name or target.name
    created: list[str] = []
    preserved: list[str] = []
    notes: list[str] = []
    initialized_git = False

    if dry_run:
        if not target.exists():
            notes.append(f"would create target directory: {target}")
        if not _is_git_repo_root(target):
            notes.append(f"would initialize a new git repo at: {target}")
        if seed_files:
            notes.append("would seed starter files from exemplar: " + ", ".join(seed_files))
        notes.append(
            "would install the Blackdog repo contract with: "
            + _recommended_scaffold_install_command(target, source_root=source_root)
        )
        return RepoScaffoldResult(
            action="scaffold",
            dry_run=True,
            target_root=str(target),
            project_name=resolved_project_name,
            like_root=str(exemplar) if exemplar is not None else None,
            source_root=str(Path(source_root).resolve()) if source_root else None,
            planned_seed_files=seed_files,
            created=(),
            preserved=(),
            initialized_git=False,
            install_result=None,
            notes=tuple(notes),
        )

    if not target.exists():
        target.mkdir(parents=True)
        created.append(str(target))
    if not _is_git_repo_root(target):
        _run_command("git", "init", "-b", "main", str(target))
        initialized_git = True
        created.append(str((target / ".git").resolve()))
    else:
        preserved.append(str((target / ".git").resolve()))

    _copy_scaffold_seed_files(
        like_root=exemplar,
        target_root=target,
        relative_paths=seed_files,
        created=created,
        preserved=preserved,
    )
    if exemplar is not None:
        notes.append(f"seeded starter files from exemplar: {exemplar}")

    install_result = install_repo(
        target,
        project_name=resolved_project_name,
        source_root=source_root,
    )
    return RepoScaffoldResult(
        action="scaffold",
        dry_run=False,
        target_root=str(target),
        project_name=resolved_project_name,
        like_root=str(exemplar) if exemplar is not None else None,
        source_root=str(Path(source_root).resolve()) if source_root else None,
        planned_seed_files=seed_files,
        created=tuple(dict.fromkeys(created)),
        preserved=tuple(dict.fromkeys(preserved)),
        initialized_git=initialized_git,
        install_result=install_result,
        notes=tuple(notes),
    )


def analyze_repo(project_root: Path) -> RepoConversionAnalysis:
    root = project_root.resolve()
    repo_root = _resolve_repo_root_or_none(root) or root
    in_git_repo = _resolve_repo_root_or_none(root) is not None
    profile_path = (repo_root / "blackdog.toml").resolve()
    profile_exists = profile_path.is_file()
    profile: RepoProfile | None = None
    profile_error: str | None = None
    if profile_exists:
        try:
            profile = load_profile(repo_root)
        except ConfigError as exc:
            profile_error = str(exc)

    project_name = profile.project_name if profile is not None else repo_root.name
    managed_name = managed_skill_name(profile or repo_root)
    current_doc_routing = profile.doc_routing_defaults if profile is not None else ()
    suggested_doc_routing = _merge_suggested_doc_routing(repo_root, current_doc_routing)
    agents_path = (repo_root / AGENTS_FILE_NAME).resolve()
    agents_text = agents_path.read_text(encoding="utf-8") if agents_path.is_file() else ""
    managed_agents_block_present = AGENTS_MANAGED_BEGIN in agents_text and AGENTS_MANAGED_END in agents_text
    ve_path = (repo_root / ".VE").resolve()
    blackdog_path = (ve_path / "bin" / "blackdog").resolve()
    managed_skill_path = (repo_root / managed_skill_relative_path(profile or repo_root)).resolve()
    legacy_skill_path = (repo_root / legacy_managed_skill_relative_path()).resolve()
    agent_docs, entrypoint_docs, package_agent_docs = _find_agent_docs(repo_root)
    skills = _find_skill_surfaces(repo_root, managed_name=managed_name)

    findings: list[RepoConversionFinding] = []
    notes: list[str] = []

    if not in_git_repo:
        findings.append(
            RepoConversionFinding(
                code="missing-git-repo",
                severity="high",
                message="The target path is not inside a git repo, so Blackdog install cannot run yet.",
                paths=(str(repo_root),),
            )
        )
    if not profile_exists:
        findings.append(
            RepoConversionFinding(
                code="missing-blackdog-profile",
                severity="high",
                message="The repo does not have `blackdog.toml`, so there is no shared Blackdog operating contract yet.",
                paths=(str(profile_path),),
            )
        )
    if profile_error is not None:
        findings.append(
            RepoConversionFinding(
                code="invalid-blackdog-profile",
                severity="high",
                message=f"`blackdog.toml` exists but did not load cleanly: {profile_error}",
                paths=(str(profile_path),),
            )
        )
    missing_routed_docs = tuple(path for path in current_doc_routing if not (repo_root / path).is_file())
    if missing_routed_docs:
        findings.append(
            RepoConversionFinding(
                code="missing-routed-docs",
                severity="high",
                message="`blackdog.toml` routes docs that do not exist in the repo, which creates contract ambiguity.",
                paths=tuple(str((repo_root / path).resolve()) for path in missing_routed_docs),
            )
        )
    if not managed_agents_block_present:
        findings.append(
            RepoConversionFinding(
                code="missing-managed-agents-contract",
                severity="medium",
                message="`AGENTS.md` does not yet carry the managed Blackdog contract block, so hard workflow rules are not anchored in repo docs.",
                paths=(str(agents_path),),
            )
        )
    if profile_exists and not managed_skill_path.is_file():
        findings.append(
            RepoConversionFinding(
                code="missing-managed-skill",
                severity="medium",
                message="The repo profile exists, but the managed Blackdog skill is missing.",
                paths=(str(managed_skill_path),),
            )
        )
    if profile_exists and not ve_path.is_dir():
        findings.append(
            RepoConversionFinding(
                code="missing-root-venv",
                severity="medium",
                message="The repo is missing the repo-local `.VE`, so the Blackdog runtime is not bootstrapped here.",
                paths=(str(ve_path),),
            )
        )
    if profile_exists and ve_path.is_dir() and not blackdog_path.is_file():
        findings.append(
            RepoConversionFinding(
                code="missing-blackdog-launcher",
                severity="medium",
                message="The repo-local `.VE` exists, but the `blackdog` launcher is missing from it.",
                paths=(str(blackdog_path),),
            )
        )
    unrouted_entrypoint_docs = tuple(
        path for path in entrypoint_docs if path != "README.md" and path not in current_doc_routing
    )
    if unrouted_entrypoint_docs:
        findings.append(
            RepoConversionFinding(
                code="unrouted-agent-entrypoints",
                severity="medium",
                message="The repo has agent entrypoint docs that are not part of the current Blackdog routed-doc contract.",
                paths=tuple(str((repo_root / path).resolve()) for path in unrouted_entrypoint_docs),
            )
        )
    custom_skills = tuple(skill for skill in skills if not skill.managed)
    delegated_custom_skills = tuple(skill for skill in custom_skills if skill.delegates_to_managed)
    if custom_skills and not delegated_custom_skills:
        findings.append(
            RepoConversionFinding(
                code="custom-skills-bypass-blackdog",
                severity="medium",
                message="The repo has custom Codex skills, but none currently delegate workflow to the managed Blackdog skill.",
                paths=tuple(skill.skill_path for skill in custom_skills),
            )
        )
    if legacy_skill_path.is_file():
        findings.append(
            RepoConversionFinding(
                code="legacy-managed-skill-path",
                severity="low",
                message="A legacy `.codex/skills/blackdog/` skill path still exists and should be migrated during refresh.",
                paths=(str(legacy_skill_path),),
            )
        )
    if not agents_path.is_file() and package_agent_docs:
        findings.append(
            RepoConversionFinding(
                code="missing-root-agents-doc",
                severity="medium",
                message="The repo has package-level `AGENTS.md` files but no root `AGENTS.md`, so the top-level agent entrypoint is ambiguous.",
                paths=tuple(str((repo_root / path).resolve()) for path in package_agent_docs),
            )
        )

    conversion_status = "not-installed"
    if profile_exists or managed_agents_block_present or managed_skill_path.is_file() or blackdog_path.is_file():
        conversion_status = "partial"
    if profile_exists and managed_agents_block_present and managed_skill_path.is_file() and blackdog_path.is_file():
        conversion_status = "blackdog-backed"

    steps: list[RepoConversionStep] = []
    if not in_git_repo:
        steps.append(
            RepoConversionStep(
                phase="precondition",
                summary="Initialize the target repo in git before attempting Blackdog conversion.",
                details="`repo install` requires the target path to be inside a git repo.",
                paths=(str(repo_root),),
                command=_shell_command("git", "-C", str(repo_root), "init"),
                managed_by_blackdog=False,
            )
        )
    steps.append(
        RepoConversionStep(
            phase="repo-owned",
            summary="Review the proposed routed docs and decide which files should be the canonical agent entrypoints.",
            details=(
                "Suggested routed docs: " + ", ".join(suggested_doc_routing)
                if suggested_doc_routing
                else "No routed docs were discovered automatically beyond AGENTS.md."
            ),
            paths=tuple(str((repo_root / path).resolve()) for path in suggested_doc_routing),
            managed_by_blackdog=False,
        )
    )
    steps.append(
        RepoConversionStep(
            phase="blackdog-managed",
            summary="Install or repair the repo-local Blackdog runtime and managed scaffold in the target repo.",
            details="This creates or repairs `blackdog.toml`, the repo-local `.VE`, the managed AGENTS contract block, and the managed skill.",
            paths=(str(profile_path), str(agents_path), str(managed_skill_path), str(blackdog_path)),
            command=_recommended_install_command(repo_root),
            managed_by_blackdog=True,
        )
    )
    if package_agent_docs or unrouted_entrypoint_docs:
        steps.append(
            RepoConversionStep(
                phase="repo-owned",
                summary="Harmonize repo-owned agent docs so repo-specific rules live outside the managed AGENTS block and no duplicate workflow docs conflict with Blackdog.",
                details="Collapse or clarify duplicate entrypoints before relying on the converted repo as a shared agent environment.",
                paths=tuple(str((repo_root / path).resolve()) for path in (*entrypoint_docs, *package_agent_docs)),
                managed_by_blackdog=False,
            )
        )
    if custom_skills:
        steps.append(
            RepoConversionStep(
                phase="repo-owned",
                summary="Review custom repo skills and update wrapper skills to delegate workflow through the managed Blackdog skill where appropriate.",
                details=f"Managed skill token: `${managed_name}`.",
                paths=tuple(skill.skill_path for skill in custom_skills),
                managed_by_blackdog=False,
            )
        )
    steps.append(
        RepoConversionStep(
            phase="blackdog-managed",
            summary="After approving repo-owned doc or skill changes, rerun refresh so the managed scaffold matches the final repo contract.",
            details="`repo refresh` regenerates the managed AGENTS block and managed skill from the profile's routed-doc contract.",
            paths=(str(profile_path), str(agents_path), str(managed_skill_path)),
            command=_recommended_refresh_command(repo_root),
            managed_by_blackdog=True,
        )
    )

    if current_doc_routing != suggested_doc_routing and suggested_doc_routing:
        notes.append("Suggested routed docs differ from the current profile and should be reviewed during conversion.")
    if custom_skills and delegated_custom_skills:
        notes.append("Some custom skills already mention the managed Blackdog skill; keep that delegation pattern when harmonizing the skill set.")

    return RepoConversionAnalysis(
        action="analyze",
        project_root=str(root),
        repo_root=str(repo_root),
        project_name=project_name,
        in_git_repo=in_git_repo,
        conversion_status=conversion_status,
        profile_exists=profile_exists,
        profile_path=str(profile_path) if profile_exists else None,
        profile_error=profile_error,
        current_doc_routing=current_doc_routing,
        suggested_doc_routing=suggested_doc_routing,
        agents_path=str(agents_path),
        managed_agents_block_present=managed_agents_block_present,
        ve_path=str(ve_path),
        ve_exists=ve_path.is_dir(),
        blackdog_path=str(blackdog_path),
        blackdog_launcher_exists=blackdog_path.is_file(),
        managed_skill_path=str(managed_skill_path),
        managed_skill_exists=managed_skill_path.is_file(),
        legacy_skill_path=str(legacy_skill_path),
        legacy_skill_exists=legacy_skill_path.is_file(),
        entrypoint_docs=entrypoint_docs,
        agent_docs=agent_docs,
        package_agent_docs=package_agent_docs,
        skills=skills,
        findings=tuple(findings),
        proposed_steps=tuple(steps),
        notes=tuple(notes),
    )


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

    skill_result = _write_repo_skill(profile, overwrite=False)
    skill_path = skill_result.skill_path
    if skill_result.changed:
        created.extend(str(path) for path in skill_result.changed)
    if skill_result.removed:
        removed.extend(str(path) for path in skill_result.removed)
    if not skill_result.changed:
        preserved.append(str(skill_path))
    legacy_skill = _legacy_managed_skill_path(profile)
    if legacy_skill.exists() and legacy_skill != skill_path:
        preserved.append(str(legacy_skill))
        notes.append("legacy repo skill path still exists; run `blackdog repo refresh` to migrate it to the repo-slug skill path")

    _append_repo_visible_dirty_note(
        notes,
        profile.paths.project_root,
        created=created,
        updated=updated,
        removed=removed,
    )
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

    _append_repo_visible_dirty_note(
        notes,
        profile.paths.project_root,
        created=created,
        updated=updated,
        removed=removed,
    )
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
    skill_result = _write_repo_skill(profile, overwrite=True)
    skill_path = skill_result.skill_path
    removed = list(_prune_repo_refresh_cleanup_artifacts(profile))
    removed.extend(str(path) for path in skill_result.removed)
    removed.extend(_prune_obsolete_managed_skill_dirs(profile))
    preserved = [str(profile.paths.profile_file)]
    updated = []
    if agents_status == "created":
        updated.append(str(agents_path))
    elif agents_status == "updated":
        updated.append(str(agents_path))
    else:
        preserved.append(str(agents_path))
    updated.extend(str(path) for path in skill_result.changed)
    notes: list[str] = []
    _apply_handler_actions(
        handler_summary,
        created=[],
        updated=[],
        preserved=preserved,
        notes=notes,
    )
    _append_repo_visible_dirty_note(
        notes,
        profile.paths.project_root,
        created=[],
        updated=updated,
        removed=removed,
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


def render_repo_analysis_text(result: RepoConversionAnalysis) -> str:
    lines = [
        f"[blackdog-repo] action: {result.action}",
        f"[blackdog-repo] project root: {result.project_root}",
        f"[blackdog-repo] repo root: {result.repo_root}",
        f"[blackdog-repo] project: {result.project_name}",
        f"[blackdog-repo] conversion status: {result.conversion_status}",
        f"[blackdog-repo] in git repo: {'yes' if result.in_git_repo else 'no'}",
        f"[blackdog-repo] profile: {result.profile_path or 'missing'}",
        f"[blackdog-repo] AGENTS contract block: {'present' if result.managed_agents_block_present else 'missing'}",
        f"[blackdog-repo] repo .VE: {'present' if result.ve_exists else 'missing'}",
        f"[blackdog-repo] launcher: {'present' if result.blackdog_launcher_exists else 'missing'} ({result.blackdog_path})",
        f"[blackdog-repo] managed skill: {'present' if result.managed_skill_exists else 'missing'} ({result.managed_skill_path})",
    ]
    if result.profile_error:
        lines.append(f"[blackdog-repo] profile error: {result.profile_error}")
    if result.current_doc_routing:
        lines.append(f"[blackdog-repo] current doc routing: {', '.join(result.current_doc_routing)}")
    if result.suggested_doc_routing:
        lines.append(f"[blackdog-repo] suggested doc routing: {', '.join(result.suggested_doc_routing)}")
    if result.entrypoint_docs:
        lines.append(f"[blackdog-repo] entrypoint docs: {', '.join(result.entrypoint_docs)}")
    if result.skills:
        skill_rows = ", ".join(
            f"{item.name}{' [managed]' if item.managed else ''}{' [delegates]' if item.delegates_to_managed else ''}"
            for item in result.skills
        )
        lines.append(f"[blackdog-repo] skills: {skill_rows}")
    if result.findings:
        lines.append("[blackdog-repo] findings:")
        for finding in result.findings:
            path_suffix = "" if not finding.paths else f" ({', '.join(finding.paths)})"
            lines.append(f"  - [{finding.severity}] {finding.code}: {finding.message}{path_suffix}")
    if result.proposed_steps:
        lines.append("[blackdog-repo] proposed steps:")
        for index, step in enumerate(result.proposed_steps, start=1):
            detail = "" if step.details is None else f" {step.details}"
            managed = " managed" if step.managed_by_blackdog else ""
            lines.append(f"  {index}. [{step.phase}{managed}] {step.summary}{detail}")
            if step.command:
                lines.append(f"     command: {step.command}")
    for note in result.notes:
        lines.append(f"[blackdog-repo] note: {note}")
    return "\n".join(lines) + "\n"


def render_repo_scaffold_text(result: RepoScaffoldResult) -> str:
    lines = [
        f"[blackdog-repo] action: {result.action}",
        f"[blackdog-repo] dry run: {'yes' if result.dry_run else 'no'}",
        f"[blackdog-repo] target root: {result.target_root}",
        f"[blackdog-repo] project: {result.project_name}",
    ]
    if result.like_root:
        lines.append(f"[blackdog-repo] exemplar: {result.like_root}")
    if result.source_root:
        lines.append(f"[blackdog-repo] source root: {result.source_root}")
    if result.planned_seed_files:
        lines.append(f"[blackdog-repo] seed files: {', '.join(result.planned_seed_files)}")
    if result.initialized_git:
        lines.append("[blackdog-repo] initialized git: yes")
    if result.install_result is not None:
        lines.append(f"[blackdog-repo] installed profile: {result.install_result.profile_path}")
        lines.append(f"[blackdog-repo] installed skill: {result.install_result.skill_path}")
        lines.append(f"[blackdog-repo] installed launcher: {result.install_result.blackdog_path}")
    if result.created:
        lines.append(f"[blackdog-repo] created: {', '.join(result.created)}")
    if result.preserved:
        lines.append(f"[blackdog-repo] preserved: {', '.join(result.preserved)}")
    for note in result.notes:
        lines.append(f"[blackdog-repo] note: {note}")
    return "\n".join(lines) + "\n"


__all__ = [
    "RepoConversionAnalysis",
    "RepoConversionFinding",
    "RepoConversionStep",
    "RepoLifecycleError",
    "RepoLifecycleResult",
    "RepoScaffoldResult",
    "analyze_repo",
    "install_repo",
    "refresh_repo",
    "render_repo_analysis_text",
    "render_repo_lifecycle_text",
    "render_repo_scaffold_text",
    "render_repo_skill",
    "scaffold_repo",
    "update_repo",
]
