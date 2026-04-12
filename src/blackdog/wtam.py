from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import shlex
import os
import subprocess
import sys
import time
import tomllib

from blackdog_core.backlog import BacklogError, TaskSpec, Workset, find_workset, finish_task, load_planning_state, start_task
from blackdog_core.profile import RepoProfile, slugify
from blackdog_core.state import (
    ValidationRecord,
    active_task_attempt,
    append_event,
    create_prompt_receipt,
    latest_task_attempt,
    load_runtime_state,
)


WTAM_WORKTREE_VE_NOTE = (
    ".VE is unversioned and bound to this worktree path; bootstrap one per worktree and do not reuse another "
    "worktree's .VE."
)
WORKSPACE_MODE_GIT_WORKTREE = "git-worktree"
WORKTREE_ROLE_PRIMARY = "primary"
WORKTREE_ROLE_TASK = "task"
WORKTREE_ROLE_LINKED = "linked"
WORKTREE_BOOTSTRAP_EDITABLE = "editable-worktree-source"
WORKTREE_BOOTSTRAP_SHIM = "launcher-shim"
WORKTREE_BOOTSTRAP_PROXY = "launcher-proxy"
WORKTREE_BOOTSTRAP_REUSE = "reuse-existing"


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
    ) -> None:
        self.primary_worktree = str(primary_worktree)
        self.branch = branch
        self.target_branch = target_branch
        self.dirty_paths = tuple(dirty_paths)
        dirty_text = ", ".join(self.dirty_paths) or "none detected"
        super().__init__(
            "dirty primary worktree contract violation: "
            f"{self.primary_worktree} has uncommitted changes blocking landing {self.branch} into {self.target_branch}; "
            f"dirty paths: {dirty_text}; "
            "clean up or land the primary worktree changes and retry without using git stash"
        )


@dataclass(frozen=True, slots=True)
class WorktreeSpec:
    workset_id: str
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
    attempt_id: str
    prompt_hash: str
    prompt_source: str | None
    workspace_ve: str
    workspace_blackdog_path: str
    bootstrap_mode: str
    bootstrap_source_root: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContractDocument:
    path: str
    kind: str
    text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WorktreeBootstrapPlan:
    ve_path: str
    blackdog_path: str
    mode: str
    source_root: str | None
    note: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WorktreePreview:
    workset_id: str
    task_id: str
    task_title: str
    task_slug: str
    actor: str
    execution_model: str
    workspace_identity: str | None
    branch: str
    base_ref: str
    base_commit: str
    target_branch: str
    integration_branch: str
    worktree_path: str
    primary_worktree: str
    current_worktree: str
    model: str | None
    reasoning_effort: str | None
    note: str | None
    prompt_hash: str
    prompt_source: str | None
    prompt_text: str | None
    task_paths: tuple[str, ...]
    task_docs: tuple[str, ...]
    task_checks: tuple[str, ...]
    validation_commands: tuple[str, ...]
    doc_routing_defaults: tuple[str, ...]
    contract_documents: tuple[ContractDocument, ...]
    bootstrap: WorktreeBootstrapPlan
    existing_branch_worktree: str | None
    path_exists: bool
    start_ready: bool
    conflicts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["contract_documents"] = [item.to_dict() for item in self.contract_documents]
        payload["bootstrap"] = self.bootstrap.to_dict()
        return payload


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
    for row in _parse_worktree_list(repo_root):
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


def find_worktree_for_branch(profile: RepoProfile, branch: str) -> str | None:
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


def _runtime_ignore_prefixes(profile: RepoProfile, *, repo_root: Path | None = None) -> tuple[str, ...]:
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
    dirty: list[str] = []
    for candidates in rows:
        for candidate in candidates:
            if candidate in ignore_paths:
                continue
            if any(candidate.startswith(prefix) for prefix in ignore_prefixes):
                continue
            dirty.append(candidate)
    return sorted(dict.fromkeys(dirty))


def _status_dirty(
    repo_root: Path,
    *,
    ignore_paths: frozenset[str] = frozenset(),
    ignore_prefixes: tuple[str, ...] = (),
) -> bool:
    return bool(dirty_paths(repo_root, ignore_paths=ignore_paths, ignore_prefixes=ignore_prefixes))


