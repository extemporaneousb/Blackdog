from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib

from blackdog import __version__ as BLACKDOG_VERSION
from blackdog.contract import LEGACY_MANAGED_SKILL_NAME, MANAGED_SKILLS_ROOT, managed_skill_name, managed_skill_relative_path
from blackdog.repo_lifecycle import (
    AGENTS_FILE_NAME,
    AGENTS_MANAGED_BEGIN,
    AGENTS_MANAGED_END,
    RepoLifecycleError,
    RepoLifecycleResult,
    install_repo,
)
from blackdog_core.codex_sessions import CodexTurn, build_codex_coverage, collect_codex_turns
from blackdog_core.profile import (
    DEFAULT_CONTROL_DIR,
    HANDLER_KIND_BLACKDOG_RUNTIME,
    HANDLER_SOURCE_MODE_MANAGED_CHECKOUT,
    PROFILE_FILE_NAME,
    PROJECT_STATUS_ACTIVE,
    PROJECT_STATUS_ARCHIVED,
    PROJECT_STATUSES,
    RepoProfile,
    ConfigError,
    load_profile,
    resolve_config_path,
)
from blackdog_core.runtime_model import AttemptView, hide_canceled_runtime_model, load_runtime_model
from blackdog_core.state import parse_iso


REPO_TABLE_COLUMNS = (
    "project_name",
    "status",
    "project_root",
    "branch",
    "dirty_count",
    "tasks_total",
    "current_ready_tasks",
    "current_active_attempts",
    "current_blocked_tasks",
    "done_tasks_total",
    "attempts_total",
    "window_attempts",
    "window_problem_attempts",
    "window_success_attempts",
    "window_blocked_attempts",
    "window_failed_attempts",
    "window_abandoned_attempts",
    "window_failure_classes",
    "window_prompt_issue_attempts",
    "window_operator_issue_attempts",
    "window_elapsed_seconds",
    "codex_sessions",
    "codex_user_turns",
    "codex_input_tokens",
    "codex_cached_input_tokens",
    "codex_output_tokens",
    "codex_reasoning_output_tokens",
    "codex_total_tokens",
    "codex_tool_calls",
    "codex_longest_completed_turn_duration_ms",
    "codex_longest_completed_turn_started_at",
    "codex_longest_completed_turn_thread_id",
    "codex_longest_completed_turn_id",
    "implementation_like_unlinked_turns",
    "linked_user_turns",
    "unlinked_user_turns",
    "linked_attempts",
    "unlinked_attempts",
    "cleanup_terminal_attempts",
    "cleanup_retained_worktrees",
    "cleanup_landed_retained_worktrees",
    "cleanup_unlanded_terminal_attempts",
    "blackdog_version",
    "managed_source_mode",
    "managed_source_status",
    "managed_source_head",
    "managed_source_origin",
    "profile_version",
    "runtime_store_version",
    "support_hash",
    "docs_count",
    "validation_count",
    "prompt_modes",
    "models",
    "reasoning_efforts",
    "error",
)
LEGACY_REPO_TABLE_COLUMNS = ("legacy_worksets",)
ALL_REPO_TABLE_COLUMNS = (*REPO_TABLE_COLUMNS, *LEGACY_REPO_TABLE_COLUMNS)

_DISCOVERY_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".VE",
    ".venv",
    ".worktrees",
    "venv",
    "node_modules",
    "__pycache__",
    ".cache",
    "cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    "coverage",
}


