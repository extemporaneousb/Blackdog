from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import os
import subprocess
import time
import uuid

from blackdog.contract import ContractDocument, contract_documents
from blackdog.handlers import HandlerPlanSummary, execute_worktree_handlers, plan_worktree_handlers
from blackdog.prompting import tune_prompt
from blackdog_core.backlog import (
    BacklogError,
    TaskSpec,
    Workset,
    find_workset,
    finish_task,
    load_planning_state,
    start_task,
    upsert_workset,
)
from blackdog_core.profile import RepoProfile, slugify
from blackdog_core.state import (
    ATTEMPT_STATUS_ABANDONED,
    ATTEMPT_STATUS_BLOCKED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_SUCCESS,
    PROMPT_MODE_RAW,
    PROMPT_MODE_TUNED,
    PromptReceiptRecord,
    ValidationRecord,
    active_task_attempt,
    append_event,
    create_prompt_receipt,
    latest_task_attempt,
    load_runtime_state,
    parse_iso,
)


WTAM_WORKTREE_VE_NOTE = (
    ".VE is unversioned and bound to this worktree path; bootstrap one per worktree and do not reuse another "
    "worktree's .VE."
)
WORKSPACE_MODE_GIT_WORKTREE = "git-worktree"
WORKTREE_ROLE_PRIMARY = "primary"
WORKTREE_ROLE_TASK = "task"
WORKTREE_ROLE_LINKED = "linked"


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
    prompt_mode: str | None
    workspace_ve: str | None
    workspace_blackdog_path: str | None
    runtime_mode: str | None
    source_root: str | None
    source_mode: str | None
    script_policy: str | None
    handlers: HandlerPlanSummary

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["handlers"] = self.handlers.to_dict()
        return payload


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
    prompt_mode: str | None
    prompt_text: str | None
    task_paths: tuple[str, ...]
    task_docs: tuple[str, ...]
    task_checks: tuple[str, ...]
    validation_commands: tuple[str, ...]
    doc_routing_defaults: tuple[str, ...]
    contract_documents: tuple[ContractDocument, ...]
    handlers: HandlerPlanSummary
    existing_branch_worktree: str | None
    path_exists: bool
    start_ready: bool
    conflicts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["contract_documents"] = [item.to_dict() for item in self.contract_documents]
        payload["handlers"] = self.handlers.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class TaskBeginSpec:
    workset_id: str
    task_id: str
    task_title: str
    actor: str
    created_workset: bool
    prompt_mode: str
    user_prompt_hash: str
    user_prompt_source: str | None
    execution_prompt_hash: str
    execution_prompt_source: str | None
    execution_prompt_text: str | None
    worktree: WorktreeSpec

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["worktree"] = self.worktree.to_dict()
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