def _current_branch(repo_root: Path) -> str:
    branch = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise WorktreeError(f"detached HEAD at {repo_root}; specify --from explicitly")
    return branch


def _require_workset_and_task(profile: RepoProfile, *, workset_id: str, task_id: str) -> tuple[Workset, TaskSpec]:
    planning_state = load_planning_state(profile.paths)
    workset = find_workset(planning_state, workset_id)
    if workset is None:
        raise BacklogError(f"Unknown workset {workset_id!r}")
    for task in workset.tasks:
        if task.task_id == task_id:
            return workset, task
    raise BacklogError(f"Unknown task {task_id!r} in workset {workset_id!r}")


def _task_slug(workset_id: str, task: TaskSpec) -> str:
    return slugify(f"{workset_id}-{task.task_id}-{task.title}")


def _run_command(*args: str, cwd: Path | None = None) -> None:
    completed = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        rendered = " ".join(args)
        raise WorktreeError(f"{rendered} failed: {detail}")


def _project_skill_path(project_root: Path) -> Path | None:
    candidate = (project_root / ".codex" / "skills" / "blackdog" / "SKILL.md").resolve()
    return candidate if candidate.is_file() else None


def _looks_like_blackdog_source_checkout(root: Path) -> bool:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    if not (root / "src" / "blackdog_cli" / "main.py").is_file():
        return False
    try:
        payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return str((payload.get("project") or {}).get("name") or "") == "blackdog"


def _current_blackdog_source_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    if _looks_like_blackdog_source_checkout(candidate):
        return candidate
    return None


def _current_blackdog_executable() -> str | None:
    candidate = (Path(sys.executable).resolve().parent / "blackdog").resolve()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _bootstrap_plan_for_worktree(profile: RepoProfile, *, worktree_path: Path) -> WorktreeBootstrapPlan:
    blackdog_path = (worktree_path / ".VE" / "bin" / "blackdog").resolve()
    if blackdog_path.is_file() and os.access(blackdog_path, os.X_OK):
        return WorktreeBootstrapPlan(
            ve_path=str((worktree_path / ".VE").resolve()),
            blackdog_path=str(blackdog_path),
            mode=WORKTREE_BOOTSTRAP_REUSE,
            source_root=None,
            note="reuse the existing worktree-local Blackdog CLI",
        )
    if _looks_like_blackdog_source_checkout(profile.paths.project_root):
        return WorktreeBootstrapPlan(
            ve_path=str((worktree_path / ".VE").resolve()),
            blackdog_path=str(blackdog_path),
            mode=WORKTREE_BOOTSTRAP_EDITABLE,
            source_root=str(worktree_path.resolve()),
            note="install editable Blackdog from the task worktree source checkout",
        )
    source_root = _current_blackdog_source_root()
    if source_root is not None:
        return WorktreeBootstrapPlan(
            ve_path=str((worktree_path / ".VE").resolve()),
            blackdog_path=str(blackdog_path),
            mode=WORKTREE_BOOTSTRAP_SHIM,
            source_root=str(source_root),
            note="create a worktree-local launcher shim backed by the current Blackdog source checkout",
        )
    current_executable = _current_blackdog_executable()
    return WorktreeBootstrapPlan(
        ve_path=str((worktree_path / ".VE").resolve()),
        blackdog_path=str(blackdog_path),
        mode=WORKTREE_BOOTSTRAP_PROXY,
        source_root=current_executable,
        note="create a worktree-local launcher proxy to the currently running Blackdog CLI",
    )


def _contract_documents(
    profile: RepoProfile,
    *,
    expand_text: bool = False,
) -> tuple[ContractDocument, ...]:
    candidates: list[tuple[str, Path]] = []
    skill_path = _project_skill_path(profile.paths.project_root)
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
        text = candidate.read_text(encoding="utf-8") if expand_text else None
        documents.append(
            ContractDocument(
                path=str(candidate),
                kind=kind,
                text=text,
            )
        )
    return tuple(documents)


