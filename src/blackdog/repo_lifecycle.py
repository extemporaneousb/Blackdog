from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shlex
import subprocess
import sys
import tomllib

from blackdog_core.profile import RepoProfile, ConfigError, load_profile, write_default_profile


MANAGED_SKILL_RELATIVE_PATH = Path(".codex") / "skills" / "blackdog" / "SKILL.md"


class RepoLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RepoLifecycleResult:
    action: str
    project_root: str
    source_root: str | None
    profile_path: str | None
    skill_path: str | None
    ve_path: str | None
    blackdog_path: str | None
    created: tuple[str, ...]
    updated: tuple[str, ...]
    preserved: tuple[str, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "project_root": self.project_root,
            "source_root": self.source_root,
            "profile_path": self.profile_path,
            "skill_path": self.skill_path,
            "ve_path": self.ve_path,
            "blackdog_path": self.blackdog_path,
            "created": list(self.created),
            "updated": list(self.updated),
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


def _resolve_source_root(source_root: str | None) -> Path:
    if source_root is not None:
        candidate = Path(source_root).resolve()
    else:
        candidate = Path(__file__).resolve().parents[2]
    if not _looks_like_blackdog_source_checkout(candidate):
        raise RepoLifecycleError(f"expected a Blackdog source checkout at {candidate}")
    return candidate


def _ensure_repo_venv(project_root: Path) -> tuple[Path, bool]:
    ve_path = (project_root / ".VE").resolve()
    python_path = ve_path / "bin" / "python"
    created = False
    if not python_path.is_file():
        ve_path.parent.mkdir(parents=True, exist_ok=True)
        _run_command(sys.executable, "-m", "venv", str(ve_path))
        created = True
    if not python_path.is_file():
        raise RepoLifecycleError(f"expected repo-local python at {python_path}")
    return ve_path, created


def _launcher_script(*, project_root: Path, source_root: Path) -> str:
    python_path = (project_root / ".VE" / "bin" / "python").resolve()
    source_src = (source_root / "src").resolve()
    return (
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(str(source_src))}"
        '${PYTHONPATH:+":$PYTHONPATH"} '
        f"exec {shlex.quote(str(python_path))} -m blackdog_cli \"$@\"\n"
    )


def _write_blackdog_launcher(*, project_root: Path, source_root: Path) -> tuple[Path, bool, bool]:
    launcher_path = (project_root / ".VE" / "bin" / "blackdog").resolve()
    launcher_path.parent.mkdir(parents=True, exist_ok=True)
    existed_before = launcher_path.is_file()
    next_text = _launcher_script(project_root=project_root, source_root=source_root)
    previous_text = launcher_path.read_text(encoding="utf-8") if existed_before else None
    launcher_path.write_text(next_text, encoding="utf-8")
    launcher_path.chmod(0o755)
    return launcher_path, existed_before, previous_text != next_text


