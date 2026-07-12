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
    set_task_runtime_status,
    start_task,
    upsert_workset,
)
from blackdog_core.codex_sessions import current_codex_runtime_context, current_codex_session_ref
from blackdog_core.profile import RepoProfile, slugify
from blackdog_core.state import (
    ATTEMPT_STATUS_ABANDONED,
    ATTEMPT_STATUS_BLOCKED,
    FAILURE_CLASS_ABANDONED,
    FAILURE_CLASS_DIRTY_PRIMARY,
    FAILURE_CLASS_MISSING_WORKTREE,
    FAILURE_CLASS_NO_CHANGES,
    FAILURE_CLASS_STALE_BRANCH,
    FAILURE_CLASS_UNKNOWN,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_SUCCESS,
    PROMPT_MODE_RAW,
    PROMPT_MODE_SKILL,
    PROMPT_MODE_TUNED,
    CodexSessionRefRecord,
    PromptReceiptRecord,
    TASK_STATUS_BLOCKED,
    TASK_STATUS_CANCELED,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_PLANNED,
    TaskRuntimeRecord,
    ValidationRecord,
    active_task_attempt,
    append_event,
    create_prompt_receipt,
    latest_task_attempt,
    load_runtime_state,
    merge_workset_runtime,
    mutate_runtime_state,
    now_iso,
    parse_iso,
    prompt_receipt_reference,
    task_claim_index,
    task_state_index,
    workset_claim,
)


WTAM_WORKTREE_VE_NOTE = (
    ".VE is unversioned and bound to this worktree path; bootstrap one per worktree and do not reuse another "
    "worktree's .VE."
)
WORKSPACE_MODE_GIT_WORKTREE = "git-worktree"
WORKTREE_ROLE_PRIMARY = "primary"
WORKTREE_ROLE_TASK = "task"
WORKTREE_ROLE_LINKED = "linked"
TASK_CLASS_IMPLEMENTATION = "implementation"
TASK_CLASS_DEPLOYMENT = "deployment"
TASK_CLASS_DATA_REFRESH = "data_refresh"
TASK_CLASS_ANALYSIS_PUBLISH = "analysis_publish"
SETUP_RECEIPT_SCHEMA_VERSION = 1


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
    setup_receipt: dict[str, Any]
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


@dataclass(frozen=True, slots=True)
class _BranchCleanupPlan:
    branch_exists: bool
    force_delete: bool
    branch_tip: str | None
    reason: str
    proof_state: str


@dataclass(frozen=True, slots=True)
class _CleanupClassification:
    status: str
    reason: str | None
    proof_state: str | None


WORKTREE_TABLE_COLUMNS = (
    "workset_id",
    "task_id",
    "task_title",
    "state",
    "latest_attempt_status",
    "started_at",
    "ended_at",
    "last_commit_at",
    "last_commit",
    "last_commit_message",
    "branch",
    "target_branch",
    "worktree_path",
    "worktree_dirty_count",
    "branch_ahead_of_target",
    "changed_paths_count",
    "size_bytes",
    "size",
    "cleanup_status",
    "cleanup_reason",
    "cleanup_command",
    "recommended_action",
)


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


def _is_git_worktree_path(path: Path) -> bool:
    return (path / ".git").exists()


def _worktree_branch_map(repo_root: Path) -> dict[str, Path]:
    rows: dict[str, Path] = {}
    for row in _parse_worktree_list(repo_root):
        branch = str(row.get("branch") or "")
        worktree = row.get("worktree")
        if branch and worktree:
            rows[branch] = Path(str(worktree)).resolve()
    return rows


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


def _configured_generated_ignores(profile: RepoProfile, *, repo_root: Path) -> tuple[frozenset[str], tuple[str, ...]]:
    exact_paths: set[str] = set()
    prefixes: set[str] = set()

    def add_path(value: str | None, *, directory: bool) -> None:
        if not value:
            return
        candidate = Path(value)
        resolved = candidate.resolve() if candidate.is_absolute() else (repo_root / candidate).resolve()
        if not _is_within(repo_root, resolved):
            return
        relative = resolved.relative_to(repo_root).as_posix().rstrip("/")
        if not relative:
            return
        if directory:
            prefixes.add(f"{relative}/")
        exact_paths.add(relative)

    for handler in profile.handlers:
        if not handler.enabled:
            continue
        add_path(getattr(handler, "root_path", None), directory=True)
        add_path(getattr(handler, "worktree_path", None), directory=True)
        add_path(getattr(handler, "launcher_path", None), directory=False)
    return frozenset(exact_paths), tuple(sorted(prefixes))


def _status_ignores(
    profile: RepoProfile,
    *,
    repo_root: Path,
    include_generated: bool = True,
) -> tuple[frozenset[str], tuple[str, ...]]:
    runtime_prefixes = set(_runtime_ignore_prefixes(profile, repo_root=repo_root))
    if not include_generated:
        return frozenset(), tuple(sorted(runtime_prefixes))
    generated_paths, generated_prefixes = _configured_generated_ignores(profile, repo_root=repo_root)
    runtime_prefixes.update(generated_prefixes)
    return generated_paths, tuple(sorted(runtime_prefixes))


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


def _managed_dirty_paths(profile: RepoProfile, repo_root: Path, *, include_generated: bool = True) -> list[str]:
    ignore_paths, ignore_prefixes = _status_ignores(
        profile,
        repo_root=repo_root,
        include_generated=include_generated,
    )
    return dirty_paths(repo_root, ignore_paths=ignore_paths, ignore_prefixes=ignore_prefixes)


def _managed_status_dirty(profile: RepoProfile, repo_root: Path, *, include_generated: bool = True) -> bool:
    return bool(_managed_dirty_paths(profile, repo_root, include_generated=include_generated))


def _current_branch(repo_root: Path) -> str:
    branch = _run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise WorktreeError(f"detached HEAD at {repo_root}; specify --from explicitly")
    return branch


def command_workspace_root(profile: RepoProfile, *, cwd: Path | None = None) -> Path:
    candidate = (cwd or Path.cwd()).resolve()
    try:
        candidate_primary = find_primary_worktree(candidate)
        profile_primary = find_primary_worktree(profile.paths.project_root)
    except WorktreeError:
        return profile.paths.project_root
    if candidate_primary == profile_primary:
        return candidate
    return profile.paths.project_root


_NEW_TASK_BEGIN_HINT = (
    "For new work, use `blackdog task begin` without --workset/--task; "
    "low-level worktree commands only target existing task ids from `task show`, "
    "`task recover`, or a prior `task begin` response."
)


def _require_workset_and_task(profile: RepoProfile, *, workset_id: str, task_id: str) -> tuple[Workset, TaskSpec]:
    planning_state = load_planning_state(profile.paths)
    workset = find_workset(planning_state, workset_id)
    if workset is None:
        raise BacklogError(f"Unknown workset {workset_id!r}. {_NEW_TASK_BEGIN_HINT}")
    for task in workset.tasks:
        if task.task_id == task_id:
            return workset, task
    raise BacklogError(f"Unknown task {task_id!r} in workset {workset_id!r}. {_NEW_TASK_BEGIN_HINT}")


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


def _classify_task_prompt(prompt: str) -> str:
    normalized = f" {str(prompt).lower()} "
    if any(token in normalized for token in (" deploy", " deployment", " production", " prod-", " release ")):
        return TASK_CLASS_DEPLOYMENT
    if any(token in normalized for token in (" publish", " sharepoint", " reportdog", " report ")):
        return TASK_CLASS_ANALYSIS_PUBLISH
    if any(token in normalized for token in (" refresh", " ingest", " ingestion", " sync", " data update")):
        return TASK_CLASS_DATA_REFRESH
    return TASK_CLASS_IMPLEMENTATION


def _deployment_route_declared(prompt: str) -> bool:
    normalized = str(prompt).lower()
    route_markers = (
        "github actions",
        "ci route",
        "ci-owned",
        "workflow_dispatch",
        "standard deploy path",
        "approved local fallback",
        "local fallback approved",
        "emergency fallback",
    )
    return any(marker in normalized for marker in route_markers)


def _task_start_guard_receipt(prompt: str) -> dict[str, Any]:
    task_class = _classify_task_prompt(prompt)
    probes: list[dict[str, Any]] = [
        {
            "name": "task_class",
            "status": "ok",
            "value": task_class,
            "required": True,
            "message": f"classified task as {task_class}",
        }
    ]
    blockers: list[str] = []
    if task_class == TASK_CLASS_DEPLOYMENT:
        route_declared = _deployment_route_declared(prompt)
        probes.append(
            {
                "name": "deployment_route",
                "status": "ok" if route_declared else "blocked",
                "required": True,
                "message": (
                    "deployment route or approved local fallback is explicit"
                    if route_declared
                    else "deployment tasks must name the CI/GitHub Actions route or explicitly approve local fallback"
                ),
            }
        )
        if not route_declared:
            blockers.append("deployment_route")
    return {
        "schema_version": SETUP_RECEIPT_SCHEMA_VERSION,
        "task_class": task_class,
        "status": "blocked" if blockers else "ok",
        "blockers": blockers,
        "probes": probes,
    }


def _guard_task_start(prompt: str) -> dict[str, Any]:
    receipt = _task_start_guard_receipt(prompt)
    blockers = receipt.get("blockers") or []
    if blockers:
        messages = [
            str(probe.get("message"))
            for probe in receipt.get("probes", [])
            if probe.get("status") == "blocked" and probe.get("message")
        ]
        detail = "; ".join(messages) or f"blocked setup probes: {', '.join(str(item) for item in blockers)}"
        raise BacklogError(f"task start blocked by setup guard: {detail}")
    return receipt


