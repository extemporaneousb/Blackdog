from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
import hashlib
import json
import re
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


_TURN_DOMAIN_ORDER = (
    "ui",
    "docs",
    "tests",
    "backend",
    "data",
    "repo_lifecycle",
    "environment",
    "deployment",
    "external_write",
    "destructive",
)
_TURN_CLASSIFICATION_SOURCE = "heuristic"

_DOMAIN_TERMS = {
    "ui": ("ui", "ux", "frontend", "front-end", "page", "html", "css", "layout", "padding", "panel", "modal", "button"),
    "docs": (
        "doc", "docs", "documentation", "readme", "changelog", "markdown", "guide", "manual", "release notes",
    ),
    "tests": ("test", "tests", "testing", "pytest", "unittest", "regression", "fixture"),
    "backend": ("backend", "back-end", "server", "api", "endpoint", "service", "handler", "database", "sql"),
    "data": (
        "data", "dataset", "ingest", "ingestion", "etl", "csv", "tsv", "jsonl", "workbook", "spreadsheet",
        "schema", "records", "query",
    ),
    "repo_lifecycle": (
        "repo", "repository", "git", "branch", "commit", "worktree", "pull request", "merge", "checkout",
    ),
    "environment": (
        "environment", "virtualenv", "venv", ".ve", "dependencies", "docker", "container", "bootstrap", "setup",
        "runtime",
    ),
    "deployment": (
        "deploy", "deployed", "deploying", "deployment", "rollout", "production", "prod", "release to production",
    ),
    "external_write": (
        "publish", "published", "publishing", "upload", "uploaded", "git push", "push branch", "push the branch",
        "push changes", "push the changes", "push commit", "push the commit", "pushed changes", "pushing changes",
        "send email", "send message",
        "post to slack", "post to teams", "create issue", "update issue", "create ticket", "update ticket",
        "create pull request",
    ),
    "destructive": (
        "rm -rf", "reset --hard", "drop table", "drop database", "drop schema", "truncate table", "force-delete",
        "force delete", "delete file", "delete the file", "delete directory", "delete the directory", "delete branch",
        "delete the branch", "delete database", "delete the database", "delete table", "delete the table",
        "delete records", "delete the records", "delete data", "delete the data", "destroy", "purge", "wipe",
        "remove file", "remove directory", "remove branch", "remove database", "remove table", "remove records",
        "remove data",
    ),
}
_STATUS_PREFIX = re.compile(
    r"^(?:(?:please|can you|could you|would you|will you)\s+)?(?:status\b|progress\b|current state\b|"
    r"where (?:are|do) we\b|what (?:has )?changed\b|"
    r"what(?:'s| is) left\b|what remains\b|what(?:'s| is) (?:the )?(?:current )?status\b|"
    r"how (?:is|are) .+ going\b|update me\b|(?:give|send) me (?:a |an )?(?:status|progress)(?: update)?\b)"
)
_IMPLEMENTATION_TERMS = (
    "add", "build", "change", "commit", "create", "delete", "deploy", "edit", "fix", "implement", "land",
    "migrate", "modify", "patch", "publish", "refactor", "remove", "rename", "replace", "restore", "run",
    "ship", "update", "upload", "validate", "write",
)
_ANALYSIS_TERMS = (
    "analyze", "assess", "audit", "compare", "diagnose", "evaluate", "examine", "explain", "inspect",
    "investigate", "review", "summarize", "trace", "understand",
)
_QUESTION_PREFIX = re.compile(
    r"^(?:what|why|how|when|where|who|which|is|are|do|does|did|has|have|can|could|should|would)\b"
)
_POLITE_IMPLEMENTATION_PREFIX = re.compile(r"^(?:please|can you|could you|would you|will you)\b")
_GUARDED_DOMAINS = frozenset({"deployment", "external_write", "destructive"})


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
    turn_classification = _best_effort_turn_classification(hook_payload)
    context_payload = {
        "schema_version": CODEX_HOOK_TASK_CONTEXT_SCHEMA_VERSION,
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "cwd": str(effective_cwd),
        "context_found": active_attempt is not None,
        "hook": _hook_observability_payload(hook_payload),
        "turn_classification": turn_classification,
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
        "turn_classification": turn_classification,
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


def _best_effort_turn_classification(hook_payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return _classify_turn(hook_payload)
    except Exception:
        # Hook classification is observability only. A classifier defect must not
        # prevent the existing task-context stamp from being recorded.
        return _unknown_turn_classification()


def _classify_turn(hook_payload: Mapping[str, Any]) -> dict[str, Any]:
    text = _hook_turn_text(hook_payload)
    if text is None:
        return _unknown_turn_classification()

    normalized = " ".join(text.casefold().split())
    domains = [
        domain
        for domain in _TURN_DOMAIN_ORDER
        if _contains_any_term(normalized, _DOMAIN_TERMS[domain])
    ]
    question_like = normalized.endswith("?") or _QUESTION_PREFIX.search(normalized) is not None
    implementation_like = _contains_any_term(normalized, _IMPLEMENTATION_TERMS)
    analysis_like = _contains_any_term(normalized, _ANALYSIS_TERMS)

    if _STATUS_PREFIX.search(normalized) is not None:
        intent = "status"
    elif implementation_like and (not question_like or _POLITE_IMPLEMENTATION_PREFIX.search(normalized) is not None):
        intent = "implementation"
    elif analysis_like:
        intent = "analysis"
    elif question_like:
        intent = "question"
    elif implementation_like:
        intent = "implementation"
    else:
        intent = "unknown"

    risk = "guarded" if any(domain in _GUARDED_DOMAINS for domain in domains) else "normal"
    confidence = "high" if intent != "unknown" else ("medium" if domains else "low")
    return {
        "intent": intent,
        "domains": domains,
        "risk": risk,
        "source": _TURN_CLASSIFICATION_SOURCE,
        "confidence": confidence,
    }


def _unknown_turn_classification() -> dict[str, Any]:
    return {
        "intent": "unknown",
        "domains": [],
        "risk": "unknown",
        "source": _TURN_CLASSIFICATION_SOURCE,
        "confidence": "low",
    }


def _hook_turn_text(hook_payload: Mapping[str, Any]) -> str | None:
    return _optional_text(hook_payload.get("prompt")) or _optional_text(hook_payload.get("message"))


def _contains_any_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text) is not None for term in terms)


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
