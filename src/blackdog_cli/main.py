from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from blackdog.handlers import HandlerError
from blackdog.prompting import preview_prompt, render_prompt_preview_text, tune_prompt
from blackdog.repo_lifecycle import (
    RepoLifecycleError,
    install_repo,
    refresh_repo,
    render_repo_lifecycle_text,
    update_repo,
)
from blackdog.wtam import (
    WorktreeError,
    begin_task_worktree,
    cleanup_task_worktree,
    close_task,
    close_task_worktree,
    inspect_task_worktree,
    land_task,
    land_task_worktree,
    render_task_begin_text,
    render_cleanup_text,
    render_close_text,
    render_land_text,
    render_preflight_text,
    render_preview_text,
    render_show_text,
    render_start_text,
    show_task,
    preview_task_worktree,
    start_task_worktree,
    worktree_preflight,
)
from blackdog_core.backlog import BacklogError, upsert_workset, workset_to_payload
from blackdog_core.profile import ConfigError, load_profile, write_default_profile
from blackdog_core.runtime_model import scope_runtime_model
from blackdog_core.snapshot import (
    build_attempts_summary,
    build_attempts_table,
    build_next_payload,
    build_runtime_snapshot,
    build_runtime_summary,
    load_runtime_model,
    render_attempts_summary_text,
    render_attempts_table_text,
    render_next_text,
    render_summary_text,
)
from blackdog_core.state import PROMPT_MODES, StoreError, VALIDATION_STATUSES, ValidationRecord


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


