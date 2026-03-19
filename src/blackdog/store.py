from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator
import fcntl
import json
import os
import tempfile
import uuid

from .config import ProjectPaths


class StoreError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "approval_tasks": {},
        "task_claims": {},
    }


def normalize_state(payload: dict[str, Any], *, state_file: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StoreError(f"State file must be a JSON object: {state_file}")
    payload.setdefault("schema_version", 1)
    payload.setdefault("approval_tasks", {})
    payload.setdefault("task_claims", {})
    if not isinstance(payload["approval_tasks"], dict):
        raise StoreError(f"approval_tasks must be an object in {state_file}")
    if not isinstance(payload["task_claims"], dict):
        raise StoreError(f"task_claims must be an object in {state_file}")
    return payload


def load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return default_state()
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreError(f"Invalid JSON in {state_file}: {exc}") from exc
    return normalize_state(payload, state_file=state_file)


def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextmanager
def locked_path(path: Path) -> Iterator[None]:
    lock_path = _lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    before_replace: Callable[[Path], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace(temp_path)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    with locked_path(state_file):
        atomic_write_text(state_file, payload)


@contextmanager
def locked_state(state_file: Path) -> Iterator[dict[str, Any]]:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with locked_path(state_file):
        if not state_file.exists():
            atomic_write_text(state_file, json.dumps(default_state(), indent=2, sort_keys=True) + "\n")
        raw = state_file.read_text(encoding="utf-8").strip()
        payload = default_state() if not raw else json.loads(raw)
        state = normalize_state(payload, state_file=state_file)
        yield state
        atomic_write_text(state_file, json.dumps(state, indent=2, sort_keys=True) + "\n")


def claim_is_active(entry: dict[str, Any]) -> bool:
    return isinstance(entry, dict) and entry.get("status") == "claimed"


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    row = json.dumps(payload, sort_keys=True) + "\n"
    with locked_path(path):
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        atomic_write_text(path, existing + row)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StoreError(f"Invalid JSONL in {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise StoreError(f"JSONL row must be an object in {path}")
        rows.append(payload)
    return rows


def append_event(
    paths: ProjectPaths,
    *,
    event_type: str,
    actor: str,
    task_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "event_id": uuid.uuid4().hex,
        "type": event_type,
        "at": now_iso(),
        "actor": actor,
        "task_id": task_id,
        "payload": payload or {},
    }
    append_jsonl(paths.events_file, event)
    return event


def record_comment(
    paths: ProjectPaths,
    *,
    actor: str,
    body: str,
    task_id: str | None = None,
    kind: str = "comment",
) -> dict[str, Any]:
    return append_event(
        paths,
        event_type="comment",
        actor=actor,
        task_id=task_id,
        payload={"kind": kind, "body": body},
    )


def load_events(
    paths: ProjectPaths,
    *,
    task_id: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    rows = load_jsonl(paths.events_file)
    if task_id:
        rows = [row for row in rows if row.get("task_id") == task_id]
    rows.sort(key=lambda row: str(row.get("at") or ""))
    if limit is not None:
        rows = rows[-limit:]
    return rows


def send_message(
    paths: ProjectPaths,
    *,
    sender: str,
    recipient: str,
    body: str,
    kind: str,
    task_id: str | None = None,
    reply_to: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    message = {
        "action": "message",
        "message_id": uuid.uuid4().hex,
        "at": now_iso(),
        "sender": sender,
        "recipient": recipient,
        "kind": kind,
        "task_id": task_id,
        "reply_to": reply_to,
        "tags": tags or [],
        "body": body,
    }
    append_jsonl(paths.inbox_file, message)
    append_event(
        paths,
        event_type="message",
        actor=sender,
        task_id=task_id,
        payload={"message_id": message["message_id"], "recipient": recipient, "kind": kind},
    )
    return message


def resolve_message(
    paths: ProjectPaths,
    *,
    message_id: str,
    actor: str,
    note: str = "",
) -> dict[str, Any]:
    row = {
        "action": "resolve",
        "message_id": message_id,
        "at": now_iso(),
        "actor": actor,
        "note": note,
    }
    append_jsonl(paths.inbox_file, row)
    append_event(
        paths,
        event_type="message_resolved",
        actor=actor,
        payload={"message_id": message_id, "note": note},
    )
    return row


def load_inbox(
    paths: ProjectPaths,
    *,
    recipient: str | None = None,
    status: str | None = None,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    rows = load_jsonl(paths.inbox_file)
    messages: dict[str, dict[str, Any]] = {}
    for row in rows:
        action = str(row.get("action") or "")
        if action == "message":
            messages[str(row["message_id"])] = {
                **row,
                "status": "open",
                "resolved_at": None,
                "resolved_by": None,
                "resolution_note": None,
            }
        elif action == "resolve":
            message = messages.get(str(row["message_id"]))
            if message is None:
                continue
            message["status"] = "resolved"
            message["resolved_at"] = row.get("at")
            message["resolved_by"] = row.get("actor")
            message["resolution_note"] = row.get("note")
    output = list(messages.values())
    if recipient:
        output = [row for row in output if row.get("recipient") == recipient]
    if status:
        output = [row for row in output if row.get("status") == status]
    if task_id:
        output = [row for row in output if row.get("task_id") == task_id]
    output.sort(key=lambda row: str(row.get("at") or ""), reverse=True)
    return output


def record_task_result(
    paths: ProjectPaths,
    *,
    task_id: str,
    actor: str,
    status: str,
    what_changed: list[str],
    validation: list[str],
    residual: list[str],
    needs_user_input: bool,
    followup_candidates: list[str],
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    result_dir = paths.results_dir / task_id
    result_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    safe_run = run_id or uuid.uuid4().hex[:8]
    result_path = result_dir / f"{timestamp}-{safe_run}.json"
    payload = {
        "schema_version": 1,
        "task_id": task_id,
        "recorded_at": now_iso(),
        "actor": actor,
        "run_id": safe_run,
        "status": status,
        "what_changed": what_changed,
        "validation": validation,
        "residual": residual,
        "needs_user_input": needs_user_input,
        "followup_candidates": followup_candidates,
        "metadata": metadata or {},
    }
    atomic_write_text(result_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    append_event(
        paths,
        event_type="task_result",
        actor=actor,
        task_id=task_id,
        payload={
            "status": status,
            "run_id": safe_run,
            "result_file": str(result_path),
            "needs_user_input": needs_user_input,
        },
    )
    return result_path


def load_task_results(paths: ProjectPaths, *, task_id: str | None = None) -> list[dict[str, Any]]:
    if not paths.results_dir.exists():
        return []
    files: list[Path] = []
    if task_id:
        task_dir = paths.results_dir / task_id
        files = sorted(task_dir.glob("*.json")) if task_dir.exists() else []
    else:
        files = sorted(paths.results_dir.glob("*/*.json"))
    results: list[dict[str, Any]] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"Invalid task-result JSON {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise StoreError(f"Task-result payload must be an object: {path}")
        payload["result_file"] = str(path)
        results.append(payload)
    results.sort(key=lambda row: str(row.get("recorded_at") or ""), reverse=True)
    return results


def claim_task_entry(
    entry: dict[str, Any],
    *,
    agent: str,
    title: str,
    summary: dict[str, Any],
    claimed_pid: int | None = None,
) -> None:
    claimed_at = now_iso()
    entry.update(
        {
            "status": "claimed",
            "title": title,
            "claimed_by": agent,
            "claimed_at": claimed_at,
            **summary,
        }
    )
    entry.pop("claim_expires_at", None)
    if claimed_pid is None:
        entry.pop("claimed_pid", None)
        entry.pop("claimed_process_missing_scans", None)
        entry.pop("claimed_process_last_seen_at", None)
        entry.pop("claimed_process_last_checked_at", None)
        return
    entry["claimed_pid"] = claimed_pid
    entry["claimed_process_missing_scans"] = 0
    entry["claimed_process_last_seen_at"] = claimed_at
    entry.pop("claimed_process_last_checked_at", None)