def _run_git_with_input(repo_root: Path, *args: str, input_text: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


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


def _derive_task_title(prompt: str) -> str:
    normalized = " ".join(str(prompt).split())
    if not normalized:
        return "Task"
    title = normalized[:72].rstrip()
    if len(normalized) > 72 and " " in title:
        title = title.rsplit(" ", 1)[0]
    return title.rstrip(" .") or "Task"


def _auto_task_workset_payload(profile: RepoProfile, *, prompt: str, title: str | None = None) -> dict[str, Any]:
    resolved_title = str(title or "").strip() or _derive_task_title(prompt)
    title_slug = slugify(resolved_title) or "task"
    workset_id = f"task-{title_slug}-{uuid.uuid4().hex[:8]}"
    primary_root = find_primary_worktree(profile.paths.project_root)
    target_branch = _current_branch(primary_root)
    return {
        "id": workset_id,
        "title": resolved_title,
        "scope": {"kind": "repo", "paths": []},
        "visibility": {"kind": "workset"},
        "policies": {"validation": list(profile.validation_commands)},
        "workspace": {
            "identity": workset_id,
            "exported_root": str(profile.paths.project_root),
        },
        "branch_intent": {
            "target_branch": target_branch,
            "integration_branch": target_branch,
        },
        "tasks": [
            {
                "id": "TASK-1",
                "title": resolved_title,
                "intent": resolved_title,
                "description": prompt,
                "docs": list(profile.doc_routing_defaults),
                "checks": list(profile.validation_commands),
                "metadata": {
                    "created_by": "task.begin",
                    "prompt_mode": PROMPT_MODE_RAW,
                },
            }
        ],
        "metadata": {
            "created_by": "task.begin",
        },
    }


def _resolve_task_begin_prompts(
    profile: RepoProfile,
    *,
    prompt: str,
    prompt_source: str | None,
    prompt_mode: str,
) -> tuple[PromptReceiptRecord, PromptReceiptRecord]:
    user_receipt = create_prompt_receipt(prompt, source=prompt_source, mode=PROMPT_MODE_RAW)
    if prompt_mode == PROMPT_MODE_TUNED:
        tuned = tune_prompt(
            profile,
            request=user_receipt.text,
            prompt_source=user_receipt.source,
        )
        execution_receipt = create_prompt_receipt(
            tuned.tuned_prompt,
            source=user_receipt.source,
            mode=PROMPT_MODE_TUNED,
        )
        return user_receipt, execution_receipt
    return user_receipt, user_receipt


def _attempt_matches_workspace(profile: RepoProfile, *, workspace_root: Path, attempt: Any) -> bool:
    if attempt.worktree_path and Path(attempt.worktree_path).resolve() == workspace_root:
        return True
    if attempt.branch:
        existing = find_worktree_for_branch(profile, attempt.branch)
        if existing and Path(existing).resolve() == workspace_root:
            return True
    return False


def _attempt_sort_key(attempt: Any) -> float:
    ended = parse_iso(attempt.ended_at)
    if ended is not None:
        return ended.timestamp()
    started = parse_iso(attempt.started_at)
    if started is not None:
        return started.timestamp()
    return 0.0


def _resolve_task_command_target(
    profile: RepoProfile,
    *,
    workset_id: str | None = None,
    task_id: str | None = None,
    cwd: Path | None = None,
    allow_latest: bool = False,
) -> tuple[str, str, Any | None]:
    resolved_workset = str(workset_id or "").strip() or None
    resolved_task = str(task_id or "").strip() or None
    runtime_state = load_runtime_state(profile.paths)
    if (resolved_workset is None) != (resolved_task is None):
        raise BacklogError("provide both --workset and --task, or neither when running inside a task worktree")
    if resolved_workset is not None and resolved_task is not None:
        attempt = active_task_attempt(runtime_state, resolved_workset, resolved_task)
        if attempt is None and allow_latest:
            attempt = latest_task_attempt(runtime_state, resolved_workset, resolved_task)
        if attempt is None and not allow_latest:
            raise BacklogError(f"No active WTAM attempt for task {resolved_task!r} in workset {resolved_workset!r}")
        return resolved_workset, resolved_task, attempt

    workspace_root = _repo_root((cwd or Path.cwd()).resolve())
    active_matches: list[tuple[str, Any]] = []
    latest_matches: list[tuple[str, Any]] = []
    for workset in runtime_state.worksets:
        for attempt in workset.attempts:
            if _attempt_matches_workspace(profile, workspace_root=workspace_root, attempt=attempt):
                latest_matches.append((workset.workset_id, attempt))
                if attempt.status == "in_progress":
                    active_matches.append((workset.workset_id, attempt))
    if len(active_matches) > 1:
        raise WorktreeError(f"multiple active task attempts are associated with {workspace_root}; specify --workset and --task")
    if active_matches:
        workset, attempt = active_matches[0]
        return workset, attempt.task_id, attempt
    if allow_latest and latest_matches:
        workset, attempt = max(latest_matches, key=lambda item: _attempt_sort_key(item[1]))
        return workset, attempt.task_id, attempt
    raise WorktreeError(f"could not infer a Blackdog task from {workspace_root}; specify --workset and --task")


def _task_surface_actions(actions: list[str]) -> list[str]:
    rewritten: list[str] = []
    for action in actions:
        text = action.replace("blackdog worktree land", "blackdog task land")
        text = text.replace(
            "blackdog worktree close --status blocked|failed|abandoned",
            "blackdog task close --status blocked|failed|abandoned",
        )
        text = text.replace("blackdog worktree cleanup", "blackdog task cleanup")
        rewritten.append(text)
    return rewritten


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


def preview_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    prompt: str,
    prompt_source: str | None = None,
    prompt_mode: str = PROMPT_MODE_RAW,
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
    prompt_receipt = create_prompt_receipt(prompt, source=prompt_source, mode=prompt_mode)
    conflicts: list[str] = []
    if existing_worktree is not None:
        conflicts.append(f"branch already has a worktree: {existing_worktree}")
    if _is_within(primary_root, worktree_path):
        conflicts.append(f"refusing worktree path inside the repository: {worktree_path}")
    elif worktree_path.exists():
        conflicts.append(f"worktree path already exists: {worktree_path}")
    handlers = plan_worktree_handlers(profile, worktree_path=worktree_path)
    if not handlers.ready:
        blocked = [action.message for action in handlers.actions if action.status == "blocked"]
        conflicts.extend(blocked)
        if handlers.remediation and handlers.remediation not in conflicts:
            conflicts.append(handlers.remediation)
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
        prompt_mode=prompt_receipt.mode,
        prompt_text=prompt_receipt.text if include_prompt else None,
        task_paths=task.paths,
        task_docs=task.docs,
        task_checks=task.checks,
        validation_commands=profile.validation_commands,
        doc_routing_defaults=profile.doc_routing_defaults,
        contract_documents=contract_documents(
            profile,
            expand_skill_text=expand_contract,
            expand_doc_text=expand_contract,
        ),
        handlers=handlers,
        existing_branch_worktree=str(existing_worktree) if existing_worktree is not None else None,
        path_exists=worktree_path.exists(),
        start_ready=not conflicts and handlers.ready,
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


def _resolve_attempt_worktree(profile: RepoProfile, *, branch: str | None, worktree_path: str | None) -> Path | None:
    if worktree_path:
        candidate = Path(worktree_path).resolve()
        if candidate.exists():
            return candidate
    if branch:
        existing = find_worktree_for_branch(profile, branch)
        if existing:
            candidate = Path(existing).resolve()
            if candidate.exists():
                return candidate
    return None


def _worktree_changed_paths(profile: RepoProfile, worktree_path: Path) -> list[str]:
    return dirty_paths(
        worktree_path,
        ignore_prefixes=_runtime_ignore_prefixes(profile, repo_root=worktree_path),
    )


def _attempt_changed_paths(
    profile: RepoProfile,
    *,
    branch: str | None,
    target_branch: str | None,
    worktree_path: Path | None,
) -> list[str]:
    changed: set[str] = set()
    if branch:
        try:
            changed.update(branch_changed_paths(profile, branch=branch, target_branch=target_branch))
        except WorktreeError:
            pass
    if worktree_path is not None and worktree_path.exists():
        changed.update(_worktree_changed_paths(profile, worktree_path))
    return sorted(changed)


def _canonical_commit_message(
    workset: Workset,
    task: TaskSpec,
    *,
    attempt_id: str,
    actor: str,
    prompt_hash: str | None,
    target_branch: str | None,
    status: str,
    summary: str,
    validations: tuple[ValidationRecord, ...],
    residuals: tuple[str, ...],
    followup_candidates: tuple[str, ...],
) -> str:
    subject = f"blackdog({workset.workset_id}/{task.task_id}): {task.title}"
    lines = [
        subject,
        "",
        summary.strip(),
        "",
        f"Blackdog-Workset: {workset.workset_id}",
        f"Blackdog-Task: {task.task_id}",
        f"Blackdog-Attempt: {attempt_id}",
        f"Blackdog-Actor: {actor}",
        f"Blackdog-Status: {status}",
    ]
    if target_branch:
        lines.append(f"Blackdog-Target-Branch: {target_branch}")
    if prompt_hash:
        lines.append(f"Blackdog-Prompt-Hash: {prompt_hash}")
    for validation in validations:
        lines.append(f"Blackdog-Validation: {validation.name}={validation.status}")
    for residual in residuals:
        lines.append(f"Blackdog-Residual: {residual}")
    for followup in followup_candidates:
        lines.append(f"Blackdog-Followup: {followup}")
    return "\n".join(lines).rstrip() + "\n"


def _landing_prep_commit_message(
    workset: Workset,
    task: TaskSpec,
    *,
    attempt_id: str,
) -> str:
    return (
        f"blackdog-wip({workset.workset_id}/{task.task_id}): prepare land\n\n"
        "Auto-commit task worktree changes so `blackdog worktree land` can create\n"
        "one canonical landed commit for the attempt.\n\n"
        f"Blackdog-Workset: {workset.workset_id}\n"
        f"Blackdog-Task: {task.task_id}\n"
        f"Blackdog-Attempt: {attempt_id}\n"
        "Blackdog-Status: staged-for-land\n"
    )


def _commit_dirty_attempt_worktree(
    profile: RepoProfile,
    *,
    workset: Workset,
    task: TaskSpec,
    branch: str | None,
    worktree_path: Path | None,
    attempt_id: str,
) -> str | None:
    if branch is None or worktree_path is None or not worktree_path.exists():
        return None
    if not _status_dirty(worktree_path, ignore_prefixes=_runtime_ignore_prefixes(profile, repo_root=worktree_path)):
        return None
    _run_git(worktree_path, "add", "-A")
    staged = _run_git_no_check(worktree_path, "diff", "--cached", "--quiet")
    if staged.returncode == 0:
        return None
    _run_git_with_input(
        worktree_path,
        "commit",
        "--quiet",
        "-F",
        "-",
        input_text=_landing_prep_commit_message(workset, task, attempt_id=attempt_id),
    )
    return _run_git(find_primary_worktree(profile.paths.project_root), "rev-parse", branch)


def start_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    prompt: str,
    prompt_source: str | None = None,
    prompt_mode: str = PROMPT_MODE_RAW,
    user_prompt_receipt: PromptReceiptRecord | None = None,
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
        prompt_mode=prompt_mode,
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
        handlers = execute_worktree_handlers(profile, worktree_path=worktree_path)
        if not handlers.ready:
            blocked = [action.message for action in handlers.actions if action.status == "blocked"]
            detail = "; ".join(blocked)
            if handlers.remediation:
                detail = "; ".join(item for item in [detail, handlers.remediation] if item)
            raise WorktreeError(detail or "worktree handler execution did not produce a ready workspace")
        attempt = start_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor=actor,
            prompt_receipt=create_prompt_receipt(prompt, source=prompt_source, mode=prompt_mode),
            user_prompt_receipt=user_prompt_receipt,
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
        prompt_mode=preview.prompt_mode,
        workspace_ve=handlers.worktree_ve_path,
        workspace_blackdog_path=handlers.blackdog_path,
        runtime_mode=handlers.runtime_mode,
        source_root=handlers.source_root,
        source_mode=handlers.source_mode,
        script_policy=handlers.script_policy,
        handlers=handlers,
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
            "prompt_mode": preview.prompt_mode,
            "user_prompt_hash": user_prompt_receipt.prompt_hash if user_prompt_receipt is not None else preview.prompt_hash,
            "user_prompt_source": user_prompt_receipt.source if user_prompt_receipt is not None else preview.prompt_source,
            "user_prompt_mode": user_prompt_receipt.mode if user_prompt_receipt is not None else preview.prompt_mode,
            "workspace_blackdog_path": handlers.blackdog_path,
            "runtime_mode": handlers.runtime_mode,
            "source_mode": handlers.source_mode,
            "script_policy": handlers.script_policy,
            "handler_actions": [action.to_dict() for action in handlers.actions],
        },
    )
    return spec


