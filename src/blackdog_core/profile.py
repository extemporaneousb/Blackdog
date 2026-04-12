from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import subprocess
import tomllib


PROFILE_FILE_NAME = "blackdog.toml"
GIT_COMMON_TOKEN = "@git-common"

DEFAULT_BUCKETS = (
    "core",
    "cli",
    "docs",
    "integration",
)
DEFAULT_DOMAINS = (
    "planning",
    "runtime",
    "events",
    "docs",
    "cli",
)
DEFAULT_VALIDATION_COMMANDS = (
    "PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'",
)
DEFAULT_CONTROL_DIR = f"{GIT_COMMON_TOKEN}/blackdog"
DEFAULT_WORKTREES_DIR = "../.worktrees"
DEFAULT_SKILL_USAGE_HEURISTIC = (
    "Prefer the machine-owned Blackdog CLI surfaces over hand-edited control-root files."
)
DEFAULT_SUPERVISOR_COMMAND = (
    "codex",
    "exec",
    "--dangerously-bypass-approvals-and-sandbox",
)
VALID_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
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


def default_host_skill_name(project_name: str) -> str:
    return f"blackdog-{slugify(project_name)}"


def default_host_skill_dir(project_name: str) -> str:
    return f".codex/skills/{default_host_skill_name(project_name)}"


def default_id_prefix(project_name: str) -> str:
    letters = re.sub(r"[^A-Za-z0-9]+", "", project_name).upper()
    return letters[:5] or "BDOG"


@dataclass(frozen=True)
class BlackdogPaths:
    project_root: Path
    profile_file: Path
    control_dir: Path
    planning_file: Path
    runtime_file: Path
    events_file: Path
    skill_dir: Path
    worktrees_dir: Path


@dataclass(frozen=True)
class RepoProfile:
    project_name: str
    profile_version: int
    id_prefix: str
    id_digest_length: int
    require_claim_for_completion: bool
    auto_render_html: bool
    buckets: tuple[str, ...]
    domains: tuple[str, ...]
    validation_commands: tuple[str, ...]
    doc_routing_defaults: tuple[str, ...]
    supervisor_launch_command: tuple[str, ...]
    supervisor_model: str | None
    supervisor_reasoning_effort: str | None
    supervisor_dynamic_reasoning: bool
    supervisor_max_parallel: int
    supervisor_workspace_mode: str
    pm_heuristics: dict[str, str]
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
    if "skill_dir" not in raw_paths:
        raise ConfigError("Profile is missing path keys: ['skill_dir']")
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
        skill_dir=_resolve_path_value(project_root, str(raw_paths["skill_dir"])),
        worktrees_dir=_resolve_path_value(project_root, str(raw_paths.get("worktrees_dir", DEFAULT_WORKTREES_DIR))),
    )


