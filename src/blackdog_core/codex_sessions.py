"""Codex session indexing and history export read models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
from .state import (
    CODEX_CAPTURE_METHOD_EXACT_ACTIVE_TURN,
    CODEX_CAPTURE_METHOD_EXACT_PROMPT_HASH,
    CODEX_CAPTURE_MISSING_REASON_CAPTURE_ERROR,
    CODEX_CAPTURE_MISSING_REASON_MULTIPLE_OPEN_TURNS,
    CODEX_CAPTURE_MISSING_REASON_NO_OPEN_TURN,
    CODEX_CAPTURE_MISSING_REASON_PROMPT_HASH_AMBIGUOUS,
    CODEX_CAPTURE_MISSING_REASON_SESSION_MISSING,
    CODEX_CAPTURE_MISSING_REASON_SESSION_PATH_MISSING,
    CODEX_CAPTURE_MISSING_REASON_SESSION_UNREADABLE,
    CODEX_CAPTURE_MISSING_REASON_THREAD_MISMATCH,
    CODEX_CAPTURE_STATUS_CAPTURED,
    CODEX_CAPTURE_STATUS_MISSING,
    CodexSessionRefRecord,
    atomic_write_text,
    now_iso,
    parse_iso,
)
from .user_state import user_state_file


CODEX_SESSION_HISTORY_SCHEMA_VERSION = 2
CODEX_SESSION_CACHE_SCHEMA_VERSION = 1
CODEX_HOOK_TASK_CONTEXT_SCHEMA_VERSION = 1
HISTORY_DIR_NAME = ".blackdog"
HISTORY_FILE_NAME = "history.jsonl"
CODEX_SESSION_CACHE_FILE_NAME = "session-cache-v1.json"
CODEX_HOOK_CONTEXT_DIR_NAME = "codex"
CODEX_HOOK_TASK_CONTEXT_FILE_NAME = "task-context.jsonl"

_TURN_CLASSIFICATION_INTENTS = (
    "implementation",
    "analysis",
    "question",
    "status",
    "unknown",
)
_TURN_CLASSIFICATION_DOMAINS = (
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
_TURN_CLASSIFICATION_RISKS = ("normal", "guarded", "unknown")
_TURN_CLASSIFICATION_SOURCES = ("heuristic",)
_TURN_CLASSIFICATION_CONFIDENCES = ("low", "medium", "high")
_TURN_CLASSIFICATION_CONFIDENCE_RANK = {
    confidence: rank for rank, confidence in enumerate(_TURN_CLASSIFICATION_CONFIDENCES)
}
_TURN_CLASSIFICATION_HOOK_EVENT_RANK = {"UserPromptSubmit": 1}

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

RELATIONSHIP_LAUNCH_TURN = "launch_turn"
RELATIONSHIP_PROMPT_HASH = "prompt_hash"
RELATIONSHIP_SESSION_SINGLE_TURN = "session_single_turn"
RELATIONSHIP_ACTIVE_ATTEMPT_WINDOW = "active_attempt_window"
RELATIONSHIP_SAME_SESSION = "same_session"
RELATIONSHIP_HOOK_CONTEXT = "hook_context"
STRONG_ATTEMPT_RELATIONSHIPS = frozenset(
    {
        RELATIONSHIP_LAUNCH_TURN,
        RELATIONSHIP_PROMPT_HASH,
        RELATIONSHIP_SESSION_SINGLE_TURN,
        RELATIONSHIP_HOOK_CONTEXT,
    }
)
_RELATIONSHIP_ORDER = {
    RELATIONSHIP_LAUNCH_TURN: 0,
    RELATIONSHIP_PROMPT_HASH: 1,
    RELATIONSHIP_SESSION_SINGLE_TURN: 2,
    RELATIONSHIP_HOOK_CONTEXT: 3,
    RELATIONSHIP_ACTIVE_ATTEMPT_WINDOW: 4,
    RELATIONSHIP_SAME_SESSION: 5,
}

_ATTEMPT_WINDOW_ENDED_OR_STARTED = "ended_at_or_started_at"
_ATTEMPT_WINDOW_STARTED = "started_at"
_ATTEMPT_WINDOW_ANCHORS = frozenset({_ATTEMPT_WINDOW_ENDED_OR_STARTED, _ATTEMPT_WINDOW_STARTED})

_ENVIRONMENT_ISSUE_CLASSES = (
    "missing_container_runtime",
    "missing_venv",
    "wrong_worktree_env",
    "missing_python_module",
    "missing_node_dependency",
    "missing_credential",
    "source_file_bad_format",
    "missing_cli",
    "unknown_environment_issue",
)
ENVIRONMENT_EVIDENCE_OBSERVED_FAILURE = "observed_failure"
ENVIRONMENT_EVIDENCE_OPERATOR_GUIDANCE = "operator_guidance"
_MAX_ENVIRONMENT_ISSUE_SCAN_CHARS = 20_000
_ENVIRONMENT_ISSUE_SCAN_EDGE_CHARS = 10_000
_ENVIRONMENT_ISSUE_CLASS_ORDER = {issue_class: index for index, issue_class in enumerate(_ENVIRONMENT_ISSUE_CLASSES)}
_ENVIRONMENT_ISSUE_SUPPRESSIONS = {
    "missing_cli": frozenset({"missing_container_runtime", "missing_venv", "wrong_worktree_env"}),
}
_ENVIRONMENT_ISSUE_PATTERN_SPECS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "missing_container_runtime",
        (
            ("docker_command_not_found", r"\bdocker:\s+command not found\b"),
            ("finch_command_not_found", r"\bfinch:\s+command not found\b"),
            ("docker_not_on_path", r"\bdocker\b.{0,80}\bon PATH\b"),
            ("docker_daemon_unavailable", r"\bcannot connect to the Docker daemon\b"),
            ("no_container_runtime", r"\bno container runtime\b"),
        ),
    ),
    (
        "missing_venv",
        (
            ("ve_path_missing", r"(?:^|\s)\.?/?\.VE/bin/[^:\s]+:\s+No such file"),
            ("ve_no_such_file", r"No such file or directory:\s*['\"]?\.?/\.VE\b"),
            ("virtualenv_missing", r"\bvirtualenv\b.{0,80}\bmissing\b"),
            ("venv_not_found", r"\bvenv\b.{0,40}\bnot found\b"),
            ("ensure_ve_required", r"\bensure-ve\b"),
        ),
    ),
    (
        "wrong_worktree_env",
        (
            ("primary_worktree", r"\bprimary worktree:\s*yes\b"),
            ("workspace_role_primary", r"\bworkspace role:\s*primary\b"),
            ("ve_bound_other_worktree", r"\.VE\b.{0,80}\bbound to another worktree\b"),
            ("wrong_project_root", r"\bwrong project root\b"),
            ("not_task_worktree", r"\bnot in (?:a )?task worktree\b"),
            ("managed_checkout_disappeared", r"\bmanaged task checkout disappeared\b"),
        ),
    ),
    (
        "missing_python_module",
        (
            ("module_not_found", r"\bModuleNotFoundError:\s+No module named\b"),
            ("import_name_missing", r"\bImportError:\s+cannot import name\b"),
        ),
    ),
    (
        "missing_node_dependency",
        (
            ("cannot_find_module", r"\bCannot find module\b"),
            ("cannot_find_package", r"\bCannot find package\b"),
            ("err_module_not_found", r"\bERR_MODULE_NOT_FOUND\b"),
            ("module_not_resolved", r"\bModule not found:\s+Can't resolve\b"),
            ("missing_node_modules", r"\bnode_modules\b.{0,80}\b(?:missing|not found|does not exist)\b"),
        ),
    ),
    (
        "missing_credential",
        (
            ("unable_locate_credentials", r"\bUnable to locate credentials\b"),
            ("no_credentials", r"\bNoCredentialsError\b"),
            ("expired_token", r"\bExpiredToken\b"),
            ("access_denied", r"\bAccessDenied\b"),
            ("http_401_403", r"\b(?:401|403)\b.{0,80}\b(?:unauthorized|forbidden|permission|access)\b"),
            ("openai_key_missing", r"\bOPENAI_API_KEY\b.{0,80}\b(?:not set|required|missing)\b"),
            ("msgraph_token_required", r"\bMSGRAPH_TOKEN\b.{0,80}\brequired\b"),
        ),
    ),
    (
        "source_file_bad_format",
        (
            ("bad_zip_file", r"\bBadZipFile\b"),
            ("not_zip_file", r"\bFile is not a zip file\b"),
            ("ole_compound_file", r"\bOLE Compound File\b"),
            ("invalid_xlsx", r"\bnot a valid xlsx\b"),
        ),
    ),
    (
        "missing_cli",
        (
            ("command_not_found", r"\bcommand not found\b"),
            ("not_in_path", r"\bexecutable file not found in \$?PATH\b"),
            ("missing_executable_path", r"No such file or directory:\s*['\"][^'\"]+['\"]"),
        ),
    ),
    (
        "unknown_environment_issue",
        (
            (
                "generic_environment_failure",
                r"\b(?:local environment|tooling|setup|preflight)\b.{0,120}\b(?:failed|failure|missing|unavailable|not found)\b",
            ),
        ),
    ),
)
_ENVIRONMENT_ISSUE_PATTERNS = tuple(
    (issue_class, tuple((name, re.compile(pattern, re.IGNORECASE)) for name, pattern in patterns))
    for issue_class, patterns in _ENVIRONMENT_ISSUE_PATTERN_SPECS
)


@dataclass(frozen=True, slots=True)
class CodexRuntimeContext:
    thread_id: str | None
    session_path: str | None
    model: str | None
    reasoning_effort: str | None


@dataclass(frozen=True, slots=True)
class EnvironmentIssueEvidence:
    issue_class: str
    source: str
    pattern: str
    excerpt: str | None
    evidence_kind: str = ENVIRONMENT_EVIDENCE_OBSERVED_FAILURE


@dataclass(frozen=True, slots=True)
class CodexAttemptRelationship:
    attempt_id: str
    task_id: str
    relationship: str
    linked: bool


@dataclass(frozen=True, slots=True)
class CodexHookTurnClassification:
    intent: str
    domains: tuple[str, ...]
    risk: str
    source: str
    confidence: str


@dataclass(frozen=True, slots=True)
class CodexHookTaskContextStamp:
    thread_id: str
    turn_id: str
    workset_id: str | None
    task_id: str | None
    attempt_id: str | None
    hook_event_name: str | None
    stamped_at: str | None
    turn_classification: CodexHookTurnClassification | None = None


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
    completed_at: str | None
    duration_ms: int | None
    time_to_first_token_ms: int | None
    tool_call_count: int
    primary_environment_issue_class: str | None
    environment_issue_classes: tuple[str, ...]
    environment_issue_evidence: tuple[EnvironmentIssueEvidence, ...]
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
    except (OSError, tomllib.TOMLDecodeError):
        return {"model": None, "reasoning_effort": None}
    if not isinstance(payload, dict):
        return {"model": None, "reasoning_effort": None}
    return {
        "model": _optional_text(payload.get("model")),
        "reasoning_effort": _optional_text(payload.get("model_reasoning_effort")),
    }


def current_codex_runtime_context(home: Path | None = None) -> CodexRuntimeContext:
    thread_id = _optional_text(os.environ.get("CODEX_THREAD_ID"))
    try:
        resolved_home = home or codex_home()
        config = read_codex_config(resolved_home)
        session_path = _find_session_path_for_thread(resolved_home, thread_id) if thread_id else None
    except Exception:
        # Runtime/session metadata is optional evidence. Its discovery must not
        # prevent task creation or handler execution.
        config = {"model": None, "reasoning_effort": None}
        session_path = None
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
    thread_id = _optional_text(os.environ.get("CODEX_THREAD_ID"))
    if thread_id is None:
        return None
    try:
        resolved_home = home or codex_home()
        session_path = _find_session_path_for_thread(resolved_home, thread_id)
    except Exception:
        return _missing_codex_session_ref(
            thread_id=thread_id,
            session_path=None,
            user_prompt_hash=user_prompt_hash,
            execution_prompt_hash=execution_prompt_hash,
            reason=CODEX_CAPTURE_MISSING_REASON_CAPTURE_ERROR,
        )
    if session_path is None:
        return _missing_codex_session_ref(
            thread_id=thread_id,
            session_path=None,
            user_prompt_hash=user_prompt_hash,
            execution_prompt_hash=execution_prompt_hash,
            reason=CODEX_CAPTURE_MISSING_REASON_SESSION_PATH_MISSING,
        )
    session_file = _session_file_from_ref(session_path, resolved_home)
    if session_file is None or not session_file.is_file():
        return _missing_codex_session_ref(
            thread_id=thread_id,
            session_path=session_path,
            user_prompt_hash=user_prompt_hash,
            execution_prompt_hash=execution_prompt_hash,
            reason=CODEX_CAPTURE_MISSING_REASON_SESSION_MISSING,
        )
    try:
        session = read_codex_session(session_file, home=resolved_home)
    except CodexSessionError:
        return _missing_codex_session_ref(
            thread_id=thread_id,
            session_path=session_path,
            user_prompt_hash=user_prompt_hash,
            execution_prompt_hash=execution_prompt_hash,
            reason=CODEX_CAPTURE_MISSING_REASON_SESSION_UNREADABLE,
        )
    except Exception:
        return _missing_codex_session_ref(
            thread_id=thread_id,
            session_path=session_path,
            user_prompt_hash=user_prompt_hash,
            execution_prompt_hash=execution_prompt_hash,
            reason=CODEX_CAPTURE_MISSING_REASON_CAPTURE_ERROR,
        )
    if session is None:
        return _missing_codex_session_ref(
            thread_id=thread_id,
            session_path=session_path,
            user_prompt_hash=user_prompt_hash,
            execution_prompt_hash=execution_prompt_hash,
            reason=CODEX_CAPTURE_MISSING_REASON_SESSION_UNREADABLE,
        )
    if session.thread_id != thread_id:
        return _missing_codex_session_ref(
            thread_id=thread_id,
            session_path=session_path,
            user_prompt_hash=user_prompt_hash,
            execution_prompt_hash=execution_prompt_hash,
            reason=CODEX_CAPTURE_MISSING_REASON_THREAD_MISMATCH,
        )

    prompt_hashes = {item for item in (user_prompt_hash, execution_prompt_hash) if item}
    prompt_matches = tuple(turn for turn in session.turns if turn.user_message_hash in prompt_hashes)
    open_turns = tuple(turn for turn in session.turns if turn.completed_at is None)
    sole_open_turn = open_turns[0] if len(open_turns) == 1 else None

    # A live invocation outranks a stale completed prompt match. Prompt text is
    # commonly reused or wrapped by skills, while the exact session has at most
    # one current open turn in the normal case.
    if sole_open_turn is not None:
        matched_turn = sole_open_turn
        capture_method = (
            CODEX_CAPTURE_METHOD_EXACT_PROMPT_HASH
            if len(prompt_matches) == 1 and prompt_matches[0].turn_id == sole_open_turn.turn_id
            else CODEX_CAPTURE_METHOD_EXACT_ACTIVE_TURN
        )
    elif len(prompt_matches) == 1 and (
        not open_turns or any(turn.turn_id == prompt_matches[0].turn_id for turn in open_turns)
    ):
        matched_turn = prompt_matches[0]
        capture_method = CODEX_CAPTURE_METHOD_EXACT_PROMPT_HASH
    elif len(prompt_matches) > 1:
        return _missing_codex_session_ref(
            thread_id=thread_id,
            session_path=session_path,
            user_prompt_hash=user_prompt_hash,
            execution_prompt_hash=execution_prompt_hash,
            reason=CODEX_CAPTURE_MISSING_REASON_PROMPT_HASH_AMBIGUOUS,
        )
    else:
        return _missing_codex_session_ref(
            thread_id=thread_id,
            session_path=session_path,
            user_prompt_hash=user_prompt_hash,
            execution_prompt_hash=execution_prompt_hash,
            reason=(
                CODEX_CAPTURE_MISSING_REASON_NO_OPEN_TURN
                if not open_turns
                else CODEX_CAPTURE_MISSING_REASON_MULTIPLE_OPEN_TURNS
            ),
        )
    return CodexSessionRefRecord(
        thread_id=thread_id,
        session_path=session_path,
        turn_id=matched_turn.turn_id,
        turn_started_at=matched_turn.started_at,
        user_prompt_hash=user_prompt_hash,
        execution_prompt_hash=execution_prompt_hash,
        capture_status=CODEX_CAPTURE_STATUS_CAPTURED,
        capture_method=capture_method,
        capture_missing_reason=None,
    )


def _missing_codex_session_ref(
    *,
    thread_id: str,
    session_path: str | None,
    user_prompt_hash: str | None,
    execution_prompt_hash: str | None,
    reason: str,
) -> CodexSessionRefRecord:
    return CodexSessionRefRecord(
        thread_id=thread_id,
        session_path=session_path,
        user_prompt_hash=user_prompt_hash,
        execution_prompt_hash=execution_prompt_hash,
        capture_status=CODEX_CAPTURE_STATUS_MISSING,
        capture_missing_reason=reason,
    )


def read_codex_session(
    path: Path,
    *,
    home: Path | None = None,
    since: str | None = None,
    include_environment_evidence: bool = True,
) -> CodexSession | None:
    resolved_home = home or codex_home()
    cutoff = _parse_since(since)
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
                "completed_at": None,
                "duration_ms": None,
                "time_to_first_token_ms": None,
                "tool_call_count": 0,
                "environment_issue_evidence": [],
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
        if row_type == "event_msg" and payload.get("type") in {"task_complete", "task_completed"}:
            current_turn_id = _optional_text(payload.get("turn_id")) or current_turn_id or f"line-{lineno}"
            turn = ensure_turn(current_turn_id)
            duration_ms = _non_negative_optional_int(payload.get("duration_ms"))
            time_to_first_token_ms = _non_negative_optional_int(payload.get("time_to_first_token_ms"))
            turn["completed_at"] = (
                _timestamp_from_payload(payload.get("completed_at"))
                or timestamp
                or turn["completed_at"]
            )
            turn["duration_ms"] = duration_ms if duration_ms is not None else turn["duration_ms"]
            turn["time_to_first_token_ms"] = (
                time_to_first_token_ms if time_to_first_token_ms is not None else turn["time_to_first_token_ms"]
            )
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
                if include_environment_evidence and _turn_payload_in_scan_window(turn, cutoff):
                    _add_environment_issue_evidence(turn, source="codex_session.assistant", text=_message_text(payload))
            elif item_type and item_type.endswith("_output"):
                if include_environment_evidence and _turn_payload_in_scan_window(turn, cutoff):
                    _add_environment_issue_evidence(
                        turn,
                        source="codex_session.tool_output",
                        text=_response_output_text(payload),
                    )
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
        turn_started_at = payload["started_at"] or started_at
        if cutoff is not None:
            parsed_started_at = parse_iso(turn_started_at)
            if parsed_started_at is None or parsed_started_at < cutoff:
                continue
        environment_evidence = _dedupe_environment_evidence(payload["environment_issue_evidence"])
        environment_classes = _environment_classes_from_evidence(environment_evidence)
        codex_turns.append(
            CodexTurn(
                thread_id=thread_id,
                session_path=session_path,
                turn_id=turn_id,
                turn_index=int(payload["turn_index"]),
                started_at=turn_started_at,
                cwd=payload["cwd"] or session_cwd,
                originator=originator,
                model=payload["model"] or session_model,
                reasoning_effort=payload["reasoning_effort"],
                user_message_hash=user_hash,
                message_excerpt=_excerpt(user_message),
                classification=classify_user_message(user_message),
                has_assistant_response=bool(payload["assistant_response_count"]),
                completed_at=payload["completed_at"],
                duration_ms=payload["duration_ms"],
                time_to_first_token_ms=payload["time_to_first_token_ms"],
                tool_call_count=int(payload["tool_call_count"]),
                primary_environment_issue_class=environment_classes[0] if environment_classes else None,
                environment_issue_classes=environment_classes,
                environment_issue_evidence=environment_evidence,
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
    until: str | None = None,
    codex_turns: Iterable[CodexTurn] | None = None,
    include_environment_evidence: bool = True,
    attempt_window_anchor: str = _ATTEMPT_WINDOW_ENDED_OR_STARTED,
) -> dict[str, Any]:
    if attempt_window_anchor not in _ATTEMPT_WINDOW_ANCHORS:
        raise CodexSessionError(f"unsupported attempt window anchor: {attempt_window_anchor!r}")
    model = load_runtime_model(profile)
    cutoff = _parse_since(since)
    upper = _parse_until(until)
    attempts = tuple(
        attempt
        for workset in model.worksets
        for attempt in workset.attempts
        if _attempt_in_bounds(
            attempt,
            cutoff=cutoff,
            upper=upper,
            anchor=attempt_window_anchor,
        )
    )
    turns, exact_reference_resolution_counts, exact_reference_resolutions = _project_turns_with_exact_attempt_refs(
        profile,
        attempts,
        since=since,
        until=until,
        codex_turns=codex_turns,
        include_environment_evidence=include_environment_evidence,
    )
    hook_stamps = _filter_hook_task_context_stamps(load_codex_task_context_stamps(profile), turns)
    turn_classifications = _hook_turn_classifications_by_turn(hook_stamps)
    relationships = _relate_turns_to_attempts(turns, attempts, hook_stamps=hook_stamps)
    linked = _strong_link_attempt_ids(relationships)
    relationships_by_attempt = _relationships_by_attempt(relationships, turns)
    linked_attempt_ids = {attempt_id for attempt_ids in linked.values() for attempt_id in attempt_ids}
    related_attempt_ids = {
        relationship.attempt_id
        for turn_relationships in relationships.values()
        for relationship in turn_relationships
    }
    rows = []
    for turn in turns:
        attempt_ids = linked.get(_turn_key(turn), ())
        turn_relationships = relationships.get(_turn_key(turn), ())
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
                "turn_classification": _turn_classification_row(
                    turn_classifications.get(_turn_key(turn))
                ),
                "linked_attempt_ids": list(attempt_ids),
                "related_attempt_ids": [relationship.attempt_id for relationship in turn_relationships],
                "attempt_relationships": [
                    _turn_relationship_row(relationship) for relationship in turn_relationships
                ],
                "has_assistant_response": turn.has_assistant_response,
                "completed_at": turn.completed_at,
                "duration_ms": turn.duration_ms,
                "time_to_first_token_ms": turn.time_to_first_token_ms,
                "tool_call_count": turn.tool_call_count,
                "primary_environment_issue_class": turn.primary_environment_issue_class,
                "environment_issue_classes": list(turn.environment_issue_classes),
                "environment_issue_evidence": [
                    _environment_issue_evidence_row(evidence) for evidence in turn.environment_issue_evidence
                ],
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
    longest_completed_turn = _longest_completed_turn(turns)
    relationship_counts = _relationship_counts(relationships)
    environment_issue_counts = _environment_issue_counts(turns)
    environment_issue_evidence_counts = _environment_issue_evidence_counts(turns)
    environment_issue_observed_counts = _environment_issue_evidence_counts(
        turns,
        evidence_kind=ENVIRONMENT_EVIDENCE_OBSERVED_FAILURE,
    )
    environment_issue_guidance_counts = _environment_issue_evidence_counts(
        turns,
        evidence_kind=ENVIRONMENT_EVIDENCE_OPERATOR_GUIDANCE,
    )
    return {
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "generated_at": now_iso(),
        "since": since,
        "until": until,
        "counts": {
            "codex_sessions": len({turn.session_path for turn in turns}),
            "codex_user_turns": len([turn for turn in turns if turn.user_message_hash is not None]),
            "blackdog_attempts": len(attempts),
            "active_attempts": len([attempt for attempt in attempts if attempt.is_active]),
            "linked_attempts": len(linked_attempt_ids),
            "unlinked_attempts": len(attempts) - len(linked_attempt_ids),
            "related_attempts": len(related_attempt_ids),
            "unrelated_attempts": len(attempts) - len(related_attempt_ids),
            "linked_user_turns": len([row for row in rows if row["linked_attempt_ids"]]),
            "unlinked_user_turns": len([row for row in rows if row["user_message_hash"] and not row["linked_attempt_ids"]]),
            "related_user_turns": len([row for row in rows if row["user_message_hash"] and row["related_attempt_ids"]]),
            "unrelated_user_turns": len(
                [row for row in rows if row["user_message_hash"] and not row["related_attempt_ids"]]
            ),
            "implementation_like_unlinked_turns": len(
                [
                    row
                    for row in rows
                    if row["classification"] == "implementation_likely" and not row["linked_attempt_ids"]
                ]
            ),
            "analysis_only_turns": by_classification.get("analysis_only", 0),
            "environment_issue_turns": len(
                [turn for turn in turns if turn.user_message_hash and turn.environment_issue_classes]
            ),
            "environment_issue_evidence": sum(len(turn.environment_issue_evidence) for turn in turns),
            "observed_environment_issue_evidence": sum(
                1
                for turn in turns
                for evidence in turn.environment_issue_evidence
                if evidence.evidence_kind == ENVIRONMENT_EVIDENCE_OBSERVED_FAILURE
            ),
            "operator_guidance_environment_issue_evidence": sum(
                1
                for turn in turns
                for evidence in turn.environment_issue_evidence
                if evidence.evidence_kind == ENVIRONMENT_EVIDENCE_OPERATOR_GUIDANCE
            ),
            "model_known_turns": len([turn for turn in turns if turn.model]),
            "model_missing_turns": len([turn for turn in turns if not turn.model]),
            "reasoning_known_turns": len([turn for turn in turns if turn.reasoning_effort]),
            "reasoning_missing_turns": len([turn for turn in turns if not turn.reasoning_effort]),
            "longest_completed_turn_duration_ms": (
                longest_completed_turn.duration_ms if longest_completed_turn is not None else None
            ),
            "longest_completed_turn_started_at": (
                longest_completed_turn.started_at if longest_completed_turn is not None else None
            ),
            "longest_completed_turn_thread_id": (
                longest_completed_turn.thread_id if longest_completed_turn is not None else None
            ),
            "longest_completed_turn_id": longest_completed_turn.turn_id if longest_completed_turn is not None else None,
            "tool_calls": sum(turn.tool_call_count for turn in turns),
            "input_tokens": sum(turn.input_tokens for turn in turns),
            "cached_input_tokens": sum(turn.cached_input_tokens for turn in turns),
            "output_tokens": sum(turn.output_tokens for turn in turns),
            "reasoning_output_tokens": sum(turn.reasoning_output_tokens for turn in turns),
            "total_tokens": sum(turn.total_tokens for turn in turns),
            "hook_task_context_stamps": len(hook_stamps),
        },
        "by_classification": by_classification,
        "turn_classification_counts": _turn_classification_counts(turn_classifications.values()),
        "exact_reference_resolution_counts": exact_reference_resolution_counts,
        "relationship_counts": relationship_counts,
        "environment_issue_counts": environment_issue_counts,
        "environment_issue_evidence_counts": environment_issue_evidence_counts,
        "observed_environment_issue_evidence_counts": environment_issue_observed_counts,
        "operator_guidance_environment_issue_evidence_counts": environment_issue_guidance_counts,
        "attempt_status_counts": attempt_status_counts,
        "turns": rows,
        "attempts": [
            _attempt_coverage_row(
                attempt,
                linked_attempt_ids,
                relationships_by_attempt.get(attempt.attempt_id, ()),
                _attempt_environment_classes(attempt, relationships_by_attempt.get(attempt.attempt_id, ())),
                exact_reference_resolutions.get(attempt.attempt_id),
            )
            for attempt in attempts
        ],
    }


def build_codex_history(
    profile: RepoProfile,
    *,
    since: str | None = None,
    write: bool = False,
) -> dict[str, Any]:
    model = load_runtime_model(profile)
    cutoff = _parse_since(since)
    attempts = tuple(
        attempt
        for workset in model.worksets
        for attempt in workset.attempts
        if _attempt_in_window(attempt, cutoff)
    )
    turns, exact_reference_resolution_counts, exact_reference_resolutions = _project_turns_with_exact_attempt_refs(
        profile,
        attempts,
        since=since,
        until=None,
        include_environment_evidence=True,
    )
    hook_stamps = _filter_hook_task_context_stamps(load_codex_task_context_stamps(profile), turns)
    turn_classifications = _hook_turn_classifications_by_turn(hook_stamps)
    relationships = _relate_turns_to_attempts(turns, attempts, hook_stamps=hook_stamps)
    relationships_by_attempt = _relationships_by_attempt(relationships, turns)
    rows = [
        _attempt_history_row(
            profile,
            workset.workset_id,
            attempt,
            relationships_by_attempt.get(attempt.attempt_id, ()),
            _attempt_environment_classes(attempt, relationships_by_attempt.get(attempt.attempt_id, ())),
            exact_reference_resolutions.get(attempt.attempt_id),
        )
        for workset in model.worksets
        for attempt in workset.attempts
        if _attempt_in_window(attempt, cutoff)
    ]
    rows.extend(
        _turn_history_row(
            profile,
            turn,
            relationships.get(_turn_key(turn), ()),
            turn_classification=turn_classifications.get(_turn_key(turn)),
        )
        for turn in turns
        if turn.user_message_hash is not None
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
        "exact_reference_resolution_counts": exact_reference_resolution_counts,
        "rows": rows,
        "counts": {
            "rows": len(rows),
            "attempt_rows": len([row for row in rows if row.get("kind") == "attempt"]),
            "codex_turn_rows": len([row for row in rows if row.get("kind") == "codex_turn"]),
        },
    }


def history_export_path(profile: RepoProfile) -> Path:
    return profile.paths.project_root / HISTORY_DIR_NAME / HISTORY_FILE_NAME


def codex_task_context_path(profile: RepoProfile) -> Path:
    return profile.paths.control_dir / CODEX_HOOK_CONTEXT_DIR_NAME / CODEX_HOOK_TASK_CONTEXT_FILE_NAME


def load_codex_task_context_stamps(profile: RepoProfile) -> tuple[CodexHookTaskContextStamp, ...]:
    path = codex_task_context_path(profile)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    rows: list[CodexHookTaskContextStamp] = []
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        stamp = _hook_task_context_stamp_from_event(row)
        if stamp is not None:
            rows.append(stamp)
    return tuple(rows)


def collect_codex_turns(
    home: Path | None = None,
    *,
    since: str | None = None,
    until: str | None = None,
    cwd_roots: Iterable[Path] = (),
    use_cache: bool = True,
    include_environment_evidence: bool = True,
) -> tuple[CodexTurn, ...]:
    return tuple(
        turn
        for session in collect_codex_sessions(
            home=home,
            since=since,
            until=until,
            cwd_roots=cwd_roots,
            use_cache=use_cache,
            include_environment_evidence=include_environment_evidence,
        )
        for turn in _filter_turns_by_window(session.turns, since=since, until=until)
    )


def collect_codex_sessions(
    home: Path | None = None,
    *,
    since: str | None = None,
    until: str | None = None,
    cwd_roots: Iterable[Path] = (),
    use_cache: bool = True,
    include_environment_evidence: bool = True,
) -> tuple[CodexSession, ...]:
    resolved_home = home or codex_home()
    cutoff = _parse_since(since)
    upper = _parse_until(until)
    roots = _normalize_cwd_roots(cwd_roots)
    root_strings = _cwd_root_strings(roots)
    cache = _load_codex_session_cache() if use_cache else {}
    changed = False
    sessions: list[CodexSession] = []
    for path in _session_files(resolved_home):
        session_path = _relative_session_path(path, resolved_home)
        session = None
        cache_key = _codex_session_cache_key(resolved_home, session_path)
        stat = _session_file_stat(path)
        cached = cache.get(cache_key)
        if use_cache and stat is not None and _cached_session_valid(cached, stat, include_environment_evidence=include_environment_evidence):
            cached_session = cached.get("session")
            if root_strings and not _cached_session_payload_may_match_roots(cached_session, root_strings):
                continue
            session = _codex_session_from_cache_payload(cached_session)
        if session is None:
            if not _session_file_may_overlap_window(path, cutoff=cutoff, upper=upper):
                continue
            if root_strings and not _session_file_may_match_roots(path, root_strings):
                continue
            session = read_codex_session(
                path,
                home=resolved_home,
                include_environment_evidence=include_environment_evidence,
            )
            if use_cache and stat is not None and session is not None:
                cache[cache_key] = {
                    "schema_version": CODEX_SESSION_CACHE_SCHEMA_VERSION,
                    "codex_home": str(resolved_home),
                    "session_path": session_path,
                    "size": stat["size"],
                    "mtime_ns": stat["mtime_ns"],
                    "parsed_at": now_iso(),
                    "environment_scan": include_environment_evidence,
                    "session": _codex_session_cache_payload(session),
                }
                changed = True
        if session is None:
            continue
        if _session_matches_roots(session, roots) and _session_overlaps_window(
            session,
            cutoff=cutoff,
            upper=upper,
        ):
            sessions.append(session)
    if use_cache and changed:
        _write_codex_session_cache(cache)
    return tuple(sessions)


def codex_project_roots(profile: RepoProfile) -> tuple[Path, ...]:
    return _project_roots(profile)


def project_codex_turns(
    profile: RepoProfile,
    *,
    since: str | None = None,
    until: str | None = None,
    codex_turns: Iterable[CodexTurn] | None = None,
) -> tuple[CodexTurn, ...]:
    return _project_turns(profile, since=since, until=until, codex_turns=codex_turns)


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
            "Related: "
            f"turns={counts['related_user_turns']} attempts={counts['related_attempts']} | "
            f"Unrelated turns={counts['unrelated_user_turns']} attempts={counts['unrelated_attempts']}"
        ),
        (
            "Unlinked implementation-like turns: "
            f"{counts['implementation_like_unlinked_turns']} | Analysis-only turns: {counts['analysis_only_turns']}"
        ),
        (
            "Environment issues: "
            f"turns={counts['environment_issue_turns']} evidence={counts['environment_issue_evidence']} "
            f"{_count_label(payload.get('environment_issue_counts', {})) or '-'}"
        ),
        (
            "Model signal: "
            f"known={counts['model_known_turns']} missing={counts['model_missing_turns']} | "
            f"Reasoning known={counts['reasoning_known_turns']} missing={counts['reasoning_missing_turns']}"
        ),
        f"Hook task-context stamps: {counts.get('hook_task_context_stamps', 0)}",
        (
            "Longest completed turn: "
            f"{counts['longest_completed_turn_duration_ms'] or 0}ms "
            f"{counts['longest_completed_turn_thread_id'] or '-'} "
            f"{counts['longest_completed_turn_id'] or '-'}"
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


def _project_turns(
    profile: RepoProfile,
    *,
    since: str | None,
    until: str | None,
    codex_turns: Iterable[CodexTurn] | None = None,
    include_environment_evidence: bool = True,
) -> tuple[CodexTurn, ...]:
    roots = _project_roots(profile)
    cutoff = _parse_since(since)
    upper = _parse_until(until)
    turns: list[CodexTurn] = []
    candidate_turns = (
        codex_turns
        if codex_turns is not None
        else collect_codex_turns(
            since=since,
            until=until,
            cwd_roots=roots,
            include_environment_evidence=include_environment_evidence,
        )
    )
    for turn in candidate_turns:
        if not _cwd_matches(roots, turn.cwd):
            continue
        if cutoff is not None:
            started = parse_iso(turn.started_at)
            if started is None or started < cutoff:
                continue
        if upper is not None:
            started = parse_iso(turn.started_at)
            if started is None or started >= upper:
                continue
        turns.append(turn)
    return _sort_dedupe_turns(turns)


def _project_turns_with_exact_attempt_refs(
    profile: RepoProfile,
    attempts: tuple[AttemptView, ...],
    *,
    since: str | None,
    until: str | None,
    codex_turns: Iterable[CodexTurn] | None = None,
    include_environment_evidence: bool,
) -> tuple[tuple[CodexTurn, ...], dict[str, int], dict[str, str]]:
    # Resolve attempt-owned references first. This exact overlay is independent
    # of repository cwd and must not be discarded by the project scan's cwd
    # pruning. It still contributes only one proven turn per attempt.
    exact_turns, resolution_counts, resolutions = _resolve_exact_attempt_turns(
        attempts,
        since=since,
        until=until,
        include_environment_evidence=include_environment_evidence,
    )
    project_turns = _project_turns(
        profile,
        since=since,
        until=until,
        codex_turns=codex_turns,
        include_environment_evidence=include_environment_evidence,
    )
    return _sort_dedupe_turns((*project_turns, *exact_turns)), resolution_counts, resolutions


def _resolve_exact_attempt_turns(
    attempts: tuple[AttemptView, ...],
    *,
    since: str | None,
    until: str | None,
    include_environment_evidence: bool,
) -> tuple[tuple[CodexTurn, ...], dict[str, int], dict[str, str]]:
    """Resolve exact attempt references without widening repository cwd scope."""

    home = codex_home()
    cutoff = _parse_since(since)
    upper = _parse_until(until)
    cache = _load_codex_session_cache()
    cache_changed = False
    parsed_sessions: dict[Path, tuple[CodexSession | None, bool]] = {}
    turns: list[CodexTurn] = []
    counts: dict[str, int] = {}
    resolutions: dict[str, str] = {}

    def record(attempt_id: str, status: str) -> None:
        counts[status] = counts.get(status, 0) + 1
        resolutions[attempt_id] = status

    for attempt in attempts:
        ref = attempt.codex_session
        if ref is None:
            continue
        if ref.capture_status == CODEX_CAPTURE_STATUS_MISSING:
            record(attempt.attempt_id, "capture_missing")
            continue
        if not ref.thread_id or not ref.session_path:
            record(attempt.attempt_id, "incomplete_reference")
            continue
        prompt_hashes = _attempt_prompt_hashes(attempt) if ref.turn_id is None else set()
        if ref.turn_id is None and (ref.capture_status is not None or not prompt_hashes):
            record(attempt.attempt_id, "incomplete_reference")
            continue
        session_candidates = _validated_session_candidates_from_ref(ref.session_path, home)
        if session_candidates is None:
            record(attempt.attempt_id, "path_outside_codex_home")
            continue
        existing_candidates = tuple(path for path, _ in session_candidates if path.is_file())
        if not existing_candidates:
            record(attempt.attempt_id, "session_missing")
            continue

        resolved_turn: CodexTurn | None = None
        used_archived_copy = False
        saw_unreadable = False
        saw_thread_mismatch = False
        saw_matching_thread = False
        legacy_matches: list[CodexTurn] = []
        for candidate, archived_copy in session_candidates:
            if not candidate.is_file():
                continue
            if candidate in parsed_sessions:
                candidate_session, errored = parsed_sessions[candidate]
            else:
                candidate_session, changed, errored = _read_cached_codex_session(
                    candidate,
                    home=home,
                    cache=cache,
                    include_environment_evidence=include_environment_evidence,
                )
                parsed_sessions[candidate] = (candidate_session, errored)
                cache_changed = cache_changed or changed
            if candidate_session is None:
                saw_unreadable = saw_unreadable or errored
                continue
            if candidate_session.thread_id != ref.thread_id:
                saw_thread_mismatch = True
                continue
            saw_matching_thread = True
            if ref.turn_id is None:
                legacy_matches.extend(
                    item
                    for item in candidate_session.turns
                    if item.user_message_hash is not None and item.user_message_hash in prompt_hashes
                )
                continue
            candidate_turn = next((item for item in candidate_session.turns if item.turn_id == ref.turn_id), None)
            if candidate_turn is None:
                continue
            resolved_turn = candidate_turn
            used_archived_copy = archived_copy
            break
        if ref.turn_id is None and saw_matching_thread:
            deduped_legacy_matches = _sort_dedupe_turns(legacy_matches)
            if not deduped_legacy_matches:
                record(attempt.attempt_id, "legacy_prompt_hash_missing")
                continue
            if len(deduped_legacy_matches) > 1:
                record(attempt.attempt_id, "legacy_prompt_hash_ambiguous")
                continue
            resolved_turn = deduped_legacy_matches[0]
            used_archived_copy = resolved_turn.session_path.startswith("archived_sessions/")
        if resolved_turn is None:
            if saw_matching_thread:
                record(attempt.attempt_id, "turn_missing")
            elif saw_thread_mismatch:
                record(attempt.attempt_id, "thread_mismatch")
            elif saw_unreadable:
                record(attempt.attempt_id, "session_unreadable")
            else:
                record(attempt.attempt_id, "session_missing")
            continue
        if not _turn_in_bounds(resolved_turn, cutoff=cutoff, upper=upper):
            record(attempt.attempt_id, "turn_outside_window")
            continue
        turns.append(resolved_turn)
        if ref.turn_id is None:
            record(
                attempt.attempt_id,
                "resolved_legacy_prompt_hash_archived_copy" if used_archived_copy else "resolved_legacy_prompt_hash",
            )
        else:
            record(attempt.attempt_id, "resolved_archived_copy" if used_archived_copy else "resolved")

    if cache_changed:
        _write_codex_session_cache(cache)
    return _sort_dedupe_turns(turns), dict(sorted(counts.items())), resolutions


def _validated_session_candidates_from_ref(
    session_path: str,
    home: Path,
) -> tuple[tuple[Path, bool], ...] | None:
    candidate = Path(session_path).expanduser()
    if not candidate.is_absolute():
        candidate = home / candidate
    candidate = candidate.resolve()
    roots = {
        "sessions": (home / "sessions").resolve(),
        "archived_sessions": (home / "archived_sessions").resolve(),
    }
    matched_root: str | None = None
    relative: Path | None = None
    for name, root in roots.items():
        try:
            relative = candidate.relative_to(root)
            matched_root = name
            break
        except ValueError:
            continue
    if matched_root is None or relative is None:
        return None
    alternate_name = "archived_sessions" if matched_root == "sessions" else "sessions"
    alternate = (roots[alternate_name] / relative).resolve()
    candidates: list[tuple[Path, bool]] = [(candidate, False)]
    try:
        alternate.relative_to(roots[alternate_name])
    except ValueError:
        alternate = candidate
    if alternate != candidate:
        candidates.append((alternate, True))
    if matched_root == "sessions":
        flattened_archive = (roots["archived_sessions"] / candidate.name).resolve()
        try:
            flattened_archive.relative_to(roots["archived_sessions"])
        except ValueError:
            flattened_archive = candidate
        if flattened_archive != candidate and all(path != flattened_archive for path, _ in candidates):
            candidates.append((flattened_archive, True))
    return tuple(candidates)


def _read_cached_codex_session(
    path: Path,
    *,
    home: Path,
    cache: dict[str, dict[str, Any]],
    include_environment_evidence: bool,
) -> tuple[CodexSession | None, bool, bool]:
    session_path = _relative_session_path(path, home)
    cache_key = _codex_session_cache_key(home, session_path)
    stat = _session_file_stat(path)
    cached = cache.get(cache_key)
    if stat is not None and _cached_session_valid(
        cached,
        stat,
        include_environment_evidence=include_environment_evidence,
    ):
        return _codex_session_from_cache_payload(cached.get("session")), False, False
    try:
        session = read_codex_session(
            path,
            home=home,
            include_environment_evidence=include_environment_evidence,
        )
    except CodexSessionError:
        return None, False, True
    if session is None or stat is None:
        return session, False, False
    cache[cache_key] = {
        "schema_version": CODEX_SESSION_CACHE_SCHEMA_VERSION,
        "codex_home": str(home),
        "session_path": session_path,
        "size": stat["size"],
        "mtime_ns": stat["mtime_ns"],
        "parsed_at": now_iso(),
        "environment_scan": include_environment_evidence,
        "session": _codex_session_cache_payload(session),
    }
    return session, True, False


def _turn_in_bounds(
    turn: CodexTurn,
    *,
    cutoff: datetime | None,
    upper: datetime | None,
) -> bool:
    started = parse_iso(turn.started_at)
    if started is None:
        return cutoff is None and upper is None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if cutoff is not None and started < cutoff.astimezone(started.tzinfo):
        return False
    if upper is not None and started >= upper.astimezone(started.tzinfo):
        return False
    return True


def _sort_dedupe_turns(turns: Iterable[CodexTurn]) -> tuple[CodexTurn, ...]:
    return tuple(
        sorted(
            _dedupe_turns(turns),
            key=lambda turn: (
                parse_iso(turn.started_at) or datetime.min.replace(tzinfo=timezone.utc),
                turn.session_path,
                turn.turn_index,
            ),
        )
    )


def _session_files(home: Path) -> Iterable[Path]:
    files: list[Path] = []
    for dirname in ("sessions", "archived_sessions"):
        sessions_dir = home / dirname
        if sessions_dir.exists():
            files.extend(sessions_dir.rglob("rollout-*.jsonl"))
    return sorted(files)


def _dedupe_turns(turns: Iterable[CodexTurn]) -> tuple[CodexTurn, ...]:
    selected: dict[tuple[str, str], CodexTurn] = {}
    for turn in turns:
        key = _turn_key(turn)
        current = selected.get(key)
        if current is None or _turn_quality(turn) > _turn_quality(current):
            selected[key] = turn
    return tuple(selected.values())


def _turn_quality(turn: CodexTurn) -> tuple[int, int, int, int, int, int]:
    return (
        1 if turn.duration_ms is not None else 0,
        1 if turn.model else 0,
        1 if turn.reasoning_effort else 0,
        turn.tool_call_count,
        turn.total_tokens,
        0 if turn.session_path.startswith("archived_sessions/") else 1,
    )


def _longest_completed_turn(turns: Iterable[CodexTurn]) -> CodexTurn | None:
    completed = [turn for turn in turns if turn.duration_ms is not None]
    if not completed:
        return None
    return max(completed, key=lambda turn: (turn.duration_ms or 0, turn.started_at or "", turn.thread_id, turn.turn_id))


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


def _session_file_from_ref(session_path: str, home: Path) -> Path | None:
    candidate = Path(session_path).expanduser()
    if candidate.is_absolute():
        return candidate
    return (home / candidate).resolve()


def _attempt_prompt_hashes(attempt: AttemptView) -> set[str]:
    hashes: set[str] = set()
    for prompt_receipt in (attempt.prompt_receipt, attempt.user_prompt_receipt):
        if prompt_receipt is not None and prompt_receipt.prompt_hash:
            hashes.add(prompt_receipt.prompt_hash)
    if attempt.codex_session is not None:
        for prompt_hash in (attempt.codex_session.user_prompt_hash, attempt.codex_session.execution_prompt_hash):
            if prompt_hash:
                hashes.add(prompt_hash)
    return hashes


def _session_ref_matches(turn: CodexTurn, attempt: AttemptView) -> bool:
    if attempt.codex_session is None:
        return False
    ref = attempt.codex_session
    if ref.thread_id and ref.thread_id != turn.thread_id:
        return False
    if ref.session_path and ref.session_path != turn.session_path:
        return False
    if ref.turn_id:
        return ref.turn_id == turn.turn_id
    if ref.turn_started_at and turn.started_at:
        return ref.turn_started_at == turn.started_at
    return bool(ref.session_path or ref.thread_id)


def _link_turns_to_attempts(
    turns: tuple[CodexTurn, ...],
    attempts: tuple[AttemptView, ...],
) -> dict[tuple[str, str], tuple[str, ...]]:
    return _strong_link_attempt_ids(_relate_turns_to_attempts(turns, attempts))


def _relate_turns_to_attempts(
    turns: tuple[CodexTurn, ...],
    attempts: tuple[AttemptView, ...],
    *,
    hook_stamps: tuple[CodexHookTaskContextStamp, ...] = (),
) -> dict[tuple[str, str], tuple[CodexAttemptRelationship, ...]]:
    attempts_by_hash: dict[str, list[str]] = {}
    attempts_by_id = {attempt.attempt_id: attempt for attempt in attempts}
    attempts_by_turn_ref: dict[tuple[str, str], list[AttemptView]] = {}
    attempts_by_turn_started_at: dict[tuple[str, str], list[AttemptView]] = {}
    attempts_by_session: dict[tuple[str, str], list[AttemptView]] = {}
    hook_relationships = _hook_relationships_by_turn(hook_stamps, attempts)
    for attempt in attempts:
        for prompt_hash in _attempt_prompt_hashes(attempt):
            attempts_by_hash.setdefault(prompt_hash, []).append(attempt.attempt_id)
        if attempt.codex_session is not None:
            ref = attempt.codex_session
            if ref.turn_id:
                attempts_by_turn_ref.setdefault((ref.thread_id, ref.turn_id), []).append(attempt)
            if ref.turn_started_at:
                attempts_by_turn_started_at.setdefault((ref.thread_id, ref.turn_started_at), []).append(attempt)
            if ref.session_path:
                attempts_by_session.setdefault((ref.thread_id, ref.session_path), []).append(attempt)
    turns_by_session: dict[tuple[str, str], list[CodexTurn]] = {}
    for turn in turns:
        turns_by_session.setdefault((turn.thread_id, turn.session_path), []).append(turn)
    relationships: dict[tuple[str, str], tuple[CodexAttemptRelationship, ...]] = {}
    for turn in turns:
        candidate_attempts: dict[str, AttemptView] = {}
        if turn.user_message_hash is not None:
            for attempt_id in attempts_by_hash.get(turn.user_message_hash, ()):
                attempt = attempts_by_id.get(attempt_id)
                if attempt is not None:
                    candidate_attempts[attempt_id] = attempt
        for attempt in attempts_by_turn_ref.get(_turn_key(turn), ()):
            candidate_attempts[attempt.attempt_id] = attempt
        if turn.started_at:
            for attempt in attempts_by_turn_started_at.get((turn.thread_id, turn.started_at), ()):
                candidate_attempts[attempt.attempt_id] = attempt
        session_key = (turn.thread_id, turn.session_path)
        session_turns = tuple(turns_by_session.get(session_key, ()))
        for attempt in attempts_by_session.get(session_key, ()):
            candidate_attempts[attempt.attempt_id] = attempt
        forced_relationships = hook_relationships.get(_turn_key(turn), {})
        for attempt_id in forced_relationships:
            attempt = attempts_by_id.get(attempt_id)
            if attempt is not None:
                candidate_attempts[attempt.attempt_id] = attempt
        turn_relationships: list[CodexAttemptRelationship] = []
        for attempt in candidate_attempts.values():
            relationship = forced_relationships.get(attempt.attempt_id)
            if relationship is None:
                relationship = _turn_attempt_relationship(turn, attempt, session_turn_count=len(session_turns))
            if relationship is None:
                continue
            turn_relationships.append(
                CodexAttemptRelationship(
                    attempt_id=attempt.attempt_id,
                    task_id=attempt.task_id,
                    relationship=relationship,
                    linked=relationship in STRONG_ATTEMPT_RELATIONSHIPS,
                )
            )
        if turn_relationships:
            relationships[_turn_key(turn)] = tuple(
                sorted(
                    turn_relationships,
                    key=lambda item: (_RELATIONSHIP_ORDER.get(item.relationship, 99), item.attempt_id),
                )
            )
    return relationships


def _hook_relationships_by_turn(
    hook_stamps: tuple[CodexHookTaskContextStamp, ...],
    attempts: tuple[AttemptView, ...],
) -> dict[tuple[str, str], dict[str, str]]:
    attempts_by_id = {attempt.attempt_id: attempt for attempt in attempts}
    relationships: dict[tuple[str, str], dict[str, str]] = {}
    for stamp in hook_stamps:
        attempt_id = stamp.attempt_id
        if attempt_id is None or attempt_id not in attempts_by_id:
            continue
        relationships.setdefault((stamp.thread_id, stamp.turn_id), {})[attempt_id] = RELATIONSHIP_HOOK_CONTEXT
    return relationships


def _turn_attempt_relationship(
    turn: CodexTurn,
    attempt: AttemptView,
    *,
    session_turn_count: int,
) -> str | None:
    ref = attempt.codex_session
    same_session = False
    if ref is not None:
        same_thread = not ref.thread_id or ref.thread_id == turn.thread_id
        same_path = not ref.session_path or ref.session_path == turn.session_path
        same_session = same_thread and same_path and bool(ref.thread_id or ref.session_path)
        if same_thread and ref.turn_id and ref.turn_id == turn.turn_id:
            return RELATIONSHIP_LAUNCH_TURN
        if same_thread and ref.turn_started_at and turn.started_at and ref.turn_started_at == turn.started_at:
            return RELATIONSHIP_LAUNCH_TURN
    if turn.user_message_hash is not None and turn.user_message_hash in _attempt_prompt_hashes(attempt):
        return RELATIONSHIP_PROMPT_HASH
    if same_session:
        if session_turn_count == 1 and _session_ref_matches(turn, attempt):
            return RELATIONSHIP_SESSION_SINGLE_TURN
        if _turn_in_attempt_window(turn, attempt):
            return RELATIONSHIP_ACTIVE_ATTEMPT_WINDOW
        return RELATIONSHIP_SAME_SESSION
    return None


def _turn_in_attempt_window(turn: CodexTurn, attempt: AttemptView) -> bool:
    turn_started = parse_iso(turn.started_at)
    attempt_started = parse_iso(attempt.started_at)
    if turn_started is None or attempt_started is None:
        return False
    if turn_started < attempt_started:
        return False
    attempt_ended = parse_iso(attempt.ended_at)
    if attempt_ended is None:
        return attempt.is_active
    return turn_started <= attempt_ended


def _strong_link_attempt_ids(
    relationships: Mapping[tuple[str, str], tuple[CodexAttemptRelationship, ...]],
) -> dict[tuple[str, str], tuple[str, ...]]:
    linked: dict[tuple[str, str], tuple[str, ...]] = {}
    for turn_key, turn_relationships in relationships.items():
        attempt_ids = tuple(
            sorted(
                relationship.attempt_id
                for relationship in turn_relationships
                if relationship.relationship in STRONG_ATTEMPT_RELATIONSHIPS
            )
        )
        if attempt_ids:
            linked[turn_key] = attempt_ids
    return linked


def _relationships_by_attempt(
    relationships: Mapping[tuple[str, str], tuple[CodexAttemptRelationship, ...]],
    turns: tuple[CodexTurn, ...],
) -> dict[str, tuple[dict[str, Any], ...]]:
    turns_by_key = {_turn_key(turn): turn for turn in turns}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for turn_key, turn_relationships in relationships.items():
        turn = turns_by_key.get(turn_key)
        if turn is None:
            continue
        for relationship in turn_relationships:
            grouped.setdefault(relationship.attempt_id, []).append(
                {
                    "codex_thread_id": turn.thread_id,
                    "codex_session_path": turn.session_path,
                    "codex_turn_id": turn.turn_id,
                    "codex_turn_index": turn.turn_index,
                    "started_at": turn.started_at,
                    "relationship": relationship.relationship,
                    "linked": relationship.linked,
                    "environment_issue_classes": list(turn.environment_issue_classes),
                }
            )
    return {
        attempt_id: tuple(
            sorted(
                items,
                key=lambda item: (
                    _RELATIONSHIP_ORDER.get(str(item.get("relationship")), 99),
                    str(item.get("started_at") or ""),
                    str(item.get("codex_turn_id") or ""),
                ),
            )
        )
        for attempt_id, items in grouped.items()
    }


def _attempt_coverage_row(
    attempt: AttemptView,
    linked_attempt_ids: set[str],
    turn_relationships: tuple[dict[str, Any], ...],
    environment_issue_classes: tuple[str, ...],
    exact_reference_resolution: str | None,
) -> dict[str, Any]:
    linked_turn_ids = tuple(
        str(relationship["codex_turn_id"])
        for relationship in turn_relationships
        if relationship.get("linked") and relationship.get("codex_turn_id")
    )
    related_turn_ids = tuple(
        str(relationship["codex_turn_id"])
        for relationship in turn_relationships
        if relationship.get("codex_turn_id")
    )
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
        "codex_turn_id": attempt.codex_session.turn_id if attempt.codex_session else None,
        "codex_turn_started_at": attempt.codex_session.turn_started_at if attempt.codex_session else None,
        "codex_capture_status": attempt.codex_session.capture_status if attempt.codex_session else None,
        "codex_capture_method": attempt.codex_session.capture_method if attempt.codex_session else None,
        "codex_capture_missing_reason": (
            attempt.codex_session.capture_missing_reason if attempt.codex_session else None
        ),
        "exact_reference_resolution": exact_reference_resolution,
        "linked_codex_turn": attempt.attempt_id in linked_attempt_ids,
        "linked_codex_turn_ids": list(linked_turn_ids),
        "related_codex_turn_ids": list(related_turn_ids),
        "codex_turn_relationships": list(turn_relationships),
        "primary_environment_issue_class": environment_issue_classes[0] if environment_issue_classes else None,
        "environment_issue_classes": list(environment_issue_classes),
        "environment_issue_count": len(environment_issue_classes),
    }


def _attempt_history_row(
    profile: RepoProfile,
    workset_id: str,
    attempt: AttemptView,
    turn_relationships: tuple[dict[str, Any], ...],
    environment_issue_classes: tuple[str, ...],
    exact_reference_resolution: str | None,
) -> dict[str, Any]:
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
        "codex_turn_started_at": attempt.codex_session.turn_started_at if attempt.codex_session else None,
        "codex_capture_status": attempt.codex_session.capture_status if attempt.codex_session else None,
        "codex_capture_method": attempt.codex_session.capture_method if attempt.codex_session else None,
        "codex_capture_missing_reason": (
            attempt.codex_session.capture_missing_reason if attempt.codex_session else None
        ),
        "exact_reference_resolution": exact_reference_resolution,
        "linked_codex_turn_ids": [
            str(relationship["codex_turn_id"])
            for relationship in turn_relationships
            if relationship.get("linked") and relationship.get("codex_turn_id")
        ],
        "related_codex_turn_ids": [
            str(relationship["codex_turn_id"])
            for relationship in turn_relationships
            if relationship.get("codex_turn_id")
        ],
        "codex_turn_relationships": list(turn_relationships),
        "primary_environment_issue_class": environment_issue_classes[0] if environment_issue_classes else None,
        "environment_issue_classes": list(environment_issue_classes),
        "environment_issue_count": len(environment_issue_classes),
        "execution_prompt_hash": prompt_hash,
        "user_prompt_hash": user_prompt_hash,
        "failure_class": attempt.failure_class,
        "recovery_action": attempt.recovery_action,
        "prompt_issue": attempt.prompt_issue,
        "operator_issue": attempt.operator_issue,
        "changed_paths": list(attempt.changed_paths),
        "changed_paths_count": len(attempt.changed_paths),
        "validations": [{"name": item.name, "status": item.status} for item in attempt.validations],
        "residuals": list(attempt.residuals),
        "followup_candidates": list(attempt.followup_candidates),
        "commit": attempt.commit,
        "landed_commit": attempt.landed_commit,
        "elapsed_seconds": attempt.elapsed_seconds,
    }


def _turn_history_row(
    profile: RepoProfile,
    turn: CodexTurn,
    relationships: tuple[CodexAttemptRelationship, ...],
    *,
    turn_classification: CodexHookTurnClassification | None,
) -> dict[str, Any]:
    linked_attempt_ids = tuple(
        relationship.attempt_id for relationship in relationships if relationship.relationship in STRONG_ATTEMPT_RELATIONSHIPS
    )
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
        "turn_classification": _turn_classification_row(turn_classification),
        "user_prompt_hash": turn.user_message_hash,
        "linked_attempt_ids": list(linked_attempt_ids),
        "related_attempt_ids": [relationship.attempt_id for relationship in relationships],
        "attempt_relationships": [_turn_relationship_row(relationship) for relationship in relationships],
        "primary_environment_issue_class": turn.primary_environment_issue_class,
        "environment_issue_classes": list(turn.environment_issue_classes),
        "environment_issue_evidence": [
            _environment_issue_evidence_row(evidence) for evidence in turn.environment_issue_evidence
        ],
        "has_assistant_response": turn.has_assistant_response,
        "completed_at": turn.completed_at,
        "duration_ms": turn.duration_ms,
        "time_to_first_token_ms": turn.time_to_first_token_ms,
        "tool_call_count": turn.tool_call_count,
        "input_tokens": turn.input_tokens,
        "cached_input_tokens": turn.cached_input_tokens,
        "output_tokens": turn.output_tokens,
        "reasoning_output_tokens": turn.reasoning_output_tokens,
        "total_tokens": turn.total_tokens,
    }


def _turn_relationship_row(relationship: CodexAttemptRelationship) -> dict[str, Any]:
    return {
        "attempt_id": relationship.attempt_id,
        "task_id": relationship.task_id,
        "relationship": relationship.relationship,
        "linked": relationship.linked,
    }


def _turn_classification_row(
    classification: CodexHookTurnClassification | None,
) -> dict[str, Any] | None:
    if classification is None:
        return None
    return {
        "intent": classification.intent,
        "domains": list(classification.domains),
        "risk": classification.risk,
        "source": classification.source,
        "confidence": classification.confidence,
    }


def _turn_classification_counts(
    classifications: Iterable[CodexHookTurnClassification],
) -> dict[str, dict[str, int]]:
    intent_counts: dict[str, int] = {}
    domain_counts: dict[str, int] = {}
    risk_counts: dict[str, int] = {}
    for classification in classifications:
        intent_counts[classification.intent] = intent_counts.get(classification.intent, 0) + 1
        risk_counts[classification.risk] = risk_counts.get(classification.risk, 0) + 1
        for domain in classification.domains:
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
    return {
        "by_intent": {
            value: intent_counts[value] for value in _TURN_CLASSIFICATION_INTENTS if value in intent_counts
        },
        "by_domain": {
            value: domain_counts[value] for value in _TURN_CLASSIFICATION_DOMAINS if value in domain_counts
        },
        "by_risk": {value: risk_counts[value] for value in _TURN_CLASSIFICATION_RISKS if value in risk_counts},
    }


def _environment_issue_evidence_row(evidence: EnvironmentIssueEvidence) -> dict[str, Any]:
    return {
        "class": evidence.issue_class,
        "source": evidence.source,
        "pattern": evidence.pattern,
        "excerpt": evidence.excerpt,
        "evidence_kind": evidence.evidence_kind,
    }


def _relationship_counts(
    relationships: Mapping[tuple[str, str], tuple[CodexAttemptRelationship, ...]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for turn_relationships in relationships.values():
        for relationship in turn_relationships:
            counts[relationship.relationship] = counts.get(relationship.relationship, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (_RELATIONSHIP_ORDER.get(item[0], 99), item[0])))


def _environment_issue_counts(turns: Iterable[CodexTurn]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for turn in turns:
        for issue_class in turn.environment_issue_classes:
            counts[issue_class] = counts.get(issue_class, 0) + 1
    return _sort_environment_issue_counts(counts)


def _environment_issue_evidence_counts(
    turns: Iterable[CodexTurn],
    *,
    evidence_kind: str | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for turn in turns:
        for evidence in turn.environment_issue_evidence:
            if evidence_kind is not None and evidence.evidence_kind != evidence_kind:
                continue
            counts[evidence.issue_class] = counts.get(evidence.issue_class, 0) + 1
    return _sort_environment_issue_counts(counts)


def _attempt_environment_classes(
    attempt: AttemptView,
    turn_relationships: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    classes: set[str] = set()
    for relationship in turn_relationships:
        for issue_class in relationship.get("environment_issue_classes") or ():
            classes.add(str(issue_class))
    classes.update(_classify_attempt_environment_issues(attempt))
    return tuple(sorted(classes, key=_environment_issue_sort_key))


def _classify_attempt_environment_issues(attempt: AttemptView) -> tuple[str, ...]:
    evidence: list[EnvironmentIssueEvidence] = []
    for source, text in (
        ("attempt.summary", attempt.summary),
        ("attempt.note", attempt.note),
        ("attempt.recovery_action", attempt.recovery_action),
    ):
        evidence.extend(
            classify_environment_issue_text(
                text,
                source=source,
                evidence_kind=ENVIRONMENT_EVIDENCE_OPERATOR_GUIDANCE,
            )
        )
    for residual in attempt.residuals:
        evidence.extend(
            classify_environment_issue_text(
                residual,
                source="attempt.residual",
                evidence_kind=ENVIRONMENT_EVIDENCE_OPERATOR_GUIDANCE,
            )
        )
    return _environment_classes_from_evidence(tuple(evidence))


def classify_environment_issue_text(
    text: str | None,
    *,
    source: str,
    evidence_kind: str = ENVIRONMENT_EVIDENCE_OBSERVED_FAILURE,
) -> tuple[EnvironmentIssueEvidence, ...]:
    if not text:
        return ()
    matched: list[EnvironmentIssueEvidence] = []
    text_value = _bounded_environment_issue_scan_text(str(text))
    for issue_class, patterns in _ENVIRONMENT_ISSUE_PATTERNS:
        if issue_class == "unknown_environment_issue" and matched:
            continue
        for pattern_name, pattern in patterns:
            match = pattern.search(text_value)
            if match is None:
                continue
            matched.append(
                EnvironmentIssueEvidence(
                    issue_class=issue_class,
                    source=source,
                    pattern=pattern_name,
                    excerpt=_evidence_excerpt(text_value, match.start(), match.end()),
                    evidence_kind=evidence_kind,
                )
            )
            break
    matched_classes = {evidence.issue_class for evidence in matched}
    return tuple(
        evidence
        for evidence in matched
        if not (_ENVIRONMENT_ISSUE_SUPPRESSIONS.get(evidence.issue_class, frozenset()) & matched_classes)
    )


def _add_environment_issue_evidence(turn: dict[str, Any], *, source: str, text: str | None) -> None:
    if not text:
        return
    evidence = turn.setdefault("environment_issue_evidence", [])
    if not isinstance(evidence, list):
        return
    evidence.extend(
        classify_environment_issue_text(
            text,
            source=source,
            evidence_kind=_environment_evidence_kind_for_source(source),
        )
    )


def _turn_payload_in_scan_window(turn: Mapping[str, Any], cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    started_at = _optional_text(turn.get("started_at"))
    if started_at is None:
        return True
    parsed = parse_iso(started_at)
    return parsed is None or parsed >= cutoff


def _dedupe_environment_evidence(items: Iterable[EnvironmentIssueEvidence]) -> tuple[EnvironmentIssueEvidence, ...]:
    selected: list[EnvironmentIssueEvidence] = []
    seen: set[tuple[str, str, str, str | None, str]] = set()
    for evidence in items:
        key = (evidence.issue_class, evidence.source, evidence.pattern, evidence.excerpt, evidence.evidence_kind)
        if key in seen:
            continue
        seen.add(key)
        selected.append(evidence)
        if len(selected) >= 12:
            break
    return tuple(
        sorted(
            selected,
            key=lambda item: (_environment_issue_sort_key(item.issue_class), item.evidence_kind, item.source, item.pattern),
        )
    )


def _environment_evidence_kind_for_source(source: str) -> str:
    if source == "codex_session.tool_output":
        return ENVIRONMENT_EVIDENCE_OBSERVED_FAILURE
    return ENVIRONMENT_EVIDENCE_OPERATOR_GUIDANCE


def _environment_classes_from_evidence(
    items: Iterable[EnvironmentIssueEvidence],
    *,
    evidence_kind: str | None = ENVIRONMENT_EVIDENCE_OBSERVED_FAILURE,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                item.issue_class
                for item in items
                if evidence_kind is None or item.evidence_kind == evidence_kind
            },
            key=_environment_issue_sort_key,
        )
    )


def _sort_environment_issue_counts(counts: Mapping[str, int]) -> dict[str, int]:
    return dict(sorted(counts.items(), key=lambda item: (_environment_issue_sort_key(item[0]), item[0])))


def _environment_issue_sort_key(issue_class: str) -> int:
    return _ENVIRONMENT_ISSUE_CLASS_ORDER.get(issue_class, len(_ENVIRONMENT_ISSUE_CLASS_ORDER))


def _response_output_text(payload: Mapping[str, Any]) -> str | None:
    for key in ("output", "result", "text"):
        text = _optional_text(payload.get(key))
        if text:
            return text
    content = payload.get("content")
    if isinstance(content, str):
        return _optional_text(content)
    return _message_text(payload)


def _bounded_environment_issue_scan_text(text: str) -> str:
    if len(text) <= _MAX_ENVIRONMENT_ISSUE_SCAN_CHARS:
        return text
    return (
        text[:_ENVIRONMENT_ISSUE_SCAN_EDGE_CHARS]
        + "\n...[blackdog truncated scan window]...\n"
        + text[-_ENVIRONMENT_ISSUE_SCAN_EDGE_CHARS:]
    )


def _evidence_excerpt(text: str, start: int, end: int) -> str | None:
    window_start = max(0, start - 80)
    window_end = min(len(text), end + 80)
    return _excerpt(text[window_start:window_end])


def _count_label(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in counts.items() if value)


def codex_session_cache_path() -> Path:
    return user_state_file("codex", CODEX_SESSION_CACHE_FILE_NAME)


def _load_codex_session_cache() -> dict[str, dict[str, Any]]:
    path = codex_session_cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    if payload.get("schema_version") != CODEX_SESSION_CACHE_SCHEMA_VERSION:
        return {}
    sessions = payload.get("sessions")
    if not isinstance(sessions, Mapping):
        return {}
    return {str(key): dict(value) for key, value in sessions.items() if isinstance(value, Mapping)}


def _write_codex_session_cache(cache: Mapping[str, Mapping[str, Any]]) -> None:
    path = codex_session_cache_path()
    payload = {
        "schema_version": CODEX_SESSION_CACHE_SCHEMA_VERSION,
        "updated_at": now_iso(),
        "sessions": dict(sorted(cache.items())),
    }
    atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n")


def _codex_session_cache_key(home: Path, session_path: str) -> str:
    return hashlib.sha256(f"{home.resolve()}|{session_path}".encode("utf-8")).hexdigest()


def _session_file_stat(path: Path) -> dict[str, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return {"size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}


def _cached_session_valid(
    cached: Any,
    stat: Mapping[str, int],
    *,
    include_environment_evidence: bool,
) -> bool:
    if not isinstance(cached, Mapping):
        return False
    if cached.get("schema_version") != CODEX_SESSION_CACHE_SCHEMA_VERSION:
        return False
    if cached.get("size") != stat.get("size") or cached.get("mtime_ns") != stat.get("mtime_ns"):
        return False
    cached_environment_scan = bool(cached.get("environment_scan", True))
    return cached_environment_scan or not include_environment_evidence


def _codex_session_cache_payload(session: CodexSession) -> dict[str, Any]:
    return {
        "thread_id": session.thread_id,
        "session_path": session.session_path,
        "started_at": session.started_at,
        "cwd": session.cwd,
        "originator": session.originator,
        "model_provider": session.model_provider,
        "model": session.model,
        "turns": [_codex_turn_cache_payload(turn) for turn in session.turns],
    }


def _codex_turn_cache_payload(turn: CodexTurn) -> dict[str, Any]:
    return {
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
        "classification": turn.classification,
        "has_assistant_response": turn.has_assistant_response,
        "completed_at": turn.completed_at,
        "duration_ms": turn.duration_ms,
        "time_to_first_token_ms": turn.time_to_first_token_ms,
        "tool_call_count": turn.tool_call_count,
        "primary_environment_issue_class": turn.primary_environment_issue_class,
        "environment_issue_classes": list(turn.environment_issue_classes),
        "environment_issue_evidence": [
            _environment_issue_evidence_row(evidence) for evidence in turn.environment_issue_evidence
        ],
        "input_tokens": turn.input_tokens,
        "cached_input_tokens": turn.cached_input_tokens,
        "output_tokens": turn.output_tokens,
        "reasoning_output_tokens": turn.reasoning_output_tokens,
        "total_tokens": turn.total_tokens,
    }


def _codex_session_from_cache_payload(payload: Any) -> CodexSession | None:
    if not isinstance(payload, Mapping):
        return None
    turns_payload = payload.get("turns")
    if not isinstance(turns_payload, list):
        return None
    turns: list[CodexTurn] = []
    for item in turns_payload:
        turn = _codex_turn_from_cache_payload(item)
        if turn is not None:
            turns.append(turn)
    return CodexSession(
        thread_id=str(payload.get("thread_id") or ""),
        session_path=str(payload.get("session_path") or ""),
        started_at=_optional_text(payload.get("started_at")),
        cwd=_optional_text(payload.get("cwd")),
        originator=_optional_text(payload.get("originator")),
        model_provider=_optional_text(payload.get("model_provider")),
        model=_optional_text(payload.get("model")),
        turns=tuple(turns),
    )


def _codex_turn_from_cache_payload(payload: Any) -> CodexTurn | None:
    if not isinstance(payload, Mapping):
        return None
    evidence = tuple(
        EnvironmentIssueEvidence(
            issue_class=str(item.get("class") or ""),
            source=str(item.get("source") or ""),
            pattern=str(item.get("pattern") or ""),
            excerpt=_optional_text(item.get("excerpt")),
            evidence_kind=str(item.get("evidence_kind") or ENVIRONMENT_EVIDENCE_OBSERVED_FAILURE),
        )
        for item in payload.get("environment_issue_evidence") or []
        if isinstance(item, Mapping)
    )
    return CodexTurn(
        thread_id=str(payload.get("thread_id") or ""),
        session_path=str(payload.get("session_path") or ""),
        turn_id=str(payload.get("turn_id") or ""),
        turn_index=_non_negative_int(payload.get("turn_index")),
        started_at=_optional_text(payload.get("started_at")),
        cwd=_optional_text(payload.get("cwd")),
        originator=_optional_text(payload.get("originator")),
        model=_optional_text(payload.get("model")),
        reasoning_effort=_optional_text(payload.get("reasoning_effort")),
        user_message_hash=_optional_text(payload.get("user_message_hash")),
        message_excerpt=_optional_text(payload.get("message_excerpt")),
        classification=_optional_text(payload.get("classification")) or "unclassified",
        has_assistant_response=bool(payload.get("has_assistant_response")),
        completed_at=_optional_text(payload.get("completed_at")),
        duration_ms=_non_negative_optional_int(payload.get("duration_ms")),
        time_to_first_token_ms=_non_negative_optional_int(payload.get("time_to_first_token_ms")),
        tool_call_count=_non_negative_int(payload.get("tool_call_count")),
        primary_environment_issue_class=_optional_text(payload.get("primary_environment_issue_class")),
        environment_issue_classes=tuple(str(item) for item in payload.get("environment_issue_classes") or ()),
        environment_issue_evidence=evidence,
        input_tokens=_non_negative_int(payload.get("input_tokens")),
        cached_input_tokens=_non_negative_int(payload.get("cached_input_tokens")),
        output_tokens=_non_negative_int(payload.get("output_tokens")),
        reasoning_output_tokens=_non_negative_int(payload.get("reasoning_output_tokens")),
        total_tokens=_non_negative_int(payload.get("total_tokens")),
    )


def _filter_turns_by_window(
    turns: Iterable[CodexTurn],
    *,
    since: str | None,
    until: str | None,
) -> tuple[CodexTurn, ...]:
    cutoff = _parse_since(since)
    upper = _parse_until(until)
    filtered: list[CodexTurn] = []
    for turn in turns:
        started = parse_iso(turn.started_at)
        if cutoff is not None and (started is None or started < cutoff):
            continue
        if upper is not None and (started is None or started >= upper):
            continue
        filtered.append(turn)
    return tuple(filtered)


def _session_overlaps_window(
    session: CodexSession,
    *,
    cutoff: datetime | None,
    upper: datetime | None,
) -> bool:
    if cutoff is None and upper is None:
        return True
    if any(_datetime_in_window(parse_iso(turn.started_at), cutoff=cutoff, upper=upper) for turn in session.turns):
        return True
    return _datetime_in_window(parse_iso(session.started_at), cutoff=cutoff, upper=upper)


def _session_file_may_overlap_window(
    path: Path,
    *,
    cutoff: datetime | None,
    upper: datetime | None,
) -> bool:
    if cutoff is None and upper is None:
        return True
    started = _session_datetime_from_filename(path)
    if started is None:
        return True
    lower_bound = cutoff.astimezone(timezone.utc) - timedelta(days=1) if cutoff is not None else None
    upper_bound = upper.astimezone(timezone.utc) + timedelta(days=1) if upper is not None else None
    if lower_bound is not None and started < lower_bound:
        return False
    if upper_bound is not None and started >= upper_bound:
        return False
    return True


def _normalize_cwd_roots(cwd_roots: Iterable[Path]) -> tuple[Path, ...]:
    roots = {Path(root).expanduser().resolve() for root in cwd_roots}
    return tuple(sorted(roots))


def _cwd_root_strings(roots: Iterable[Path]) -> tuple[str, ...]:
    return tuple(sorted((os.path.normpath(str(root)) for root in roots), key=len, reverse=True))


def _cwd_text_matches_root_strings(cwd: str | None, root_strings: tuple[str, ...]) -> bool:
    if cwd is None:
        return False
    candidate = os.path.normpath(os.path.realpath(os.path.expanduser(cwd)))
    for root in root_strings:
        if candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def _session_matches_roots(session: CodexSession, roots: tuple[Path, ...]) -> bool:
    if not roots:
        return True
    if _cwd_matches(roots, session.cwd):
        return True
    return any(_cwd_matches(roots, turn.cwd) for turn in session.turns)


def _cached_session_payload_may_match_roots(payload: Any, root_strings: tuple[str, ...]) -> bool:
    if not isinstance(payload, Mapping):
        return True
    saw_cwd = False
    for cwd in _session_payload_cwds(payload):
        saw_cwd = True
        if _cwd_text_matches_root_strings(cwd, root_strings):
            return True
    return not saw_cwd


def _session_payload_cwds(payload: Mapping[str, Any]) -> Iterable[str]:
    cwd = _optional_text(payload.get("cwd"))
    if cwd is not None:
        yield cwd
    turns = payload.get("turns")
    if isinstance(turns, list):
        for turn in turns:
            if not isinstance(turn, Mapping):
                continue
            turn_cwd = _optional_text(turn.get("cwd"))
            if turn_cwd is not None:
                yield turn_cwd


def _session_file_may_match_roots(path: Path, root_strings: tuple[str, ...]) -> bool:
    saw_cwd = False
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if '"cwd"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, Mapping):
                    continue
                if row.get("type") not in {"session_meta", "turn_context"}:
                    continue
                payload = row.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                cwd = _optional_text(payload.get("cwd"))
                if cwd is None:
                    continue
                saw_cwd = True
                if _cwd_text_matches_root_strings(cwd, root_strings):
                    return True
    except OSError:
        return True
    return not saw_cwd


def _datetime_in_window(
    value: datetime | None,
    *,
    cutoff: datetime | None,
    upper: datetime | None,
) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if cutoff is not None and value < cutoff.astimezone(value.tzinfo or timezone.utc):
        return False
    if upper is not None and value >= upper.astimezone(value.tzinfo or timezone.utc):
        return False
    return True


def _session_datetime_from_filename(path: Path) -> datetime | None:
    match = re.search(
        r"rollout-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})",
        path.name,
    )
    if not match:
        return None
    year, month, day, hour, minute, second = (int(group) for group in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


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


def _parse_until(value: str | None) -> datetime | None:
    return _parse_since(value)


def _attempt_in_window(attempt: AttemptView, cutoff: datetime | None) -> bool:
    return _attempt_in_bounds(attempt, cutoff=cutoff, upper=None)


def _attempt_in_bounds(
    attempt: AttemptView,
    *,
    cutoff: datetime | None,
    upper: datetime | None,
    anchor: str = _ATTEMPT_WINDOW_ENDED_OR_STARTED,
) -> bool:
    if cutoff is None:
        lower_ok = True
    else:
        lower_ok = False
    started = parse_iso(attempt.started_at)
    ended = parse_iso(attempt.ended_at)
    anchor_time = started if anchor == _ATTEMPT_WINDOW_STARTED else ended or started
    if anchor_time is None:
        return False
    if anchor_time.tzinfo is None:
        anchor_time = anchor_time.replace(tzinfo=timezone.utc)
    if cutoff is not None:
        lower_ok = anchor_time >= cutoff.astimezone(anchor_time.tzinfo or timezone.utc)
    if not lower_ok:
        return False
    if upper is not None and anchor_time >= upper.astimezone(anchor_time.tzinfo or timezone.utc):
        return False
    return True


def _filter_hook_task_context_stamps(
    stamps: tuple[CodexHookTaskContextStamp, ...],
    turns: tuple[CodexTurn, ...],
) -> tuple[CodexHookTaskContextStamp, ...]:
    turn_keys = {_turn_key(turn) for turn in turns}
    return tuple(stamp for stamp in stamps if (stamp.thread_id, stamp.turn_id) in turn_keys)


def _hook_turn_classifications_by_turn(
    stamps: Iterable[CodexHookTaskContextStamp],
) -> dict[tuple[str, str], CodexHookTurnClassification]:
    selected: dict[tuple[str, str], CodexHookTaskContextStamp] = {}
    for stamp in stamps:
        classification = stamp.turn_classification
        if classification is None:
            continue
        key = (stamp.thread_id, stamp.turn_id)
        current = selected.get(key)
        if current is None or _hook_turn_classification_quality(stamp) > _hook_turn_classification_quality(current):
            selected[key] = stamp
    return {
        key: stamp.turn_classification
        for key, stamp in selected.items()
        if stamp.turn_classification is not None
    }


def _hook_turn_classification_quality(stamp: CodexHookTaskContextStamp) -> tuple[int, int, int, int, float]:
    classification = stamp.turn_classification
    if classification is None:
        return (0, 0, 0, 0, 0.0)
    meaningful = (
        classification.intent != "unknown"
        or bool(classification.domains)
        or classification.risk != "unknown"
    )
    event_rank = _TURN_CLASSIFICATION_HOOK_EVENT_RANK.get(stamp.hook_event_name or "", 0) if meaningful else 0
    stamped_at = parse_iso(stamp.stamped_at)
    return (
        event_rank,
        *_turn_classification_quality(classification),
        stamped_at.timestamp() if stamped_at is not None else 0.0,
    )


def _turn_classification_quality(classification: CodexHookTurnClassification) -> tuple[int, int, int]:
    information = sum(
        (
            classification.intent != "unknown",
            bool(classification.domains),
            classification.risk != "unknown",
        )
    )
    return (
        information,
        _TURN_CLASSIFICATION_CONFIDENCE_RANK[classification.confidence],
        len(classification.domains),
    )


def _hook_task_context_stamp_from_event(row: Any) -> CodexHookTaskContextStamp | None:
    if not isinstance(row, Mapping):
        return None
    if row.get("type") != "codex.hook.task_context":
        return None
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        return None
    hook = payload.get("hook")
    active_attempt = payload.get("active_attempt")
    if not isinstance(hook, Mapping):
        return None
    thread_id = _optional_text(hook.get("session_id")) or _optional_text(hook.get("thread_id"))
    turn_id = _optional_text(hook.get("turn_id"))
    if not all((thread_id, turn_id)):
        return None
    workset_id = _optional_text(active_attempt.get("workset_id")) if isinstance(active_attempt, Mapping) else None
    task_id = _optional_text(active_attempt.get("task_id")) if isinstance(active_attempt, Mapping) else None
    attempt_id = _optional_text(active_attempt.get("attempt_id")) if isinstance(active_attempt, Mapping) else None
    if not all((workset_id, task_id, attempt_id)):
        workset_id = task_id = attempt_id = None
    return CodexHookTaskContextStamp(
        thread_id=str(thread_id),
        turn_id=str(turn_id),
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt_id,
        hook_event_name=_optional_text(hook.get("hook_event_name")),
        stamped_at=_optional_text(row.get("at")),
        turn_classification=_turn_classification_from_payload(payload.get("turn_classification")),
    )


def _turn_classification_from_payload(value: Any) -> CodexHookTurnClassification | None:
    if not isinstance(value, Mapping):
        return None
    intent = value.get("intent")
    domains = value.get("domains")
    risk = value.get("risk")
    source = value.get("source")
    confidence = value.get("confidence")
    if intent not in _TURN_CLASSIFICATION_INTENTS:
        return None
    if not isinstance(domains, list) or len(domains) > len(_TURN_CLASSIFICATION_DOMAINS):
        return None
    if any(
        not isinstance(domain, str) or domain not in _TURN_CLASSIFICATION_DOMAINS for domain in domains
    ):
        return None
    if risk not in _TURN_CLASSIFICATION_RISKS:
        return None
    if source not in _TURN_CLASSIFICATION_SOURCES:
        return None
    if confidence not in _TURN_CLASSIFICATION_CONFIDENCES:
        return None
    ordered_domains = tuple(domain for domain in _TURN_CLASSIFICATION_DOMAINS if domain in domains)
    return CodexHookTurnClassification(
        intent=str(intent),
        domains=ordered_domains,
        risk=str(risk),
        source=str(source),
        confidence=str(confidence),
    )


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


def _non_negative_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value)


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
    "CODEX_HOOK_TASK_CONTEXT_FILE_NAME",
    "CODEX_HOOK_TASK_CONTEXT_SCHEMA_VERSION",
    "CODEX_SESSION_CACHE_FILE_NAME",
    "CODEX_SESSION_CACHE_SCHEMA_VERSION",
    "CODEX_SESSION_HISTORY_SCHEMA_VERSION",
    "HISTORY_DIR_NAME",
    "HISTORY_FILE_NAME",
    "STRONG_ATTEMPT_RELATIONSHIPS",
    "CodexAttemptRelationship",
    "CodexHookTaskContextStamp",
    "CodexHookTurnClassification",
    "CodexRuntimeContext",
    "CodexSession",
    "CodexSessionError",
    "CodexTurn",
    "EnvironmentIssueEvidence",
    "build_codex_coverage",
    "build_codex_history",
    "classify_environment_issue_text",
    "classify_user_message",
    "codex_home",
    "codex_project_roots",
    "codex_session_cache_path",
    "codex_task_context_path",
    "collect_codex_sessions",
    "collect_codex_turns",
    "current_codex_runtime_context",
    "current_codex_session_ref",
    "history_export_path",
    "load_codex_task_context_stamps",
    "project_codex_turns",
    "read_codex_config",
    "read_codex_session",
    "render_codex_coverage_text",
    "render_codex_history_text",
]
