from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import hashlib
import json
import subprocess

from blackdog_core.codex_sessions import CODEX_HOOK_TASK_CONTEXT_SCHEMA_VERSION, codex_task_context_path
from blackdog_core.profile import RepoProfile
from blackdog_core.state import (
    ATTEMPT_ACTIVE_STATUSES,
    TaskAttemptRecord,
    append_event,
    load_runtime_state,
)


class CodexHookError(RuntimeError):
    pass


def load_codex_hook_payload(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CodexHookError(f"Codex hook payload must be valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CodexHookError("Codex hook payload must be a JSON object")
    return payload


def stamp_codex_task_context(
    profile: RepoProfile,
    *,
    hook_payload: Mapping[str, Any],
    cwd: Path | None = None,
) -> dict[str, Any]:
    session_cwd = _optional_text(hook_payload.get("cwd"))
    effective_cwd = Path(session_cwd).expanduser().resolve() if session_cwd else (cwd or Path.cwd()).resolve()
    active_attempt = _active_attempt_context(profile, effective_cwd)
    context_payload = {
        "schema_version": CODEX_HOOK_TASK_CONTEXT_SCHEMA_VERSION,
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "cwd": str(effective_cwd),
        "context_found": active_attempt is not None,
        "hook": _hook_observability_payload(hook_payload),
        "active_attempt": active_attempt,
    }
    path = codex_task_context_path(profile)
    event = append_event(
        path,
        event_type="codex.hook.task_context",
        actor="codex-hook",
        payload=context_payload,
    )
    return {
        "stamped": True,
        "stamp_path": str(path),
        "context_found": active_attempt is not None,
        "hook_event_name": context_payload["hook"].get("hook_event_name"),
        "session_id": context_payload["hook"].get("session_id"),
        "turn_id": context_payload["hook"].get("turn_id"),
        "active_attempt": active_attempt,
        "event_id": event["event_id"],
    }


def _active_attempt_context(profile: RepoProfile, cwd: Path) -> dict[str, Any] | None:
    runtime_state = load_runtime_state(profile.paths)
    current_branch = _current_branch(cwd)
    candidates: list[tuple[tuple[int, int, str], str, TaskAttemptRecord, str]] = []
    for workset in runtime_state.worksets:
        for attempt in workset.attempts:
            if attempt.status not in ATTEMPT_ACTIVE_STATUSES or attempt.ended_at is not None:
                continue
            worktree_path = Path(attempt.worktree_path).expanduser().resolve() if attempt.worktree_path else None
            if worktree_path is not None and _path_contains(worktree_path, cwd):
                score = (0, -len(str(worktree_path)), attempt.attempt_id)
                candidates.append((score, workset.workset_id, attempt, "worktree_path"))
                continue
            if current_branch and attempt.branch == current_branch:
                score = (1, 0, attempt.attempt_id)
                candidates.append((score, workset.workset_id, attempt, "branch"))
    if not candidates:
        return None
    _score, workset_id, attempt, matched_by = sorted(candidates, key=lambda item: item[0])[0]
    return {
        "workset_id": workset_id,
        "task_id": attempt.task_id,
        "attempt_id": attempt.attempt_id,
        "status": attempt.status,
        "actor": attempt.actor,
        "started_at": attempt.started_at,
        "branch": attempt.branch,
        "target_branch": attempt.target_branch,
        "worktree_path": attempt.worktree_path,
        "matched_by": matched_by,
        "codex_thread_id": attempt.codex_session.thread_id if attempt.codex_session is not None else None,
        "codex_session_path": attempt.codex_session.session_path if attempt.codex_session is not None else None,
        "codex_turn_id": attempt.codex_session.turn_id if attempt.codex_session is not None else None,
    }


def _hook_observability_payload(hook_payload: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "hook_event_name",
        "session_id",
        "turn_id",
        "transcript_path",
        "cwd",
        "model",
        "permission_mode",
        "source",
        "tool_name",
        "tool_use_id",
    ):
        value = _optional_text(hook_payload.get(key))
        if value is not None:
            payload[key] = value
    prompt_text = _hook_turn_text(hook_payload)
    if prompt_text is not None:
        payload["prompt_hash"] = _hash_text(prompt_text)
    tool_input = hook_payload.get("tool_input")
    if isinstance(tool_input, Mapping):
        command = _optional_text(tool_input.get("command"))
        if command is not None:
            payload["tool_command_hash"] = _hash_text(command)
    return payload


def _hook_turn_text(hook_payload: Mapping[str, Any]) -> str | None:
    return _optional_text(hook_payload.get("prompt")) or _optional_text(hook_payload.get("message"))


def _current_branch(cwd: Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    branch = completed.stdout.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def _path_contains(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _hash_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "CodexHookError",
    "load_codex_hook_payload",
    "stamp_codex_task_context",
]
