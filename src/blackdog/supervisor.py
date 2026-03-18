from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO
import json
import os
import queue
import shlex
import subprocess
import threading
import textwrap
import time
import uuid

from .backlog import (
    BacklogError,
    BacklogSnapshot,
    TaskInfo,
    classify_task_status,
    load_backlog,
    next_runnable_tasks,
    sweep_completed_tasks,
    sync_state_for_backlog,
    task_done,
)
from .config import DEFAULT_SUPERVISOR_COMMAND, Profile
from .scaffold import render_project_html
from .store import (
    atomic_write_text,
    append_event,
    claim_task_entry,
    load_inbox,
    load_state,
    load_task_results,
    locked_state,
    now_iso,
    record_task_result,
    resolve_message,
    save_state,
    send_message,
)
from .worktree import (
    DirtyPrimaryWorktreeError,
    WorktreeError,
    WorktreeSpec,
    branch_ahead_of_target,
    land_branch,
    rebase_branch_onto_target,
    supervisor_task_branch,
    supervisor_task_worktree_path,
    start_task_worktree,
    worktree_contract,
)


class SupervisorError(RuntimeError):
    pass


DESKTOP_CODEX_BINARY = Path("/Applications/Codex.app/Contents/Resources/codex")
SUPERVISOR_STATUS_READY_LIMIT = 8
SUPERVISOR_STATUS_RESULT_LIMIT = 5
SUPERVISOR_STATUS_CONTROL_LIMIT = 8
DEFAULT_SUPERVISOR_POLL_INTERVAL_SECONDS = 1.0
CLAIM_LIVENESS_SCAN_INTERVAL_SECONDS = 60.0
CLAIM_LIVENESS_MISSING_SCAN_LIMIT = 2


@dataclass
class ChildRun:
    task: TaskInfo
    child_agent: str
    launch_command: tuple[str, ...]
    workspace: Path
    workspace_mode: str
    run_dir: Path
    prompt_file: Path
    stdout_file: Path
    stderr_file: Path
    message_id: str | None
    result_files_before: set[str]
    process: subprocess.Popen[str] | None
    stdout_handle: TextIO | None
    stderr_handle: TextIO | None
    started_at: float
    worktree_spec: WorktreeSpec | None = None
    launch_error: str | None = None
    exit_code: int | None = None
    missing_process: bool = False
    result_recorded: bool = False
    final_task_status: str | None = None
    branch_ahead: bool = False
    land_result: dict[str, Any] | None = None
    landed: bool = False
    land_error: str | None = None
    land_needs_user_input: bool = False
    land_followup_candidates: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PreparedWorkspace:
    workspace: Path
    worktree_spec: WorktreeSpec | None = None


def _preferred_blackdog_command(profile: Profile, *, workspace: Path | None = None) -> str:
    candidate = ((workspace or profile.paths.project_root) / ".VE" / "bin" / "blackdog").resolve()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return shlex.quote(str(candidate))
    return "./.VE/bin/blackdog"


def _notify_supervisor(
    profile: Profile,
    *,
    actor: str,
    task_id: str | None,
    kind: str,
    tags: list[str],
    body: str,
) -> None:
    send_message(
        profile.paths,
        sender="blackdog",
        recipient=actor,
        body=body,
        kind=kind,
        task_id=task_id,
        tags=tags,
    )


def _emit_render(profile: Profile) -> None:
    if profile.auto_render_html:
        render_project_html(profile)


def _supervisor_text(view: dict[str, Any]) -> str:
    lines = [
        f"Supervisor run: {view['run_id']}",
        f"Launch actor: {view['actor']}",
        f"Workspace mode: {view['workspace_mode']}",
        f"Final status: {view.get('final_status') or 'running'}",
        f"Steps: {len(view.get('steps', []))}",
        f"Tasks launched: {len(view['children'])}",
    ]
    if view.get("draining"):
        lines.append("Draining: yes")
    if view.get("status_file"):
        lines.append(f"Status file: {view['status_file']}")
    for child in view["children"]:
        exit_text = "launch-error" if child["launch_error"] else child["exit_code"]
        if child["missing_process"]:
            exit_text = "interrupted"
        lines.append(
            f"- {child['task_id']} -> {child['child_agent']} | {child['workspace_mode']} | exit {exit_text} | final {child['final_task_status']}"
        )
    return "\n".join(lines) + "\n"