def begin_task_worktree(
    profile: RepoProfile,
    *,
    actor: str,
    prompt: str,
    prompt_source: str | None = None,
    prompt_mode: str = PROMPT_MODE_RAW,
    workset_id: str | None = None,
    task_id: str | None = None,
    title: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    branch: str | None = None,
    from_ref: str | None = None,
    path: str | None = None,
    note: str | None = None,
    include_prompt: bool = False,
) -> TaskBeginSpec:
    resolved_workset = str(workset_id or "").strip() or None
    resolved_task = str(task_id or "").strip() or None
    if prompt_mode not in {PROMPT_MODE_RAW, PROMPT_MODE_TUNED}:
        raise BacklogError(f"prompt mode must be one of {PROMPT_MODE_RAW}, {PROMPT_MODE_TUNED}")
    if (resolved_workset is None) != (resolved_task is None):
        raise BacklogError("task begin requires both --workset and --task when targeting existing planning state")

    user_receipt, execution_receipt = _resolve_task_begin_prompts(
        profile,
        prompt=prompt,
        prompt_source=prompt_source,
        prompt_mode=prompt_mode,
    )
    created_workset = False
    if resolved_workset is None:
        payload = _auto_task_workset_payload(profile, prompt=user_receipt.text, title=title)
        payload["tasks"][0]["metadata"]["prompt_mode"] = prompt_mode
        workset = upsert_workset(profile, payload)
        resolved_workset = workset.workset_id
        resolved_task = workset.tasks[0].task_id
        created_workset = True

    spec = start_task_worktree(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
        actor=actor,
        prompt=execution_receipt.text,
        prompt_source=execution_receipt.source,
        prompt_mode=prompt_mode,
        user_prompt_receipt=user_receipt,
        model=model,
        reasoning_effort=reasoning_effort,
        branch=branch,
        from_ref=from_ref,
        path=path,
        note=note,
    )
    return TaskBeginSpec(
        workset_id=resolved_workset,
        task_id=resolved_task,
        task_title=spec.task_title,
        actor=actor,
        created_workset=created_workset,
        prompt_mode=prompt_mode,
        user_prompt_hash=user_receipt.prompt_hash,
        user_prompt_source=user_receipt.source,
        execution_prompt_hash=execution_receipt.prompt_hash,
        execution_prompt_source=execution_receipt.source,
        execution_prompt_text=execution_receipt.text if include_prompt else None,
        worktree=spec,
    )