def _ensure_workspace_ve(plan: WorktreeBootstrapPlan) -> None:
    ve_path = Path(plan.ve_path).resolve()
    blackdog_path = Path(plan.blackdog_path).resolve()
    if plan.mode == WORKTREE_BOOTSTRAP_REUSE:
        return
    ve_path.parent.mkdir(parents=True, exist_ok=True)
    if not (ve_path / "bin" / "python").is_file():
        _run_command(sys.executable, "-m", "venv", str(ve_path))
    workspace_python = (ve_path / "bin" / "python").resolve()
    if not workspace_python.is_file():
        raise WorktreeError(f"expected worktree-local python at {workspace_python}")
    if plan.mode == WORKTREE_BOOTSTRAP_EDITABLE:
        source_root = Path(str(plan.source_root or "")).resolve()
        _run_command(str(workspace_python), "-m", "pip", "install", "-e", str(source_root))
        return
    blackdog_path.parent.mkdir(parents=True, exist_ok=True)
    if plan.mode == WORKTREE_BOOTSTRAP_SHIM:
        source_root = Path(str(plan.source_root or "")).resolve()
        script = (
            "#!/bin/sh\n"
            f"PYTHONPATH={shlex.quote(str((source_root / 'src').resolve()))}"
            '${PYTHONPATH:+":$PYTHONPATH"} '
            f"exec {shlex.quote(str(workspace_python))} -m blackdog_cli \"$@\"\n"
        )
    else:
        target = str(plan.source_root or "").strip()
        if not target:
            raise WorktreeError("could not determine a Blackdog launcher target for the worktree")
        script = (
            "#!/bin/sh\n"
            f"exec {shlex.quote(target)} \"$@\"\n"
        )
    blackdog_path.write_text(script, encoding="utf-8")
    blackdog_path.chmod(0o755)


def preview_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    prompt: str,
    prompt_source: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    branch: str | None = None,
    from_ref: str | None = None,
    path: str | None = None,
    note: str | None = None,
    include_prompt: bool = False,
    expand_contract: bool = False,
) -> WorktreePreview:
    workset, task = _require_workset_and_task(profile, workset_id=workset_id, task_id=task_id)
    current_root = _repo_root(profile.paths.project_root)
    primary_root = find_primary_worktree(profile.paths.project_root)
    target_branch = str(workset.branch_intent.get("target_branch") or "").strip() or _current_branch(primary_root)
    base_ref = _resolve_from_ref(primary_root, from_ref, default_branch=target_branch)
    base_commit = _run_git(primary_root, "rev-parse", f"{base_ref}^{{commit}}")
    resolved_branch = branch or default_task_branch(workset_id, task)
    integration_branch = (
        str(workset.branch_intent.get("integration_branch") or resolved_branch).strip() or resolved_branch
    )
    worktree_path = Path(path).resolve() if path else default_task_worktree_path(profile, workset_id=workset_id, task=task).resolve()
    existing_worktree = _find_worktree_for_branch(primary_root, f"refs/heads/{resolved_branch}")
    prompt_receipt = create_prompt_receipt(prompt, source=prompt_source)
    conflicts: list[str] = []
    if existing_worktree is not None:
        conflicts.append(f"branch already has a worktree: {existing_worktree}")
    if _is_within(primary_root, worktree_path):
        conflicts.append(f"refusing worktree path inside the repository: {worktree_path}")
    elif worktree_path.exists():
        conflicts.append(f"worktree path already exists: {worktree_path}")
    bootstrap = _bootstrap_plan_for_worktree(profile, worktree_path=worktree_path)
    return WorktreePreview(
        workset_id=workset_id,
        task_id=task.task_id,
        task_title=task.title,
        task_slug=_task_slug(workset_id, task),
        actor=actor,
        execution_model="direct_wtam",
        workspace_identity=str(workset.workspace.get("identity") or "").strip() or None,
        branch=resolved_branch,
        base_ref=base_ref,
        base_commit=base_commit,
        target_branch=target_branch,
        integration_branch=integration_branch,
        worktree_path=str(worktree_path),
        primary_worktree=str(primary_root),
        current_worktree=str(current_root),
        model=model,
        reasoning_effort=reasoning_effort,
        note=note,
        prompt_hash=prompt_receipt.prompt_hash,
        prompt_source=prompt_receipt.source,
        prompt_text=prompt_receipt.text if include_prompt else None,
        task_paths=task.paths,
        task_docs=task.docs,
        task_checks=task.checks,
        validation_commands=profile.validation_commands,
        doc_routing_defaults=profile.doc_routing_defaults,
        contract_documents=_contract_documents(profile, expand_text=expand_contract),
        bootstrap=bootstrap,
        existing_branch_worktree=str(existing_worktree) if existing_worktree is not None else None,
        path_exists=worktree_path.exists(),
        start_ready=not conflicts,
        conflicts=tuple(conflicts),
    )