def _handler_setup_receipt(guard_receipt: dict[str, Any], handlers: HandlerPlanSummary) -> dict[str, Any]:
    probes = list(guard_receipt.get("probes") or [])
    blockers = [str(item) for item in guard_receipt.get("blockers") or []]
    for action in handlers.actions:
        status = "ok" if action.status in {"validated", "created", "preserved", "skipped"} else "blocked"
        probe_name = f"{action.handler_id}.{action.action}"
        probes.append(
            {
                "name": probe_name,
                "status": status,
                "handler_id": action.handler_id,
                "kind": action.kind,
                "action": action.action,
                "target_path": action.target_path,
                "required": True,
                "message": action.message,
                "elapsed_ms": action.elapsed_ms,
            }
        )
        if status == "blocked":
            blockers.append(probe_name)
    return {
        "schema_version": SETUP_RECEIPT_SCHEMA_VERSION,
        "checked_at": now_iso(),
        "task_class": guard_receipt.get("task_class") or TASK_CLASS_IMPLEMENTATION,
        "status": "ok" if handlers.ready and not blockers else "blocked",
        "blockers": blockers,
        "workspace_ve": handlers.worktree_ve_path,
        "workspace_blackdog_path": handlers.blackdog_path,
        "runtime_mode": handlers.runtime_mode,
        "source_mode": handlers.source_mode,
        "script_policy": handlers.script_policy,
        "probes": probes,
    }


def _auto_task_workset_payload(
    profile: RepoProfile,
    *,
    prompt: str,
    title: str | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    resolved_title = str(title or "").strip() or _derive_task_title(prompt)
    title_slug = slugify(resolved_title) or "task"
    workset_id = f"task-{title_slug}-{uuid.uuid4().hex[:8]}"
    target_branch = _target_branch_for_current_worktree(profile, repo_root=workspace_root)
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
    user_prompt: str | None = None,
    user_prompt_source: str | None = None,
    prompt_mode: str,
) -> tuple[PromptReceiptRecord, PromptReceiptRecord]:
    resolved_user_prompt = user_prompt if user_prompt is not None else prompt
    resolved_user_source = user_prompt_source if user_prompt is not None else prompt_source
    user_receipt = create_prompt_receipt(resolved_user_prompt, source=resolved_user_source, mode=PROMPT_MODE_RAW)
    if prompt_mode == PROMPT_MODE_TUNED:
        tuned = tune_prompt(
            profile,
            request=prompt,
            prompt_source=prompt_source,
        )
        execution_receipt = create_prompt_receipt(
            tuned.tuned_prompt,
            source=prompt_source,
            mode=PROMPT_MODE_TUNED,
        )
        return user_receipt, execution_receipt
    if prompt_mode == PROMPT_MODE_SKILL:
        execution_receipt = create_prompt_receipt(prompt, source=prompt_source, mode=PROMPT_MODE_SKILL)
        return user_receipt, execution_receipt
    if user_prompt is not None:
        execution_receipt = create_prompt_receipt(prompt, source=prompt_source, mode=PROMPT_MODE_RAW)
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
    raise WorktreeError(
        f"could not infer a Blackdog task from {workspace_root}; run from a task worktree or provide both "
        "--workset and --task for an existing task. For new work, use `blackdog task begin` without "
        "--workset/--task."
    )


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
    cwd: Path | None = None,
    note: str | None = None,
    include_prompt: bool = False,
    expand_contract: bool = False,
) -> WorktreePreview:
    workset, task = _require_workset_and_task(profile, workset_id=workset_id, task_id=task_id)
    current_root = _repo_root(command_workspace_root(profile, cwd=cwd))
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


def _target_branch_for_current_worktree(profile: RepoProfile, *, repo_root: Path | None = None) -> str:
    current_root = _repo_root(repo_root or profile.paths.project_root)
    primary_root = find_primary_worktree(profile.paths.project_root)
    primary_branch = _current_branch(primary_root)
    current_branch = _current_branch(current_root)
    if not _is_primary_worktree(current_root) and not _is_task_branch(profile, current_branch):
        return current_branch
    return primary_branch


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
    primary_branch = _current_branch(primary_root)
    current_branch = _run_git(resolved_workspace, "rev-parse", "--abbrev-ref", "HEAD")
    workspace_role = WORKTREE_ROLE_PRIMARY if current_is_primary else (
        WORKTREE_ROLE_TASK if _is_task_branch(profile, current_branch) else WORKTREE_ROLE_LINKED
    )
    target_branch = current_branch if workspace_role == WORKTREE_ROLE_LINKED else primary_branch
    return {
        "workspace_mode": workspace_mode or WORKSPACE_MODE_GIT_WORKTREE,
        "current_worktree": str(resolved_workspace),
        "current_branch": current_branch,
        "current_is_primary": current_is_primary,
        "workspace_role": workspace_role,
        "primary_worktree": str(primary_root),
        "primary_branch": primary_branch,
        "target_branch": target_branch,
        "primary_dirty": _managed_status_dirty(profile, primary_root),
        "primary_dirty_paths": _managed_dirty_paths(profile, primary_root),
        "workspace_ve": str(resolved_workspace / ".VE"),
        "workspace_blackdog_path": str(workspace_blackdog),
        "workspace_has_local_blackdog": workspace_has_local_blackdog,
        "ve_expectation": WTAM_WORKTREE_VE_NOTE,
    }


def worktree_preflight(profile: RepoProfile, *, cwd: Path | None = None) -> dict[str, Any]:
    resolved_cwd = command_workspace_root(profile, cwd=cwd).resolve()
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
        "implementation_dirty": _managed_status_dirty(profile, current_root),
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
    if ignore_runtime:
        return _managed_status_dirty(profile, primary_root)
    return _status_dirty(primary_root)


def primary_worktree_dirty_paths(profile: RepoProfile, *, ignore_runtime: bool = True) -> list[str]:
    primary_root = find_primary_worktree(profile.paths.project_root)
    if ignore_runtime:
        return _managed_dirty_paths(profile, primary_root)
    return dirty_paths(primary_root)


def dirty_primary_worktree_error(profile: RepoProfile, *, branch: str, target_branch: str | None = None) -> DirtyPrimaryWorktreeError:
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    return DirtyPrimaryWorktreeError(
        primary_worktree=primary_root,
        branch=branch,
        target_branch=resolved_target,
        dirty_paths=primary_worktree_dirty_paths(profile, ignore_runtime=True),
    )


def branch_changed_paths(
    profile: RepoProfile,
    *,
    branch: str,
    target_branch: str | None = None,
    primary_root: Path | None = None,
    changed_paths_cache: dict[tuple[str, str | None], list[str]] | None = None,
) -> list[str]:
    cache_key = (branch, target_branch)
    if changed_paths_cache is not None and cache_key in changed_paths_cache:
        return list(changed_paths_cache[cache_key])
    primary_root = primary_root or find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    completed = _run_git_no_check(primary_root, "diff", "--name-only", f"{resolved_target}..{branch}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git diff --name-only {resolved_target}..{branch} failed: {detail}")
    changed = sorted({line.strip() for line in completed.stdout.splitlines() if line.strip()})
    if changed_paths_cache is not None:
        changed_paths_cache[cache_key] = changed
    return changed


