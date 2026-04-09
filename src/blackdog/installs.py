from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from blackdog_core.profile import BlackdogPaths
from blackdog_core.state import StoreError, atomic_write_text


def tracked_installs_file(paths: BlackdogPaths) -> Path:
    return paths.control_dir / "tracked-installs.json"


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


def load_tracked_installs(paths: BlackdogPaths) -> dict[str, Any]:
    installs_file = tracked_installs_file(paths)
    if not installs_file.exists():
        return default_tracked_installs()
    try:
        payload = json.loads(installs_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreError(f"Invalid JSON in {installs_file}: {exc}") from exc
    return normalize_tracked_installs(payload, installs_file=installs_file)


def save_tracked_installs(paths: BlackdogPaths, payload: dict[str, Any]) -> Path:
    installs_file = tracked_installs_file(paths)
    normalized = normalize_tracked_installs(dict(payload), installs_file=installs_file)
    atomic_write_text(installs_file, json.dumps(normalized, indent=2, sort_keys=True) + "\n")
    return installs_file
