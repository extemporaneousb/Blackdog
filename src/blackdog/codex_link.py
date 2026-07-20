from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import shlex
from typing import Any
from urllib.parse import quote, urlencode

from blackdog.wtam import show_task
from blackdog_core.profile import RepoProfile


CODEX_LINK_SCHEMA_VERSION = 1
CODEX_LINK_PROMPT_MAX_CHARS = 1024


class CodexLinkError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CodexWorkspaceLink:
    schema_version: int
    kind: str
    workspace_owner: str
    workspace_role: str
    codex_workspace_kind: str
    thread_continuity: str
    auto_submits: bool
    project_root: str
    workset_id: str
    task_id: str
    attempt_id: str
    branch: str
    target_branch: str
    workspace_path: str
    prompt: str
    url: str
    fallback_argv: tuple[str, ...]
    fallback_prefills_prompt: bool

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["fallback_argv"] = list(self.fallback_argv)
        return payload


def build_codex_workspace_url(*, workspace_path: Path, prompt: str) -> str:
    resolved_path = workspace_path.expanduser().resolve()
    query = urlencode(
        {"path": str(resolved_path), "prompt": prompt},
        quote_via=quote,
        safe="",
    )
    return f"codex://threads/new?{query}"


def build_codex_workspace_link(
    profile: RepoProfile,
    *,
    workset_id: str | None = None,
    task_id: str | None = None,
    cwd: Path | None = None,
) -> CodexWorkspaceLink:
    task = show_task(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        cwd=cwd,
    )
    payload = task.to_dict()
    if not payload.get("active_attempt") or payload.get("attempt_status") != "in_progress":
        raise CodexLinkError("codex link requires an active in-progress Blackdog task attempt")
    if not payload.get("worktree_exists"):
        raise CodexLinkError("codex link requires the active Blackdog task worktree to exist")

    resolved_workset = _required_text(payload, "workset_id")
    resolved_task = _required_text(payload, "task_id")
    attempt_id = _required_text(payload, "attempt_id")
    branch = _required_text(payload, "branch")
    target_branch = _required_text(payload, "target_branch")
    workspace_path = Path(_required_text(payload, "worktree_path")).expanduser().resolve()
    if not workspace_path.is_dir():
        raise CodexLinkError(f"active Blackdog task worktree is not a directory: {workspace_path}")

    show_argv = (
        "./.VE/bin/blackdog",
        "task",
        "show",
        "--project-root=.",
        f"--workset={resolved_workset}",
        f"--task={resolved_task}",
        "--json",
    )
    prompt = (
        "Continue this active Blackdog task in the current workspace. "
        f"Run {shlex.join(show_argv)} and follow its next_action exactly. "
        "Do not create or hand off to another Codex worktree; Blackdog owns this "
        "worktree, branch, landing, and cleanup."
    )
    if len(prompt) > CODEX_LINK_PROMPT_MAX_CHARS:
        raise CodexLinkError(
            f"codex link prompt exceeds the {CODEX_LINK_PROMPT_MAX_CHARS}-character bound"
        )

    return CodexWorkspaceLink(
        schema_version=CODEX_LINK_SCHEMA_VERSION,
        kind="codex_local_workspace_link",
        workspace_owner="blackdog",
        workspace_role="task",
        codex_workspace_kind="local",
        thread_continuity="new_thread",
        auto_submits=False,
        project_root=str(profile.paths.project_root),
        workset_id=resolved_workset,
        task_id=resolved_task,
        attempt_id=attempt_id,
        branch=branch,
        target_branch=target_branch,
        workspace_path=str(workspace_path),
        prompt=prompt,
        url=build_codex_workspace_url(workspace_path=workspace_path, prompt=prompt),
        fallback_argv=("codex", "app", str(workspace_path)),
        fallback_prefills_prompt=False,
    )


def render_codex_workspace_link_text(link: CodexWorkspaceLink) -> str:
    fallback = shlex.join(link.fallback_argv)
    return "\n".join(
        [
            "Codex local workspace link",
            f"Workspace: {link.workspace_path}",
            "Owner: Blackdog (worktree, branch, landing, cleanup)",
            "Thread: new; prompt is prefilled but not submitted",
            f"Open: {link.url}",
            f"Fallback: {fallback} (does not prefill the prompt)",
            "",
        ]
    )


def _required_text(payload: dict[str, Any], field: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise CodexLinkError(f"active Blackdog task is missing {field}")
    return value


__all__ = [
    "CODEX_LINK_PROMPT_MAX_CHARS",
    "CODEX_LINK_SCHEMA_VERSION",
    "CodexLinkError",
    "CodexWorkspaceLink",
    "build_codex_workspace_link",
    "build_codex_workspace_url",
    "render_codex_workspace_link_text",
]
