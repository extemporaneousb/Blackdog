"""Bounded identity observations shared by preparation and artifact publication."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
from typing import Any


MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOOL_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_FILES = 30000


class PreparationError(RuntimeError):
    """A declared preparation requirement could not be verified."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _file(path: Path, *, max_bytes: int = MAX_FILE_BYTES) -> dict[str, Any]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        raise PreparationError("preparation inputs must be bounded regular files")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise PreparationError("preparation file exceeds its size bound")
            digest.update(chunk)
    after = path.stat()
    if (info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise PreparationError("preparation file changed while being observed")
    return {"sha256": digest.hexdigest(), "size": size, "mode": stat.S_IMODE(after.st_mode)}


def _bounded_entry(rows: dict[str, dict[str, Any]], relative: str, value: dict[str, Any], total: int) -> int:
    total += int(value.get("size", 0))
    if len(rows) >= MAX_SNAPSHOT_FILES or total > MAX_SNAPSHOT_BYTES:
        raise PreparationError("preparation snapshot exceeds supported file or byte bounds")
    rows[relative] = value
    return total