def render_repo_skill(profile: RepoProfile) -> str:
    docs = "\n".join(f"- `{item}`" for item in profile.doc_routing_defaults)
    return (
        "---\n"
        'name: blackdog\n'
        f'description: "Use the repo-local Blackdog CLI and contract for {profile.project_name}."\n'
        "---\n\n"
        f"# Blackdog: {profile.project_name}\n\n"
        "Use the repo-local Blackdog CLI instead of mutating control-root files by hand.\n\n"
        "## CLI Entry Point\n\n"
        "- `./.VE/bin/blackdog`\n\n"
        "## Shipped Workflow Families\n\n"
        "- repo lifecycle: `repo install`, `repo update`, `repo refresh`\n"
        "- workset/task runtime: `workset put`, `summary`, `next`, `snapshot`\n"
        "- WTAM kept-change execution: `worktree preflight`, `worktree preview`, `worktree start`, `worktree land`, `worktree cleanup`\n\n"
        "## Repo Lifecycle Flow\n\n"
        "1. `./.VE/bin/blackdog repo update --project-root .`\n"
        "2. `./.VE/bin/blackdog repo refresh --project-root .`\n"
        "3. review the routed docs below before editing\n\n"
        "## WTAM Flow\n\n"
        "1. `./.VE/bin/blackdog summary --project-root .`\n"
        "2. `./.VE/bin/blackdog next --project-root .`\n"
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


def _require_profile(project_root: Path) -> RepoProfile:
    profile_path = (project_root / "blackdog.toml").resolve()
    if not profile_path.exists():
        raise RepoLifecycleError(f"{profile_path} is missing; run `blackdog repo install` first")
    try:
        return load_profile(project_root)
    except ConfigError as exc:
        raise RepoLifecycleError(str(exc)) from exc


def install_repo(
    project_root: Path,
    *,
    project_name: str | None = None,
    source_root: str | None = None,
) -> RepoLifecycleResult:
    repo_root = _resolve_repo_root(project_root)
    source = _resolve_source_root(source_root)
    created: list[str] = []
    updated: list[str] = []
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

    profile = load_profile(repo_root)
    ve_path, ve_created = _ensure_repo_venv(profile.paths.project_root)
    if ve_created:
        created.append(str(ve_path))
    launcher_path, launcher_existed, launcher_changed = _write_blackdog_launcher(
        project_root=profile.paths.project_root,
        source_root=source,
    )
    if launcher_changed and launcher_existed:
        updated.append(str(launcher_path))
    elif launcher_changed:
        created.append(str(launcher_path))
    else:
        preserved.append(str(launcher_path))

    skill_path, skill_changed = _write_repo_skill(profile, overwrite=False)
    if skill_changed:
        created.append(str(skill_path))
    else:
        preserved.append(str(skill_path))

    return RepoLifecycleResult(
        action="install",
        project_root=str(profile.paths.project_root),
        source_root=str(source),
        profile_path=str(profile.paths.profile_file),
        skill_path=str(skill_path),
        ve_path=str(ve_path),
        blackdog_path=str(launcher_path),
        created=tuple(dict.fromkeys(created)),
        updated=tuple(dict.fromkeys(updated)),
        preserved=tuple(dict.fromkeys(preserved)),
        notes=tuple(notes),
    )


def update_repo(
    project_root: Path,
    *,
    source_root: str | None = None,
) -> RepoLifecycleResult:
    repo_root = _resolve_repo_root(project_root)
    source = _resolve_source_root(source_root)
    profile = _require_profile(repo_root)
    created: list[str] = []
    updated: list[str] = []
    preserved: list[str] = [str(profile.paths.profile_file)]
    notes: list[str] = []

    ve_path, ve_created = _ensure_repo_venv(profile.paths.project_root)
    if ve_created:
        created.append(str(ve_path))
    launcher_path, launcher_existed, launcher_changed = _write_blackdog_launcher(
        project_root=profile.paths.project_root,
        source_root=source,
    )
    if launcher_changed and launcher_existed:
        updated.append(str(launcher_path))
    elif launcher_changed:
        created.append(str(launcher_path))
    else:
        preserved.append(str(launcher_path))

    skill_path = (profile.paths.project_root / MANAGED_SKILL_RELATIVE_PATH).resolve()
    if skill_path.exists():
        preserved.append(str(skill_path))
    else:
        notes.append("repo skill is missing; run `blackdog repo refresh` to regenerate it")

    return RepoLifecycleResult(
        action="update",
        project_root=str(profile.paths.project_root),
        source_root=str(source),
        profile_path=str(profile.paths.profile_file),
        skill_path=str(skill_path),
        ve_path=str(ve_path),
        blackdog_path=str(launcher_path),
        created=tuple(dict.fromkeys(created)),
        updated=tuple(dict.fromkeys(updated)),
        preserved=tuple(dict.fromkeys(preserved)),
        notes=tuple(notes),
    )


def refresh_repo(project_root: Path) -> RepoLifecycleResult:
    repo_root = _resolve_repo_root(project_root)
    profile = _require_profile(repo_root)
    skill_path, skill_changed = _write_repo_skill(profile, overwrite=True)
    preserved = [str(profile.paths.profile_file)]
    updated = [str(skill_path)] if skill_changed else []
    ve_path = (profile.paths.project_root / ".VE").resolve()
    blackdog_path = (ve_path / "bin" / "blackdog").resolve()
    notes: list[str] = []
    if not blackdog_path.is_file() or not os.access(blackdog_path, os.X_OK):
        notes.append("repo-local blackdog launcher is missing; run `blackdog repo install` or `blackdog repo update`")
    return RepoLifecycleResult(
        action="refresh",
        project_root=str(profile.paths.project_root),
        source_root=None,
        profile_path=str(profile.paths.profile_file),
        skill_path=str(skill_path),
        ve_path=str(ve_path),
        blackdog_path=str(blackdog_path),
        created=(),
        updated=tuple(updated),
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
    if result.profile_path:
        lines.append(f"[blackdog-repo] profile: {result.profile_path}")
    if result.skill_path:
        lines.append(f"[blackdog-repo] skill: {result.skill_path}")
    if result.blackdog_path:
        lines.append(f"[blackdog-repo] launcher: {result.blackdog_path}")
    if result.created:
        lines.append(f"[blackdog-repo] created: {', '.join(result.created)}")
    if result.updated:
        lines.append(f"[blackdog-repo] updated: {', '.join(result.updated)}")
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
