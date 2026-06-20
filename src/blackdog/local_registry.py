"""User-local registry of Blackdog repos."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
import json

from blackdog.repo_lifecycle import RepoLifecycleError
from blackdog_core.profile import ConfigError, load_profile
from blackdog_core.state import atomic_write_text, now_iso
from blackdog_core.user_state import user_state_file


LOCAL_REPO_REGISTRY_SCHEMA_VERSION = 1
LOCAL_REPO_REGISTRY_FILE_NAME = "local-repos.json"


@dataclass(frozen=True, slots=True)
class LocalRepoRegistryResult:
    action: str
    registry_path: str
    rows: tuple[dict[str, object], ...]
    changed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "registry_path": self.registry_path,
            "changed": self.changed,
            "rows": [dict(row) for row in self.rows],
        }


def local_repo_registry_path() -> Path:
    return user_state_file(LOCAL_REPO_REGISTRY_FILE_NAME)


def add_local_repo(project_root: Path) -> LocalRepoRegistryResult:
    row = _registry_row_for_project(project_root)
    rows = list(_load_registry_rows())
    rows_by_root = {str(item["project_root"]): dict(item) for item in rows}
    existing = rows_by_root.get(str(row["project_root"]), {})
    changed = (
        not existing
        or existing.get("project_name") != row["project_name"]
        or existing.get("status") != row["status"]
    )
    rows_by_root[str(row["project_root"])] = {
        **existing,
        **row,
        "added_at": existing.get("added_at") or row["added_at"],
        "updated_at": row["updated_at"] if changed else existing.get("updated_at", row["updated_at"]),
    }
    result_rows = tuple(sorted(rows_by_root.values(), key=lambda item: str(item["project_root"])))
    if changed:
        _write_registry_rows(result_rows)
    return LocalRepoRegistryResult(
        action="add",
        registry_path=str(local_repo_registry_path()),
        rows=result_rows,
        changed=changed,
    )


def remove_local_repo(project_root: Path) -> LocalRepoRegistryResult:
    target = str(project_root.expanduser().resolve())
    existing_rows = _load_registry_rows()
    rows = tuple(row for row in existing_rows if row.get("project_root") != target)
    changed = len(rows) != len(existing_rows)
    if changed:
        _write_registry_rows(rows)
    return LocalRepoRegistryResult(
        action="remove",
        registry_path=str(local_repo_registry_path()),
        rows=rows,
        changed=changed,
    )


def list_local_repos() -> LocalRepoRegistryResult:
    return LocalRepoRegistryResult(
        action="list",
        registry_path=str(local_repo_registry_path()),
        rows=_load_registry_rows(),
        changed=False,
    )


def registered_project_roots() -> tuple[Path, ...]:
    return tuple(Path(str(row["project_root"])) for row in _load_registry_rows())


def render_local_repo_registry_text(result: LocalRepoRegistryResult) -> str:
    columns = ("project_name", "project_root", "status", "added_at", "updated_at")
    lines = ["\t".join(columns)]
    for row in result.rows:
        lines.append("\t".join(_tsv_value(row.get(column)) for column in columns))
    return "\n".join(lines) + "\n"


def _registry_row_for_project(project_root: Path) -> dict[str, object]:
    try:
        profile = load_profile(project_root.resolve())
    except ConfigError as exc:
        raise RepoLifecycleError(f"{project_root.resolve()} is not a Blackdog repo: {exc}") from exc
    now = now_iso()
    return {
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "status": profile.status,
        "added_at": now,
        "updated_at": now,
    }


def _load_registry_rows() -> tuple[dict[str, object], ...]:
    path = local_repo_registry_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ()
    if not isinstance(payload, Mapping):
        return ()
    if payload.get("schema_version") != LOCAL_REPO_REGISTRY_SCHEMA_VERSION:
        return ()
    rows = payload.get("repos")
    if not isinstance(rows, list):
        return ()
    normalized: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        root = str(row.get("project_root") or "").strip()
        if not root:
            continue
        normalized[str(Path(root).expanduser().resolve())] = {
            "project_name": str(row.get("project_name") or Path(root).name),
            "project_root": str(Path(root).expanduser().resolve()),
            "status": str(row.get("status") or "active"),
            "added_at": str(row.get("added_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }
    return tuple(sorted(normalized.values(), key=lambda item: str(item["project_root"])))


def _write_registry_rows(rows: tuple[dict[str, object], ...]) -> None:
    payload = {
        "schema_version": LOCAL_REPO_REGISTRY_SCHEMA_VERSION,
        "updated_at": now_iso(),
        "repos": list(rows),
    }
    atomic_write_text(local_repo_registry_path(), json.dumps(payload, sort_keys=True) + "\n")


def _tsv_value(value: object) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


__all__ = [
    "LOCAL_REPO_REGISTRY_FILE_NAME",
    "LOCAL_REPO_REGISTRY_SCHEMA_VERSION",
    "LocalRepoRegistryResult",
    "add_local_repo",
    "list_local_repos",
    "local_repo_registry_path",
    "registered_project_roots",
    "remove_local_repo",
    "render_local_repo_registry_text",
]
