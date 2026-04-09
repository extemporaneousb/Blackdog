"""Blackdog-owned conversation thread storage.

These thread artifacts are part of the Blackdog product layer for
prompt/task workflows. They are distinct from external client chat or
session stores such as Codex transcripts.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import json
import uuid

from blackdog_core.profile import BlackdogPaths
from blackdog_core.state import (
    StoreError,
    append_event,
    append_jsonl,
    atomic_write_text,
    load_jsonl,
    locked_path,
    now_iso,
)


THREAD_SCHEMA_VERSION = 1
THREAD_ENTRY_SCHEMA_VERSION = 1
THREAD_ROLES = frozenset({"user", "assistant", "system"})


def _thread_dir(paths: BlackdogPaths, thread_id: str) -> Path:
    return paths.threads_dir / thread_id


def _thread_file(paths: BlackdogPaths, thread_id: str) -> Path:
    return _thread_dir(paths, thread_id) / "thread.json"


def _thread_entries_file(paths: BlackdogPaths, thread_id: str) -> Path:
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


def _thread_summary(paths: BlackdogPaths, metadata: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
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


def load_thread(paths: BlackdogPaths, thread_id: str) -> dict[str, Any]:
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


def list_threads(paths: BlackdogPaths, *, task_id: str | None = None) -> list[dict[str, Any]]:
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
    paths: BlackdogPaths,
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
    paths: BlackdogPaths,
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
    paths: BlackdogPaths,
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


def thread_ids_for_task(paths: BlackdogPaths, task_id: str) -> list[str]:
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


def mirror_task_result_to_threads(
    paths: BlackdogPaths,
    *,
    task_id: str,
    actor: str,
    status: str,
    what_changed: list[str],
    validation: list[str],
    residual: list[str],
    needs_user_input: bool,
    result_path: Path,
    run_id: str,
    task_shaping_telemetry: dict[str, Any] | None = None,
) -> list[str]:
    linked_thread_ids = thread_ids_for_task(paths, task_id)
    for linked_thread_id in linked_thread_ids:
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
                "run_id": run_id,
            },
        )
    return linked_thread_ids
