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


def _tracked_installs_file(paths: ProjectPaths) -> Path:
    return paths.control_dir / "tracked-installs.json"


THREAD_SCHEMA_VERSION = 1
THREAD_ENTRY_SCHEMA_VERSION = 1
THREAD_ROLES = frozenset({"user", "assistant", "system"})


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "approval_tasks": {},
        "task_claims": {},
    }


def default_tracked_installs() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "repos": [],
    }


def normalize_tracked_installs(payload: dict[str, Any], *, installs_file: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StoreError(f"Tracked installs file must be a JSON object: {installs_file}")
    payload.setdefault("schema_version", 1)
    payload.setdefault("repos", [])
    repos = payload.get("repos")
    if not isinstance(repos, list):
        raise StoreError(f"repos must be a list in {installs_file}")
    normalized: list[dict[str, Any]] = []
    seen_roots: set[str] = set()
    for row in repos:
        if not isinstance(row, dict):
            raise StoreError(f"Tracked install rows must be objects in {installs_file}")
        project_root = str(row.get("project_root") or "").strip()
        if not project_root:
            raise StoreError(f"Tracked install rows must include project_root in {installs_file}")
        if project_root in seen_roots:
            continue
        seen_roots.add(project_root)
        normalized.append(
            {
                "project_root": project_root,
                "project_name": str(row.get("project_name") or Path(project_root).name),
                "profile_file": str(row.get("profile_file") or ""),
                "control_dir": str(row.get("control_dir") or ""),
                "blackdog_cli": str(row.get("blackdog_cli") or ""),
                "added_at": str(row.get("added_at") or ""),
                "last_update": row.get("last_update") if isinstance(row.get("last_update"), dict) else {},
                "last_observation": row.get("last_observation") if isinstance(row.get("last_observation"), dict) else {},
            }
        )
    payload["repos"] = sorted(normalized, key=lambda row: (row["project_name"].lower(), row["project_root"]))
    return payload


def load_tracked_installs(paths: ProjectPaths) -> dict[str, Any]:
    installs_file = _tracked_installs_file(paths)
    if not installs_file.exists():
        return default_tracked_installs()
    try:
        payload = json.loads(installs_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreError(f"Invalid JSON in {installs_file}: {exc}") from exc
    return normalize_tracked_installs(payload, installs_file=installs_file)


def save_tracked_installs(paths: ProjectPaths, payload: dict[str, Any]) -> Path:
    installs_file = _tracked_installs_file(paths)
    normalized = normalize_tracked_installs(dict(payload), installs_file=installs_file)
    atomic_write_text(installs_file, json.dumps(normalized, indent=2, sort_keys=True) + "\n")
    return installs_file


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


def _thread_dir(paths: ProjectPaths, thread_id: str) -> Path:
    return paths.threads_dir / thread_id


def _thread_file(paths: ProjectPaths, thread_id: str) -> Path:
    return _thread_dir(paths, thread_id) / "thread.json"


def _thread_entries_file(paths: ProjectPaths, thread_id: str) -> Path:
    return _thread_dir(paths, thread_id) / "entries.jsonl"


def _new_thread_id() -> str:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return f"thread-{timestamp}-{uuid.uuid4().hex[:8]}"


def _unique_thread_task_ids(raw_task_ids: Any) -> list[str]:
    if raw_task_ids in (None, ""):
        return []
    if not isinstance(raw_task_ids, list):
        raise StoreError("thread task_ids must be a list when present")
    task_ids: list[str] = []
    seen: set[str] = set()
    for item in raw_task_ids:
        task_id = str(item or "").strip()
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        task_ids.append(task_id)
    return task_ids


def _normalize_thread_metadata(payload: dict[str, Any], *, thread_file: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StoreError(f"Thread metadata must be an object: {thread_file}")
    thread_id = str(payload.get("thread_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not thread_id:
        raise StoreError(f"thread_id is required in {thread_file}")
    if not title:
        raise StoreError(f"title is required in {thread_file}")
    status = str(payload.get("status") or "open").strip() or "open"
    if status not in {"open", "archived"}:
        raise StoreError(f"Unsupported thread status {status!r} in {thread_file}")
    created_at = str(payload.get("created_at") or "").strip()
    created_by = str(payload.get("created_by") or "").strip()
    if not created_at:
        raise StoreError(f"created_at is required in {thread_file}")
    if not created_by:
        raise StoreError(f"created_by is required in {thread_file}")
    return {
        "schema_version": int(payload.get("schema_version") or THREAD_SCHEMA_VERSION),
        "thread_id": thread_id,
        "title": title,
        "status": status,
        "created_at": created_at,
        "created_by": created_by,
        "task_ids": _unique_thread_task_ids(payload.get("task_ids")),
    }


def _normalize_thread_entry(payload: dict[str, Any], *, thread_id: str, entries_file: Path) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise StoreError(f"Thread entry must be an object in {entries_file}")
    entry_id = str(payload.get("entry_id") or "").strip()
    if not entry_id:
        raise StoreError(f"entry_id is required in {entries_file}")
    payload_thread_id = str(payload.get("thread_id") or "").strip()
    if payload_thread_id != thread_id:
        raise StoreError(f"Thread entry {entry_id} in {entries_file} points at {payload_thread_id!r}, expected {thread_id!r}")
    role = str(payload.get("role") or "").strip()
    if role not in THREAD_ROLES:
        raise StoreError(f"Thread entry {entry_id} in {entries_file} uses unsupported role {role!r}")
    kind = str(payload.get("kind") or "message").strip() or "message"
    actor = str(payload.get("actor") or "").strip()
    body = str(payload.get("body") or "")
    created_at = str(payload.get("created_at") or "").strip()
    if not actor:
        raise StoreError(f"Thread entry {entry_id} in {entries_file} requires actor")
    if not body.strip():
        raise StoreError(f"Thread entry {entry_id} in {entries_file} requires body")
    if not created_at:
        raise StoreError(f"Thread entry {entry_id} in {entries_file} requires created_at")
    duration_seconds = payload.get("duration_seconds")
    if duration_seconds is not None:
        try:
            duration_seconds = int(duration_seconds)
        except (TypeError, ValueError) as exc:
            raise StoreError(f"Thread entry {entry_id} in {entries_file} has invalid duration_seconds") from exc
        if duration_seconds < 0:
            raise StoreError(f"Thread entry {entry_id} in {entries_file} has negative duration_seconds")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return {
        "schema_version": int(payload.get("schema_version") or THREAD_ENTRY_SCHEMA_VERSION),
        "entry_id": entry_id,
        "thread_id": thread_id,
        "role": role,
        "kind": kind,
        "actor": actor,
        "body": body,
        "created_at": created_at,
        "duration_seconds": duration_seconds,
        "task_id": str(payload.get("task_id") or "").strip() or None,
        "metadata": metadata,
    }


def _thread_preview(body: str, *, limit: int = 160) -> str:
    normalized = " ".join(str(body or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "..."


def _thread_summary(paths: ProjectPaths, metadata: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    latest = entries[-1] if entries else None
    return {
        **metadata,
        "updated_at": latest.get("created_at") if latest else metadata["created_at"],
        "entry_count": len(entries),
        "user_entry_count": sum(1 for entry in entries if entry["role"] == "user"),
        "assistant_entry_count": sum(1 for entry in entries if entry["role"] == "assistant"),
        "system_entry_count": sum(1 for entry in entries if entry["role"] == "system"),
        "latest_entry_at": latest.get("created_at") if latest else None,
        "latest_entry_role": latest.get("role") if latest else None,
        "latest_entry_actor": latest.get("actor") if latest else None,
        "latest_entry_preview": _thread_preview(latest["body"]) if latest else "",
        "thread_dir": str(_thread_dir(paths, metadata["thread_id"])),
        "thread_file": str(_thread_file(paths, metadata["thread_id"])),
        "entries_file": str(_thread_entries_file(paths, metadata["thread_id"])),
    }


def load_thread(paths: ProjectPaths, thread_id: str) -> dict[str, Any]:
    thread_file = _thread_file(paths, thread_id)
    if not thread_file.exists():
        raise StoreError(f"Unknown thread: {thread_id}")
    try:
        payload = json.loads(thread_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreError(f"Invalid JSON in {thread_file}: {exc}") from exc
    metadata = _normalize_thread_metadata(payload, thread_file=thread_file)
    entries_file = _thread_entries_file(paths, thread_id)
    entries = [
        _normalize_thread_entry(row, thread_id=thread_id, entries_file=entries_file)
        for row in load_jsonl(entries_file)
    ]
    entries.sort(key=lambda row: str(row.get("created_at") or ""))
    summary = _thread_summary(paths, metadata, entries)
    return {
        **summary,
        "entries": entries,
    }


def list_threads(paths: ProjectPaths, *, task_id: str | None = None) -> list[dict[str, Any]]:
    if not paths.threads_dir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for thread_file in sorted(paths.threads_dir.glob("*/thread.json")):
        thread_id = thread_file.parent.name
        thread = load_thread(paths, thread_id)
        if task_id and task_id not in thread.get("task_ids", []):
            continue
        rows.append({key: value for key, value in thread.items() if key != "entries"})
    rows.sort(
        key=lambda row: (
            str(row.get("updated_at") or ""),
            str(row.get("thread_id") or ""),
        ),
        reverse=True,
    )
    return rows


def create_thread(
    paths: ProjectPaths,
    *,
    title: str,
    actor: str,
    body: str | None = None,
) -> dict[str, Any]:
    title = str(title or "").strip()
    actor = str(actor or "").strip()
    if not title:
        raise StoreError("thread title must be non-empty")
    if not actor:
        raise StoreError("thread actor must be non-empty")
    thread_id = _new_thread_id()
    metadata = {
        "schema_version": THREAD_SCHEMA_VERSION,
        "thread_id": thread_id,
        "title": title,
        "status": "open",
        "created_at": now_iso(),
        "created_by": actor,
        "task_ids": [],
    }
    thread_dir = _thread_dir(paths, thread_id)
    thread_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(_thread_file(paths, thread_id), json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    append_event(
        paths,
        event_type="thread_created",
        actor=actor,
        payload={"thread_id": thread_id, "title": title},
    )
    if body is not None and str(body).strip():
        append_thread_entry(paths, thread_id=thread_id, role="user", actor=actor, body=body)
    return load_thread(paths, thread_id)


def append_thread_entry(
    paths: ProjectPaths,
    *,
    thread_id: str,
    role: str,
    actor: str,
    body: str,
    kind: str = "message",
    task_id: str | None = None,
    duration_seconds: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    thread = load_thread(paths, thread_id)
    role = str(role or "").strip()
    actor = str(actor or "").strip()
    body = str(body or "")
    if role not in THREAD_ROLES:
        raise StoreError(f"Unsupported thread role: {role!r}")
    if not actor:
        raise StoreError("thread entry actor must be non-empty")
    if not body.strip():
        raise StoreError("thread entry body must be non-empty")
    if duration_seconds is not None and int(duration_seconds) < 0:
        raise StoreError("thread entry duration_seconds must be non-negative")
    entry = {
        "schema_version": THREAD_ENTRY_SCHEMA_VERSION,
        "entry_id": uuid.uuid4().hex,
        "thread_id": thread_id,
        "role": role,
        "kind": str(kind or "message").strip() or "message",
        "actor": actor,
        "body": body,
        "created_at": now_iso(),
        "duration_seconds": None if duration_seconds is None else int(duration_seconds),
        "task_id": str(task_id or "").strip() or None,
        "metadata": metadata or {},
    }
    append_jsonl(_thread_entries_file(paths, thread_id), entry)
    append_event(
        paths,
        event_type="thread_entry_added",
        actor=actor,
        task_id=task_id,
        payload={
            "thread_id": thread_id,
            "entry_id": entry["entry_id"],
            "role": role,
            "kind": entry["kind"],
            "thread_title": thread["title"],
        },
    )
    return load_thread(paths, thread_id)


def link_thread_task(
    paths: ProjectPaths,
    *,
    thread_id: str,
    task_id: str,
    actor: str,
) -> dict[str, Any]:
    thread_file = _thread_file(paths, thread_id)
    if not thread_file.exists():
        raise StoreError(f"Unknown thread: {thread_id}")
    task_id = str(task_id or "").strip()
    actor = str(actor or "").strip()
    if not task_id:
        raise StoreError("task_id is required to link a thread")
    if not actor:
        raise StoreError("actor is required to link a thread")
    with locked_path(thread_file):
        try:
            payload = json.loads(thread_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"Invalid JSON in {thread_file}: {exc}") from exc
        metadata = _normalize_thread_metadata(payload, thread_file=thread_file)
        if task_id not in metadata["task_ids"]:
            metadata["task_ids"].append(task_id)
            atomic_write_text(thread_file, json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    append_event(
        paths,
        event_type="thread_task_linked",
        actor=actor,
        task_id=task_id,
        payload={"thread_id": thread_id},
    )
    return load_thread(paths, thread_id)


def thread_ids_for_task(paths: ProjectPaths, task_id: str) -> list[str]:
    task_id = str(task_id or "").strip()
    if not task_id or not paths.threads_dir.exists():
        return []
    thread_ids: list[str] = []
    for thread_file in sorted(paths.threads_dir.glob("*/thread.json")):
        try:
            payload = json.loads(thread_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StoreError(f"Invalid JSON in {thread_file}: {exc}") from exc
        metadata = _normalize_thread_metadata(payload, thread_file=thread_file)
        if task_id in metadata["task_ids"]:
            thread_ids.append(metadata["thread_id"])
    return thread_ids


def _thread_result_body(
    *,
    task_id: str,
    status: str,
    what_changed: list[str],
    validation: list[str],
    residual: list[str],
    needs_user_input: bool,
) -> str:
    lines = [f"Task `{task_id}` recorded `{status}`."]
    if what_changed:
        lines.extend(["", "## What Changed", *[f"- {item}" for item in what_changed]])
    if validation:
        lines.extend(["", "## Validation", *[f"- {item}" for item in validation]])
    if residual:
        lines.extend(["", "## Residual", *[f"- {item}" for item in residual]])
    if needs_user_input:
        lines.extend(["", "## Follow-up", "- User input is still required for this task."])
    return "\n".join(lines).strip()


def _thread_result_duration_seconds(task_shaping_telemetry: dict[str, Any] | None) -> int | None:
    if not isinstance(task_shaping_telemetry, dict):
        return None
    seconds = task_shaping_telemetry.get("actual_task_seconds")
    if seconds is not None:
        try:
            return max(0, int(seconds))
        except (TypeError, ValueError):
            return None
    minutes = task_shaping_telemetry.get("actual_task_minutes")
    if minutes is not None:
        try:
            return max(0, int(minutes) * 60)
        except (TypeError, ValueError):
            return None
    return None


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
    task_shaping_telemetry: dict[str, Any] | None = None,
) -> Path:
    if metadata is not None and not isinstance(metadata, dict):
        raise StoreError("metadata must be an object when present")
    if task_shaping_telemetry is not None and not isinstance(task_shaping_telemetry, dict):
        raise StoreError("task_shaping_telemetry must be an object when present")
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
        "task_shaping_telemetry": task_shaping_telemetry or {},
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
    for linked_thread_id in thread_ids_for_task(paths, task_id):
        append_thread_entry(
            paths,
            thread_id=linked_thread_id,
            role="assistant",
            actor=actor,
            body=_thread_result_body(
                task_id=task_id,
                status=status,
                what_changed=what_changed,
                validation=validation,
                residual=residual,
                needs_user_input=needs_user_input,
            ),
            kind="task_result",
            task_id=task_id,
            duration_seconds=_thread_result_duration_seconds(task_shaping_telemetry),
            metadata={
                "result_file": str(result_path),
                "status": status,
                "run_id": safe_run,
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
