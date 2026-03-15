from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
import fcntl
import json
import uuid

from .config import ProjectPaths


class StoreError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


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


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@contextmanager
def locked_state(state_file: Path) -> Iterator[dict[str, Any]]:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if not state_file.exists():
        save_state(state_file, default_state())
    with state_file.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            raw = handle.read().strip()
            payload = default_state() if not raw else json.loads(raw)
            state = normalize_state(payload, state_file=state_file)
            yield state
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def claim_is_active(entry: dict[str, Any]) -> bool:
    if not isinstance(entry, dict) or entry.get("status") != "claimed":
        return False
    expires = parse_datetime(entry.get("claim_expires_at"))
    if expires is None:
        return True
    return expires > datetime.now().astimezone()


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


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
    }
    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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


def claim_task_entry(entry: dict[str, Any], *, agent: str, lease_hours: int, title: str, summary: dict[str, Any]) -> None:
    claimed_at = now_iso()
    claim_expires_at = (datetime.now().astimezone() + timedelta(hours=lease_hours)).isoformat(timespec="seconds")
    entry.update(
        {
            "status": "claimed",
            "title": title,
            "claimed_by": agent,
            "claimed_at": claimed_at,
            "claim_expires_at": claim_expires_at,
            **summary,
        }
    )