def show_task(
    profile: RepoProfile,
    *,
    workset_id: str | None = None,
    task_id: str | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    resolved_workset, resolved_task, _attempt = _resolve_task_command_target(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        cwd=cwd,
        allow_latest=True,
    )
    payload = inspect_task_worktree(profile, workset_id=resolved_workset, task_id=resolved_task)
    payload["recommended_actions"] = _task_surface_actions(list(payload["recommended_actions"]))
    return payload


def land_task(
    profile: RepoProfile,
    *,
    summary: str,
    actor: str | None = None,
    workset_id: str | None = None,
    task_id: str | None = None,
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
    cleanup: bool = True,
    cwd: Path | None = None,
) -> dict[str, Any]:
    resolved_workset, resolved_task, attempt = _resolve_task_command_target(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        cwd=cwd,
        allow_latest=False,
    )
    resolved_actor = str(actor or getattr(attempt, "actor", "")).strip() or None
    if resolved_actor is None:
        raise BacklogError("task land requires an active attempt actor")
    return land_task_worktree(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
        actor=resolved_actor,
        summary=summary,
        validations=validations,
        residuals=residuals,
        followup_candidates=followup_candidates,
        note=note,
        cleanup=cleanup,
    )


def close_task(
    profile: RepoProfile,
    *,
    status: str,
    summary: str,
    actor: str | None = None,
    workset_id: str | None = None,
    task_id: str | None = None,
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
    cleanup: bool = False,
    cwd: Path | None = None,
) -> dict[str, Any]:
    resolved_workset, resolved_task, attempt = _resolve_task_command_target(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        cwd=cwd,
        allow_latest=False,
    )
    resolved_actor = str(actor or getattr(attempt, "actor", "")).strip() or None
    if resolved_actor is None:
        raise BacklogError("task close requires an active attempt actor")
    return close_task_worktree(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
        actor=resolved_actor,
        status=status,
        summary=summary,
        validations=validations,
        residuals=residuals,
        followup_candidates=followup_candidates,
        note=note,
        cleanup=cleanup,
    )


def cleanup_task(
    profile: RepoProfile,
    *,
    workset_id: str | None = None,
    task_id: str | None = None,
    path: str | None = None,
    branch: str | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    resolved_workset, resolved_task, _attempt = _resolve_task_command_target(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        cwd=cwd,
        allow_latest=True,
    )
    return cleanup_task_worktree(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
        path=path,
        branch=branch,
    )


def land_branch(
    profile: RepoProfile,
    *,
    branch: str | None = None,
    target_branch: str | None = None,
    commit_message: str,
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
    landing_worktree: Path | None = None
    created_target = False
    created_landing = False
    try:
        if target_worktree is not None:
            if _status_dirty(target_worktree, ignore_prefixes=_runtime_ignore_prefixes(profile, repo_root=target_worktree)):
                if target_worktree == primary_root:
                    raise dirty_primary_worktree_error(profile, branch=resolved_branch, target_branch=resolved_target)
                raise WorktreeError(f"target worktree has uncommitted changes: {target_worktree}")
        else:
            target_worktree = (
                profile.paths.worktrees_dir / f"wt-land-{slugify(f'{resolved_target}-{int(time.time())}')}"
            ).resolve()
            target_worktree.parent.mkdir(parents=True, exist_ok=True)
            _run_git(primary_root, "worktree", "add", str(target_worktree), resolved_target)
            created_target = True

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

        changed_paths = branch_changed_paths(profile, branch=resolved_branch, target_branch=resolved_target)
        if not changed_paths:
            raise WorktreeError(f"cannot land: {resolved_branch} has no changes relative to {resolved_target}")

        if created_target:
            landing_worktree = target_worktree
        else:
            landing_worktree = (
                profile.paths.worktrees_dir / f"wt-land-{slugify(f'{resolved_target}-{int(time.time())}-shadow')}"
            ).resolve()
            landing_worktree.parent.mkdir(parents=True, exist_ok=True)
            _run_git(primary_root, "worktree", "add", "--detach", str(landing_worktree), resolved_target)
            created_landing = True

        completed = _run_git_no_check(landing_worktree, "merge", "--squash", "--no-commit", resolved_branch)
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
            raise WorktreeError(f"git merge --squash --no-commit {resolved_branch} failed: {detail}")
        _run_git_with_input(
            landing_worktree,
            "commit",
            "--quiet",
            "-F",
            "-",
            input_text=commit_message,
        )
        landed_commit = _run_git(landing_worktree, "rev-parse", "HEAD")
        if landing_worktree != target_worktree:
            _run_git(target_worktree, "merge", "--ff-only", landed_commit)

        cleaned_worktree: str | None = None
        deleted_branch = False
        branch_worktree = _find_worktree_for_branch(primary_root, f"refs/heads/{resolved_branch}")
        if cleanup and branch_worktree is not None and branch_worktree != target_worktree:
            if _status_dirty(branch_worktree, ignore_prefixes=_runtime_ignore_prefixes(profile, repo_root=branch_worktree)):
                raise WorktreeError(f"refusing cleanup: worktree has uncommitted changes: {branch_worktree}")
            _run_git(primary_root, "worktree", "remove", str(branch_worktree))
            cleaned_worktree = str(branch_worktree)
            _run_git(target_worktree, "branch", "-D", resolved_branch)
            deleted_branch = True

        removed_target = False
        if created_landing and landing_worktree is not None and landing_worktree.exists():
            _run_git(primary_root, "worktree", "remove", str(landing_worktree))
        if created_target and target_worktree.exists():
            _run_git(primary_root, "worktree", "remove", str(target_worktree))
            removed_target = True

        return {
            "branch": resolved_branch,
            "target_branch": resolved_target,
            "primary_worktree": str(primary_root),
            "target_worktree": str(target_worktree),
            "landing_worktree": str(landing_worktree),
            "landed_commit": landed_commit,
            "diff_file": None,
            "diffstat_file": None,
            "changed_paths": changed_paths,
            "cleanup": cleanup,
            "cleaned_worktree": cleaned_worktree,
            "deleted_branch": deleted_branch,
            "removed_temporary_target": removed_target,
        }
    except Exception:
        if created_landing and landing_worktree is not None and landing_worktree.exists():
            _run_git_no_check(primary_root, "worktree", "remove", "--force", str(landing_worktree))
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
    cleanup: bool = True,
) -> dict[str, Any]:
    workset, task = _require_workset_and_task(profile, workset_id=workset_id, task_id=task_id)
    runtime_state = load_runtime_state(profile.paths)
    attempt = active_task_attempt(runtime_state, workset_id, task_id)
    if attempt is None:
        raise BacklogError(f"No active WTAM attempt for task {task_id!r} in workset {workset_id!r}")
    if attempt.branch is None:
        raise WorktreeError(f"active attempt {attempt.attempt_id} is missing its branch")
    if attempt.target_branch is None:
        raise WorktreeError(f"active attempt {attempt.attempt_id} is missing its target_branch")
    resolved_summary = str(summary or "").strip() or task.title
    task_worktree = _resolve_attempt_worktree(
        profile,
        branch=attempt.branch,
        worktree_path=attempt.worktree_path,
    )
    prompt_hash = attempt.prompt_receipt.prompt_hash if attempt.prompt_receipt is not None else None
    branch_head_commit: str | None = None
    commit_message = _canonical_commit_message(
        workset,
        task,
        attempt_id=attempt.attempt_id,
        actor=actor,
        prompt_hash=prompt_hash,
        target_branch=attempt.target_branch,
        status="success",
        summary=resolved_summary,
        validations=validations,
        residuals=residuals,
        followup_candidates=followup_candidates,
    )
    try:
        prepared_commit = _commit_dirty_attempt_worktree(
            profile,
            workset=workset,
            task=task,
            branch=attempt.branch,
            worktree_path=task_worktree,
            attempt_id=attempt.attempt_id,
        )
        branch_head_commit = prepared_commit or _run_git(find_primary_worktree(profile.paths.project_root), "rev-parse", attempt.branch)
        landing = land_branch(
            profile,
            branch=attempt.branch,
            target_branch=attempt.target_branch,
            commit_message=commit_message,
            cleanup=cleanup,
        )
    except Exception as exc:
        payload = close_task_worktree(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor=actor,
            status=ATTEMPT_STATUS_BLOCKED,
            summary=f"Landing blocked: {exc}",
            validations=validations,
            residuals=residuals,
            followup_candidates=followup_candidates,
            note=note or str(exc),
            cleanup=False,
        )
        payload["error"] = str(exc)
        return payload

    changed = tuple(landing["changed_paths"])
    finished = finish_task(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt.attempt_id,
        actor=actor,
        status="success",
        summary=resolved_summary,
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
            "commit_message": commit_message,
            "cleanup": landing["cleanup"],
        },
    )
    return {
        **landing,
        "attempt_id": finished.attempt_id,
        "task_id": finished.task_id,
        "status": "success",
        "summary": resolved_summary,
        "commit": branch_head_commit,
        "commit_message": commit_message,
        "changed_paths": list(changed),
    }


def inspect_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
) -> dict[str, Any]:
    _workset, task = _require_workset_and_task(profile, workset_id=workset_id, task_id=task_id)
    runtime_state = load_runtime_state(profile.paths)
    active_attempt = active_task_attempt(runtime_state, workset_id, task_id)
    latest_attempt = latest_task_attempt(runtime_state, workset_id, task_id)
    selected_attempt = active_attempt or latest_attempt
    branch = selected_attempt.branch if selected_attempt is not None else None
    target_branch = selected_attempt.target_branch if selected_attempt is not None else None
    task_worktree = (
        _resolve_attempt_worktree(
            profile,
            branch=branch,
            worktree_path=selected_attempt.worktree_path if selected_attempt is not None else None,
        )
        if selected_attempt is not None
        else None
    )
    worktree_dirty_paths = _worktree_changed_paths(profile, task_worktree) if task_worktree is not None else []
    branch_ahead = (
        branch_ahead_of_target(profile, branch=branch, target_branch=target_branch)
        if branch and target_branch
        else False
    )
    recommended_actions: list[str] = []
    if active_attempt is None:
        recommended_actions.append("start a new WTAM attempt for this task")
    else:
        if worktree_dirty_paths or branch_ahead:
            recommended_actions.append("run `blackdog task land` to create the canonical landed commit")
        recommended_actions.append("run `blackdog task close --status blocked|failed|abandoned` to close without landing")
    if task_worktree is not None and not worktree_dirty_paths:
        recommended_actions.append("run `blackdog task cleanup` if the task workspace is no longer needed")
    return {
        "workset_id": workset_id,
        "task_id": task_id,
        "task_title": task.title,
        "active_attempt": active_attempt is not None,
        "attempt_id": selected_attempt.attempt_id if selected_attempt is not None else None,
        "latest_attempt_id": latest_attempt.attempt_id if latest_attempt is not None else None,
        "latest_attempt_status": latest_attempt.status if latest_attempt is not None else None,
        "latest_attempt_summary": latest_attempt.summary if latest_attempt is not None else None,
        "actor": selected_attempt.actor if selected_attempt is not None else None,
        "branch": branch,
        "target_branch": target_branch,
        "worktree_path": str(task_worktree) if task_worktree is not None else None,
        "worktree_exists": task_worktree is not None and task_worktree.exists(),
        "worktree_dirty": bool(worktree_dirty_paths),
        "worktree_dirty_paths": worktree_dirty_paths,
        "branch_ahead_of_target": branch_ahead,
        "changed_paths": _attempt_changed_paths(
            profile,
            branch=branch,
            target_branch=target_branch,
            worktree_path=task_worktree,
        ),
        "execution_prompt_hash": (
            selected_attempt.prompt_receipt.prompt_hash
            if selected_attempt is not None and selected_attempt.prompt_receipt is not None
            else None
        ),
        "execution_prompt_source": (
            selected_attempt.prompt_receipt.source
            if selected_attempt is not None and selected_attempt.prompt_receipt is not None
            else None
        ),
        "execution_prompt_mode": (
            selected_attempt.prompt_receipt.mode
            if selected_attempt is not None and selected_attempt.prompt_receipt is not None
            else None
        ),
        "user_prompt_hash": (
            selected_attempt.user_prompt_receipt.prompt_hash
            if selected_attempt is not None and selected_attempt.user_prompt_receipt is not None
            else None
        ),
        "user_prompt_source": (
            selected_attempt.user_prompt_receipt.source
            if selected_attempt is not None and selected_attempt.user_prompt_receipt is not None
            else None
        ),
        "user_prompt_mode": (
            selected_attempt.user_prompt_receipt.mode
            if selected_attempt is not None and selected_attempt.user_prompt_receipt is not None
            else None
        ),
        "prompt_hash": (
            selected_attempt.prompt_receipt.prompt_hash
            if selected_attempt is not None and selected_attempt.prompt_receipt is not None
            else None
        ),
        "prompt_source": (
            selected_attempt.prompt_receipt.source
            if selected_attempt is not None and selected_attempt.prompt_receipt is not None
            else None
        ),
        "started_at": selected_attempt.started_at if selected_attempt is not None else None,
        "ended_at": selected_attempt.ended_at if selected_attempt is not None else None,
        "primary_worktree": str(find_primary_worktree(profile.paths.project_root)),
        "primary_dirty": primary_worktree_is_dirty(profile, ignore_runtime=True),
        "primary_dirty_paths": primary_worktree_dirty_paths(profile, ignore_runtime=True),
        "recommended_actions": recommended_actions,
    }


