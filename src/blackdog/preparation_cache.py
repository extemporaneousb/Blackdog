"""Repository-local, immutable file bundles; never shared task environments.

A per-key lock serializes builders. Only an atomically published pointer makes
a verified generation consumable. Failed candidates and replaced generations
are retained, never repaired in place. Recipes and the local user are trusted;
read-only modes detect accidents, not hostile writes by the owning user.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
from typing import Any, Callable, Iterator
import uuid

from blackdog.preparation_files import PreparationError, _bounded_entry, _digest, _file
from blackdog_core.state import atomic_write_text, _fsync_directory


def _directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise PreparationError("artifact directory is missing or is a symlink")


def _walk_error(error: OSError) -> None:
    raise error


def inventory(root: Path, *, immutable: bool = False) -> dict[str, Any]:
    """Hash a bounded, self-contained tree, rejecting links and special files."""
    _directory(root)
    rows: dict[str, dict[str, Any]] = {}
    total = 0
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=_walk_error):
        for path in [Path(directory), *(Path(directory) / name for name in sorted(files))]:
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                value = {"directory": True, "mode": mode}
                expected_modes = {0o755}
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                value = _file(path)
                expected_modes = {0o444, 0o555}
            else:
                raise PreparationError("artifacts require regular files and directories without links")
            if mode & 0o7000 or (immutable and mode not in expected_modes):
                raise PreparationError("artifact permissions are not immutable")
            total = _bounded_entry(rows, str(path.relative_to(root)), value, total)
        for name in dirs:
            _directory(Path(directory) / name)
    return {"sha256": _digest(rows), "entries": len(rows), "bytes": total}


def _freeze(root: Path) -> None:
    # Inventory must precede chmod: never follow a link or chmod a hardlink.
    inventory(root)
    for directory, dirs, files in os.walk(root, topdown=False, onerror=_walk_error):
        for name in files:
            path = Path(directory) / name
            path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        # Immutability is the generation protocol plus verified content, not a
        # filesystem seal. Writable directories permit ordinary owned-worktree
        # and repository unbind cleanup; files remain read-only accident guards.
        Path(directory).chmod(0o755)
        _fsync_directory(Path(directory))


@contextmanager
def _key_lock(root: Path, deadline: float) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    _directory(root)
    lock = root / "publication.lock"
    if lock.is_symlink():
        raise PreparationError("artifact publication lock is a symlink")
    with lock.open("a+") as stream:
        while True:
            if time.monotonic() >= deadline:
                raise PreparationError("artifact publication lock budget expired")
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise PreparationError("artifact publication evidence is missing or invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PreparationError("artifact publication evidence must be an object")
    return value


def validate_reference(value: Any, input_sha256: str) -> None:
    """Reject incomplete/corrupt cache or retained-task artifact evidence."""
    if not isinstance(value, dict):
        raise PreparationError("artifact reference must be an object")
    artifact = value.get("artifact")
    if (type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("input_sha256") != input_sha256
            or not isinstance(value.get("generation"), str)
            or re.fullmatch(r"[0-9a-f]{32}", value["generation"]) is None
            or not isinstance(artifact, dict) or set(artifact) != {"sha256", "entries", "bytes"}
            or not isinstance(artifact.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is None
            or type(artifact.get("entries")) is not int or artifact["entries"] < 1
            or type(artifact.get("bytes")) is not int or artifact["bytes"] < 0):
        raise PreparationError("artifact reference has invalid identity or inventory")


def _published(root: Path, identity: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    pointer = _json(root / "current.json")
    if set(pointer) != {"schema_version", "input_sha256", "generation", "artifact"}:
        raise PreparationError("artifact publication fields differ")
    validate_reference(pointer, _digest(identity))
    generation = root / "generations" / pointer["generation"]
    _directory(root / "generations")
    _directory(generation)
    if _json(generation / "manifest.json") != {**pointer, "identity": identity}:
        raise PreparationError("artifact manifest does not match its publication")
    payload = generation / "payload"
    if inventory(payload, immutable=True) != pointer["artifact"]:
        raise PreparationError("published artifact contents changed")
    return payload, pointer


def acquire(
    cache: Path, identity: dict[str, Any], *, build: Callable[[Path], None], deadline: float,
) -> tuple[Path, dict[str, Any], str]:
    """Return a verified generation, rebuilding invalid candidates in new state."""
    cache.mkdir(parents=True, exist_ok=True)
    _directory(cache)
    key = _digest(identity)
    root = cache / key
    with _key_lock(root, deadline):
        disposition = "built"
        if (root / "current.json").exists() or (root / "current.json").is_symlink():
            try:
                payload, pointer = _published(root, identity)
            except (PreparationError, FileNotFoundError, json.JSONDecodeError, UnicodeError):
                disposition = "rebuilt"
            else:
                return payload, pointer, "reused"
        for name in ("candidates", "generations"):
            (root / name).mkdir(exist_ok=True)
            _directory(root / name)
        generation_id = uuid.uuid4().hex
        candidate = root / "candidates" / generation_id
        candidate.mkdir(mode=0o700)
        atomic_write_text(candidate / "intent.json", json.dumps({"schema_version": 1, "identity": identity}, sort_keys=True) + "\n")
        payload = candidate / "payload"
        payload.mkdir()
        build(payload)
        _freeze(payload)
        pointer = {"schema_version": 1, "input_sha256": key, "generation": generation_id,
                   "artifact": inventory(payload, immutable=True)}
        atomic_write_text(candidate / "manifest.json", json.dumps({**pointer, "identity": identity}, sort_keys=True) + "\n")
        generation = root / "generations" / generation_id
        os.rename(candidate, generation)
        _fsync_directory(root / "generations")
        _fsync_directory(root / "candidates")
        atomic_write_text(root / "current.json", json.dumps(pointer, sort_keys=True) + "\n")
        payload, pointer = _published(root, identity)
        return payload, pointer, disposition


def materialize(payload: Path, destination: Path, expected: dict[str, Any]) -> None:
    """Copy bytes, never links, into a fresh task-owned destination and verify."""
    if inventory(payload, immutable=True) != expected:
        raise PreparationError("artifact changed before materialization")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(payload, destination, symlinks=True)
    if inventory(destination, immutable=True) != expected or inventory(payload, immutable=True) != expected:
        raise PreparationError("artifact changed during materialization")
