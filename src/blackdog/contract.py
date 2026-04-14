from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Union

from blackdog_core.profile import RepoProfile, slugify


MANAGED_SKILLS_ROOT = Path(".codex") / "skills"
LEGACY_MANAGED_SKILL_NAME = "blackdog"
SKILL_FILE_NAME = "SKILL.md"


SkillIdentity = Union[RepoProfile, Path, str]


@dataclass(frozen=True, slots=True)
class ContractDocument:
    path: str
    kind: str
    text: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _skill_identity(value: SkillIdentity) -> str:
    if isinstance(value, RepoProfile):
        return value.project_name
    if isinstance(value, Path):
        return value.resolve().name
    return str(value)


def managed_skill_name(value: SkillIdentity) -> str:
    return slugify(_skill_identity(value))


def managed_skill_relative_path(value: SkillIdentity) -> Path:
    return MANAGED_SKILLS_ROOT / managed_skill_name(value) / SKILL_FILE_NAME


def legacy_managed_skill_relative_path() -> Path:
    return MANAGED_SKILLS_ROOT / LEGACY_MANAGED_SKILL_NAME / SKILL_FILE_NAME


def project_skill_candidates(value: SkillIdentity, *, project_root: Path | None = None) -> tuple[Path, ...]:
    root = (project_root or (value.paths.project_root if isinstance(value, RepoProfile) else Path(value))).resolve()
    candidates = [(root / managed_skill_relative_path(value)).resolve()]
    legacy = (root / legacy_managed_skill_relative_path()).resolve()
    if legacy not in candidates:
        candidates.append(legacy)
    return tuple(candidates)


def project_skill_path(value: SkillIdentity, *, project_root: Path | None = None) -> Path | None:
    for candidate in project_skill_candidates(value, project_root=project_root):
        if candidate.is_file():
            return candidate
    return None


def contract_documents(
    profile: RepoProfile,
    *,
    expand_skill_text: bool = False,
    expand_doc_text: bool = False,
) -> tuple[ContractDocument, ...]:
    candidates: list[tuple[str, Path]] = []
    skill_path = project_skill_path(profile, project_root=profile.paths.project_root)
    if skill_path is not None:
        candidates.append(("skill", skill_path))
    for raw in profile.doc_routing_defaults:
        candidate = (profile.paths.project_root / raw).resolve()
        if candidate.is_file():
            candidates.append(("doc", candidate))
    seen: set[Path] = set()
    documents: list[ContractDocument] = []
    for kind, candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        include_text = (kind == "skill" and expand_skill_text) or (kind == "doc" and expand_doc_text)
        text = candidate.read_text(encoding="utf-8") if include_text else None
        documents.append(
            ContractDocument(
                path=str(candidate),
                kind=kind,
                text=text,
            )
        )
    return tuple(documents)


__all__ = [
    "ContractDocument",
    "contract_documents",
    "legacy_managed_skill_relative_path",
    "managed_skill_name",
    "managed_skill_relative_path",
    "project_skill_path",
    "project_skill_candidates",
]