def branch_ahead_of_target(profile: RepoProfile, *, branch: str, target_branch: str | None = None) -> bool:
    primary_root = find_primary_worktree(profile.paths.project_root)
    resolved_target = target_branch or _current_branch(primary_root)
    completed = _run_git_no_check(primary_root, "rev-list", "--count", f"{resolved_target}..{branch}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise WorktreeError(f"git rev-list --count {resolved_target}..{branch} failed: {detail}")
    return int(completed.stdout.strip() or "0") > 0


def _ref_exists(repo_root: Path, ref: str | None, *, ref_cache: dict[str, str | None] | None = None) -> bool | None:
    if not ref:
        return None
    return _resolve_commit(repo_root, ref, ref_cache=ref_cache) is not None


def _recovery_branch_state(
    profile: RepoProfile,
    *,
    branch: str | None,
    target_branch: str | None,
    primary_root: Path | None = None,
    ref_cache: dict[str, str | None] | None = None,
    branch_ahead_cache: dict[tuple[str, str], tuple[bool, str | None]] | None = None,
) -> tuple[bool, bool | None, bool | None, str | None]:
    primary_root = primary_root or find_primary_worktree(profile.paths.project_root)
    branch_exists = _ref_exists(primary_root, branch, ref_cache=ref_cache)
    target_exists = _ref_exists(primary_root, target_branch, ref_cache=ref_cache)
    if not branch or not target_branch:
        return False, branch_exists, target_exists, None
    if branch_exists is False:
        return False, branch_exists, target_exists, f"task branch {branch!r} is missing"
    if target_exists is False:
        return False, branch_exists, target_exists, f"target branch {target_branch!r} is missing"
    cache_key = (target_branch, branch)
    if branch_ahead_cache is not None and cache_key in branch_ahead_cache:
        cached_ahead, cached_error = branch_ahead_cache[cache_key]
        return cached_ahead, branch_exists, target_exists, cached_error
    completed = _run_git_no_check(primary_root, "rev-list", "--count", f"{target_branch}..{branch}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        error = f"git rev-list --count {target_branch}..{branch} failed: {detail}"
        if branch_ahead_cache is not None:
            branch_ahead_cache[cache_key] = (False, error)
        return False, branch_exists, target_exists, error
    ahead = int(completed.stdout.strip() or "0") > 0
    if branch_ahead_cache is not None:
        branch_ahead_cache[cache_key] = (ahead, None)
    return ahead, branch_exists, target_exists, None


def _resolve_commit(repo_root: Path, ref: str | None, *, ref_cache: dict[str, str | None] | None = None) -> str | None:
    if not ref:
        return None
    if ref_cache is not None and ref in ref_cache:
        return ref_cache[ref]
    completed = _run_git_no_check(repo_root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if completed.returncode != 0:
        if ref_cache is not None:
            ref_cache[ref] = None
        return None
    resolved = completed.stdout.strip() or None
    if ref_cache is not None:
        ref_cache[ref] = resolved
    return resolved


def _can_force_delete_landed_task_branch(
    primary_root: Path,
    *,
    branch: str,
    branch_tip: str,
    latest_attempt: TaskRuntimeRecord | None,
    ref_cache: dict[str, str | None] | None = None,
) -> tuple[bool, str]:
    if latest_attempt is None:
        return False, "no runtime attempt metadata"
    if latest_attempt.branch != branch:
        return False, "latest attempt branch does not match the cleanup branch"
    if latest_attempt.status != ATTEMPT_STATUS_SUCCESS:
        return False, f"latest attempt status is {latest_attempt.status!r}, not success"
    if not latest_attempt.commit:
        return False, "latest attempt is missing the recorded task-branch commit"
    if not latest_attempt.landed_commit:
        return False, "latest attempt is missing the canonical landed commit"
    if not latest_attempt.target_branch:
        return False, "latest attempt is missing the target branch"

    recorded_commit = _resolve_commit(primary_root, latest_attempt.commit, ref_cache=ref_cache)
    if recorded_commit is None:
        return False, f"recorded task-branch commit {latest_attempt.commit} is missing"
    if recorded_commit != branch_tip:
        return False, "branch tip changed after the recorded landed attempt"

    landed_commit = _resolve_commit(primary_root, latest_attempt.landed_commit, ref_cache=ref_cache)
    if landed_commit is None:
        return False, f"landed commit {latest_attempt.landed_commit} is missing"
    if _resolve_commit(primary_root, latest_attempt.target_branch, ref_cache=ref_cache) is None:
        return False, f"target branch {latest_attempt.target_branch} is missing"

    landed_reachable = _run_git_no_check(
        primary_root,
        "merge-base",
        "--is-ancestor",
        landed_commit,
        latest_attempt.target_branch,
    )
    if landed_reachable.returncode != 0:
        return False, f"landed commit {landed_commit[:12]} is not reachable from {latest_attempt.target_branch}"

    same_tree = _run_git_no_check(primary_root, "diff", "--quiet", branch_tip, landed_commit)
    if same_tree.returncode != 0:
        return False, "recorded task-branch tree differs from the landed commit"

    return True, "recorded task branch is patch-equivalent to the canonical landed commit"


def _can_force_delete_terminal_patch_equivalent_branch(
    primary_root: Path,
    *,
    branch: str,
    latest_attempt: TaskRuntimeRecord | None,
    ref_cache: dict[str, str | None] | None = None,
) -> tuple[bool, str]:
    if latest_attempt is None:
        return False, "no runtime attempt metadata"
    if latest_attempt.branch != branch:
        return False, "latest attempt branch does not match the cleanup branch"
    if latest_attempt.status not in {
        ATTEMPT_STATUS_SUCCESS,
        ATTEMPT_STATUS_BLOCKED,
        ATTEMPT_STATUS_FAILED,
        ATTEMPT_STATUS_ABANDONED,
    }:
        return False, f"latest attempt status {latest_attempt.status!r} is not terminal"
    if not latest_attempt.target_branch:
        return False, "latest attempt is missing the target branch"
    if _resolve_commit(primary_root, latest_attempt.target_branch, ref_cache=ref_cache) is None:
        return False, f"target branch {latest_attempt.target_branch!r} is missing"

    merge_commits = _run_git_no_check(
        primary_root,
        "rev-list",
        "--merges",
        f"{latest_attempt.target_branch}..{branch}",
    )
    if merge_commits.returncode != 0:
        detail = merge_commits.stderr.strip() or merge_commits.stdout.strip() or f"exit code {merge_commits.returncode}"
        return False, f"could not inspect task-branch merge commits: {detail}"
    if merge_commits.stdout.strip():
        return False, "task branch contains merge commits that cannot be proven by patch equivalence"

    cherry = _run_git_no_check(primary_root, "cherry", latest_attempt.target_branch, branch)
    if cherry.returncode != 0:
        detail = cherry.stderr.strip() or cherry.stdout.strip() or f"exit code {cherry.returncode}"
        return False, f"git cherry patch-equivalence check failed: {detail}"
    patch_rows = [line.strip() for line in cherry.stdout.splitlines() if line.strip()]
    if not patch_rows:
        return False, "task branch has no independently verifiable patch-equivalence rows"
    unlanded_rows = [line for line in patch_rows if not line.startswith("- ")]
    if unlanded_rows:
        return False, f"{len(unlanded_rows)} task-branch patch(es) are not represented on {latest_attempt.target_branch}"
    return True, f"all terminal task-branch patches are equivalent to patches on {latest_attempt.target_branch}"


def _plan_task_branch_cleanup(
    primary_root: Path,
    *,
    branch: str,
    latest_attempt: TaskRuntimeRecord | None,
    ref_cache: dict[str, str | None] | None = None,
) -> _BranchCleanupPlan:
    branch_tip = _resolve_commit(primary_root, branch, ref_cache=ref_cache)
    if branch_tip is None:
        return _BranchCleanupPlan(
            branch_exists=False,
            force_delete=False,
            branch_tip=None,
            reason="branch already absent",
            proof_state="branch_absent",
        )

    target_branch = latest_attempt.target_branch if latest_attempt and latest_attempt.target_branch else _current_branch(primary_root)
    target_commit = _resolve_commit(primary_root, target_branch, ref_cache=ref_cache)
    if target_commit is not None:
        if branch_tip == target_commit:
            return _BranchCleanupPlan(
                branch_exists=True,
                force_delete=False,
                branch_tip=branch_tip,
                reason=f"branch has no commits ahead of {target_branch}",
                proof_state="no_ahead",
            )
        merged = _run_git_no_check(primary_root, "merge-base", "--is-ancestor", branch_tip, target_branch)
        if merged.returncode == 0:
            return _BranchCleanupPlan(
                branch_exists=True,
                force_delete=False,
                branch_tip=branch_tip,
                reason=f"branch is already merged into {target_branch}",
                proof_state="contained",
            )

    can_force_delete, reason = _can_force_delete_landed_task_branch(
        primary_root,
        branch=branch,
        branch_tip=branch_tip,
        latest_attempt=latest_attempt,
        ref_cache=ref_cache,
    )
    if can_force_delete:
        return _BranchCleanupPlan(
            branch_exists=True,
            force_delete=True,
            branch_tip=branch_tip,
            reason=reason,
            proof_state="patch_equivalent",
        )
    terminal_patch_equivalent, patch_reason = _can_force_delete_terminal_patch_equivalent_branch(
        primary_root,
        branch=branch,
        latest_attempt=latest_attempt,
        ref_cache=ref_cache,
    )
    if terminal_patch_equivalent:
        return _BranchCleanupPlan(
            branch_exists=True,
            force_delete=True,
            branch_tip=branch_tip,
            reason=patch_reason,
            proof_state="patch_equivalent",
        )
    raise WorktreeError(
        f"refusing cleanup: branch {branch} has commits not proven landed on {target_branch} "
        f"({reason}; {patch_reason})"
    )


def _resolve_attempt_worktree(
    profile: RepoProfile,
    *,
    branch: str | None,
    worktree_path: str | None,
    worktree_by_branch: dict[str, Path] | None = None,
) -> Path | None:
    if worktree_path:
        candidate = Path(worktree_path).resolve()
        if candidate.exists() and _is_git_worktree_path(candidate):
            return candidate
    if branch:
        branch_ref = branch if branch.startswith("refs/heads/") else f"refs/heads/{branch}"
        if worktree_by_branch is not None:
            candidate = worktree_by_branch.get(branch_ref)
            if candidate is not None and candidate.exists():
                return candidate.resolve()
            return None
        existing = find_worktree_for_branch(profile, branch)
        if existing:
            candidate = Path(existing).resolve()
            if candidate.exists():
                return candidate
    return None


def _worktree_changed_paths(profile: RepoProfile, worktree_path: Path) -> list[str]:
    return _managed_dirty_paths(profile, worktree_path)


def _attempt_changed_paths(
    profile: RepoProfile,
    *,
    branch: str | None,
    target_branch: str | None,
    worktree_path: Path | None,
    primary_root: Path | None = None,
    changed_paths_cache: dict[tuple[str, str | None], list[str]] | None = None,
) -> list[str]:
    changed: set[str] = set()
    if branch:
        try:
            changed.update(
                branch_changed_paths(
                    profile,
                    branch=branch,
                    target_branch=target_branch,
                    primary_root=primary_root,
                    changed_paths_cache=changed_paths_cache,
                )
            )
        except WorktreeError:
            pass
    if worktree_path is not None and worktree_path.exists():
        changed.update(_worktree_changed_paths(profile, worktree_path))
    return sorted(changed)


def _format_size(size_bytes: int | None) -> str | None:
    if size_bytes is None:
        return None
    units = ("B", "KiB", "MiB", "GiB")
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def _path_size_bytes(path: Path | None) -> int | None:
    if path is None or not path.exists():
        return None
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for filename in files:
            candidate = Path(root) / filename
            if candidate.is_symlink():
                continue
            total += candidate.stat().st_size
    return total


def _commit_metadata(repo_root: Path | None, ref: str = "HEAD") -> dict[str, str | None]:
    empty = {"commit": None, "date": None, "message": None}
    if repo_root is None or not repo_root.exists():
        return empty
    completed = _run_git_no_check(repo_root, "show", "-s", "--format=%H%x00%cI%x00%s", ref)
    if completed.returncode != 0:
        return empty
    parts = completed.stdout.rstrip("\n").split("\x00", 2)
    if len(parts) != 3:
        return empty
    return {"commit": parts[0] or None, "date": parts[1] or None, "message": parts[2] or None}


def _cleanup_classification(
    profile: RepoProfile,
    *,
    primary_root: Path,
    branch: str | None,
    worktree_exists: bool,
    worktree_dirty_paths: list[str],
    active_attempt: bool,
    latest_attempt: TaskRuntimeRecord | None,
    ref_cache: dict[str, str | None] | None = None,
) -> _CleanupClassification:
    if not worktree_exists:
        if active_attempt:
            return _CleanupClassification("missing_worktree", "active attempt workspace is missing", "missing_active")
        if not branch:
            return _CleanupClassification("absent", "no branch or worktree remains", "absent")
        try:
            plan = _plan_task_branch_cleanup(
                primary_root,
                branch=branch,
                latest_attempt=latest_attempt,
                ref_cache=ref_cache,
            )
        except WorktreeError as exc:
            return _CleanupClassification("blocked_unlanded", str(exc), "unproven")
        if plan.branch_exists:
            return _CleanupClassification("cleanup_ready", f"worktree already absent; {plan.reason}", plan.proof_state)
        return _CleanupClassification("absent", "worktree and branch are already absent", plan.proof_state)
    if worktree_dirty_paths:
        return _CleanupClassification("blocked_dirty", "worktree has uncommitted changes", "dirty")
    if active_attempt:
        return _CleanupClassification("active", "active attempts must be landed or closed before cleanup", "active")
    if not branch:
        return _CleanupClassification("cleanup_ready", "no branch recorded", "no_branch")
    try:
        plan = _plan_task_branch_cleanup(
            primary_root,
            branch=branch,
            latest_attempt=latest_attempt,
            ref_cache=ref_cache,
        )
    except WorktreeError as exc:
        return _CleanupClassification("blocked_unlanded", str(exc), "unproven")
    return _CleanupClassification("cleanup_ready", plan.reason, plan.proof_state)


def _worktree_table_row(
    profile: RepoProfile,
    *,
    workset_id: str,
    task: TaskSpec,
    runtime_state: Any | None = None,
    primary_root: Path | None = None,
    primary_dirty: bool | None = None,
    primary_dirty_paths: list[str] | None = None,
    worktree_by_branch: dict[str, Path] | None = None,
    ref_cache: dict[str, str | None] | None = None,
    branch_ahead_cache: dict[tuple[str, str], tuple[bool, str | None]] | None = None,
    changed_paths_cache: dict[tuple[str, str | None], list[str]] | None = None,
) -> dict[str, Any] | None:
    runtime_state = runtime_state or load_runtime_state(profile.paths)
    primary_root = primary_root or find_primary_worktree(profile.paths.project_root)
    active_attempt = active_task_attempt(runtime_state, workset_id, task.task_id)
    latest_attempt = latest_task_attempt(runtime_state, workset_id, task.task_id)
    selected_attempt = active_attempt or latest_attempt
    if selected_attempt is None:
        return None

    payload = _task_recovery_payload(
        profile,
        workset_id=workset_id,
        task_id=task.task_id,
        runtime_state=runtime_state,
        primary_root=primary_root,
        primary_dirty=primary_dirty,
        primary_dirty_paths=primary_dirty_paths,
        worktree_by_branch=worktree_by_branch,
        ref_cache=ref_cache,
        branch_ahead_cache=branch_ahead_cache,
        changed_paths_cache=changed_paths_cache,
    )

    worktree_path = Path(payload["worktree_path"]).resolve() if payload["worktree_path"] else None
    worktree_dirty_paths = list(payload["worktree_dirty_paths"])
    resolved_branch = (
        payload["branch"]
        or (latest_attempt.branch if latest_attempt is not None else None)
        or default_task_branch(workset_id, task)
    )
    cleanup = _cleanup_classification(
        profile,
        primary_root=primary_root,
        branch=resolved_branch,
        worktree_exists=bool(payload["worktree_exists"]),
        worktree_dirty_paths=worktree_dirty_paths,
        active_attempt=bool(payload["active_attempt"]),
        latest_attempt=latest_attempt,
        ref_cache=ref_cache,
    )
    if (
        not payload["worktree_exists"]
        and not payload["active_attempt"]
        and not payload["stale_claim"]
        and cleanup.status == "absent"
    ):
        return None
    commit_root = worktree_path if worktree_path is not None and worktree_path.exists() else primary_root
    commit_ref = "HEAD" if worktree_path is not None and worktree_path.exists() else resolved_branch or "HEAD"
    commit = _commit_metadata(commit_root, commit_ref)
    size_bytes = _path_size_bytes(worktree_path)
    cleanup_command = (
        f"blackdog task cleanup --project-root . --workset {workset_id} --task {task.task_id}"
        if cleanup.status == "cleanup_ready"
        else None
    )
    recommended_action = cleanup_command
    if recommended_action is None and payload["recommended_actions"]:
        recommended_action = payload["recommended_actions"][0]
    if recommended_action is None:
        recommended_action = cleanup.reason
    return {
        "workset_id": workset_id,
        "task_id": task.task_id,
        "task_title": task.title,
        "state": payload["recovery_state"],
        "latest_attempt_status": payload["latest_attempt_status"],
        "started_at": payload["started_at"],
        "ended_at": payload["ended_at"],
        "last_commit_at": commit["date"],
        "last_commit": commit["commit"],
        "last_commit_message": commit["message"],
        "branch": resolved_branch,
        "target_branch": payload["target_branch"],
        "worktree_path": payload["worktree_path"],
        "worktree_dirty_count": len(worktree_dirty_paths),
        "branch_ahead_of_target": payload["branch_ahead_of_target"],
        "changed_paths_count": len(payload["changed_paths"]),
        "size_bytes": size_bytes,
        "size": _format_size(size_bytes),
        "cleanup_status": cleanup.status,
        "cleanup_reason": cleanup.reason,
        "cleanup_proof": cleanup.proof_state,
        "cleanup_command": cleanup_command,
        "recommended_action": recommended_action,
    }


def build_worktree_table(profile: RepoProfile) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    planning_state = load_planning_state(profile.paths)
    runtime_state = load_runtime_state(profile.paths)
    primary_root = find_primary_worktree(profile.paths.project_root)
    primary_dirty_paths = _managed_dirty_paths(profile, primary_root)
    primary_dirty = bool(primary_dirty_paths)
    worktree_by_branch = _worktree_branch_map(primary_root)
    ref_cache: dict[str, str | None] = {}
    branch_ahead_cache: dict[tuple[str, str], tuple[bool, str | None]] = {}
    changed_paths_cache: dict[tuple[str, str | None], list[str]] = {}
    for workset in planning_state.worksets:
        for task in workset.tasks:
            row = _worktree_table_row(
                profile,
                workset_id=workset.workset_id,
                task=task,
                runtime_state=runtime_state,
                primary_root=primary_root,
                primary_dirty=primary_dirty,
                primary_dirty_paths=primary_dirty_paths,
                worktree_by_branch=worktree_by_branch,
                ref_cache=ref_cache,
                branch_ahead_cache=branch_ahead_cache,
                changed_paths_cache=changed_paths_cache,
            )
            if row is not None:
                rows.append(row)
    rows.sort(
        key=lambda row: (
            str(row.get("started_at") or ""),
            str(row.get("workset_id") or ""),
            str(row.get("task_id") or ""),
        ),
        reverse=True,
    )
    return {
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "columns": list(WORKTREE_TABLE_COLUMNS),
        "rows": rows,
        "counts": {
            "rows": len(rows),
            "cleanup_ready": sum(1 for row in rows if row["cleanup_status"] == "cleanup_ready"),
            "blocked": sum(1 for row in rows if str(row["cleanup_status"]).startswith("blocked_")),
            "active": sum(1 for row in rows if row["state"] == "active_attempt"),
            "missing_worktree": sum(1 for row in rows if row["cleanup_status"] == "missing_worktree"),
        },
    }


def cleanup_worktree_table(profile: RepoProfile) -> dict[str, Any]:
    before = build_worktree_table(profile)
    cleaned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for row in before["rows"]:
        if row["cleanup_status"] != "cleanup_ready":
            skipped.append(
                {
                    "workset_id": row["workset_id"],
                    "task_id": row["task_id"],
                    "cleanup_status": row["cleanup_status"],
                    "reason": row["recommended_action"],
                }
            )
            continue
        try:
            cleanup = cleanup_task_worktree(
                profile,
                workset_id=row["workset_id"],
                task_id=row["task_id"],
                path=row["worktree_path"],
                branch=row["branch"],
            )
        except WorktreeError as exc:
            errors.append(
                {
                    "workset_id": row["workset_id"],
                    "task_id": row["task_id"],
                    "worktree_path": row["worktree_path"],
                    "error": str(exc),
                }
            )
        else:
            cleaned.append(
                {
                    "workset_id": row["workset_id"],
                    "task_id": row["task_id"],
                    **cleanup,
                }
            )
    return {
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "cleaned": cleaned,
        "skipped": skipped,
        "errors": errors,
        "before": before,
        "remaining": build_worktree_table(profile),
    }


def _append_prompt_lineage_trailers(
    lines: list[str],
    *,
    prompt_receipt: PromptReceiptRecord | None,
    user_prompt_receipt: PromptReceiptRecord | None,
) -> None:
    if prompt_receipt is None:
        return
    lines.append(f"Blackdog-Prompt-Hash: {prompt_receipt.prompt_hash}")
    if prompt_receipt.source:
        lines.append(f"Blackdog-Prompt-Source: {prompt_receipt.source}")
    if prompt_receipt.mode:
        lines.append(f"Blackdog-Prompt-Mode: {prompt_receipt.mode}")
    if user_prompt_receipt is None:
        return
    same_prompt_lineage = (
        user_prompt_receipt.prompt_hash == prompt_receipt.prompt_hash
        and user_prompt_receipt.source == prompt_receipt.source
        and user_prompt_receipt.mode == prompt_receipt.mode
    )
    if same_prompt_lineage:
        return
    lines.append(f"Blackdog-User-Prompt-Hash: {user_prompt_receipt.prompt_hash}")
    if user_prompt_receipt.source:
        lines.append(f"Blackdog-User-Prompt-Source: {user_prompt_receipt.source}")
    if user_prompt_receipt.mode:
        lines.append(f"Blackdog-User-Prompt-Mode: {user_prompt_receipt.mode}")


def _append_codex_session_trailers(
    lines: list[str],
    *,
    codex_session: CodexSessionRefRecord | None,
) -> None:
    if codex_session is None:
        return
    lines.append(f"Blackdog-Codex-Thread: {codex_session.thread_id}")
    if codex_session.session_path:
        lines.append(f"Blackdog-Codex-Session: {codex_session.session_path}")
    if codex_session.turn_id:
        lines.append(f"Blackdog-Codex-Turn: {codex_session.turn_id}")


def _canonical_commit_message(
    workset: Workset,
    task: TaskSpec,
    *,
    attempt_id: str,
    actor: str,
    changed_paths: tuple[str, ...],
    prompt_receipt: PromptReceiptRecord | None,
    user_prompt_receipt: PromptReceiptRecord | None,
    codex_session: CodexSessionRefRecord | None,
    execution_model: str | None,
    model: str | None,
    reasoning_effort: str | None,
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
    if execution_model:
        lines.append(f"Blackdog-Execution-Model: {execution_model}")
    if model:
        lines.append(f"Blackdog-Model: {model}")
    if reasoning_effort:
        lines.append(f"Blackdog-Reasoning-Effort: {reasoning_effort}")
    _append_prompt_lineage_trailers(
        lines,
        prompt_receipt=prompt_receipt,
        user_prompt_receipt=user_prompt_receipt,
    )
    _append_codex_session_trailers(lines, codex_session=codex_session)
    for path in changed_paths:
        lines.append(f"Blackdog-Changed-Path: {path}")
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
    if not _managed_status_dirty(profile, worktree_path):
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
    cwd: Path | None = None,
    note: str | None = None,
) -> WorktreeSpec:
    codex_context = current_codex_runtime_context()
    resolved_model = model or codex_context.model
    resolved_reasoning_effort = reasoning_effort or codex_context.reasoning_effort
    guard_receipt = _guard_task_start(prompt)
    preview = preview_task_worktree(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        actor=actor,
        prompt=prompt,
        prompt_source=prompt_source,
        prompt_mode=prompt_mode,
        model=resolved_model,
        reasoning_effort=resolved_reasoning_effort,
        branch=branch,
        from_ref=from_ref,
        path=path,
        cwd=cwd,
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
        setup_receipt = _handler_setup_receipt(guard_receipt, handlers)
        if not handlers.ready:
            blocked = [action.message for action in handlers.actions if action.status == "blocked"]
            detail = "; ".join(blocked)
            if handlers.remediation:
                detail = "; ".join(item for item in [detail, handlers.remediation] if item)
            raise WorktreeError(detail or "worktree handler execution did not produce a ready workspace")
        execution_receipt = create_prompt_receipt(prompt, source=prompt_source, mode=prompt_mode)
        stored_execution_receipt = prompt_receipt_reference(execution_receipt)
        stored_user_receipt = prompt_receipt_reference(user_prompt_receipt)
        codex_session = current_codex_session_ref(
            user_prompt_hash=(
                stored_user_receipt.prompt_hash
                if stored_user_receipt is not None
                else stored_execution_receipt.prompt_hash
            ),
            execution_prompt_hash=stored_execution_receipt.prompt_hash,
        )
        attempt = start_task(
            profile,
            workset_id=workset_id,
            task_id=task_id,
            actor=actor,
            prompt_receipt=stored_execution_receipt,
            user_prompt_receipt=stored_user_receipt,
            workspace_identity=preview.workspace_identity,
            workspace_mode=WORKSPACE_MODE_GIT_WORKTREE,
            worktree_role=WORKTREE_ROLE_TASK,
            worktree_path=str(worktree_path),
            branch=preview.branch,
            target_branch=preview.target_branch,
            integration_branch=preview.integration_branch,
            start_commit=preview.base_commit,
            model=resolved_model,
            reasoning_effort=resolved_reasoning_effort,
            codex_session=codex_session,
            note=note,
            setup_receipt=setup_receipt,
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
        setup_receipt=setup_receipt,
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
            "setup_receipt": setup_receipt,
            "model": attempt.model,
            "reasoning_effort": attempt.reasoning_effort,
            "codex_thread_id": attempt.codex_session.thread_id if attempt.codex_session is not None else None,
            "codex_session_path": attempt.codex_session.session_path if attempt.codex_session is not None else None,
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
    user_prompt: str | None = None,
    user_prompt_source: str | None = None,
    prompt_mode: str = PROMPT_MODE_RAW,
    workset_id: str | None = None,
    task_id: str | None = None,
    title: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    branch: str | None = None,
    from_ref: str | None = None,
    path: str | None = None,
    cwd: Path | None = None,
    note: str | None = None,
    include_prompt: bool = False,
) -> TaskBeginSpec:
    resolved_workset = str(workset_id or "").strip() or None
    resolved_task = str(task_id or "").strip() or None
    if prompt_mode not in {PROMPT_MODE_RAW, PROMPT_MODE_SKILL, PROMPT_MODE_TUNED}:
        raise BacklogError(f"prompt mode must be one of {PROMPT_MODE_RAW}, {PROMPT_MODE_SKILL}, {PROMPT_MODE_TUNED}")
    if (resolved_workset is None) != (resolved_task is None):
        raise BacklogError(
            "task begin received only one of --workset/--task. For new work, omit both flags; "
            "to target existing planning state, provide both."
        )

    user_receipt, execution_receipt = _resolve_task_begin_prompts(
        profile,
        prompt=prompt,
        prompt_source=prompt_source,
        user_prompt=user_prompt,
        user_prompt_source=user_prompt_source,
        prompt_mode=prompt_mode,
    )
    _guard_task_start(execution_receipt.text)
    created_workset = False
    if resolved_workset is None:
        workspace_root = command_workspace_root(profile, cwd=cwd)
        payload = _auto_task_workset_payload(
            profile,
            prompt=user_receipt.text,
            title=title,
            workspace_root=workspace_root,
        )
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
        cwd=cwd,
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


def _task_state_payload(
    *,
    profile: RepoProfile,
    workset_id: str,
    task_id: str,
    actor: str,
    status: str,
    summary: str | None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
) -> dict[str, Any]:
    record = set_task_runtime_status(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        actor=actor,
        status=status,
        summary=summary,
        failure_class=failure_class,
        recovery_action=recovery_action,
        prompt_issue=prompt_issue,
        operator_issue=operator_issue,
    )
    return {
        "workset_id": workset_id,
        "task_id": task_id,
        "actor": actor,
        "status": record.status,
        "updated_at": record.updated_at,
        "summary": record.note,
        "failure_class": record.failure_class,
        "recovery_action": record.recovery_action,
        "prompt_issue": record.prompt_issue,
        "operator_issue": record.operator_issue,
    }


def cancel_task(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    summary: str | None = None,
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
) -> dict[str, Any]:
    return _task_state_payload(
        profile=profile,
        workset_id=workset_id,
        task_id=task_id,
        actor=actor,
        status=TASK_STATUS_CANCELED,
        summary=summary,
        failure_class=failure_class,
        recovery_action=recovery_action,
        prompt_issue=prompt_issue,
        operator_issue=operator_issue,
    )


def reopen_task(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    actor: str,
    summary: str | None = None,
) -> dict[str, Any]:
    return _task_state_payload(
        profile=profile,
        workset_id=workset_id,
        task_id=task_id,
        actor=actor,
        status=TASK_STATUS_PLANNED,
        summary=summary,
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


def _task_recovery_state(
    *,
    active_attempt: bool,
    stale_claim: bool,
    worktree_exists: bool,
    worktree_dirty: bool,
    branch_ahead_of_target: bool,
    reference_issue: bool,
) -> str:
    if stale_claim:
        return "stale_claim"
    if active_attempt:
        return "active_attempt"
    if reference_issue:
        return "stale_reference"
    if worktree_exists and (worktree_dirty or branch_ahead_of_target):
        return "retained_dirty_worktree"
    if worktree_exists:
        return "cleanup_ready"
    return "idle"


def _command_row(command: str, *, reason: str, disposition: str) -> dict[str, str]:
    return {
        "command": command,
        "reason": reason,
        "disposition": disposition,
    }


def _task_recovery_payload(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    runtime_state: Any | None = None,
    primary_root: Path | None = None,
    primary_dirty: bool | None = None,
    primary_dirty_paths: list[str] | None = None,
    worktree_by_branch: dict[str, Path] | None = None,
    ref_cache: dict[str, str | None] | None = None,
    branch_ahead_cache: dict[tuple[str, str], tuple[bool, str | None]] | None = None,
    changed_paths_cache: dict[tuple[str, str | None], list[str]] | None = None,
) -> dict[str, Any]:
    _workset, task = _require_workset_and_task(profile, workset_id=workset_id, task_id=task_id)
    runtime_state = runtime_state or load_runtime_state(profile.paths)
    runtime_task_claims = task_claim_index(runtime_state, workset_id)
    current_task_claim = runtime_task_claims.get(task_id)
    current_workset_claim = workset_claim(runtime_state, workset_id)
    runtime_task_state = task_state_index(runtime_state, workset_id).get(
        task_id,
        TaskRuntimeRecord(task_id=task_id, status=TASK_STATUS_PLANNED),
    )
    active_attempt = active_task_attempt(runtime_state, workset_id, task_id)
    latest_attempt = latest_task_attempt(runtime_state, workset_id, task_id)
    selected_attempt = active_attempt or latest_attempt
    branch = selected_attempt.branch if selected_attempt is not None else None
    target_branch = selected_attempt.target_branch if selected_attempt is not None else None
    recorded_worktree_path = selected_attempt.worktree_path if selected_attempt is not None else None
    task_worktree = (
        _resolve_attempt_worktree(
            profile,
            branch=branch,
            worktree_path=recorded_worktree_path,
            worktree_by_branch=worktree_by_branch,
        )
        if selected_attempt is not None
        else None
    )
    worktree_exists = task_worktree is not None and task_worktree.exists()
    worktree_dirty_paths = _worktree_changed_paths(profile, task_worktree) if task_worktree is not None else []
    branch_ahead, branch_exists, target_branch_exists, branch_ahead_error = _recovery_branch_state(
        profile,
        branch=branch,
        target_branch=target_branch,
        primary_root=primary_root,
        ref_cache=ref_cache,
        branch_ahead_cache=branch_ahead_cache,
    )
    landed_cleanup_complete = bool(
        active_attempt is None
        and selected_attempt is not None
        and selected_attempt.status == ATTEMPT_STATUS_SUCCESS
        and selected_attempt.landed_commit
        and not worktree_exists
        and branch_exists is False
        and target_branch_exists is True
    )
    if landed_cleanup_complete:
        branch_ahead_error = None
    reference_issue = bool(
        (branch_exists is False and not landed_cleanup_complete)
        or target_branch_exists is False
        or branch_ahead_error
    )
    primary_root = primary_root or find_primary_worktree(profile.paths.project_root)
    if primary_dirty_paths is None:
        primary_dirty_paths = _managed_dirty_paths(profile, primary_root)
    if primary_dirty is None:
        primary_dirty = bool(primary_dirty_paths)
    stale_claim = current_task_claim is not None and active_attempt is None
    recovery_state = _task_recovery_state(
        active_attempt=active_attempt is not None,
        stale_claim=stale_claim,
        worktree_exists=worktree_exists,
        worktree_dirty=bool(worktree_dirty_paths),
        branch_ahead_of_target=branch_ahead,
        reference_issue=reference_issue,
    )
    failure_class = runtime_task_state.failure_class
    recovery_action = runtime_task_state.recovery_action
    prompt_issue = runtime_task_state.prompt_issue
    operator_issue = runtime_task_state.operator_issue
    if failure_class is None and active_attempt is not None and not worktree_exists:
        failure_class = FAILURE_CLASS_MISSING_WORKTREE
        recovery_action = "restore_or_cleanup_worktree"
        operator_issue = True
    elif failure_class is None and reference_issue:
        failure_class = FAILURE_CLASS_STALE_BRANCH
        recovery_action = "restore_ref_or_cancel_task"
        operator_issue = True
    recommended_actions: list[str] = []
    if branch_exists is False and branch and not landed_cleanup_complete:
        recommended_actions.append(f"restore task branch `{branch}` before landing or close/cancel this stale task")
    if target_branch_exists is False and target_branch:
        recommended_actions.append(f"restore target branch `{target_branch}` or close/cancel this stale task if it is obsolete")
    if branch_ahead_error and branch_exists is not False and target_branch_exists is not False:
        recommended_actions.append("inspect the recorded task branch and target branch before landing")
    if stale_claim:
        if task_worktree is not None and (worktree_dirty_paths or branch_ahead):
            recommended_actions.append("inspect the retained task workspace before releasing the stale claim")
        recommended_actions.append(
            "run `blackdog task recover --release-stale-claim --status blocked|failed|abandoned --summary \"...\"` "
            "to release the stale claim"
        )
    elif active_attempt is not None:
        if primary_dirty:
            recommended_actions.append("clean or land the primary worktree changes before `blackdog task land`")
        if not worktree_exists:
            recommended_actions.append("restore the task workspace or close the attempt before starting new work")
        if worktree_dirty_paths or branch_ahead:
            recommended_actions.append("run `blackdog worktree land` to create the canonical landed commit")
        recommended_actions.append("run `blackdog worktree close --status blocked|failed|abandoned` to close without landing")
    elif selected_attempt is not None and reference_issue:
        recommended_actions.append("use `blackdog task cancel` if this stale task should stay out of normal ready work")
    elif task_worktree is None and not landed_cleanup_complete:
        recommended_actions.append("start a new WTAM attempt for this task")
    elif worktree_dirty_paths or branch_ahead:
        recommended_actions.append("inspect the retained task workspace and clean or discard its changes before cleanup")
    if task_worktree is not None and not worktree_dirty_paths:
        recommended_actions.append("run `blackdog task cleanup` if the task workspace is no longer needed")
    recommended_commands: list[dict[str, str]] = []
    if stale_claim:
        recommended_commands.append(
            _command_row(
                'blackdog task recover --release-stale-claim --status blocked|failed|abandoned --summary "..."',
                reason="release a stale task claim without deleting retained work",
                disposition="retryable_after_operator_choice",
            )
        )
    elif active_attempt is not None:
        if worktree_dirty_paths or branch_ahead:
            recommended_commands.append(
                _command_row(
                    "blackdog task land --summary \"...\"",
                    reason="land the active task attempt through the canonical success path",
                    disposition="auto_safe_after_validation",
                )
            )
        recommended_commands.append(
            _command_row(
                "blackdog task close --status blocked|failed|abandoned --summary \"...\"",
                reason="close the active attempt without landing code",
                disposition="operator_choice",
            )
        )
    elif selected_attempt is not None and reference_issue:
        recommended_commands.append(
            _command_row(
                "blackdog task cancel --summary \"...\"",
                reason="keep stale task state out of normal ready work",
                disposition="operator_choice",
            )
        )
    elif task_worktree is None and not landed_cleanup_complete:
        recommended_commands.append(
            _command_row(
                "blackdog task begin --prompt \"...\"",
                reason="start a new WTAM attempt for this task",
                disposition="auto_safe",
            )
        )
    if task_worktree is not None and not worktree_dirty_paths:
        recommended_commands.append(
            _command_row(
                "blackdog task cleanup",
                reason="remove retained task workspace after proving it is disposable",
                disposition="auto_safe_if_cleanup_ready",
            )
        )
    elif task_worktree is not None:
        recommended_commands.append(
            _command_row(
                "git status --short",
                reason="inspect retained task workspace before cleanup",
                disposition="read_only",
            )
        )
    return {
        "workset_id": workset_id,
        "task_id": task_id,
        "task_title": task.title,
        "task_runtime_status": runtime_task_state.status,
        "task_runtime_note": runtime_task_state.note,
        "task_runtime_updated_at": runtime_task_state.updated_at,
        "active_attempt": active_attempt is not None,
        "attempt_id": selected_attempt.attempt_id if selected_attempt is not None else None,
        "latest_attempt_id": latest_attempt.attempt_id if latest_attempt is not None else None,
        "latest_attempt_status": latest_attempt.status if latest_attempt is not None else None,
        "latest_attempt_summary": latest_attempt.summary if latest_attempt is not None else None,
        "actor": selected_attempt.actor if selected_attempt is not None else None,
        "branch": branch,
        "target_branch": target_branch,
        "worktree_path": str(task_worktree) if task_worktree is not None else recorded_worktree_path,
        "worktree_exists": worktree_exists,
        "worktree_dirty": bool(worktree_dirty_paths),
        "worktree_dirty_paths": worktree_dirty_paths,
        "branch_ahead_of_target": branch_ahead,
        "branch_exists": branch_exists,
        "target_branch_exists": target_branch_exists,
        "branch_ahead_error": branch_ahead_error,
        "changed_paths": _attempt_changed_paths(
            profile,
            branch=branch,
            target_branch=target_branch,
            worktree_path=task_worktree,
            primary_root=primary_root,
            changed_paths_cache=changed_paths_cache,
        ),
        "task_claim": (
            {
                "actor": current_task_claim.actor,
                "execution_model": current_task_claim.execution_model,
                "claimed_at": current_task_claim.claimed_at,
                "attempt_id": current_task_claim.attempt_id,
                "note": current_task_claim.note,
            }
            if current_task_claim is not None
            else None
        ),
        "workset_claim": (
            {
                "actor": current_workset_claim.actor,
                "execution_model": current_workset_claim.execution_model,
                "claimed_at": current_workset_claim.claimed_at,
                "note": current_workset_claim.note,
            }
            if current_workset_claim is not None
            else None
        ),
        "stale_claim": stale_claim,
        "recovery_state": recovery_state,
        "failure_class": failure_class,
        "recovery_action": recovery_action,
        "prompt_issue": prompt_issue,
        "operator_issue": operator_issue,
        "setup_receipt": selected_attempt.setup_receipt if selected_attempt is not None else None,
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
        "primary_worktree": str(primary_root),
        "primary_dirty": primary_dirty,
        "primary_dirty_paths": primary_dirty_paths,
        "recommended_actions": recommended_actions,
        "recommended_commands": recommended_commands,
    }


def _release_stale_task_claim(
    profile: RepoProfile,
    *,
    workset_id: str,
    task_id: str,
    status: str,
    summary: str,
    note: str | None = None,
) -> dict[str, Any]:
    workset, _task = _require_workset_and_task(profile, workset_id=workset_id, task_id=task_id)
    if status not in {ATTEMPT_STATUS_BLOCKED, ATTEMPT_STATUS_FAILED, ATTEMPT_STATUS_ABANDONED}:
        raise BacklogError("task recover stale-claim status must be one of blocked, failed, abandoned")
    resolved_summary = str(summary or "").strip()
    if not resolved_summary:
        raise BacklogError("task recover --release-stale-claim requires --summary")
    released_at: str | None = None
    stale_task_claim = None
    current_workset_claim = None
    repaired_runtime_status: str | None = None
    release_workset_claim = False
    failure_details = _failure_details_for_status(status, recovery_action="release_stale_claim")

    def mutate(runtime_state):
        nonlocal released_at, stale_task_claim, current_workset_claim, repaired_runtime_status, release_workset_claim
        if active_task_attempt(runtime_state, workset_id, task_id) is not None:
            raise BacklogError("task recover can only release a stale claim when no active WTAM attempt exists")
        runtime_task_claims = task_claim_index(runtime_state, workset_id)
        stale_task_claim = runtime_task_claims.get(task_id)
        if stale_task_claim is None:
            raise BacklogError("task recover did not find a stale task claim to release")
        released_at = now_iso()
        current_task_state = task_state_index(runtime_state, workset_id).get(task_id)
        incoming_records: tuple[TaskRuntimeRecord, ...] | None = None
        if current_task_state is not None and current_task_state.status == TASK_STATUS_IN_PROGRESS:
            repaired_runtime_status = TASK_STATUS_CANCELED if status == ATTEMPT_STATUS_ABANDONED else TASK_STATUS_BLOCKED
            incoming_records = (
                TaskRuntimeRecord(
                    task_id=task_id,
                    status=repaired_runtime_status,
                    updated_at=released_at,
                    note=resolved_summary,
                    **failure_details,
                ),
            )
        current_workset_claim = workset_claim(runtime_state, workset_id)
        remaining_task_claims = tuple(
            claim
            for claim_task_id, claim in runtime_task_claims.items()
            if claim_task_id != task_id
        )
        release_workset_claim = current_workset_claim is not None and not remaining_task_claims
        return merge_workset_runtime(
            runtime_state,
            workset_id=workset_id,
            task_ids={item.task_id for item in workset.tasks},
            incoming_records=incoming_records,
            incoming_workset_claim=None if release_workset_claim else current_workset_claim,
            released_task_claim_ids=(task_id,),
        )

    mutate_runtime_state(profile.paths, mutate)
    if stale_task_claim is None or released_at is None:
        raise BacklogError("task recover did not release a stale task claim")
    event_actor = str(stale_task_claim.actor or (current_workset_claim.actor if current_workset_claim is not None else "")).strip() or "blackdog"
    append_event(
        profile.paths.events_file,
        event_type="task.release",
        actor=event_actor,
        payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "attempt_id": stale_task_claim.attempt_id,
            "released_at": released_at,
            "status": status,
            "summary": resolved_summary,
            "note": note,
            "recovery": "stale_claim",
            "repaired_runtime_status": repaired_runtime_status,
            **failure_details,
        },
    )
    if release_workset_claim:
        append_event(
            profile.paths.events_file,
            event_type="workset.release",
            actor=event_actor,
            payload={
                "workset_id": workset_id,
                "released_at": released_at,
                "status": status,
                "summary": resolved_summary,
                "note": note,
                "recovery": "stale_claim",
                **failure_details,
            },
        )
    payload = _task_recovery_payload(profile, workset_id=workset_id, task_id=task_id)
    payload["released_stale_claim"] = True
    payload["released_attempt_id"] = stale_task_claim.attempt_id
    payload["release_status"] = status
    payload["release_summary"] = resolved_summary
    payload["release_note"] = note
    payload["released_workset_claim"] = release_workset_claim
    payload["repaired_runtime_status"] = repaired_runtime_status
    payload.update(failure_details)
    return payload


def recover_task(
    profile: RepoProfile,
    *,
    workset_id: str | None = None,
    task_id: str | None = None,
    release_stale_claim: bool = False,
    status: str | None = None,
    summary: str | None = None,
    note: str | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    if not release_stale_claim and any(item is not None for item in (status, summary, note)):
        raise BacklogError("task recover only accepts --status, --summary, and --note with --release-stale-claim")
    resolved_workset, resolved_task, _attempt = _resolve_task_command_target(
        profile,
        workset_id=workset_id,
        task_id=task_id,
        cwd=cwd,
        allow_latest=True,
    )
    if not release_stale_claim:
        payload = _task_recovery_payload(profile, workset_id=resolved_workset, task_id=resolved_task)
        payload["recommended_actions"] = _task_surface_actions(list(payload["recommended_actions"]))
        payload["released_stale_claim"] = False
        return payload
    payload = _release_stale_task_claim(
        profile,
        workset_id=resolved_workset,
        task_id=resolved_task,
        status=str(status or "").strip(),
        summary=str(summary or "").strip(),
        note=note,
    )
    payload["recommended_actions"] = _task_surface_actions(list(payload["recommended_actions"]))
    return payload


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
            if _managed_status_dirty(profile, target_worktree):
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
            branch_worktree = _find_worktree_for_branch(primary_root, f"refs/heads/{resolved_branch}")
            rebase_location = f" -C {branch_worktree}" if branch_worktree is not None else ""
            raise WorktreeError(
                f"cannot land: {resolved_branch} is not based on the current {resolved_target}; "
                f"rebase it first with `git{rebase_location} rebase {resolved_target}`"
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
            if _managed_status_dirty(profile, branch_worktree):
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


def _failure_details_for_status(status: str, *, recovery_action: str | None = None) -> dict[str, Any]:
    if status == ATTEMPT_STATUS_ABANDONED:
        return {
            "failure_class": FAILURE_CLASS_ABANDONED,
            "recovery_action": recovery_action or "reopen_if_needed",
            "prompt_issue": False,
            "operator_issue": True,
        }
    return {
        "failure_class": FAILURE_CLASS_UNKNOWN,
        "recovery_action": recovery_action or "inspect",
        "prompt_issue": False,
        "operator_issue": False,
    }


def _failure_details_for_land_error(exc: Exception) -> dict[str, Any]:
    message = str(exc)
    if "dirty primary worktree" in message:
        return {
            "failure_class": FAILURE_CLASS_DIRTY_PRIMARY,
            "recovery_action": "clean_primary_worktree",
            "prompt_issue": False,
            "operator_issue": True,
        }
    if "is not based on the current" in message:
        return {
            "failure_class": FAILURE_CLASS_STALE_BRANCH,
            "recovery_action": "rebase_task_branch",
            "prompt_issue": False,
            "operator_issue": True,
        }
    if "missing" in message and "worktree" in message:
        return {
            "failure_class": FAILURE_CLASS_MISSING_WORKTREE,
            "recovery_action": "restore_or_cleanup_worktree",
            "prompt_issue": False,
            "operator_issue": True,
        }
    if "has no changes relative to" in message:
        return {
            "failure_class": FAILURE_CLASS_NO_CHANGES,
            "recovery_action": "close_no_change_attempt",
            "prompt_issue": False,
            "operator_issue": False,
        }
    return _failure_details_for_status(ATTEMPT_STATUS_BLOCKED)


def _terminal_land_failure_status(exc: Exception) -> str | None:
    message = str(exc)
    if "has no changes relative to" in message:
        return ATTEMPT_STATUS_BLOCKED
    return None


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
    changed_paths = tuple(
        _attempt_changed_paths(
            profile,
            branch=attempt.branch,
            target_branch=attempt.target_branch,
            worktree_path=task_worktree,
        )
    )
    branch_head_commit: str | None = None
    commit_message = _canonical_commit_message(
        workset,
        task,
        attempt_id=attempt.attempt_id,
        actor=actor,
        changed_paths=changed_paths,
        prompt_receipt=attempt.prompt_receipt,
        user_prompt_receipt=attempt.user_prompt_receipt,
        codex_session=attempt.codex_session,
        execution_model=attempt.execution_model,
        model=attempt.model,
        reasoning_effort=attempt.reasoning_effort,
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
        if branch_head_commit is None:
            completed = _run_git_no_check(find_primary_worktree(profile.paths.project_root), "rev-parse", attempt.branch)
            if completed.returncode == 0:
                branch_head_commit = completed.stdout.strip() or None
        terminal_status = _terminal_land_failure_status(exc)
        failure_details = _failure_details_for_land_error(exc)
        if terminal_status is not None:
            payload = close_task_worktree(
                profile,
                workset_id=workset_id,
                task_id=task_id,
                actor=actor,
                status=terminal_status,
                summary=f"Landing closed: {exc}",
                validations=validations,
                residuals=residuals,
                followup_candidates=followup_candidates,
                note=note or str(exc),
                cleanup=cleanup,
                **failure_details,
            )
            payload["error"] = str(exc)
            payload["attempt_active"] = False
            payload["land_failure_disposition"] = "closed"
            payload.update(failure_details)
            payload["recommended_actions"] = []
            if not payload.get("cleanup_performed"):
                payload["recommended_actions"].append("run `blackdog task cleanup` if the task workspace is no longer needed")
            return payload
        recommended_actions = []
        if failure_details["failure_class"] == FAILURE_CLASS_STALE_BRANCH:
            rebase_location = f" -C {task_worktree}" if task_worktree is not None else ""
            recommended_actions.append(
                f"rebase the task branch onto {attempt.target_branch}: `git{rebase_location} rebase {attempt.target_branch}`"
            )
        recommended_actions.extend(
            [
                "fix the landing blocker and rerun `blackdog task land` from the task worktree, "
                "or `blackdog worktree land` with --workset/--task",
                "run `blackdog task close --status blocked|failed|abandoned` from the task worktree, "
                "or `blackdog worktree close --status blocked|failed|abandoned` with --workset/--task",
            ]
        )
        return {
            "branch": attempt.branch,
            "target_branch": attempt.target_branch,
            "primary_worktree": str(find_primary_worktree(profile.paths.project_root)),
            "target_worktree": None,
            "landing_worktree": None,
            "landed_commit": None,
            "diff_file": None,
            "diffstat_file": None,
            "changed_paths": list(changed_paths),
            "cleanup": cleanup,
            "cleaned_worktree": None,
            "deleted_branch": False,
            "removed_temporary_target": False,
            "attempt_id": attempt.attempt_id,
            "task_id": attempt.task_id,
            "status": "blocked",
            "summary": f"Landing blocked: {exc}",
            "commit": branch_head_commit,
            "commit_message": commit_message,
            "worktree_path": str(task_worktree) if task_worktree is not None else None,
            "error": str(exc),
            "attempt_active": True,
            "land_failure_disposition": "retryable",
            **failure_details,
            "recommended_actions": recommended_actions,
        }

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
    return _task_recovery_payload(profile, workset_id=workset_id, task_id=task_id)


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
    failure_class: str | None = None,
    recovery_action: str | None = None,
    prompt_issue: bool = False,
    operator_issue: bool = False,
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
    failure_details = _failure_details_for_status(status, recovery_action=recovery_action)
    if failure_class is not None:
        failure_details["failure_class"] = failure_class
    failure_details["prompt_issue"] = bool(prompt_issue or failure_details["prompt_issue"])
    failure_details["operator_issue"] = bool(operator_issue or failure_details["operator_issue"])
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
        **failure_details,
    )
    cleanup_reason: str | None = None
    cleanup_payload: dict[str, Any] | None = None
    if cleanup and task_worktree is not None and task_worktree.exists():
        if _managed_status_dirty(profile, task_worktree):
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
            **failure_details,
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
        **failure_details,
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
    path_exists = resolved_path.exists()
    worktree_exists = path_exists and _is_git_worktree_path(resolved_path)
    active_attempt = active_task_attempt(runtime_state, workset_id, task_id)
    if active_attempt is not None:
        if not worktree_exists:
            raise WorktreeError(f"active task worktree path is missing or is not a git worktree: {resolved_path}")
        raise WorktreeError("refusing cleanup: active attempts must be landed or closed before cleanup")
    if worktree_exists and _managed_status_dirty(profile, resolved_path):
        raise WorktreeError(f"refusing cleanup: worktree has uncommitted changes: {resolved_path}")
    branch_cleanup = (
        _plan_task_branch_cleanup(
            primary_root,
            branch=resolved_branch,
            latest_attempt=latest_attempt,
        )
        if resolved_branch
        else _BranchCleanupPlan(
            branch_exists=False,
            force_delete=False,
            branch_tip=None,
            reason="no branch recorded",
            proof_state="no_branch",
        )
    )
    if worktree_exists:
        _run_git(primary_root, "worktree", "remove", str(resolved_path))
    deleted_branch = False
    if resolved_branch and branch_cleanup.branch_exists:
        current_tip = _resolve_commit(primary_root, resolved_branch)
        if current_tip != branch_cleanup.branch_tip:
            raise WorktreeError(f"refusing cleanup: branch {resolved_branch} changed during cleanup")
        delete_flag = "-D" if branch_cleanup.force_delete else "-d"
        delete = _run_git_no_check(primary_root, "branch", delete_flag, resolved_branch)
        if delete.returncode == 0:
            deleted_branch = True
        else:
            detail = delete.stderr.strip() or delete.stdout.strip() or f"exit code {delete.returncode}"
            raise WorktreeError(f"git branch {delete_flag} {resolved_branch} failed: {detail}")
    append_event(
        profile.paths.events_file,
        event_type="worktree.cleanup",
        payload={
            "workset_id": workset_id,
            "task_id": task_id,
            "branch": resolved_branch,
            "worktree_path": str(resolved_path),
            "deleted_branch": deleted_branch,
            "branch_cleanup_reason": branch_cleanup.reason,
            "branch_cleanup_proof": branch_cleanup.proof_state,
            "force_deleted_branch": bool(deleted_branch and branch_cleanup.force_delete),
        },
    )
    return {
        "worktree_path": str(resolved_path),
        "worktree_existed": worktree_exists,
        "branch": resolved_branch,
        "deleted_branch": deleted_branch,
        "branch_cleanup_reason": branch_cleanup.reason,
        "branch_cleanup_proof": branch_cleanup.proof_state,
        "force_deleted_branch": bool(deleted_branch and branch_cleanup.force_delete),
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
        f"{prefix} setup: {spec.setup_receipt.get('status', 'unknown')} task_class={spec.setup_receipt.get('task_class', 'unknown')}",
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
        action = "closed" if payload.get("land_failure_disposition") == "closed" else "blocked"
        lines = [
            f"{prefix} land {action}: {payload['branch']} -> {payload['target_branch']}",
            f"{prefix} attempt: {payload['attempt_id']}",
            f"{prefix} attempt remains active: {'yes' if payload.get('attempt_active') else 'no'}",
        ]
        if payload.get("summary"):
            lines.append(f"{prefix} summary: {payload['summary']}")
        if payload.get("failure_class"):
            lines.append(f"{prefix} failure class: {payload['failure_class']}")
        if payload.get("recovery_action"):
            lines.append(f"{prefix} recovery action: {payload['recovery_action']}")
        if payload.get("worktree_path"):
            lines.append(f"{prefix} {workspace_label}: {payload['worktree_path']}")
        if payload.get("commit"):
            lines.append(f"{prefix} branch commit: {payload['commit']}")
        if payload.get("changed_paths"):
            lines.append(f"{prefix} changed paths: {', '.join(payload['changed_paths'])}")
        if payload.get("cleanup_performed") and payload.get("cleanup"):
            lines.append(f"{prefix} removed {workspace_label}: {payload['cleanup']['worktree_path']}")
        elif payload.get("cleanup_reason"):
            lines.append(f"{prefix} cleanup: {payload['cleanup_reason']}")
        if payload.get("error"):
            lines.append(f"{prefix} error: {payload['error']}")
        if payload.get("recommended_actions"):
            recommended_actions = list(payload["recommended_actions"])
            lines.append(f"{prefix} recommended actions:")
            lines.extend(f"  - {item}" for item in recommended_actions)
        return "\n".join(lines) + "\n"
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


def render_task_state_text(payload: dict[str, Any]) -> str:
    lines = [
        f"[blackdog-task] state: {payload['workset_id']}/{payload['task_id']} {payload['status']}",
        f"[blackdog-task] actor: {payload['actor']}",
    ]
    if payload.get("updated_at"):
        lines.append(f"[blackdog-task] updated at: {payload['updated_at']}")
    if payload.get("summary"):
        lines.append(f"[blackdog-task] summary: {payload['summary']}")
    if payload.get("failure_class"):
        lines.append(f"[blackdog-task] failure class: {payload['failure_class']}")
    if payload.get("recovery_action"):
        lines.append(f"[blackdog-task] recovery action: {payload['recovery_action']}")
    return "\n".join(lines) + "\n"


def render_task_begin_text(spec: TaskBeginSpec, *, show_prompt: bool = False) -> str:
    lines = [
        f"[blackdog-task] begin: {spec.task_id} actor={spec.actor}",
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
    if payload.get("branch_exists") is False:
        lines.append(f"{prefix} branch exists: no")
    if payload.get("target_branch_exists") is False:
        lines.append(f"{prefix} target branch exists: no")
    if payload.get("branch_ahead_error"):
        lines.append(f"{prefix} branch ahead check: {payload['branch_ahead_error']}")
    lines.append(f"{prefix} {workspace_label} exists: {'yes' if payload['worktree_exists'] else 'no'}")
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
    if payload.get("failure_class"):
        lines.append(f"{prefix} failure class: {payload['failure_class']}")
    if payload.get("recovery_action"):
        lines.append(f"{prefix} recovery action: {payload['recovery_action']}")
    if payload["recommended_actions"]:
        lines.append(f"{prefix} recommended actions:")
        lines.extend(f"  - {item}" for item in payload["recommended_actions"])
    return "\n".join(lines) + "\n"


def render_recover_text(payload: dict[str, Any]) -> str:
    prefix = "[blackdog-task]"
    workspace_label = "task workspace"
    lines = [
        f"{prefix} recover: {payload['task_id']} {payload['task_title']}",
        f"{prefix} recovery state: {payload['recovery_state']}",
        f"{prefix} task runtime: {payload['task_runtime_status']}",
        f"{prefix} active attempt: {'yes' if payload['active_attempt'] else 'no'}",
        f"{prefix} stale claim: {'yes' if payload['stale_claim'] else 'no'}",
    ]
    if payload.get("released_stale_claim"):
        lines.append(f"{prefix} released stale claim: yes")
        lines.append(f"{prefix} release status: {payload['release_status']}")
        lines.append(f"{prefix} release summary: {payload['release_summary']}")
        if payload.get("repaired_runtime_status"):
            lines.append(f"{prefix} repaired task runtime: {payload['repaired_runtime_status']}")
    task_claim = payload.get("task_claim")
    if task_claim is not None:
        lines.append(
            f"{prefix} task claim: {task_claim['actor']}/{task_claim['execution_model']} claimed_at={task_claim['claimed_at']}"
        )
        if task_claim.get("attempt_id"):
            lines.append(f"{prefix} claimed attempt: {task_claim['attempt_id']}")
    workset_claim = payload.get("workset_claim")
    if workset_claim is not None:
        lines.append(
            f"{prefix} workset claim: {workset_claim['actor']}/{workset_claim['execution_model']} claimed_at={workset_claim['claimed_at']}"
        )
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
    if payload.get("branch_exists") is False:
        lines.append(f"{prefix} branch exists: no")
    if payload.get("target_branch_exists") is False:
        lines.append(f"{prefix} target branch exists: no")
    if payload.get("branch_ahead_error"):
        lines.append(f"{prefix} branch ahead check: {payload['branch_ahead_error']}")
    lines.append(f"{prefix} {workspace_label} exists: {'yes' if payload['worktree_exists'] else 'no'}")
    lines.append(f"{prefix} {workspace_label} dirty: {'yes' if payload['worktree_dirty'] else 'no'}")
    lines.append(f"{prefix} branch ahead of target: {'yes' if payload['branch_ahead_of_target'] else 'no'}")
    lines.append(f"{prefix} primary dirty: {'yes' if payload['primary_dirty'] else 'no'}")
    if payload["worktree_dirty_paths"]:
        lines.append(f"{prefix} {workspace_label} dirty paths: {', '.join(payload['worktree_dirty_paths'])}")
    if payload["changed_paths"]:
        lines.append(f"{prefix} attempt paths: {', '.join(payload['changed_paths'])}")
    if payload.get("task_runtime_note"):
        lines.append(f"{prefix} task note: {payload['task_runtime_note']}")
    if payload.get("failure_class"):
        lines.append(f"{prefix} failure class: {payload['failure_class']}")
    if payload.get("recovery_action"):
        lines.append(f"{prefix} recovery action: {payload['recovery_action']}")
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
    if payload.get("failure_class"):
        lines.append(f"{prefix} failure class: {payload['failure_class']}")
    if payload.get("recovery_action"):
        lines.append(f"{prefix} recovery action: {payload['recovery_action']}")
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


def render_worktree_table_text(payload: dict[str, Any]) -> str:
    rows = payload["rows"]
    columns = payload["columns"]
    if not rows:
        return "\t".join(columns) + "\n"
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join("" if row.get(column) is None else str(row.get(column)) for column in columns))
    return "\n".join(lines) + "\n"


def render_worktree_cleanup_all_text(payload: dict[str, Any]) -> str:
    lines = [
        (
            f"[blackdog-worktree] cleanup all: cleaned={len(payload['cleaned'])} "
            f"skipped={len(payload['skipped'])} errors={len(payload['errors'])}"
        ),
        f"[blackdog-worktree] remaining rows: {payload['remaining']['counts']['rows']}",
    ]
    for row in payload["cleaned"]:
        branch = f" branch={row['branch']}" if row.get("branch") else ""
        lines.append(f"[blackdog-worktree] removed: {row['worktree_path']}{branch}")
    for row in payload["errors"]:
        lines.append(f"[blackdog-worktree] error: {row['workset_id']}/{row['task_id']} {row['error']}")
    return "\n".join(lines) + "\n"


__all__ = [
    "DirtyPrimaryWorktreeError",
    "TaskBeginSpec",
    "WORKTREE_TABLE_COLUMNS",
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
    "cancel_task",
    "build_worktree_table",
    "cleanup_task",
    "cleanup_task_worktree",
    "cleanup_worktree_table",
    "close_task",
    "close_task_worktree",
    "command_workspace_root",
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
    "render_recover_text",
    "render_show_text",
    "render_start_text",
    "render_task_begin_text",
    "render_task_state_text",
    "render_worktree_cleanup_all_text",
    "render_worktree_table_text",
    "recover_task",
    "reopen_task",
    "show_task",
    "start_task_worktree",
    "worktree_contract",
    "worktree_preflight",
]
