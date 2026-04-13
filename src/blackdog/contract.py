from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from blackdog_core.profile import RepoProfile


@dataclass(frozen=True, slots=True)
class ContractDocument:
    path: str
    kind: str
    text: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def project_skill_path(project_root: Path) -> Path | None:
    candidate = (project_root / ".codex" / "skills" / "blackdog" / "SKILL.md").resolve()
    return candidate if candidate.is_file() else None


def contract_documents(
    profile: RepoProfile,
    *,
    expand_skill_text: bool = False,
    expand_doc_text: bool = False,
) -> tuple[ContractDocument, ...]:
    candidates: list[tuple[str, Path]] = []
    skill_path = project_skill_path(profile.paths.project_root)
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
    "project_skill_path",
]
