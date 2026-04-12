from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import tomllib


PROFILE_FILE_NAME = "blackdog.toml"
GIT_COMMON_TOKEN = "@git-common"

DEFAULT_VALIDATION_COMMANDS = (
    "PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'",
)
DEFAULT_CONTROL_DIR = f"{GIT_COMMON_TOKEN}/blackdog"
DEFAULT_WORKTREES_DIR = "../.worktrees"
DEFAULT_DOC_ROUTING = (
    "AGENTS.md",
    "docs/INDEX.md",
    "docs/PRODUCT_SPEC.md",
    "docs/ARCHITECTURE.md",
    "docs/TARGET_MODEL.md",
    "docs/CLI.md",
    "docs/FILE_FORMATS.md",
)


class ConfigError(RuntimeError):
    pass


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "project"


@dataclass(frozen=True)
class BlackdogPaths:
    project_root: Path
    profile_file: Path
    control_dir: Path
    planning_file: Path
    runtime_file: Path
    events_file: Path
    worktrees_dir: Path


@dataclass(frozen=True)
class RepoProfile:
    project_name: str
    profile_version: int
    validation_commands: tuple[str, ...]
    doc_routing_defaults: tuple[str, ...]
    paths: BlackdogPaths


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / PROFILE_FILE_NAME).exists():
            return candidate
    raise ConfigError(f"Could not find {PROFILE_FILE_NAME} from {current}")


def _resolve_rel(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()


def _run_git(project_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise ConfigError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _git_common_dir(project_root: Path) -> Path:
    raw = _run_git(project_root, "rev-parse", "--git-common-dir")
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate.resolve()
    return (project_root / candidate).resolve()


def _resolve_path_value(project_root: Path, value: str) -> Path:
    if value == GIT_COMMON_TOKEN:
        return _git_common_dir(project_root)
    if value.startswith(f"{GIT_COMMON_TOKEN}/"):
        return (_git_common_dir(project_root) / value[len(GIT_COMMON_TOKEN) + 1 :]).resolve()
    return _resolve_rel(project_root, value)


def _default_control_paths(control_dir: Path) -> dict[str, Path]:
    return {
        "planning_file": control_dir / "planning.json",
        "runtime_file": control_dir / "runtime.json",
        "events_file": control_dir / "events.jsonl",
    }


def _prune_stale_git_worktrees(project_root: Path) -> None:
    _run_git(project_root, "worktree", "prune")


def _ensure_control_root_layout(paths: BlackdogPaths) -> None:
    paths.control_dir.mkdir(parents=True, exist_ok=True)
    _prune_stale_git_worktrees(paths.project_root)


def _paths_from_raw(project_root: Path, raw_paths: dict[str, str]) -> BlackdogPaths:
    if "control_dir" in raw_paths:
        control_dir = _resolve_path_value(project_root, str(raw_paths["control_dir"]))
        defaults = _default_control_paths(control_dir)
    else:
        control_dir = None
        defaults = {}

    def resolve_runtime_path(key: str) -> Path:
        if key in raw_paths:
            return _resolve_path_value(project_root, str(raw_paths[key]))
        if key in defaults:
            return defaults[key]
        raise ConfigError(f"Profile is missing path keys: ['{key}']")

    resolved_control_dir = control_dir or resolve_runtime_path("planning_file").parent
    return BlackdogPaths(
        project_root=project_root,
        profile_file=(project_root / PROFILE_FILE_NAME).resolve(),
        control_dir=resolved_control_dir,
        planning_file=resolve_runtime_path("planning_file"),
        runtime_file=resolve_runtime_path("runtime_file"),
        events_file=resolve_runtime_path("events_file"),
        worktrees_dir=_resolve_path_value(project_root, str(raw_paths.get("worktrees_dir", DEFAULT_WORKTREES_DIR))),
    )


def load_profile(project_root: Path | None = None) -> RepoProfile:
    root = find_project_root(project_root)
    profile_file = root / PROFILE_FILE_NAME
    with profile_file.open("rb") as handle:
        payload = tomllib.load(handle)

    project = payload.get("project") or {}
    taxonomy = payload.get("taxonomy") or {}
    raw_paths = payload.get("paths") or {}

    if "control_dir" not in raw_paths:
        required_runtime = {"planning_file", "runtime_file", "events_file"}
        missing_runtime = sorted(required_runtime - set(raw_paths))
        if missing_runtime:
            raise ConfigError(
                "Profile must define either paths.control_dir or explicit runtime path keys: "
                + ", ".join(missing_runtime)
            )

    paths = _paths_from_raw(root, {str(key): str(value) for key, value in raw_paths.items()})
    _ensure_control_root_layout(paths)

    project_name = str(project.get("name") or root.name)
    return RepoProfile(
        project_name=project_name,
        profile_version=int(project.get("profile_version") or 1),
        validation_commands=tuple(
            str(item) for item in taxonomy.get("validation_commands") or DEFAULT_VALIDATION_COMMANDS
        ),
        doc_routing_defaults=tuple(
            str(item) for item in taxonomy.get("doc_routing_defaults") or DEFAULT_DOC_ROUTING
        ),
        paths=paths,
    )


def render_default_profile(project_name: str) -> str:
    validations = ", ".join(f'"{item}"' for item in DEFAULT_VALIDATION_COMMANDS)
    doc_routing = ", ".join(f'"{item}"' for item in DEFAULT_DOC_ROUTING)
    return (
        f'[project]\n'
        f'name = "{project_name}"\n'
        f"profile_version = 1\n\n"
        f'[paths]\n'
        f'control_dir = "{DEFAULT_CONTROL_DIR}"\n'
        f'worktrees_dir = "{DEFAULT_WORKTREES_DIR}"\n\n'
        f'[taxonomy]\n'
        f'validation_commands = [{validations}]\n'
        f'doc_routing_defaults = [{doc_routing}]\n'
    )


def write_default_profile(project_root: Path, project_name: str, *, force: bool = False) -> Path:
    root = project_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    profile_file = root / PROFILE_FILE_NAME
    if profile_file.exists() and not force:
        raise ConfigError(f"Refusing to overwrite {profile_file}; pass force=True to replace it")
    profile_file.write_text(render_default_profile(project_name), encoding="utf-8")
    return profile_file
