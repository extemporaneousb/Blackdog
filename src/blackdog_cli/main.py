from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from blackdog.wtam import (
    WorktreeError,
    cleanup_task_worktree,
    land_task_worktree,
    render_cleanup_text,
    render_land_text,
    render_preflight_text,
    render_start_text,
    start_task_worktree,
    worktree_preflight,
)
from blackdog_core.backlog import BacklogError, upsert_workset, workset_to_payload
from blackdog_core.profile import ConfigError, load_profile, write_default_profile
from blackdog_core.snapshot import build_runtime_snapshot, build_runtime_summary, load_runtime_model, render_next_text, render_summary_text
from blackdog_core.state import StoreError, VALIDATION_STATUSES, ValidationRecord


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _load_json_payload(*, raw_json: str | None, file_path: str | None) -> dict[str, Any]:
    if raw_json is None and file_path is None:
        raise BacklogError("workset put requires either --json or --file")
    if raw_json is not None and file_path is not None:
        raise BacklogError("workset put accepts only one of --json or --file")
    if raw_json is not None:
        text = raw_json
    else:
        candidate = Path(file_path or "")
        text = sys.stdin.read() if file_path == "-" else candidate.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BacklogError(f"workset put requires valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise BacklogError("workset put requires a JSON object payload")
    return payload


def _load_text_input(*, label: str, raw_text: str | None, file_path: str | None) -> str:
    if raw_text is None and file_path is None:
        raise BacklogError(f"{label} requires --prompt or --prompt-file")
    if raw_text is not None and file_path is not None:
        raise BacklogError(f"{label} accepts only one prompt source")
    if raw_text is not None:
        text = raw_text
    else:
        candidate = Path(file_path or "")
        text = sys.stdin.read() if file_path == "-" else candidate.read_text(encoding="utf-8")
    normalized = str(text).strip()
    if not normalized:
        raise BacklogError(f"{label} text is required")
    return normalized


def _load_model(project_root: str | None):
    profile = load_profile(Path(project_root).resolve() if project_root else None)
    return profile, load_runtime_model(profile)


def _parse_validation_flags(values: list[str]) -> tuple[ValidationRecord, ...]:
    rows: list[ValidationRecord] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        if "=" not in text:
            raise BacklogError("validation rows must use NAME=STATUS")
        name, status = text.split("=", 1)
        name = name.strip()
        status = status.strip()
        if not name or not status:
            raise BacklogError("validation rows must use NAME=STATUS")
        if status not in VALIDATION_STATUSES:
            raise BacklogError(f"validation status must be one of {', '.join(sorted(VALIDATION_STATUSES))}")
        rows.append(ValidationRecord(name=name, status=status))
    return tuple(rows)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blackdog")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_init = subparsers.add_parser("init", help="Write a default vNext Blackdog profile")
    p_init.add_argument("--project-root", default=".")
    p_init.add_argument("--project-name", required=True)

    p_summary = subparsers.add_parser("summary", help="Summarize vNext workset and task runtime state")
    p_summary.add_argument("--project-root", default=".")
    p_summary.add_argument("--json", action="store_true")

    p_snapshot = subparsers.add_parser("snapshot", help="Emit the machine-readable vNext runtime snapshot")
    p_snapshot.add_argument("--project-root", default=".")

    p_next = subparsers.add_parser("next", help="Show ready tasks from the current vNext worksets")
    p_next.add_argument("--project-root", default=".")
    p_next.add_argument("--json", action="store_true")

    p_workset = subparsers.add_parser("workset", help="Create or update vNext workset planning state")
    workset_subparsers = p_workset.add_subparsers(dest="workset_command", required=True)
    p_workset_put = workset_subparsers.add_parser("put", help="Upsert one workset and optional task runtime rows")
    p_workset_put.add_argument("--project-root", default=".")
    p_workset_put.add_argument("--json")
    p_workset_put.add_argument("--file")

    p_worktree = subparsers.add_parser("worktree", help="WTAM branch-backed implementation workflow")
    worktree_subparsers = p_worktree.add_subparsers(dest="worktree_command", required=True)

    p_worktree_preflight = worktree_subparsers.add_parser("preflight", help="Show the current WTAM worktree contract")
    p_worktree_preflight.add_argument("--project-root", default=".")
    p_worktree_preflight.add_argument("--json", action="store_true")

    p_worktree_start = worktree_subparsers.add_parser("start", help="Create a task worktree and start the WTAM attempt")
    p_worktree_start.add_argument("--project-root", default=".")
    p_worktree_start.add_argument("--workset", required=True)
    p_worktree_start.add_argument("--task", required=True)
    p_worktree_start.add_argument("--actor", required=True)
    prompt_group = p_worktree_start.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt")
    prompt_group.add_argument("--prompt-file")
    p_worktree_start.add_argument("--branch")
    p_worktree_start.add_argument("--from", dest="from_ref")
    p_worktree_start.add_argument("--path")
    p_worktree_start.add_argument("--model")
    p_worktree_start.add_argument("--reasoning-effort")
    p_worktree_start.add_argument("--note")
    p_worktree_start.add_argument("--json", action="store_true")

    p_worktree_land = worktree_subparsers.add_parser("land", help="Land the active WTAM task branch and record success")
    p_worktree_land.add_argument("--project-root", default=".")
    p_worktree_land.add_argument("--workset", required=True)
    p_worktree_land.add_argument("--task", required=True)
    p_worktree_land.add_argument("--actor", required=True)
    p_worktree_land.add_argument("--summary")
    p_worktree_land.add_argument("--validation", action="append", default=[])
    p_worktree_land.add_argument("--residual", action="append", default=[])
    p_worktree_land.add_argument("--followup", action="append", default=[])
    p_worktree_land.add_argument("--note")
    p_worktree_land.add_argument("--json", action="store_true")

    p_worktree_cleanup = worktree_subparsers.add_parser("cleanup", help="Remove the landed WTAM worktree and delete its branch")
    p_worktree_cleanup.add_argument("--project-root", default=".")
    p_worktree_cleanup.add_argument("--workset", required=True)
    p_worktree_cleanup.add_argument("--task", required=True)
    p_worktree_cleanup.add_argument("--path")
    p_worktree_cleanup.add_argument("--branch")
    p_worktree_cleanup.add_argument("--json", action="store_true")

    p_analysis = subparsers.add_parser("analysis", help="Separate analysis-only workflow (deferred in vNext)")
    p_analysis.add_argument("--project-root", default=".")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            profile_path = write_default_profile(Path(args.project_root), args.project_name)
            _emit_json(
                {
                    "project_root": str(Path(args.project_root).resolve()),
                    "profile": str(profile_path),
                }
            )
            return 0

        if args.command == "summary":
            profile, model = _load_model(args.project_root)
            if args.json:
                _emit_json(build_runtime_summary(profile))
            else:
                print(render_summary_text(model))
            return 0

        if args.command == "snapshot":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            _emit_json(build_runtime_snapshot(profile))
            return 0

        if args.command == "next":
            _, model = _load_model(args.project_root)
            if args.json:
                _emit_json(
                    [
                        {
                            "task_id": task.task_id,
                            "title": task.title,
                            "intent": task.intent,
                        }
                        for task in model.next_tasks
                    ]
                )
            else:
                print(render_next_text(model))
            return 0

        if args.command == "workset" and args.workset_command == "put":
            payload = _load_json_payload(raw_json=args.json, file_path=args.file)
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            workset = upsert_workset(profile, payload)
            _emit_json({"workset": workset_to_payload(workset)})
            return 0

        if args.command == "worktree" and args.worktree_command == "preflight":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            payload = worktree_preflight(profile, cwd=profile.paths.project_root)
            if args.json:
                _emit_json(payload)
            else:
                print(render_preflight_text(payload), end="")
            return 0

        if args.command == "worktree" and args.worktree_command == "start":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            prompt_text = _load_text_input(
                label="worktree start prompt",
                raw_text=args.prompt,
                file_path=args.prompt_file,
            )
            spec = start_task_worktree(
                profile,
                workset_id=args.workset,
                task_id=args.task,
                actor=args.actor,
                prompt=prompt_text,
                branch=args.branch,
                from_ref=args.from_ref,
                path=args.path,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                note=args.note,
            )
            if args.json:
                _emit_json({"worktree": spec.to_dict()})
            else:
                print(render_start_text(spec), end="")
            return 0

        if args.command == "worktree" and args.worktree_command == "land":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            payload = land_task_worktree(
                profile,
                workset_id=args.workset,
                task_id=args.task,
                actor=args.actor,
                summary=args.summary,
                validations=_parse_validation_flags(args.validation),
                residuals=tuple(args.residual),
                followup_candidates=tuple(args.followup),
                note=args.note,
            )
            if args.json:
                _emit_json({"landing": payload})
            else:
                print(render_land_text(payload), end="")
            return 0

        if args.command == "worktree" and args.worktree_command == "cleanup":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            payload = cleanup_task_worktree(
                profile,
                workset_id=args.workset,
                task_id=args.task,
                path=args.path,
                branch=args.branch,
            )
            if args.json:
                _emit_json({"cleanup": payload})
            else:
                print(render_cleanup_text(payload), end="")
            return 0

        if args.command == "analysis":
            raise BacklogError("analysis workflow is intentionally separate and has not been rebuilt in vNext yet")

        raise BacklogError(f"Unsupported command: {args.command}")
    except (BacklogError, ConfigError, StoreError, WorktreeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