def close_task_worktree(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    status: str,
    summary: str,
    validations: tuple[ValidationRecord, ...] = (),
    residuals: tuple[str, ...] = (),
    followup_candidates: tuple[str, ...] = (),
    note: str | None = None,
    cleanup: bool = False,
) -> dict[str, Any]:
    runtime_state = load_runtime_state(profile.paths)
    attempt = active_task_attempt(runtime_state, workset_id, task_id)
    if attempt is None:
        raise BacklogError(f"No active WTAM attempt for task {task_id!r} in workset {workset_id!r}")
    resolved_summary = str(summary or "").strip() or f"{status} {task_id}"
    task_worktree = _resolve_attempt_worktree(
        profile,
        branch=attempt.branch,
        worktree_path=attempt.worktree_path,
    )
    changed = tuple(
        _attempt_changed_paths(
            profile,
            branch=attempt.branch,
            target_branch=attempt.target_branch,
            worktree_path=task_worktree,
        )
    )
    branch_head_commit: str | None = None
    if attempt.branch:
        completed = _run_git_no_check(find_primary_worktree(profile.paths.project_root), "rev-parse", attempt.branch)
        if completed.returncode == 0:
            branch_head_commit = completed.stdout.strip() or None
    finished = finish_task(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        attempt_id=attempt.attempt_id,
        actor=actor,
        status=status,
        summary=resolved_summary,
        changed_paths=changed,
        validations=validations,
        residuals=residuals,
        followup_candidates=followup_candidates,
        commit=branch_head_commit,
        note=note,
    )
    cleanup_reason: str | None = None
    cleanup_payload: dict[str, Any] | None = None
    if cleanup and task_worktree is not None and task_worktree.exists():
        if _status_dirty(task_worktree, ignore_prefixes=_runtime_ignore_prefixes(profile, repo_root=task_worktree)):
            cleanup_reason = f"cleanup skipped because the task worktree is dirty: {task_worktree}"
        else:
            try:
                cleanup_payload = cleanup_task_worktree(
                    profile,
                    workset_id=workset_id,
                    task_id=task_id,
                    path=str(task_worktree),
                    branch=attempt.branch,
                )
            except WorktreeError as exc:
                cleanup_reason = str(exc)
    append_event(
        profile.paths.events_file,
        event_type="worktree.close",
        actor=actor,
        payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": attempt.attempt_id,
            "status": status,
            "summary": resolved_summary,
            "branch": attempt.branch,
            "target_branch": attempt.target_branch,
            "worktree_path": str(task_worktree) if task_worktree is not None else None,
            "changed_paths": list(changed),
            "commit": branch_head_commit,
            "cleanup_requested": cleanup,
            "cleanup_performed": cleanup_payload is not None,
            "cleanup_reason": cleanup_reason,
        },
    )
    return {
        "workset_id": workset_id,
        "task_id": task_id,
        "attempt_id": finished.attempt_id,
        "status": finished.status,
        "summary": resolved_summary,
        "branch": finished.branch,
        "target_branch": finished.target_branch,
        "worktree_path": str(task_worktree) if task_worktree is not None else None,
        "changed_paths": list(changed),
        "commit": branch_head_commit,
        "cleanup_requested": cleanup,
        "cleanup_performed": cleanup_payload is not None,
        "cleanup_reason": cleanup_reason,
        "cleanup": cleanup_payload,
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
        can_force_delete = (
            latest_attempt is not None
            and latest_attempt.branch == resolved_branch
            and latest_attempt.status == ATTEMPT_STATUS_SUCCESS
            and latest_attempt.landed_commit is not None
        )
        delete = _run_git_no_check(primary_root, "branch", "-d", resolved_branch)
        if delete.returncode == 0:
            deleted_branch = True
        else:
            detail_text = "\n".join(item for item in [delete.stderr, delete.stdout] if item).lower()
            if "not found" in detail_text:
                pass
            elif "not fully merged" in detail_text and can_force_delete:
                forced = _run_git_no_check(primary_root, "branch", "-D", resolved_branch)
                if forced.returncode == 0:
                    deleted_branch = True
                else:
                    detail = forced.stderr.strip() or forced.stdout.strip() or f"exit code {forced.returncode}"
                    raise WorktreeError(f"git branch -D {resolved_branch} failed: {detail}")
            else:
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
        f"[blackdog-worktree] prompt mode: {preview.prompt_mode or 'unset'}",
        f"[blackdog-worktree] runtime mode: {preview.handlers.runtime_mode or 'unset'}",
        f"[blackdog-worktree] workspace CLI: {preview.handlers.blackdog_path or 'missing'}",
        f"[blackdog-worktree] start ready: {'yes' if preview.start_ready else 'no'}",
    ]
    if preview.handlers.script_policy:
        lines.append(f"[blackdog-worktree] script policy: {preview.handlers.script_policy}")
    if preview.handlers.source_mode:
        lines.append(f"[blackdog-worktree] source mode: {preview.handlers.source_mode}")
    if preview.handlers.source_root:
        lines.append(f"[blackdog-worktree] source root: {preview.handlers.source_root}")
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
    if preview.handlers.actions:
        lines.append("[blackdog-worktree] handler plan:")
        for action in preview.handlers.actions:
            target = f" -> {action.target_path}" if action.target_path else ""
            lines.append(f"  - {action.handler_id}: {action.action} {action.status}{target} ({action.message})")
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


