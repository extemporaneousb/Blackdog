from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
import sys
import tempfile
import time
import tomllib
from typing import Any

from .backlog import (
    BacklogError,
    add_task,
    build_plan_view,
    build_view_model,
    classify_task_status,
    seed_tune_task,
    load_backlog,
    next_runnable_tasks,
    render_plan_text,
    render_summary_text,
    sync_state_for_backlog,
)
from .config import ConfigError, load_profile
from .scaffold import (
    ScaffoldError,
    bootstrap_project,
    create_project,
    remove_named_backlog,
    render_project_html,
    reset_default_backlog,
    scaffold_named_backlog,
    scaffold_project,
)
from .store import (
    StoreError,
    append_event,
    claim_is_active,
    claim_task_entry,
    load_events,
    load_inbox,
    load_state,
    load_task_results,
    locked_state,
    now_iso,
    record_comment,
    record_task_result,
    resolve_message,
    save_state,
    send_message,
)
from .supervisor import (
    SupervisorError,
    build_supervisor_status_view,
    build_supervisor_recover_view,
    build_supervisor_observation_view,
    render_supervisor_output,
    render_supervisor_status_output,
    render_supervisor_recover_output,
    render_supervisor_observation_output,
    run_supervisor,
)
from .ui import UIError, build_ui_snapshot
from .worktree import (
    WorktreeError,
    cleanup_task_worktree,
    land_branch,
    render_cleanup_text,
    render_land_text,
    render_preflight_text,
    render_start_text,
    start_task_worktree,
    task_id_for_branch,
    worktree_preflight,
)