def default_task_branch(workset_id: str, task: TaskSpec) -> str:
    return f"agent/{_task_slug(workset_id, task)}"


def default_task_worktree_path(profile: RepoProfile, *, workset_id: str, task: TaskSpec) -> Path:
    return profile.paths.worktrees_dir / f"wt-{_task_slug(workset_id, task)}"


def _resolve_from_ref(primary_root: Path, from_ref: str | None, *, default_branch: str) -> str:
    if not from_ref:
        return default_branch
    if _run_git_no_check(primary_root, "rev-parse", "--verify", f"{from_ref}^{{commit}}").returncode == 0:
        return from_ref
    remote_ref = f"origin/{from_ref}"
    if _run_git_no_check(primary_root, "rev-parse", "--verify", f"{remote_ref}^{{commit}}").returncode == 0:
        return remote_ref
    raise WorktreeError(f"could not resolve --from ref: {from_ref} (try: git fetch --all --prune)")


def _is_task_branch(profile: RepoProfile, branch: str | None) -> bool:
    resolved = str(branch or "").strip()
    if not resolved:
        return False
    planning_state = load_planning_state(profile.paths)
    for workset in planning_state.worksets:
        for task in workset.tasks:
            if resolved == default_task_branch(workset.workset_id, task):
                return True
    return False


def worktree_contract(
    profile: RepoProfile,
    *,
    workspace: Path | None = None,
    workspace_mode: str | None = None,
) -> dict[str, Any]:
    resolved_workspace = _repo_root(workspace or profile.paths.project_root)
    primary_root = find_primary_worktree(profile.paths.project_root)
    current_is_primary = _is_primary_worktree(resolved_workspace)
    workspace_blackdog = resolved_workspace / ".VE" / "bin" / "blackdog"
    workspace_has_local_blackdog = workspace_blackdog.is_file() and os.access(workspace_blackdog, os.X_OK)
    target_branch = _current_branch(primary_root)
    current_branch = _run_git(resolved_workspace, "rev-parse", "--abbrev-ref", "HEAD")
    workspace_role = WORKTREE_ROLE_PRIMARY if current_is_primary else (
        WORKTREE_ROLE_TASK if _is_task_branch(profile, current_branch) else WORKTREE_ROLE_LINKED
    )
    return {
        "workspace_mode": workspace_mode or WORKSPACE_MODE_GIT_WORKTREE,
        "current_worktree": str(resolved_workspace),
        "current_branch": current_branch,
        "current_is_primary": current_is_primary,
        "workspace_role": workspace_role,
        "primary_worktree": str(primary_root),
        "primary_branch": target_branch,
        "target_branch": target_branch,
        "primary_dirty": _status_dirty(primary_root, ignore_prefixes=_runtime_ignore_prefixes(profile, repo_root=primary_root)),
        "primary_dirty_paths": dirty_paths(
            primary_root,
            ignore_prefixes=_runtime_ignore_prefixes(profile, repo_root=primary_root),
        ),
        "workspace_ve": str(resolved_workspace / ".VE"),
        "workspace_blackdog_path": str(workspace_blackdog),
        "workspace_has_local_blackdog": workspace_has_local_blackdog,
        "ve_expectation": WTAM_WORKTREE_VE_NOTE,
    }


