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
HANDLER_KIND_PYTHON_OVERLAY_VENV = "python-overlay-venv"
HANDLER_KIND_BLACKDOG_RUNTIME = "blackdog-runtime"
HANDLER_SCRIPT_POLICY_ROOT_BIN_FALLBACK = "root-bin-fallback"
HANDLER_SOURCE_MODE_MANAGED_CHECKOUT = "managed-checkout"
HANDLER_SOURCE_MODE_TARGET_REPO = "target-repo"
HANDLER_SOURCE_MODE_LOCAL_OVERRIDE = "local-override"
HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE = "editable-worktree-source"
HANDLER_INSTALL_MODE_LAUNCHER_SHIM = "launcher-shim"
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
class HandlerConfig:
    handler_id: str
    kind: str
    enabled: bool
    depends_on: tuple[str, ...]


@dataclass(frozen=True)
class PythonOverlayVenvHandlerConfig(HandlerConfig):
    root_path: str
    worktree_path: str
    script_policy: str


@dataclass(frozen=True)
class BlackdogRuntimeHandlerConfig(HandlerConfig):
    launcher_path: str
    source_mode: str
    managed_source_dir: str
    self_repo_install_mode: str
    other_repo_install_mode: str


RepoHandlerConfig = PythonOverlayVenvHandlerConfig | BlackdogRuntimeHandlerConfig


@dataclass(frozen=True)
class RepoProfile:
    project_name: str
    profile_version: int
    validation_commands: tuple[str, ...]
    doc_routing_defaults: tuple[str, ...]
    paths: BlackdogPaths
    handlers: tuple[RepoHandlerConfig, ...]
    handlers_explicit: bool


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


def resolve_config_path(project_root: Path, value: str) -> Path:
    return _resolve_path_value(project_root, value)


def _default_control_paths(control_dir: Path) -> dict[str, Path]:
    return {
        "planning_file": control_dir / "planning.json",
        "runtime_file": control_dir / "runtime.json",
        "events_file": control_dir / "events.jsonl",
    }


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{field} must be a list")
    rows: list[str] = []
    for item in value:
        text = _optional_text(item)
        if text is None:
            raise ConfigError(f"{field} must contain only non-empty strings")
        rows.append(text)
    return tuple(rows)


