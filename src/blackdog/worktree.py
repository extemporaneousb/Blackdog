from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import os
import subprocess
import time

from .backlog import BacklogError, TaskInfo, load_backlog
from .config import Profile, slugify


class WorktreeError(RuntimeError):
    pass


class DirtyPrimaryWorktreeError(WorktreeError):
    def __init__(
        self,
        *,
        primary_worktree: Path,
        branch: str,
        target_branch: str,
        dirty_paths: list[str],
        overlap_paths: list[str],
    ) -> None:
        self.primary_worktree = str(primary_worktree)
        self.branch = branch
        self.target_branch = target_branch
        self.dirty_paths = tuple(dirty_paths)
        self.overlap_paths = tuple(overlap_paths)
        dirty_text = ", ".join(self.dirty_paths) or "none detected"
        overlap_text = ", ".join(self.overlap_paths) or "none detected"
        super().__init__(
            "dirty primary worktree contract violation: "
            f"{self.primary_worktree} has uncommitted changes blocking landing {self.branch} into {self.target_branch}; "
            f"overlap with branch changes: {overlap_text}; "
            f"dirty paths: {dirty_text}; "
            "clean up or land the primary worktree changes and retry without using git stash"
        )


WTAM_WORKTREE_VE_NOTE = (
    ".VE is unversioned and bound to this worktree path; bootstrap one per worktree and do not reuse another "
    "worktree's .VE."
)