def render_start_text(spec: WorktreeSpec, *, surface: str = "worktree") -> str:
    prefix = f"[blackdog-{surface}]"
    lines = [
        f"{prefix} created: {spec.worktree_path}",
        f"{prefix} branch: {spec.branch}",
        f"{prefix} base: {spec.base_ref} ({spec.base_commit})",
        f"{prefix} target branch: {spec.target_branch}",
        f"{prefix} task: {spec.task_id} {spec.task_title}",
        f"{prefix} attempt: {spec.attempt_id}",
        f"{prefix} prompt hash: {spec.prompt_hash}",
        f"{prefix} prompt source: {spec.prompt_source or 'unspecified'}",
        f"{prefix} prompt mode: {spec.prompt_mode or 'unset'}",
        f"{prefix} workspace CLI: {spec.workspace_blackdog_path or 'missing'}",
        f"{prefix} runtime mode: {spec.runtime_mode or 'unset'}",
    ]
    if spec.script_policy:
        lines.append(f"{prefix} script policy: {spec.script_policy}")
    if spec.source_mode:
        lines.append(f"{prefix} source mode: {spec.source_mode}")
    if spec.source_root:
        lines.append(f"{prefix} source root: {spec.source_root}")
    if spec.handlers.actions:
        lines.append(f"{prefix} handler results:")
        for action in spec.handlers.actions:
            target = f" -> {action.target_path}" if action.target_path else ""
            timing = "" if action.elapsed_ms is None else f" [{action.elapsed_ms}ms]"
            lines.append(
                f"  - {action.handler_id}: {action.action} {action.status}{target}{timing} ({action.message})"
            )
    return "\n".join(lines) + "\n"


