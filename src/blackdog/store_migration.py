"""Explicit, resumable migration of quiescent legacy stores.

Legacy interpretation is isolated here. Normal runtime reads remain schema-4
only; original bytes and identity mappings remain in a private migration archive.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from blackdog_core.profile import load_profile
from blackdog_core.state import (
    JsonRuntimeStore, StoreError, atomic_write_text, exclusive_file_lock,
    runtime_state_from_payload,
)

_FILES = ("runtime.json", "planning.json", "events.jsonl")
_PENDING = "migration-pending.json"


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise StoreError(f"Migration requires a regular file: {path.name}")
    return path.read_bytes()


def _parse(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise StoreError("Invalid migration JSON") from exc
    if not isinstance(value, dict):
        raise StoreError("Migration JSON must be an object")
    return value


def _index(rows: Any, key: str) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise StoreError("Legacy records must be a list")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(key), str) or not row[key]:
            raise StoreError("Invalid legacy record identity")
        if row[key] in result:
            raise StoreError("Duplicate legacy record identity")
        result[row[key]] = row
    return result


def _quiescent_git(root: Path) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise StoreError("Unable to inspect repository worktrees")
    if sum(line.startswith("worktree ") for line in result.stdout.splitlines()) != 1:
        raise StoreError("Migration requires no linked worktrees; preserve and finish retained work first")


def _convert(raw: dict[str, bytes], control: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    runtime, planning = (_parse(raw[name]) for name in _FILES[:2])
    if (runtime.get("schema_version"), runtime.get("store_version")) != (3, "blackdog.runtime/vnext3"):
        raise StoreError("This migration supports only runtime schema 3")
    if (planning.get("schema_version"), planning.get("store_version")) != (1, "blackdog.planning/vnext1"):
        raise StoreError("This migration requires the matching legacy planning store")
    groups = _index(runtime.get("worksets"), "id")
    plans = _index(planning.get("worksets"), "id")
    if groups.keys() != plans.keys():
        raise StoreError("Legacy planning/runtime group identities disagree")
    tasks, mappings = [], []
    for group_id, group in groups.items():
        if group.get("workset_claim") is not None or group.get("task_claims") != []:
            raise StoreError("Legacy claims must be released by their owner before migration")
        states = _index(group.get("task_states"), "task_id")
        descriptions = _index(plans[group_id].get("tasks"), "id")
        if states.keys() != descriptions.keys():
            raise StoreError("Every legacy task needs an explicit matching terminal state")
        attempts = _index(group.get("attempts"), "attempt_id")
        if any(a.get("task_id") not in states for a in attempts.values()):
            raise StoreError("Legacy attempt has no matching task")
        for old_id, state in states.items():
            if state.get("status") not in {"done", "canceled"}:
                raise StoreError("Migration requires terminal tasks; finish or cancel existing work with the previous runtime")
            task_id = "task-" + _hash(_json([group_id, old_id]).encode())[:32]
            converted = []
            for old_attempt_id, attempt in attempts.items():
                if attempt["task_id"] != old_id:
                    continue
                if attempt.get("status") == "in_progress":
                    raise StoreError("An in-progress attempt prevents migration")
                path = attempt.get("worktree_path")
                if path and (Path(path).exists() or Path(path).is_symlink()):
                    raise StoreError("A retained attempt workspace prevents migration; preserve and finish its recovery first")
                item = dict(attempt)
                item.pop("task_id")
                item["attempt_id"] = "attempt-" + _hash(_json([group_id, old_attempt_id]).encode())[:32]
                if item.get("codex_session") is not None:
                    session = dict(item["codex_session"])
                    capture = session.pop("capture", None)
                    if capture is not None:
                        if not isinstance(capture, dict) or set(capture) != {"status", "method", "missing_reason"}:
                            raise StoreError("Unrecognized legacy session capture")
                        session.update({"capture_" + key: value for key, value in capture.items()})
                    item["codex_session"] = session
                mappings.append({"legacy_group": group_id, "legacy_attempt": old_attempt_id, "attempt_id": item["attempt_id"]})
                converted.append(item)
            item = {key: value for key, value in state.items() if key != "task_id"}
            item.update(id=task_id, title=descriptions[old_id]["title"], attempts=converted)
            item["created_at"] = converted[0]["started_at"] if converted else state.get("updated_at")
            tasks.append(item)
            mappings.append({"legacy_group": group_id, "legacy_task": old_id, "task_id": task_id})
    payload = {"schema_version": 4, "store_version": "blackdog.runtime/v4", "tasks": tasks}
    runtime_state_from_payload(payload, source=control / "runtime.json")
    return payload, mappings


def _archive_write(path: Path, raw: bytes) -> None:
    if path.exists():
        if _read(path) != raw:
            raise StoreError("Migration archive conflicts with the expected bytes")
        return
    atomic_write_text(path, raw.decode("utf-8"))


def _durable_unlink(path: Path) -> None:
    path.unlink()
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _result(root: Path, digest: str, payload: dict[str, Any], status: str, archive: Path) -> dict[str, Any]:
    argv = [str(root / ".VE/bin/blackdog"), "repo", "migrate", "--project-root", str(root), "--apply", "--expected-digest", digest, "--json"]
    return {
        "status": status, "source_digest": digest, "archive_path": str(archive),
        "task_count": len(payload["tasks"]),
        "attempt_count": sum(len(task["attempts"]) for task in payload["tasks"]),
        "next_action": {"kind": "complete" if status == "applied" else "command", "argv": [] if status == "applied" else argv},
    }


def migrate_store(root: Path, *, apply: bool = False, expected_digest: str | None = None) -> dict[str, Any]:
    """Preview without target writes; apply only the exact reviewed snapshot.

    A durable pending marker blocks all ordinary runtime readers across a crash.
    Replaying the emitted apply command verifies originals and every allowed
    intermediate file before finishing publication. History is never relabeled.
    """
    profile = load_profile(root)
    control = profile.paths.control_dir
    pending = control / _PENDING
    if not control.exists():
        raise StoreError("No existing store to migrate")
    with ExitStack() as locks:
        if apply:
            locks.enter_context(exclusive_file_lock(profile.paths.runtime_file))
            locks.enter_context(exclusive_file_lock(profile.paths.events_file))
        if pending.exists():
            journal = _parse(_read(pending))
            digest = journal.get("source_digest")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise StoreError("Invalid migration journal")
            archive = control / "migrations" / digest
            if archive.is_symlink() or archive.parent.is_symlink():
                raise StoreError("Migration archive directories must not be symbolic links")
            manifest = _parse(_read(archive / "manifest.json"))
            raw = {name: _read(archive / name) for name in _FILES}
        else:
            current = _parse(_read(profile.paths.runtime_file))
            if current.get("schema_version") == 4:
                JsonRuntimeStore().load(profile.paths.runtime_file)
                return {"status": "current", "next_action": {"kind": "complete", "argv": []}}
            raw = {name: _read(control / name) for name in _FILES}
            manifest = {name: _hash(value) for name, value in raw.items()}
            digest = _hash(_json(manifest).encode())
            archive = control / "migrations" / digest
        _quiescent_git(root)
        if archive.is_symlink() or archive.parent.is_symlink():
            raise StoreError("Migration archive directories must not be symbolic links")
        if {name: _hash(value) for name, value in raw.items()} != manifest or _hash(_json(manifest).encode()) != digest:
            raise StoreError("Migration archive digest mismatch")
        payload, mappings = _convert(raw, control)
        target = _json(payload).encode()
        event = _json({
            "event_id": "store-migration-" + digest, "type": "store_migrated", "actor": "migration",
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "payload": {"source_digest": digest, "task_count": len(payload["tasks"]), "archive": str(archive.relative_to(control))},
        })
        # Event timestamps describe the cutover, never a historical task event.
        if pending.exists():
            event = _read(archive / "target-events.jsonl").decode()
            if _hash(event.encode()) != journal.get("events_sha256") or _hash(target) != journal.get("runtime_sha256"):
                raise StoreError("Migration publication digest mismatch")
            if _read(archive / "identity-map.json") != _json(mappings).encode():
                raise StoreError("Migration identity map mismatch")
        else:
            event = json.dumps(json.loads(event), sort_keys=True) + "\n"
        event_bytes = event.encode()
        for name in _FILES:
            path = control / name
            allowed = [raw[name]]
            if pending.exists():
                if name == "runtime.json": allowed.append(target)
                if name == "events.jsonl": allowed.append(event_bytes)
                if name == "planning.json" and not path.exists(): continue
            if _read(path) not in allowed:
                raise StoreError("Store changed after migration preview; no further writes are permitted")
        if not apply:
            return _result(root, digest, payload, "pending" if pending.exists() else "preview", archive)
        if expected_digest != digest:
            raise StoreError("Apply requires the exact --expected-digest from a fresh preview")
        if not pending.exists():
            archive.mkdir(parents=True, exist_ok=True, mode=0o700)
            for name, value in raw.items(): _archive_write(archive / name, value)
            _archive_write(archive / "manifest.json", _json(manifest).encode())
            _archive_write(archive / "identity-map.json", _json(mappings).encode())
            atomic_write_text(archive / "target-events.jsonl", event)
            atomic_write_text(pending, _json({
                "source_digest": digest, "events_sha256": _hash(event_bytes),
                "runtime_sha256": _hash(target),
            }))
        atomic_write_text(profile.paths.events_file, event)
        atomic_write_text(profile.paths.runtime_file, target.decode())
        planning_path = control / "planning.json"
        if planning_path.exists():
            _durable_unlink(planning_path)
        _durable_unlink(pending)
        JsonRuntimeStore().load(profile.paths.runtime_file)
        return _result(root, digest, payload, "applied", archive)