def render_supervisor_output(view: dict[str, Any], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(view, indent=2) + "\n"
    return _supervisor_text(view)


def _supervisor_status_text(view: dict[str, Any]) -> str:
    lines = [f"Supervisor actor: {view['actor']}"]
    latest_run = view.get("latest_run")
    if isinstance(latest_run, dict):
        lines.append(
            f"Latest run: {latest_run['status']} | {latest_run['run_id']} | steps {latest_run['step_count']} | workspace {latest_run['workspace_mode']}"
        )
        lines.append(f"Status file: {latest_run['status_file']}")
        last_step = latest_run.get("last_step")
        if isinstance(last_step, dict):
            lines.append(f"Last step: {last_step.get('status')} @ {last_step.get('at')}")
    else:
        lines.append("Latest run: none")
        lines.append("Status file: none")
    contract = view.get("workspace_contract")
    if isinstance(contract, dict):
        primary_state = "dirty" if contract.get("primary_dirty") else "clean"
        local_ve_state = "ready" if contract.get("workspace_has_local_blackdog") else "missing"
        lines.append(
            "WTAM contract: "
            f"{contract.get('workspace_mode') or 'unknown'} -> {contract.get('target_branch') or '?'}"
            f" | primary {primary_state}"
            f" | local .VE {local_ve_state}"
        )
        if contract.get("primary_dirty_paths"):
            lines.append("Primary dirty paths: " + ", ".join(str(item) for item in contract["primary_dirty_paths"]))
        lines.append(f".VE rule: {contract.get('ve_expectation') or ''}")
    control = view.get("control_action")
    if isinstance(control, dict):
        lines.append(f"Run control: {control['action']} via {control['message_id']}")

    lines.extend(["", "Open supervisor controls:"])
    if view["open_control_messages"]:
        for message in view["open_control_messages"]:
            lines.append(
                f"- {message['message_id']} {message['sender']} [{message['control_action']}] {message['body']}"
            )
    else:
        lines.append("- No open control messages.")

    lines.extend(["", "Ready tasks:"])
    if view["ready_tasks"]:
        for task in view["ready_tasks"]:
            lines.append(f"- {task['id']} [{task['risk']}] {task['title']}")
    else:
        lines.append("- No runnable tasks.")

    lines.extend(["", "Recent child-run results:"])
    if view["recent_results"]:
        for result in view["recent_results"]:
            lines.append(
                f"- {result['task_id']} [{result['status']}] {result['actor']} {result['recorded_at']} {result['title']}"
            )
    else:
        lines.append("- No recent child-run results.")
    return "\n".join(lines) + "\n"


def render_supervisor_status_output(view: dict[str, Any], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(view, indent=2) + "\n"
    return _supervisor_status_text(view)


def _select_tasks(
    snapshot: BacklogSnapshot,
    state: dict[str, Any],
    *,
    task_ids: list[str],
    allow_high_risk: bool,
    limit: int,
    force: bool,
) -> list[TaskInfo]:
    if task_ids:
        selected: list[TaskInfo] = []
        for task_id in task_ids:
            task = snapshot.tasks.get(task_id)
            if task is None:
                raise BacklogError(f"Unknown task id: {task_id}")
            status, detail = classify_task_status(task, snapshot, state, allow_high_risk=allow_high_risk)
            if status != "ready" and not force:
                raise BacklogError(f"Task {task_id} is not launchable: {detail}")
            selected.append(task)
        return selected[:limit]
    return next_runnable_tasks(snapshot, state, allow_high_risk=allow_high_risk, limit=limit)


def _load_synced_runtime(profile: Profile) -> tuple[BacklogSnapshot, dict[str, Any]]:
    snapshot = load_backlog(profile.paths, profile)
    state = load_state(profile.paths.state_file)
    state = sync_state_for_backlog(state, snapshot)
    save_state(profile.paths.state_file, state)
    return snapshot, state


def _run_control_action(messages: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None]:
    for message in messages:
        action = _message_control_action(message)
        if action == "stop":
            return "stop", message
    return None, None


def _message_control_action(message: dict[str, Any]) -> str | None:
    tags = {str(tag).strip().lower() for tag in message.get("tags") or []}
    body = str(message.get("body") or "").strip().lower()
    if "stop" in tags or body.startswith("stop"):
        return "stop"
    return None


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _latest_run_status(profile: Profile, *, actor: str) -> dict[str, Any] | None:
    status_files = sorted(profile.paths.supervisor_runs_dir.glob("*/status.json"), reverse=True)
    for status_file in status_files:
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("actor") or "") != actor:
            continue
        if not str(payload.get("run_id") or "").strip():
            continue
        steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
        last_step = steps[-1] if steps and isinstance(steps[-1], dict) else None
        status = str(payload.get("final_status") or (last_step or {}).get("status") or "running")
        if not payload.get("final_status") and not _pid_alive(payload.get("supervisor_pid")):
            status = "interrupted"
        return {
            "run_id": payload.get("run_id"),
            "actor": payload.get("actor"),
            "status": status,
            "workspace_mode": payload.get("workspace_mode"),
            "poll_interval_seconds": payload.get("poll_interval_seconds"),
            "draining": bool(payload.get("draining")),
            "run_dir": payload.get("run_dir") or str(status_file.parent),
            "status_file": str(status_file),
            "step_count": len(steps),
            "last_step": last_step,
            "completed_at": payload.get("completed_at"),
            "final_status": payload.get("final_status"),
            "stopped_by_message_id": payload.get("stopped_by_message_id"),
            "supervisor_pid": payload.get("supervisor_pid"),
        }
    return None


def build_supervisor_status_view(
    profile: Profile,
    *,
    actor: str,
    allow_high_risk: bool,
) -> dict[str, Any]:
    snapshot = load_backlog(profile.paths, profile)
    state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
    latest_run = _latest_run_status(profile, actor=actor)
    workspace_mode = str((latest_run or {}).get("workspace_mode") or profile.supervisor_workspace_mode)
    open_messages = load_inbox(profile.paths, recipient=actor, status="open")
    control_messages = []
    for message in open_messages:
        action = _message_control_action(message)
        if action is None:
            continue
        control_messages.append(
            {
                "message_id": str(message.get("message_id") or ""),
                "at": message.get("at"),
                "sender": message.get("sender"),
                "recipient": message.get("recipient"),
                "kind": message.get("kind"),
                "task_id": message.get("task_id"),
                "tags": list(message.get("tags") or []),
                "body": message.get("body"),
                "control_action": action,
            }
        )
        if len(control_messages) >= SUPERVISOR_STATUS_CONTROL_LIMIT:
            break
    control_action, control_message = _run_control_action(open_messages)
    ready_tasks = [
        {
            "id": task.id,
            "title": task.title,
            "lane": task.lane_title,
            "wave": task.wave,
            "risk": task.payload["risk"],
            "priority": task.payload["priority"],
        }
        for task in next_runnable_tasks(
            snapshot,
            state,
            allow_high_risk=allow_high_risk,
            limit=SUPERVISOR_STATUS_READY_LIMIT,
        )
    ]
    recent_results = []
    child_actor_prefix = f"{actor}/child-"
    for row in load_task_results(profile.paths):
        result_actor = str(row.get("actor") or "")
        if result_actor != actor and not result_actor.startswith(child_actor_prefix):
            continue
        task_id = str(row.get("task_id") or "")
        task = snapshot.tasks.get(task_id)
        recent_results.append(
            {
                "task_id": task_id,
                "title": task.title if task is not None else "",
                "status": row.get("status"),
                "actor": result_actor,
                "run_id": row.get("run_id"),
                "recorded_at": row.get("recorded_at"),
                "needs_user_input": bool(row.get("needs_user_input")),
                "result_file": row.get("result_file"),
            }
        )
        if len(recent_results) >= SUPERVISOR_STATUS_RESULT_LIMIT:
            break
    return {
        "actor": actor,
        "latest_run": latest_run,
        "workspace_contract": worktree_contract(profile, workspace_mode=workspace_mode),
        "control_action": (
            {
                "action": control_action,
                "message_id": str(control_message.get("message_id") or ""),
            }
            if control_action is not None and control_message is not None
            else None
        ),
        "open_control_messages": control_messages,
        "ready_tasks": ready_tasks,
        "recent_results": recent_results,
    }


def _write_run_status(status_file: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(status_file, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _claim_for_child(profile: Profile, snapshot: BacklogSnapshot, task: TaskInfo, *, child_agent: str) -> None:
    with locked_state(profile.paths.state_file) as state:
        entry = state.setdefault("task_claims", {}).get(task.id) or {}
        claim_task_entry(
            entry,
            agent=child_agent,
            title=task.title,
            summary={
                "bucket": task.payload["bucket"],
                "paths": task.payload["paths"],
                "priority": task.payload["priority"],
                "risk": task.payload["risk"],
            },
        )
        state["task_claims"][task.id] = entry
    append_event(
        profile.paths,
        event_type="claim",
        actor=child_agent,
        task_id=task.id,
        payload={"via": "supervisor"},
    )


def _record_child_claim_process(profile: Profile, task_id: str, *, child_agent: str, pid: int) -> None:
    if pid < 1:
        return
    with locked_state(profile.paths.state_file) as state:
        entry = state.setdefault("task_claims", {}).get(task_id) or {}
        if entry.get("status") != "claimed" or entry.get("claimed_by") != child_agent:
            return
        entry["claimed_pid"] = pid
        entry["claimed_process_missing_scans"] = 0
        entry["claimed_process_last_seen_at"] = now_iso()
        entry.pop("claim_expires_at", None)
        state["task_claims"][task_id] = entry


def _scan_claim_process_liveness(
    profile: Profile,
    *,
    actor: str,
    skip_task_ids: set[str],
) -> list[str]:
    released: list[str] = []
    release_events: list[tuple[str, str]] = []
    with locked_state(profile.paths.state_file) as state:
        claims = state.setdefault("task_claims", {})
        for task_id, entry in claims.items():
            if task_id in skip_task_ids:
                continue
            if not isinstance(entry, dict) or entry.get("status") != "claimed":
                continue
            pid = entry.get("claimed_pid")
            if not isinstance(pid, int) or pid < 1:
                continue
            if _pid_alive(pid):
                entry["claimed_process_missing_scans"] = 0
                entry["claimed_process_last_seen_at"] = now_iso()
                claims[task_id] = entry
                continue
            missing_scans = int(entry.get("claimed_process_missing_scans") or 0) + 1
            entry["claimed_process_missing_scans"] = missing_scans
            entry["claimed_process_last_checked_at"] = now_iso()
            if missing_scans < CLAIM_LIVENESS_MISSING_SCAN_LIMIT:
                claims[task_id] = entry
                continue
            note = (
                f"Supervisor released orphaned claim after process {pid} was missing in "
                f"{CLAIM_LIVENESS_MISSING_SCAN_LIMIT} successive liveness scans."
            )
            entry["status"] = "released"
            entry["released_by"] = actor
            entry["released_at"] = now_iso()
            entry["release_note"] = note
            entry.pop("claim_expires_at", None)
            entry.pop("claimed_pid", None)
            entry.pop("claimed_process_missing_scans", None)
            entry.pop("claimed_process_last_seen_at", None)
            entry.pop("claimed_process_last_checked_at", None)
            claims[task_id] = entry
            released.append(task_id)
            release_events.append((task_id, note))
    for task_id, note in release_events:
        append_event(
            profile.paths,
            event_type="release",
            actor=actor,
            task_id=task_id,
            payload={"note": note},
        )
    if release_events:
        _emit_render(profile)
    return released


def _release_if_still_claimed(profile: Profile, task_id: str, *, child_agent: str, note: str) -> None:
    with locked_state(profile.paths.state_file) as state:
        entry = state.setdefault("task_claims", {}).get(task_id) or {}
        if entry.get("status") != "claimed" or entry.get("claimed_by") != child_agent:
            return
        entry["status"] = "released"
        entry["released_by"] = "supervisor"
        entry["released_at"] = now_iso()
        entry["release_note"] = note
        entry.pop("claim_expires_at", None)
        entry.pop("claimed_pid", None)
        entry.pop("claimed_process_missing_scans", None)
        entry.pop("claimed_process_last_seen_at", None)
        entry.pop("claimed_process_last_checked_at", None)
        state["task_claims"][task_id] = entry
    append_event(
        profile.paths,
        event_type="release",
        actor="supervisor",
        task_id=task_id,
        payload={"note": note},
    )


def _complete_if_still_claimed(profile: Profile, task_id: str, *, child_agent: str, note: str) -> None:
    with locked_state(profile.paths.state_file) as state:
        entry = state.setdefault("task_claims", {}).get(task_id) or {}
        if entry.get("status") == "done":
            return
        if entry.get("status") != "claimed" or entry.get("claimed_by") != child_agent:
            raise SupervisorError(f"Task {task_id} is not claimed by {child_agent}; cannot complete after land")
        entry["status"] = "done"
        entry["completed_by"] = child_agent
        entry["completed_at"] = now_iso()
        entry["completion_note"] = note
        entry.pop("claim_expires_at", None)
        entry.pop("claimed_pid", None)
        entry.pop("claimed_process_missing_scans", None)
        entry.pop("claimed_process_last_seen_at", None)
        entry.pop("claimed_process_last_checked_at", None)
        state["task_claims"][task_id] = entry
        approvals = state.setdefault("approval_tasks", {})
        if task_id in approvals and isinstance(approvals[task_id], dict):
            approvals[task_id]["status"] = "done"
    append_event(profile.paths, event_type="complete", actor=child_agent, task_id=task_id, payload={"note": note})


def _prepare_workspace(
    profile: Profile,
    task: TaskInfo,
    *,
    workspace_mode: str,
    run_id: str,
) -> PreparedWorkspace:
    if workspace_mode != "git-worktree":
        raise SupervisorError("Blackdog only supports git-worktree supervisor workspaces")
    profile.paths.worktrees_dir.mkdir(parents=True, exist_ok=True)
    branch = supervisor_task_branch(task, run_id)
    workspace = supervisor_task_worktree_path(profile, task, run_id).resolve()
    try:
        spec = start_task_worktree(
            profile,
            task_id=task.id,
            branch=branch,
            path=str(workspace),
        )
    except WorktreeError as exc:
        raise SupervisorError(f"Failed to create task worktree for {task.id}: {exc}") from exc
    return PreparedWorkspace(workspace=workspace, worktree_spec=spec)


def _land_child_branch(profile: Profile, child: ChildRun, *, actor: str) -> dict[str, Any]:
    spec = child.worktree_spec
    if spec is None:
        raise WorktreeError("missing worktree spec for branch-backed child run")

    errors: list[str] = []
    rebase_result: dict[str, Any] | None = None
    for _ in range(2):
        try:
            payload = land_branch(
                profile,
                branch=spec.branch,
                target_branch=spec.target_branch,
                cleanup=True,
            )
            if rebase_result is not None:
                payload["rebase"] = rebase_result
            if errors:
                payload["retry_errors"] = list(errors)
            return payload
        except DirtyPrimaryWorktreeError as exc:
            _notify_supervisor(
                profile,
                actor=actor,
                task_id=child.task.id,
                kind="warning",
                tags=["supervisor", "dirty-primary", "contract-violation", "land"],
                body=(
                    f"Landing {spec.branch} for {child.task.id} is blocked by dirty primary-worktree changes and "
                    f"violates the WTAM contract. Overlap with branch changes: "
                    f"{', '.join(exc.overlap_paths) or 'none detected'}. Dirty paths: "
                    f"{', '.join(exc.dirty_paths) or 'none detected'}. Clean up or land the primary worktree "
                    "changes, then rerun the task. Blackdog will not auto-stash the primary checkout."
                ),
            )
            raise
        except WorktreeError as exc:
            detail = str(exc)
            errors.append(detail)
            if "cannot land:" in detail and "not based on the current" in detail:
                try:
                    rebase_result = rebase_branch_onto_target(
                        profile,
                        branch=spec.branch,
                        target_branch=spec.target_branch,
                    )
                except WorktreeError as rebase_exc:
                    errors.append(str(rebase_exc))
                    break
                time.sleep(1)
                continue
            break

    raise WorktreeError("; ".join(errors[-4:]) if errors else "landing failed")


def _build_child_prompt(
    profile: Profile,
    task: TaskInfo,
    *,
    child_agent: str,
    workspace_mode: str,
    workspace: Path,
    worktree_spec: WorktreeSpec | None = None,
) -> str:
    if worktree_spec is None:
        raise SupervisorError("Blackdog only supports branch-backed task worktrees for child runs")
    blackdog_command = _preferred_blackdog_command(profile, workspace=workspace)
    contract = worktree_contract(profile, workspace=workspace, workspace_mode=workspace_mode)
    docs = "\n".join(f"- {item}" for item in task.payload.get("docs", [])) or "- No routed docs."
    checks = "\n".join(f"- {item}" for item in task.payload.get("checks", [])) or "- No validation commands."
    paths = "\n".join(f"- {item}" for item in task.payload.get("paths", [])) or "- No specific paths."
    domains = ", ".join(str(item) for item in task.payload.get("domains", [])) or "none"
    workspace_baseline_rule = "- This branch-backed worktree was created from the primary worktree branch. Treat committed repo state as the baseline for this task."
    preserve_rule = "- Keep your changes isolated to the task branch and target paths unless the task requires broader edits."
    primary_cleanliness_rule = (
        f"- Primary-worktree landing gate: currently dirty ({', '.join(contract['primary_dirty_paths'])}). "
        f"The supervisor cannot land `{worktree_spec.branch}` into `{contract['target_branch']}` until the primary checkout is clean."
        if contract["primary_dirty_paths"]
        else f"- Primary-worktree landing gate: `{contract['primary_worktree']}` must stay clean for the supervisor to land changes into `{contract['target_branch']}`."
    )
    venv_rule = (
        f"- `{contract['ve_expectation']}` Preferred CLI for this workspace: `{contract['workspace_blackdog_path']}`."
        if contract["workspace_has_local_blackdog"]
        else f"- `{contract['ve_expectation']}` This workspace does not currently have `{contract['workspace_blackdog_path']}`, so use `blackdog` from the active environment or bootstrap `./.VE` here."
    )
    branch_rules = textwrap.dedent(
        f"""
        - This is a branch-backed task worktree on branch `{worktree_spec.branch}` targeting `{worktree_spec.target_branch}`.
        - Commit your code changes on that task branch before you exit if you want the supervisor to land them.
        - Do not land, merge, or delete the branch yourself. The supervisor will land `{worktree_spec.branch}` through the primary worktree and then clean it up.
        - Do not run `{blackdog_command} complete` for this task from a branch-backed child run; the supervisor will complete it after a successful land.
        """
    ).strip()
    return textwrap.dedent(
        f"""
        You are Blackdog child agent `{child_agent}` working on one Blackdog backlog task.

        Current workspace for code changes: `{workspace}`
        Central Blackdog project root for backlog state: `{profile.paths.project_root}`
        Workspace mode: `{workspace_mode}`

        Task id: `{task.id}`
        Title: {task.title}
        Objective: {task.payload.get("objective") or "unassigned"}
        Epic: {task.epic_title or "Unplanned"}
        Lane: {task.lane_title or "Unplanned"}
        Wave: {task.wave if task.wave is not None else "unplanned"}
        Priority: {task.payload.get("priority")}
        Risk: {task.payload.get("risk")}
        Domains: {domains}

        Why it matters: {task.narrative.why or "See backlog task entry."}
        Evidence: {task.narrative.evidence or "See backlog task entry."}
        Safe first slice: {task.payload.get("safe_first_slice")}

        Target paths:
        {paths}

        Docs to review:
        {docs}

        Checks to run if you change behavior:
        {checks}

        Required operating rules:
        {workspace_baseline_rule}
        {preserve_rule}
        - Supervisor workspace mode for this run: `{contract['workspace_mode']}`.
        {primary_cleanliness_rule}
        {venv_rule}
        - The supervisor has already claimed `{task.id}` for you as `{child_agent}`. Do not run `blackdog claim` for this task again.
        - Prefer Blackdog CLI output over direct reads of raw state files when checking claims, inbox state, results, or task status.
        - Work only on `{task.id}`.
        - Use the current directory for code edits.
        - For Blackdog state commands, always target the central root with `--project-root {profile.paths.project_root}`.
        - Before starting, read your inbox with `{blackdog_command} inbox list --project-root {profile.paths.project_root} --recipient {child_agent}`.
        - When finished, record a structured result with `{blackdog_command} result record --project-root {profile.paths.project_root} --id {task.id} --actor {child_agent} ...`.
        {branch_rules}
        - If blocked, record a blocked or partial result and release the task with `{blackdog_command} release --project-root {profile.paths.project_root} --agent {child_agent} --id {task.id} --note "<reason>"`.
        - Do not start unrelated tasks.
        """
    ).strip()


def _resolved_launch_command(profile: Profile) -> list[str]:
    command = list(profile.supervisor_launch_command)
    if (
        tuple(profile.supervisor_launch_command) == DEFAULT_SUPERVISOR_COMMAND
        and DESKTOP_CODEX_BINARY.is_file()
        and os.access(DESKTOP_CODEX_BINARY, os.X_OK)
    ):
        command[0] = str(DESKTOP_CODEX_BINARY)
    return command


def _build_launch_command(launch_command: tuple[str, ...], prompt: str) -> list[str]:
    return [*launch_command, prompt]


def _preflight_launch_command(launch_command: tuple[str, ...]) -> None:
    binary = launch_command[0]
    if Path(binary).name != "codex":
        return
    if len(launch_command) < 2 or launch_command[1] != "exec":
        raise SupervisorError("Codex supervisor launches must use `codex exec`; prompt-launcher support has been removed")
    completed = subprocess.run(
        [binary, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    help_text = "\n".join([completed.stdout or "", completed.stderr or ""])
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise SupervisorError(f"Unable to inspect Codex launcher {binary}: {detail}")
    if "Commands:" not in help_text or "  exec" not in help_text:
        raise SupervisorError(f"Codex launcher {binary} does not support `exec`; prompt launcher support has been removed")


def _capture_child_diff_artifacts(child: ChildRun) -> None:
    spec = child.worktree_spec
    if spec is None:
        return
    primary_root = Path(spec.primary_worktree)
    commands = (
        (child.run_dir / "changes.diff", ["git", "-C", str(primary_root), "diff", "--binary", f"{spec.target_branch}..{spec.branch}"]),
        (child.run_dir / "changes.stat.txt", ["git", "-C", str(primary_root), "diff", "--stat", f"{spec.target_branch}..{spec.branch}"]),
    )
    for output_path, command in commands:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
        if completed.returncode == 0:
            output_path.write_text(completed.stdout, encoding="utf-8")


def _attempt_land_child_worktree(profile: Profile, child: ChildRun, *, actor: str, run_id: str) -> None:
    spec = child.worktree_spec
    if spec is None or child.launch_error or child.missing_process or child.exit_code not in {0, None}:
        return
    try:
        branch_ready = branch_ahead_of_target(profile, branch=spec.branch, target_branch=spec.target_branch)
        child.branch_ahead = branch_ready
    except WorktreeError as exc:
        child.land_error = str(exc)
        return
    state = load_state(profile.paths.state_file)
    current_status = str((state.get("task_claims", {}).get(child.task.id) or {}).get("status") or "open")
    if not branch_ready:
        if current_status == "claimed":
            child.landed = False
            _release_if_still_claimed(
                profile,
                child.task.id,
                child_agent=child.child_agent,
                note="Supervisor released task after child run ended without a committable branch change.",
            )
        return
    _capture_child_diff_artifacts(child)
    try:
        payload = _land_child_branch(profile, child, actor=actor)
    except DirtyPrimaryWorktreeError as exc:
        child.land_error = str(exc)
        child.landed = False
        child.land_needs_user_input = True
        child.land_followup_candidates = [
            "Clean up or land the primary worktree changes in the primary checkout.",
            f"Rerun {child.task.id} after the primary worktree is clean.",
        ]
        if current_status == "claimed":
            _release_if_still_claimed(
                profile,
                child.task.id,
                child_agent=child.child_agent,
                note=f"Supervisor released task after dirty-primary land block: {exc}",
            )
        return
    except WorktreeError as exc:
        child.land_error = str(exc)
        child.landed = False
        if current_status == "claimed":
            _release_if_still_claimed(
                profile,
                child.task.id,
                child_agent=child.child_agent,
                note=f"Supervisor released task after land failed: {exc}",
            )
        return
    child.land_result = payload
    child.landed = True
    append_event(
        profile.paths,
        event_type="worktree_land",
        actor=actor,
        task_id=child.task.id,
        payload={"run_id": run_id, "child_agent": child.child_agent, **payload},
    )
    if current_status != "done":
        _complete_if_still_claimed(
            profile,
            child.task.id,
            child_agent=child.child_agent,
            note=f"Supervisor landed {spec.branch} into {spec.target_branch} and completed the task.",
        )


def _finalize_child_run(profile: Profile, child: ChildRun, *, actor: str) -> None:
    if child.stdout_handle is not None:
        child.stdout_handle.close()
    if child.stderr_handle is not None:
        child.stderr_handle.close()

    after_results = {
        str(row["result_file"]) for row in load_task_results(profile.paths, task_id=child.task.id) if row.get("result_file")
    }
    child.result_recorded = bool(after_results - child.result_files_before)
    state = load_state(profile.paths.state_file)
    child.final_task_status = "done" if task_done(child.task.id, state) else str(
        (state.get("task_claims", {}).get(child.task.id) or {}).get("status") or "open"
    )
    if child.result_recorded and child.final_task_status == "done" and not child.land_error:
        if child.message_id:
            resolve_message(
                profile.paths,
                message_id=child.message_id,
                actor=actor,
                note=f"Child run finished with final task status: {child.final_task_status}",
            )
        return

    validation = [f"Child launch command: {' '.join(child.launch_command)}"]
    if child.launch_error:
        validation.append(f"Launch error: {child.launch_error}")
    elif child.missing_process:
        validation.append(
            f"Claiming process disappeared before task completion after {CLAIM_LIVENESS_MISSING_SCAN_LIMIT} missed liveness scans"
        )
    else:
        validation.append(f"Exit code: {child.exit_code}")
    if child.land_result is not None:
        validation.append(f"Landed branch {child.land_result['branch']} into {child.land_result['target_branch']}")
        rebase = child.land_result.get("rebase")
        if isinstance(rebase, dict):
            validation.append(
                f"Rebased {rebase['branch']} onto {rebase['target_branch']} before landing"
            )
    if child.land_error:
        validation.append(f"Land error: {child.land_error}")
    if child.final_task_status == "done":
        status = "success"
        residual = ["Supervisor had to backfill the task result because the child run completed without writing one."]
        if child.land_error:
            status = "blocked"
            residual = ["Task state is done, but the supervisor could not land the child branch through the primary worktree."]
    elif child.missing_process or child.launch_error or child.exit_code not in {0, None}:
        status = "blocked"
        residual = ["Child run failed or disappeared before completing the task protocol."]
        if child.land_error:
            residual.append("The supervisor also failed to land the branch-backed child worktree.")
    else:
        status = "partial"
        residual = ["Child run exited cleanly but did not complete the task."]
        if child.land_error:
            status = "blocked"
            residual.append("The supervisor failed to land the branch-backed child worktree.")
    if child.land_needs_user_input:
        residual.append("Primary worktree cleanup is required before the supervisor can land the child branch.")
    record_task_result(
        profile.paths,
        task_id=child.task.id,
        actor=actor,
        status=status,
        what_changed=[
            f"Supervisor captured the child run outcome for {child.child_agent}.",
            f"Workspace: {child.workspace}",
            f"Stdout: {child.stdout_file}",
            f"Stderr: {child.stderr_file}",
        ]
        + (
            [f"Landed commit: {child.land_result['landed_commit']}"]
            if child.land_result is not None and child.land_result.get("landed_commit")
            else []
        ),
        validation=validation,
        residual=residual,
        needs_user_input=child.land_needs_user_input,
        followup_candidates=list(child.land_followup_candidates),
        run_id=child.run_dir.name,
    )
    if child.final_task_status != "done":
        _release_if_still_claimed(
            profile,
            child.task.id,
            child_agent=child.child_agent,
            note="Supervisor released task after child run ended without completion.",
        )
        state = load_state(profile.paths.state_file)
        child.final_task_status = str((state.get("task_claims", {}).get(child.task.id) or {}).get("status") or "open")
    if child.message_id:
        resolve_message(
            profile.paths,
            message_id=child.message_id,
            actor=actor,
            note=f"Child run finished with final task status: {child.final_task_status}",
        )


def _wait_for_child_process(child: ChildRun, completion_queue: queue.Queue[tuple[str, int | None]]) -> None:
    process = child.process
    if process is None:
        return
    try:
        completion_queue.put((child.child_agent, process.wait()))
    except Exception:
        completion_queue.put((child.child_agent, process.poll()))


def _finish_child(
    profile: Profile,
    child: ChildRun,
    *,
    actor: str,
    run_id: str,
) -> None:
    _attempt_land_child_worktree(profile, child, actor=actor, run_id=run_id)
    _finalize_child_run(profile, child, actor=actor)
    append_event(
        profile.paths,
        event_type="child_finish",
        actor=actor,
        task_id=child.task.id,
        payload={
            "run_id": run_id,
            "child_agent": child.child_agent,
            "exit_code": child.exit_code,
            "missing_process": child.missing_process,
            "result_recorded": child.result_recorded,
            "final_task_status": child.final_task_status,
            "land_error": child.land_error,
            "branch_ahead": child.branch_ahead,
            "landed": child.landed,
            "landed_commit": (child.land_result or {}).get("landed_commit"),
        },
    )
    _emit_render(profile)


def _next_run_tasks(
    snapshot: BacklogSnapshot,
    state: dict[str, Any],
    *,
    task_ids: list[str],
    allow_high_risk: bool,
    limit: int,
    force: bool,
    attempted_task_ids: set[str],
    active_task_ids: set[str],
) -> list[TaskInfo]:
    excluded_ids = attempted_task_ids | active_task_ids
    if task_ids:
        remaining_ids = [task_id for task_id in task_ids if task_id not in excluded_ids]
        if not remaining_ids:
            return []
        return _select_tasks(
            snapshot,
            state,
            task_ids=remaining_ids,
            allow_high_risk=allow_high_risk,
            limit=limit,
            force=force,
        )
    window = max(limit + len(excluded_ids), len(snapshot.tasks))
    ready = next_runnable_tasks(snapshot, state, allow_high_risk=allow_high_risk, limit=max(window, limit))
    return [task for task in ready if task.id not in excluded_ids][:limit]


def _launch_child_run(
    profile: Profile,
    task: TaskInfo,
    *,
    actor: str,
    child_agent: str,
    run_id: str,
    run_dir: Path,
    launch_command: tuple[str, ...],
    workspace_mode: str,
) -> ChildRun:
    _claim_for_child(profile, load_backlog(profile.paths, profile), task, child_agent=child_agent)
    child_run_dir = run_dir / task.id
    child_run_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = child_run_dir / "prompt.txt"
    stdout_file = child_run_dir / "stdout.log"
    stderr_file = child_run_dir / "stderr.log"
    workspace_path = supervisor_task_worktree_path(profile, task, run_id)
    result_files_before = {
        str(row["result_file"])
        for row in load_task_results(profile.paths, task_id=task.id)
        if row.get("result_file")
    }
    started_at = time.monotonic()
    try:
        prepared = _prepare_workspace(profile, task, workspace_mode=workspace_mode, run_id=run_id)
    except SupervisorError as exc:
        child = ChildRun(
            task=task,
            child_agent=child_agent,
            launch_command=launch_command,
            workspace=workspace_path,
            workspace_mode=workspace_mode,
            run_dir=child_run_dir,
            prompt_file=prompt_file,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            message_id=None,
            result_files_before=result_files_before,
            process=None,
            stdout_handle=None,
            stderr_handle=None,
            started_at=started_at,
            launch_error=str(exc),
            exit_code=None,
        )
        _finalize_child_run(profile, child, actor=actor)
        append_event(
            profile.paths,
            event_type="child_launch_failed",
            actor=actor,
            task_id=task.id,
            payload={"run_id": run_id, "child_agent": child_agent, "error": str(exc)},
        )
        _emit_render(profile)
        return child
    workspace = prepared.workspace
    if prepared.worktree_spec is not None:
        append_event(
            profile.paths,
            event_type="worktree_start",
            actor=actor,
            task_id=task.id,
            payload={"run_id": run_id, "child_agent": child_agent, **prepared.worktree_spec.to_dict()},
        )
    prompt = _build_child_prompt(
        profile,
        task,
        child_agent=child_agent,
        workspace_mode=workspace_mode,
        workspace=workspace,
        worktree_spec=prepared.worktree_spec,
    )
    metadata_file = child_run_dir / "metadata.json"
    prompt_file.write_text(prompt + "\n", encoding="utf-8")
    message = send_message(
        profile.paths,
        sender=actor,
        recipient=child_agent,
        body=f"Execute {task.id} from {workspace}. The launch prompt is saved at {prompt_file}.",
        kind="instruction",
        task_id=task.id,
        tags=["supervisor-run", workspace_mode],
    )
    metadata = {
        "task_id": task.id,
        "child_agent": child_agent,
        "workspace": str(workspace),
        "workspace_mode": workspace_mode,
        "prompt_file": str(prompt_file),
        "stdout_file": str(stdout_file),
        "stderr_file": str(stderr_file),
        "launched_at": now_iso(),
    }
    if prepared.worktree_spec is not None:
        metadata["worktree_spec"] = prepared.worktree_spec.to_dict()
    metadata_file.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    stdout_handle = stdout_file.open("w", encoding="utf-8")
    stderr_handle = stderr_file.open("w", encoding="utf-8")
    child = ChildRun(
        task=task,
        child_agent=child_agent,
        launch_command=launch_command,
        workspace=workspace,
        workspace_mode=workspace_mode,
        run_dir=child_run_dir,
        prompt_file=prompt_file,
        stdout_file=stdout_file,
        stderr_file=stderr_file,
        message_id=str(message["message_id"]),
        result_files_before=result_files_before,
        process=None,
        stdout_handle=stdout_handle,
        stderr_handle=stderr_handle,
        started_at=started_at,
        worktree_spec=prepared.worktree_spec,
    )
    command = _build_launch_command(launch_command, prompt)
    env = os.environ.copy()
    env.update(
        {
            "BLACKDOG_PROJECT_ROOT": str(profile.paths.project_root),
            "BLACKDOG_TASK_ID": task.id,
            "BLACKDOG_AGENT_NAME": child_agent,
            "BLACKDOG_WORKSPACE": str(workspace),
            "BLACKDOG_WORKSPACE_MODE": workspace_mode,
            "BLACKDOG_RUN_DIR": str(child_run_dir),
            "BLACKDOG_PROMPT_FILE": str(prompt_file),
        }
    )
    if prepared.worktree_spec is not None:
        env.update(
            {
                "BLACKDOG_TASK_BRANCH": prepared.worktree_spec.branch,
                "BLACKDOG_TARGET_BRANCH": prepared.worktree_spec.target_branch,
                "BLACKDOG_PRIMARY_WORKTREE": prepared.worktree_spec.primary_worktree,
            }
        )
    try:
        child.process = subprocess.Popen(
            command,
            cwd=workspace,
            stdout=stdout_handle,
            stderr=stderr_handle,
            stdin=subprocess.DEVNULL,
            text=True,
            env=env,
        )
    except OSError as exc:
        child.launch_error = str(exc)
        child.exit_code = None
        _finalize_child_run(profile, child, actor=actor)
        append_event(
            profile.paths,
            event_type="child_launch_failed",
            actor=actor,
            task_id=task.id,
            payload={"run_id": run_id, "child_agent": child_agent, "error": str(exc)},
        )
        _emit_render(profile)
        return child
    append_event(
        profile.paths,
        event_type="child_launch",
        actor=actor,
        task_id=task.id,
        payload={
            "run_id": run_id,
            "child_agent": child_agent,
            "workspace": str(workspace),
            "workspace_mode": workspace_mode,
            "pid": child.process.pid,
        },
    )
    _record_child_claim_process(profile, task.id, child_agent=child_agent, pid=child.process.pid)
    _emit_render(profile)
    return child


def _append_run_step(
    status_payload: dict[str, Any],
    status_file: Path,
    *,
    status: str,
    ready_task_ids: list[str],
    running_task_ids: list[str],
    open_message_ids: list[str],
    launched_task_ids: list[str] | None = None,
    finished_task_ids: list[str] | None = None,
    control_message_id: str | None = None,
    removed_task_ids: list[str] | None = None,
    released_task_ids: list[str] | None = None,
) -> None:
    step = {
        "index": len(status_payload["steps"]) + 1,
        "at": now_iso(),
        "status": status,
        "ready_task_ids": list(ready_task_ids),
        "running_task_ids": list(running_task_ids),
        "open_message_ids": list(open_message_ids),
        "draining": bool(status_payload.get("draining")),
    }
    if launched_task_ids:
        step["launched_task_ids"] = list(launched_task_ids)
    if finished_task_ids:
        step["finished_task_ids"] = list(finished_task_ids)
    if control_message_id:
        step["control_message_id"] = control_message_id
    if removed_task_ids:
        step["removed_task_ids"] = list(removed_task_ids)
    if released_task_ids:
        step["released_task_ids"] = list(released_task_ids)
    status_payload["steps"].append(step)
    _write_run_status(status_file, status_payload)


def run_supervisor(
    profile: Profile,
    *,
    actor: str,
    task_ids: list[str],
    count: int,
    allow_high_risk: bool,
    force: bool,
    workspace_mode: str | None,
    poll_interval_seconds: float | None = None,
) -> dict[str, Any]:
    selected_count = count or profile.supervisor_max_parallel
    resolved_workspace_mode = workspace_mode or profile.supervisor_workspace_mode
    resolved_poll_interval_seconds = (
        DEFAULT_SUPERVISOR_POLL_INTERVAL_SECONDS if poll_interval_seconds is None else poll_interval_seconds
    )
    if resolved_workspace_mode != "git-worktree":
        raise BacklogError("workspace mode must be 'git-worktree'")
    if resolved_poll_interval_seconds < 0:
        raise BacklogError("poll interval must be at least 0 seconds")

    sweep = sweep_completed_tasks(profile)
    run_id = uuid.uuid4().hex[:8]
    run_dir = profile.paths.supervisor_runs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    status_file = run_dir / "status.json"
    status_payload: dict[str, Any] = {
        "run_id": run_id,
        "actor": actor,
        "workspace_mode": resolved_workspace_mode,
        "poll_interval_seconds": resolved_poll_interval_seconds,
        "draining": False,
        "run_dir": str(run_dir),
        "status_file": str(status_file),
        "supervisor_pid": os.getpid(),
        "steps": [],
    }
    append_event(
        profile.paths,
        event_type="supervisor_run_started",
        actor=actor,
        payload={
            "run_id": run_id,
            "workspace_mode": resolved_workspace_mode,
            "task_ids": list(task_ids),
        },
    )
    if sweep["changed"]:
        append_event(
            profile.paths,
            event_type="supervisor_run_sweep",
            actor=actor,
            payload={
                "run_id": run_id,
                "removed_task_ids": list(sweep["removed_task_ids"]),
                "removed_lane_ids": list(sweep["removed_lane_ids"]),
                "removed_epic_ids": list(sweep["removed_epic_ids"]),
                "wave_map": dict(sweep["wave_map"]),
            },
        )
        _emit_render(profile)
    _append_run_step(
        status_payload,
        status_file,
        status="swept",
        ready_task_ids=[],
        running_task_ids=[],
        open_message_ids=[],
        removed_task_ids=list(sweep["removed_task_ids"]),
    )

    resolved_launch_command = tuple(_resolved_launch_command(profile))
    children: list[ChildRun] = []
    active: dict[str, ChildRun] = {}
    completion_queue: queue.Queue[tuple[str, int | None]] = queue.Queue()
    attempted_task_ids: set[str] = set()
    launched_count = 0
    launch_command_checked = False
    next_claim_liveness_scan_at = 0.0

    def start_child(task: TaskInfo) -> None:
        nonlocal launched_count, launch_command_checked
        if not launch_command_checked:
            _preflight_launch_command(resolved_launch_command)
            launch_command_checked = True
        launched_count += 1
        child_agent = f"{actor}/child-{launched_count:02d}"
        child = _launch_child_run(
            profile,
            task,
            actor=actor,
            child_agent=child_agent,
            run_id=run_id,
            run_dir=run_dir,
            launch_command=resolved_launch_command,
            workspace_mode=resolved_workspace_mode,
        )
        children.append(child)
        attempted_task_ids.add(task.id)
        if child.process is not None:
            active[child.child_agent] = child
            threading.Thread(target=_wait_for_child_process, args=(child, completion_queue), daemon=True).start()

    while True:
        snapshot, state = _load_synced_runtime(profile)
        open_messages = load_inbox(profile.paths, recipient=actor, status="open")
        control_action, control_message = _run_control_action(open_messages)
        control_message_id: str | None = None
        if control_action == "stop" and control_message is not None:
            status_payload["draining"] = True
            control_message_id = str(control_message["message_id"])
            status_payload["stopped_by_message_id"] = control_message_id

        finished_task_ids: list[str] = []
        while True:
            try:
                child_agent, exit_code = completion_queue.get_nowait()
            except queue.Empty:
                break
            child = active.pop(child_agent, None)
            if child is None:
                continue
            child.exit_code = exit_code if exit_code is not None else child.process.poll() if child.process is not None else None
            _finish_child(profile, child, actor=actor, run_id=run_id)
            finished_task_ids.append(child.task.id)

        snapshot, state = _load_synced_runtime(profile)
        active_task_ids = {child.task.id for child in active.values()}
        current = time.monotonic()
        released_task_ids: list[str] = []
        if current >= next_claim_liveness_scan_at:
            released_task_ids = _scan_claim_process_liveness(
                profile,
                actor=actor,
                skip_task_ids=active_task_ids,
            )
            next_claim_liveness_scan_at = current + CLAIM_LIVENESS_SCAN_INTERVAL_SECONDS
            if released_task_ids:
                snapshot, state = _load_synced_runtime(profile)
        ready_tasks = _next_run_tasks(
            snapshot,
            state,
            task_ids=task_ids,
            allow_high_risk=allow_high_risk,
            limit=max(0, selected_count - len(active)),
            force=force,
            attempted_task_ids=attempted_task_ids,
            active_task_ids=active_task_ids,
        )

        launched_task_ids: list[str] = []
        if not status_payload["draining"]:
            for task in ready_tasks:
                if len(active) >= selected_count:
                    break
                start_child(task)
                launched_task_ids.append(task.id)
            if launched_task_ids:
                snapshot, state = _load_synced_runtime(profile)
                active_task_ids = {child.task.id for child in active.values()}
                ready_tasks = _next_run_tasks(
                    snapshot,
                    state,
                    task_ids=task_ids,
                    allow_high_risk=allow_high_risk,
                    limit=max(0, selected_count - len(active)),
                    force=force,
                    attempted_task_ids=attempted_task_ids,
                    active_task_ids=active_task_ids,
                )

        if launched_task_ids or finished_task_ids or released_task_ids or control_message_id:
            _append_run_step(
                status_payload,
                status_file,
                status="draining" if status_payload["draining"] else "running",
                ready_task_ids=[task.id for task in ready_tasks],
                running_task_ids=[child.task.id for child in active.values()],
                open_message_ids=[str(message.get("message_id") or "") for message in open_messages],
                launched_task_ids=launched_task_ids,
                finished_task_ids=finished_task_ids,
                control_message_id=control_message_id,
                released_task_ids=released_task_ids,
            )

        if not active:
            if status_payload["draining"]:
                status_payload["completed_at"] = now_iso()
                status_payload["final_status"] = "stopped"
                _append_run_step(
                    status_payload,
                    status_file,
                    status="stopped",
                    ready_task_ids=[],
                    running_task_ids=[],
                    open_message_ids=[str(message.get("message_id") or "") for message in open_messages],
                    control_message_id=str(status_payload.get("stopped_by_message_id") or "") or None,
                )
                if status_payload.get("stopped_by_message_id"):
                    resolve_message(
                        profile.paths,
                        message_id=str(status_payload["stopped_by_message_id"]),
                        actor=actor,
                        note="Supervisor run stopped after draining active child work.",
                    )
                _emit_render(profile)
                break
            if not ready_tasks:
                status_payload["completed_at"] = now_iso()
                status_payload["final_status"] = "idle"
                _append_run_step(
                    status_payload,
                    status_file,
                    status="idle",
                    ready_task_ids=[],
                    running_task_ids=[],
                    open_message_ids=[str(message.get("message_id") or "") for message in open_messages],
                )
                _emit_render(profile)
                break
            continue

        try:
            child_agent, exit_code = completion_queue.get(timeout=resolved_poll_interval_seconds)
        except queue.Empty:
            continue
        child = active.pop(child_agent, None)
        if child is None:
            continue
        child.exit_code = exit_code if exit_code is not None else child.process.poll() if child.process is not None else None
        _finish_child(profile, child, actor=actor, run_id=run_id)
        _append_run_step(
            status_payload,
            status_file,
            status="draining" if status_payload["draining"] else "running",
            ready_task_ids=[],
            running_task_ids=[row.task.id for row in active.values()],
            open_message_ids=[str(message.get("message_id") or "") for message in open_messages],
            finished_task_ids=[child.task.id],
        )

    append_event(
        profile.paths,
        event_type="supervisor_run_finished",
        actor=actor,
        payload={
            "run_id": run_id,
            "workspace_mode": resolved_workspace_mode,
            "task_ids": [child.task.id for child in children],
            "final_status": status_payload.get("final_status") or "idle",
            "stopped_by_message_id": status_payload.get("stopped_by_message_id"),
        },
    )
    _write_run_status(status_file, status_payload)
    return {
        "run_id": run_id,
        "actor": actor,
        "launch_command": list(resolved_launch_command),
        "workspace_mode": resolved_workspace_mode,
        "poll_interval_seconds": resolved_poll_interval_seconds,
        "draining": bool(status_payload.get("draining")),
        "final_status": status_payload.get("final_status") or "idle",
        "run_dir": str(run_dir),
        "status_file": str(status_file),
        "steps": list(status_payload["steps"]),
        "stopped_by_message_id": status_payload.get("stopped_by_message_id"),
        "children": [
            {
                "task_id": child.task.id,
                "title": child.task.title,
                "child_agent": child.child_agent,
                "launch_command": list(child.launch_command),
                "workspace": str(child.workspace),
                "workspace_mode": child.workspace_mode,
                "prompt_file": str(child.prompt_file),
                "stdout_file": str(child.stdout_file),
                "stderr_file": str(child.stderr_file),
                "launch_error": child.launch_error,
                "exit_code": child.exit_code,
                "missing_process": child.missing_process,
                "result_recorded": child.result_recorded,
                "final_task_status": child.final_task_status,
                "task_branch": child.worktree_spec.branch if child.worktree_spec is not None else None,
                "target_branch": child.worktree_spec.target_branch if child.worktree_spec is not None else None,
                "land_result": child.land_result,
                "branch_ahead": child.branch_ahead,
                "landed": child.landed,
                "land_error": child.land_error,
            }
            for child in children
        ],
    }