_COVERAGE_LINE_RE = re.compile(r"^\s*(?:(?P<count>\d+)\s*:|(?P<missing>>>>>>))\s*(?P<code>.*)$")
_ENV_ASSIGN_RE = re.compile(r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$")
_TRACE_RUNNER = """
import io
import os
import runpy
import sys
import trace


def _system_exit_code(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    print(value, file=sys.stderr)
    return 1


def _main():
    cover_dir, mode, target, *arguments = sys.argv[1:]
    tracer = trace.Trace(count=1, trace=0)
    exit_code = 0
    try:
        if mode == "module":
            _, mod_spec, code = runpy._get_module_details(target)
            sys.argv = [code.co_filename, *arguments]
            globs = {
                "__name__": "__main__",
                "__file__": code.co_filename,
                "__package__": mod_spec.parent,
                "__loader__": mod_spec.loader,
                "__spec__": mod_spec,
                "__cached__": None,
            }
        elif mode == "script":
            sys.argv = [target, *arguments]
            sys.path[0] = os.path.dirname(target)
            with io.open_code(target) as handle:
                code = compile(handle.read(), target, "exec")
            globs = {
                "__file__": target,
                "__name__": "__main__",
                "__package__": None,
                "__cached__": None,
            }
        else:
            raise SystemExit(f"Unsupported coverage runner mode: {mode}")
        tracer.runctx(code, globs, globs)
    except OSError as err:
        sys.exit(f"Cannot run file {sys.argv[0]!r} because: {err}")
    except SystemExit as exc:
        exit_code = _system_exit_code(exc.code)
    tracer.results().write_results(show_missing=True, summary=False, coverdir=cover_dir)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    _main()
""".strip()


def _load_runtime(project_root: Path | None = None):
    profile = load_profile(project_root)
    snapshot = load_backlog(profile.paths, profile)
    state = load_state(profile.paths.state_file)
    state = sync_state_for_backlog(state, snapshot)
    save_state(profile.paths.state_file, state)
    return profile, snapshot, state


def _emit_render(profile) -> None:
    if profile.auto_render_html:
        render_project_html(profile)


def _env_default(value: str | None, env_var: str) -> str | None:
    if value:
        return value
    return os.environ.get(env_var)


def _env_required(value: str | None, env_var: str, *, arg_name: str, command: str) -> str:
    resolved = _env_default(value, env_var)
    if resolved:
        return resolved
    raise BacklogError(f"{command} requires --{arg_name} (or set ${env_var})")


def _parse_json_object(value: str | None, *, command: str, flag: str) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BacklogError(f"{command} requires valid JSON for {flag}; parse failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise BacklogError(f"{command} requires {flag} to be a JSON object")
    return payload


def _parse_trace_command(command: str) -> tuple[dict[str, str], list[str]]:
    parts = shlex.split(command)
    if not parts:
        raise BacklogError("Coverage command is empty.")
    env: dict[str, str] = {}
    while parts:
        match = _ENV_ASSIGN_RE.match(parts[0])
        if match is None:
            break
        env[match.group("key")] = match.group("value")
        parts.pop(0)
    if not parts:
        raise BacklogError(f"Coverage command is only environment assignments: {command!r}")
    return env, parts


def _load_coverage_profile_settings(project_root: Path) -> dict[str, object]:
    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return {}
    with pyproject_path.open("rb") as handle:
        payload = tomllib.load(handle)
    return dict((payload.get("tool") or {}).get("blackdog", {}).get("coverage") or {})


def _build_trace_runner(parts: list[str], *, cover_dir: Path) -> list[str]:
    if not parts:
        raise BacklogError(f"Coverage command is not executable: {parts!r}")

    command = [sys.executable, "-c", _TRACE_RUNNER, str(cover_dir)]
    head = parts[0]
    if Path(head).name.startswith("python"):
        python_args = parts[1:]
        if not python_args:
            raise BacklogError(f"Coverage command is incomplete: {parts!r}")
    elif Path(head).suffix == ".py":
        python_args = parts
    else:
        raise BacklogError(
            f"Coverage command must start with a Python executable or a .py script path: {parts!r}"
        )

    if python_args[0] == "-m":
        if len(python_args) < 2:
            raise BacklogError(f"Coverage command is incomplete: {parts!r}")
        command.extend(["module", python_args[1], *python_args[2:]])
    else:
        command.extend(["script", *python_args])
    return command


def _parse_coverage_file(path: Path) -> tuple[int, int]:
    covered = 0
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _COVERAGE_LINE_RE.match(line)
        if match is None:
            continue
        code = match.group("code").strip()
        if not code:
            continue
        total += 1
        if match.group("count") is not None and match.group("count").strip():
            covered += 1
    return covered, total


def _coverage_source(profile_root: Path, source_root: Path, cover_file: Path) -> Path | None:
    if not source_root.is_dir():
        return None
    target = source_root / Path(*cover_file.stem.split(".")).with_suffix(".py")
    if not target.exists():
        return None
    try:
        relative = target.relative_to(profile_root)
    except ValueError:
        return None
    if not (relative.parts[0] == "src" and len(relative.parts) > 1 and relative.parts[1] == "blackdog"):
        return None
    return target


def _truncate_text(value: str, *, max_chars: int = 6_000) -> str | None:
    value = value.strip()
    if not value:
        return None
    if len(value) <= max_chars:
        return value
    return value[:max_chars] + "\n[truncated]"


def _collect_trace_coverage(profile_root: Path, source_root: Path, *, cover_dir: Path) -> dict[str, dict[str, int | float]]:
    modules: dict[str, dict[str, int | float]] = {}
    for cover_file in sorted(cover_dir.glob("*.cover")):
        source = _coverage_source(profile_root, source_root, cover_file)
        if source is None:
            continue
        covered, total = _parse_coverage_file(cover_file)
        if total <= 0:
            continue
        key = str(source.relative_to(profile_root))
        percent = round((covered / total * 100.0), 2) if total else 0.0
        modules[key] = {"covered": covered, "total": total, "coverage_percent": percent}
    return modules


def _merge_coverage(
    a: dict[str, dict[str, int | float]],
    b: dict[str, dict[str, int | float]],
) -> dict[str, dict[str, int | float]]:
    merged = dict(a)
    for path, payload in b.items():
        if path not in merged:
            merged[path] = payload.copy()
            continue
        merged[path]["covered"] = max(merged[path]["covered"], payload["covered"])
        merged[path]["total"] = max(merged[path]["total"], payload["total"])
        if merged[path]["total"]:
            merged[path]["coverage_percent"] = round((merged[path]["covered"] / merged[path]["total"]) * 100.0, 2)
        else:
            merged[path]["coverage_percent"] = 0.0
    return merged


def _run_coverage_command(command: str, *, project_root: Path, cover_dir: Path) -> dict[str, Any]:
    env_assignments, command_parts = _parse_trace_command(command)
    env = os.environ.copy()
    env.update(env_assignments)
    start = time.perf_counter()
    runner = _build_trace_runner(command_parts, cover_dir=cover_dir)
    completed = subprocess.run(
        runner,
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - start
    coverage = _collect_trace_coverage(project_root, project_root / "src", cover_dir=cover_dir)
    status = "passed" if completed.returncode == 0 else "failed"
    return {
        "command": command,
        "status": status,
        "returncode": completed.returncode,
        "elapsed_seconds": round(elapsed, 3),
        "stdout": _truncate_text(completed.stdout or "", max_chars=6_000),
        "stderr": _truncate_text(completed.stderr or "", max_chars=6_000),
        "coverage": coverage,
    }


def _coverage_summary(modules: dict[str, dict[str, int | float]]) -> dict[str, Any]:
    total = sum(int(payload["total"]) for payload in modules.values())
    covered = sum(int(payload["covered"]) for payload in modules.values())
    percent = round((covered / total * 100.0), 2) if total else 0.0
    return {
        "modules": modules,
        "module_count": len(modules),
        "total_lines": total,
        "covered_lines": covered,
        "coverage_percent": percent,
    }


def cmd_init(args: argparse.Namespace) -> int:
    profile = scaffold_project(
        Path(args.project_root or "."),
        project_name=args.project_name or Path(args.project_root or ".").resolve().name,
        force=args.force,
        objectives=args.objective,
        push_objective=args.push_objective,
        non_negotiables=args.non_negotiable,
        evidence_requirements=args.evidence_requirement,
        release_gates=args.release_gate,
    )
    print(json.dumps({"project_root": str(profile.paths.project_root), "profile": str(profile.paths.profile_file)}, indent=2))
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    profile, skill_file = bootstrap_project(
        Path(args.project_root or "."),
        project_name=args.project_name or Path(args.project_root or ".").resolve().name,
        force=args.force,
        objectives=args.objective,
        push_objective=args.push_objective,
        non_negotiables=args.non_negotiable,
        evidence_requirements=args.evidence_requirement,
        release_gates=args.release_gate,
    )
    print(
        json.dumps(
            {
                "project_root": str(profile.paths.project_root),
                "profile": str(profile.paths.profile_file),
                "skill_file": str(skill_file),
            },
            indent=2,
        )
    )
    return 0


def cmd_create_project(args: argparse.Namespace) -> int:
    profile, skill_file, venv_dir, source_root = create_project(
        Path(args.project_root),
        project_name=args.project_name or Path(args.project_root).resolve().name,
        blackdog_source=Path(args.blackdog_source) if args.blackdog_source else None,
        objectives=args.objective,
        push_objective=args.push_objective,
        non_negotiables=args.non_negotiable,
        evidence_requirements=args.evidence_requirement,
        release_gates=args.release_gate,
    )
    print(
        json.dumps(
            {
                "project_root": str(profile.paths.project_root),
                "profile": str(profile.paths.profile_file),
                "skill_file": str(skill_file),
                "venv": str(venv_dir),
                "blackdog_cli": str(venv_dir / "bin" / "blackdog"),
                "blackdog_skill_cli": str(venv_dir / "bin" / "blackdog-skill"),
                "blackdog_source": str(source_root),
            },
            indent=2,
        )
    )
    return 0


def cmd_backlog_new(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    backlog_dir = scaffold_named_backlog(profile, args.name, force=args.force)
    print(json.dumps({"name": args.name, "backlog_dir": str(backlog_dir)}, indent=2))
    return 0


def cmd_backlog_remove(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    backlog_dir = remove_named_backlog(profile, args.name)
    print(json.dumps({"name": args.name, "removed": str(backlog_dir)}, indent=2))
    return 0


def cmd_backlog_reset(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    backlog_dir = reset_default_backlog(profile, purge_named=args.purge_named)
    print(json.dumps({"backlog_dir": str(backlog_dir), "purge_named": bool(args.purge_named)}, indent=2))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    profile, snapshot, state = _load_runtime(Path(args.project_root) if args.project_root else None)
    payload = {
        "project": profile.project_name,
        "backlog_file": str(profile.paths.backlog_file),
        "state_file": str(profile.paths.state_file),
        "events_file": str(profile.paths.events_file),
        "inbox_file": str(profile.paths.inbox_file),
        "tasks": len(snapshot.tasks),
        "lanes": len(snapshot.plan.get("lanes", [])),
        "epics": len(snapshot.plan.get("epics", [])),
        "claims": sum(1 for entry in state.get("task_claims", {}).values() if isinstance(entry, dict) and claim_is_active(entry)),
        "open_messages": len([row for row in load_inbox(profile.paths) if row.get("status") == "open"]),
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    payload = add_task(
        profile,
        title=args.title,
        bucket=args.bucket,
        priority=args.priority,
        risk=args.risk,
        effort=args.effort,
        why=args.why,
        evidence=args.evidence,
        safe_first_slice=args.safe_first_slice,
        paths=args.path,
        checks=args.check,
        docs=args.doc,
        domains=args.domain,
        packages=args.package,
        affected_paths=args.affected_path,
        task_shaping=_parse_json_object(args.task_shaping, command="add", flag="--task-shaping"),
        objective=args.objective or "",
        requires_approval=args.requires_approval,
        approval_reason=args.approval_reason or "",
        epic_id=args.epic_id,
        epic_title=args.epic_title,
        lane_id=args.lane_id,
        lane_title=args.lane_title,
        wave=args.wave,
    )
    append_event(
        profile.paths,
        event_type="task_added",
        actor=args.actor,
        task_id=str(payload["id"]),
        payload={"title": payload["title"], "bucket": payload["bucket"]},
    )
    _emit_render(profile)
    print(json.dumps(payload, indent=2))
    return 0


def _summary_view(profile, snapshot, state) -> dict[str, Any]:
    return build_view_model(
        profile,
        snapshot,
        state,
        events=load_events(profile.paths, limit=20),
        messages=load_inbox(profile.paths),
        results=load_task_results(profile.paths),
    )


def cmd_summary(args: argparse.Namespace) -> int:
    profile, snapshot, state = _load_runtime(Path(args.project_root) if args.project_root else None)
    view = _summary_view(profile, snapshot, state)
    if args.format == "json":
        print(json.dumps(view, indent=2))
    else:
        print(render_summary_text(view), end="")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    profile, snapshot, state = _load_runtime(Path(args.project_root) if args.project_root else None)
    view = build_plan_view(profile, snapshot, state, allow_high_risk=args.allow_high_risk)
    if args.format == "json":
        print(json.dumps(view, indent=2))
    else:
        print(render_plan_text(view), end="")
    return 0


def cmd_supervise_run(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    payload = run_supervisor(
        profile,
        actor=args.actor,
        task_ids=args.id,
        count=args.count,
        allow_high_risk=args.allow_high_risk,
        force=args.force,
        workspace_mode=None,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    _emit_render(profile)
    print(render_supervisor_output(payload, as_json=args.format == "json"), end="")
    return 0


def cmd_supervise_status(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    payload = build_supervisor_status_view(
        profile,
        actor=args.actor,
        allow_high_risk=args.allow_high_risk,
    )
    print(render_supervisor_status_output(payload, as_json=args.format == "json"), end="")
    return 0


def cmd_supervise_recover(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    payload = build_supervisor_recover_view(profile, actor=args.actor)
    print(render_supervisor_recover_output(payload, as_json=args.format == "json"), end="")
    return 0


def cmd_supervise_report(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    payload = build_supervisor_observation_view(profile, actor=args.actor, run_limit=args.run_limit)
    print(render_supervisor_observation_output(payload, as_json=args.format == "json"), end="")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    profile, snapshot, state = _load_runtime(Path(args.project_root) if args.project_root else None)
    rows = [
        {
            "id": task.id,
            "title": task.title,
            "lane": task.lane_title,
            "wave": task.wave,
            "risk": task.payload["risk"],
        }
        for task in next_runnable_tasks(snapshot, state, allow_high_risk=args.allow_high_risk, limit=args.count)
    ]
    if args.format == "json":
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print(f"{row['id']} [{row['risk']}] {row['title']}")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    print(json.dumps(build_ui_snapshot(profile), indent=2))
    return 0


def cmd_worktree_preflight(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    payload = worktree_preflight(profile, cwd=Path.cwd())
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(render_preflight_text(payload), end="")
    return 0


def cmd_worktree_start(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    spec = start_task_worktree(
        profile,
        task_id=args.id,
        branch=args.branch,
        from_ref=args.from_ref,
        path=args.path,
    )
    append_event(
        profile.paths,
        event_type="worktree_start",
        actor=args.actor,
        task_id=spec.task_id,
        payload=spec.to_dict(),
    )
    if args.format == "json":
        print(json.dumps(spec.to_dict(), indent=2))
    else:
        print(render_start_text(spec), end="")
    return 0


def cmd_worktree_land(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    payload = land_branch(
        profile,
        branch=args.branch,
        target_branch=args.target_branch,
        pull=not args.no_pull,
        cleanup=args.cleanup,
    )
    task_id = args.id or task_id_for_branch(profile, str(payload.get("branch") or ""))
    append_event(
        profile.paths,
        event_type="worktree_land",
        actor=args.actor,
        task_id=task_id,
        payload=payload,
    )
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(render_land_text(payload), end="")
    return 0


def cmd_worktree_cleanup(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    payload = cleanup_task_worktree(
        profile,
        task_id=args.id,
        path=args.path,
        branch=args.branch,
    )
    append_event(
        profile.paths,
        event_type="worktree_cleanup",
        actor=args.actor,
        task_id=args.id,
        payload=payload,
    )
    if args.format == "json":
        print(json.dumps(payload, indent=2))
    else:
        print(render_cleanup_text(payload), end="")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    profile, snapshot, _ = _load_runtime(Path(args.project_root) if args.project_root else None)
    with locked_state(profile.paths.state_file) as state:
        state = sync_state_for_backlog(state, snapshot)
        if args.id:
            selected = []
            for task_id in args.id:
                task = snapshot.tasks.get(task_id)
                if task is None:
                    raise BacklogError(f"Unknown task id: {task_id}")
                blocker = classify_task_status(task, snapshot, state, allow_high_risk=args.allow_high_risk)
                if blocker[0] != "ready" and not args.force:
                    raise BacklogError(f"Task {task_id} is not claimable: {blocker[1]}")
                selected.append(task)
        else:
            selected = next_runnable_tasks(snapshot, state, allow_high_risk=args.allow_high_risk, limit=args.count)
        claimed = []
        for task in selected:
            entry = state.setdefault("task_claims", {}).get(task.id) or {}
            if args.pid is not None and args.pid < 1:
                raise BacklogError("--pid must be a positive integer")
            claim_task_entry(
                entry,
                agent=args.agent,
                title=task.title,
                summary={
                    "bucket": task.payload["bucket"],
                    "paths": task.payload["paths"],
                    "priority": task.payload["priority"],
                    "risk": task.payload["risk"],
                },
                claimed_pid=args.pid,
            )
            state["task_claims"][task.id] = entry
            event_payload: dict[str, Any] = {}
            if isinstance(entry.get("claimed_pid"), int):
                event_payload["claimed_pid"] = entry["claimed_pid"]
            append_event(
                profile.paths,
                event_type="claim",
                actor=args.agent,
                task_id=task.id,
                payload=event_payload,
            )
            row: dict[str, Any] = {"id": task.id, "title": task.title}
            if isinstance(entry.get("claimed_pid"), int):
                row["claimed_pid"] = entry["claimed_pid"]
            claimed.append(row)
    _emit_render(profile)
    print(json.dumps(claimed, indent=2))
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    project_root = _env_required(args.project_root, "BLACKDOG_PROJECT_ROOT", arg_name="project-root", command="release")
    task_id = _env_required(args.id, "BLACKDOG_TASK_ID", arg_name="id", command="release")
    agent = _env_required(args.agent, "BLACKDOG_AGENT_NAME", arg_name="agent", command="release")
    profile, _, _ = _load_runtime(Path(project_root))
    with locked_state(profile.paths.state_file) as state:
        entry = state.setdefault("task_claims", {}).get(task_id) or {}
        if entry.get("claimed_by") and entry.get("claimed_by") != agent and not args.force:
            raise BacklogError(f"Task {task_id} is claimed by {entry.get('claimed_by')}; use --force to override")
        entry["status"] = "released"
        entry["released_by"] = agent
        entry["released_at"] = now_iso()
        if args.note:
            entry["release_note"] = args.note
        entry.pop("claim_expires_at", None)
        entry.pop("claimed_pid", None)
        entry.pop("claimed_process_missing_scans", None)
        entry.pop("claimed_process_last_seen_at", None)
        entry.pop("claimed_process_last_checked_at", None)
        state["task_claims"][task_id] = entry
    append_event(profile.paths, event_type="release", actor=agent, task_id=task_id, payload={"note": args.note or ""})
    _emit_render(profile)
    print(task_id)
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    profile, _, _ = _load_runtime(Path(args.project_root) if args.project_root else None)
    with locked_state(profile.paths.state_file) as state:
        entry = state.setdefault("task_claims", {}).get(args.id) or {}
        owner = entry.get("claimed_by")
        if profile.require_claim_for_completion and owner and owner != args.agent and not args.force:
            raise BacklogError(f"Task {args.id} is claimed by {owner}; use --force to override")
        entry["status"] = "done"
        entry["completed_by"] = args.agent
        entry["completed_at"] = now_iso()
        if args.note:
            entry["completion_note"] = args.note
        entry.pop("claim_expires_at", None)
        entry.pop("claimed_pid", None)
        entry.pop("claimed_process_missing_scans", None)
        entry.pop("claimed_process_last_seen_at", None)
        entry.pop("claimed_process_last_checked_at", None)
        state["task_claims"][args.id] = entry
        approvals = state.setdefault("approval_tasks", {})
        if args.id in approvals and isinstance(approvals[args.id], dict):
            approvals[args.id]["status"] = "done"
    append_event(profile.paths, event_type="complete", actor=args.agent, task_id=args.id, payload={"note": args.note or ""})
    _emit_render(profile)
    print(args.id)
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    profile, snapshot, _ = _load_runtime(Path(args.project_root) if args.project_root else None)
    task = snapshot.tasks.get(args.id)
    if task is None:
        raise BacklogError(f"Unknown task id: {args.id}")
    with locked_state(profile.paths.state_file) as state:
        state = sync_state_for_backlog(state, snapshot)
        approvals = state.setdefault("approval_tasks", {})
        entry = approvals.get(args.id) or {}
        entry["status"] = args.decision
        entry["decided_at"] = now_iso()
        entry["decided_by"] = args.agent
        if args.note:
            entry["decision_note"] = args.note
        approvals[args.id] = entry
    append_event(profile.paths, event_type="decision", actor=args.agent, task_id=args.id, payload={"decision": args.decision, "note": args.note or ""})
    _emit_render(profile)
    print(args.id)
    return 0


def cmd_comment(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    event = record_comment(profile.paths, actor=args.actor, body=args.body, task_id=args.id, kind=args.kind)
    _emit_render(profile)
    print(json.dumps(event, indent=2))
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    rows = load_events(profile.paths, task_id=args.id, limit=args.limit)
    print(json.dumps(rows, indent=2))
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    output = render_project_html(profile)
    append_event(profile.paths, event_type="render", actor=args.actor, payload={"html_file": str(output)})
    print(str(output))
    return 0


def cmd_result_record(args: argparse.Namespace) -> int:
    project_root = _env_required(args.project_root, "BLACKDOG_PROJECT_ROOT", arg_name="project-root", command="result record")
    profile = load_profile(
        Path(project_root)
    )
    task_id = _env_required(args.id, "BLACKDOG_TASK_ID", arg_name="id", command="result record")
    actor = _env_required(args.actor, "BLACKDOG_AGENT_NAME", arg_name="actor", command="result record")
    result_path = record_task_result(
        profile.paths,
        task_id=task_id,
        actor=actor,
        status=args.status,
        what_changed=args.what_changed,
        validation=args.validation,
        residual=args.residual,
        needs_user_input=args.needs_user_input,
        followup_candidates=args.followup,
        run_id=args.run_id,
        task_shaping_telemetry=_parse_json_object(
            args.task_shaping_telemetry,
            command="result record",
            flag="--task-shaping-telemetry",
        ),
    )
    _emit_render(profile)
    print(str(result_path))
    return 0


def cmd_coverage(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    coverage_settings = _load_coverage_profile_settings(profile.paths.project_root)
    default_output = coverage_settings.get("artifact_output")
    output_path = args.output
    if output_path is None and isinstance(default_output, str) and default_output.strip():
        output_path = str(Path(default_output))

    commands = [args.command] if args.command else list(profile.validation_commands)
    runs: list[dict[str, Any]] = []
    merged_modules: dict[str, dict[str, int | float]] = {}
    status = "passed"
    for command in commands:
        with tempfile.TemporaryDirectory(prefix="blackdog-coverage-") as raw_tmp_dir:
            run = _run_coverage_command(
                command,
                project_root=profile.paths.project_root,
                cover_dir=Path(raw_tmp_dir),
            )
        merged_modules = _merge_coverage(merged_modules, run["coverage"])
        runs.append(run)
        if run["status"] != "passed":
            status = "failed"

    summary = _coverage_summary(merged_modules)
    payload = {
        "project_root": str(profile.paths.project_root),
        "profile": str(profile.paths.profile_file),
        "status": status,
        "runs": runs,
        "summary": summary,
    }
    if output_path is not None:
        output = (profile.paths.project_root / Path(output_path)).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        payload["output"] = str(output)
    print(json.dumps(payload, indent=2))
    return 0 if status == "passed" else 1


def cmd_tune(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    payload, created = seed_tune_task(profile)
    if created:
        append_event(
            profile.paths,
            event_type="task_added",
            actor=args.actor,
            task_id=payload["id"],
            payload={"title": payload["title"], "bucket": payload["bucket"]},
        )
    _emit_render(profile)
    print(json.dumps(payload, indent=2))
    return 0


def cmd_inbox_send(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    message = send_message(
        profile.paths,
        sender=args.sender,
        recipient=args.recipient,
        body=args.body,
        kind=args.kind,
        task_id=args.id,
        reply_to=args.reply_to,
        tags=args.tag,
    )
    _emit_render(profile)
    print(json.dumps(message, indent=2))
    return 0


def cmd_inbox_list(args: argparse.Namespace) -> int:
    project_root = _env_default(args.project_root, "BLACKDOG_PROJECT_ROOT")
    profile = load_profile(Path(project_root) if project_root else None)
    recipient = _env_default(args.recipient, "BLACKDOG_AGENT_NAME")
    rows = load_inbox(profile.paths, recipient=recipient, status=args.status, task_id=args.id)
    print(json.dumps(rows, indent=2))
    return 0


def cmd_inbox_resolve(args: argparse.Namespace) -> int:
    profile = load_profile(Path(args.project_root) if args.project_root else None)
    row = resolve_message(profile.paths, message_id=args.message_id, actor=args.actor, note=args.note or "")
    _emit_render(profile)
    print(json.dumps(row, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Blackdog CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_create_project = subparsers.add_parser(
        "create-project",
        help="Create a new git repo, install Blackdog into a repo-local .VE, and bootstrap the project scaffold",
    )
    p_create_project.add_argument("--project-root", required=True)
    p_create_project.add_argument("--project-name", default=None)
    p_create_project.add_argument("--blackdog-source", default=None)
    p_create_project.add_argument("--objective", action="append", default=[])
    p_create_project.add_argument("--push-objective", action="append", default=[])
    p_create_project.add_argument("--non-negotiable", action="append", default=[])
    p_create_project.add_argument("--evidence-requirement", action="append", default=[])
    p_create_project.add_argument("--release-gate", action="append", default=[])
    p_create_project.set_defaults(func=cmd_create_project)

    p_bootstrap = subparsers.add_parser("bootstrap", help="Initialize backlog artifacts and generate the project-local Blackdog skill")
    p_bootstrap.add_argument("--project-root", default=".")
    p_bootstrap.add_argument("--project-name", default=None)
    p_bootstrap.add_argument("--force", action="store_true")
    p_bootstrap.add_argument("--objective", action="append", default=[])
    p_bootstrap.add_argument("--push-objective", action="append", default=[])
    p_bootstrap.add_argument("--non-negotiable", action="append", default=[])
    p_bootstrap.add_argument("--evidence-requirement", action="append", default=[])
    p_bootstrap.add_argument("--release-gate", action="append", default=[])
    p_bootstrap.set_defaults(func=cmd_bootstrap)

    p_init = subparsers.add_parser("init", help="Initialize repo-local Blackdog files without generating a project skill")
    p_init.add_argument("--project-root", default=".")
    p_init.add_argument("--project-name", default=None)
    p_init.add_argument("--force", action="store_true")
    p_init.add_argument("--objective", action="append", default=[])
    p_init.add_argument("--push-objective", action="append", default=[])
    p_init.add_argument("--non-negotiable", action="append", default=[])
    p_init.add_argument("--evidence-requirement", action="append", default=[])
    p_init.add_argument("--release-gate", action="append", default=[])
    p_init.set_defaults(func=cmd_init)

    p_backlog = subparsers.add_parser("backlog", help="Manage default and named backlog artifact sets")
    backlog_subparsers = p_backlog.add_subparsers(dest="backlog_command", required=True)
    p_backlog_new = backlog_subparsers.add_parser("new", help="Create a named backlog artifact set under the control root")
    p_backlog_new.add_argument("--project-root", default=None)
    p_backlog_new.add_argument("name")
    p_backlog_new.add_argument("--force", action="store_true")
    p_backlog_new.set_defaults(func=cmd_backlog_new)
    p_backlog_remove = backlog_subparsers.add_parser("remove", help="Delete a named backlog artifact set from the control root")
    p_backlog_remove.add_argument("--project-root", default=None)
    p_backlog_remove.add_argument("name")
    p_backlog_remove.set_defaults(func=cmd_backlog_remove)
    p_backlog_reset = backlog_subparsers.add_parser("reset", help="Rebuild the default backlog and runtime state from scratch")
    p_backlog_reset.add_argument("--project-root", default=None)
    p_backlog_reset.add_argument("--purge-named", action="store_true")
    p_backlog_reset.set_defaults(func=cmd_backlog_reset)

    p_validate = subparsers.add_parser("validate", help="Validate profile, backlog, state, inbox, and events")
    p_validate.add_argument("--project-root", default=None)
    p_validate.set_defaults(func=cmd_validate)

    p_add = subparsers.add_parser("add", help="Add a backlog task")
    p_add.add_argument("--project-root", default=None)
    p_add.add_argument("--actor", default="blackdog")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--bucket", required=True)
    p_add.add_argument("--priority", choices=sorted({"P1", "P2", "P3"}), default="P2")
    p_add.add_argument("--risk", choices=sorted({"low", "medium", "high"}), default="medium")
    p_add.add_argument("--effort", choices=sorted({"S", "M", "L"}), default="M")
    p_add.add_argument("--why", required=True)
    p_add.add_argument("--evidence", required=True)
    p_add.add_argument("--safe-first-slice", required=True)
    p_add.add_argument("--path", action="append", default=[])
    p_add.add_argument("--affected-path", action="append", default=[])
    p_add.add_argument("--task-shaping", default=None)
    p_add.add_argument("--check", action="append", default=[])
    p_add.add_argument("--doc", action="append", default=[])
    p_add.add_argument("--domain", action="append", default=[])
    p_add.add_argument("--package", action="append", default=[])
    p_add.add_argument("--objective", default="")
    p_add.add_argument("--requires-approval", action="store_true")
    p_add.add_argument("--approval-reason", default="")
    p_add.add_argument("--epic-id", default=None)
    p_add.add_argument("--epic-title", default=None)
    p_add.add_argument("--lane-id", default=None)
    p_add.add_argument("--lane-title", default=None)
    p_add.add_argument("--wave", type=int, default=None)
    p_add.set_defaults(func=cmd_add)

    p_summary = subparsers.add_parser("summary", help="Summarize backlog state")
    p_summary.add_argument("--project-root", default=None)
    p_summary.add_argument("--format", choices=("text", "json"), default="text")
    p_summary.set_defaults(func=cmd_summary)

    p_plan = subparsers.add_parser("plan", help="Show epics, lanes, and waves from the backlog plan")
    p_plan.add_argument("--project-root", default=None)
    p_plan.add_argument("--allow-high-risk", action="store_true")
    p_plan.add_argument("--format", choices=("text", "json"), default="text")
    p_plan.set_defaults(func=cmd_plan)

    p_next = subparsers.add_parser("next", help="Show next runnable tasks")
    p_next.add_argument("--project-root", default=None)
    p_next.add_argument("--count", type=int, default=4)
    p_next.add_argument("--allow-high-risk", action="store_true")
    p_next.add_argument("--format", choices=("text", "json"), default="text")
    p_next.set_defaults(func=cmd_next)

    p_snapshot = subparsers.add_parser("snapshot", help="Print the canonical static-HTML snapshot contract")
    p_snapshot.add_argument("--project-root", default=None)
    p_snapshot.set_defaults(func=cmd_snapshot)

    p_tune = subparsers.add_parser("tune", help="Seed a self-tuning backlog analysis task")
    p_tune.add_argument("--project-root", default=None)
    p_tune.add_argument("--actor", default="blackdog")
    p_tune.set_defaults(func=cmd_tune)

    p_worktree = subparsers.add_parser("worktree", help="Branch-backed worktree lifecycle for implementation tasks")
    worktree_subparsers = p_worktree.add_subparsers(dest="worktree_command", required=True)
    p_worktree_preflight = worktree_subparsers.add_parser("preflight", help="Show current worktree/branch/backing model details")
    p_worktree_preflight.add_argument("--project-root", default=None)
    p_worktree_preflight.add_argument("--format", choices=("text", "json"), default="text")
    p_worktree_preflight.set_defaults(func=cmd_worktree_preflight)
    p_worktree_start = worktree_subparsers.add_parser("start", help="Create a branch-backed task worktree from the primary worktree")
    p_worktree_start.add_argument("--project-root", default=None)
    p_worktree_start.add_argument("--actor", default="blackdog")
    p_worktree_start.add_argument("--id", required=True)
    p_worktree_start.add_argument("--branch", default=None)
    p_worktree_start.add_argument("--from", dest="from_ref", default=None)
    p_worktree_start.add_argument("--path", default=None)
    p_worktree_start.add_argument("--format", choices=("text", "json"), default="text")
    p_worktree_start.set_defaults(func=cmd_worktree_start)
    p_worktree_land = worktree_subparsers.add_parser("land", help="Fast-forward a task branch into the target branch")
    p_worktree_land.add_argument("--project-root", default=None)
    p_worktree_land.add_argument("--actor", default="blackdog")
    p_worktree_land.add_argument("--id", default=None)
    p_worktree_land.add_argument("--branch", default=None)
    p_worktree_land.add_argument("--into", dest="target_branch", default=None)
    p_worktree_land.add_argument("--no-pull", action="store_true")
    p_worktree_land.add_argument("--cleanup", action="store_true")
    p_worktree_land.add_argument("--format", choices=("text", "json"), default="text")
    p_worktree_land.set_defaults(func=cmd_worktree_land)
    p_worktree_cleanup = worktree_subparsers.add_parser("cleanup", help="Remove a landed task worktree and optionally delete its branch")
    p_worktree_cleanup.add_argument("--project-root", default=None)
    p_worktree_cleanup.add_argument("--actor", default="blackdog")
    p_worktree_cleanup.add_argument("--id", default=None)
    p_worktree_cleanup.add_argument("--path", default=None)
    p_worktree_cleanup.add_argument("--branch", default=None)
    p_worktree_cleanup.add_argument("--format", choices=("text", "json"), default="text")
    p_worktree_cleanup.set_defaults(func=cmd_worktree_cleanup)

    p_supervise = subparsers.add_parser("supervise", help="Launch child agents against runnable backlog tasks")
    supervise_subparsers = p_supervise.add_subparsers(dest="supervise_command", required=True)
    p_supervise_run = supervise_subparsers.add_parser("run", help="Drain runnable work with one supervisor run")
    p_supervise_run.add_argument("--project-root", default=None)
    p_supervise_run.add_argument("--actor", default="supervisor")
    p_supervise_run.add_argument("--id", action="append", default=[])
    p_supervise_run.add_argument("--count", type=int, default=0)
    p_supervise_run.add_argument("--allow-high-risk", action="store_true")
    p_supervise_run.add_argument("--force", action="store_true")
    p_supervise_run.add_argument("--poll-interval-seconds", type=float, default=1.0)
    p_supervise_run.add_argument("--format", choices=("text", "json"), default="text")
    p_supervise_run.set_defaults(func=cmd_supervise_run)
    p_supervise_status = supervise_subparsers.add_parser("status", help="Report latest run state, open controls, ready tasks, and recent child results")
    p_supervise_status.add_argument("--project-root", default=None)
    p_supervise_status.add_argument("--actor", default="supervisor")
    p_supervise_status.add_argument("--allow-high-risk", action="store_true")
    p_supervise_status.add_argument("--format", choices=("text", "json"), default="text")
    p_supervise_status.set_defaults(func=cmd_supervise_status)
    p_supervise_recover = supervise_subparsers.add_parser("recover", help="Report interrupt/blocked/partial cases and suggested recovery actions")
    p_supervise_recover.add_argument("--project-root", default=None)
    p_supervise_recover.add_argument("--actor", default="supervisor")
    p_supervise_recover.add_argument("--format", choices=("text", "json"), default="text")
    p_supervise_recover.set_defaults(func=cmd_supervise_recover)
    p_supervise_report = supervise_subparsers.add_parser("report", help="Show aggregated startup/retry/output-shape/landing observations")
    p_supervise_report.add_argument("--project-root", default=None)
    p_supervise_report.add_argument("--actor", default="supervisor")
    p_supervise_report.add_argument("--run-limit", type=int, default=0)
    p_supervise_report.add_argument("--format", choices=("text", "json"), default="text")
    p_supervise_report.set_defaults(func=cmd_supervise_report)

    p_claim = subparsers.add_parser("claim", help="Claim tasks for an agent")
    p_claim.add_argument("--project-root", default=None)
    p_claim.add_argument("--agent", required=True)
    p_claim.add_argument("--id", action="append", default=[])
    p_claim.add_argument("--count", type=int, default=1)
    p_claim.add_argument("--pid", type=int, default=None)
    p_claim.add_argument("--allow-high-risk", action="store_true")
    p_claim.add_argument("--force", action="store_true")
    p_claim.set_defaults(func=cmd_claim)

    p_release = subparsers.add_parser("release", help="Release a claimed task")
    p_release.add_argument("--project-root", default=None)
    p_release.add_argument("--id", default=None)
    p_release.add_argument("--agent", default=None)
    p_release.add_argument("--note", default="")
    p_release.add_argument("--force", action="store_true")
    p_release.set_defaults(func=cmd_release)

    p_complete = subparsers.add_parser("complete", help="Mark a task complete")
    p_complete.add_argument("--project-root", default=None)
    p_complete.add_argument("--id", required=True)
    p_complete.add_argument("--agent", required=True)
    p_complete.add_argument("--note", default="")
    p_complete.add_argument("--force", action="store_true")
    p_complete.set_defaults(func=cmd_complete)

    p_decide = subparsers.add_parser("decide", help="Record an approval decision")
    p_decide.add_argument("--project-root", default=None)
    p_decide.add_argument("--id", required=True)
    p_decide.add_argument("--agent", required=True)
    p_decide.add_argument("--decision", choices=("pending", "approved", "denied", "deferred", "done"), required=True)
    p_decide.add_argument("--note", default="")
    p_decide.set_defaults(func=cmd_decide)

    p_comment = subparsers.add_parser("comment", help="Append a task or project comment to the event log")
    p_comment.add_argument("--project-root", default=None)
    p_comment.add_argument("--actor", required=True)
    p_comment.add_argument("--id", default=None)
    p_comment.add_argument("--kind", default="comment")
    p_comment.add_argument("--body", required=True)
    p_comment.set_defaults(func=cmd_comment)

    p_events = subparsers.add_parser("events", help="List recent event-log rows")
    p_events.add_argument("--project-root", default=None)
    p_events.add_argument("--id", default=None)
    p_events.add_argument("--limit", type=int, default=20)
    p_events.set_defaults(func=cmd_events)

    p_render = subparsers.add_parser("render", help="Render the static backlog HTML page")
    p_render.add_argument("--project-root", default=None)
    p_render.add_argument("--actor", default="blackdog")
    p_render.set_defaults(func=cmd_render)

    p_result = subparsers.add_parser("result", help="Record a structured task result")
    result_subparsers = p_result.add_subparsers(dest="result_command", required=True)
    p_result_record = result_subparsers.add_parser("record", help="Write a task-result JSON file")
    p_result_record.add_argument("--project-root", default=None)
    p_result_record.add_argument("--id", default=None)
    p_result_record.add_argument("--actor", default=None)
    p_result_record.add_argument("--status", required=True)
    p_result_record.add_argument("--run-id", default=None)
    p_result_record.add_argument("--what-changed", action="append", default=[])
    p_result_record.add_argument("--validation", action="append", default=[])
    p_result_record.add_argument("--residual", action="append", default=[])
    p_result_record.add_argument("--followup", action="append", default=[])
    p_result_record.add_argument("--task-shaping-telemetry", default=None)
    p_result_record.add_argument("--needs-user-input", action="store_true")
    p_result_record.set_defaults(func=cmd_result_record)

    p_coverage = subparsers.add_parser("coverage", help="Run validation checks and emit coverage report")
    p_coverage.add_argument("--project-root", default=None)
    p_coverage.add_argument("--command", default=None)
    p_coverage.add_argument("--output", default=None)
    p_coverage.set_defaults(func=cmd_coverage)

    p_inbox = subparsers.add_parser("inbox", help="Inbox messaging for supervisor and child agents")
    inbox_subparsers = p_inbox.add_subparsers(dest="inbox_command", required=True)
    p_inbox_send = inbox_subparsers.add_parser("send", help="Send an inbox message")
    p_inbox_send.add_argument("--project-root", default=None)
    p_inbox_send.add_argument("--sender", required=True)
    p_inbox_send.add_argument("--recipient", required=True)
    p_inbox_send.add_argument("--id", default=None)
    p_inbox_send.add_argument("--kind", default="instruction")
    p_inbox_send.add_argument("--reply-to", default=None)
    p_inbox_send.add_argument("--tag", action="append", default=[])
    p_inbox_send.add_argument("--body", required=True)
    p_inbox_send.set_defaults(func=cmd_inbox_send)

    p_inbox_list = inbox_subparsers.add_parser("list", help="List inbox messages")
    p_inbox_list.add_argument("--project-root", default=None)
    p_inbox_list.add_argument("--recipient", default=None)
    p_inbox_list.add_argument("--status", default=None)
    p_inbox_list.add_argument("--id", default=None)
    p_inbox_list.set_defaults(func=cmd_inbox_list)

    p_inbox_resolve = inbox_subparsers.add_parser("resolve", help="Resolve an inbox message")
    p_inbox_resolve.add_argument("--project-root", default=None)
    p_inbox_resolve.add_argument("--message-id", required=True)
    p_inbox_resolve.add_argument("--actor", required=True)
    p_inbox_resolve.add_argument("--note", default="")
    p_inbox_resolve.set_defaults(func=cmd_inbox_resolve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (BacklogError, ConfigError, ScaffoldError, StoreError, SupervisorError, UIError, WorktreeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