def worktree_preflight(profile: RepoProfile, *, cwd: Path | None = None) -> dict[str, Any]:
    resolved_cwd = (cwd or Path.cwd()).resolve()
    current_root = _repo_root(resolved_cwd)
    contract = worktree_contract(profile, workspace=current_root)
    primary_root = Path(contract["primary_worktree"]).resolve()
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
        "workspace_role": contract["workspace_role"],
        "primary_worktree": contract["primary_worktree"],
        "primary_branch": contract["primary_branch"],
        "dirty": _status_dirty(current_root),
        "implementation_dirty": _status_dirty(current_root, ignore_prefixes=_runtime_ignore_prefixes(profile, repo_root=current_root)),
        "workspace_mode": contract["workspace_mode"],
        "target_branch": contract["target_branch"],
        "primary_dirty": contract["primary_dirty"],
        "landing_state": "blocked" if contract["primary_dirty"] else "ready",
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


def primary_worktree_is_dirty(profile: RepoProfile, *, ignore_runtime: bool = True) -> bool:
    primary_root = find_primary_worktree(profile.paths.project_root)
    ignore_prefixes = _runtime_ignore_prefixes(profile, repo_root=primary_root) if ignore_runtime else ()
    return _status_dirty(primary_root, ignore_prefixes=ignore_prefixes)


def primary_worktree_dirty_paths(profile: RepoProfile, *, ignore_runtime: bool = True) -> list[str]:
    primary_root = find_primary_worktree(profile.paths.project_root)
    ignore_prefixes = _runtime_ignore_prefixes(profile, repo_root=primary_root) if ignore_runtime else ()
    return dirty_paths(primary_root, ignore_prefixes=ignore_prefixes)


def dirty_primary_worktree_error(profile: RepoProfile, *, branch: str, target_branch: str | None = None) -> DirtyPrimaryWorktreeError:
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    return DirtyPrimaryWorktreeError(
        primary_worktree=primary_root,
        branch=branch,
        target_branch=resolved_target,
        dirty_paths=primary_worktree_dirty_paths(profile, ignore_runtime=True),
    )


def branch_changed_paths(profile: RepoProfile, *, branch: str, target_branch: str | None = None) -> list[str]:
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    completed = _run_git_no_check(primary_root, "diff", "--name-only", f"{resolved_target}..{branch}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git diff --name-only {resolved_target}..{branch} failed: {detail}")
    return sorted({line.strip() for line in completed.stdout.splitlines() if line.strip()})


def branch_ahead_of_target(profile: RepoProfile, *, branch: str, target_branch: str | None = None) -> bool:
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    completed = _run_git_no_check(primary_root, "rev-list", "--count", f"{resolved_target}..{branch}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git rev-list --count {resolved_target}..{branch} failed: {detail}")
    return int(completed.stdout.strip() or "0") > 0