def _load_text_input(*, label: str, raw_text: str | None, file_path: str | None) -> tuple[str, str]:
    if raw_text is None and file_path is None:
        raise BacklogError(f"{label} requires --prompt or --prompt-file")
    if raw_text is not None and file_path is not None:
        raise BacklogError(f"{label} accepts only one prompt source")
    if raw_text is not None:
        text = raw_text
        source = "inline:--prompt"
    else:
        candidate = Path(file_path or "")
        text = sys.stdin.read() if file_path == "-" else candidate.read_text(encoding="utf-8")
        source = "stdin" if file_path == "-" else str(candidate.resolve())
    normalized = str(text).strip()
    if not normalized:
        raise BacklogError(f"{label} text is required")
    return normalized, source


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
    p_summary.add_argument("--workset")
    p_summary.add_argument("--json", action="store_true")

    p_snapshot = subparsers.add_parser("snapshot", help="Emit the machine-readable vNext runtime snapshot")
    p_snapshot.add_argument("--project-root", default=".")
    p_snapshot.add_argument("--workset")

    p_next = subparsers.add_parser("next", help="Select the next task within one workset")
    p_next.add_argument("--project-root", default=".")
    p_next.add_argument("--workset", required=True)
    p_next.add_argument("--json", action="store_true")

    p_prompt = subparsers.add_parser("prompt", help="Preview or tune prompt composition against the repo contract")
    prompt_subparsers = p_prompt.add_subparsers(dest="prompt_command", required=True)

    p_prompt_preview = prompt_subparsers.add_parser("preview", help="Show repo-contract prompt composition without starting execution")
    p_prompt_preview.add_argument("--project-root", default=".")
    preview_input_group = p_prompt_preview.add_mutually_exclusive_group(required=True)
    preview_input_group.add_argument("--prompt")
    preview_input_group.add_argument("--prompt-file")
    p_prompt_preview.add_argument("--show-prompt", action="store_true")
    p_prompt_preview.add_argument("--expand-skill-text", action="store_true")
    p_prompt_preview.add_argument("--expand-contract", action="store_true")
    p_prompt_preview.add_argument("--json", action="store_true")

    p_prompt_tune = prompt_subparsers.add_parser("tune", help="Rewrite a request into a repo-contract-aware prompt")
    p_prompt_tune.add_argument("--project-root", default=".")
    tune_input_group = p_prompt_tune.add_mutually_exclusive_group(required=True)
    tune_input_group.add_argument("--prompt")
    tune_input_group.add_argument("--prompt-file")
    p_prompt_tune.add_argument("--expand-skill-text", action="store_true")
    p_prompt_tune.add_argument("--expand-contract", action="store_true")
    p_prompt_tune.add_argument("--json", action="store_true")

    p_attempts = subparsers.add_parser("attempts", help="Inspect completed attempt history")
    attempts_subparsers = p_attempts.add_subparsers(dest="attempts_command", required=True)

    p_attempts_summary = attempts_subparsers.add_parser("summary", help="Summarize completed attempts")
    p_attempts_summary.add_argument("--project-root", default=".")
    p_attempts_summary.add_argument("--workset")
    p_attempts_summary.add_argument("--json", action="store_true")

    p_attempts_table = attempts_subparsers.add_parser("table", help="Emit a stable table over completed attempts")
    p_attempts_table.add_argument("--project-root", default=".")
    p_attempts_table.add_argument("--workset")
    p_attempts_table.add_argument("--json", action="store_true")

    p_repo = subparsers.add_parser("repo", help="Manage repo-local Blackdog install and contract surfaces")
    repo_subparsers = p_repo.add_subparsers(dest="repo_command", required=True)

    p_repo_install = repo_subparsers.add_parser("install", help="Install or repair repo-local Blackdog runtime handlers")
    p_repo_install.add_argument("--project-root", default=".")
    p_repo_install.add_argument("--project-name")
    p_repo_install.add_argument("--source-root")
    p_repo_install.add_argument("--json", action="store_true")

    p_repo_update = repo_subparsers.add_parser("update", help="Refresh the repo-local Blackdog launcher from a source checkout")
    p_repo_update.add_argument("--project-root", default=".")
    p_repo_update.add_argument("--source-root")
    p_repo_update.add_argument("--json", action="store_true")

    p_repo_refresh = repo_subparsers.add_parser("refresh", help="Regenerate repo-local managed contract surfaces")
    p_repo_refresh.add_argument("--project-root", default=".")
    p_repo_refresh.add_argument("--json", action="store_true")

    p_workset = subparsers.add_parser("workset", help="Create or update vNext workset planning state")
    workset_subparsers = p_workset.add_subparsers(dest="workset_command", required=True)
    p_workset_put = workset_subparsers.add_parser("put", help="Upsert one workset and optional task runtime rows")
    p_workset_put.add_argument("--project-root", default=".")
    p_workset_put.add_argument("--json")
    p_workset_put.add_argument("--file")

    p_task = subparsers.add_parser("task", help="Composed single-agent task workflow")
    task_subparsers = p_task.add_subparsers(dest="task_command", required=True)

    p_task_begin = task_subparsers.add_parser(
        "begin",
        help="Create or reuse a task envelope and start the WTAM attempt",
    )
    p_task_begin.add_argument("--project-root", default=".")
    p_task_begin.add_argument("--actor", required=True)
    task_begin_prompt_group = p_task_begin.add_mutually_exclusive_group(required=True)
    task_begin_prompt_group.add_argument("--prompt")
    task_begin_prompt_group.add_argument("--prompt-file")
    p_task_begin.add_argument("--prompt-mode", choices=sorted(PROMPT_MODES), default="raw")
    p_task_begin.add_argument("--workset")
    p_task_begin.add_argument("--task")
    p_task_begin.add_argument("--title")
    p_task_begin.add_argument("--branch")
    p_task_begin.add_argument("--from", dest="from_ref")
    p_task_begin.add_argument("--path")
    p_task_begin.add_argument("--model")
    p_task_begin.add_argument("--reasoning-effort")
    p_task_begin.add_argument("--note")
    p_task_begin.add_argument("--show-prompt", action="store_true")
    p_task_begin.add_argument("--json", action="store_true")

    p_task_show = task_subparsers.add_parser("show", help="Inspect the current or latest task for this worktree")
    p_task_show.add_argument("--project-root", default=".")
    p_task_show.add_argument("--workset")
    p_task_show.add_argument("--task")
    p_task_show.add_argument("--json", action="store_true")

    p_task_land = task_subparsers.add_parser("land", help="Land the current task and close it")
    p_task_land.add_argument("--project-root", default=".")
    p_task_land.add_argument("--workset")
    p_task_land.add_argument("--task")
    p_task_land.add_argument("--actor")
    p_task_land.add_argument("--summary", required=True)
    p_task_land.add_argument("--validation", action="append", default=[])
    p_task_land.add_argument("--residual", action="append", default=[])
    p_task_land.add_argument("--followup", action="append", default=[])
    p_task_land.add_argument("--note")
    p_task_land.add_argument("--keep-worktree", action="store_true")
    p_task_land.add_argument("--json", action="store_true")

    p_task_close = task_subparsers.add_parser("close", help="Close the current task without landing code")
    p_task_close.add_argument("--project-root", default=".")
    p_task_close.add_argument("--workset")
    p_task_close.add_argument("--task")
    p_task_close.add_argument("--actor")
    p_task_close.add_argument("--status", required=True, choices=["blocked", "failed", "abandoned"])
    p_task_close.add_argument("--summary", required=True)
    p_task_close.add_argument("--validation", action="append", default=[])
    p_task_close.add_argument("--residual", action="append", default=[])
    p_task_close.add_argument("--followup", action="append", default=[])
    p_task_close.add_argument("--note")
    p_task_close.add_argument("--cleanup", action="store_true")
    p_task_close.add_argument("--json", action="store_true")

    p_worktree = subparsers.add_parser("worktree", help="WTAM branch-backed implementation workflow")
    worktree_subparsers = p_worktree.add_subparsers(dest="worktree_command", required=True)

    p_worktree_preflight = worktree_subparsers.add_parser("preflight", help="Show the current WTAM worktree contract")
    p_worktree_preflight.add_argument("--project-root", default=".")
    p_worktree_preflight.add_argument("--json", action="store_true")

    p_worktree_preview = worktree_subparsers.add_parser("preview", help="Preview the WTAM start plan and prompt receipt")
    p_worktree_preview.add_argument("--project-root", default=".")
    p_worktree_preview.add_argument("--workset", required=True)
    p_worktree_preview.add_argument("--task", required=True)
    p_worktree_preview.add_argument("--actor", required=True)
    preview_prompt_group = p_worktree_preview.add_mutually_exclusive_group(required=True)
    preview_prompt_group.add_argument("--prompt")
    preview_prompt_group.add_argument("--prompt-file")
    p_worktree_preview.add_argument("--branch")
    p_worktree_preview.add_argument("--from", dest="from_ref")
    p_worktree_preview.add_argument("--path")
    p_worktree_preview.add_argument("--model")
    p_worktree_preview.add_argument("--reasoning-effort")
    p_worktree_preview.add_argument("--note")
    p_worktree_preview.add_argument("--show-prompt", action="store_true")
    p_worktree_preview.add_argument("--expand-contract", action="store_true")
    p_worktree_preview.add_argument("--json", action="store_true")

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

    p_worktree_show = worktree_subparsers.add_parser("show", help="Inspect the current or latest WTAM attempt for one task")
    p_worktree_show.add_argument("--project-root", default=".")
    p_worktree_show.add_argument("--workset", required=True)
    p_worktree_show.add_argument("--task", required=True)
    p_worktree_show.add_argument("--json", action="store_true")

    p_worktree_land = worktree_subparsers.add_parser("land", help="Create the canonical landed commit for the active WTAM task and close it")
    p_worktree_land.add_argument("--project-root", default=".")
    p_worktree_land.add_argument("--workset", required=True)
    p_worktree_land.add_argument("--task", required=True)
    p_worktree_land.add_argument("--actor", required=True)
    p_worktree_land.add_argument("--summary", required=True)
    p_worktree_land.add_argument("--validation", action="append", default=[])
    p_worktree_land.add_argument("--residual", action="append", default=[])
    p_worktree_land.add_argument("--followup", action="append", default=[])
    p_worktree_land.add_argument("--note")
    p_worktree_land.add_argument("--keep-worktree", action="store_true")
    p_worktree_land.add_argument("--json", action="store_true")

    p_worktree_close = worktree_subparsers.add_parser("close", help="Close the active WTAM task without landing code")
    p_worktree_close.add_argument("--project-root", default=".")
    p_worktree_close.add_argument("--workset", required=True)
    p_worktree_close.add_argument("--task", required=True)
    p_worktree_close.add_argument("--actor", required=True)
    p_worktree_close.add_argument("--status", required=True, choices=["blocked", "failed", "abandoned"])
    p_worktree_close.add_argument("--summary", required=True)
    p_worktree_close.add_argument("--validation", action="append", default=[])
    p_worktree_close.add_argument("--residual", action="append", default=[])
    p_worktree_close.add_argument("--followup", action="append", default=[])
    p_worktree_close.add_argument("--note")
    p_worktree_close.add_argument("--cleanup", action="store_true")
    p_worktree_close.add_argument("--json", action="store_true")

    p_worktree_cleanup = worktree_subparsers.add_parser("cleanup", help="Remove a retained or leftover WTAM worktree and delete its branch")
    p_worktree_cleanup.add_argument("--project-root", default=".")
    p_worktree_cleanup.add_argument("--workset", required=True)
    p_worktree_cleanup.add_argument("--task", required=True)
    p_worktree_cleanup.add_argument("--path")
    p_worktree_cleanup.add_argument("--branch")
    p_worktree_cleanup.add_argument("--json", action="store_true")

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
            if args.workset:
                model = scope_runtime_model(model, workset_id=args.workset)
            if args.json:
                _emit_json(build_runtime_summary(profile, workset_id=args.workset))
            else:
                print(render_summary_text(model))
            return 0

        if args.command == "snapshot":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            _emit_json(build_runtime_snapshot(profile, workset_id=args.workset))
            return 0

        if args.command == "next":
            _, model = _load_model(args.project_root)
            payload = build_next_payload(model, workset_id=args.workset)
            if args.json:
                _emit_json(payload)
            else:
                print(render_next_text(payload))
            return 0

        if args.command == "prompt" and args.prompt_command == "preview":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            prompt_text, prompt_source = _load_text_input(
                label="prompt preview",
                raw_text=args.prompt,
                file_path=args.prompt_file,
            )
            preview = preview_prompt(
                profile,
                request=prompt_text,
                prompt_source=prompt_source,
                include_prompt=args.show_prompt,
                expand_skill_text=args.expand_skill_text,
                expand_contract=args.expand_contract,
            )
            if args.json:
                _emit_json({"prompt_preview": preview.to_dict()})
            else:
                print(render_prompt_preview_text(preview, show_prompt=args.show_prompt), end="")
            return 0

        if args.command == "prompt" and args.prompt_command == "tune":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            prompt_text, prompt_source = _load_text_input(
                label="prompt tune",
                raw_text=args.prompt,
                file_path=args.prompt_file,
            )
            tuned = tune_prompt(
                profile,
                request=prompt_text,
                prompt_source=prompt_source,
                expand_skill_text=args.expand_skill_text,
                expand_contract=args.expand_contract,
            )
            if args.json:
                _emit_json({"prompt_tune": tuned.to_dict()})
            else:
                print(tuned.tuned_prompt, end="")
            return 0

        if args.command == "attempts" and args.attempts_command == "summary":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            payload = build_attempts_summary(profile, workset_id=args.workset)
            if args.json:
                _emit_json(payload)
            else:
                print(render_attempts_summary_text(payload), end="")
            return 0

        if args.command == "attempts" and args.attempts_command == "table":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            payload = build_attempts_table(profile, workset_id=args.workset)
            if args.json:
                _emit_json(payload)
            else:
                print(render_attempts_table_text(payload), end="")
            return 0

        if args.command == "repo" and args.repo_command == "install":
            result = install_repo(
                Path(args.project_root).resolve(),
                project_name=args.project_name,
                source_root=args.source_root,
            )
            if args.json:
                _emit_json({"repo": result.to_dict()})
            else:
                print(render_repo_lifecycle_text(result), end="")
            return 0

        if args.command == "repo" and args.repo_command == "update":
            result = update_repo(
                Path(args.project_root).resolve(),
                source_root=args.source_root,
            )
            if args.json:
                _emit_json({"repo": result.to_dict()})
            else:
                print(render_repo_lifecycle_text(result), end="")
            return 0

        if args.command == "repo" and args.repo_command == "refresh":
            result = refresh_repo(Path(args.project_root).resolve())
            if args.json:
                _emit_json({"repo": result.to_dict()})
            else:
                print(render_repo_lifecycle_text(result), end="")
            return 0

        if args.command == "workset" and args.workset_command == "put":
            payload = _load_json_payload(raw_json=args.json, file_path=args.file)
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            workset = upsert_workset(profile, payload)
            _emit_json({"workset": workset_to_payload(workset)})
            return 0

        if args.command == "task" and args.task_command == "begin":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            prompt_text, prompt_source = _load_text_input(
                label="task begin prompt",
                raw_text=args.prompt,
                file_path=args.prompt_file,
            )
            spec = begin_task_worktree(
                profile,
                actor=args.actor,
                prompt=prompt_text,
                prompt_source=prompt_source,
                prompt_mode=args.prompt_mode,
                workset_id=args.workset,
                task_id=args.task,
                title=args.title,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                branch=args.branch,
                from_ref=args.from_ref,
                path=args.path,
                note=args.note,
                include_prompt=args.show_prompt,
            )
            if args.json:
                _emit_json({"task": spec.to_dict()})
            else:
                print(render_task_begin_text(spec, show_prompt=args.show_prompt), end="")
            return 0

        if args.command == "task" and args.task_command == "show":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            payload = show_task(
                profile,
                workset_id=args.workset,
                task_id=args.task,
                cwd=Path.cwd(),
            )
            if args.json:
                _emit_json({"task_show": payload})
            else:
                print(render_show_text(payload), end="")
            return 0

        if args.command == "task" and args.task_command == "land":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            payload = land_task(
                profile,
                workset_id=args.workset,
                task_id=args.task,
                actor=args.actor,
                summary=args.summary,
                validations=_parse_validation_flags(args.validation),
                residuals=tuple(args.residual),
                followup_candidates=tuple(args.followup),
                note=args.note,
                cleanup=not args.keep_worktree,
                cwd=Path.cwd(),
            )
            if args.json:
                _emit_json({"landing": payload})
            else:
                print(render_land_text(payload), end="")
            return 0 if payload.get("status") == "success" else 1

        if args.command == "task" and args.task_command == "close":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            payload = close_task(
                profile,
                workset_id=args.workset,
                task_id=args.task,
                actor=args.actor,
                status=args.status,
                summary=args.summary,
                validations=_parse_validation_flags(args.validation),
                residuals=tuple(args.residual),
                followup_candidates=tuple(args.followup),
                note=args.note,
                cleanup=args.cleanup,
                cwd=Path.cwd(),
            )
            if args.json:
                _emit_json({"closure": payload})
            else:
                print(render_close_text(payload), end="")
            return 0

        if args.command == "worktree" and args.worktree_command == "preflight":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            payload = worktree_preflight(profile, cwd=profile.paths.project_root)
            if args.json:
                _emit_json(payload)
            else:
                print(render_preflight_text(payload), end="")
            return 0

        if args.command == "worktree" and args.worktree_command == "preview":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            prompt_text, prompt_source = _load_text_input(
                label="worktree preview prompt",
                raw_text=args.prompt,
                file_path=args.prompt_file,
            )
            preview = preview_task_worktree(
                profile,
                workset_id=args.workset,
                task_id=args.task,
                actor=args.actor,
                prompt=prompt_text,
                prompt_source=prompt_source,
                branch=args.branch,
                from_ref=args.from_ref,
                path=args.path,
                model=args.model,
                reasoning_effort=args.reasoning_effort,
                note=args.note,
                include_prompt=args.show_prompt,
                expand_contract=args.expand_contract,
            )
            if args.json:
                _emit_json({"worktree_preview": preview.to_dict()})
            else:
                print(
                    render_preview_text(
                        preview,
                        show_prompt=args.show_prompt,
                        expand_contract=args.expand_contract,
                    ),
                    end="",
                )
            return 0

        if args.command == "worktree" and args.worktree_command == "start":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            prompt_text, prompt_source = _load_text_input(
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
                prompt_source=prompt_source,
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

        if args.command == "worktree" and args.worktree_command == "show":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            payload = inspect_task_worktree(
                profile,
                workset_id=args.workset,
                task_id=args.task,
            )
            if args.json:
                _emit_json({"worktree_show": payload})
            else:
                print(render_show_text(payload), end="")
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
                cleanup=not args.keep_worktree,
            )
            if args.json:
                _emit_json({"landing": payload})
            else:
                print(render_land_text(payload), end="")
            return 0 if payload.get("status") == "success" else 1

        if args.command == "worktree" and args.worktree_command == "close":
            profile = load_profile(Path(args.project_root).resolve() if args.project_root else None)
            payload = close_task_worktree(
                profile,
                workset_id=args.workset,
                task_id=args.task,
                actor=args.actor,
                status=args.status,
                summary=args.summary,
                validations=_parse_validation_flags(args.validation),
                residuals=tuple(args.residual),
                followup_candidates=tuple(args.followup),
                note=args.note,
                cleanup=args.cleanup,
            )
            if args.json:
                _emit_json({"closure": payload})
            else:
                print(render_close_text(payload), end="")
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

        raise BacklogError(f"Unsupported command: {args.command}")
    except (BacklogError, ConfigError, HandlerError, RepoLifecycleError, StoreError, WorktreeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
