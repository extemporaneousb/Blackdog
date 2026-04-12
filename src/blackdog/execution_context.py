from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


WORKSPACE_MODE_GIT_WORKTREE = "git-worktree"
WORKTREE_ROLE_PRIMARY = "primary"
WORKTREE_ROLE_LINKED = "linked"


class ExecutionContextError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitExecutionContext:
    workspace_mode: str
    worktree_role: str
    worktree_path: str
    branch: str | None
    start_commit: str


def _run_git(project_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise ExecutionContextError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def resolve_git_execution_context(project_root: Path) -> GitExecutionContext:
    worktree_root = Path(_run_git(project_root, "rev-parse", "--show-toplevel")).resolve()
    git_marker = worktree_root / ".git"
    if git_marker.is_dir():
        worktree_role = WORKTREE_ROLE_PRIMARY
    elif git_marker.is_file():
        worktree_role = WORKTREE_ROLE_LINKED
    else:
        raise ExecutionContextError(f"could not determine worktree role for {worktree_root}")
    branch = _run_git(worktree_root, "branch", "--show-current") or None
    start_commit = _run_git(worktree_root, "rev-parse", "HEAD")
    return GitExecutionContext(
        workspace_mode=WORKSPACE_MODE_GIT_WORKTREE,
        worktree_role=worktree_role,
        worktree_path=str(worktree_root),
        branch=branch,
        start_commit=start_commit,
    )


__all__ = [
    "ExecutionContextError",
    "GitExecutionContext",
    "WORKSPACE_MODE_GIT_WORKTREE",
    "WORKTREE_ROLE_LINKED",
    "WORKTREE_ROLE_PRIMARY",
    "resolve_git_execution_context",
]
