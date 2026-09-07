"""Shared Git worktree identity and lineage proof for task effects."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import subprocess
from typing import Any

from blackdog.lifecycle import WorktreeError
from blackdog_core.profile import RepoProfile
from blackdog_core.state import (
    TaskAttemptRecord,
    WORKSPACE_MODE_GIT_WORKTREE,
    WORKTREE_ROLE_TASK,
)


@dataclass(frozen=True, slots=True)
class TaskWorktreeProof:
    valid: bool
    path: str | None
    branch: str | None
    registered_path: str | None
    head_commit: str | None
    start_commit: str | None
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_git(repo_root: Path, *args: str, input_text: str | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"exit code {completed.returncode}"
        )
        raise WorktreeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _run_git_no_check(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _repo_root(path: Path) -> Path:
    return Path(_run_git(path, "rev-parse", "--show-toplevel")).resolve()


def _git_common_dir(path: Path) -> Path:
    root = _repo_root(path)
    value = Path(_run_git(root, "rev-parse", "--git-common-dir"))
    return value.resolve() if value.is_absolute() else (root / value).resolve()


def _parse_worktree_list(repo_root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in _run_git(repo_root, "worktree", "list", "--porcelain").splitlines():
        if not line.strip():
            if current:
                rows.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value.strip()
    if current:
        rows.append(current)
    return rows


def find_primary_worktree(project_root: Path) -> Path:
    root = _repo_root(project_root)
    for row in _parse_worktree_list(root):
        candidate = Path(str(row.get("worktree") or "")).resolve()
        if (candidate / ".git").is_dir():
            return candidate
    raise WorktreeError("could not find the primary worktree")


def _find_worktree_for_branch(project_root: Path, branch: str) -> Path | None:
    root = _repo_root(project_root)
    branch_ref = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
    for row in _parse_worktree_list(root):
        if row.get("branch") == branch_ref:
            return Path(row["worktree"]).resolve()
    return None


def _current_branch(repo_root: Path) -> str:
    branch = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise WorktreeError(f"detached HEAD at {repo_root}")
    return branch


def inspect_task_worktree(
    profile: RepoProfile,
    attempt: TaskAttemptRecord,
) -> TaskWorktreeProof:
    path = Path(attempt.worktree_path).resolve() if attempt.worktree_path else None
    primary = find_primary_worktree(profile.paths.project_root)
    branch = attempt.branch
    registered = _find_worktree_for_branch(primary, branch) if branch else None

    def result(
        valid: bool,
        reason: str | None,
        *,
        head: str | None = None,
    ) -> TaskWorktreeProof:
        return TaskWorktreeProof(
            valid=valid,
            path=str(path) if path is not None else None,
            branch=branch,
            registered_path=str(registered) if registered is not None else None,
            head_commit=head,
            start_commit=attempt.start_commit,
            reason=reason,
        )

    if (
        attempt.workspace_mode != WORKSPACE_MODE_GIT_WORKTREE
        or attempt.worktree_role != WORKTREE_ROLE_TASK
    ):
        return result(False, "attempt does not declare the task Git-worktree contract")
    if path is None or branch is None or attempt.start_commit is None:
        return result(
            False, "attempt worktree, branch, or start lineage metadata is missing"
        )
    if path == primary:
        return result(False, "attempt resolves to the primary worktree")
    if registered != path:
        return result(
            False, "attempt branch is not registered at the durable task-worktree path"
        )
    if not path.is_dir():
        return result(False, "registered task-worktree path is missing")
    try:
        if _current_branch(path) != branch:
            return result(
                False, "registered task worktree is checked out on a different branch"
            )
        head = _run_git(path, "rev-parse", "HEAD^{commit}")
        branch_head = _run_git(primary, "rev-parse", f"refs/heads/{branch}^{{commit}}")
        if head != branch_head:
            return result(
                False, "task branch and registered worktree HEAD disagree", head=head
            )
        lineage = _run_git_no_check(
            primary,
            "merge-base",
            "--is-ancestor",
            attempt.start_commit,
            head,
        )
        if lineage.returncode != 0:
            return result(
                False,
                "task worktree HEAD is not descended from its recorded start commit",
                head=head,
            )
    except (OSError, WorktreeError) as exc:
        return result(False, f"task-worktree Git proof failed: {exc}")
    return result(True, None, head=head)
