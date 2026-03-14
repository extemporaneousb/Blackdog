from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import subprocess
import time

from .backlog import BacklogError, TaskInfo, load_backlog
from .config import Profile, slugify


class WorktreeError(RuntimeError):
    pass


IGNORED_RUNTIME_PATHS = frozenset(
    {
        ".blackdog/backlog-state.json",
        ".blackdog/events.jsonl",
        ".blackdog/inbox.jsonl",
        ".blackdog/backlog-index.html",
    }
)
IGNORED_RUNTIME_PREFIXES = (
    ".blackdog/task-results/",
    ".blackdog/supervisor-runs/",
    ".blackdog/worktrees/",
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


def _status_dirty(repo_root: Path, *, ignore_runtime: bool = False) -> bool:
    completed = _run_git_no_check(repo_root, "status", "--porcelain")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git status --porcelain failed: {detail}")
    raw = completed.stdout
    if not raw:
        return False
    if not ignore_runtime:
        return True
    for line in raw.splitlines():
        path_text = line[3:].strip()
        candidates = [item.strip() for item in path_text.split(" -> ")]
        runtime_only = True
        for candidate in candidates:
            if candidate in IGNORED_RUNTIME_PATHS:
                continue
            if any(candidate.startswith(prefix) for prefix in IGNORED_RUNTIME_PREFIXES):
                continue
            runtime_only = False
            break
        if not runtime_only:
            return True
    return False


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


def worktree_preflight(profile: Profile) -> dict[str, Any]:
    current_root = _repo_root(profile.paths.project_root)
    primary_root = find_primary_worktree(profile.paths.project_root)
    current_branch = _run_git(current_root, "rev-parse", "--abbrev-ref", "HEAD")
    primary_branch = _run_git(primary_root, "rev-parse", "--abbrev-ref", "HEAD")
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
        "repo_root": str(current_root),
        "current_worktree": str(profile.paths.project_root.resolve()),
        "current_branch": current_branch,
        "current_is_primary": _is_primary_worktree(profile.paths.project_root.resolve()),
        "primary_worktree": str(primary_root),
        "primary_branch": primary_branch,
        "dirty": _status_dirty(profile.paths.project_root.resolve()),
        "implementation_dirty": _status_dirty(profile.paths.project_root.resolve(), ignore_runtime=True),
        "worktree_model": "branch-backed",
        "worktrees_dir": str(configured_worktrees_dir),
        "worktrees_dir_inside_repo": _is_within(primary_root, configured_worktrees_dir),
        "worktrees": worktrees,
    }


def primary_worktree_is_dirty(profile: Profile, *, ignore_runtime: bool = True) -> bool:
    primary_root = find_primary_worktree(profile.paths.project_root)
    return _status_dirty(primary_root, ignore_runtime=ignore_runtime)


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

    if _status_dirty(target_worktree, ignore_runtime=True):
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
        if _status_dirty(branch_worktree, ignore_runtime=True):
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
    primary = "yes" if payload["current_is_primary"] else f"no (hint: {payload['primary_worktree']})"
    location = "inside repo" if payload["worktrees_dir_inside_repo"] else "outside repo"
    lines = [
        f"[blackdog-worktree] preflight: {payload['repo_root']} (branch: {payload['current_branch']}, dirty: {dirty})",
        f"[blackdog-worktree] cwd: {payload['current_worktree']}",
        f"[blackdog-worktree] primary worktree: {primary}",
        f"[blackdog-worktree] model: {payload['worktree_model']}",
        f"[blackdog-worktree] implementation dirty: {implementation_dirty}",
        f"[blackdog-worktree] worktrees dir: {payload['worktrees_dir']} ({location})",
    ]
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