@dataclass(frozen=True)
class WorktreeSpec:
    task_id: str
    task_title: str
    task_slug: str
    branch: str
    base_ref: str
    base_commit: str
    target_branch: str
    worktree_path: str
    primary_worktree: str
    current_worktree: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run_git(repo_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _run_git_no_check(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _repo_root(project_root: Path) -> Path:
    return Path(_run_git(project_root, "rev-parse", "--show-toplevel")).resolve()


def _is_primary_worktree(repo_root: Path) -> bool:
    return (repo_root / ".git").is_dir()


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _parse_worktree_list(repo_root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    raw = _run_git(repo_root, "worktree", "list", "--porcelain")
    for line in raw.splitlines():
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
    repo_root = _repo_root(project_root)
    worktrees = _parse_worktree_list(repo_root)
    for row in worktrees:
        path = Path(str(row.get("worktree") or "")).resolve()
        if path and _is_primary_worktree(path):
            return path
    raise WorktreeError("could not find primary worktree")


def _find_worktree_for_branch(project_root: Path, branch_ref: str) -> Path | None:
    repo_root = _repo_root(project_root)
    for row in _parse_worktree_list(repo_root):
        if str(row.get("branch") or "") == branch_ref:
            return Path(str(row["worktree"])).resolve()
    return None


def find_worktree_for_branch(profile: Profile, branch: str) -> str | None:
    branch_ref = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
    resolved = _find_worktree_for_branch(profile.paths.project_root, branch_ref)
    return str(resolved) if resolved is not None else None


def _status_entries(repo_root: Path) -> list[list[str]]:
    completed = _run_git_no_check(repo_root, "status", "--porcelain")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git status --porcelain failed: {detail}")
    rows: list[list[str]] = []
    for line in (completed.stdout or "").splitlines():
        path_text = line[3:].strip()
        rows.append([item.strip() for item in path_text.split(" -> ") if item.strip()])
    return rows


def _runtime_ignore_prefixes(profile: Profile, *, repo_root: Path | None = None) -> tuple[str, ...]:
    repo_root = (repo_root or _repo_root(profile.paths.project_root)).resolve()
    control_dir = profile.paths.control_dir.resolve()
    if not _is_within(repo_root, control_dir):
        return ()
    relative = control_dir.relative_to(repo_root).as_posix().rstrip("/")
    return (f"{relative}/",)


def dirty_paths(
    repo_root: Path,
    *,
    ignore_paths: frozenset[str] = frozenset(),
    ignore_prefixes: tuple[str, ...] = (),
) -> list[str]:
    rows = _status_entries(repo_root)
    if not rows:
        return []
    dirty: list[str] = []
    for candidates in rows:
        matched = False
        for candidate in candidates:
            if candidate in ignore_paths:
                continue
            if any(candidate.startswith(prefix) for prefix in ignore_prefixes):
                continue
            dirty.append(candidate)
            matched = True
        if matched:
            continue
    return sorted(dict.fromkeys(dirty))


def _status_dirty(
    repo_root: Path,
    *,
    ignore_paths: frozenset[str] = frozenset(),
    ignore_prefixes: tuple[str, ...] = (),
) -> bool:
    return bool(dirty_paths(repo_root, ignore_paths=ignore_paths, ignore_prefixes=ignore_prefixes))


def worktree_contract(
    profile: Profile,
    *,
    workspace: Path | None = None,
    workspace_mode: str | None = None,
) -> dict[str, Any]:
    resolved_workspace = _repo_root(workspace or profile.paths.project_root)
    primary_root = find_primary_worktree(profile.paths.project_root)
    target_branch = _run_git(primary_root, "rev-parse", "--abbrev-ref", "HEAD")
    ignore_prefixes = _runtime_ignore_prefixes(profile, repo_root=primary_root)
    workspace_blackdog = resolved_workspace / ".VE" / "bin" / "blackdog"
    workspace_has_local_blackdog = workspace_blackdog.is_file() and os.access(workspace_blackdog, os.X_OK)
    return {
        "workspace_mode": workspace_mode or profile.supervisor_workspace_mode,
        "current_worktree": str(resolved_workspace),
        "current_branch": _run_git(resolved_workspace, "rev-parse", "--abbrev-ref", "HEAD"),
        "current_is_primary": _is_primary_worktree(resolved_workspace),
        "primary_worktree": str(primary_root),
        "primary_branch": target_branch,
        "target_branch": target_branch,
        "primary_dirty": _status_dirty(primary_root, ignore_prefixes=ignore_prefixes),
        "primary_dirty_paths": dirty_paths(primary_root, ignore_prefixes=ignore_prefixes),
        "workspace_ve": str(resolved_workspace / ".VE"),
        "workspace_blackdog_path": str(workspace_blackdog),
        "workspace_has_local_blackdog": workspace_has_local_blackdog,
        "ve_expectation": WTAM_WORKTREE_VE_NOTE,
    }


def _current_branch(repo_root: Path) -> str:
    branch = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise WorktreeError(f"detached HEAD at {repo_root}; specify --from explicitly")
    return branch


def _task_for_id(profile: Profile, task_id: str) -> TaskInfo:
    snapshot = load_backlog(profile.paths, profile)
    task = snapshot.tasks.get(task_id)
    if task is None:
        raise BacklogError(f"Unknown task id: {task_id}")
    return task


def _task_slug(task: TaskInfo) -> str:
    return slugify(f"{task.id}-{task.title}")


def default_task_branch(task: TaskInfo) -> str:
    return f"agent/{_task_slug(task)}"


def task_id_for_branch(profile: Profile, branch: str | None) -> str | None:
    resolved_branch = str(branch or "").strip()
    if not resolved_branch:
        return None
    snapshot = load_backlog(profile.paths, profile)
    for task in snapshot.tasks.values():
        task_branch = default_task_branch(task)
        if resolved_branch == task_branch or resolved_branch.startswith(f"{task_branch}-"):
            return task.id
    return None


def default_task_worktree_path(profile: Profile, task: TaskInfo) -> Path:
    return profile.paths.worktrees_dir / f"wt-{_task_slug(task)}"


def supervisor_task_branch(task: TaskInfo, run_id: str) -> str:
    return f"{default_task_branch(task)}-{run_id}"


def supervisor_task_worktree_path(profile: Profile, task: TaskInfo, run_id: str) -> Path:
    return profile.paths.worktrees_dir / f"wt-{_task_slug(task)}-{run_id}"


def _resolve_from_ref(primary_root: Path, from_ref: str | None, *, default_branch: str) -> str:
    if not from_ref:
        return default_branch
    if _run_git_no_check(primary_root, "rev-parse", "--verify", f"{from_ref}^{{commit}}").returncode == 0:
        return from_ref
    remote_ref = f"origin/{from_ref}"
    if _run_git_no_check(primary_root, "rev-parse", "--verify", f"{remote_ref}^{{commit}}").returncode == 0:
        return remote_ref
    raise WorktreeError(f"could not resolve --from ref: {from_ref} (try: git fetch --all --prune)")


def worktree_preflight(profile: Profile, *, cwd: Path | None = None) -> dict[str, Any]:
    resolved_cwd = (cwd or Path.cwd()).resolve()
    current_root = _repo_root(resolved_cwd)
    contract = worktree_contract(profile, workspace=current_root)
    primary_root = Path(contract["primary_worktree"]).resolve()
    runtime_ignore_prefixes = _runtime_ignore_prefixes(profile)
    configured_worktrees_dir = profile.paths.worktrees_dir.resolve()
    worktrees = []
    for row in _parse_worktree_list(profile.paths.project_root):
        path = Path(str(row.get("worktree") or "")).resolve()
        branch = str(row.get("branch") or "")
        worktrees.append(
            {
                "path": str(path),
                "branch": branch.removeprefix("refs/heads/") if branch else "",
                "is_primary": _is_primary_worktree(path),
            }
        )
    return {
        "project_root": str(profile.paths.project_root),
        "repo_root": str(current_root),
        "cwd": str(resolved_cwd),
        "current_worktree": contract["current_worktree"],
        "current_branch": contract["current_branch"],
        "current_is_primary": contract["current_is_primary"],
        "primary_worktree": contract["primary_worktree"],
        "primary_branch": contract["primary_branch"],
        "dirty": _status_dirty(current_root),
        "implementation_dirty": _status_dirty(
            current_root,
            ignore_prefixes=runtime_ignore_prefixes,
        ),
        "worktree_model": "branch-backed",
        "workspace_mode": contract["workspace_mode"],
        "target_branch": contract["target_branch"],
        "primary_dirty": contract["primary_dirty"],
        "primary_dirty_paths": contract["primary_dirty_paths"],
        "current_worktree_ve": contract["workspace_ve"],
        "current_worktree_blackdog_path": contract["workspace_blackdog_path"],
        "current_worktree_has_local_blackdog": contract["workspace_has_local_blackdog"],
        "ve_expectation": contract["ve_expectation"],
        "workspace_contract": contract,
        "worktrees_dir": str(configured_worktrees_dir),
        "worktrees_dir_inside_repo": _is_within(primary_root, configured_worktrees_dir),
        "worktrees": worktrees,
    }


def primary_worktree_is_dirty(profile: Profile, *, ignore_runtime: bool = True) -> bool:
    primary_root = find_primary_worktree(profile.paths.project_root)
    ignore_prefixes = _runtime_ignore_prefixes(profile) if ignore_runtime else ()
    return _status_dirty(primary_root, ignore_prefixes=ignore_prefixes)


def primary_worktree_dirty_paths(profile: Profile, *, ignore_runtime: bool = True) -> list[str]:
    primary_root = find_primary_worktree(profile.paths.project_root)
    ignore_prefixes = _runtime_ignore_prefixes(profile) if ignore_runtime else ()
    return dirty_paths(primary_root, ignore_prefixes=ignore_prefixes)


def dirty_primary_worktree_error(
    profile: Profile,
    *,
    branch: str,
    target_branch: str | None = None,
) -> DirtyPrimaryWorktreeError:
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    dirty = primary_worktree_dirty_paths(profile, ignore_runtime=True)
    overlap = sorted(set(dirty).intersection(branch_changed_paths(profile, branch=branch, target_branch=resolved_target)))
    return DirtyPrimaryWorktreeError(
        primary_worktree=primary_root,
        branch=branch,
        target_branch=resolved_target,
        dirty_paths=dirty,
        overlap_paths=overlap,
    )


def start_task_worktree(
    profile: Profile,
    *,
    task_id: str,
    branch: str | None = None,
    from_ref: str | None = None,
    path: str | None = None,
) -> WorktreeSpec:
    task = _task_for_id(profile, task_id)
    current_root = _repo_root(profile.paths.project_root)
    primary_root = find_primary_worktree(profile.paths.project_root)
    target_branch = _current_branch(primary_root)
    base_ref = _resolve_from_ref(primary_root, from_ref, default_branch=target_branch)
    base_commit = _run_git(primary_root, "rev-parse", f"{base_ref}^{{commit}}")
    resolved_branch = branch or default_task_branch(task)
    worktree_path = Path(path).resolve() if path else default_task_worktree_path(profile, task).resolve()
    if _is_within(primary_root, worktree_path):
        raise WorktreeError(f"refusing worktree path inside the repository: {worktree_path}")
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists():
        raise WorktreeError(f"worktree path already exists: {worktree_path}")
    completed = _run_git_no_check(primary_root, "worktree", "add", str(worktree_path), "-b", resolved_branch, base_ref)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git worktree add failed: {detail}")
    return WorktreeSpec(
        task_id=task.id,
        task_title=task.title,
        task_slug=_task_slug(task),
        branch=resolved_branch,
        base_ref=base_ref,
        base_commit=base_commit,
        target_branch=target_branch,
        worktree_path=str(worktree_path),
        primary_worktree=str(primary_root),
        current_worktree=str(current_root),
    )


def land_branch(
    profile: Profile,
    *,
    branch: str | None = None,
    target_branch: str | None = None,
    pull: bool = True,
    cleanup: bool = False,
) -> dict[str, Any]:
    current_root = _repo_root(profile.paths.project_root)
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_branch = branch or _current_branch(current_root)
    resolved_target = target_branch or _current_branch(primary_root)
    if resolved_branch == resolved_target:
        raise WorktreeError(f"refusing to land into the same branch: {resolved_target}")
    if resolved_branch == "main":
        raise WorktreeError("refusing to land branch=main")

    target_ref = f"refs/heads/{resolved_target}"
    target_worktree = _find_worktree_for_branch(primary_root, target_ref)
    created_target = False
    if target_worktree is None:
        target_worktree = (profile.paths.worktrees_dir / f"wt-land-{slugify(f'{resolved_target}-{int(time.time())}') }").resolve()
        target_worktree.parent.mkdir(parents=True, exist_ok=True)
        _run_git(primary_root, "worktree", "add", str(target_worktree), resolved_target)
        created_target = True
    try:
        if _status_dirty(target_worktree, ignore_prefixes=_runtime_ignore_prefixes(profile)):
            if target_worktree == primary_root:
                raise dirty_primary_worktree_error(profile, branch=resolved_branch, target_branch=resolved_target)
            raise WorktreeError(f"target worktree has uncommitted changes: {target_worktree}")
        if pull:
            upstream = _run_git_no_check(target_worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
            if upstream.returncode == 0:
                _run_git(target_worktree, "pull", "--ff-only")

        head_commit = _run_git(target_worktree, "rev-parse", "HEAD")
        ancestor = _run_git_no_check(target_worktree, "merge-base", "--is-ancestor", head_commit, resolved_branch)
        if ancestor.returncode != 0:
            raise WorktreeError(
                f"cannot land: {resolved_branch} is not based on the current {resolved_target}; rebase it first"
            )
        landed_commit = _run_git(target_worktree, "rev-parse", resolved_branch)
        _run_git(target_worktree, "merge", "--ff-only", resolved_branch)

        cleaned_worktree: str | None = None
        deleted_branch = False
        branch_worktree = _find_worktree_for_branch(primary_root, f"refs/heads/{resolved_branch}")
        if cleanup and branch_worktree is not None and branch_worktree != target_worktree:
            if _status_dirty(branch_worktree, ignore_prefixes=_runtime_ignore_prefixes(profile)):
                raise WorktreeError(f"refusing cleanup: worktree has uncommitted changes: {branch_worktree}")
            _run_git(primary_root, "worktree", "remove", str(branch_worktree))
            cleaned_worktree = str(branch_worktree)
            _run_git(target_worktree, "branch", "-d", resolved_branch)
            deleted_branch = True

        removed_target = False
        if created_target:
            _run_git(primary_root, "worktree", "remove", str(target_worktree))
            removed_target = True

        return {
            "branch": resolved_branch,
            "target_branch": resolved_target,
            "primary_worktree": str(primary_root),
            "target_worktree": str(target_worktree),
            "landed_commit": landed_commit,
            "cleanup": cleanup,
            "cleaned_worktree": cleaned_worktree,
            "deleted_branch": deleted_branch,
            "removed_temporary_target": removed_target,
        }
    except Exception:
        if created_target and target_worktree.exists():
            _run_git_no_check(primary_root, "worktree", "remove", "--force", str(target_worktree))
        raise


def branch_changed_paths(profile: Profile, *, branch: str, target_branch: str | None = None) -> list[str]:
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    completed = _run_git_no_check(primary_root, "diff", "--name-only", f"{resolved_target}..{branch}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git diff --name-only {resolved_target}..{branch} failed: {detail}")
    return sorted({line.strip() for line in completed.stdout.splitlines() if line.strip()})


def working_tree_matches_ref(
    profile: Profile,
    *,
    ref: str,
    paths: list[str],
    repo_root: Path | None = None,
) -> bool:
    resolved_root = _repo_root(repo_root or profile.paths.project_root)
    for path in paths:
        candidate = (resolved_root / path).resolve()
        if candidate.exists() and candidate.is_dir():
            return False
        completed = subprocess.run(
            ["git", "-C", str(resolved_root), "show", f"{ref}:{path}"],
            capture_output=True,
            check=False,
        )
        ref_has_path = completed.returncode == 0
        if ref_has_path:
            if not candidate.exists() or candidate.is_dir():
                return False
            if candidate.read_bytes() != completed.stdout:
                return False
            continue
        if candidate.exists():
            return False
    return True


def commit_working_tree_paths(
    profile: Profile,
    *,
    paths: list[str],
    message: str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    if not paths:
        raise WorktreeError("cannot commit an empty path set")
    resolved_root = _repo_root(repo_root or profile.paths.project_root)
    add = _run_git_no_check(resolved_root, "add", "-A", "--", *paths)
    if add.returncode != 0:
        detail = add.stderr.strip() or add.stdout.strip() or f"exit code {add.returncode}"
        raise WorktreeError(f"git add failed: {detail}")
    staged = _run_git_no_check(resolved_root, "diff", "--cached", "--quiet", "--exit-code")
    if staged.returncode == 0:
        raise WorktreeError("git add produced no staged changes to commit")
    if staged.returncode not in {0, 1}:
        detail = staged.stderr.strip() or staged.stdout.strip() or f"exit code {staged.returncode}"
        raise WorktreeError(f"git diff --cached --quiet failed: {detail}")
    commit = _run_git_no_check(resolved_root, "commit", "-m", message)
    if commit.returncode != 0:
        detail = commit.stderr.strip() or commit.stdout.strip() or f"exit code {commit.returncode}"
        raise WorktreeError(f"git commit failed: {detail}")
    return {
        "repo_root": str(resolved_root),
        "commit": _run_git(resolved_root, "rev-parse", "HEAD"),
        "message": message,
        "paths": list(paths),
    }


def stash_working_tree(
    profile: Profile,
    *,
    message: str,
    repo_root: Path | None = None,
    include_untracked: bool = True,
) -> dict[str, Any]:
    resolved_root = _repo_root(repo_root or profile.paths.project_root)
    before = _run_git_no_check(resolved_root, "stash", "list", "--format=%gd%x00%gs")
    if before.returncode != 0:
        detail = before.stderr.strip() or before.stdout.strip() or f"exit code {before.returncode}"
        raise WorktreeError(f"git stash list failed: {detail}")
    command = ["stash", "push"]
    if include_untracked:
        command.append("-u")
    command.extend(["-m", message])
    push = _run_git_no_check(resolved_root, *command)
    if push.returncode != 0:
        detail = push.stderr.strip() or push.stdout.strip() or f"exit code {push.returncode}"
        raise WorktreeError(f"git stash push failed: {detail}")
    after = _run_git_no_check(resolved_root, "stash", "list", "--format=%gd%x00%gs")
    if after.returncode != 0:
        detail = after.stderr.strip() or after.stdout.strip() or f"exit code {after.returncode}"
        raise WorktreeError(f"git stash list failed: {detail}")
    rows = [line for line in after.stdout.splitlines() if line.strip()]
    if not rows:
        raise WorktreeError("git stash push succeeded but no stash entry was created")
    ref, _, subject = rows[0].partition("\x00")
    if message not in subject:
        raise WorktreeError(f"expected newest stash entry to include {message!r}, got {subject!r}")
    return {
        "repo_root": str(resolved_root),
        "stash_ref": ref.strip(),
        "message": subject.strip(),
    }


def rebase_branch_onto_target(
    profile: Profile,
    *,
    branch: str,
    target_branch: str | None = None,
    pull: bool = True,
) -> dict[str, Any]:
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    target_ref = f"refs/heads/{resolved_target}"
    target_worktree = _find_worktree_for_branch(primary_root, target_ref)
    created_target = False
    if target_worktree is None:
        target_worktree = (profile.paths.worktrees_dir / f"wt-rebase-{slugify(f'{resolved_target}-{int(time.time())}')}").resolve()
        target_worktree.parent.mkdir(parents=True, exist_ok=True)
        _run_git(primary_root, "worktree", "add", str(target_worktree), resolved_target)
        created_target = True
    try:
        branch_worktree = _find_worktree_for_branch(primary_root, f"refs/heads/{branch}")
        if branch_worktree is None:
            raise WorktreeError(f"could not find worktree for branch: {branch}")
        if _status_dirty(branch_worktree, ignore_prefixes=_runtime_ignore_prefixes(profile)):
            raise WorktreeError(f"branch worktree has uncommitted changes: {branch_worktree}")
        if _status_dirty(target_worktree, ignore_prefixes=_runtime_ignore_prefixes(profile)):
            raise WorktreeError(f"target worktree has uncommitted changes: {target_worktree}")
        if pull:
            upstream = _run_git_no_check(target_worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
            if upstream.returncode == 0:
                _run_git(target_worktree, "pull", "--ff-only")

        before_commit = _run_git(branch_worktree, "rev-parse", "HEAD")
        rebase = _run_git_no_check(branch_worktree, "rebase", resolved_target)
        if rebase.returncode != 0:
            _run_git_no_check(branch_worktree, "rebase", "--abort")
            detail = rebase.stderr.strip() or rebase.stdout.strip() or f"exit code {rebase.returncode}"
            raise WorktreeError(f"git rebase {resolved_target} failed: {detail}")
        after_commit = _run_git(branch_worktree, "rev-parse", "HEAD")

        return {
            "branch": branch,
            "target_branch": resolved_target,
            "worktree": str(branch_worktree),
            "before_commit": before_commit,
            "after_commit": after_commit,
        }
    finally:
        if created_target and target_worktree.exists():
            _run_git_no_check(primary_root, "worktree", "remove", "--force", str(target_worktree))


def branch_ahead_of_target(profile: Profile, *, branch: str, target_branch: str | None = None) -> bool:
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    completed = _run_git_no_check(primary_root, "rev-list", "--count", f"{resolved_target}..{branch}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git rev-list --count {resolved_target}..{branch} failed: {detail}")
    return int(completed.stdout.strip() or "0") > 0


def cleanup_task_worktree(
    profile: Profile,
    *,
    task_id: str | None = None,
    path: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_path: Path
    resolved_branch = branch
    if task_id:
        task = _task_for_id(profile, task_id)
        resolved_path = default_task_worktree_path(profile, task).resolve()
        resolved_branch = resolved_branch or default_task_branch(task)
    elif path:
        resolved_path = Path(path).resolve()
    else:
        raise WorktreeError("cleanup requires --id or --path")
    if not resolved_path.exists():
        raise WorktreeError(f"worktree path not found: {resolved_path}")
    _run_git(primary_root, "worktree", "remove", str(resolved_path))
    deleted_branch = False
    if resolved_branch:
        _run_git(primary_root, "branch", "-d", resolved_branch)
        deleted_branch = True
    return {
        "worktree_path": str(resolved_path),
        "branch": resolved_branch,
        "deleted_branch": deleted_branch,
    }


def render_preflight_text(payload: dict[str, Any]) -> str:
    dirty = "yes" if payload["dirty"] else "no"
    implementation_dirty = "yes" if payload["implementation_dirty"] else "no"
    primary_clean = "yes" if not payload["primary_dirty"] else "no"
    primary = "yes" if payload["current_is_primary"] else f"no (hint: {payload['primary_worktree']})"
    location = "inside repo" if payload["worktrees_dir_inside_repo"] else "outside repo"
    workspace_blackdog = (
        payload["current_worktree_blackdog_path"] if payload["current_worktree_has_local_blackdog"] else "blackdog"
    )
    lines = [
        f"[blackdog-worktree] preflight: {payload['repo_root']} (branch: {payload['current_branch']}, dirty: {dirty})",
        f"[blackdog-worktree] project root: {payload['project_root']}",
        f"[blackdog-worktree] cwd: {payload['cwd']}",
        f"[blackdog-worktree] current worktree: {payload['current_worktree']}",
        f"[blackdog-worktree] primary worktree: {primary}",
        f"[blackdog-worktree] workspace mode: {payload['workspace_mode']}",
        f"[blackdog-worktree] model: {payload['worktree_model']}",
        f"[blackdog-worktree] target branch: {payload['target_branch']}",
        f"[blackdog-worktree] primary clean for landing: {primary_clean}",
        f"[blackdog-worktree] implementation dirty: {implementation_dirty}",
        f"[blackdog-worktree] worktrees dir: {payload['worktrees_dir']} ({location})",
        f"[blackdog-worktree] current worktree CLI: {workspace_blackdog}",
        f"[blackdog-worktree] .VE rule: {payload['ve_expectation']}",
    ]
    if payload["primary_dirty_paths"]:
        lines.append(f"[blackdog-worktree] primary dirty paths: {', '.join(payload['primary_dirty_paths'])}")
    for row in payload["worktrees"]:
        label = "primary" if row["is_primary"] else row["branch"] or "(detached)"
        lines.append(f"[blackdog-worktree] known: {row['path']} [{label}]")
    return "\n".join(lines) + "\n"


def render_start_text(spec: WorktreeSpec) -> str:
    lines = [
        f"[blackdog-worktree] created: {spec.worktree_path}",
        f"[blackdog-worktree] branch: {spec.branch}",
        f"[blackdog-worktree] base: {spec.base_ref} ({spec.base_commit})",
        f"[blackdog-worktree] target branch: {spec.target_branch}",
        f"[blackdog-worktree] task: {spec.task_id} {spec.task_title}",
    ]
    return "\n".join(lines) + "\n"


def render_land_text(payload: dict[str, Any]) -> str:
    lines = [
        f"[blackdog-worktree] landed: {payload['branch']} -> {payload['target_branch']}",
        f"[blackdog-worktree] target worktree: {payload['target_worktree']}",
        f"[blackdog-worktree] landed commit: {payload['landed_commit']}",
    ]
    if payload["cleaned_worktree"]:
        lines.append(f"[blackdog-worktree] cleaned worktree: {payload['cleaned_worktree']}")
    if payload["deleted_branch"]:
        lines.append(f"[blackdog-worktree] deleted branch: {payload['branch']}")
    return "\n".join(lines) + "\n"


def render_cleanup_text(payload: dict[str, Any]) -> str:
    lines = [f"[blackdog-worktree] removed: {payload['worktree_path']}"]
    if payload["branch"]:
        action = "deleted" if payload["deleted_branch"] else "kept"
        lines.append(f"[blackdog-worktree] branch: {payload['branch']} ({action})")
    return "\n".join(lines) + "\n"
