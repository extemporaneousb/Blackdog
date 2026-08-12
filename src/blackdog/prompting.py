from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from blackdog.contract import ContractDocument, contract_documents
from blackdog.observability import observe_lifecycle
from blackdog.workflow_contract import (
    AGENT_WORKFLOW,
    REPO_OPERATOR_COMMANDS,
    TASK_AND_REPAIR_COMMANDS,
)
from blackdog_core.profile import RepoProfile
from blackdog_core.state import create_prompt_receipt


REPO_LIFECYCLE_COMMANDS = REPO_OPERATOR_COMMANDS
WTAM_COMMANDS = TASK_AND_REPAIR_COMMANDS


@dataclass(frozen=True, slots=True)
class PromptPreview:
    project_name: str
    project_root: str
    workflow_family: str
    prompt_hash: str
    prompt_recorded_at: str
    prompt_source: str | None
    prompt_text: str | None
    composed_prompt: str | None
    validation_commands: tuple[str, ...]
    doc_routing_defaults: tuple[str, ...]
    repo_lifecycle_commands: tuple[str, ...]
    wtam_commands: tuple[str, ...]
    contract_documents: tuple[ContractDocument, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["contract_documents"] = [item.to_dict() for item in self.contract_documents]
        return payload


@dataclass(frozen=True, slots=True)
class TunedPrompt:
    project_name: str
    project_root: str
    workflow_family: str
    prompt_hash: str
    prompt_recorded_at: str
    prompt_source: str | None
    tuned_prompt: str
    validation_commands: tuple[str, ...]
    doc_routing_defaults: tuple[str, ...]
    contract_documents: tuple[ContractDocument, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["contract_documents"] = [item.to_dict() for item in self.contract_documents]
        return payload


def _compose_prompt(
    profile: RepoProfile,
    *,
    request: str,
    include_skill_text: bool,
    include_doc_text: bool,
) -> tuple[str, tuple[ContractDocument, ...]]:
    documents = contract_documents(
        profile,
        expand_skill_text=include_skill_text,
        expand_doc_text=include_doc_text,
    )
    lines = [
        f"You are working in the repo {profile.project_name} at {profile.paths.project_root}.",
        "Follow the repo-local managed `AGENTS.md` Blackdog contract.",
        "Use `./.VE/bin/blackdog` when it exists.",
        f"Normal implementation starts with `{AGENT_WORKFLOW.begin_command}`.",
        "The returned task workspace is the only place for implementation edits.",
    ]
    if profile.validation_commands:
        lines.append("Validation commands configured for this repo:")
        lines.extend(f"- `{command}`" for command in profile.validation_commands)
    if profile.doc_routing_defaults:
        lines.append(
            "Document routing catalog: inspect only entries relevant to this request; "
            "do not load every document by default:"
        )
        lines.extend(f"- `{item}`" for item in profile.doc_routing_defaults)
    lines.append("User request:")
    lines.append(request)
    if documents:
        lines.append("Available contract inputs:")
        for document in documents:
            lines.append(f"- {document.kind}: {document.path}")
            if document.text is not None:
                lines.append("")
                lines.append(f"[{document.kind}] {document.path}")
                lines.append(document.text.rstrip())
    return "\n".join(lines).strip() + "\n", documents


def preview_prompt(
    profile: RepoProfile,
    *,
    request: str,
    prompt_source: str | None = None,
    include_prompt: bool = False,
    expand_skill_text: bool = False,
    expand_contract: bool = False,
) -> PromptPreview:
    receipt = create_prompt_receipt(request, source=prompt_source)
    composed_prompt, documents = _compose_prompt(
        profile,
        request=receipt.text,
        include_skill_text=expand_skill_text,
        include_doc_text=expand_contract,
    )
    observe_lifecycle(
        profile,
        surface="prompt.preview",
        operation_key=receipt.prompt_hash,
        labels={"prompt_role": "request", "prompt_mode": receipt.mode, "operation_phase": "completed"},
    )
    return PromptPreview(
        project_name=profile.project_name,
        project_root=str(profile.paths.project_root),
        workflow_family="repo-lifecycle",
        prompt_hash=receipt.prompt_hash,
        prompt_recorded_at=receipt.recorded_at,
        prompt_source=receipt.source,
        prompt_text=receipt.text if include_prompt else None,
        composed_prompt=composed_prompt if include_prompt else None,
        validation_commands=profile.validation_commands,
        doc_routing_defaults=profile.doc_routing_defaults,
        repo_lifecycle_commands=REPO_LIFECYCLE_COMMANDS,
        wtam_commands=WTAM_COMMANDS,
        contract_documents=documents,
    )


def tune_prompt(
    profile: RepoProfile,
    *,
    request: str,
    prompt_source: str | None = None,
    expand_skill_text: bool = False,
    expand_contract: bool = False,
) -> TunedPrompt:
    receipt = create_prompt_receipt(request, source=prompt_source)
    composed_prompt, documents = _compose_prompt(
        profile,
        request=receipt.text,
        include_skill_text=expand_skill_text,
        include_doc_text=expand_contract,
    )
    observe_lifecycle(
        profile,
        surface="prompt.tune",
        operation_key=receipt.prompt_hash,
        labels={"prompt_role": "request", "prompt_mode": receipt.mode, "operation_phase": "completed"},
    )
    return TunedPrompt(
        project_name=profile.project_name,
        project_root=str(profile.paths.project_root),
        workflow_family="repo-lifecycle",
        prompt_hash=receipt.prompt_hash,
        prompt_recorded_at=receipt.recorded_at,
        prompt_source=receipt.source,
        tuned_prompt=composed_prompt,
        validation_commands=profile.validation_commands,
        doc_routing_defaults=profile.doc_routing_defaults,
        contract_documents=documents,
    )


def render_prompt_preview_text(preview: PromptPreview, *, show_prompt: bool = False) -> str:
    lines = [
        f"[blackdog-prompt] project: {preview.project_name}",
        f"[blackdog-prompt] project root: {preview.project_root}",
        f"[blackdog-prompt] workflow family: {preview.workflow_family}",
        f"[blackdog-prompt] prompt hash: {preview.prompt_hash}",
    ]
    if preview.prompt_source:
        lines.append(f"[blackdog-prompt] prompt source: {preview.prompt_source}")
    if preview.validation_commands:
        lines.append(
            f"[blackdog-prompt] validation commands: {', '.join(preview.validation_commands)}"
        )
    lines.append("[blackdog-prompt] repo lifecycle, registry, reporting, and evidence commands:")
    lines.extend(f"  - {command}" for command in preview.repo_lifecycle_commands)
    lines.append("[blackdog-prompt] task and explicit low-level diagnosis or repair commands:")
    lines.extend(f"  - {command}" for command in preview.wtam_commands)
    if preview.contract_documents:
        lines.append("[blackdog-prompt] contract documents:")
        for document in preview.contract_documents:
            lines.append(f"  - {document.kind}: {document.path}")
    if show_prompt and preview.composed_prompt is not None:
        lines.append("")
        lines.append(preview.composed_prompt.rstrip())
    return "\n".join(lines) + "\n"


__all__ = [
    "PromptPreview",
    "TunedPrompt",
    "preview_prompt",
    "render_prompt_preview_text",
    "tune_prompt",
]
