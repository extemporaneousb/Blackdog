"""Codex session indexing and history export read models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
import hashlib
import json
import os
import re
import subprocess
import tomllib

from .profile import RepoProfile
from .runtime_model import AttemptView, load_runtime_model
from .state import CodexSessionRefRecord, atomic_write_text, now_iso, parse_iso


CODEX_SESSION_HISTORY_SCHEMA_VERSION = 1
HISTORY_DIR_NAME = ".blackdog"
HISTORY_FILE_NAME = "history.jsonl"

_IMPLEMENTATION_KEYWORDS = frozenset(
    {
        "add",
        "build",
        "change",
        "clean up",
        "commit",
        "create",
        "delete",
        "deploy",
        "edit",
        "fix",
        "implement",
        "land",
        "migrate",
        "patch",
        "port",
        "refactor",
        "remove",
        "run",
        "test",
        "update",
        "validate",
    }
)
_ANALYSIS_KEYWORDS = frozenset(
    {
        "analyze",
        "assess",
        "audit",
        "compare",
        "diagnose",
        "explain",
        "inspect",
        "plan",
        "review",
        "understand",
    }
)
_WTAM_KEYWORDS = frozenset({"blackdog", "$blackdog", "wtam", "task begin", "worktree start"})


@dataclass(frozen=True, slots=True)
class CodexRuntimeContext:
    thread_id: str | None
    session_path: str | None
    model: str | None
    reasoning_effort: str | None


@dataclass(frozen=True, slots=True)
class CodexTurn:
    thread_id: str
    session_path: str
    turn_id: str
    turn_index: int
    started_at: str | None
    cwd: str | None
    originator: str | None
    model: str | None
    reasoning_effort: str | None
    user_message_hash: str | None
    message_excerpt: str | None
    classification: str
    has_assistant_response: bool
    tool_call_count: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class CodexSession:
    thread_id: str
    session_path: str
    started_at: str | None
    cwd: str | None
    originator: str | None
    model_provider: str | None
    model: str | None
    turns: tuple[CodexTurn, ...]


class CodexSessionError(RuntimeError):
    pass


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def read_codex_config(home: Path | None = None) -> dict[str, str | None]:
    config_path = (home or codex_home()) / "config.toml"
    try:
        payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"model": None, "reasoning_effort": None}
    if not isinstance(payload, dict):
        return {"model": None, "reasoning_effort": None}
    return {
        "model": _optional_text(payload.get("model")),
        "reasoning_effort": _optional_text(payload.get("model_reasoning_effort")),
    }


def current_codex_runtime_context(home: Path | None = None) -> CodexRuntimeContext:
    resolved_home = home or codex_home()
    config = read_codex_config(resolved_home)
    thread_id = _optional_text(os.environ.get("CODEX_THREAD_ID"))
    session_path = _find_session_path_for_thread(resolved_home, thread_id) if thread_id else None
    return CodexRuntimeContext(
        thread_id=thread_id,
        session_path=session_path,
        model=config["model"],
        reasoning_effort=config["reasoning_effort"],
    )


def current_codex_session_ref(
    *,
    user_prompt_hash: str | None = None,
    execution_prompt_hash: str | None = None,
    home: Path | None = None,
) -> CodexSessionRefRecord | None:
    context = current_codex_runtime_context(home)
    if context.thread_id is None:
        return None
    return CodexSessionRefRecord(
        thread_id=context.thread_id,
        session_path=context.session_path,
        user_prompt_hash=user_prompt_hash,
        execution_prompt_hash=execution_prompt_hash,
    )


def read_codex_session(path: Path, *, home: Path | None = None) -> CodexSession | None:
    resolved_home = home or codex_home()
    session_path = _relative_session_path(path, resolved_home)
    thread_id = ""
    started_at: str | None = None
    session_cwd: str | None = None
    originator: str | None = None
    model_provider: str | None = None
    session_model: str | None = None
    current_turn_id: str | None = None
    turns: dict[str, dict[str, Any]] = {}
    turn_order: list[str] = []

    def ensure_turn(turn_id: str) -> dict[str, Any]:
        if turn_id not in turns:
            turns[turn_id] = {
                "turn_id": turn_id,
                "turn_index": len(turn_order),
                "started_at": None,
                "cwd": None,
                "model": None,
                "reasoning_effort": None,
                "user_message": None,
                "fallback_user_messages": [],
                "assistant_response_count": 0,
                "tool_call_count": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 0,
            }
            turn_order.append(turn_id)
        return turns[turn_id]

    try:
        line_iter = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CodexSessionError(f"could not read Codex session {path}: {exc}") from exc
    for lineno, line in enumerate(line_iter, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping):
            continue
        row_type = row.get("type")
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            continue
        timestamp = _optional_text(row.get("timestamp"))
        if row_type == "session_meta":
            thread_id = _optional_text(payload.get("id")) or thread_id
            started_at = _optional_text(payload.get("timestamp")) or timestamp or started_at
            session_cwd = _optional_text(payload.get("cwd")) or session_cwd
            originator = _optional_text(payload.get("originator")) or originator
            model_provider = _optional_text(payload.get("model_provider")) or model_provider
            session_model = _optional_text(payload.get("model")) or session_model
            continue
        if row_type == "event_msg" and payload.get("type") == "task_started":
            current_turn_id = _optional_text(payload.get("turn_id")) or f"line-{lineno}"
            turn = ensure_turn(current_turn_id)
            turn["started_at"] = _timestamp_from_payload(payload.get("started_at")) or timestamp or turn["started_at"]
            continue
        if row_type == "turn_context":
            current_turn_id = _optional_text(payload.get("turn_id")) or current_turn_id or f"line-{lineno}"
            turn = ensure_turn(current_turn_id)
            turn["started_at"] = _optional_text(payload.get("started_at")) or timestamp or turn["started_at"]
            turn["cwd"] = _optional_text(payload.get("cwd")) or turn["cwd"]
            turn["model"] = _optional_text(payload.get("model")) or _nested_setting(payload, "model") or turn["model"]
            turn["reasoning_effort"] = (
                _optional_text(payload.get("effort"))
                or _nested_setting(payload, "reasoning_effort")
                or turn["reasoning_effort"]
            )
            continue
        if row_type == "event_msg" and payload.get("type") == "user_message":
            turn = ensure_turn(current_turn_id or f"line-{lineno}")
            turn["user_message"] = _optional_text(payload.get("message")) or turn["user_message"]
            continue
        if row_type == "event_msg" and payload.get("type") == "token_count":
            usage = _token_usage_from_payload(payload)
            if usage is not None and current_turn_id is not None:
                turn = ensure_turn(current_turn_id)
                _add_token_usage(turn, usage)
            continue
        if row_type == "response_item":
            item_type = _optional_text(payload.get("type"))
            role = _optional_text(payload.get("role"))
            turn = ensure_turn(current_turn_id or f"line-{lineno}")
            if item_type == "message" and role == "user":
                text = _message_text(payload)
                if text:
                    turn["fallback_user_messages"].append(text)
            elif item_type == "message" and role == "assistant":
                turn["assistant_response_count"] += 1
            elif item_type and ("tool_call" in item_type or item_type in {"function_call", "local_shell_call"}):
                turn["tool_call_count"] += 1

    if not thread_id:
        thread_id = _thread_id_from_filename(path) or path.stem
    codex_turns: list[CodexTurn] = []
    for turn_id in turn_order:
        payload = turns[turn_id]
        user_message = payload["user_message"]
        if user_message is None and payload["fallback_user_messages"]:
            user_message = payload["fallback_user_messages"][-1]
        user_hash = _hash_text(user_message) if user_message else None
        codex_turns.append(
            CodexTurn(
                thread_id=thread_id,
                session_path=session_path,
                turn_id=turn_id,
                turn_index=int(payload["turn_index"]),
                started_at=payload["started_at"] or started_at,
                cwd=payload["cwd"] or session_cwd,
                originator=originator,
                model=payload["model"] or session_model,
                reasoning_effort=payload["reasoning_effort"],
                user_message_hash=user_hash,
                message_excerpt=_excerpt(user_message),
                classification=classify_user_message(user_message),
                has_assistant_response=bool(payload["assistant_response_count"]),
                tool_call_count=int(payload["tool_call_count"]),
                input_tokens=int(payload["input_tokens"]),
                cached_input_tokens=int(payload["cached_input_tokens"]),
                output_tokens=int(payload["output_tokens"]),
                reasoning_output_tokens=int(payload["reasoning_output_tokens"]),
                total_tokens=int(payload["total_tokens"]),
            )
        )
    return CodexSession(
        thread_id=thread_id,
        session_path=session_path,
        started_at=started_at,
        cwd=session_cwd,
        originator=originator,
        model_provider=model_provider,
        model=session_model,
        turns=tuple(codex_turns),
    )


def build_codex_coverage(
    profile: RepoProfile,
    *,
    since: str | None = None,
) -> dict[str, Any]:
    turns = _project_turns(profile, since=since)
    model = load_runtime_model(profile)
    cutoff = _parse_since(since)
    attempts = tuple(
        attempt
        for workset in model.worksets
        for attempt in workset.attempts
        if _attempt_in_window(attempt, cutoff)
    )
    linked = _link_turns_to_attempts(turns, attempts)
    linked_attempt_ids = {attempt_id for attempt_ids in linked.values() for attempt_id in attempt_ids}
    rows = []
    for turn in turns:
        attempt_ids = linked.get(_turn_key(turn), ())
        classification = "blackdog_attempt" if attempt_ids else turn.classification
        rows.append(
            {
                "thread_id": turn.thread_id,
                "session_path": turn.session_path,
                "turn_id": turn.turn_id,
                "turn_index": turn.turn_index,
                "started_at": turn.started_at,
                "cwd": turn.cwd,
                "originator": turn.originator,
                "model": turn.model,
                "reasoning_effort": turn.reasoning_effort,
                "user_message_hash": turn.user_message_hash,
                "message_excerpt": turn.message_excerpt,
                "classification": classification,
                "linked_attempt_ids": list(attempt_ids),
                "has_assistant_response": turn.has_assistant_response,
                "tool_call_count": turn.tool_call_count,
                "input_tokens": turn.input_tokens,
                "cached_input_tokens": turn.cached_input_tokens,
                "output_tokens": turn.output_tokens,
                "reasoning_output_tokens": turn.reasoning_output_tokens,
                "total_tokens": turn.total_tokens,
            }
        )
    by_classification: dict[str, int] = {}
    for row in rows:
        key = str(row["classification"])
        by_classification[key] = by_classification.get(key, 0) + 1
    attempt_status_counts: dict[str, int] = {}
    for attempt in attempts:
        attempt_status_counts[attempt.status] = attempt_status_counts.get(attempt.status, 0) + 1
    return {
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "generated_at": now_iso(),
        "since": since,
        "counts": {
            "codex_sessions": len({turn.session_path for turn in turns}),
            "codex_user_turns": len([turn for turn in turns if turn.user_message_hash is not None]),
            "blackdog_attempts": len(attempts),
            "active_attempts": len([attempt for attempt in attempts if attempt.is_active]),
            "linked_attempts": len(linked_attempt_ids),
            "unlinked_attempts": len(attempts) - len(linked_attempt_ids),
            "linked_user_turns": len([row for row in rows if row["linked_attempt_ids"]]),
            "unlinked_user_turns": len([row for row in rows if row["user_message_hash"] and not row["linked_attempt_ids"]]),
            "implementation_like_unlinked_turns": len(
                [
                    row
                    for row in rows
                    if row["classification"] == "implementation_likely" and not row["linked_attempt_ids"]
                ]
            ),
            "analysis_only_turns": by_classification.get("analysis_only", 0),
            "model_known_turns": len([turn for turn in turns if turn.model]),
            "model_missing_turns": len([turn for turn in turns if not turn.model]),
            "reasoning_known_turns": len([turn for turn in turns if turn.reasoning_effort]),
            "reasoning_missing_turns": len([turn for turn in turns if not turn.reasoning_effort]),
            "tool_calls": sum(turn.tool_call_count for turn in turns),
            "input_tokens": sum(turn.input_tokens for turn in turns),
            "cached_input_tokens": sum(turn.cached_input_tokens for turn in turns),
            "output_tokens": sum(turn.output_tokens for turn in turns),
            "reasoning_output_tokens": sum(turn.reasoning_output_tokens for turn in turns),
            "total_tokens": sum(turn.total_tokens for turn in turns),
        },
        "by_classification": by_classification,
        "attempt_status_counts": attempt_status_counts,
        "turns": rows,
        "attempts": [_attempt_coverage_row(attempt, linked_attempt_ids) for attempt in attempts],
    }


def build_codex_history(
    profile: RepoProfile,
    *,
    since: str | None = None,
    write: bool = False,
) -> dict[str, Any]:
    turns = _project_turns(profile, since=since)
    model = load_runtime_model(profile)
    cutoff = _parse_since(since)
    attempts = tuple(
        attempt
        for workset in model.worksets
        for attempt in workset.attempts
        if _attempt_in_window(attempt, cutoff)
    )
    linked = _link_turns_to_attempts(turns, attempts)
    linked_turn_keys = {key for key, attempt_ids in linked.items() if attempt_ids}
    rows = [
        _attempt_history_row(profile, workset.workset_id, attempt)
        for workset in model.worksets
        for attempt in workset.attempts
        if _attempt_in_window(attempt, cutoff)
    ]
    rows.extend(
        _turn_history_row(profile, turn)
        for turn in turns
        if turn.user_message_hash is not None and _turn_key(turn) not in linked_turn_keys
    )
    rows = sorted(rows, key=lambda row: (str(row.get("started_at") or ""), str(row.get("row_id") or "")))
    history_path = history_export_path(profile)
    if write:
        atomic_write_text(history_path, "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    return {
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "generated_at": now_iso(),
        "since": since,
        "history_path": str(history_path),
        "written": write,
        "rows": rows,
        "counts": {
            "rows": len(rows),
            "attempt_rows": len([row for row in rows if row.get("kind") == "attempt"]),
            "codex_turn_rows": len([row for row in rows if row.get("kind") == "codex_turn"]),
        },
    }


def history_export_path(profile: RepoProfile) -> Path:
    return profile.paths.project_root / HISTORY_DIR_NAME / HISTORY_FILE_NAME


def render_codex_coverage_text(payload: Mapping[str, Any]) -> str:
    counts = payload["counts"]
    lines = [
        f"Project: {payload['project_name']}",
        (
            "Codex sessions: "
            f"{counts['codex_sessions']} | User turns: {counts['codex_user_turns']} | "
            f"Blackdog attempts: {counts['blackdog_attempts']}"
        ),
        (
            "Linked: "
            f"turns={counts['linked_user_turns']} attempts={counts['linked_attempts']} | "
            f"Unlinked turns={counts['unlinked_user_turns']} attempts={counts['unlinked_attempts']}"
        ),
        (
            "Unlinked implementation-like turns: "
            f"{counts['implementation_like_unlinked_turns']} | Analysis-only turns: {counts['analysis_only_turns']}"
        ),
        (
            "Model signal: "
            f"known={counts['model_known_turns']} missing={counts['model_missing_turns']} | "
            f"Reasoning known={counts['reasoning_known_turns']} missing={counts['reasoning_missing_turns']}"
        ),
        (
            "Token signal: "
            f"input={counts['input_tokens']} cached_input={counts['cached_input_tokens']} "
            f"output={counts['output_tokens']} reasoning_output={counts['reasoning_output_tokens']} "
            f"total={counts['total_tokens']}"
        ),
    ]
    gaps = [
        row
        for row in payload.get("turns", [])
        if row.get("classification") == "implementation_likely" and not row.get("linked_attempt_ids")
    ][:10]
    if gaps:
        lines.append("")
        lines.append("Recent unlinked implementation-like turns:")
        for row in gaps:
            lines.append(
                (
                    f"  - {row.get('started_at') or 'unknown'} {row.get('thread_id')} "
                    f"hash={str(row.get('user_message_hash') or '')[:12]} {row.get('message_excerpt') or ''}"
                ).rstrip()
            )
    return "\n".join(lines) + "\n"


def render_codex_history_text(payload: Mapping[str, Any]) -> str:
    counts = payload["counts"]
    written = f" | Written: {payload['history_path']}" if payload.get("written") else ""
    return (
        f"Project: {payload['project_name']}\n"
        f"History rows: {counts['rows']} | Attempts: {counts['attempt_rows']} | "
        f"Codex turns: {counts['codex_turn_rows']}{written}\n"
    )


def classify_user_message(message: str | None) -> str:
    text = (message or "").lower()
    if not text:
        return "no_user_message"
    if any(_contains_keyword(text, keyword) for keyword in _WTAM_KEYWORDS):
        return "explicit_wtam"
    implementation_like = any(_contains_keyword(text, keyword) for keyword in _IMPLEMENTATION_KEYWORDS)
    analysis_like = any(_contains_keyword(text, keyword) for keyword in _ANALYSIS_KEYWORDS)
    if implementation_like:
        return "implementation_likely"
    if analysis_like:
        return "analysis_only"
    return "unclassified"


def _contains_keyword(text: str, keyword: str) -> bool:
    if " " in keyword or keyword.startswith("$"):
        return keyword in text
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def _project_turns(profile: RepoProfile, *, since: str | None) -> tuple[CodexTurn, ...]:
    roots = _project_roots(profile)
    cutoff = _parse_since(since)
    turns: list[CodexTurn] = []
    home = codex_home()
    for path in _session_files(home):
        session = read_codex_session(path, home=home)
        if session is None:
            continue
        for turn in session.turns:
            if not _cwd_matches(roots, turn.cwd):
                continue
            if cutoff is not None:
                started = parse_iso(turn.started_at)
                if started is None or started < cutoff:
                    continue
            turns.append(turn)
    return tuple(sorted(turns, key=lambda turn: (parse_iso(turn.started_at) or datetime.min.replace(tzinfo=timezone.utc), turn.session_path, turn.turn_index)))


def _session_files(home: Path) -> Iterable[Path]:
    sessions_dir = home / "sessions"
    if not sessions_dir.exists():
        return ()
    return sorted(sessions_dir.rglob("rollout-*.jsonl"))


def _project_roots(profile: RepoProfile) -> tuple[Path, ...]:
    roots = {profile.paths.project_root.resolve()}
    completed = subprocess.run(
        ["git", "-C", str(profile.paths.project_root), "worktree", "list", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        for line in completed.stdout.splitlines():
            if line.startswith("worktree "):
                roots.add(Path(line.partition(" ")[2]).resolve())
    return tuple(sorted(roots))


def _cwd_matches(roots: tuple[Path, ...], cwd: str | None) -> bool:
    if cwd is None:
        return False
    candidate = Path(cwd).expanduser().resolve()
    for root in roots:
        try:
            candidate.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _link_turns_to_attempts(turns: tuple[CodexTurn, ...], attempts: tuple[AttemptView, ...]) -> dict[tuple[str, str], tuple[str, ...]]:
    attempts_by_hash: dict[str, list[str]] = {}
    attempts_by_codex_hash: dict[str, list[str]] = {}
    for attempt in attempts:
        for prompt_receipt in (attempt.prompt_receipt, attempt.user_prompt_receipt):
            if prompt_receipt is not None:
                attempts_by_hash.setdefault(prompt_receipt.prompt_hash, []).append(attempt.attempt_id)
        if attempt.codex_session is not None:
            for prompt_hash in (attempt.codex_session.user_prompt_hash, attempt.codex_session.execution_prompt_hash):
                if prompt_hash:
                    attempts_by_codex_hash.setdefault(prompt_hash, []).append(attempt.attempt_id)
    linked: dict[tuple[str, str], tuple[str, ...]] = {}
    for turn in turns:
        if turn.user_message_hash is None:
            continue
        attempt_ids = tuple(
            sorted(
                set(
                    [
                        *attempts_by_hash.get(turn.user_message_hash, []),
                        *attempts_by_codex_hash.get(turn.user_message_hash, []),
                    ]
                )
            )
        )
        if attempt_ids:
            linked[_turn_key(turn)] = attempt_ids
    return linked


def _attempt_coverage_row(attempt: AttemptView, linked_attempt_ids: set[str]) -> dict[str, Any]:
    return {
        "attempt_id": attempt.attempt_id,
        "task_id": attempt.task_id,
        "status": attempt.status,
        "actor": attempt.actor,
        "started_at": attempt.started_at,
        "ended_at": attempt.ended_at,
        "model": attempt.model,
        "reasoning_effort": attempt.reasoning_effort,
        "codex_thread_id": attempt.codex_session.thread_id if attempt.codex_session else None,
        "codex_session_path": attempt.codex_session.session_path if attempt.codex_session else None,
        "linked_codex_turn": attempt.attempt_id in linked_attempt_ids,
    }


def _attempt_history_row(profile: RepoProfile, workset_id: str, attempt: AttemptView) -> dict[str, Any]:
    prompt_hash = attempt.prompt_receipt.prompt_hash if attempt.prompt_receipt else None
    user_prompt_hash = attempt.user_prompt_receipt.prompt_hash if attempt.user_prompt_receipt else None
    return {
        "schema_version": CODEX_SESSION_HISTORY_SCHEMA_VERSION,
        "kind": "attempt",
        "row_id": _row_id("attempt", profile.project_name, workset_id, attempt.task_id, attempt.attempt_id),
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "workset_id": workset_id,
        "task_id": attempt.task_id,
        "attempt_id": attempt.attempt_id,
        "status": attempt.status,
        "actor": attempt.actor,
        "started_at": attempt.started_at,
        "ended_at": attempt.ended_at,
        "model": attempt.model,
        "reasoning_effort": attempt.reasoning_effort,
        "execution_model": attempt.execution_model,
        "codex_thread_id": attempt.codex_session.thread_id if attempt.codex_session else None,
        "codex_session_path": attempt.codex_session.session_path if attempt.codex_session else None,
        "codex_turn_id": attempt.codex_session.turn_id if attempt.codex_session else None,
        "execution_prompt_hash": prompt_hash,
        "user_prompt_hash": user_prompt_hash,
        "changed_paths": list(attempt.changed_paths),
        "changed_paths_count": len(attempt.changed_paths),
        "validations": [{"name": item.name, "status": item.status} for item in attempt.validations],
        "residuals": list(attempt.residuals),
        "followup_candidates": list(attempt.followup_candidates),
        "commit": attempt.commit,
        "landed_commit": attempt.landed_commit,
        "elapsed_seconds": attempt.elapsed_seconds,
    }


def _turn_history_row(profile: RepoProfile, turn: CodexTurn) -> dict[str, Any]:
    return {
        "schema_version": CODEX_SESSION_HISTORY_SCHEMA_VERSION,
        "kind": "codex_turn",
        "row_id": _row_id("codex_turn", profile.project_name, turn.thread_id, turn.turn_id, turn.user_message_hash or ""),
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "started_at": turn.started_at,
        "codex_thread_id": turn.thread_id,
        "codex_session_path": turn.session_path,
        "codex_turn_id": turn.turn_id,
        "codex_turn_index": turn.turn_index,
        "cwd": turn.cwd,
        "originator": turn.originator,
        "model": turn.model,
        "reasoning_effort": turn.reasoning_effort,
        "classification": turn.classification,
        "user_prompt_hash": turn.user_message_hash,
        "has_assistant_response": turn.has_assistant_response,
        "tool_call_count": turn.tool_call_count,
        "input_tokens": turn.input_tokens,
        "cached_input_tokens": turn.cached_input_tokens,
        "output_tokens": turn.output_tokens,
        "reasoning_output_tokens": turn.reasoning_output_tokens,
        "total_tokens": turn.total_tokens,
    }


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = parse_iso(value)
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(f"{value}T00:00:00")
        except ValueError as exc:
            raise CodexSessionError(f"--since must be an ISO timestamp or date: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _attempt_in_window(attempt: AttemptView, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    started = parse_iso(attempt.started_at)
    ended = parse_iso(attempt.ended_at)
    anchor = ended or started
    if anchor is None:
        return False
    return anchor >= cutoff


def _turn_key(turn: CodexTurn) -> tuple[str, str]:
    return (turn.thread_id, turn.turn_id)


def _row_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _hash_text(text: str | None) -> str | None:
    if text is None:
        return None
    normalized = str(text).strip()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _message_text(payload: Mapping[str, Any]) -> str | None:
    content = payload.get("content")
    if not isinstance(content, list):
        return None
    chunks: list[str] = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") in {"input_text", "output_text", "text"}:
            text = _optional_text(item.get("text"))
            if text:
                chunks.append(text)
    return "\n".join(chunks).strip() or None


def _excerpt(text: str | None) -> str | None:
    if not text:
        return None
    compact = " ".join(str(text).split())
    if len(compact) <= 160:
        return compact
    return compact[:157].rstrip() + "..."


def _nested_setting(payload: Mapping[str, Any], key: str) -> str | None:
    collaboration = payload.get("collaboration_mode")
    if not isinstance(collaboration, Mapping):
        return None
    settings = collaboration.get("settings")
    if not isinstance(settings, Mapping):
        return None
    return _optional_text(settings.get(key))


def _timestamp_from_payload(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _optional_text(value)
    return datetime.fromtimestamp(number, tz=timezone.utc).isoformat(timespec="seconds")


_TOKEN_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def _token_usage_from_payload(payload: Mapping[str, Any]) -> dict[str, int] | None:
    info = payload.get("info")
    if not isinstance(info, Mapping):
        return None
    usage = info.get("last_token_usage")
    if not isinstance(usage, Mapping):
        return None
    return {key: _non_negative_int(usage.get(key)) for key in _TOKEN_USAGE_KEYS}


def _add_token_usage(turn: dict[str, Any], usage: Mapping[str, int]) -> None:
    for key in _TOKEN_USAGE_KEYS:
        turn[key] = int(turn.get(key) or 0) + int(usage.get(key) or 0)


def _non_negative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _relative_session_path(path: Path, home: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(home.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _find_session_path_for_thread(home: Path, thread_id: str | None) -> str | None:
    if not thread_id:
        return None
    sessions_dir = home / "sessions"
    if not sessions_dir.exists():
        return None
    matches = sorted(sessions_dir.rglob(f"rollout-*{thread_id}.jsonl"))
    if not matches:
        return None
    return _relative_session_path(matches[-1], home)


def _thread_id_from_filename(path: Path) -> str | None:
    stem = path.stem
    if "-" not in stem:
        return None
    return stem.rsplit("-", 1)[-1] or None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "CODEX_SESSION_HISTORY_SCHEMA_VERSION",
    "HISTORY_DIR_NAME",
    "HISTORY_FILE_NAME",
    "CodexRuntimeContext",
    "CodexSession",
    "CodexSessionError",
    "CodexTurn",
    "build_codex_coverage",
    "build_codex_history",
    "classify_user_message",
    "codex_home",
    "current_codex_runtime_context",
    "current_codex_session_ref",
    "history_export_path",
    "read_codex_config",
    "read_codex_session",
    "render_codex_coverage_text",
    "render_codex_history_text",
]