def _bool_value(value: object, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{field} must be a boolean")


def _python_overlay_handler_defaults() -> PythonOverlayVenvHandlerConfig:
    return PythonOverlayVenvHandlerConfig(
        handler_id="python",
        kind=HANDLER_KIND_PYTHON_OVERLAY_VENV,
        enabled=True,
        depends_on=(),
        root_path=".VE",
        worktree_path=".VE",
        script_policy=HANDLER_SCRIPT_POLICY_ROOT_BIN_FALLBACK,
    )


def _blackdog_runtime_handler_defaults() -> BlackdogRuntimeHandlerConfig:
    return BlackdogRuntimeHandlerConfig(
        handler_id="blackdog",
        kind=HANDLER_KIND_BLACKDOG_RUNTIME,
        enabled=True,
        depends_on=("python",),
        launcher_path=".VE/bin/blackdog",
        source_mode=HANDLER_SOURCE_MODE_MANAGED_CHECKOUT,
        managed_source_dir=f"{GIT_COMMON_TOKEN}/blackdog/source/blackdog",
        self_repo_install_mode=HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE,
        other_repo_install_mode=HANDLER_INSTALL_MODE_LAUNCHER_SHIM,
    )


def default_handler_configs() -> tuple[RepoHandlerConfig, ...]:
    return (
        _python_overlay_handler_defaults(),
        _blackdog_runtime_handler_defaults(),
    )


def _handler_from_payload(payload: dict[str, object], *, index: int) -> RepoHandlerConfig:
    field_prefix = f"handlers[{index}]"
    handler_id = _optional_text(payload.get("id"))
    if handler_id is None:
        raise ConfigError(f"{field_prefix}.id is required")
    kind = _optional_text(payload.get("kind"))
    if kind is None:
        raise ConfigError(f"{field_prefix}.kind is required")
    enabled = True if "enabled" not in payload else _bool_value(payload.get("enabled"), field=f"{field_prefix}.enabled")
    depends_on = _string_tuple(payload.get("depends_on"), field=f"{field_prefix}.depends_on")
    if kind == HANDLER_KIND_PYTHON_OVERLAY_VENV:
        root_path = _optional_text(payload.get("root_path"))
        worktree_path = _optional_text(payload.get("worktree_path"))
        script_policy = _optional_text(payload.get("script_policy"))
        if root_path is None:
            raise ConfigError(f"{field_prefix}.root_path is required")
        if worktree_path is None:
            raise ConfigError(f"{field_prefix}.worktree_path is required")
        if script_policy != HANDLER_SCRIPT_POLICY_ROOT_BIN_FALLBACK:
            raise ConfigError(
                f"{field_prefix}.script_policy must be {HANDLER_SCRIPT_POLICY_ROOT_BIN_FALLBACK!r}"
            )
        return PythonOverlayVenvHandlerConfig(
            handler_id=handler_id,
            kind=kind,
            enabled=enabled,
            depends_on=depends_on,
            root_path=root_path,
            worktree_path=worktree_path,
            script_policy=script_policy,
        )
    if kind == HANDLER_KIND_BLACKDOG_RUNTIME:
        launcher_path = _optional_text(payload.get("launcher_path"))
        source_mode = _optional_text(payload.get("source_mode"))
        managed_source_dir = _optional_text(payload.get("managed_source_dir"))
        self_repo_install_mode = _optional_text(payload.get("self_repo_install_mode"))
        other_repo_install_mode = _optional_text(payload.get("other_repo_install_mode"))
        if launcher_path is None:
            raise ConfigError(f"{field_prefix}.launcher_path is required")
        if source_mode not in {
            HANDLER_SOURCE_MODE_MANAGED_CHECKOUT,
            HANDLER_SOURCE_MODE_TARGET_REPO,
            HANDLER_SOURCE_MODE_LOCAL_OVERRIDE,
        }:
            raise ConfigError(
                f"{field_prefix}.source_mode must be one of "
                f"{sorted({HANDLER_SOURCE_MODE_MANAGED_CHECKOUT, HANDLER_SOURCE_MODE_TARGET_REPO, HANDLER_SOURCE_MODE_LOCAL_OVERRIDE})}"
            )
        if managed_source_dir is None:
            raise ConfigError(f"{field_prefix}.managed_source_dir is required")
        if self_repo_install_mode != HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE:
            raise ConfigError(
                f"{field_prefix}.self_repo_install_mode must be {HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE!r}"
            )
        if other_repo_install_mode != HANDLER_INSTALL_MODE_LAUNCHER_SHIM:
            raise ConfigError(
                f"{field_prefix}.other_repo_install_mode must be {HANDLER_INSTALL_MODE_LAUNCHER_SHIM!r}"
            )
        return BlackdogRuntimeHandlerConfig(
            handler_id=handler_id,
            kind=kind,
            enabled=enabled,
            depends_on=depends_on,
            launcher_path=launcher_path,
            source_mode=source_mode,
            managed_source_dir=managed_source_dir,
            self_repo_install_mode=self_repo_install_mode,
            other_repo_install_mode=other_repo_install_mode,
        )
    raise ConfigError(f"{field_prefix}.kind is not supported: {kind!r}")


def _validate_handler_graph(handlers: tuple[RepoHandlerConfig, ...]) -> None:
    seen: dict[str, RepoHandlerConfig] = {}
    for handler in handlers:
        if handler.handler_id in seen:
            raise ConfigError(f"duplicate handler id {handler.handler_id!r}")
        seen[handler.handler_id] = handler
    for handler in handlers:
        missing = [dependency for dependency in handler.depends_on if dependency not in seen]
        if missing:
            raise ConfigError(
                f"handler {handler.handler_id!r} depends on unknown handlers: {', '.join(missing)}"
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(handler_id: str) -> None:
        if handler_id in visited:
            return
        if handler_id in visiting:
            raise ConfigError(f"handler dependency cycle detected at {handler_id!r}")
        visiting.add(handler_id)
        for dependency in seen[handler_id].depends_on:
            visit(dependency)
        visiting.remove(handler_id)
        visited.add(handler_id)

    for handler in handlers:
        visit(handler.handler_id)


def _load_handlers(payload: object) -> tuple[tuple[RepoHandlerConfig, ...], bool]:
    if payload is None:
        handlers = default_handler_configs()
        _validate_handler_graph(handlers)
        return handlers, False
    if not isinstance(payload, list):
        raise ConfigError("handlers must be an array of tables")
    rows: list[RepoHandlerConfig] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ConfigError("handlers must contain only tables")
        rows.append(_handler_from_payload(item, index=index))
    handlers = tuple(rows)
    _validate_handler_graph(handlers)
    return handlers, True


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
    handlers, handlers_explicit = _load_handlers(payload.get("handlers"))

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
        handlers=handlers,
        handlers_explicit=handlers_explicit,
    )


def render_default_handlers() -> str:
    return (
        "[[handlers]]\n"
        'id = "python"\n'
        f'kind = "{HANDLER_KIND_PYTHON_OVERLAY_VENV}"\n'
        "enabled = true\n"
        'root_path = ".VE"\n'
        'worktree_path = ".VE"\n'
        f'script_policy = "{HANDLER_SCRIPT_POLICY_ROOT_BIN_FALLBACK}"\n\n'
        "[[handlers]]\n"
        'id = "blackdog"\n'
        f'kind = "{HANDLER_KIND_BLACKDOG_RUNTIME}"\n'
        "enabled = true\n"
        'depends_on = ["python"]\n'
        'launcher_path = ".VE/bin/blackdog"\n'
        f'source_mode = "{HANDLER_SOURCE_MODE_MANAGED_CHECKOUT}"\n'
        f'managed_source_dir = "{GIT_COMMON_TOKEN}/blackdog/source/blackdog"\n'
        f'self_repo_install_mode = "{HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE}"\n'
        f'other_repo_install_mode = "{HANDLER_INSTALL_MODE_LAUNCHER_SHIM}"\n'
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
        f'doc_routing_defaults = [{doc_routing}]\n\n'
        f"{render_default_handlers()}"
    )


def ensure_default_handlers_in_profile(profile_file: Path) -> bool:
    text = profile_file.read_text(encoding="utf-8")
    payload = tomllib.loads(text)
    if payload.get("handlers") is not None:
        return False
    profile_file.write_text(text.rstrip() + "\n\n" + render_default_handlers() + "\n", encoding="utf-8")
    return True


def write_default_profile(project_root: Path, project_name: str, *, force: bool = False) -> Path:
    root = project_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    profile_file = root / PROFILE_FILE_NAME
    if profile_file.exists() and not force:
        raise ConfigError(f"Refusing to overwrite {profile_file}; pass force=True to replace it")
    profile_file.write_text(render_default_profile(project_name), encoding="utf-8")
    return profile_file