@dataclass(frozen=True, slots=True)
class RepoTableResult:
    action: str
    roots: tuple[str, ...]
    since: str | None
    include_archived: bool
    include_codex: bool
    include_legacy_worksets: bool
    columns: tuple[str, ...]
    rows: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "roots": list(self.roots),
            "since": self.since,
            "include_archived": self.include_archived,
            "include_codex": self.include_codex,
            "include_legacy_worksets": self.include_legacy_worksets,
            "columns": list(self.columns),
            "rows": [dict(row) for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class RepoStatusResult:
    action: str
    project_root: str
    profile_path: str
    previous_status: str
    status: str
    updated: tuple[str, ...]
    preserved: tuple[str, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "project_root": self.project_root,
            "profile_path": self.profile_path,
            "previous_status": self.previous_status,
            "status": self.status,
            "updated": list(self.updated),
            "preserved": list(self.preserved),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class RepoUnbindResult:
    action: str
    project_root: str
    confirmed: bool
    profile_path: str
    control_dir: str | None
    planned_updates: tuple[str, ...]
    planned_removals: tuple[str, ...]
    updated: tuple[str, ...]
    removed: tuple[str, ...]
    preserved: tuple[str, ...]
    warnings: tuple[str, ...]
    unrelated_dirty_paths: tuple[str, ...]
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "project_root": self.project_root,
            "confirmed": self.confirmed,
            "profile_path": self.profile_path,
            "control_dir": self.control_dir,
            "planned_updates": list(self.planned_updates),
            "planned_removals": list(self.planned_removals),
            "updated": list(self.updated),
            "removed": list(self.removed),
            "preserved": list(self.preserved),
            "warnings": list(self.warnings),
            "unrelated_dirty_paths": list(self.unrelated_dirty_paths),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class _MembershipContext:
    project_root: Path
    project_name: str
    status: str
    profile_path: Path
    control_dir: Path | None


def _run_git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
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


def _git_common_dir(repo_root: Path) -> Path | None:
    try:
        raw = _run_git(repo_root, "rev-parse", "--git-common-dir")
    except RepoLifecycleError:
        return None
    candidate = Path(raw)
    return candidate.resolve() if candidate.is_absolute() else (repo_root / candidate).resolve()


def _project_status_from_payload(project: object) -> str:
    if project is None:
        return PROJECT_STATUS_ACTIVE
    if not isinstance(project, dict):
        raise RepoLifecycleError("project table must be a TOML table")
    raw_status = project.get("status")
    if raw_status is None:
        return PROJECT_STATUS_ACTIVE
    status = str(raw_status).strip()
    if status not in PROJECT_STATUSES:
        raise RepoLifecycleError(f"project.status must be one of {', '.join(PROJECT_STATUSES)}")
    return status


def _load_toml_payload(profile_path: Path) -> dict[str, Any]:
    try:
        payload = tomllib.loads(profile_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RepoLifecycleError(f"could not read {profile_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise RepoLifecycleError(f"{profile_path} is not valid TOML: {exc}") from exc
    if not isinstance(payload, dict):
        raise RepoLifecycleError(f"{profile_path} must contain a TOML table")
    return payload


def _profile_control_dir(repo_root: Path, payload: dict[str, Any]) -> Path | None:
    raw_paths = payload.get("paths") or {}
    if not isinstance(raw_paths, dict):
        raise RepoLifecycleError("paths table must be a TOML table")
    if "control_dir" in raw_paths:
        return resolve_config_path(repo_root, str(raw_paths["control_dir"]))
    if "planning_file" in raw_paths:
        return resolve_config_path(repo_root, str(raw_paths["planning_file"])).parent
    return resolve_config_path(repo_root, DEFAULT_CONTROL_DIR)


def _load_membership_context(project_root: Path) -> _MembershipContext:
    repo_root = _resolve_repo_root(project_root)
    profile_path = (repo_root / PROFILE_FILE_NAME).resolve()
    if not profile_path.is_file():
        raise RepoLifecycleError(f"{profile_path} is missing; run `blackdog repo bind` first")
    payload = _load_toml_payload(profile_path)
    project = payload.get("project") or {}
    if not isinstance(project, dict):
        raise RepoLifecycleError("project table must be a TOML table")
    project_name = str(project.get("name") or repo_root.name)
    status = _project_status_from_payload(project)
    return _MembershipContext(
        project_root=repo_root,
        project_name=project_name,
        status=status,
        profile_path=profile_path,
        control_dir=_profile_control_dir(repo_root, payload),
    )


def bind_repo(
    project_root: Path,
    *,
    project_name: str | None = None,
    source_root: str | None = None,
) -> RepoLifecycleResult:
    return replace(
        install_repo(project_root, project_name=project_name, source_root=source_root),
        action="bind",
    )


def _set_project_status_text(text: str, status: str) -> str:
    lines = text.splitlines(keepends=True)
    project_start: int | None = None
    table_header = re.compile(r"^\s*\[[^\n]*\]\s*(?:#.*)?$")
    project_header = re.compile(r"^\s*\[project\]\s*(?:#.*)?$")
    for index, line in enumerate(lines):
        if project_header.match(line):
            project_start = index
            break

    status_line = f'status = "{status}"\n'
    if project_start is None:
        prefix = "[project]\n" + status_line + "\n"
        return prefix + text.lstrip("\n")

    project_end = len(lines)
    for index in range(project_start + 1, len(lines)):
        if table_header.match(lines[index]):
            project_end = index
            break

    status_key = re.compile(r"^(\s*)status\s*=.*$")
    for index in range(project_start + 1, project_end):
        match = status_key.match(lines[index].rstrip("\n"))
        if not match:
            continue
        newline = "\n" if lines[index].endswith("\n") else ""
        lines[index] = f'{match.group(1)}status = "{status}"{newline}'
        return "".join(lines)

    lines.insert(project_start + 1, status_line)
    return "".join(lines)


def set_repo_status(project_root: Path, *, status: str, action: str, reason: str | None = None) -> RepoStatusResult:
    if status not in PROJECT_STATUSES:
        raise RepoLifecycleError(f"status must be one of {', '.join(PROJECT_STATUSES)}")
    repo_root = _resolve_repo_root(project_root)
    profile_path = (repo_root / PROFILE_FILE_NAME).resolve()
    if not profile_path.is_file():
        raise RepoLifecycleError(f"{profile_path} is missing; run `blackdog repo bind` first")
    old_text = profile_path.read_text(encoding="utf-8")
    payload = _load_toml_payload(profile_path)
    previous_status = _project_status_from_payload(payload.get("project") or {})
    notes: list[str] = []
    if reason:
        notes.append(f"archive reason: {reason}")
    if previous_status == status:
        return RepoStatusResult(
            action=action,
            project_root=str(repo_root),
            profile_path=str(profile_path),
            previous_status=previous_status,
            status=status,
            updated=(),
            preserved=(str(profile_path),),
            notes=tuple(notes),
        )

    new_text = _set_project_status_text(old_text, status)
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise RepoLifecycleError(f"refusing to write invalid {PROFILE_FILE_NAME}: {exc}") from exc
    profile_path.write_text(new_text, encoding="utf-8")
    try:
        profile = load_profile(repo_root)
    except ConfigError as exc:
        raise RepoLifecycleError(f"updated profile did not load cleanly: {exc}") from exc
    if profile.status != status:
        raise RepoLifecycleError(f"updated profile status mismatch: expected {status}, loaded {profile.status}")
    return RepoStatusResult(
        action=action,
        project_root=str(repo_root),
        profile_path=str(profile_path),
        previous_status=previous_status,
        status=status,
        updated=(str(profile_path),),
        preserved=(),
        notes=tuple(notes),
    )


def archive_repo(project_root: Path, *, reason: str | None = None) -> RepoStatusResult:
    return set_repo_status(project_root, status=PROJECT_STATUS_ARCHIVED, action="archive", reason=reason)


def unarchive_repo(project_root: Path) -> RepoStatusResult:
    return set_repo_status(project_root, status=PROJECT_STATUS_ACTIVE, action="unarchive")


def discover_profile_dirs(root: Path) -> tuple[Path, ...]:
    candidate = root.resolve()
    if not candidate.exists():
        raise RepoLifecycleError(f"discovery root does not exist: {candidate}")
    if candidate.is_file():
        return (candidate.parent,) if candidate.name == PROFILE_FILE_NAME else ()
    discovered: list[Path] = []
    for current_root, dirnames, filenames in os.walk(candidate):
        dirnames[:] = sorted(name for name in dirnames if name not in _DISCOVERY_SKIP_DIRS)
        if PROFILE_FILE_NAME in filenames:
            discovered.append(Path(current_root).resolve())
            dirnames[:] = []
    return tuple(discovered)


def _current_branch(repo_root: Path) -> str | None:
    try:
        branch = _run_git(repo_root, "branch", "--show-current")
    except RepoLifecycleError:
        return None
    if branch:
        return branch
    try:
        commit = _run_git(repo_root, "rev-parse", "--short", "HEAD")
    except RepoLifecycleError:
        return None
    return f"detached:{commit}" if commit else None


def _dirty_count(repo_root: Path) -> int | None:
    try:
        output = _run_git(repo_root, "status", "--porcelain=v1", "-uall")
    except RepoLifecycleError:
        return None
    return len([line for line in output.splitlines() if line.strip()])


def _empty_table_row(project_root: Path) -> dict[str, object]:
    return {
        "project_name": project_root.name,
        "status": PROJECT_STATUS_ACTIVE,
        "project_root": str(project_root.resolve()),
        "branch": None,
        "dirty_count": None,
        "tasks_total": None,
        "current_ready_tasks": None,
        "current_active_attempts": None,
        "current_blocked_tasks": None,
        "done_tasks_total": None,
        "attempts_total": None,
        "window_attempts": None,
        "window_problem_attempts": None,
        "window_success_attempts": None,
        "window_blocked_attempts": None,
        "window_failed_attempts": None,
        "window_abandoned_attempts": None,
        "window_failure_classes": "",
        "window_prompt_issue_attempts": None,
        "window_operator_issue_attempts": None,
        "window_elapsed_seconds": None,
        "codex_sessions": None,
        "codex_user_turns": None,
        "codex_input_tokens": None,
        "codex_cached_input_tokens": None,
        "codex_output_tokens": None,
        "codex_reasoning_output_tokens": None,
        "codex_total_tokens": None,
        "codex_tool_calls": None,
        "codex_longest_completed_turn_duration_ms": None,
        "codex_longest_completed_turn_started_at": None,
        "codex_longest_completed_turn_thread_id": None,
        "codex_longest_completed_turn_id": None,
        "implementation_like_unlinked_turns": None,
        "linked_user_turns": None,
        "unlinked_user_turns": None,
        "linked_attempts": None,
        "unlinked_attempts": None,
        "cleanup_terminal_attempts": None,
        "cleanup_retained_worktrees": None,
        "cleanup_landed_retained_worktrees": None,
        "cleanup_unlanded_terminal_attempts": None,
        "blackdog_version": None,
        "managed_source_mode": None,
        "managed_source_status": None,
        "managed_source_head": None,
        "managed_source_origin": None,
        "profile_version": None,
        "runtime_store_version": None,
        "support_hash": None,
        "docs_count": None,
        "validation_count": None,
        "prompt_modes": "",
        "models": "",
        "reasoning_efforts": "",
        "legacy_worksets": None,
        "error": None,
    }


def _append_error(errors: list[str], message: str) -> None:
    if message and message not in errors:
        errors.append(message)


def _string_set_label(values: set[str]) -> str:
    return ",".join(sorted(value for value in values if value))


def _count_label(counts: dict[str, int]) -> str:
    return ",".join(f"{key}={counts[key]}" for key in sorted(counts) if key and counts[key])


def _parse_table_since(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = parse_iso(value)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(f"{value}T00:00:00")
        except ValueError as exc:
            raise RepoLifecycleError(f"--since must be an ISO timestamp or date: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _attempt_in_window(attempt: AttemptView, cutoff: datetime | None, now: datetime) -> bool:
    if cutoff is None:
        return True
    ended = parse_iso(attempt.ended_at) or (now if attempt.is_active else None)
    return ended is not None and ended >= cutoff


def _attempt_elapsed_seconds(attempt: AttemptView, *, now: datetime, cutoff: datetime | None) -> int:
    if cutoff is None and attempt.elapsed_seconds is not None:
        return max(0, int(attempt.elapsed_seconds))
    started = parse_iso(attempt.started_at)
    if started is None:
        return 0
    ended = parse_iso(attempt.ended_at) or (now if attempt.is_active else None)
    if ended is None:
        return 0
    window_start = max(started, cutoff) if cutoff is not None else started
    return max(0, int((ended - window_start).total_seconds()))


def _add_attempt_labels(
    attempt: AttemptView,
    *,
    models: set[str],
    prompt_modes: set[str],
    reasoning_counts: dict[str, int],
) -> None:
    if attempt.model:
        models.add(attempt.model)
    if attempt.reasoning_effort:
        reasoning_counts[attempt.reasoning_effort] = reasoning_counts.get(attempt.reasoning_effort, 0) + 1
    for receipt in (attempt.prompt_receipt, attempt.user_prompt_receipt):
        if receipt is not None and receipt.mode:
            prompt_modes.add(receipt.mode)


def _runtime_store_version(profile: RepoProfile) -> str | None:
    try:
        payload = json.loads(profile.paths.runtime_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("store_version")
    return str(value).strip() if value else None


def _managed_agents_block_text(project_root: Path) -> str:
    agents_path = project_root / AGENTS_FILE_NAME
    try:
        text = agents_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    start = text.find(AGENTS_MANAGED_BEGIN)
    end = text.find(AGENTS_MANAGED_END)
    if start == -1 or end == -1 or end < start:
        return ""
    return text[start : end + len(AGENTS_MANAGED_END)]


def _support_hash(profile: RepoProfile) -> str:
    chunks: list[str] = [
        f"blackdog_version={BLACKDOG_VERSION}",
        f"profile_version={profile.profile_version}",
    ]
    for path in (
        profile.paths.project_root / PROFILE_FILE_NAME,
        profile.paths.project_root / managed_skill_relative_path(profile),
        profile.paths.project_root / MANAGED_SKILLS_ROOT / managed_skill_name(profile) / "agents" / "openai.yaml",
    ):
        try:
            chunks.append(path.read_text(encoding="utf-8"))
        except OSError:
            if path.is_relative_to(profile.paths.project_root):
                missing = path.relative_to(profile.paths.project_root)
            else:
                missing = path
            chunks.append(f"missing:{missing}")
    chunks.append(_managed_agents_block_text(profile.paths.project_root))
    return hashlib.sha256("\n\n".join(chunks).encode("utf-8")).hexdigest()[:12]


def attempt_cleanup_health_counts(attempts: Iterable[AttemptView]) -> dict[str, int]:
    counts = {
        "cleanup_terminal_attempts": 0,
        "cleanup_retained_worktrees": 0,
        "cleanup_landed_retained_worktrees": 0,
        "cleanup_unlanded_terminal_attempts": 0,
    }
    for attempt in attempts:
        if attempt.is_active or not (attempt.worktree_path or attempt.branch):
            continue
        counts["cleanup_terminal_attempts"] += 1
        worktree_exists = _recorded_worktree_exists(attempt.worktree_path)
        if worktree_exists:
            counts["cleanup_retained_worktrees"] += 1
            if attempt.landed_commit:
                counts["cleanup_landed_retained_worktrees"] += 1
        if not attempt.landed_commit:
            counts["cleanup_unlanded_terminal_attempts"] += 1
    return counts


def _recorded_worktree_exists(worktree_path: str | None) -> bool:
    if not worktree_path:
        return False
    try:
        return Path(worktree_path).expanduser().exists()
    except OSError:
        return False


def _git_ref(repo_root: Path, ref: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--short", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _git_count(repo_root: Path, rev_range: str) -> int | None:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "rev-list", "--count", rev_range],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        return int(completed.stdout.strip())
    except ValueError:
        return None


def _managed_source_state(profile: RepoProfile) -> dict[str, object | None]:
    for handler in profile.handlers:
        if handler.kind != HANDLER_KIND_BLACKDOG_RUNTIME or not handler.enabled:
            continue
        source_mode = getattr(handler, "source_mode", None)
        if source_mode != HANDLER_SOURCE_MODE_MANAGED_CHECKOUT:
            return {
                "managed_source_mode": source_mode,
                "managed_source_status": source_mode,
                "managed_source_head": None,
                "managed_source_origin": None,
            }
        source_root = resolve_config_path(profile.paths.project_root, str(getattr(handler, "managed_source_dir")))
        if not (source_root / ".git").exists():
            return {
                "managed_source_mode": source_mode,
                "managed_source_status": "missing",
                "managed_source_head": None,
                "managed_source_origin": None,
            }
        head = _git_ref(source_root, "HEAD")
        origin = _git_ref(source_root, "origin/main")
        if head is None:
            status = "unknown"
        elif origin is None:
            status = "no_origin"
        else:
            ahead = _git_count(source_root, "origin/main..HEAD")
            behind = _git_count(source_root, "HEAD..origin/main")
            if ahead == 0 and behind == 0:
                status = "current"
            elif ahead and behind:
                status = "diverged"
            elif ahead:
                status = "ahead"
            elif behind:
                status = "behind"
            else:
                status = "unknown"
        return {
            "managed_source_mode": source_mode,
            "managed_source_status": status,
            "managed_source_head": head,
            "managed_source_origin": origin,
        }
    return {
        "managed_source_mode": None,
        "managed_source_status": "unconfigured",
        "managed_source_head": None,
        "managed_source_origin": None,
    }


def _repo_table_row(
    profile_dir: Path,
    *,
    since: str | None,
    include_codex: bool,
    codex_turns: tuple[CodexTurn, ...] | None = None,
    codex_read_error: str | None = None,
) -> dict[str, object]:
    row = _empty_table_row(profile_dir)
    errors: list[str] = []
    cutoff = _parse_table_since(since)
    try:
        profile = load_profile(profile_dir)
    except (ConfigError, RepoLifecycleError, OSError, tomllib.TOMLDecodeError) as exc:
        row["error"] = str(exc)
        return {column: row.get(column) for column in ALL_REPO_TABLE_COLUMNS}

    row["project_name"] = profile.project_name
    row["status"] = profile.status
    row["project_root"] = str(profile.paths.project_root)
    row["branch"] = _current_branch(profile.paths.project_root)
    row["dirty_count"] = _dirty_count(profile.paths.project_root)
    row["blackdog_version"] = BLACKDOG_VERSION
    row.update(_managed_source_state(profile))
    row["profile_version"] = profile.profile_version
    row["runtime_store_version"] = _runtime_store_version(profile)
    row["support_hash"] = _support_hash(profile)
    row["docs_count"] = len(profile.doc_routing_defaults)
    row["validation_count"] = len(profile.validation_commands)
    window_attempt_views: tuple[AttemptView, ...] = ()

    try:
        model = hide_canceled_runtime_model(load_runtime_model(profile))
        counts = model.counts
        attempts = tuple(attempt for workset in model.worksets for attempt in workset.attempts)
        row.update(attempt_cleanup_health_counts(attempts))
        now = datetime.now().astimezone()
        window_attempts = tuple(attempt for attempt in attempts if _attempt_in_window(attempt, cutoff, now))
        window_attempt_views = window_attempts
        window_status_counts: dict[str, int] = {}
        window_failure_counts: dict[str, int] = {}
        for attempt in window_attempts:
            window_status_counts[attempt.status] = window_status_counts.get(attempt.status, 0) + 1
            if attempt.failure_class:
                window_failure_counts[attempt.failure_class] = window_failure_counts.get(attempt.failure_class, 0) + 1
        row["legacy_worksets"] = counts.get("worksets", 0)
        row["tasks_total"] = counts.get("tasks", 0)
        row["current_ready_tasks"] = counts.get("ready", 0)
        row["current_active_attempts"] = counts.get("active_attempts", 0)
        row["current_blocked_tasks"] = counts.get("blocked", 0)
        row["done_tasks_total"] = counts.get("done", 0)
        row["attempts_total"] = counts.get("attempts", 0)
        row["window_attempts"] = len(window_attempts)
        row["window_problem_attempts"] = sum(window_status_counts.get(status, 0) for status in ("blocked", "failed", "abandoned"))
        row["window_success_attempts"] = window_status_counts.get("success", 0)
        row["window_blocked_attempts"] = window_status_counts.get("blocked", 0)
        row["window_failed_attempts"] = window_status_counts.get("failed", 0)
        row["window_abandoned_attempts"] = window_status_counts.get("abandoned", 0)
        row["window_failure_classes"] = _count_label(window_failure_counts)
        row["window_prompt_issue_attempts"] = sum(1 for attempt in window_attempts if attempt.prompt_issue)
        row["window_operator_issue_attempts"] = sum(1 for attempt in window_attempts if attempt.operator_issue)
        row["window_elapsed_seconds"] = sum(_attempt_elapsed_seconds(attempt, now=now, cutoff=cutoff) for attempt in window_attempts)
    except Exception as exc:  # read model errors should not hide other repos
        _append_error(errors, f"runtime summary failed: {exc}")

    models: set[str] = set()
    prompt_modes: set[str] = set()
    reasoning_counts: dict[str, int] = {}
    for attempt in window_attempt_views:
        _add_attempt_labels(attempt, models=models, prompt_modes=prompt_modes, reasoning_counts=reasoning_counts)

    if include_codex:
        try:
            if codex_read_error is not None:
                raise RepoLifecycleError(codex_read_error)
            coverage = build_codex_coverage(profile, since=since, codex_turns=codex_turns)
            coverage_counts = coverage["counts"]
            row["codex_sessions"] = coverage_counts.get("codex_sessions", 0)
            row["codex_user_turns"] = coverage_counts.get("codex_user_turns", 0)
            row["codex_input_tokens"] = coverage_counts.get("input_tokens", 0)
            row["codex_cached_input_tokens"] = coverage_counts.get("cached_input_tokens", 0)
            row["codex_output_tokens"] = coverage_counts.get("output_tokens", 0)
            row["codex_reasoning_output_tokens"] = coverage_counts.get("reasoning_output_tokens", 0)
            row["codex_total_tokens"] = coverage_counts.get("total_tokens", 0)
            row["codex_tool_calls"] = coverage_counts.get("tool_calls", 0)
            row["codex_longest_completed_turn_duration_ms"] = coverage_counts.get(
                "longest_completed_turn_duration_ms",
            )
            row["codex_longest_completed_turn_started_at"] = coverage_counts.get(
                "longest_completed_turn_started_at",
            )
            row["codex_longest_completed_turn_thread_id"] = coverage_counts.get(
                "longest_completed_turn_thread_id",
            )
            row["codex_longest_completed_turn_id"] = coverage_counts.get("longest_completed_turn_id")
            row["implementation_like_unlinked_turns"] = coverage_counts.get(
                "implementation_like_unlinked_turns",
                0,
            )
            row["linked_user_turns"] = coverage_counts.get("linked_user_turns", 0)
            row["unlinked_user_turns"] = coverage_counts.get("unlinked_user_turns", 0)
            row["linked_attempts"] = coverage_counts.get("linked_attempts", 0)
            row["unlinked_attempts"] = coverage_counts.get("unlinked_attempts", 0)
            for turn in coverage.get("turns", ()):
                model = turn.get("model")
                if model:
                    models.add(str(model))
                reasoning_effort = turn.get("reasoning_effort")
                if reasoning_effort:
                    effort = str(reasoning_effort)
                    reasoning_counts[effort] = reasoning_counts.get(effort, 0) + 1
        except Exception as exc:
            _append_error(errors, f"codex coverage failed: {exc}")
    else:
        row["codex_sessions"] = None
        row["codex_user_turns"] = None
        row["codex_input_tokens"] = None
        row["codex_cached_input_tokens"] = None
        row["codex_output_tokens"] = None
        row["codex_reasoning_output_tokens"] = None
        row["codex_total_tokens"] = None
        row["codex_tool_calls"] = None
        row["codex_longest_completed_turn_duration_ms"] = None
        row["codex_longest_completed_turn_started_at"] = None
        row["codex_longest_completed_turn_thread_id"] = None
        row["codex_longest_completed_turn_id"] = None
        row["implementation_like_unlinked_turns"] = None
        row["linked_user_turns"] = None
        row["unlinked_user_turns"] = None
        row["linked_attempts"] = None
        row["unlinked_attempts"] = None

    row["prompt_modes"] = _string_set_label(prompt_modes)
    row["models"] = _string_set_label(models)
    row["reasoning_efforts"] = _count_label(reasoning_counts)
    row["error"] = "; ".join(errors) if errors else None
    return {column: row.get(column) for column in ALL_REPO_TABLE_COLUMNS}


def build_repo_table(
    roots: tuple[Path, ...],
    *,
    since: str | None = None,
    include_archived: bool = False,
    include_codex: bool = True,
    include_legacy_worksets: bool = False,
) -> RepoTableResult:
    if not roots:
        raise RepoLifecycleError("repo table requires at least one --root")
    discovered: list[Path] = []
    for root in roots:
        discovered.extend(discover_profile_dirs(root))

    rows: list[dict[str, object]] = []
    seen_roots: set[Path] = set()
    codex_turns: tuple[CodexTurn, ...] | None = None
    codex_read_error: str | None = None
    if include_codex:
        try:
            codex_turns = collect_codex_turns(since=since)
        except Exception as exc:
            codex_read_error = str(exc)
    for profile_dir in sorted(dict.fromkeys(discovered)):
        row = _repo_table_row(
            profile_dir,
            since=since,
            include_codex=include_codex,
            codex_turns=codex_turns,
            codex_read_error=codex_read_error,
        )
        project_root = Path(str(row["project_root"])).resolve()
        if project_root in seen_roots:
            continue
        seen_roots.add(project_root)
        if row["status"] == PROJECT_STATUS_ARCHIVED and not include_archived:
            continue
        rows.append(row)

    columns = (*REPO_TABLE_COLUMNS, *LEGACY_REPO_TABLE_COLUMNS) if include_legacy_worksets else REPO_TABLE_COLUMNS
    return RepoTableResult(
        action="table",
        roots=tuple(str(root.resolve()) for root in roots),
        since=since,
        include_archived=include_archived,
        include_codex=include_codex,
        include_legacy_worksets=include_legacy_worksets,
        columns=columns,
        rows=tuple({column: row.get(column) for column in columns} for row in rows),
    )


def _tsv_value(value: object) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def render_repo_table_text(result: RepoTableResult) -> str:
    lines = ["\t".join(result.columns)]
    for row in result.rows:
        lines.append("\t".join(_tsv_value(row.get(column)) for column in result.columns))
    return "\n".join(lines) + "\n"


def render_repo_status_text(result: RepoStatusResult) -> str:
    lines = [
        f"[blackdog-repo] action: {result.action}",
        f"[blackdog-repo] project root: {result.project_root}",
        f"[blackdog-repo] profile: {result.profile_path}",
        f"[blackdog-repo] previous status: {result.previous_status}",
        f"[blackdog-repo] status: {result.status}",
    ]
    if result.updated:
        lines.append(f"[blackdog-repo] updated: {', '.join(result.updated)}")
    if result.preserved:
        lines.append(f"[blackdog-repo] preserved: {', '.join(result.preserved)}")
    for note in result.notes:
        lines.append(f"[blackdog-repo] note: {note}")
    return "\n".join(lines) + "\n"


def _strip_managed_agents_block(text: str) -> tuple[str, bool]:
    start = text.find(AGENTS_MANAGED_BEGIN)
    end = text.find(AGENTS_MANAGED_END)
    if start == -1 or end == -1 or end < start:
        return text, False
    end += len(AGENTS_MANAGED_END)
    prefix = text[:start].rstrip()
    suffix = text[end:].lstrip("\n")
    parts = [part for part in (prefix, suffix.rstrip()) if part]
    if not parts:
        return "", True
    return "\n\n".join(parts).rstrip() + "\n", True


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _managed_skill_dir(repo_root: Path, project_name: str) -> Path:
    return (repo_root / MANAGED_SKILLS_ROOT / managed_skill_name(project_name)).resolve()


def _legacy_managed_skill_dir(repo_root: Path) -> Path:
    return (repo_root / MANAGED_SKILLS_ROOT / LEGACY_MANAGED_SKILL_NAME).resolve()


def _looks_like_managed_skill_dir(skill_dir: Path) -> bool:
    if (skill_dir / ".blackdog-managed.json").is_file():
        return True
    skill_path = skill_dir / "SKILL.md"
    if not skill_path.is_file():
        return False
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return (
        "Repo-local AI development workflow" in text
        and "backed by Blackdog" in text
        and "`blackdog.toml` is the machine-readable source of truth" in text
    )


def _dirty_paths(repo_root: Path) -> tuple[str, ...]:
    output = _run_git(repo_root, "status", "--porcelain=v1", "-uall")
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        raw_path = line[3:]
        if " -> " in raw_path:
            left, _, right = raw_path.partition(" -> ")
            paths.extend([left, right])
        else:
            paths.append(raw_path)
    return tuple(dict.fromkeys(paths))


def _planned_relative_roots(repo_root: Path, paths: tuple[Path, ...]) -> tuple[str, ...]:
    roots: list[str] = []
    for path in paths:
        if not _is_relative_to(path, repo_root):
            continue
        relative = path.resolve().relative_to(repo_root.resolve()).as_posix()
        if relative:
            roots.append(relative)
    return tuple(dict.fromkeys(roots))


def _dirty_path_matches(path: str, planned_root: str) -> bool:
    normalized = path.strip().strip('"')
    return normalized == planned_root or normalized.startswith(planned_root.rstrip("/") + "/")


def _unrelated_dirty_paths(repo_root: Path, planned_paths: tuple[Path, ...]) -> tuple[str, ...]:
    planned_roots = _planned_relative_roots(repo_root, planned_paths)
    unrelated: list[str] = []
    for dirty_path in _dirty_paths(repo_root):
        if any(_dirty_path_matches(dirty_path, planned_root) for planned_root in planned_roots):
            continue
        unrelated.append(dirty_path)
    return tuple(unrelated)


def _remove_path(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def _remove_control_dir(control_dir: Path, *, repo_root: Path) -> tuple[str, ...]:
    if not control_dir.exists():
        return ()
    history_path = (repo_root / ".blackdog" / "history.jsonl").resolve()
    if control_dir.resolve() != (repo_root / ".blackdog").resolve() or not history_path.exists():
        return (str(control_dir),) if _remove_path(control_dir) else ()

    removed: list[str] = []
    for child in sorted(control_dir.iterdir()):
        if child.resolve() == history_path:
            continue
        if _remove_path(child):
            removed.append(str(child))
    return tuple(removed)


def unbind_repo(
    project_root: Path,
    *,
    confirm: bool = False,
    keep_control_dir: bool = False,
) -> RepoUnbindResult:
    context = _load_membership_context(project_root)
    repo_root = context.project_root
    agents_path = (repo_root / AGENTS_FILE_NAME).resolve()
    managed_skill_dir = _managed_skill_dir(repo_root, context.project_name)
    legacy_skill_dir = _legacy_managed_skill_dir(repo_root)
    launcher_path = (repo_root / ".VE" / "bin" / "blackdog").resolve()
    planned_updates: list[Path] = []
    planned_removals: list[Path] = []
    preserved: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    agents_text = agents_path.read_text(encoding="utf-8") if agents_path.is_file() else ""
    _, has_managed_agents_block = _strip_managed_agents_block(agents_text)
    if has_managed_agents_block:
        planned_updates.append(agents_path)
    elif agents_path.exists():
        preserved.append(str(agents_path))
        notes.append("AGENTS.md has no managed Blackdog block to strip")

    if context.profile_path.exists():
        planned_removals.append(context.profile_path)

    if managed_skill_dir.exists():
        planned_removals.append(managed_skill_dir)

    if legacy_skill_dir.exists() and legacy_skill_dir != managed_skill_dir:
        if _looks_like_managed_skill_dir(legacy_skill_dir):
            planned_removals.append(legacy_skill_dir)
        else:
            preserved.append(str(legacy_skill_dir))
            notes.append("preserved legacy .codex/skills/blackdog because it does not look Blackdog-managed")

    if launcher_path.exists():
        planned_removals.append(launcher_path)

    control_dir = context.control_dir
    if control_dir is not None:
        if keep_control_dir:
            preserved.append(str(control_dir))
            notes.append("preserved control dir because --keep-control-dir was set")
        elif control_dir.exists():
            git_common = _git_common_dir(repo_root)
            if _is_relative_to(control_dir, repo_root) or (git_common is not None and _is_relative_to(control_dir, git_common)):
                planned_removals.append(control_dir)
            else:
                preserved.append(str(control_dir))
                warnings.append(f"preserved external control dir outside repo/git-common: {control_dir}")

    history_path = (repo_root / ".blackdog" / "history.jsonl").resolve()
    if history_path.exists():
        preserved.append(str(history_path))
        notes.append("preserved .blackdog/history.jsonl")

    planned_paths = tuple(dict.fromkeys([*planned_updates, *planned_removals]))
    unrelated_dirty = _unrelated_dirty_paths(repo_root, planned_paths)
    updated: list[str] = []
    removed: list[str] = []

    if confirm:
        if agents_path in planned_updates:
            new_agents_text, changed = _strip_managed_agents_block(agents_text)
            if changed and new_agents_text != agents_text:
                agents_path.write_text(new_agents_text, encoding="utf-8")
                updated.append(str(agents_path))
        for path in planned_removals:
            if control_dir is not None and path == control_dir:
                removed.extend(_remove_control_dir(control_dir, repo_root=repo_root))
            elif _remove_path(path):
                removed.append(str(path))
    else:
        notes.append("preview only; pass --confirm to remove planned Blackdog-managed paths")

    return RepoUnbindResult(
        action="unbind",
        project_root=str(repo_root),
        confirmed=confirm,
        profile_path=str(context.profile_path),
        control_dir=str(control_dir) if control_dir is not None else None,
        planned_updates=tuple(str(path) for path in planned_updates),
        planned_removals=tuple(str(path) for path in planned_removals),
        updated=tuple(dict.fromkeys(updated)),
        removed=tuple(dict.fromkeys(removed)),
        preserved=tuple(dict.fromkeys(preserved)),
        warnings=tuple(warnings),
        unrelated_dirty_paths=unrelated_dirty,
        notes=tuple(notes),
    )


def render_repo_unbind_text(result: RepoUnbindResult) -> str:
    lines = [
        f"[blackdog-repo] action: {result.action}",
        f"[blackdog-repo] confirmed: {'yes' if result.confirmed else 'no'}",
        f"[blackdog-repo] project root: {result.project_root}",
        f"[blackdog-repo] profile: {result.profile_path}",
    ]
    if result.control_dir:
        lines.append(f"[blackdog-repo] control dir: {result.control_dir}")
    if result.planned_updates:
        lines.append(f"[blackdog-repo] planned updates: {', '.join(result.planned_updates)}")
    if result.planned_removals:
        lines.append(f"[blackdog-repo] planned removals: {', '.join(result.planned_removals)}")
    if result.updated:
        lines.append(f"[blackdog-repo] updated: {', '.join(result.updated)}")
    if result.removed:
        lines.append(f"[blackdog-repo] removed: {', '.join(result.removed)}")
    if result.preserved:
        lines.append(f"[blackdog-repo] preserved: {', '.join(result.preserved)}")
    if result.unrelated_dirty_paths:
        lines.append(f"[blackdog-repo] unrelated dirty paths: {', '.join(result.unrelated_dirty_paths)}")
    for warning in result.warnings:
        lines.append(f"[blackdog-repo] warning: {warning}")
    for note in result.notes:
        lines.append(f"[blackdog-repo] note: {note}")
    return "\n".join(lines) + "\n"


__all__ = [
    "REPO_TABLE_COLUMNS",
    "RepoStatusResult",
    "RepoTableResult",
    "RepoUnbindResult",
    "archive_repo",
    "attempt_cleanup_health_counts",
    "bind_repo",
    "build_repo_table",
    "discover_profile_dirs",
    "render_repo_status_text",
    "render_repo_table_text",
    "render_repo_unbind_text",
    "set_repo_status",
    "unbind_repo",
    "unarchive_repo",
]