def render_land_text(payload: dict[str, Any], *, surface: str = "worktree") -> str:
    prefix = f"[blackdog-{surface}]"
    workspace_label = "task workspace" if surface == "task" else "worktree"
    target_label = "checkout" if surface == "task" else "worktree"
    if payload.get("status") and payload["status"] != "success":
        return render_close_text(payload, surface=surface)
    lines = [
        f"{prefix} landed: {payload['branch']} -> {payload['target_branch']}",
        f"{prefix} target {target_label}: {payload['target_worktree']}",
        f"{prefix} landed commit: {payload['landed_commit']}",
    ]
    if payload["changed_paths"]:
        lines.append(f"{prefix} changed paths: {', '.join(payload['changed_paths'])}")
    if payload.get("cleaned_worktree"):
        lines.append(f"{prefix} removed {workspace_label}: {payload['cleaned_worktree']}")
    if payload.get("deleted_branch"):
        lines.append(f"{prefix} deleted branch: {payload['branch']}")
    return "\n".join(lines) + "\n"


def render_task_begin_text(spec: TaskBeginSpec, *, show_prompt: bool = False) -> str:
    lines = [
        f"[blackdog-task] begin: {spec.workset_id}/{spec.task_id} actor={spec.actor}",
        f"[blackdog-task] created workset: {'yes' if spec.created_workset else 'no'}",
        f"[blackdog-task] prompt mode: {spec.prompt_mode}",
        f"[blackdog-task] user prompt hash: {spec.user_prompt_hash}",
        f"[blackdog-task] execution prompt hash: {spec.execution_prompt_hash}",
    ]
    if show_prompt and spec.execution_prompt_text is not None:
        lines.append("[blackdog-task] execution prompt:")
        lines.extend(f"  {line}" for line in spec.execution_prompt_text.splitlines())
    lines.append(render_start_text(spec.worktree, surface="task").rstrip())
    return "\n".join(lines) + "\n"