def load_profile(project_root: Path | None = None) -> RepoProfile:
    root = find_project_root(project_root)
    profile_file = root / PROFILE_FILE_NAME
    with profile_file.open("rb") as handle:
        payload = tomllib.load(handle)

    project = payload.get("project") or {}
    ids = payload.get("ids") or {}
    rules = payload.get("rules") or {}
    taxonomy = payload.get("taxonomy") or {}
    supervisor = payload.get("supervisor") or {}
    heuristics = payload.get("pm_heuristics") or {}
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

    raw_launch_command = supervisor.get("launch_command") or list(DEFAULT_SUPERVISOR_COMMAND)
    if isinstance(raw_launch_command, str):
        launch_command = tuple(shlex.split(raw_launch_command))
    else:
        launch_command = tuple(str(item) for item in raw_launch_command)
    if not launch_command:
        raise ConfigError("supervisor.launch_command must contain at least one argv token")

    model = str(supervisor.get("model") or "").strip() or None
    reasoning_effort = str(supervisor.get("reasoning_effort") or "").strip() or None
    if reasoning_effort is not None and reasoning_effort not in VALID_REASONING_EFFORTS:
        raise ConfigError(
            "supervisor.reasoning_effort must be one of: " + ", ".join(sorted(VALID_REASONING_EFFORTS))
        )
    dynamic_reasoning = bool(supervisor.get("dynamic_reasoning", False))
    workspace_mode = str(supervisor.get("workspace_mode") or "git-worktree").strip()
    if workspace_mode != "git-worktree":
        raise ConfigError("supervisor.workspace_mode must be 'git-worktree'")
    max_parallel_raw = supervisor.get("max_parallel")
    max_parallel = int(2 if max_parallel_raw is None else max_parallel_raw)
    if max_parallel < 1:
        raise ConfigError("supervisor.max_parallel must be at least 1")

    project_name = str(project.get("name") or root.name)
    return RepoProfile(
        project_name=project_name,
        profile_version=int(project.get("profile_version") or 1),
        id_prefix=str(ids.get("prefix") or default_id_prefix(project_name)),
        id_digest_length=int(ids.get("digest_length") or 10),
        require_claim_for_completion=bool(rules.get("require_claim_for_completion", False)),
        auto_render_html=bool(rules.get("auto_render_html", False)),
        buckets=tuple(str(item) for item in taxonomy.get("buckets") or DEFAULT_BUCKETS),
        domains=tuple(str(item) for item in taxonomy.get("domains") or DEFAULT_DOMAINS),
        validation_commands=tuple(
            str(item) for item in taxonomy.get("validation_commands") or DEFAULT_VALIDATION_COMMANDS
        ),
        doc_routing_defaults=tuple(
            str(item) for item in taxonomy.get("doc_routing_defaults") or DEFAULT_DOC_ROUTING
        ),
        supervisor_launch_command=launch_command,
        supervisor_model=model,
        supervisor_reasoning_effort=reasoning_effort,
        supervisor_dynamic_reasoning=dynamic_reasoning,
        supervisor_max_parallel=max_parallel,
        supervisor_workspace_mode=workspace_mode,
        pm_heuristics={str(key): str(value) for key, value in heuristics.items()},
        paths=paths,
    )


def render_default_profile(project_name: str) -> str:
    prefix = default_id_prefix(project_name)
    buckets = ", ".join(f'"{item}"' for item in DEFAULT_BUCKETS)
    domains = ", ".join(f'"{item}"' for item in DEFAULT_DOMAINS)
    validations = ", ".join(f'"{item}"' for item in DEFAULT_VALIDATION_COMMANDS)
    doc_routing = ", ".join(f'"{item}"' for item in DEFAULT_DOC_ROUTING)
    return (
        f'[project]\n'
        f'name = "{project_name}"\n'
        f"profile_version = 1\n\n"
        f'[paths]\n'
        f'control_dir = "{DEFAULT_CONTROL_DIR}"\n'
        f'skill_dir = "{default_host_skill_dir(project_name)}"\n'
        f'worktrees_dir = "{DEFAULT_WORKTREES_DIR}"\n\n'
        f'[ids]\n'
        f'prefix = "{prefix}"\n'
        f'digest_length = 10\n\n'
        f'[rules]\n'
        f'require_claim_for_completion = false\n'
        f'auto_render_html = false\n\n'
        f'[supervisor]\n'
        f'launch_command = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]\n'
        f'max_parallel = 2\n\n'
        f'[taxonomy]\n'
        f'buckets = [{buckets}]\n'
        f'domains = [{domains}]\n'
        f'validation_commands = [{validations}]\n'
        f'doc_routing_defaults = [{doc_routing}]\n\n'
        f'[pm_heuristics]\n'
        f'summary_focus = "Lead with workset status, then runnable tasks, then validation state."\n'
        f'skill_usage = "{DEFAULT_SKILL_USAGE_HEURISTIC}"\n'
    )


def write_default_profile(project_root: Path, project_name: str, *, force: bool = False) -> Path:
    root = project_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    profile_file = root / PROFILE_FILE_NAME
    if profile_file.exists() and not force:
        raise ConfigError(f"Refusing to overwrite {profile_file}; pass force=True to replace it")
    profile_file.write_text(render_default_profile(project_name), encoding="utf-8")
    return profile_file
