from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO
import json
import os
import queue
import shlex
import subprocess
import threading
import time
import uuid

from blackdog_core.backlog import (
    BacklogError,
    BacklogSnapshot,
    BacklogTask,
    add_task,
    classify_task_status,
    load_backlog,
    next_runnable_tasks,
    sweep_completed_tasks,
    sync_state_for_backlog,
    task_done,
)
from blackdog_core.profile import DEFAULT_SUPERVISOR_COMMAND, RepoProfile
from .scaffold import render_project_html
from .supervisor_policy import (
    CHILD_PROMPT_TEMPLATE_HASH,
    CHILD_PROMPT_TEMPLATE_VERSION,
    DYNAMIC_REASONING_BASE_EFFORT,
    DYNAMIC_REASONING_COMPLEX_EFFORT,
    apply_launch_overrides as _apply_launch_overrides,
    build_child_launch_telemetry as _build_child_launch_telemetry,
    build_child_prompt as _build_child_prompt,
    launch_defaults_view as _launch_defaults_view,
    launch_settings_reasoning_label as _launch_settings_reasoning_label,
    launch_settings_view as _launch_settings_view,
    resolved_task_launch_overrides as _resolved_task_launch_overrides,
)
from blackdog_core.state import (
    APPROVAL_STATUS_DONE,
    CLAIM_STATUS_CLAIMED,
    CLAIM_STATUS_DONE,
    CLAIM_STATUS_RELEASED,
    atomic_write_text,
    append_event,
    claim_task_entry,
    load_inbox,
    load_events,
    load_state,
    load_task_results,
    locked_state,
    now_iso,
    record_task_result,
    resolve_message,
    save_state,
    send_message,
)
from .conversations import mirror_task_result_to_threads
from .worktree import (
    DirtyPrimaryWorktreeError,
    WorktreeError,
    WorktreeSpec,
    WORKSPACE_MODE_GIT_WORKTREE,
    branch_ahead_of_target,
    branch_changed_paths,
    commit_working_tree_paths,
    land_branch,
    find_worktree_for_branch,
    rebase_branch_onto_target,
    stash_working_tree,
    supervisor_task_branch,
    supervisor_task_worktree_path,
    start_task_worktree,
    normalize_workspace_mode,
    working_tree_matches_ref,
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
CHILD_PROTOCOL_HELPER = "blackdog-child"
SUPERVISOR_RUN_STATUS_RUNNING = "running"
SUPERVISOR_RUN_STATUS_DRAINING = "draining"
SUPERVISOR_RUN_STATUS_IDLE = "idle"
SUPERVISOR_RUN_STATUS_STOPPED = "stopped"
SUPERVISOR_RUN_STATUS_INTERRUPTED = "interrupted"
SUPERVISOR_RUN_STATUS_HISTORICAL = "historical"
SUPERVISOR_RUN_STEP_STATUS_SWEPT = "swept"
SUPERVISOR_RUN_STATUS_ALIASES = {
    SUPERVISOR_RUN_STEP_STATUS_SWEPT: SUPERVISOR_RUN_STATUS_RUNNING,
    "complete": SUPERVISOR_RUN_STATUS_IDLE,
    "finished": SUPERVISOR_RUN_STATUS_IDLE,
}
SUPERVISOR_RUN_STEP_STATUSES = frozenset(
    {
        SUPERVISOR_RUN_STEP_STATUS_SWEPT,
        SUPERVISOR_RUN_STATUS_RUNNING,
        SUPERVISOR_RUN_STATUS_DRAINING,
        SUPERVISOR_RUN_STATUS_STOPPED,
        SUPERVISOR_RUN_STATUS_IDLE,
    }
)
SUPERVISOR_RUN_FINAL_STATUSES = frozenset(
    {
        SUPERVISOR_RUN_STATUS_IDLE,
        SUPERVISOR_RUN_STATUS_STOPPED,
        SUPERVISOR_RUN_STATUS_INTERRUPTED,
    }
)
SUPERVISOR_RUN_RUNTIME_STATUSES = frozenset(
    {
        SUPERVISOR_RUN_STATUS_RUNNING,
        SUPERVISOR_RUN_STATUS_DRAINING,
        SUPERVISOR_RUN_STATUS_IDLE,
        SUPERVISOR_RUN_STATUS_STOPPED,
        SUPERVISOR_RUN_STATUS_INTERRUPTED,
        SUPERVISOR_RUN_STATUS_HISTORICAL,
    }
)
SUPERVISOR_ATTEMPT_STATUS_PREPARED = "prepared"
SUPERVISOR_ATTEMPT_STATUS_RUNNING = "running"
SUPERVISOR_ATTEMPT_STATUS_LAUNCH_FAILED = "launch-failed"
SUPERVISOR_ATTEMPT_STATUS_INTERRUPTED = "interrupted"
SUPERVISOR_ATTEMPT_STATUS_BLOCKED = "blocked"
SUPERVISOR_ATTEMPT_STATUS_FAILED = "failed"
SUPERVISOR_ATTEMPT_STATUS_DONE = "done"
SUPERVISOR_ATTEMPT_STATUS_UNKNOWN = "unknown"
SUPERVISOR_FINAL_TASK_STATUS_OPEN = "open"
SUPERVISOR_FINAL_TASK_STATUS_PARTIAL = "partial"
SUPERVISOR_FINAL_TASK_STATUS_FINISHED = "finished"
SUPERVISOR_ATTEMPT_STATUS_ALIASES = {"finished": SUPERVISOR_ATTEMPT_STATUS_DONE}
SUPERVISOR_ATTEMPT_FINAL_TASK_STATUSES = frozenset(
    {
        CLAIM_STATUS_CLAIMED,
        CLAIM_STATUS_RELEASED,
        CLAIM_STATUS_DONE,
        SUPERVISOR_ATTEMPT_STATUS_FAILED,
        SUPERVISOR_FINAL_TASK_STATUS_PARTIAL,
        SUPERVISOR_FINAL_TASK_STATUS_OPEN,
        SUPERVISOR_FINAL_TASK_STATUS_FINISHED,
    }
)
SUPERVISOR_ATTEMPT_STATUSES = frozenset(
    {
        SUPERVISOR_ATTEMPT_STATUS_PREPARED,
        SUPERVISOR_ATTEMPT_STATUS_RUNNING,
        SUPERVISOR_ATTEMPT_STATUS_LAUNCH_FAILED,
        SUPERVISOR_ATTEMPT_STATUS_INTERRUPTED,
        SUPERVISOR_ATTEMPT_STATUS_BLOCKED,
        SUPERVISOR_ATTEMPT_STATUS_FAILED,
        CLAIM_STATUS_RELEASED,
        SUPERVISOR_ATTEMPT_STATUS_DONE,
        SUPERVISOR_FINAL_TASK_STATUS_PARTIAL,
        SUPERVISOR_ATTEMPT_STATUS_UNKNOWN,
    }
)
SUPERVISOR_RECOVERY_CASE_BLOCKED_BY_DIRTY_PRIMARY = "blocked_by_dirty_primary"
SUPERVISOR_RECOVERY_CASE_BLOCKED_LAND = "blocked_land"
SUPERVISOR_RECOVERY_CASE_PARTIAL_RUN = "partial_run"
SUPERVISOR_RECOVERY_CASE_LANDED_BUT_UNFINISHED = "landed_but_unfinished"
SUPERVISOR_RECOVERY_CASES = frozenset(
    {
        SUPERVISOR_RECOVERY_CASE_BLOCKED_BY_DIRTY_PRIMARY,
        SUPERVISOR_RECOVERY_CASE_BLOCKED_LAND,
        SUPERVISOR_RECOVERY_CASE_PARTIAL_RUN,
        SUPERVISOR_RECOVERY_CASE_LANDED_BUT_UNFINISHED,
    }
)

SUPERVISOR_REPORT_REQUIRED_ARTIFACTS = ("prompt", "stdout", "stderr", "metadata")
SUPERVISOR_REPORT_ARTIFACT_FILES = {
    "prompt": "prompt.txt",
    "stdout": "stdout.log",
    "stderr": "stderr.log",
    "metadata": "metadata.json",
}

def _normalize_supervisor_attempt_status(value: Any, *, default: str = SUPERVISOR_ATTEMPT_STATUS_UNKNOWN) -> str:
    status = str(value or "").strip() or default
    status = SUPERVISOR_ATTEMPT_STATUS_ALIASES.get(status, status)
    return status if status in SUPERVISOR_ATTEMPT_STATUSES else default


def _normalize_supervisor_attempt_final_task_status(value: Any) -> str | None:
    status = str(value or "").strip()
    if not status:
        return None
    return status if status in SUPERVISOR_ATTEMPT_FINAL_TASK_STATUSES else None


def _attempt_status_from_final_task_status(value: Any, *, default: str = SUPERVISOR_ATTEMPT_STATUS_DONE) -> str:
    status = _normalize_supervisor_attempt_final_task_status(value)
    if status is None:
        return default
    if status == SUPERVISOR_FINAL_TASK_STATUS_FINISHED:
        return SUPERVISOR_ATTEMPT_STATUS_DONE
    if status in {SUPERVISOR_FINAL_TASK_STATUS_PARTIAL, SUPERVISOR_FINAL_TASK_STATUS_OPEN, CLAIM_STATUS_CLAIMED}:
        return SUPERVISOR_FINAL_TASK_STATUS_PARTIAL
    return _normalize_supervisor_attempt_status(status, default=default)


def _normalize_supervisor_runtime_status(value: Any, *, default: str = SUPERVISOR_RUN_STATUS_RUNNING) -> str:
    status = str(value or "").strip() or default
    status = SUPERVISOR_RUN_STATUS_ALIASES.get(status, status)
    return status if status in SUPERVISOR_RUN_RUNTIME_STATUSES else default

_CHILD_PROTOCOL_HELPER_TEMPLATE = """#!/usr/bin/env python3
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__PROJECT_ROOT__)
TASK_ID = __TASK_ID__
CHILD_AGENT = __CHILD_AGENT__
WORKSPACE = Path(__WORKSPACE__)
CLI_CANDIDATES = (
    str(WORKSPACE / ".VE" / "bin" / "blackdog"),
    str(PROJECT_ROOT / ".VE" / "bin" / "blackdog"),
    "blackdog",
    "python3 -m blackdog_cli",
)


def _is_path_command(command: str) -> bool:
    return "/" in command or command.startswith(".")


def _resolve_cli() -> list[str]:
    for raw in CLI_CANDIDATES:
        command = shlex.split(raw)
        if not command:
            continue
        executable = command[0]
        if _is_path_command(executable):
            if os.access(executable, os.X_OK):
                return command
            continue
        if shutil.which(executable) is None:
            continue
        return command
    raise SystemExit(
        "No usable Blackdog CLI found. Bootstrap ./.VE/bin/blackdog in the child worktree, "
        "or ensure a `blackdog` or `python3` executable is available in PATH."
    )


def _main() -> int:
    if len(sys.argv) < 2:
        print("Usage: blackdog-child <blackdog command...>", file=sys.stderr)
        return 2
    os.environ.setdefault("BLACKDOG_PROJECT_ROOT", str(PROJECT_ROOT))
    os.environ.setdefault("BLACKDOG_TASK_ID", TASK_ID)
    os.environ.setdefault("BLACKDOG_AGENT_NAME", CHILD_AGENT)
    cli = _resolve_cli()
    return subprocess.run(cli + sys.argv[1:], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(_main())
"""


@dataclass
class ChildRun:
    task: BacklogTask
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
    telemetry: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedWorkspace:
    workspace: Path
    worktree_spec: WorktreeSpec | None = None


def _preferred_blackdog_command(profile: RepoProfile, *, workspace: Path | None = None) -> str:
    candidate = ((workspace or profile.paths.project_root) / ".VE" / "bin" / "blackdog").resolve()
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return shlex.quote(str(candidate))
    return "./.VE/bin/blackdog"


def _build_child_protocol_helper(
    child_run_dir: Path,
    *,
    workspace: Path,
    project_root: Path,
    task_id: str,
    child_agent: str,
) -> Path:
    child_protocol_script = child_run_dir / CHILD_PROTOCOL_HELPER
    script = _CHILD_PROTOCOL_HELPER_TEMPLATE
    script = script.replace("__PROJECT_ROOT__", json.dumps(str(project_root.resolve())))
    script = script.replace("__WORKSPACE__", json.dumps(str(workspace.resolve())))
    script = script.replace("__TASK_ID__", json.dumps(task_id))
    script = script.replace("__CHILD_AGENT__", json.dumps(child_agent))
    child_protocol_script.write_text(script + "\n", encoding="utf-8")
    child_protocol_script.chmod(0o755)
    return child_protocol_script


def _notify_supervisor(
    profile: RepoProfile,
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


def _emit_render(profile: RepoProfile) -> None:
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
    if view.get("recovery_actions"):
        lines.append(f"Recovery actions: {len(view['recovery_actions'])}")
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


def _launch_settings_summary(settings: dict[str, Any] | None) -> str | None:
    if not isinstance(settings, dict):
        return None
    return (
        f"{settings.get('launcher') or '?'}"
        f" | strategy {settings.get('strategy') or 'unknown'}"
        f" | model {settings.get('model') or 'default'}"
        f" | reasoning {_launch_settings_reasoning_label(settings)}"
    )


def _supervisor_status_text(view: dict[str, Any]) -> str:
    lines = [f"Supervisor actor: {view['actor']}"]
    latest_run = view.get("latest_run")
    if isinstance(latest_run, dict):
        lines.append(
            f"Latest run: {latest_run['status']} | {latest_run['run_id']} | steps {latest_run['step_count']} | workspace {latest_run['workspace_mode']}"
        )
        lines.append(f"Status file: {latest_run['status_file']}")
        if latest_run.get("last_checked_at"):
            lines.append(f"Last checked: {latest_run['last_checked_at']}")
        last_step = latest_run.get("last_step")
        if isinstance(last_step, dict):
            lines.append(f"Last step: {last_step.get('status')} @ {last_step.get('at')}")
        latest_run_launch = _launch_settings_summary(latest_run.get("launch_settings"))
        if latest_run_launch is not None:
            lines.append(f"Latest run launch: {latest_run_launch}")
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
    launch_defaults = view.get("launch_defaults")
    if isinstance(launch_defaults, dict):
        lines.append(f"Launch defaults: {_launch_settings_summary(launch_defaults)}")
    recovery = view.get("prelaunch_recovery")
    if isinstance(recovery, dict):
        lines.append(
            "Pre-launch recovery: "
            f"{recovery.get('action')} | {recovery.get('task_id') or 'primary'} | {recovery.get('summary')}"
        )
    recovery_needed = view.get("recovery_needed")
    if isinstance(recovery_needed, dict):
        lines.extend(["", "Recovery needed:"])
        if recovery_needed["cases"]:
            for case in recovery_needed["cases"]:
                lines.append(f"- {case['task_id']} [{case['case']}] {case['summary']}")
                child_result = case.get("latest_child_result_status")
                if child_result is not None:
                    lines.append(
                        "  child result: "
                        f"{child_result} by {case.get('latest_child_result_actor') or '?'}"
                        f" @ {case.get('latest_child_result_recorded_at') or '?'}"
                    )
                supervisor_result = case.get("latest_supervisor_result_status")
                if supervisor_result is not None:
                    lines.append(
                        "  supervisor result: "
                        f"{supervisor_result} by {case.get('latest_supervisor_result_actor') or '?'}"
                        f" @ {case.get('latest_supervisor_result_recorded_at') or '?'}"
                    )
                if case.get("land_error"):
                    lines.append(f"  land_error: {case['land_error']}")
                lines.append(f"  next: {', '.join(case['next_actions'])}")
        else:
            lines.append("- No recovery-needed child outcomes.")
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


def render_supervisor_sweep_output(view: dict[str, Any], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(view, indent=2) + "\n"
    lines = [
        f"Supervisor sweep actor: {view['actor']}",
        f"Sweep changed: {'yes' if view['sweep']['changed'] else 'no'}",
    ]
    if view["sweep"]["removed_task_ids"]:
        lines.append("Removed tasks: " + ", ".join(view["sweep"]["removed_task_ids"]))
    if view["released_task_ids"]:
        lines.append("Released orphaned claims: " + ", ".join(view["released_task_ids"]))
    launch_defaults = view.get("launch_defaults")
    if isinstance(launch_defaults, dict):
        lines.append(f"Launch defaults: {_launch_settings_summary(launch_defaults)}")
    lines.extend(["", "Ready tasks:"])
    if view["ready_tasks"]:
        for task in view["ready_tasks"]:
            lines.append(f"- {task['id']} [{task['risk']}] {task['title']}")
    else:
        lines.append("- No runnable tasks.")
    return "\n".join(lines) + "\n"


def _run_dir_for_id(profile: RepoProfile, run_id: str) -> Path | None:
    matches = sorted(profile.paths.supervisor_runs_dir.glob(f"*-{run_id}"))
    return matches[0].resolve() if matches else None


def _load_supervisor_recovery_status_files(profile: RepoProfile, *, actor: str) -> dict[str, dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    for status_file in sorted(profile.paths.supervisor_runs_dir.glob("*/status.json"), reverse=True):
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("actor") or "") != actor:
            continue
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            continue
        if str(payload.get("status_file") or "") != str(status_file):
            payload["status_file"] = str(status_file)
        if str(payload.get("run_dir") or "") != str(status_file.parent):
            payload["run_dir"] = str(status_file.parent)
        if "steps" not in payload:
            payload["steps"] = []
        runs[run_id] = payload
    return runs


def _load_run_events(profile: RepoProfile, *, actor: str) -> dict[str, list[dict[str, Any]]]:
    events = load_events(profile.paths)
    recovered: dict[str, list[dict[str, Any]]] = {}
    relevant = {"supervisor_run_started", "worktree_start", "child_launch", "child_launch_failed", "child_finish"}
    for event in events:
        if str(event.get("actor") or "") != actor:
            continue
        if str(event.get("type") or "") not in relevant:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            continue
        recovered.setdefault(run_id, []).append(event)
    for rows in recovered.values():
        rows.sort(key=lambda row: str(row.get("at") or ""))
    return recovered


def _build_recovery_child_run(
    profile: RepoProfile,
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    state: dict[str, Any],
    workspace_mode: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "run_id": run_id,
        "task_id": "",
        "child_agent": None,
        "workspace_mode": None,
        "task_branch": None,
        "target_branch": None,
        "primary_worktree": None,
        "workspace": None,
        "pid": None,
        "run_status": None,
        "final_task_status": None,
        "branch_ahead": None,
        "landed": False,
        "land_error": None,
        "exit_code": None,
        "missing_process": False,
        "claim_status": None,
        "run_dir": None,
        "child_artifact_dir": None,
    }
    for event in rows:
        event_type = str(event.get("type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        task_id = str(event.get("task_id") or row["task_id"] or "")
        if task_id:
            row["task_id"] = task_id
        if payload.get("child_agent"):
            row["child_agent"] = payload.get("child_agent")
        if payload.get("workspace_mode"):
            row["workspace_mode"] = normalize_workspace_mode(payload.get("workspace_mode"))
        if payload.get("workspace"):
            row["workspace"] = str(payload.get("workspace"))
        if payload.get("worktree_path"):
            row["workspace"] = str(payload.get("worktree_path"))
        if payload.get("branch"):
            row["task_branch"] = str(payload.get("branch"))
        if payload.get("target_branch"):
            row["target_branch"] = str(payload.get("target_branch"))
        if payload.get("primary_worktree"):
            row["primary_worktree"] = str(payload.get("primary_worktree"))
        if event_type == "worktree_start":
            row["run_status"] = SUPERVISOR_ATTEMPT_STATUS_PREPARED
        elif event_type == "child_launch":
            row["run_status"] = SUPERVISOR_ATTEMPT_STATUS_RUNNING
            row["pid"] = payload.get("pid")
            row["child_agent"] = payload.get("child_agent") or row.get("child_agent")
        elif event_type == "child_launch_failed":
            row["run_status"] = SUPERVISOR_ATTEMPT_STATUS_LAUNCH_FAILED
        elif event_type == "child_finish":
            final_task_status = _normalize_supervisor_attempt_final_task_status(payload.get("final_task_status"))
            if payload.get("missing_process"):
                row["run_status"] = SUPERVISOR_ATTEMPT_STATUS_INTERRUPTED
            elif payload.get("land_error"):
                row["run_status"] = SUPERVISOR_ATTEMPT_STATUS_BLOCKED
            elif payload.get("exit_code") not in {0, None}:
                row["run_status"] = SUPERVISOR_ATTEMPT_STATUS_FAILED
            else:
                row["run_status"] = _attempt_status_from_final_task_status(
                    final_task_status or SUPERVISOR_FINAL_TASK_STATUS_FINISHED,
                    default=SUPERVISOR_ATTEMPT_STATUS_DONE,
                )
            row["exit_code"] = payload.get("exit_code")
            row["missing_process"] = bool(payload.get("missing_process"))
            row["final_task_status"] = final_task_status
            row["branch_ahead"] = bool(payload.get("branch_ahead"))
            row["landed"] = bool(payload.get("landed"))
            row["land_error"] = payload.get("land_error")
            row["child_agent"] = payload.get("child_agent") or row.get("child_agent")
            row["task_branch"] = str(payload.get("branch") or row.get("task_branch") or "")
            row["target_branch"] = str(payload.get("target_branch") or row.get("target_branch") or "")

    if row["run_status"] == SUPERVISOR_ATTEMPT_STATUS_RUNNING:
        if not _pid_alive(row.get("pid")):
            row["run_status"] = SUPERVISOR_ATTEMPT_STATUS_INTERRUPTED
    if row["claim_status"] is None:
        task_id = str(row["task_id"])
        claim_entry = state.get("task_claims", {}).get(task_id) or {}
        if isinstance(claim_entry, dict):
            row["claim_status"] = claim_entry.get("status")
    if row["workspace"] is None and row["task_branch"]:
        resolved_path = find_worktree_for_branch(profile, str(row["task_branch"]))
        row["workspace"] = resolved_path
    if row["workspace_mode"] is None:
        row["workspace_mode"] = normalize_workspace_mode(workspace_mode)
    if row["run_status"] is None:
        row["run_status"] = SUPERVISOR_ATTEMPT_STATUS_UNKNOWN
    if row["task_id"]:
        run_dir = _run_dir_for_id(profile, run_id)
        row["run_dir"] = str(run_dir) if run_dir is not None else None
        row["child_artifact_dir"] = str((run_dir / row["task_id"]).resolve()) if run_dir is not None else None
    return row


def _recovery_case_recommendations(row: dict[str, Any]) -> dict[str, Any] | None:
    run_status = str(row.get("run_status") or "")
    if str(row.get("final_task_status") or "") == CLAIM_STATUS_DONE or str(row.get("claim_status") or "") == CLAIM_STATUS_DONE:
        return None
    if row.get("landed") and str(row.get("final_task_status") or "") not in {"", CLAIM_STATUS_DONE}:
        return {
            "case": SUPERVISOR_RECOVERY_CASE_LANDED_BUT_UNFINISHED,
            "severity": "high",
            "summary": "Child branch was landed but task state is not complete.",
            "next_actions": [
                "record a completion result",
                "create a replacement child run",
                "clean up the task worktree",
            ],
        }
    if run_status == SUPERVISOR_ATTEMPT_STATUS_BLOCKED:
        error_text = str(row.get("land_error") or "").lower()
        if "dirty primary worktree contract violation" in error_text:
            return {
                "case": SUPERVISOR_RECOVERY_CASE_BLOCKED_BY_DIRTY_PRIMARY,
                "severity": "high",
                "summary": "Landing is blocked by primary checkout dirtiness.",
                "next_actions": [
                    "clean dirty primary worktree state",
                    "retry the task run",
                    "create a replacement task run from a fresh branch",
                ],
            }
        return {
            "case": SUPERVISOR_RECOVERY_CASE_BLOCKED_LAND,
            "severity": "high",
            "summary": "Landing failed and blocked child completion.",
            "next_actions": [
                "resolve landing block",
                "retry the task run",
                "create a replacement task run",
            ],
        }
    if run_status in {
        SUPERVISOR_ATTEMPT_STATUS_INTERRUPTED,
        SUPERVISOR_ATTEMPT_STATUS_FAILED,
        SUPERVISOR_ATTEMPT_STATUS_LAUNCH_FAILED,
        SUPERVISOR_FINAL_TASK_STATUS_PARTIAL,
        CLAIM_STATUS_RELEASED,
    }:
        return {
            "case": SUPERVISOR_RECOVERY_CASE_PARTIAL_RUN,
            "severity": "high",
            "summary": "Child run ended without a clean completion outcome.",
            "next_actions": [
                "retry the child run",
                "cancel and clean the worktree",
                "create a replacement task run",
            ],
        }
    return None


def _build_supervisor_recovery_runs(
    profile: RepoProfile,
    *,
    actor: str,
    events_by_run: dict[str, list[dict[str, Any]]],
    status_by_run: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    claim_state = load_state(profile.paths.state_file)
    run_ids = sorted(set(events_by_run.keys()) | set(status_by_run.keys()), reverse=True)
    runs: list[dict[str, Any]] = []
    for run_id in run_ids:
        status_payload = status_by_run.get(run_id, {})
        status = _normalize_supervisor_runtime_status(
            status_payload.get("final_status") or status_payload.get("status"),
            default=SUPERVISOR_RUN_STATUS_HISTORICAL,
        )
        if status == SUPERVISOR_RUN_STATUS_RUNNING and not _pid_alive(status_payload.get("supervisor_pid")):
            status = SUPERVISOR_RUN_STATUS_INTERRUPTED
        run_dir = status_payload.get("run_dir")
        if run_dir is None:
            run_dir = str(_run_dir_for_id(profile, run_id)) if _run_dir_for_id(profile, run_id) else None
        events = events_by_run.get(run_id, [])
        child_map: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            task_id = str(event.get("task_id") or "")
            if not task_id:
                continue
            child_rows = child_map.setdefault(task_id, [])
            child_rows.append(event)
        children: list[dict[str, Any]] = []
        for task_id in sorted(child_map.keys()):
            child_payload = _build_recovery_child_run(
                profile,
                child_map[task_id],
                run_id=run_id,
                state=claim_state,
                workspace_mode=status_payload.get("workspace_mode"),
            )
            case = _recovery_case_recommendations(child_payload)
            if case is not None:
                child_payload["recovery_case"] = case
            children.append(child_payload)
        run_children = sorted(children, key=lambda item: str(item.get("task_id") or ""))
        runs.append(
            {
                "run_id": run_id,
                "status": status,
                "workspace_mode": normalize_workspace_mode(status_payload.get("workspace_mode")),
                "draining": bool(status_payload.get("draining")),
                "run_dir": str(Path(run_dir).resolve()) if run_dir else None,
                "status_file": status_payload.get("status_file"),
                "step_count": len(status_payload.get("steps") or []),
                "children": run_children,
            }
        )
    return runs


def build_supervisor_recover_view(profile: RepoProfile, *, actor: str) -> dict[str, Any]:
    workspace_mode = profile.supervisor_workspace_mode
    latest_run = _latest_run_status(profile, actor=actor)
    if latest_run:
        workspace_mode = normalize_workspace_mode(latest_run.get("workspace_mode") or workspace_mode)
    events_by_run = _load_run_events(profile, actor=actor)
    status_by_run = _load_supervisor_recovery_status_files(profile, actor=actor)
    runs = _build_supervisor_recovery_runs(
        profile,
        actor=actor,
        events_by_run=events_by_run,
        status_by_run=status_by_run,
    )
    recoverable_cases: list[dict[str, Any]] = []
    for run in runs:
        for child in run.get("children", []):
            case = child.get("recovery_case")
            if isinstance(case, dict):
                recoverable_cases.append(
                    {
                        "run_id": run["run_id"],
                        "task_id": child["task_id"],
                        "child_agent": child.get("child_agent"),
                        "task_branch": child.get("task_branch"),
                        "target_branch": child.get("target_branch"),
                        "primary_worktree": child.get("primary_worktree"),
                        "workspace": child.get("workspace"),
                        "child_artifact_dir": child.get("child_artifact_dir"),
                        "run_status": child.get("run_status"),
                        "claim_status": child.get("claim_status"),
                        "final_task_status": child.get("final_task_status"),
                        "branch_ahead": child.get("branch_ahead"),
                        "landed": child.get("landed"),
                        "land_error": child.get("land_error"),
                        "case": case.get("case"),
                        "severity": case.get("severity"),
                        "summary": case.get("summary"),
                        "next_actions": list(case.get("next_actions") or []),
                    }
                )
    severity_rank = {"high": 3, "medium": 2, "low": 1}
    recoverable_cases.sort(
        key=lambda item: (
            -severity_rank.get(str(item.get("severity") or "low"), 1),
            str(item.get("run_id")),
            str(item.get("task_id")),
        )
    )
    return {
        "actor": actor,
        "latest_run": latest_run,
        "workspace_contract": worktree_contract(profile, workspace_mode=workspace_mode),
        "runs": runs,
        "recoverable_cases": recoverable_cases,
    }


def render_supervisor_recover_output(view: dict[str, Any], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(view, indent=2) + "\n"
    lines: list[str] = [f"Supervisor actor: {view['actor']}"]
    latest_run = view.get("latest_run")
    if isinstance(latest_run, dict):
        lines.append(
            f"Latest run: {latest_run.get('status')} | {latest_run.get('run_id')} | steps {latest_run.get('step_count')} | workspace {latest_run.get('workspace_mode')}"
        )
    else:
        lines.append("Latest run: none")
    if view["recoverable_cases"]:
        lines.append(f"Recoverable cases: {len(view['recoverable_cases'])}")
        for index, case in enumerate(view["recoverable_cases"], start=1):
            lines.append(
                f"{index}. {case['task_id']} [{case['run_id']}] {case['run_status']} -> {case['case']} ({case['severity']})"
            )
            lines.append(f"   workspace: {case['workspace']}")
            lines.append(f"   summary: {case['summary']}")
            lines.append(f"   actions: {', '.join(case['next_actions'])}")
    else:
        lines.append("No recoverable supervisor cases detected.")
    return "\n".join(lines) + "\n"


def _artifact_payload_for_attempt(
    run_dir: Path | None,
    task_id: str,
) -> dict[str, Any]:
    if run_dir is None:
        return {f"{item}_exists": False for item in SUPERVISOR_REPORT_REQUIRED_ARTIFACTS}
    attempt_dir = run_dir / task_id
    payload = {"artifacts_dir": str(attempt_dir)}
    for artifact, filename in SUPERVISOR_REPORT_ARTIFACT_FILES.items():
        payload[f"{artifact}_exists"] = (attempt_dir / filename).exists()
    return payload


def _parse_attempt_metadata(artifacts_dir: str | None) -> dict[str, Any]:
    if not artifacts_dir:
        return {"valid": False, "parse_error": False}
    metadata_path = Path(artifacts_dir) / "metadata.json"
    if not metadata_path.is_file():
        return {"valid": False, "parse_error": False, "prompt_hash": None}
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"valid": False, "parse_error": True, "prompt_hash": None}
    if not isinstance(payload, dict):
        return {"valid": False, "parse_error": True, "prompt_hash": None}
    return {
        "valid": True,
        "parse_error": False,
        "prompt_hash": payload.get("prompt_hash"),
    }


def _attempt_payload_from_events(
    events: list[dict[str, Any]],
    *,
    run_id: str,
    run_dir: Path | None,
) -> list[dict[str, Any]]:
    attempts: dict[str, dict[str, Any]] = {}
    for event in events:
        event_type = str(event.get("type") or "")
        if event_type not in {"worktree_start", "child_launch", "child_launch_failed", "child_finish"}:
            continue
        task_id = str(event.get("task_id") or "")
        if not task_id:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        attempt = attempts.setdefault(
            task_id,
            {
                "task_id": task_id,
                "run_id": run_id,
                "child_agent": None,
                "attempted_at": None,
                "workspace": None,
                "workspace_mode": None,
                "branch": None,
                "target_branch": None,
                "launch_error": None,
                "launched": False,
                "exit_code": None,
                "missing_process": None,
                "final_task_status": None,
                "result_recorded": None,
                "branch_ahead": None,
                "landed": None,
                "land_error": None,
                "landed_commit": None,
                "prompt_hash": None,
                "launch_command": None,
                "launch_command_strategy": None,
                "launch_settings": None,
            },
        )
        event_at = str(event.get("at") or "")
        if event_at:
            prev = str(attempt["attempted_at"] or "")
            if not prev or event_at < prev:
                attempt["attempted_at"] = event_at
        if event_type == "worktree_start":
            if payload.get("child_agent"):
                attempt["child_agent"] = str(payload.get("child_agent"))
            if payload.get("workspace"):
                attempt["workspace"] = str(payload.get("workspace"))
            if payload.get("workspace_mode"):
                attempt["workspace_mode"] = normalize_workspace_mode(payload.get("workspace_mode"))
            if payload.get("branch"):
                attempt["branch"] = str(payload.get("branch"))
            if payload.get("target_branch"):
                attempt["target_branch"] = str(payload.get("target_branch"))
        elif event_type == "child_launch":
            attempt["launched"] = True
            attempt["attempted_at"] = attempt["attempted_at"] or event_at
            if payload.get("child_agent"):
                attempt["child_agent"] = str(payload.get("child_agent"))
            if payload.get("workspace"):
                attempt["workspace"] = str(payload.get("workspace"))
            if payload.get("workspace_mode"):
                attempt["workspace_mode"] = normalize_workspace_mode(payload.get("workspace_mode"))
        elif event_type == "child_launch_failed":
            attempt["launch_error"] = str(
                payload.get("error") or payload.get("launch_error") or "launch failed"
            )
            if payload.get("launch_command") is not None:
                attempt["launch_command"] = payload.get("launch_command")
            if payload.get("launch_command_strategy") is not None:
                attempt["launch_command_strategy"] = str(payload.get("launch_command_strategy"))
            if isinstance(payload.get("launch_settings"), dict):
                attempt["launch_settings"] = dict(payload["launch_settings"])
        elif event_type == "child_finish":
            if payload.get("child_agent"):
                attempt["child_agent"] = str(payload.get("child_agent"))
            if payload.get("exit_code") is not None:
                attempt["exit_code"] = payload.get("exit_code")
            if "missing_process" in payload:
                attempt["missing_process"] = bool(payload.get("missing_process"))
            if "result_recorded" in payload:
                attempt["result_recorded"] = bool(payload.get("result_recorded"))
            if payload.get("branch_ahead") is not None:
                attempt["branch_ahead"] = bool(payload.get("branch_ahead"))
            if payload.get("final_task_status") is not None:
                attempt["final_task_status"] = _normalize_supervisor_attempt_final_task_status(
                    payload.get("final_task_status")
                ) or str(payload.get("final_task_status"))
            if "landed" in payload:
                attempt["landed"] = bool(payload.get("landed"))
            if payload.get("land_error") is not None:
                attempt["land_error"] = str(payload.get("land_error"))
            if payload.get("landed_commit") is not None:
                attempt["landed_commit"] = str(payload.get("landed_commit"))
            if payload.get("branch"):
                attempt["branch"] = str(payload.get("branch"))
            if payload.get("target_branch"):
                attempt["target_branch"] = str(payload.get("target_branch"))
            if payload.get("launch_command") is not None:
                attempt["launch_command"] = payload.get("launch_command")
            if payload.get("launch_command_strategy") is not None:
                attempt["launch_command_strategy"] = str(payload.get("launch_command_strategy"))
            if isinstance(payload.get("launch_settings"), dict):
                attempt["launch_settings"] = dict(payload["launch_settings"])
            if payload.get("prompt_hash") is not None:
                attempt["prompt_hash"] = str(payload.get("prompt_hash"))
            attempt["attempted_at"] = attempt["attempted_at"] or event_at
    for attempt in attempts.values():
        artifact_payload = _artifact_payload_for_attempt(run_dir, str(attempt["task_id"]))
        attempt.update(artifact_payload)
        artifact_count = sum(1 for item in SUPERVISOR_REPORT_REQUIRED_ARTIFACTS if attempt.get(f"{item}_exists"))
        attempt["artifact_count"] = artifact_count
        attempt["artifact_complete"] = artifact_count == len(SUPERVISOR_REPORT_REQUIRED_ARTIFACTS)
        attempt["output_shape_note"] = "complete" if attempt["artifact_complete"] else "missing artifacts"
        parsed = _parse_attempt_metadata(attempt.get("artifacts_dir"))
        attempt["metadata_valid"] = bool(parsed["valid"])
        attempt["metadata_parse_error"] = bool(parsed["parse_error"])
        attempt["metadata_prompt_hash"] = parsed.get("prompt_hash")
        if attempt.get("launch_error"):
            attempt["output_shape_note"] = "launch failed"
        attempt["attempted_at"] = str(attempt["attempted_at"] or "")
    return sorted(
        attempts.values(),
        key=lambda row: (
            str(row["task_id"]),
            str(row.get("attempted_at") or ""),
        ),
    )


def _format_percent(attempted: int, total: int) -> float:
    return (attempted / total) * 100 if total else 0.0


def _blank_recovery_result_fields() -> dict[str, Any]:
    return {
        "latest_result_status": None,
        "latest_result_actor": None,
        "latest_result_recorded_at": None,
        "latest_result_needs_user_input": None,
        "latest_result_file": None,
        "latest_child_result_status": None,
        "latest_child_result_actor": None,
        "latest_child_result_recorded_at": None,
        "latest_child_result_needs_user_input": None,
        "latest_child_result_file": None,
        "latest_supervisor_result_status": None,
        "latest_supervisor_result_actor": None,
        "latest_supervisor_result_recorded_at": None,
        "latest_supervisor_result_needs_user_input": None,
        "latest_supervisor_result_file": None,
    }


def _index_supervisor_results(
    results: list[dict[str, Any]],
    *,
    actor: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    child_prefix = f"{actor}/child-"
    index: dict[tuple[str, str], dict[str, Any]] = {}

    def _set_result_fields(entry: dict[str, Any], prefix: str, row: dict[str, Any], actor_name: str) -> None:
        status_key = f"{prefix}_status"
        if entry[status_key] is not None:
            return
        entry[status_key] = row.get("status")
        entry[f"{prefix}_actor"] = actor_name or None
        entry[f"{prefix}_recorded_at"] = row.get("recorded_at")
        entry[f"{prefix}_needs_user_input"] = bool(row.get("needs_user_input"))
        entry[f"{prefix}_file"] = row.get("result_file")

    for row in results:
        task_id = str(row.get("task_id") or "")
        run_id = str(row.get("run_id") or "")
        if not task_id or not run_id:
            continue
        actor_name = str(row.get("actor") or "")
        entry = index.setdefault((task_id, run_id), _blank_recovery_result_fields())
        _set_result_fields(entry, "latest_result", row, actor_name)
        if actor_name.startswith(child_prefix):
            _set_result_fields(entry, "latest_child_result", row, actor_name)
        elif actor_name == actor:
            _set_result_fields(entry, "latest_supervisor_result", row, actor_name)
    return index


def _index_supervisor_results_by_task(
    results: list[dict[str, Any]],
    *,
    actor: str,
) -> dict[str, dict[str, Any]]:
    child_prefix = f"{actor}/child-"
    index: dict[str, dict[str, Any]] = {}

    def _set_result_fields(entry: dict[str, Any], prefix: str, row: dict[str, Any], actor_name: str) -> None:
        status_key = f"{prefix}_status"
        if entry[status_key] is not None:
            return
        entry[status_key] = row.get("status")
        entry[f"{prefix}_actor"] = actor_name or None
        entry[f"{prefix}_recorded_at"] = row.get("recorded_at")
        entry[f"{prefix}_needs_user_input"] = bool(row.get("needs_user_input"))
        entry[f"{prefix}_file"] = row.get("result_file")

    for row in results:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        actor_name = str(row.get("actor") or "")
        entry = index.setdefault(task_id, _blank_recovery_result_fields())
        _set_result_fields(entry, "latest_result", row, actor_name)
        if actor_name.startswith(child_prefix):
            _set_result_fields(entry, "latest_child_result", row, actor_name)
        elif actor_name == actor:
            _set_result_fields(entry, "latest_supervisor_result", row, actor_name)
    return index


def _attach_recovery_result_fields(
    cases: list[dict[str, Any]],
    *,
    result_index: dict[tuple[str, str], dict[str, Any]],
    task_result_index: dict[str, dict[str, Any]] | None = None,
    task_fallback_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        row = dict(case)
        task_id = str(row.get("task_id") or "")
        row.update(
            result_index.get(
                (task_id, str(row.get("run_id") or "")),
                _blank_recovery_result_fields(),
            )
        )
        task_fields = task_result_index.get(task_id) if task_result_index is not None else None
        if (
            task_fields is not None
            and task_fallback_ids is not None
            and task_id in task_fallback_ids
            and row.get("latest_result_status") is None
        ):
            for prefix in ("latest_result", "latest_child_result", "latest_supervisor_result"):
                for suffix in ("status", "actor", "recorded_at", "needs_user_input", "file"):
                    key = f"{prefix}_{suffix}"
                    if row.get(key) is None:
                        row[key] = task_fields.get(key)
        rows.append(row)
    return rows


def _recovery_needed_section(
    cases: list[dict[str, Any]],
    *,
    result_index: dict[tuple[str, str], dict[str, Any]],
    task_result_index: dict[str, dict[str, Any]] | None = None,
    task_fallback_ids: set[str] | None = None,
) -> dict[str, Any]:
    rows = _attach_recovery_result_fields(
        cases,
        result_index=result_index,
        task_result_index=task_result_index,
        task_fallback_ids=task_fallback_ids,
    )
    return {"count": len(rows), "cases": rows}


def _run_status_for_attempt(attempt: dict[str, Any]) -> str:
    if attempt.get("missing_process"):
        return SUPERVISOR_ATTEMPT_STATUS_INTERRUPTED
    if attempt.get("land_error"):
        return SUPERVISOR_ATTEMPT_STATUS_BLOCKED
    if attempt.get("launch_error"):
        return SUPERVISOR_ATTEMPT_STATUS_LAUNCH_FAILED
    if attempt.get("exit_code") not in {0, None}:
        return SUPERVISOR_ATTEMPT_STATUS_FAILED
    final_task_status = _normalize_supervisor_attempt_final_task_status(attempt.get("final_task_status"))
    if final_task_status:
        return _attempt_status_from_final_task_status(final_task_status)
    if attempt.get("launched"):
        return SUPERVISOR_ATTEMPT_STATUS_RUNNING
    return SUPERVISOR_ATTEMPT_STATUS_UNKNOWN


def _build_report_recovery_needed_cases(
    runs: list[dict[str, Any]],
    *,
    result_index: dict[tuple[str, str], dict[str, Any]],
    task_result_index: dict[str, dict[str, Any]] | None = None,
    task_fallback_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    severity_rank = {"high": 3, "medium": 2, "low": 1}
    for run in runs:
        run_id = str(run.get("run_id") or "")
        for attempt in run.get("attempts", []):
            run_status = _run_status_for_attempt(attempt)
            case = _recovery_case_recommendations(
                {
                    "run_status": run_status,
                    "final_task_status": attempt.get("final_task_status"),
                    "claim_status": None,
                    "landed": attempt.get("landed"),
                    "land_error": attempt.get("land_error"),
                }
            )
            if case is None:
                continue
            rows.append(
                {
                    "run_id": run_id,
                    "task_id": str(attempt.get("task_id") or ""),
                    "child_agent": attempt.get("child_agent"),
                    "task_branch": attempt.get("branch"),
                    "target_branch": attempt.get("target_branch"),
                    "primary_worktree": None,
                    "workspace": attempt.get("workspace"),
                    "child_artifact_dir": attempt.get("artifacts_dir"),
                    "run_status": run_status,
                    "claim_status": None,
                    "final_task_status": attempt.get("final_task_status"),
                    "branch_ahead": attempt.get("branch_ahead"),
                    "landed": attempt.get("landed"),
                    "land_error": attempt.get("land_error"),
                    "case": case.get("case"),
                    "severity": case.get("severity"),
                    "summary": case.get("summary"),
                    "next_actions": list(case.get("next_actions") or []),
                }
            )
    rows.sort(
        key=lambda item: (
            -severity_rank.get(str(item.get("severity") or "low"), 1),
            str(item.get("run_id") or ""),
            str(item.get("task_id") or ""),
        )
    )
    return _attach_recovery_result_fields(
        rows,
        result_index=result_index,
        task_result_index=task_result_index,
        task_fallback_ids=task_fallback_ids,
    )


def build_supervisor_observation_view(
    profile: RepoProfile,
    *,
    actor: str,
    run_limit: int | None = None,
) -> dict[str, Any]:
    status_by_run = _load_supervisor_recovery_status_files(profile, actor=actor)
    events_by_run = _load_run_events(profile, actor=actor)
    results = load_task_results(profile.paths)
    result_index = _index_supervisor_results(results, actor=actor)
    task_result_index = _index_supervisor_results_by_task(results, actor=actor)
    result_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in results:
        task_id = str(row.get("task_id") or "")
        run_id = str(row.get("run_id") or "")
        if not task_id or not run_id:
            continue
        key = (task_id, run_id)
        result_by_key.setdefault(key, row)

    run_ids = sorted(set(status_by_run) | set(events_by_run), reverse=True)
    if run_limit is not None and run_limit > 0:
        run_ids = run_ids[:run_limit]

    runs: list[dict[str, Any]] = []
    startup_attempts = 0
    startup_launches = 0
    startup_failures = 0
    landed_attempts = 0
    landing_failures = 0
    artifact_total = 0
    artifact_complete = 0
    landing_error_by_kind: Counter[str] = Counter()
    task_attempt_counts = Counter()

    for run_id in run_ids:
        status = status_by_run.get(run_id, {})
        run_dir = None
        if status.get("run_dir"):
            run_dir = Path(str(status["run_dir"]))
        else:
            run_dir = _run_dir_for_id(profile, run_id)
        events = events_by_run.get(run_id, [])
        attempts = _attempt_payload_from_events(events, run_id=run_id, run_dir=run_dir)
        run_steps = list(status.get("steps") or [])
        runs_row_attempts = 0
        run_launch_failures = 0
        for attempt in attempts:
            task_id = str(attempt["task_id"])
            if not task_id:
                continue
            task_attempt_counts[task_id] += 1
            startup_attempts += 1
            runs_row_attempts += 1
            startup_launches += 1 if bool(attempt["launched"]) else 0
            if attempt["launch_error"]:
                startup_failures += 1
                run_launch_failures += 1
            if attempt["landed"]:
                landed_attempts += 1
            if attempt["land_error"]:
                landing_failures += 1
                landing_error_by_kind[str(attempt["land_error"]).strip().lower() or "unknown"] += 1
            artifact_total += 1
            if attempt["artifact_complete"]:
                artifact_complete += 1
            result_row = result_by_key.get((task_id, run_id))
            if result_row is not None:
                attempt["result_status"] = result_row.get("status")
                attempt["result_file"] = result_row.get("result_file")
                attempt["result_actor"] = result_row.get("actor")
            else:
                attempt["result_status"] = None
                attempt["result_file"] = None
                attempt["result_actor"] = None
        if attempts:
            runs.append(
                {
                    "run_id": run_id,
                    "actor": str(status.get("actor") or actor),
                    "workspace_mode": status.get("workspace_mode"),
                    "final_status": _normalize_supervisor_runtime_status(
                        status.get("final_status") or status.get("status"),
                        default=SUPERVISOR_RUN_STATUS_HISTORICAL,
                    ),
                    "attempts": attempts,
                    "run_dir": str(run_dir) if run_dir else None,
                    "status_file": str(status.get("status_file") or ""),
                    "started_at": str((run_steps[0] if run_steps else {}).get("at") or status.get("started_at") or ""),
                    "completed_at": str(status.get("completed_at") or status.get("finished_at") or ""),
                    "step_count": len(run_steps),
                    "attempt_count": runs_row_attempts,
                    "launch_failures": run_launch_failures,
                    "landed_count": sum(1 for attempt in attempts if attempt.get("landed")),
                    "launch_command": list(status.get("launch_command") or []),
                    "launch_overrides": dict(status.get("launch_overrides") or {}),
                    "launch_settings": (
                        dict(status["launch_settings"])
                        if isinstance(status.get("launch_settings"), dict)
                        else None
                    ),
                }
            )

    summary = {
        "runs_total": len(runs),
        "startup": {
            "attempts": startup_attempts,
            "launched": startup_launches,
            "launch_failures": startup_failures,
            "launch_success_rate": _format_percent(startup_launches - startup_failures, startup_launches + startup_failures),
        },
        "retry": {
            "task_attempt_count": len(task_attempt_counts),
            "retried_tasks": [task_id for task_id, count in sorted(task_attempt_counts.items()) if count > 1],
            "retry_total": sum(max(0, count - 1) for count in task_attempt_counts.values() if count > 1),
            "retry_by_task": {
                task_id: count
                for task_id, count in task_attempt_counts.items()
                if count > 1
            },
        },
        "output_shape": {
            "artifact_total_attempts": artifact_total,
            "artifact_complete_attempts": artifact_complete,
            "artifact_incomplete_attempts": artifact_total - artifact_complete,
            "artifact_completion_rate": _format_percent(artifact_complete, artifact_total),
        },
        "landing": {
            "land_error_count": landing_failures,
            "land_error_reason_count": dict(landing_error_by_kind),
            "landed_attempts": landed_attempts,
            "landing_success_rate": _format_percent(landed_attempts, startup_launches),
        },
    }
    report_task_attempt_counts: Counter[str] = Counter()
    for run in runs:
        for attempt in run.get("attempts", []):
            task_id = str(attempt.get("task_id") or "")
            if task_id:
                report_task_attempt_counts[task_id] += 1
    task_fallback_ids = {task_id for task_id, count in report_task_attempt_counts.items() if count == 1}
    recovery_needed_cases = _build_report_recovery_needed_cases(
        runs,
        result_index=result_index,
        task_result_index=task_result_index,
        task_fallback_ids=task_fallback_ids,
    )
    summary["recovery_needed"] = {
        "count": len(recovery_needed_cases),
        "case_count": dict(Counter(str(row.get("case") or "unknown") for row in recovery_needed_cases)),
    }

    observations: list[dict[str, Any]] = []
    if startup_failures:
        observations.append(
            {
                "category": "startup_friction",
                "severity": "high",
                "summary": "Supervisor startup launched fewer children than attempts.",
                "detail": (
                    f"{startup_failures} launch failures were observed across {startup_attempts} startup attempts."
                ),
            }
        )
    if summary["retry"]["retry_total"]:
        observations.append(
            {
                "category": "retry_pressure",
                "severity": "medium",
                "summary": "Tasks required retries before the requested completion outcome.",
                "detail": (
                    f"{summary['retry']['retry_total']} extra attempts were made on "
                    f"{len(summary['retry']['retried_tasks'])} task(s)."
                ),
            }
        )
    if summary["output_shape"]["artifact_incomplete_attempts"]:
        observations.append(
            {
                "category": "output_shape_consistency",
                "severity": "medium",
                "summary": "Supervisor artifact bundles were not always complete.",
                "detail": (
                    f"{summary['output_shape']['artifact_incomplete_attempts']} attempts missed "
                    "required stdout/stderr/prompt/metadata outputs."
                ),
            }
        )
    if landing_failures:
        observations.append(
            {
                "category": "landing_failures",
                "severity": "high",
                "summary": "Child run landing failures were observed.",
                "detail": (
                    f"{landing_failures} attempts ended with landing failure details; "
                    "operator follow-up is required."
                ),
            }
        )
    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "actor": actor,
        "run_limit": run_limit,
        "runs": runs,
        "summary": summary,
        "recovery_needed": {
            "count": len(recovery_needed_cases),
            "cases": recovery_needed_cases,
        },
        "observations": observations,
    }


def _observation_text(view: dict[str, Any]) -> str:
    summary = view["summary"]
    lines = [f"Supervisor actor: {view['actor']}", f"Runs included: {summary['runs_total']}"]
    if view["run_limit"]:
        lines.append(f"Run limit: {view['run_limit']}")
    startup = summary["startup"]
    lines.extend(
        [
            "",
            "Startup friction:",
            f"- attempts={startup['attempts']} launched={startup['launched']} failures={startup['launch_failures']}",
            f"- launch success rate: {startup['launch_success_rate']:.1f}%",
        ]
    )
    retry = summary["retry"]
    lines.extend(
        [
            "",
            "Retry pressure:",
            f"- tasks={retry['task_attempt_count']} retried_tasks={len(retry['retried_tasks'])} extra_attempts={retry['retry_total']}",
        ]
    )
    output_shape = summary["output_shape"]
    lines.extend(
        [
            "",
            "Output-shape consistency:",
            (
                "- complete="
                f"{output_shape['artifact_complete_attempts']}/{output_shape['artifact_total_attempts']} "
                f"({output_shape['artifact_completion_rate']:.1f}%)"
            ),
        ]
    )
    landing = summary["landing"]
    lines.extend(
        [
            "",
            "Landing outcomes:",
            f"- landing errors={landing['land_error_count']} landed_attempts={landing['landed_attempts']} "
            f"({landing['landing_success_rate']:.1f}%)",
        ]
    )
    if landing["land_error_reason_count"]:
        lines.append("  reasons:")
        for reason, count in sorted(landing["land_error_reason_count"].items()):
            lines.append(f"  - {reason}: {count}")
    recovery_needed = view.get("recovery_needed")
    if isinstance(recovery_needed, dict):
        lines.extend(
            [
                "",
                "Recovery needed:",
                f"- cases={recovery_needed['count']}",
            ]
        )
        for case in recovery_needed["cases"]:
            lines.append(f"  - {case['task_id']} [{case['case']}] {case['summary']}")
            child_result = case.get("latest_child_result_status")
            if child_result is not None:
                lines.append(
                    "    child result: "
                    f"{child_result} by {case.get('latest_child_result_actor') or '?'}"
                )
            supervisor_result = case.get("latest_supervisor_result_status")
            if supervisor_result is not None:
                lines.append(
                    "    supervisor result: "
                    f"{supervisor_result} by {case.get('latest_supervisor_result_actor') or '?'}"
                )
    lines.extend(["", "Observations:"])
    if view["observations"]:
        for index, row in enumerate(view["observations"], start=1):
            lines.append(
                f"{index}. [{row['severity']}] {row['category']}: {row['summary']} :: {row['detail']}"
            )
    else:
        lines.append("- No blocking operational observations.")

    if view["runs"]:
        lines.extend(["", "Runs:"])
        for run in view["runs"]:
            lines.append(
                f"- {run['run_id']} status={run['final_status']} attempts={run['attempt_count']} "
                f"launch_failures={run['launch_failures']} landed={run['landed_count']}"
            )
            launch_summary = _launch_settings_summary(run.get("launch_settings"))
            if launch_summary is not None:
                lines.append(f"  launch: {launch_summary}")
            for attempt in run["attempts"]:
                status = "launched" if attempt["launched"] else "not launched"
                lines.append(
                    f"  * {attempt['task_id']} {attempt['child_agent'] or 'child-unknown'} "
                    f"attempted={attempt['attempted_at'] or '-'} status={status}"
                )
                attempt_launch_summary = _launch_settings_summary(attempt.get("launch_settings"))
                if attempt_launch_summary is not None:
                    lines.append(f"    launch: {attempt_launch_summary}")
                if attempt["launch_error"]:
                    lines.append(f"    launch_error: {attempt['launch_error']}")
                if attempt["land_error"]:
                    lines.append(f"    land_error: {attempt['land_error']}")
                lines.append(
                    "    artifacts="
                    f"{attempt['artifact_count']}/{len(SUPERVISOR_REPORT_REQUIRED_ARTIFACTS)} "
                    f"output_shape={attempt['output_shape_note']}"
                )
    else:
        lines.append("")
        lines.append("No supervisor runs with child attempts were found.")
    return "\n".join(lines) + "\n"


def render_supervisor_observation_output(view: dict[str, Any], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(view, indent=2) + "\n"
    return _observation_text(view)


def _land_branch_with_retry(profile: RepoProfile, *, branch: str, target_branch: str) -> dict[str, Any]:
    errors: list[str] = []
    rebase_result: dict[str, Any] | None = None
    for _ in range(2):
        try:
            payload = land_branch(
                profile,
                branch=branch,
                target_branch=target_branch,
                cleanup=True,
            )
            if rebase_result is not None:
                payload["rebase"] = rebase_result
            if errors:
                payload["retry_errors"] = list(errors)
            return payload
        except WorktreeError as exc:
            detail = str(exc)
            errors.append(detail)
            if "cannot land:" in detail and "not based on the current" in detail:
                try:
                    rebase_result = rebase_branch_onto_target(
                        profile,
                        branch=branch,
                        target_branch=target_branch,
                    )
                except WorktreeError as rebase_exc:
                    errors.append(str(rebase_exc))
                    break
                time.sleep(1)
                continue
            break
    raise WorktreeError("; ".join(errors[-4:]) if errors else "landing failed")


def _mark_task_done(profile: RepoProfile, task_id: str, *, actor: str, note: str) -> None:
    with locked_state(profile.paths.state_file) as state:
        entry = state.setdefault("task_claims", {}).get(task_id) or {}
        if entry.get("status") == CLAIM_STATUS_DONE:
            return
        entry["status"] = CLAIM_STATUS_DONE
        entry["completed_by"] = actor
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
            approvals[task_id]["status"] = APPROVAL_STATUS_DONE
    append_event(profile.paths, event_type="complete", actor=actor, task_id=task_id, payload={"note": note})


def _matching_dirty_primary_case(
    profile: RepoProfile,
    *,
    case: dict[str, Any],
    dirty_paths: list[str],
) -> bool:
    branch = str(case.get("task_branch") or "").strip()
    target_branch = str(case.get("target_branch") or "").strip()
    primary_root = Path(str(case.get("primary_worktree") or profile.paths.project_root)).resolve()
    if not branch or not target_branch or not dirty_paths:
        return False
    branch_paths = branch_changed_paths(profile, branch=branch, target_branch=target_branch)
    if sorted(dict.fromkeys(branch_paths)) != sorted(dict.fromkeys(dirty_paths)):
        return False
    return working_tree_matches_ref(
        profile,
        ref=branch,
        paths=dirty_paths,
        repo_root=primary_root,
    )


def _ensure_stash_followup_task(
    profile: RepoProfile,
    *,
    source_task_id: str | None,
    dirty_paths: list[str],
    stash_ref: str,
) -> str:
    snapshot = load_backlog(profile.paths, profile)
    state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
    source_task = snapshot.tasks.get(source_task_id or "")
    title = (
        f"Resolve supervisor recovery stash for {source_task_id}"
        if source_task_id
        else "Resolve supervisor recovery stash"
    )
    for task in snapshot.tasks.values():
        if task.title == title and not task_done(task.id, state):
            return task.id
    payload = add_task(
        profile,
        title=title,
        bucket="cli",
        priority="P1",
        risk="medium",
        effort="S",
        why="The supervisor stashed dirty primary-worktree changes to recover the WTAM launch gate before starting new child work.",
        evidence=f"Recovery stash {stash_ref} contains: {', '.join(dirty_paths) or 'no explicit paths recorded'}.",
        safe_first_slice="Inspect the recovery stash, decide whether each change belongs in a landed task or standalone work, then apply or drop it explicitly.",
        paths=list(dirty_paths),
        checks=list(profile.validation_commands),
        docs=list(profile.doc_routing_defaults),
        domains=["cli", "state", "events", "inbox"],
        packages=[],
        affected_paths=list(dirty_paths),
        task_shaping=None,
        objective=source_task.payload.get("objective") if source_task is not None else "Supervisor recovery and task outcome UX",
        requires_approval=False,
        approval_reason="",
        epic_id=None,
        epic_title=source_task.epic_title if source_task is not None else "Supervisor recovery and task outcome UX",
        lane_id=None,
        lane_title="Recovery stash follow-up",
        wave=source_task.wave if source_task is not None else 0,
    )
    append_event(
        profile.paths,
        event_type="task_added",
        actor="supervisor",
        task_id=str(payload["id"]),
        payload={"title": payload["title"], "bucket": payload["bucket"]},
    )
    return str(payload["id"])


def _plan_prelaunch_recovery(profile: RepoProfile, *, actor: str) -> dict[str, Any] | None:
    recover_view = build_supervisor_recover_view(profile, actor=actor)
    contract = recover_view.get("workspace_contract") if isinstance(recover_view, dict) else {}
    primary_dirty_paths = list((contract or {}).get("primary_dirty_paths") or [])
    blocked_cases = [
        case
        for case in recover_view.get("recoverable_cases", [])
        if isinstance(case, dict)
        and str(case.get("case") or "") == "blocked_by_dirty_primary"
        and str(case.get("task_branch") or "").strip()
    ]
    if primary_dirty_paths:
        for case in blocked_cases:
            if _matching_dirty_primary_case(profile, case=case, dirty_paths=primary_dirty_paths):
                return {
                    "action": "commit",
                    "summary": f"Commit primary dirty state as the recovered landing for {case['task_id']}.",
                    "dirty_paths": primary_dirty_paths,
                    **case,
                }
        return {
            "action": "stash",
            "summary": "Stash dirty primary state and create an explicit follow-up task before launching new child work.",
            "dirty_paths": primary_dirty_paths,
            **(blocked_cases[0] if blocked_cases else {}),
        }
    if blocked_cases:
        return {
            "action": "land",
            "summary": f"Land the blocked child branch for {blocked_cases[0]['task_id']} before launching new work.",
            **blocked_cases[0],
        }
    return None


def _run_prelaunch_recovery(profile: RepoProfile, *, actor: str, run_id: str) -> dict[str, Any] | None:
    plan = _plan_prelaunch_recovery(profile, actor=actor)
    if plan is None:
        return None
    action = str(plan["action"])
    task_id = str(plan.get("task_id") or "")
    branch = str(plan.get("task_branch") or "")
    target_branch = str(plan.get("target_branch") or "")
    dirty_paths = list(plan.get("dirty_paths") or [])
    if action == "land":
        payload = _land_branch_with_retry(profile, branch=branch, target_branch=target_branch)
        _mark_task_done(
            profile,
            task_id,
            actor=actor,
            note=f"Supervisor recovered blocked landing by landing {branch} into {target_branch}.",
        )
        result_path = record_task_result(
            profile.paths,
            task_id=task_id,
            actor=actor,
            status="success",
            what_changed=[
                f"Recovered the blocked child branch for {task_id} before launching new work.",
                f"Landed {branch} into {target_branch}.",
            ],
            validation=[
                f"Recovered landing for {branch} into {target_branch}.",
                f"Landed commit: {payload.get('landed_commit')}",
            ],
            residual=[],
            needs_user_input=False,
            followup_candidates=[],
            run_id=run_id,
        )
        mirror_task_result_to_threads(
            profile.paths,
            task_id=task_id,
            actor=actor,
            status="success",
            what_changed=[
                f"Recovered the blocked child branch for {task_id} before launching new work.",
                f"Landed {branch} into {target_branch}.",
            ],
            validation=[
                f"Recovered landing for {branch} into {target_branch}.",
                f"Landed commit: {payload.get('landed_commit')}",
            ],
            residual=[],
            needs_user_input=False,
            result_path=result_path,
            run_id=run_id,
        )
        _emit_render(profile)
        return {"action": action, "task_id": task_id, "task_branch": branch, "land_result": payload}
    if action == "commit":
        payload = commit_working_tree_paths(
            profile,
            paths=dirty_paths,
            message=f"Recover {task_id} from dirty primary landing state",
            repo_root=Path(str(plan.get("primary_worktree") or profile.paths.project_root)),
        )
        _mark_task_done(
            profile,
            task_id,
            actor=actor,
            note=f"Supervisor recovered blocked landing by committing the primary dirty state for {task_id}.",
        )
        result_path = record_task_result(
            profile.paths,
            task_id=task_id,
            actor=actor,
            status="success",
            what_changed=[
                f"Recovered the blocked landing for {task_id} by committing the primary dirty state.",
                f"Committed paths: {', '.join(dirty_paths)}",
            ],
            validation=[
                f"Recovered primary commit: {payload['commit']}",
                f"Commit message: {payload['message']}",
            ],
            residual=[
                f"Task branch {branch} is left in place for inspection because the recovery landed through the primary worktree commit path."
            ],
            needs_user_input=False,
            followup_candidates=[],
            run_id=run_id,
        )
        mirror_task_result_to_threads(
            profile.paths,
            task_id=task_id,
            actor=actor,
            status="success",
            what_changed=[
                f"Recovered the blocked landing for {task_id} by committing the primary dirty state.",
                f"Committed paths: {', '.join(dirty_paths)}",
            ],
            validation=[
                f"Recovered primary commit: {payload['commit']}",
                f"Commit message: {payload['message']}",
            ],
            residual=[
                f"Task branch {branch} is left in place for inspection because the recovery landed through the primary worktree commit path."
            ],
            needs_user_input=False,
            result_path=result_path,
            run_id=run_id,
        )
        _emit_render(profile)
        return {
            "action": action,
            "task_id": task_id,
            "task_branch": branch,
            "commit": payload["commit"],
            "paths": list(dirty_paths),
        }
    if action == "stash":
        message = f"blackdog recovery {task_id or 'primary'} {run_id}"
        payload = stash_working_tree(
            profile,
            message=message,
            repo_root=Path(str(plan.get("primary_worktree") or profile.paths.project_root)),
            include_untracked=True,
        )
        followup_task_id = _ensure_stash_followup_task(
            profile,
            source_task_id=task_id or None,
            dirty_paths=dirty_paths,
            stash_ref=str(payload["stash_ref"]),
        )
        _notify_supervisor(
            profile,
            actor=actor,
            task_id=task_id or None,
            kind="warning",
            tags=["supervisor", "recovery", "stash"],
            body=(
                f"Stashed dirty primary-worktree state as {payload['stash_ref']} before launching new child work. "
                f"Created follow-up task {followup_task_id} to resolve the stash contents explicitly."
            ),
        )
        _emit_render(profile)
        return {
            "action": action,
            "task_id": task_id or None,
            "paths": list(dirty_paths),
            "stash_ref": payload["stash_ref"],
            "followup_task_id": followup_task_id,
        }
    raise SupervisorError(f"unknown prelaunch recovery action: {action}")


def _select_tasks(
    snapshot: BacklogSnapshot,
    state: dict[str, Any],
    *,
    task_ids: list[str],
    allow_high_risk: bool,
    limit: int,
    force: bool,
) -> list[BacklogTask]:
    if task_ids:
        selected: list[BacklogTask] = []
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


def _load_synced_runtime(profile: RepoProfile) -> tuple[BacklogSnapshot, dict[str, Any]]:
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


def _latest_run_status(profile: RepoProfile, *, actor: str) -> dict[str, Any] | None:
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
        status = _normalize_supervisor_runtime_status(
            payload.get("final_status") or (last_step or {}).get("status"),
            default=SUPERVISOR_RUN_STATUS_RUNNING,
        )
        if not payload.get("final_status") and not _pid_alive(payload.get("supervisor_pid")):
            status = SUPERVISOR_RUN_STATUS_INTERRUPTED
        return {
            "run_id": payload.get("run_id"),
            "actor": payload.get("actor"),
            "status": status,
            "workspace_mode": normalize_workspace_mode(payload.get("workspace_mode")),
            "poll_interval_seconds": payload.get("poll_interval_seconds"),
            "draining": bool(payload.get("draining")),
            "run_dir": payload.get("run_dir") or str(status_file.parent),
            "status_file": str(status_file),
            "step_count": len(steps),
            "last_step": last_step,
            "last_checked_at": payload.get("last_checked_at") or (last_step or {}).get("at") or payload.get("completed_at"),
            "completed_at": payload.get("completed_at"),
            "final_status": payload.get("final_status"),
            "stopped_by_message_id": payload.get("stopped_by_message_id"),
            "supervisor_pid": payload.get("supervisor_pid"),
            "launch_command": list(payload.get("launch_command") or []),
            "launch_overrides": dict(payload.get("launch_overrides") or {}),
            "launch_settings": (
                dict(payload["launch_settings"])
                if isinstance(payload.get("launch_settings"), dict)
                else None
            ),
        }
    return None


def build_supervisor_status_view(
    profile: RepoProfile,
    *,
    actor: str,
    allow_high_risk: bool,
) -> dict[str, Any]:
    snapshot = load_backlog(profile.paths, profile)
    state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
    latest_run = _latest_run_status(profile, actor=actor)
    workspace_mode = normalize_workspace_mode((latest_run or {}).get("workspace_mode") or profile.supervisor_workspace_mode)
    open_messages = load_inbox(profile.paths, recipient=actor, status="open")
    results = load_task_results(profile.paths)
    result_index = _index_supervisor_results(results, actor=actor)
    task_result_index = _index_supervisor_results_by_task(results, actor=actor)
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
    for row in results:
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
    recover_view = build_supervisor_recover_view(profile, actor=actor)
    recoverable_cases = list(recover_view.get("recoverable_cases") or [])
    recovery_task_counts: Counter[str] = Counter(
        str(case.get("task_id") or "") for case in recoverable_cases if str(case.get("task_id") or "")
    )
    task_fallback_ids = {task_id for task_id, count in recovery_task_counts.items() if count == 1}
    return {
        "actor": actor,
        "latest_run": latest_run,
        "workspace_contract": worktree_contract(profile, workspace_mode=workspace_mode),
        "launch_defaults": build_supervisor_launch_defaults_view(profile),
        "prelaunch_recovery": _plan_prelaunch_recovery(profile, actor=actor),
        "recovery_needed": _recovery_needed_section(
            recoverable_cases,
            result_index=result_index,
            task_result_index=task_result_index,
            task_fallback_ids=task_fallback_ids,
        ),
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


def _mark_run_checked(
    status_payload: dict[str, Any],
    status_file: Path,
    *,
    checked_at: str | None = None,
    persist: bool = False,
) -> str:
    resolved_checked_at = checked_at or now_iso()
    status_payload["last_checked_at"] = resolved_checked_at
    if persist:
        _write_run_status(status_file, status_payload)
    return resolved_checked_at


def _claim_for_child(profile: RepoProfile, snapshot: BacklogSnapshot, task: BacklogTask, *, child_agent: str) -> None:
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


def _record_child_claim_process(profile: RepoProfile, task_id: str, *, child_agent: str, pid: int) -> None:
    if pid < 1:
        return
    with locked_state(profile.paths.state_file) as state:
        entry = state.setdefault("task_claims", {}).get(task_id) or {}
        if entry.get("status") != CLAIM_STATUS_CLAIMED or entry.get("claimed_by") != child_agent:
            return
        entry["claimed_pid"] = pid
        entry["claimed_process_missing_scans"] = 0
        entry["claimed_process_last_seen_at"] = now_iso()
        entry.pop("claim_expires_at", None)
        state["task_claims"][task_id] = entry


def _scan_claim_process_liveness(
    profile: RepoProfile,
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
            if not isinstance(entry, dict) or entry.get("status") != CLAIM_STATUS_CLAIMED:
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
            entry["status"] = CLAIM_STATUS_RELEASED
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


def _release_if_still_claimed(profile: RepoProfile, task_id: str, *, child_agent: str, note: str) -> None:
    with locked_state(profile.paths.state_file) as state:
        entry = state.setdefault("task_claims", {}).get(task_id) or {}
        if entry.get("status") != CLAIM_STATUS_CLAIMED or entry.get("claimed_by") != child_agent:
            return
        entry["status"] = CLAIM_STATUS_RELEASED
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


def _complete_if_still_claimed(profile: RepoProfile, task_id: str, *, child_agent: str, note: str) -> None:
    with locked_state(profile.paths.state_file) as state:
        entry = state.setdefault("task_claims", {}).get(task_id) or {}
        if entry.get("status") == CLAIM_STATUS_DONE:
            return
        owner = entry.get("claimed_by")
        if owner and owner != child_agent:
            raise SupervisorError(f"Task {task_id} is not owned by {child_agent}; cannot complete after land")
        entry["status"] = CLAIM_STATUS_DONE
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
            approvals[task_id]["status"] = APPROVAL_STATUS_DONE
    append_event(profile.paths, event_type="complete", actor=child_agent, task_id=task_id, payload={"note": note})


def _prepare_workspace(
    profile: RepoProfile,
    task: BacklogTask,
    *,
    workspace_mode: str,
    run_id: str,
) -> PreparedWorkspace:
    if workspace_mode != WORKSPACE_MODE_GIT_WORKTREE:
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


def _land_child_branch(profile: RepoProfile, child: ChildRun, *, actor: str) -> dict[str, Any]:
    spec = child.worktree_spec
    if spec is None:
        raise WorktreeError("missing worktree spec for branch-backed child run")

    try:
        return _land_branch_with_retry(profile, branch=spec.branch, target_branch=spec.target_branch)
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


def build_supervisor_launch_defaults_view(profile: RepoProfile) -> dict[str, Any]:
    command, strategy = _resolved_launch_command_with_strategy(profile)
    return _launch_defaults_view(profile, _launch_settings_view(command, strategy=strategy))


def _resolved_launch_command(profile: RepoProfile) -> list[str]:
    command, _ = _resolved_launch_command_with_strategy(profile)
    return command


def _resolved_launch_command_with_strategy(
    profile: RepoProfile,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> tuple[list[str], str]:
    command = list(profile.supervisor_launch_command)
    strategy = "profile"
    if (
        tuple(profile.supervisor_launch_command) == DEFAULT_SUPERVISOR_COMMAND
        and DESKTOP_CODEX_BINARY.is_file()
        and os.access(DESKTOP_CODEX_BINARY, os.X_OK)
    ):
        command[0] = str(DESKTOP_CODEX_BINARY)
        strategy = "default-desktop-codex"
    command = _apply_launch_overrides(command, model=model, reasoning_effort=reasoning_effort)
    return command, strategy


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


def _attempt_land_child_worktree(profile: RepoProfile, child: ChildRun, *, actor: str, run_id: str) -> None:
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
        if current_status == CLAIM_STATUS_CLAIMED:
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
        if current_status == CLAIM_STATUS_CLAIMED:
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
        if current_status == CLAIM_STATUS_CLAIMED:
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
    if current_status != CLAIM_STATUS_DONE:
        _complete_if_still_claimed(
            profile,
            child.task.id,
            child_agent=child.child_agent,
            note=f"Supervisor landed {spec.branch} into {spec.target_branch} and completed the task.",
        )


def _finalize_child_run(profile: RepoProfile, child: ChildRun, *, actor: str) -> None:
    if child.stdout_handle is not None:
        child.stdout_handle.close()
    if child.stderr_handle is not None:
        child.stderr_handle.close()

    after_results = {
        str(row["result_file"]) for row in load_task_results(profile.paths, task_id=child.task.id) if row.get("result_file")
    }
    child.result_recorded = bool(after_results - child.result_files_before)
    state = load_state(profile.paths.state_file)
    child.final_task_status = CLAIM_STATUS_DONE if task_done(child.task.id, state) else str(
        (state.get("task_claims", {}).get(child.task.id) or {}).get("status") or "open"
    )
    if child.result_recorded and child.final_task_status == CLAIM_STATUS_DONE and not child.land_error:
        result_metadata = dict(child.telemetry)
        result_metadata.update(
            {
                "final_task_status": child.final_task_status,
                "exit_code": child.exit_code,
                "missing_process": child.missing_process,
                "branch_ahead": child.branch_ahead,
                "landed": child.landed,
                "land_error": child.land_error,
                "result_recorded": child.result_recorded,
                "landed_commit": (child.land_result or {}).get("landed_commit"),
                "run_dir": str(child.run_dir),
            }
        )
        result_rows = [
            str(row["result_file"]) for row in load_task_results(profile.paths, task_id=child.task.id) if row.get("result_file")
        ]
        result_paths = sorted(path for path in result_rows if path in after_results - child.result_files_before)
        for result_file in result_paths[-1:]:
            path = Path(result_file)
            try:
                result_payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, FileNotFoundError):
                continue
            if not isinstance(result_payload, dict):
                continue
            existing_metadata = result_payload.get("metadata")
            if isinstance(existing_metadata, dict):
                existing_metadata.update(result_metadata)
                result_payload["metadata"] = existing_metadata
            else:
                result_payload["metadata"] = result_metadata
            atomic_write_text(path, json.dumps(result_payload, indent=2, sort_keys=True) + "\n")
            break
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
    if child.final_task_status == CLAIM_STATUS_DONE:
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
    result_metadata = dict(child.telemetry)
    result_metadata.update(
        {
            "final_task_status": child.final_task_status,
            "exit_code": child.exit_code,
            "missing_process": child.missing_process,
            "branch_ahead": child.branch_ahead,
            "landed": child.landed,
            "land_error": child.land_error,
            "result_recorded": child.result_recorded,
            "landed_commit": (child.land_result or {}).get("landed_commit"),
            "run_dir": str(child.run_dir),
        }
    )
    result_path = record_task_result(
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
        metadata=result_metadata,
    )
    mirror_task_result_to_threads(
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
        result_path=result_path,
        run_id=child.run_dir.name,
    )
    if child.final_task_status != CLAIM_STATUS_DONE:
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
    profile: RepoProfile,
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
            **child.telemetry,
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
) -> list[BacklogTask]:
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
    profile: RepoProfile,
    task: BacklogTask,
    *,
    actor: str,
    child_agent: str,
    run_id: str,
    run_dir: Path,
    launch_command: tuple[str, ...],
    launch_command_strategy: str,
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
            telemetry=_build_child_launch_telemetry(
                launch_command=tuple(launch_command),
                launch_command_strategy=launch_command_strategy,
                prompt="",
            ),
        )
        _finalize_child_run(profile, child, actor=actor)
        append_event(
            profile.paths,
            event_type="child_launch_failed",
            actor=actor,
            task_id=task.id,
            payload={
                "run_id": run_id,
                "child_agent": child_agent,
                "error": str(exc),
                **child.telemetry,
            },
        )
        _emit_render(profile)
        return child
    workspace = prepared.workspace
    protocol_command = _build_child_protocol_helper(
        child_run_dir,
        workspace=workspace,
        project_root=profile.paths.project_root,
        task_id=task.id,
        child_agent=child_agent,
    )
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
        protocol_command=protocol_command,
        worktree_spec=prepared.worktree_spec,
    )
    launch_telemetry = _build_child_launch_telemetry(
        launch_command=launch_command,
        launch_command_strategy=launch_command_strategy,
        prompt=prompt,
    )
    metadata_file = child_run_dir / "metadata.json"
    metadata = {
        "run_id": run_id,
        **launch_telemetry,
    }
    metadata.update(
        {
            "task_id": task.id,
            "child_agent": child_agent,
            "workspace": str(workspace),
            "workspace_mode": workspace_mode,
            "protocol_command": str(protocol_command),
            "prompt_file": str(prompt_file),
            "stdout_file": str(stdout_file),
            "stderr_file": str(stderr_file),
            "launched_at": now_iso(),
            "metadata_file": str(metadata_file),
        }
    )
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
        telemetry=launch_telemetry,
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
            payload={
                "run_id": run_id,
                "child_agent": child_agent,
                "error": str(exc),
                **child.telemetry,
            },
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
    recovery_actions: list[dict[str, Any]] | None = None,
) -> None:
    step_at = _mark_run_checked(status_payload, status_file, persist=False)
    step = {
        "index": len(status_payload["steps"]) + 1,
        "at": step_at,
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
    if recovery_actions:
        step["recovery_actions"] = list(recovery_actions)
    status_payload["steps"].append(step)
    _write_run_status(status_file, status_payload)


def run_supervisor(
    profile: RepoProfile,
    *,
    actor: str,
    task_ids: list[str],
    count: int,
    allow_high_risk: bool,
    force: bool,
    workspace_mode: str | None,
    poll_interval_seconds: float | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    selected_count = count or profile.supervisor_max_parallel
    resolved_workspace_mode = normalize_workspace_mode(workspace_mode or profile.supervisor_workspace_mode)
    resolved_poll_interval_seconds = (
        DEFAULT_SUPERVISOR_POLL_INTERVAL_SECONDS if poll_interval_seconds is None else poll_interval_seconds
    )
    if resolved_workspace_mode != WORKSPACE_MODE_GIT_WORKTREE:
        raise BacklogError("workspace mode must be 'git-worktree'")
    if resolved_poll_interval_seconds < 0:
        raise BacklogError("poll interval must be at least 0 seconds")

    sweep = sweep_completed_tasks(profile)
    run_id = uuid.uuid4().hex[:8]
    run_dir = profile.paths.supervisor_runs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    status_file = run_dir / "status.json"
    base_launch_command, launch_command_strategy = _resolved_launch_command_with_strategy(profile)
    base_launch_command = tuple(base_launch_command)
    run_launch_command = tuple(
        _apply_launch_overrides(
            list(base_launch_command),
            model=model if model is not None else profile.supervisor_model,
            reasoning_effort=(
                reasoning_effort
                if reasoning_effort is not None
                else (None if profile.supervisor_dynamic_reasoning else profile.supervisor_reasoning_effort)
            ),
        )
    )
    run_launch_overrides = {
        "model": model,
        "reasoning_effort": reasoning_effort,
    }
    run_launch_settings = _launch_defaults_view(
        profile,
        _launch_settings_view(run_launch_command, strategy=launch_command_strategy),
    )
    status_payload: dict[str, Any] = {
        "run_id": run_id,
        "actor": actor,
        "workspace_mode": resolved_workspace_mode,
        "poll_interval_seconds": resolved_poll_interval_seconds,
        "draining": False,
        "run_dir": str(run_dir),
        "status_file": str(status_file),
        "supervisor_pid": os.getpid(),
        "last_checked_at": now_iso(),
        "recovery_actions": [],
        "launch_command": list(run_launch_command),
        "launch_overrides": dict(run_launch_overrides),
        "launch_settings": dict(run_launch_settings),
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
            "launch_command": list(run_launch_command),
            "launch_overrides": dict(run_launch_overrides),
            "launch_settings": dict(run_launch_settings),
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
        status=SUPERVISOR_RUN_STEP_STATUS_SWEPT,
        ready_task_ids=[],
        running_task_ids=[],
        open_message_ids=[],
        removed_task_ids=list(sweep["removed_task_ids"]),
    )

    children: list[ChildRun] = []
    active: dict[str, ChildRun] = {}
    completion_queue: queue.Queue[tuple[str, int | None]] = queue.Queue()
    attempted_task_ids: set[str] = set()
    recovery_actions: list[dict[str, Any]] = []
    launched_count = 0
    launch_command_checked = False
    next_claim_liveness_scan_at = 0.0

    def start_child(task: BacklogTask) -> None:
        nonlocal launched_count, launch_command_checked
        launch_overrides = _resolved_task_launch_overrides(
            profile,
            task,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        child_launch_command = tuple(
            _apply_launch_overrides(
                list(base_launch_command),
                model=launch_overrides["model"],
                reasoning_effort=launch_overrides["reasoning_effort"],
            )
        )
        if not launch_command_checked:
            _preflight_launch_command(child_launch_command)
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
            launch_command=child_launch_command,
            launch_command_strategy=launch_command_strategy,
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
        pending_requested_task_ids = [
            task_id for task_id in task_ids if task_id not in attempted_task_ids and task_id not in active_task_ids
        ]
        step_recovery_actions: list[dict[str, Any]] = []
        if not active and not status_payload["draining"] and pending_requested_task_ids:
            while True:
                recovery = _run_prelaunch_recovery(profile, actor=actor, run_id=run_dir.name)
                if recovery is None:
                    break
                step_recovery_actions.append(recovery)
                recovery_actions.append(recovery)
                status_payload["recovery_actions"] = list(recovery_actions)
                snapshot, state = _load_synced_runtime(profile)
                active_task_ids = {child.task.id for child in active.values()}
                pending_requested_task_ids = [
                    task_id for task_id in task_ids if task_id not in attempted_task_ids and task_id not in active_task_ids
                ]
                if not pending_requested_task_ids:
                    break
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
        if not active and not status_payload["draining"] and ready_tasks and not step_recovery_actions:
            while True:
                recovery = _run_prelaunch_recovery(profile, actor=actor, run_id=run_dir.name)
                if recovery is None:
                    break
                step_recovery_actions.append(recovery)
                recovery_actions.append(recovery)
                status_payload["recovery_actions"] = list(recovery_actions)
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
                if not ready_tasks:
                    break

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

        if launched_task_ids or finished_task_ids or released_task_ids or control_message_id or step_recovery_actions:
            _append_run_step(
                status_payload,
                status_file,
                status=SUPERVISOR_RUN_STATUS_DRAINING if status_payload["draining"] else SUPERVISOR_RUN_STATUS_RUNNING,
                ready_task_ids=[task.id for task in ready_tasks],
                running_task_ids=[child.task.id for child in active.values()],
                open_message_ids=[str(message.get("message_id") or "") for message in open_messages],
                launched_task_ids=launched_task_ids,
                finished_task_ids=finished_task_ids,
                control_message_id=control_message_id,
                released_task_ids=released_task_ids,
                recovery_actions=step_recovery_actions,
            )

        if not active:
            if status_payload["draining"]:
                status_payload["completed_at"] = now_iso()
                status_payload["final_status"] = SUPERVISOR_RUN_STATUS_STOPPED
                _append_run_step(
                    status_payload,
                    status_file,
                    status=SUPERVISOR_RUN_STATUS_STOPPED,
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
                status_payload["final_status"] = SUPERVISOR_RUN_STATUS_IDLE
                _append_run_step(
                    status_payload,
                    status_file,
                    status=SUPERVISOR_RUN_STATUS_IDLE,
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
            _mark_run_checked(status_payload, status_file, persist=True)
            continue
        child = active.pop(child_agent, None)
        if child is None:
            continue
        child.exit_code = exit_code if exit_code is not None else child.process.poll() if child.process is not None else None
        _finish_child(profile, child, actor=actor, run_id=run_id)
        _append_run_step(
            status_payload,
            status_file,
            status=SUPERVISOR_RUN_STATUS_DRAINING if status_payload["draining"] else SUPERVISOR_RUN_STATUS_RUNNING,
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
            "final_status": status_payload.get("final_status") or SUPERVISOR_RUN_STATUS_IDLE,
            "stopped_by_message_id": status_payload.get("stopped_by_message_id"),
            "launch_command": list(run_launch_command),
            "launch_overrides": dict(run_launch_overrides),
            "launch_settings": dict(run_launch_settings),
        },
    )
    status_payload["recovery_actions"] = list(recovery_actions)
    _write_run_status(status_file, status_payload)
    return {
        "run_id": run_id,
        "actor": actor,
        "launch_command": list(run_launch_command),
        "launch_overrides": dict(run_launch_overrides),
        "launch_settings": dict(run_launch_settings),
        "workspace_mode": resolved_workspace_mode,
        "poll_interval_seconds": resolved_poll_interval_seconds,
        "draining": bool(status_payload.get("draining")),
        "final_status": status_payload.get("final_status") or SUPERVISOR_RUN_STATUS_IDLE,
        "run_dir": str(run_dir),
        "status_file": str(status_file),
        "steps": list(status_payload["steps"]),
        "stopped_by_message_id": status_payload.get("stopped_by_message_id"),
        "recovery_actions": list(recovery_actions),
        "children": [
            {
                "task_id": child.task.id,
                "title": child.task.title,
                "child_agent": child.child_agent,
                "launch_command": list(child.launch_command),
                "launch_settings": _launch_settings_view(child.launch_command, strategy=launch_command_strategy),
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


def run_supervisor_sweep(
    profile: RepoProfile,
    *,
    actor: str,
    allow_high_risk: bool,
) -> dict[str, Any]:
    sweep = sweep_completed_tasks(profile)
    released_task_ids = _scan_claim_process_liveness(profile, actor=actor, skip_task_ids=set())
    snapshot = load_backlog(profile.paths, profile)
    state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
    if sweep["changed"]:
        _emit_render(profile)
    view = build_supervisor_status_view(profile, actor=actor, allow_high_risk=allow_high_risk)
    return {
        **view,
        "sweep": {
            "changed": bool(sweep["changed"]),
            "removed_task_ids": list(sweep["removed_task_ids"]),
            "removed_lane_ids": list(sweep["removed_lane_ids"]),
            "removed_epic_ids": list(sweep["removed_epic_ids"]),
            "wave_map": dict(sweep["wave_map"]),
        },
        "released_task_ids": list(released_task_ids),
        "ready_tasks": [
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
        ],
    }