def start_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    prompt: str,
    prompt_source: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    branch: str | None = None,
    from_ref: str | None = None,
    path: str | None = None,
    note: str | None = None,
) -> WorktreeSpec:
    preview = preview_task_worktree(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        actor=actor,
        prompt=prompt,
        prompt_source=prompt_source,
        model=model,
        reasoning_effort=reasoning_effort,
        branch=branch,
        from_ref=from_ref,
        path=path,
        note=note,
        include_prompt=False,
        expand_contract=False,
    )
    if not preview.start_ready:
        raise WorktreeError("; ".join(preview.conflicts))
    primary_root = Path(preview.primary_worktree).resolve()
    worktree_path = Path(preview.worktree_path).resolve()
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    completed = _run_git_no_check(
        primary_root,
        "worktree",
        "add",
        str(worktree_path),
        "-b",
        preview.branch,
        preview.base_ref,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git worktree add failed: {detail}")
    try:
        _ensure_workspace_ve(preview.bootstrap)
        attempt = start_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor=actor,
            prompt_receipt=create_prompt_receipt(prompt, source=prompt_source),
            workspace_identity=preview.workspace_identity,
            workspace_mode=WORKSPACE_MODE_GIT_WORKTREE,
            worktree_role=WORKTREE_ROLE_TASK,
            worktree_path=str(worktree_path),
            branch=preview.branch,
            target_branch=preview.target_branch,
            integration_branch=preview.integration_branch,
            start_commit=preview.base_commit,
            model=model,
            reasoning_effort=reasoning_effort,
            note=note,
        )
    except Exception:
        _run_git_no_check(primary_root, "worktree", "remove", "--force", str(worktree_path))
        _run_git_no_check(primary_root, "branch", "-D", preview.branch)
        raise
    spec = WorktreeSpec(
        workset_id=preview.workset_id,
        task_id=preview.task_id,
        task_title=preview.task_title,
        task_slug=preview.task_slug,
        branch=preview.branch,
        base_ref=preview.base_ref,
        base_commit=preview.base_commit,
        target_branch=preview.target_branch,
        worktree_path=str(worktree_path),
        primary_worktree=preview.primary_worktree,
        current_worktree=preview.current_worktree,
        attempt_id=attempt.attempt_id,
        prompt_hash=preview.prompt_hash,
        prompt_source=preview.prompt_source,
        workspace_ve=preview.bootstrap.ve_path,
        workspace_blackdog_path=preview.bootstrap.blackdog_path,
        bootstrap_mode=preview.bootstrap.mode,
        bootstrap_source_root=preview.bootstrap.source_root,
    )
    append_event(
        profile.paths.events_file,
        event_type="worktree.start",
        actor=actor,
        payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt.attempt_id,
            "branch": preview.branch,
            "target_branch": preview.target_branch,
            "base_ref": preview.base_ref,
            "base_commit": preview.base_commit,
            "worktree_path": str(worktree_path),
            "prompt_hash": preview.prompt_hash,
            "prompt_source": preview.prompt_source,
            "workspace_blackdog_path": preview.bootstrap.blackdog_path,
            "bootstrap_mode": preview.bootstrap.mode,
        },
    )
    return spec


def land_branch(
    profile: RepoProfile,
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
        if _status_dirty(target_worktree, ignore_prefixes=_runtime_ignore_prefixes(profile, repo_root=target_worktree)):
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
            if _status_dirty(branch_worktree, ignore_prefixes=_runtime_ignore_prefixes(profile, repo_root=branch_worktree)):
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
            "diff_file": None,
            "diffstat_file": None,
            "cleanup": cleanup,
            "cleaned_worktree": cleaned_worktree,
            "deleted_branch": deleted_branch,
            "removed_temporary_target": removed_target,
        }
    except Exception:
        if created_target and target_worktree.exists():
            _run_git_no_check(primary_root, "worktree", "remove", "--force", str(target_worktree))
        raise


def land_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    summary: str | None = None,
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
) -> dict[str, Any]:
    runtime_state = load_runtime_state(profile.paths)
    attempt = active_task_attempt(runtime_state, workset_id, task_id)
    if attempt is None:
        raise BacklogError(f"No active WTAM attempt for task {task_id!r} in workset {workset_id!r}")
    if attempt.branch is None:
        raise WorktreeError(f"active attempt {attempt.attempt_id} is missing its branch")
    if attempt.target_branch is None:
        raise WorktreeError(f"active attempt {attempt.attempt_id} is missing its target_branch")
    branch_head_commit = _run_git(find_primary_worktree(profile.paths.project_root), "rev-parse", attempt.branch)
    changed = tuple(branch_changed_paths(profile, branch=attempt.branch, target_branch=attempt.target_branch))
    landing = land_branch(profile, branch=attempt.branch, target_branch=attempt.target_branch, cleanup=False)
    finished = finish_task(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt.attempt_id,
        actor=actor,
        status="success",
        summary=summary,
        changed_paths=changed,
        validations=validations,
        residuals=residuals,
        followup_candidates=followup_candidates,
        commit=branch_head_commit,
        landed_commit=str(landing["landed_commit"]),
        note=note,
    )
    append_event(
        profile.paths.events_file,
        event_type="worktree.land",
        actor=actor,
        payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt.attempt_id,
            "branch": attempt.branch,
            "target_branch": attempt.target_branch,
            "landed_commit": landing["landed_commit"],
            "changed_paths": list(changed),
        },
    )
    return {
        **landing,
        "attempt_id": finished.attempt_id,
        "task_id": finished.task_id,
        "commit": branch_head_commit,
        "changed_paths": list(changed),
    }


