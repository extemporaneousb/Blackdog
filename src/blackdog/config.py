from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


PROFILE_FILE_NAME = "blackdog.toml"

DEFAULT_BUCKETS = (
    "core",
    "cli",
    "html",
    "skills",
    "docs",
    "testing",
    "integration",
)
DEFAULT_DOMAINS = (
    "cli",
    "docs",
    "html",
    "state",
    "events",
    "inbox",
    "results",
    "skills",
)
DEFAULT_VALIDATION_COMMANDS = (
    "PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'",
)
DEFAULT_DOC_ROUTING = (
    "AGENTS.md",
    "docs/INDEX.md",
    "docs/ARCHITECTURE.md",
    "docs/CLI.md",
    "docs/FILE_FORMATS.md",
)


class ConfigError(RuntimeError):
    pass


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "project"


def default_id_prefix(project_name: str) -> str:
    letters = re.sub(r"[^A-Za-z0-9]+", "", project_name).upper()
    if not letters:
        return "BDOG"
    return letters[:5]


@dataclass(frozen=True)
class ProjectPaths:
    project_root: Path
    profile_file: Path
    backlog_dir: Path
    backlog_file: Path
    state_file: Path
    events_file: Path
    results_dir: Path
    inbox_file: Path
    html_file: Path
    skill_dir: Path


@dataclass(frozen=True)
class Profile:
    project_name: str
    profile_version: int
    id_prefix: str
    id_digest_length: int
    default_claim_lease_hours: int
    require_claim_for_completion: bool
    auto_render_html: bool
    buckets: tuple[str, ...]
    domains: tuple[str, ...]
    validation_commands: tuple[str, ...]
    doc_routing_defaults: tuple[str, ...]
    pm_heuristics: dict[str, str]
    paths: ProjectPaths


def find_project_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / PROFILE_FILE_NAME).exists():
            return candidate
    raise ConfigError(f"Could not find {PROFILE_FILE_NAME} from {current}")


def _resolve_rel(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()


def _paths_from_raw(project_root: Path, raw_paths: dict[str, str]) -> ProjectPaths:
    return ProjectPaths(
        project_root=project_root,
        profile_file=(project_root / PROFILE_FILE_NAME).resolve(),
        backlog_dir=_resolve_rel(project_root, raw_paths["backlog_dir"]),
        backlog_file=_resolve_rel(project_root, raw_paths["backlog_file"]),
        state_file=_resolve_rel(project_root, raw_paths["state_file"]),
        events_file=_resolve_rel(project_root, raw_paths["events_file"]),
        results_dir=_resolve_rel(project_root, raw_paths["results_dir"]),
        inbox_file=_resolve_rel(project_root, raw_paths["inbox_file"]),
        html_file=_resolve_rel(project_root, raw_paths["html_file"]),
        skill_dir=_resolve_rel(project_root, raw_paths["skill_dir"]),
    )


def load_profile(project_root: Path | None = None) -> Profile:
    root = find_project_root(project_root)
    profile_file = root / PROFILE_FILE_NAME
    with profile_file.open("rb") as handle:
        payload = tomllib.load(handle)

    project = payload.get("project") or {}
    ids = payload.get("ids") or {}
    rules = payload.get("rules") or {}
    taxonomy = payload.get("taxonomy") or {}
    heuristics = payload.get("pm_heuristics") or {}
    raw_paths = payload.get("paths") or {}

    required_path_keys = {
        "backlog_dir",
        "backlog_file",
        "state_file",
        "events_file",
        "results_dir",
        "inbox_file",
        "html_file",
        "skill_dir",
    }
    missing = sorted(required_path_keys - set(raw_paths))
    if missing:
        raise ConfigError(f"Profile is missing path keys: {missing}")

    paths = _paths_from_raw(root, raw_paths)
    return Profile(
        project_name=str(project.get("name") or root.name),
        profile_version=int(project.get("profile_version") or 1),
        id_prefix=str(ids.get("prefix") or default_id_prefix(str(project.get("name") or root.name))),
        id_digest_length=int(ids.get("digest_length") or 10),
        default_claim_lease_hours=int(rules.get("default_claim_lease_hours") or 4),
        require_claim_for_completion=bool(rules.get("require_claim_for_completion", True)),
        auto_render_html=bool(rules.get("auto_render_html", True)),
        buckets=tuple(str(item) for item in taxonomy.get("buckets") or DEFAULT_BUCKETS),
        domains=tuple(str(item) for item in taxonomy.get("domains") or DEFAULT_DOMAINS),
        validation_commands=tuple(
            str(item) for item in taxonomy.get("validation_commands") or DEFAULT_VALIDATION_COMMANDS
        ),
        doc_routing_defaults=tuple(
            str(item) for item in taxonomy.get("doc_routing_defaults") or DEFAULT_DOC_ROUTING
        ),
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
        f'backlog_dir = ".blackdog"\n'
        f'backlog_file = ".blackdog/backlog.md"\n'
        f'state_file = ".blackdog/backlog-state.json"\n'
        f'events_file = ".blackdog/events.jsonl"\n'
        f'results_dir = ".blackdog/task-results"\n'
        f'inbox_file = ".blackdog/inbox.jsonl"\n'
        f'html_file = ".blackdog/backlog-index.html"\n'
        f'skill_dir = ".codex/skills/blackdog-backlog"\n\n'
        f'[ids]\n'
        f'prefix = "{prefix}"\n'
        f"digest_length = 10\n\n"
        f'[rules]\n'
        f"default_claim_lease_hours = 4\n"
        f"require_claim_for_completion = true\n"
        f"auto_render_html = true\n\n"
        f'[taxonomy]\n'
        f"buckets = [{buckets}]\n"
        f"domains = [{domains}]\n"
        f"validation_commands = [{validations}]\n"
        f"doc_routing_defaults = [{doc_routing}]\n\n"
        f'[pm_heuristics]\n'
        f'summary_focus = "Lead with direct status, then backlog state, then test focus."\n'
        f'skill_usage = "Prefer the repo-versioned blackdog CLI over hand-edited state transitions."\n'
    )


def write_default_profile(project_root: Path, project_name: str, *, force: bool = False) -> Path:
    root = project_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    profile_file = root / PROFILE_FILE_NAME
    if profile_file.exists() and not force:
        raise ConfigError(f"Refusing to overwrite {profile_file}; pass force=True to replace it")
    profile_file.write_text(render_default_profile(project_name), encoding="utf-8")
    return profile_file

