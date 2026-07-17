"""Shared product-layer repository scope selection.

Repository scope is operator/read-model input, not durable task or runtime
state.  This module keeps exact roots, read-only profile discovery, and the
user-local registry explicit while giving fleet surfaces one selection and
deduplication contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tomllib
from typing import Iterable, Mapping

from blackdog.local_registry import registered_project_roots
from blackdog.repo_lifecycle import RepoLifecycleError
from blackdog_core.profile import ConfigError, PROFILE_FILE_NAME, RepoProfile, load_profile


SCOPE_EVIDENCE_LIMIT = 25
REGISTRY_FALLBACK_NOTE = (
    "No repository scope flag was supplied; using the user-local registry for "
    "backward compatibility. Pass --registry to select it explicitly."
)

_DISCOVERY_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".VE",
    ".venv",
    ".worktrees",
    "venv",
    "node_modules",
    "__pycache__",
    ".cache",
    "cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    "coverage",
}


@dataclass(frozen=True, slots=True)
class RepoScope:
    """Resolved repository candidates plus bounded selection evidence."""

    scope_source: str
    supplied_roots: tuple[str, ...]
    project_roots: tuple[Path, ...]
    discovery_roots: tuple[str, ...]
    deduped_project_roots: tuple[str, ...]
    scope_evidence: tuple[dict[str, str], ...]
    scope_notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProfileScopeError:
    candidate_root: str
    message: str


@dataclass(frozen=True, slots=True)
class CanonicalRepoScope:
    """Canonical read-only profile resolution shared by fleet surfaces."""

    scope_source: str
    supplied_roots: tuple[str, ...]
    discovery_roots: tuple[str, ...]
    profiles: tuple[RepoProfile, ...]
    project_roots: tuple[str, ...]
    deduped_project_roots: tuple[str, ...]
    profile_errors: tuple[ProfileScopeError, ...]
    scope_evidence: tuple[dict[str, str], ...]
    scope_notes: tuple[str, ...]

    def metadata(self) -> dict[str, object]:
        return {
            "scope_source": self.scope_source,
            "supplied_roots": list(self.supplied_roots),
            "discovery_roots": list(self.discovery_roots),
            "project_roots": list(self.project_roots),
            "deduped_project_roots": list(self.deduped_project_roots),
            "scope_evidence": [dict(row) for row in self.scope_evidence],
            "scope_notes": list(self.scope_notes),
        }


def resolve_repo_scope(
    *,
    command: str,
    project_roots: tuple[Path, ...] = (),
    discovery_roots: tuple[Path, ...] = (),
    registry: bool = False,
    registry_fallback: bool = False,
    registry_roots: tuple[Path, ...] | None = None,
) -> RepoScope:
    """Resolve one explicit scope mode without mutating repo or registry state."""

    selected_modes = sum((bool(project_roots), bool(discovery_roots), registry))
    if selected_modes > 1:
        raise RepoLifecycleError(
            f"{command} accepts exactly one repository scope: --project-root, --root, or --registry"
        )

    notes: tuple[str, ...] = ()
    selection_evidence: list[Mapping[str, object]] = []
    if project_roots:
        scope_source = "explicit_project_roots"
        supplied = project_roots
        candidates = project_roots
        discovery = ()
    elif discovery_roots:
        scope_source = "discovery_roots"
        supplied = discovery_roots
        discovered: list[Path] = []
        for discovery_root in discovery_roots:
            try:
                discovered.extend(discover_profile_dirs(discovery_root))
            except RepoLifecycleError as exc:
                selection_evidence.append(
                    {
                        "kind": "discovery_root_error",
                        "status": "error",
                        "path": str(discovery_root.expanduser().resolve()),
                        "message": str(exc),
                    }
                )
        candidates = tuple(discovered)
        discovery = tuple(str(root.expanduser().resolve()) for root in discovery_roots)
    elif registry or registry_fallback:
        scope_source = "registry"
        candidates = registered_project_roots() if registry_roots is None else registry_roots
        supplied = candidates
        discovery = ()
        if registry_fallback and not registry:
            notes = (REGISTRY_FALLBACK_NOTE,)
    else:
        raise RepoLifecycleError(
            f"{command} requires one repository scope: --project-root, --root, or --registry"
        )

    unique_roots, deduped_roots, dedupe_evidence = dedupe_project_roots(candidates)
    evidence = bounded_scope_evidence((*selection_evidence, *dedupe_evidence))
    return RepoScope(
        scope_source=scope_source,
        supplied_roots=tuple(str(root.expanduser().resolve()) for root in supplied),
        project_roots=unique_roots,
        discovery_roots=discovery,
        deduped_project_roots=deduped_roots,
        scope_evidence=evidence,
        scope_notes=notes,
    )


def canonicalize_repo_scope(scope: RepoScope) -> CanonicalRepoScope:
    """Load candidates read-only and canonicalize aliases through profile truth."""

    profiles_by_root: dict[Path, RepoProfile] = {}
    candidate_canonical_roots: dict[str, str] = {}
    deduped_aliases = list(scope.deduped_project_roots)
    errors: list[ProfileScopeError] = []
    evidence: list[Mapping[str, object]] = []
    for candidate in scope.project_roots:
        try:
            profile = load_profile(candidate, read_only=True)
        except (ConfigError, OSError, tomllib.TOMLDecodeError) as exc:
            error = ProfileScopeError(candidate_root=str(candidate), message=str(exc))
            errors.append(error)
            evidence.append(
                {
                    "kind": "profile_error",
                    "status": "error",
                    "path": error.candidate_root,
                    "message": error.message,
                }
            )
            continue
        canonical_root = profile.paths.project_root.resolve()
        candidate_canonical_roots[str(candidate)] = str(canonical_root)
        if canonical_root in profiles_by_root:
            alias = str(candidate)
            deduped_aliases.append(alias)
            evidence.append(
                {
                    "kind": "duplicate_project_root",
                    "status": "skipped",
                    "path": alias,
                    "canonical_project_root": str(canonical_root),
                    "message": "profile resolves to an already selected project root",
                }
            )
            continue
        profiles_by_root[canonical_root] = profile

    base_evidence: list[Mapping[str, object]] = []
    for source_row in scope.scope_evidence:
        row = dict(source_row)
        if row.get("kind") == "duplicate_project_root" and "canonical_project_root" not in row:
            canonical_root = candidate_canonical_roots.get(str(row.get("path") or ""))
            if canonical_root:
                row["canonical_project_root"] = canonical_root
        base_evidence.append(row)
    evidence = [*base_evidence, *evidence]

    canonical_roots = tuple(sorted(profiles_by_root))
    return CanonicalRepoScope(
        scope_source=scope.scope_source,
        supplied_roots=scope.supplied_roots,
        discovery_roots=scope.discovery_roots,
        profiles=tuple(profiles_by_root[root] for root in canonical_roots),
        project_roots=tuple(str(root) for root in canonical_roots),
        deduped_project_roots=_unique_strings(deduped_aliases),
        profile_errors=tuple(errors),
        scope_evidence=bounded_scope_evidence(evidence),
        scope_notes=scope.scope_notes,
    )


def reject_exact_profile_errors(scope: CanonicalRepoScope, *, command: str) -> None:
    """Keep exact-root selection strict while fleet modes degrade per candidate."""

    if scope.scope_source != "explicit_project_roots" or not scope.profile_errors:
        return
    first = scope.profile_errors[0]
    raise RepoLifecycleError(
        f"{command} exact project root {first.candidate_root} is not a usable Blackdog repo: {first.message}"
    )


def discover_profile_dirs(root: Path) -> tuple[Path, ...]:
    """Discover repo profile directories below one explicit, read-only root."""

    candidate = root.expanduser().resolve()
    if not candidate.exists():
        raise RepoLifecycleError(f"discovery root does not exist: {candidate}")
    if candidate.is_file():
        return (candidate.parent,) if candidate.name == PROFILE_FILE_NAME else ()
    discovered: list[Path] = []
    for current_root, dirnames, filenames in os.walk(candidate):
        dirnames[:] = sorted(name for name in dirnames if name not in _DISCOVERY_SKIP_DIRS)
        if PROFILE_FILE_NAME in filenames:
            discovered.append(Path(current_root).resolve())
            dirnames[:] = []
    return tuple(discovered)


def dedupe_project_roots(
    roots: Iterable[Path],
) -> tuple[tuple[Path, ...], tuple[str, ...], tuple[dict[str, str], ...]]:
    """Resolve aliases and retain bounded evidence for skipped duplicates."""

    selected: list[Path] = []
    seen: set[Path] = set()
    deduped: list[str] = []
    evidence: list[dict[str, str]] = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved in seen:
            text = str(resolved)
            deduped.append(text)
            evidence.append(
                {
                    "kind": "duplicate_project_root",
                    "status": "skipped",
                    "path": text,
                    "message": "repository candidate resolves to an already selected path",
                }
            )
            continue
        seen.add(resolved)
        selected.append(resolved)
    return tuple(selected), tuple(_unique_strings(deduped)), bounded_scope_evidence(evidence)


def bounded_scope_evidence(
    rows: Iterable[Mapping[str, object]],
) -> tuple[dict[str, str], ...]:
    """Normalize and bound scope diagnostics so fleet JSON stays compact."""

    normalized: list[dict[str, str]] = []
    for row in rows:
        if len(normalized) >= SCOPE_EVIDENCE_LIMIT:
            break
        normalized.append({str(key): str(value) for key, value in row.items() if value is not None})
    return tuple(normalized)


def _unique_strings(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


__all__ = [
    "CanonicalRepoScope",
    "ProfileScopeError",
    "REGISTRY_FALLBACK_NOTE",
    "RepoScope",
    "SCOPE_EVIDENCE_LIMIT",
    "bounded_scope_evidence",
    "canonicalize_repo_scope",
    "dedupe_project_roots",
    "discover_profile_dirs",
    "resolve_repo_scope",
    "reject_exact_profile_errors",
]