def cleanup_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    path: str | None = None,
    branch: str | None = None,
) -> dict[str, Any]:
    _workset, task = _require_workset_and_task(profile, workset_id=workset_id, task_id=task_id)
    primary_root = find_primary_worktree(profile.paths.project_root)
    runtime_state = load_runtime_state(profile.paths)
    latest_attempt = latest_task_attempt(runtime_state, workset_id, task_id)
    resolved_branch = (
        branch
        or (latest_attempt.branch if latest_attempt is not None else None)
        or default_task_branch(workset_id, task)
    )
    resolved_path: Path
    if path is not None:
        resolved_path = Path(path).resolve()
    elif latest_attempt is not None and latest_attempt.worktree_path:
        resolved_path = Path(latest_attempt.worktree_path).resolve()
    else:
        resolved_path = default_task_worktree_path(profile, workset_id=workset_id, task=task).resolve()
    if not resolved_path.exists():
        raise WorktreeError(f"worktree path not found: {resolved_path}")
    _run_git(primary_root, "worktree", "remove", str(resolved_path))
    deleted_branch = False
    if resolved_branch:
        delete = _run_git_no_check(primary_root, "branch", "-d", resolved_branch)
        if delete.returncode == 0:
            deleted_branch = True
        elif "not found" not in (delete.stderr or "").lower():
            detail = delete.stderr.strip() or delete.stdout.strip() or f"exit code {delete.returncode}"
            raise WorktreeError(f"git branch -d {resolved_branch} failed: {detail}")
    append_event(
        profile.paths.events_file,
        event_type="worktree.cleanup",
        payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "branch": resolved_branch,
            "worktree_path": str(resolved_path),
            "deleted_branch": deleted_branch,
        },
    )
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
        f"[blackdog-worktree] workspace role: {payload['workspace_role']}",
        f"[blackdog-worktree] primary worktree: {primary}",
        f"[blackdog-worktree] workspace mode: {payload['workspace_mode']}",
        f"[blackdog-worktree] target branch: {payload['target_branch']}",
        f"[blackdog-worktree] landing state: {payload['landing_state']}",
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


def render_preview_text(
    preview: WorktreePreview,
    *,
    show_prompt: bool = False,
    expand_contract: bool = False,
) -> str:
    lines = [
        f"[blackdog-worktree] preview: {preview.task_id} {preview.task_title}",
        f"[blackdog-worktree] actor: {preview.actor} exec={preview.execution_model}",
        f"[blackdog-worktree] branch: {preview.branch}",
        f"[blackdog-worktree] base: {preview.base_ref} ({preview.base_commit})",
        f"[blackdog-worktree] target branch: {preview.target_branch}",
        f"[blackdog-worktree] integration branch: {preview.integration_branch}",
        f"[blackdog-worktree] worktree: {preview.worktree_path}",
        f"[blackdog-worktree] workspace identity: {preview.workspace_identity or 'unset'}",
        f"[blackdog-worktree] prompt hash: {preview.prompt_hash}",
        f"[blackdog-worktree] prompt source: {preview.prompt_source or 'unspecified'}",
        f"[blackdog-worktree] bootstrap: {preview.bootstrap.mode} ({preview.bootstrap.note})",
        f"[blackdog-worktree] workspace CLI: {preview.bootstrap.blackdog_path}",
        f"[blackdog-worktree] start ready: {'yes' if preview.start_ready else 'no'}",
    ]
    if preview.model:
        lines.append(f"[blackdog-worktree] model: {preview.model}")
    if preview.reasoning_effort:
        lines.append(f"[blackdog-worktree] reasoning effort: {preview.reasoning_effort}")
    if preview.task_paths:
        lines.append(f"[blackdog-worktree] task paths: {', '.join(preview.task_paths)}")
    if preview.task_docs:
        lines.append(f"[blackdog-worktree] task docs: {', '.join(preview.task_docs)}")
    if preview.task_checks:
        lines.append(f"[blackdog-worktree] task checks: {', '.join(preview.task_checks)}")
    if preview.validation_commands:
        lines.append(f"[blackdog-worktree] default validations: {', '.join(preview.validation_commands)}")
    if preview.contract_documents:
        lines.append("[blackdog-worktree] repo contract inputs:")
        for document in preview.contract_documents:
            lines.append(f"  - {document.kind}: {document.path}")
    if preview.conflicts:
        lines.append(f"[blackdog-worktree] conflicts: {'; '.join(preview.conflicts)}")
    if show_prompt and preview.prompt_text is not None:
        lines.append("[blackdog-worktree] prompt text:")
        lines.extend(f"  {line}" for line in preview.prompt_text.splitlines())
    if expand_contract:
        for document in preview.contract_documents:
            if document.text is None:
                continue
            lines.append(f"[blackdog-worktree] contract text: {document.path}")
            lines.extend(f"  {line}" for line in document.text.splitlines())
    return "\n".join(lines) + "\n"


