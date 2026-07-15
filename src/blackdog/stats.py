"""First-class local Blackdog stats reporting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import tomllib
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from blackdog.local_registry import registered_project_roots
from blackdog.repo_membership import attempt_cleanup_health_counts, discover_profile_dirs
from blackdog.repo_lifecycle import RepoLifecycleError
from blackdog_core.codex_sessions import (
    CodexTurn,
    build_codex_coverage,
    codex_project_roots,
    collect_codex_turns,
)
from blackdog_core.profile import ConfigError, RepoProfile, load_profile
from blackdog_core.runtime_model import AttemptView, RuntimeModel, load_runtime_model
from blackdog_core.state import now_iso, parse_iso


STATS_BUCKET_COLUMNS = (
    "bucket",
    "repos",
    "attempts_started",
    "completed_attempts",
    "success_attempts",
    "abandoned_attempts",
    "blocked_attempts",
    "failed_attempts",
    "landed_attempts",
    "not_landed_attempts",
    "codex_user_turns",
    "codex_linked_user_turns",
    "codex_unlinked_user_turns",
    "codex_implementation_like_unlinked_turns",
    "codex_tool_calls",
    "codex_input_tokens",
    "codex_cached_input_tokens",
    "codex_output_tokens",
    "codex_reasoning_output_tokens",
    "codex_total_tokens",
)

_GLOBALLY_DEDUPED_CODEX_SUMMARY_KEYS = frozenset(
    {
        "codex_sessions",
        "codex_user_turns",
        "codex_linked_user_turns",
        "codex_unlinked_user_turns",
        "codex_implementation_like_unlinked_turns",
        "codex_tool_calls",
        "codex_input_tokens",
        "codex_cached_input_tokens",
        "codex_output_tokens",
        "codex_reasoning_output_tokens",
        "codex_total_tokens",
    }
)


@dataclass(frozen=True, slots=True)
class StatsResult:
    scope_source: str
    discovery_roots: tuple[str, ...]
    project_roots: tuple[str, ...]
    since: str | None
    until: str | None
    by: str
    timezone: str
    summary: dict[str, int]
    repos: tuple[dict[str, object], ...]
    buckets: tuple[dict[str, object], ...]
    deduped_project_roots: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": now_iso(),
            "scope_source": self.scope_source,
            "discovery_roots": list(self.discovery_roots),
            "project_roots": list(self.project_roots),
            "since": self.since,
            "until": self.until,
            "by": self.by,
            "timezone": self.timezone,
            "summary": dict(self.summary),
            "repos": [dict(row) for row in self.repos],
            "buckets": [dict(row) for row in self.buckets],
            "deduped_project_roots": list(self.deduped_project_roots),
        }


def build_stats(
    *,
    project_roots: tuple[Path, ...] = (),
    discovery_roots: tuple[Path, ...] = (),
    since: str | None = None,
    until: str | None = None,
    by: str = "day",
    timezone_name: str = "UTC",
) -> StatsResult:
    if by != "day":
        raise RepoLifecycleError("--by currently supports only day")
    local_tz = _load_timezone(timezone_name)
    since_dt = _parse_local_bound(since, local_tz, until=False)
    until_dt = _parse_local_bound(until, local_tz, until=True)
    if since_dt is not None and until_dt is not None and since_dt >= until_dt:
        raise RepoLifecycleError("--since must be before --until")
    if project_roots and discovery_roots:
        raise RepoLifecycleError("stats accepts either --project-root or --root, not both")
    if project_roots:
        scope_source = "explicit_project_roots"
        profile_roots = project_roots
    elif discovery_roots:
        scope_source = "discovery_roots"
        profile_roots = tuple(
            profile_dir
            for discovery_root in discovery_roots
            for profile_dir in discover_profile_dirs(discovery_root)
        )
    else:
        scope_source = "registry"
        profile_roots = registered_project_roots()
    profiles, deduped_roots = _load_stats_profiles(profile_roots)
    if not profiles:
        if discovery_roots:
            raise RepoLifecycleError("stats found no Blackdog repos under the supplied --root values")
        raise RepoLifecycleError("stats requires --project-root, --root, or at least one registered local repo")

    codex_since = since_dt.astimezone(timezone.utc).isoformat() if since_dt else None
    codex_until = until_dt.astimezone(timezone.utc).isoformat() if until_dt else None
    all_codex_turns = collect_codex_turns(
        since=codex_since,
        until=codex_until,
        cwd_roots=_stats_codex_cwd_roots(profiles),
        include_environment_evidence=False,
    )
    repo_rows: list[dict[str, object]] = []
    bucket_rows: dict[str, dict[str, object]] = {}
    summary = _empty_summary()
    global_codex_turn_rows: dict[tuple[str, str], dict[str, Any]] = {}

    for profile in profiles:
        model = load_runtime_model(profile)
        repo_summary, repo_buckets = _repo_runtime_stats(
            profile,
            model,
            since_dt=since_dt,
            until_dt=until_dt,
            local_tz=local_tz,
        )
        coverage = build_codex_coverage(
            profile,
            since=codex_since,
            until=codex_until,
            codex_turns=all_codex_turns,
            include_environment_evidence=False,
            attempt_window_anchor="started_at",
        )
        codex_summary, _ = _repo_codex_stats(profile, coverage=coverage, local_tz=local_tz)
        repo_row = _merge_counts(repo_summary, codex_summary)
        repo_row["project_name"] = profile.project_name
        repo_row["project_root"] = str(profile.paths.project_root)
        repo_rows.append(repo_row)
        _add_summary(summary, repo_row, exclude=_GLOBALLY_DEDUPED_CODEX_SUMMARY_KEYS)
        for bucket in repo_buckets:
            key = str(bucket["bucket"])
            aggregate = bucket_rows.setdefault(key, _empty_bucket(key))
            _merge_bucket(aggregate, bucket)
        for turn in coverage.get("turns", ()):
            if not isinstance(turn, dict):
                continue
            turn_key = _turn_row_key(turn)
            existing = global_codex_turn_rows.get(turn_key)
            global_codex_turn_rows[turn_key] = (
                dict(turn) if existing is None else _merge_global_turn_rows(existing, turn)
            )
            if turn.get("user_message_hash"):
                bucket_key = _bucket_for_started_at(str(turn.get("started_at") or ""), local_tz)
                aggregate = bucket_rows.setdefault(bucket_key, _empty_bucket(bucket_key))
                _add_bucket_repo(aggregate, profile)

    summary["codex_sessions"] = len({turn_key[0] for turn_key in global_codex_turn_rows})
    for turn in global_codex_turn_rows.values():
        if not turn.get("user_message_hash"):
            continue
        _add_turn_row_to_summary(summary, turn)
        bucket_key = _bucket_for_started_at(str(turn.get("started_at") or ""), local_tz)
        aggregate = bucket_rows.setdefault(bucket_key, _empty_bucket(bucket_key))
        _add_turn_row_to_bucket(aggregate, turn)

    buckets = tuple(_clean_bucket(bucket_rows[key]) for key in sorted(bucket_rows))
    return StatsResult(
        scope_source=scope_source,
        discovery_roots=tuple(str(root.resolve()) for root in discovery_roots),
        project_roots=tuple(str(profile.paths.project_root) for profile in profiles),
        since=since_dt.isoformat() if since_dt else None,
        until=until_dt.isoformat() if until_dt else None,
        by=by,
        timezone=timezone_name,
        summary=summary,
        repos=tuple(sorted(repo_rows, key=lambda row: str(row["project_root"]))),
        buckets=buckets,
        deduped_project_roots=deduped_roots,
    )


def render_stats_text(result: StatsResult) -> str:
    summary = result.summary
    lines = [
        f"Blackdog stats ({result.timezone})",
        f"Repos: {len(result.project_roots)}",
        (
            "Tasks: "
            f"total={summary['tasks_total']} current={summary['current_tasks']} "
            f"done={summary['current_done_tasks']} canceled={summary['canceled_tasks']} "
            f"blocked={summary['current_blocked_tasks']}"
        ),
        (
            "Attempts: "
            f"total={summary['attempts_total']} current={summary['current_attempts']} "
            f"completed={summary['completed_attempts']} success={summary['success_attempts']} "
            f"abandoned={summary['abandoned_attempts']} blocked={summary['blocked_attempts']} "
            f"failed={summary['failed_attempts']}"
        ),
        f"Landing: landed={summary['landed_attempts']} not_landed={summary['not_landed_attempts']}",
        (
            "Cleanup: "
            f"terminal={summary['cleanup_terminal_attempts']} "
            f"retained_worktrees={summary['cleanup_retained_worktrees']} "
            f"landed_retained={summary['cleanup_landed_retained_worktrees']} "
            f"unlanded_terminal={summary['cleanup_unlanded_terminal_attempts']}"
        ),
        (
            "Codex: "
            f"sessions={summary['codex_sessions']} turns={summary['codex_user_turns']} "
            f"linked_turns={summary['codex_linked_user_turns']} "
            f"unlinked_turns={summary['codex_unlinked_user_turns']} "
            f"implementation_like_unlinked={summary['codex_implementation_like_unlinked_turns']} "
            f"linked_attempts={summary['codex_linked_attempts']} "
            f"unlinked_attempts={summary['codex_unlinked_attempts']} "
            f"tools={summary['codex_tool_calls']} "
            f"tokens={summary['codex_total_tokens']}"
        ),
    ]
    if result.deduped_project_roots:
        lines.append(f"Deduped project roots: {len(result.deduped_project_roots)}")
    if result.buckets:
        lines.append("")
        lines.append(render_stats_tsv(result).rstrip())
    return "\n".join(lines) + "\n"


def render_stats_tsv(result: StatsResult) -> str:
    lines = ["\t".join(STATS_BUCKET_COLUMNS)]
    for row in result.buckets:
        lines.append("\t".join(_tsv_value(row.get(column)) for column in STATS_BUCKET_COLUMNS))
    return "\n".join(lines) + "\n"


def _load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise RepoLifecycleError(f"unknown timezone: {name}") from exc


def _parse_local_bound(value: str | None, local_tz: ZoneInfo, *, until: bool) -> datetime | None:
    if not value:
        return None
    if _looks_like_date(value):
        parsed_date = date.fromisoformat(value)
        if until:
            parsed_date += timedelta(days=1)
        return datetime.combine(parsed_date, time.min, tzinfo=local_tz)
    parsed = parse_iso(value)
    if parsed is None:
        raise RepoLifecycleError(f"--{'until' if until else 'since'} must be an ISO timestamp or date: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


def _looks_like_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return "T" not in value and " " not in value


def _load_stats_profiles(roots: Iterable[Path]) -> tuple[tuple[RepoProfile, ...], tuple[str, ...]]:
    profiles_by_root: dict[Path, RepoProfile] = {}
    deduped: list[str] = []
    for root in roots:
        try:
            profile = load_profile(root.resolve())
        except (ConfigError, OSError, tomllib.TOMLDecodeError) as exc:
            raise RepoLifecycleError(f"{root.resolve()} is not a Blackdog repo: {exc}") from exc
        key = profile.paths.project_root.resolve()
        if key in profiles_by_root:
            deduped.append(str(root.resolve()))
            continue
        profiles_by_root[key] = profile
    return tuple(profiles_by_root[path] for path in sorted(profiles_by_root)), tuple(deduped)


def _stats_codex_cwd_roots(profiles: Iterable[RepoProfile]) -> tuple[Path, ...]:
    roots: set[Path] = set()
    for profile in profiles:
        roots.update(codex_project_roots(profile))
    return tuple(sorted(roots))


def _repo_runtime_stats(
    profile: RepoProfile,
    model: RuntimeModel,
    *,
    since_dt: datetime | None,
    until_dt: datetime | None,
    local_tz: ZoneInfo,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    summary = _empty_summary()
    tasks = tuple(task for workset in model.worksets for task in workset.tasks)
    attempts = tuple(attempt for workset in model.worksets for attempt in workset.attempts)
    summary["tasks_total"] = len(tasks)
    summary["current_tasks"] = sum(1 for task in tasks if task.runtime_status not in {"done", "canceled"})
    summary["current_done_tasks"] = sum(1 for task in tasks if task.runtime_status == "done")
    summary["current_blocked_tasks"] = sum(1 for task in tasks if task.runtime_status == "blocked")
    summary["canceled_tasks"] = sum(1 for task in tasks if task.runtime_status == "canceled")
    summary["current_attempts"] = sum(1 for attempt in attempts if attempt.is_active)
    summary["attempts_total"] = len(attempts)
    summary.update(attempt_cleanup_health_counts(attempts))

    buckets: dict[str, dict[str, object]] = {}
    for attempt in attempts:
        if not _attempt_started_in_window(attempt, since_dt=since_dt, until_dt=until_dt):
            continue
        _add_attempt_to_summary(summary, attempt)
        bucket = buckets.setdefault(_bucket_for_started_at(attempt.started_at, local_tz), _empty_bucket(_bucket_for_started_at(attempt.started_at, local_tz)))
        _add_bucket_repo(bucket, profile)
        _add_attempt_to_bucket(bucket, attempt)
    return summary, tuple(row for _, row in sorted(buckets.items()))


def _repo_codex_stats(
    profile: RepoProfile,
    *,
    coverage: dict[str, Any],
    local_tz: ZoneInfo,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    summary = _empty_summary()
    coverage_counts = coverage.get("counts", {})
    summary["codex_sessions"] = int(coverage_counts.get("codex_sessions") or 0)
    summary["codex_linked_attempts"] = int(coverage_counts.get("linked_attempts") or 0)
    summary["codex_unlinked_attempts"] = int(coverage_counts.get("unlinked_attempts") or 0)
    buckets: dict[str, dict[str, object]] = {}
    for turn in coverage.get("turns", ()):
        if not isinstance(turn, dict) or not turn.get("user_message_hash"):
            continue
        _add_turn_row_to_summary(summary, turn)
        bucket_key = _bucket_for_started_at(str(turn.get("started_at") or ""), local_tz)
        bucket = buckets.setdefault(bucket_key, _empty_bucket(bucket_key))
        _add_bucket_repo(bucket, profile)
        _add_turn_row_to_bucket(bucket, turn)
    return summary, tuple(row for _, row in sorted(buckets.items()))


def _attempt_started_in_window(
    attempt: AttemptView,
    *,
    since_dt: datetime | None,
    until_dt: datetime | None,
) -> bool:
    started = parse_iso(attempt.started_at)
    if started is None:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if since_dt is not None and started < since_dt.astimezone(started.tzinfo or timezone.utc):
        return False
    if until_dt is not None and started >= until_dt.astimezone(started.tzinfo or timezone.utc):
        return False
    return True


def _bucket_for_started_at(started_at: str | None, local_tz: ZoneInfo) -> str:
    parsed = parse_iso(started_at)
    if parsed is None:
        return "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(local_tz).date().isoformat()


def _empty_summary() -> dict[str, int]:
    return {
        "tasks_total": 0,
        "current_tasks": 0,
        "current_done_tasks": 0,
        "current_blocked_tasks": 0,
        "canceled_tasks": 0,
        "attempts_total": 0,
        "current_attempts": 0,
        "completed_attempts": 0,
        "success_attempts": 0,
        "abandoned_attempts": 0,
        "blocked_attempts": 0,
        "failed_attempts": 0,
        "landed_attempts": 0,
        "not_landed_attempts": 0,
        "cleanup_terminal_attempts": 0,
        "cleanup_retained_worktrees": 0,
        "cleanup_landed_retained_worktrees": 0,
        "cleanup_unlanded_terminal_attempts": 0,
        "codex_sessions": 0,
        "codex_user_turns": 0,
        "codex_linked_user_turns": 0,
        "codex_unlinked_user_turns": 0,
        "codex_implementation_like_unlinked_turns": 0,
        "codex_linked_attempts": 0,
        "codex_unlinked_attempts": 0,
        "codex_tool_calls": 0,
        "codex_input_tokens": 0,
        "codex_cached_input_tokens": 0,
        "codex_output_tokens": 0,
        "codex_reasoning_output_tokens": 0,
        "codex_total_tokens": 0,
    }


def _empty_bucket(bucket: str) -> dict[str, object]:
    row = {column: 0 for column in STATS_BUCKET_COLUMNS}
    row["bucket"] = bucket
    return row


def _add_attempt_to_summary(summary: dict[str, int], attempt: AttemptView) -> None:
    if not attempt.is_active:
        summary["completed_attempts"] += 1
    if attempt.status == "success":
        summary["success_attempts"] += 1
    elif attempt.status == "abandoned":
        summary["abandoned_attempts"] += 1
    elif attempt.status == "blocked":
        summary["blocked_attempts"] += 1
    elif attempt.status == "failed":
        summary["failed_attempts"] += 1
    if not attempt.is_active:
        if attempt.landed_commit:
            summary["landed_attempts"] += 1
        else:
            summary["not_landed_attempts"] += 1


def _add_attempt_to_bucket(bucket: dict[str, object], attempt: AttemptView) -> None:
    bucket["attempts_started"] = int(bucket["attempts_started"]) + 1
    if not attempt.is_active:
        bucket["completed_attempts"] = int(bucket["completed_attempts"]) + 1
    if attempt.status == "success":
        bucket["success_attempts"] = int(bucket["success_attempts"]) + 1
    elif attempt.status == "abandoned":
        bucket["abandoned_attempts"] = int(bucket["abandoned_attempts"]) + 1
    elif attempt.status == "blocked":
        bucket["blocked_attempts"] = int(bucket["blocked_attempts"]) + 1
    elif attempt.status == "failed":
        bucket["failed_attempts"] = int(bucket["failed_attempts"]) + 1
    if not attempt.is_active:
        key = "landed_attempts" if attempt.landed_commit else "not_landed_attempts"
        bucket[key] = int(bucket[key]) + 1


def _add_turn_to_summary(summary: dict[str, int], turn: CodexTurn) -> None:
    summary["codex_user_turns"] += 1
    summary["codex_tool_calls"] += turn.tool_call_count
    summary["codex_input_tokens"] += turn.input_tokens
    summary["codex_cached_input_tokens"] += turn.cached_input_tokens
    summary["codex_output_tokens"] += turn.output_tokens
    summary["codex_reasoning_output_tokens"] += turn.reasoning_output_tokens
    summary["codex_total_tokens"] += turn.total_tokens


def _add_turn_to_bucket(bucket: dict[str, object], turn: CodexTurn) -> None:
    bucket["codex_user_turns"] = int(bucket["codex_user_turns"]) + 1
    bucket["codex_tool_calls"] = int(bucket["codex_tool_calls"]) + turn.tool_call_count
    bucket["codex_input_tokens"] = int(bucket["codex_input_tokens"]) + turn.input_tokens
    bucket["codex_cached_input_tokens"] = int(bucket["codex_cached_input_tokens"]) + turn.cached_input_tokens
    bucket["codex_output_tokens"] = int(bucket["codex_output_tokens"]) + turn.output_tokens
    bucket["codex_reasoning_output_tokens"] = int(bucket["codex_reasoning_output_tokens"]) + turn.reasoning_output_tokens
    bucket["codex_total_tokens"] = int(bucket["codex_total_tokens"]) + turn.total_tokens


def _add_turn_row_to_summary(summary: dict[str, int], turn: dict[str, Any]) -> None:
    linked = bool(turn.get("linked_attempt_ids"))
    summary["codex_user_turns"] += 1
    if linked:
        summary["codex_linked_user_turns"] += 1
    else:
        summary["codex_unlinked_user_turns"] += 1
    if turn.get("classification") == "implementation_likely" and not linked:
        summary["codex_implementation_like_unlinked_turns"] += 1
    summary["codex_tool_calls"] += _int_value(turn.get("tool_call_count"))
    summary["codex_input_tokens"] += _int_value(turn.get("input_tokens"))
    summary["codex_cached_input_tokens"] += _int_value(turn.get("cached_input_tokens"))
    summary["codex_output_tokens"] += _int_value(turn.get("output_tokens"))
    summary["codex_reasoning_output_tokens"] += _int_value(turn.get("reasoning_output_tokens"))
    summary["codex_total_tokens"] += _int_value(turn.get("total_tokens"))


def _add_turn_row_to_bucket(bucket: dict[str, object], turn: dict[str, Any]) -> None:
    linked = bool(turn.get("linked_attempt_ids"))
    bucket["codex_user_turns"] = int(bucket["codex_user_turns"]) + 1
    if linked:
        bucket["codex_linked_user_turns"] = int(bucket["codex_linked_user_turns"]) + 1
    else:
        bucket["codex_unlinked_user_turns"] = int(bucket["codex_unlinked_user_turns"]) + 1
    if turn.get("classification") == "implementation_likely" and not linked:
        bucket["codex_implementation_like_unlinked_turns"] = (
            int(bucket["codex_implementation_like_unlinked_turns"]) + 1
        )
    bucket["codex_tool_calls"] = int(bucket["codex_tool_calls"]) + _int_value(turn.get("tool_call_count"))
    bucket["codex_input_tokens"] = int(bucket["codex_input_tokens"]) + _int_value(turn.get("input_tokens"))
    bucket["codex_cached_input_tokens"] = int(bucket["codex_cached_input_tokens"]) + _int_value(
        turn.get("cached_input_tokens")
    )
    bucket["codex_output_tokens"] = int(bucket["codex_output_tokens"]) + _int_value(turn.get("output_tokens"))
    bucket["codex_reasoning_output_tokens"] = int(bucket["codex_reasoning_output_tokens"]) + _int_value(
        turn.get("reasoning_output_tokens")
    )
    bucket["codex_total_tokens"] = int(bucket["codex_total_tokens"]) + _int_value(turn.get("total_tokens"))


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _merge_counts(left: dict[str, object], right: dict[str, object]) -> dict[str, object]:
    merged = dict(left)
    for key, value in right.items():
        if isinstance(value, int):
            merged[key] = int(merged.get(key) or 0) + value
    return merged


def _turn_row_key(turn: dict[str, Any]) -> tuple[str, str]:
    thread_id = str(turn.get("thread_id") or "")
    turn_id = str(turn.get("turn_id") or "")
    if not thread_id:
        thread_id = f"session:{turn.get('session_path') or ''}"
    if not turn_id:
        turn_id = f"index:{turn.get('turn_index') or 0}"
    return thread_id, turn_id


def _merge_global_turn_rows(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    existing_attempt_ids = {str(item) for item in existing.get("linked_attempt_ids") or ()}
    incoming_attempt_ids = {str(item) for item in incoming.get("linked_attempt_ids") or ()}
    linked_attempt_ids = existing_attempt_ids | incoming_attempt_ids
    merged["linked_attempt_ids"] = sorted(linked_attempt_ids)
    if linked_attempt_ids:
        merged["classification"] = "blackdog_attempt"
    elif incoming.get("classification") == "implementation_likely":
        merged["classification"] = "implementation_likely"
    for key in (
        "tool_call_count",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        merged[key] = max(_int_value(existing.get(key)), _int_value(incoming.get(key)))
    for key in ("user_message_hash", "started_at", "session_path"):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    return merged


def _add_summary(
    summary: dict[str, int],
    row: dict[str, object],
    *,
    exclude: frozenset[str] = frozenset(),
) -> None:
    for key in summary:
        if key in exclude:
            continue
        summary[key] += int(row.get(key) or 0)


def _merge_bucket(target: dict[str, object], source: dict[str, object]) -> None:
    if source.get("_repo_names"):
        existing = {item for item in str(target.get("_repo_names") or "").split("|") if item}
        incoming = {item for item in str(source.get("_repo_names") or "").split("|") if item}
        combined = existing | incoming
        target["_repo_names"] = "|".join(sorted(item for item in combined if item))
        target["repos"] = len(combined)
    for key, value in source.items():
        if key in {"bucket", "repos", "_repo_names"}:
            continue
        if isinstance(value, int):
            target[key] = int(target.get(key) or 0) + value


def _clean_bucket(row: dict[str, object]) -> dict[str, object]:
    return {column: row.get(column, 0) for column in STATS_BUCKET_COLUMNS}


def _add_bucket_repo(bucket: dict[str, object], profile: RepoProfile) -> None:
    repo_key = str(profile.paths.project_root)
    existing = {item for item in str(bucket.get("_repo_names") or "").split("|") if item}
    if repo_key not in existing:
        existing.add(repo_key)
        bucket["_repo_names"] = "|".join(sorted(existing))
        bucket["repos"] = len(existing)


def _tsv_value(value: object) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


__all__ = [
    "STATS_BUCKET_COLUMNS",
    "StatsResult",
    "build_stats",
    "render_stats_text",
    "render_stats_tsv",
]