def render_show_text(payload: dict[str, Any], *, surface: str = "worktree") -> str:
    prefix = f"[blackdog-{surface}]"
    workspace_label = "task workspace" if surface == "task" else "worktree"
    lines = [
        f"{prefix} show: {payload['task_id']} {payload['task_title']}",
        f"{prefix} active attempt: {'yes' if payload['active_attempt'] else 'no'}",
    ]
    if payload["attempt_id"]:
        lines.append(f"{prefix} attempt: {payload['attempt_id']}")
    if payload["latest_attempt_status"]:
        lines.append(f"{prefix} latest attempt: {payload['latest_attempt_status']} {payload['latest_attempt_id']}")
    if payload["latest_attempt_summary"]:
        lines.append(f"{prefix} latest summary: {payload['latest_attempt_summary']}")
    if payload["branch"]:
        lines.append(f"{prefix} branch: {payload['branch']}")
    if payload["target_branch"]:
        lines.append(f"{prefix} target branch: {payload['target_branch']}")
    if payload["worktree_path"]:
        lines.append(f"{prefix} {workspace_label}: {payload['worktree_path']}")
    lines.append(f"{prefix} {workspace_label} dirty: {'yes' if payload['worktree_dirty'] else 'no'}")
    lines.append(f"{prefix} branch ahead of target: {'yes' if payload['branch_ahead_of_target'] else 'no'}")
    lines.append(f"{prefix} primary dirty: {'yes' if payload['primary_dirty'] else 'no'}")
    if payload["worktree_dirty_paths"]:
        lines.append(f"{prefix} {workspace_label} dirty paths: {', '.join(payload['worktree_dirty_paths'])}")
    if payload["changed_paths"]:
        lines.append(f"{prefix} attempt paths: {', '.join(payload['changed_paths'])}")
    if payload["user_prompt_hash"]:
        lines.append(f"{prefix} user prompt hash: {payload['user_prompt_hash']}")
    if payload["user_prompt_source"]:
        lines.append(f"{prefix} user prompt source: {payload['user_prompt_source']}")
    if payload["user_prompt_mode"]:
        lines.append(f"{prefix} user prompt mode: {payload['user_prompt_mode']}")
    if payload["execution_prompt_hash"]:
        lines.append(f"{prefix} execution prompt hash: {payload['execution_prompt_hash']}")
    if payload["execution_prompt_source"]:
        lines.append(f"{prefix} execution prompt source: {payload['execution_prompt_source']}")
    if payload["execution_prompt_mode"]:
        lines.append(f"{prefix} execution prompt mode: {payload['execution_prompt_mode']}")
    if payload["recommended_actions"]:
        lines.append(f"{prefix} recommended actions:")
        lines.extend(f"  - {item}" for item in payload["recommended_actions"])
    return "\n".join(lines) + "\n"


def render_close_text(payload: dict[str, Any], *, surface: str = "worktree") -> str:
    prefix = f"[blackdog-{surface}]"
    workspace_label = "task workspace" if surface == "task" else "worktree"
    lines = [
        f"{prefix} closed: {payload['task_id']} attempt={payload['attempt_id']} status={payload['status']}",
        f"{prefix} summary: {payload['summary']}",
    ]
    if payload.get("branch"):
        lines.append(f"{prefix} branch: {payload['branch']}")
    if payload.get("target_branch"):
        lines.append(f"{prefix} target branch: {payload['target_branch']}")
    if payload.get("worktree_path"):
        lines.append(f"{prefix} {workspace_label}: {payload['worktree_path']}")
    if payload.get("changed_paths"):
        lines.append(f"{prefix} changed paths: {', '.join(payload['changed_paths'])}")
    if payload.get("cleanup_performed") and payload.get("cleanup"):
        lines.append(f"{prefix} removed: {payload['cleanup']['worktree_path']}")
    elif payload.get("cleanup_reason"):
        lines.append(f"{prefix} cleanup: {payload['cleanup_reason']}")
    if payload.get("error"):
        lines.append(f"{prefix} error: {payload['error']}")
    return "\n".join(lines) + "\n"


def render_cleanup_text(payload: dict[str, Any], *, surface: str = "worktree") -> str:
    prefix = f"[blackdog-{surface}]"
    lines = [f"{prefix} removed: {payload['worktree_path']}"]
    if payload["branch"]:
        action = "deleted" if payload["deleted_branch"] else "kept"
        lines.append(f"{prefix} branch: {payload['branch']} ({action})")
    return "\n".join(lines) + "\n"


__all__ = [
    "DirtyPrimaryWorktreeError",
    "TaskBeginSpec",
    "WORKSPACE_MODE_GIT_WORKTREE",
    "WORKTREE_ROLE_LINKED",
    "WORKTREE_ROLE_PRIMARY",
    "WORKTREE_ROLE_TASK",
    "WTAM_WORKTREE_VE_NOTE",
    "WorktreeError",
    "WorktreeSpec",
    "branch_ahead_of_target",
    "branch_changed_paths",
    "begin_task_worktree",
    "cleanup_task",
    "cleanup_task_worktree",
    "close_task",
    "close_task_worktree",
    "default_task_branch",
    "default_task_worktree_path",
    "dirty_paths",
    "dirty_primary_worktree_error",
    "find_primary_worktree",
    "find_worktree_for_branch",
    "inspect_task_worktree",
    "land_task",
    "land_branch",
    "land_task_worktree",
    "primary_worktree_dirty_paths",
    "primary_worktree_is_dirty",
    "preview_task_worktree",
    "render_cleanup_text",
    "render_close_text",
    "render_land_text",
    "render_preflight_text",
    "render_preview_text",
    "render_show_text",
    "render_start_text",
    "render_task_begin_text",
    "show_task",
    "start_task_worktree",
    "worktree_contract",
    "worktree_preflight",
]