def render_start_text(spec: WorktreeSpec) -> str:
    lines = [
        f"[blackdog-worktree] created: {spec.worktree_path}",
        f"[blackdog-worktree] branch: {spec.branch}",
        f"[blackdog-worktree] base: {spec.base_ref} ({spec.base_commit})",
        f"[blackdog-worktree] target branch: {spec.target_branch}",
        f"[blackdog-worktree] task: {spec.task_id} {spec.task_title}",
        f"[blackdog-worktree] attempt: {spec.attempt_id}",
        f"[blackdog-worktree] prompt hash: {spec.prompt_hash}",
        f"[blackdog-worktree] prompt source: {spec.prompt_source or 'unspecified'}",
        f"[blackdog-worktree] workspace CLI: {spec.workspace_blackdog_path}",
        f"[blackdog-worktree] bootstrap: {spec.bootstrap_mode}",
    ]
    return "\n".join(lines) + "\n"


def render_land_text(payload: dict[str, Any]) -> str:
    lines = [
        f"[blackdog-worktree] landed: {payload['branch']} -> {payload['target_branch']}",
        f"[blackdog-worktree] target worktree: {payload['target_worktree']}",
        f"[blackdog-worktree] landed commit: {payload['landed_commit']}",
    ]
    if payload["changed_paths"]:
        lines.append(f"[blackdog-worktree] changed paths: {', '.join(payload['changed_paths'])}")
    return "\n".join(lines) + "\n"


def render_cleanup_text(payload: dict[str, Any]) -> str:
    lines = [f"[blackdog-worktree] removed: {payload['worktree_path']}"]
    if payload["branch"]:
        action = "deleted" if payload["deleted_branch"] else "kept"
        lines.append(f"[blackdog-worktree] branch: {payload['branch']} ({action})")
    return "\n".join(lines) + "\n"


__all__ = [
    "DirtyPrimaryWorktreeError",
    "WORKSPACE_MODE_GIT_WORKTREE",
    "WORKTREE_ROLE_LINKED",
    "WORKTREE_ROLE_PRIMARY",
    "WORKTREE_ROLE_TASK",
    "WTAM_WORKTREE_VE_NOTE",
    "WorktreeError",
    "WorktreeSpec",
    "branch_ahead_of_target",
    "branch_changed_paths",
    "cleanup_task_worktree",
    "default_task_branch",
    "default_task_worktree_path",
    "dirty_paths",
    "dirty_primary_worktree_error",
    "find_primary_worktree",
    "find_worktree_for_branch",
    "land_branch",
    "land_task_worktree",
    "primary_worktree_dirty_paths",
    "primary_worktree_is_dirty",
    "preview_task_worktree",
    "render_cleanup_text",
    "render_land_text",
    "render_preflight_text",
    "render_preview_text",
    "render_start_text",
    "start_task_worktree",
    "worktree_contract",
    "worktree_preflight",
]
