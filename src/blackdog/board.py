from __future__ import annotations

from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
import html as html_lib
import json
import os
import re
import subprocess

from blackdog_core.backlog import load_backlog, sync_state_for_backlog
from blackdog_core.profile import RepoProfile, BlackdogPaths
from blackdog_core.snapshot import build_runtime_snapshot
from blackdog_core.state import load_events, load_inbox, load_state, load_task_results, now_iso
from .installs import load_tracked_installs
from .conversations import list_threads
from .tuning import build_tune_analysis
from .worktree import worktree_contract


BOARD_SNAPSHOT_SCHEMA_VERSION = 11
EMBEDDED_RESPONSE_CHAR_LIMIT = 24_000
PROGRESS_STATUS_KEYS = ("running", "claimed", "ready", "waiting", "blocked", "failed", "complete")
_GIT_FIELD_SEPARATOR = "\x1f"
_GIT_RECORD_SEPARATOR = "\x1e"


class UIError(RuntimeError):
    pass


def _ui_stylesheet() -> str:
    try:
        return resources.files("blackdog").joinpath("ui.css").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise UIError("Packaged UI stylesheet is missing") from exc


def _parse_iso(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _duration_seconds(start: datetime | None, end: datetime | None = None) -> int | None:
    if start is None:
        return None
    stop = end or datetime.now().astimezone()
    return max(0, int((stop - start).total_seconds()))


def _format_duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


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


def _artifact_href(paths: BlackdogPaths, path: str | Path | None, *, must_exist: bool = False) -> str | None:
    if not path:
        return None
    candidate = Path(path).resolve()
    if must_exist and not candidate.exists():
        return None
    try:
        relative = candidate.relative_to(paths.backlog_dir.resolve())
    except ValueError:
        return None
    return quote(relative.as_posix(), safe="/")


def _run_git_capture(repo_root: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    text = completed.stdout.strip()
    return text or None


def _git_commit_metadata_row(
    commit_hash: str,
    subject: str,
    author: str,
    committed_at: str,
    message: str,
    *,
    github_repo_url: str | None = None,
) -> dict[str, Any]:
    return {
        "commit": commit_hash,
        "commit_short": _short_commit(commit_hash),
        "commit_url": f"{github_repo_url}/commit/{commit_hash}" if github_repo_url else None,
        "commit_subject": subject or None,
        "commit_author": author or None,
        "commit_at": committed_at or None,
        "commit_message": message or None,
    }


def _git_commit_metadata_batch(
    repo_root: Path,
    refs: list[str | None] | tuple[str | None, ...],
    *,
    github_repo_url: str | None = None,
) -> dict[str, dict[str, Any]]:
    ordered_refs: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        ref_text = str(ref or "").strip()
        if not ref_text or ref_text in seen:
            continue
        seen.add(ref_text)
        ordered_refs.append(ref_text)
    if not ordered_refs:
        return {}
    raw = _run_git_capture(
        repo_root,
        "show",
        "-s",
        "--no-patch",
        "--date=iso-strict",
        f"--format=%H{_GIT_FIELD_SEPARATOR}%s{_GIT_FIELD_SEPARATOR}%an{_GIT_FIELD_SEPARATOR}%ad{_GIT_FIELD_SEPARATOR}%B{_GIT_RECORD_SEPARATOR}",
        *ordered_refs,
    )
    if not raw:
        return {}
    metadata: dict[str, dict[str, Any]] = {}
    for record in raw.split(_GIT_RECORD_SEPARATOR):
        record = record.rstrip("\n")
        if not record:
            continue
        commit_hash, subject, author, committed_at, message = (record.split(_GIT_FIELD_SEPARATOR, 4) + [""] * 5)[:5]
        commit_hash = commit_hash.strip()
        if not commit_hash:
            continue
        metadata[commit_hash] = _git_commit_metadata_row(
            commit_hash,
            subject,
            author,
            committed_at,
            message,
            github_repo_url=github_repo_url,
        )
    return metadata


def _git_branch_metadata_batch(
    repo_root: Path,
    branches: list[str | None] | tuple[str | None, ...],
    *,
    github_repo_url: str | None = None,
) -> dict[str, dict[str, Any]]:
    ordered_refs: list[str] = []
    seen: set[str] = set()
    for branch in branches:
        branch_text = str(branch or "").strip()
        if not branch_text or branch_text in seen:
            continue
        seen.add(branch_text)
        ordered_refs.append(branch_text if branch_text.startswith("refs/") else f"refs/heads/{branch_text}")
    if not ordered_refs:
        return {}
    raw = _run_git_capture(
        repo_root,
        "for-each-ref",
        f"--format=%(refname){_GIT_FIELD_SEPARATOR}%(refname:short){_GIT_FIELD_SEPARATOR}%(objectname){_GIT_FIELD_SEPARATOR}%(subject){_GIT_FIELD_SEPARATOR}%(authorname){_GIT_FIELD_SEPARATOR}%(authordate:iso-strict){_GIT_FIELD_SEPARATOR}%(contents){_GIT_RECORD_SEPARATOR}",
        *ordered_refs,
    )
    if not raw:
        return {}
    metadata: dict[str, dict[str, Any]] = {}
    for record in raw.split(_GIT_RECORD_SEPARATOR):
        record = record.rstrip("\n")
        if not record:
            continue
        full_ref, short_ref, commit_hash, subject, author, committed_at, message = (
            record.split(_GIT_FIELD_SEPARATOR, 6) + [""] * 7
        )[:7]
        commit_hash = commit_hash.strip()
        if not commit_hash:
            continue
        row = _git_commit_metadata_row(
            commit_hash,
            subject,
            author,
            committed_at,
            message,
            github_repo_url=github_repo_url,
        )
        if short_ref:
            metadata[short_ref] = row
        if full_ref:
            metadata[full_ref] = row
    return metadata


def _github_repo_url(repo_root: Path) -> str | None:
    remote_url = _run_git_capture(repo_root, "remote", "get-url", "origin")
    if not remote_url:
        return None
    patterns = (
        r"^(?:https?://)?github\.com/(?P<path>[^?#]+?)(?:\.git)?/?$",
        r"^(?:ssh://)?git@github\.com[:/](?P<path>[^?#]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.match(pattern, remote_url)
        if match is None:
            continue
        repo_path = match.group("path").strip("/")
        if repo_path:
            return f"https://github.com/{repo_path}"
    return None


def _read_artifact_text(path: Path | None, *, char_limit: int = EMBEDDED_RESPONSE_CHAR_LIMIT) -> tuple[str | None, bool]:
    if path is None or not path.is_file():
        return None, False
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None, False
    if not text:
        return None, False
    truncated = len(text) > char_limit
    if truncated:
        text = text[:char_limit].rstrip() + "\n\n[truncated in reader; open Stdout for the full response]"
    return text, truncated


def _safe_markdown_href(raw: str) -> str | None:
    href = str(raw or "").strip()
    if not href:
        return None
    parsed = urlsplit(href)
    scheme = parsed.scheme.lower()
    if scheme and scheme not in {"http", "https", "mailto"}:
        return None
    if href.lower().startswith(("javascript:", "data:", "vbscript:")):
        return None
    return href


def _render_markdown_inline(text: str) -> str:
    tokens: dict[str, str] = {}

    def stash(html: str) -> str:
        token = f"@@BLACKDOGTOKEN{len(tokens)}@@"
        tokens[token] = html
        return token

    def replace_code(match: re.Match[str]) -> str:
        return stash(f"<code>{html_lib.escape(match.group(1))}</code>")

    def replace_link(match: re.Match[str]) -> str:
        label = _render_markdown_inline(match.group(1))
        raw_href = match.group(2).strip().split()[0]
        href = _safe_markdown_href(raw_href)
        if href is None:
            return match.group(0)
        return stash(
            f'<a class="text-link" href="{html_lib.escape(href, quote=True)}">{label}</a>'
        )

    def replace_strong(match: re.Match[str]) -> str:
        return stash(f"<strong>{_render_markdown_inline(match.group(2))}</strong>")

    def replace_em(match: re.Match[str]) -> str:
        return stash(f"<em>{_render_markdown_inline(match.group(2))}</em>")

    rendered = re.sub(r"`([^`\n]+)`", replace_code, text)
    rendered = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace_link, rendered)
    rendered = re.sub(r"(\*\*|__)(.+?)\1", replace_strong, rendered)
    rendered = re.sub(r"(\*|_)(.+?)\1", replace_em, rendered)
    rendered = html_lib.escape(rendered)
    for token, html in tokens.items():
        rendered = rendered.replace(token, html)
    return rendered


def _render_markdown_html(text: str | None, *, wrap: bool = True) -> str | None:
    if text is None:
        return None
    normalized = str(text).replace("\r\n", "\n").strip()
    if not normalized:
        return None

    blocks: list[str] = []
    lines = normalized.split("\n")
    paragraph: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if not paragraph:
            return
        body = "<br>".join(_render_markdown_inline(line) for line in paragraph)
        blocks.append(f"<p>{body}</p>")
        paragraph.clear()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if line.startswith("```"):
            flush_paragraph()
            fence_language = line[3:].strip()
            code_lines: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                code_lines.append(lines[index])
                index += 1
            code = html_lib.escape("\n".join(code_lines))
            language_attr = (
                f' data-language="{html_lib.escape(fence_language, quote=True)}"' if fence_language else ""
            )
            blocks.append(
                f'<pre class="detail-pre detail-pre-code"{language_attr}><code>{code}</code></pre>'
            )
            if index < len(lines):
                index += 1
            continue
        if not stripped:
            flush_paragraph()
            index += 1
            continue
        heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading_match is not None:
            flush_paragraph()
            level = len(heading_match.group(1))
            blocks.append(f"<h{level}>{_render_markdown_inline(heading_match.group(2).strip())}</h{level}>")
            index += 1
            continue
        if re.match(r"^[-*+]\s+.+$", stripped):
            flush_paragraph()
            items: list[str] = []
            while index < len(lines):
                current = lines[index].strip()
                match = re.match(r"^[-*+]\s+(.+)$", current)
                if match is None:
                    break
                items.append(f"<li>{_render_markdown_inline(match.group(1).strip())}</li>")
                index += 1
            blocks.append("<ul>" + "".join(items) + "</ul>")
            continue
        if re.match(r"^\d+\.\s+.+$", stripped):
            flush_paragraph()
            items = []
            while index < len(lines):
                current = lines[index].strip()
                match = re.match(r"^\d+\.\s+(.+)$", current)
                if match is None:
                    break
                items.append(f"<li>{_render_markdown_inline(match.group(1).strip())}</li>")
                index += 1
            blocks.append("<ol>" + "".join(items) + "</ol>")
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            quote_lines: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[index]).rstrip())
                index += 1
            quote_html = _render_markdown_html("\n".join(quote_lines), wrap=False) or ""
            blocks.append(f"<blockquote>{quote_html}</blockquote>")
            continue
        paragraph.append(stripped)
        index += 1

    flush_paragraph()
    body = "".join(blocks)
    if not body:
        return None
    if not wrap:
        return body
    return f'<div class="detail-markdown">{body}</div>'


def _find_run_dir(paths: BlackdogPaths, run_id: str) -> Path | None:
    matches = sorted(paths.supervisor_runs_dir.glob(f"*-{run_id}"))
    return matches[0].resolve() if matches else None


def _best_thread_artifact(*candidates: Path) -> Path | None:
    fallback: Path | None = None
    for candidate in candidates:
        if not candidate.is_file():
            continue
        if fallback is None:
            fallback = candidate
        try:
            if candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return fallback


def _child_artifacts(paths: BlackdogPaths, run_dir: Path | None, task_id: str) -> dict[str, Any]:
    if run_dir is None:
        return {
            "run_dir_href": None,
            "prompt_href": None,
            "thread_href": None,
            "stdout_href": None,
            "stderr_href": None,
            "metadata_href": None,
            "diff_href": None,
            "diffstat_href": None,
            "model_response": None,
            "model_response_truncated": False,
        }
    child_dir = run_dir / task_id
    stdout_path = child_dir / "stdout.log"
    stderr_path = child_dir / "stderr.log"
    thread_path = _best_thread_artifact(stderr_path, stdout_path)
    model_response, model_response_truncated = _read_artifact_text(stdout_path)
    return {
        "run_dir_href": _artifact_href(paths, child_dir, must_exist=True),
        "prompt_href": _artifact_href(paths, child_dir / "prompt.txt", must_exist=True),
        "thread_href": _artifact_href(paths, thread_path, must_exist=True),
        "stdout_href": _artifact_href(paths, stdout_path, must_exist=True),
        "stderr_href": _artifact_href(paths, stderr_path, must_exist=True),
        "metadata_href": _artifact_href(paths, child_dir / "metadata.json", must_exist=True),
        "diff_href": _artifact_href(paths, child_dir / "changes.diff", must_exist=True),
        "diffstat_href": _artifact_href(paths, child_dir / "changes.stat.txt", must_exist=True),
        "model_response": model_response,
        "model_response_truncated": model_response_truncated,
    }


def _direct_land_artifacts(paths: BlackdogPaths, payload: dict[str, Any]) -> dict[str, str | None]:
    return {
        "diff_href": _artifact_href(paths, payload.get("diff_file"), must_exist=True),
        "diffstat_href": _artifact_href(paths, payload.get("diffstat_file"), must_exist=True),
    }


def _empty_task_activity() -> dict[str, Any]:
    return {
        "claimed_by": None,
        "claimed_at": None,
        "completed_at": None,
        "released_at": None,
        "active_compute_seconds": None,
        "active_compute_label": None,
        "total_compute_seconds": 0,
        "total_compute_label": "0s",
        "latest_result_status": None,
        "latest_result_at": None,
        "latest_result_href": None,
    }


def _latest_supervisor_check_at(profile: RepoProfile) -> str | None:
    latest_check = None
    latest_parsed = None
    for status_file in profile.paths.supervisor_runs_dir.glob("*/status.json"):
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        raw = None
        if payload.get("last_checked_at"):
            raw = payload.get("last_checked_at")
        elif payload.get("completed_at"):
            raw = payload.get("completed_at")
        elif isinstance(payload.get("steps"), list) and payload["steps"]:
            last_step = payload["steps"][-1]
            if isinstance(last_step, dict):
                raw = last_step.get("at")
        if not raw:
            continue
        parsed = _parse_iso(raw)
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if latest_parsed is None or parsed > latest_parsed:
            latest_check = str(raw)
            latest_parsed = parsed
    return latest_check


def _latest_timestamp(*values: Any) -> str | None:
    latest_value = None
    latest_parsed = None
    for value in values:
        parsed = _parse_iso(value)
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        if latest_parsed is None or parsed > latest_parsed:
            latest_parsed = parsed
            latest_value = str(value)
    return latest_value


def _build_task_activity(
    paths: BlackdogPaths,
    state: dict[str, Any],
    events: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    activities: dict[str, dict[str, Any]] = {}
    open_claims: dict[str, dict[str, Any]] = {}
    ordered_events = sorted(events, key=lambda row: str(row.get("at") or ""))
    for event in ordered_events:
        task_id = str(event.get("task_id") or "")
        if not task_id:
            continue
        event_type = str(event.get("type") or "")
        event_at_text = str(event.get("at") or "")
        event_at = _parse_iso(event_at_text)
        activity = activities.setdefault(task_id, _empty_task_activity())
        if event_type == "claim" and event_at is not None:
            open_claims[task_id] = {
                "started_at": event_at,
                "started_at_text": event_at_text,
                "claimed_by": str(event.get("actor") or ""),
            }
            activity["claimed_at"] = event_at_text
            activity["claimed_by"] = str(event.get("actor") or "")
        elif event_type in {"release", "complete"}:
            current = open_claims.pop(task_id, None)
            if current and event_at is not None:
                activity["total_compute_seconds"] = int(activity.get("total_compute_seconds") or 0) + (
                    _duration_seconds(current["started_at"], event_at) or 0
                )
            if event_type == "release":
                activity["released_at"] = event_at_text
            else:
                activity["completed_at"] = event_at_text

    latest_results: dict[str, dict[str, Any]] = {}
    for row in results:
        task_id = str(row.get("task_id") or "")
        if task_id and task_id not in latest_results:
            latest_results[task_id] = row

    for task_id, entry in state.get("task_claims", {}).items():
        if not isinstance(entry, dict):
            continue
        task_id_text = str(task_id)
        activity = activities.setdefault(task_id_text, _empty_task_activity())
        claimed_at_text = str(entry.get("claimed_at") or "") or None
        claimed_at = _parse_iso(claimed_at_text)
        completed_at_text = str(entry.get("completed_at") or "") or None
        completed_at = _parse_iso(completed_at_text)
        released_at_text = str(entry.get("released_at") or "") or None
        status = str(entry.get("status") or "")
        if claimed_at_text and not activity.get("claimed_at"):
            activity["claimed_at"] = claimed_at_text
        if entry.get("claimed_by"):
            activity["claimed_by"] = str(entry.get("claimed_by") or "")
        if completed_at_text:
            activity["completed_at"] = completed_at_text
        if released_at_text:
            activity["released_at"] = released_at_text

        total_seconds = int(activity.get("total_compute_seconds") or 0)
        if status == "claimed" and claimed_at is not None:
            active_seconds = _duration_seconds(claimed_at)
            activity["active_compute_seconds"] = active_seconds
            activity["active_compute_label"] = _format_duration(active_seconds)
            total_seconds += active_seconds or 0
        elif status == "done" and total_seconds == 0 and claimed_at is not None and completed_at is not None:
            total_seconds = _duration_seconds(claimed_at, completed_at) or 0
        activity["total_compute_seconds"] = total_seconds
        activity["total_compute_label"] = _format_duration(total_seconds)

    for task_id, row in latest_results.items():
        activity = activities.setdefault(task_id, _empty_task_activity())
        activity["latest_result_status"] = row.get("status")
        activity["latest_result_at"] = row.get("recorded_at")
        activity["latest_result_href"] = _artifact_href(paths, row.get("result_file"), must_exist=True)
    return activities


def _build_task_lifecycle(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lifecycle: dict[str, dict[str, Any]] = {}
    for event in sorted(events, key=lambda row: str(row.get("at") or "")):
        task_id = str(event.get("task_id") or "")
        event_at = str(event.get("at") or "")
        if not task_id or not event_at:
            continue
        event_type = str(event.get("type") or "")
        entry = lifecycle.setdefault(
            task_id,
            {
                "created_at": event_at,
                "updated_at": event_at,
            },
        )
        if event_type == "task_added" and not entry.get("created_at"):
            entry["created_at"] = event_at
        if not entry.get("created_at"):
            entry["created_at"] = event_at
        entry["updated_at"] = event_at
    return lifecycle


def _result_preview(row: dict[str, Any] | None) -> str | None:
    if not isinstance(row, dict):
        return None
    for key in ("what_changed", "residual", "validation"):
        values = row.get(key)
        if isinstance(values, list):
            for item in values:
                text = str(item).strip()
                if text:
                    return text
    return None


def _build_result_index(paths: BlackdogPaths, results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for row in results:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        counts[task_id] = counts.get(task_id, 0) + 1
        if task_id in index:
            continue
        result_dir = paths.results_dir / task_id
        index[task_id] = {
            "result_count": counts[task_id],
            "latest_result_file": row.get("result_file"),
            "latest_result_href": _artifact_href(paths, row.get("result_file"), must_exist=True),
            "latest_result_dir_href": _artifact_href(paths, result_dir, must_exist=result_dir.exists()),
            "latest_result_status": row.get("status"),
            "latest_result_at": row.get("recorded_at"),
            "latest_result_run_id": row.get("run_id"),
            "latest_result_actor": row.get("actor"),
            "latest_result_preview": _result_preview(row),
            "latest_result_what_changed": list(row.get("what_changed") or []),
            "latest_result_validation": list(row.get("validation") or []),
            "latest_result_residual": list(row.get("residual") or []),
            "latest_result_needs_user_input": bool(row.get("needs_user_input")),
            "latest_result_task_shaping_telemetry": dict(row.get("task_shaping_telemetry") or {}),
        }
    for task_id, count in counts.items():
        index.setdefault(
            task_id,
            {
                "result_count": count,
                "latest_result_file": None,
                "latest_result_href": None,
                "latest_result_dir_href": _artifact_href(paths, paths.results_dir / task_id, must_exist=True),
                "latest_result_status": None,
                "latest_result_at": None,
                "latest_result_run_id": None,
                "latest_result_actor": None,
                "latest_result_preview": None,
                "latest_result_what_changed": [],
                "latest_result_validation": [],
                "latest_result_residual": [],
                "latest_result_needs_user_input": False,
                "latest_result_task_shaping_telemetry": {},
            },
        )
        index[task_id]["result_count"] = count
    return index


def _build_task_run_artifacts(paths: BlackdogPaths, events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    ordered_events = sorted(events, key=lambda row: str(row.get("at") or ""))
    branch_task_ids: dict[str, str] = {}
    for event in ordered_events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        branch = str(payload.get("branch") or "")
        task_id = str(event.get("task_id") or "")
        if branch and task_id:
            branch_task_ids.setdefault(branch, task_id)

    relevant_events = {"worktree_start", "worktree_land", "child_launch", "child_launch_failed", "child_finish"}
    for event in ordered_events:
        event_type = str(event.get("type") or "")
        if event_type not in relevant_events:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        task_id = str(event.get("task_id") or "") or branch_task_ids.get(str(payload.get("branch") or ""), "")
        run_id = str(payload.get("run_id") or "")
        if not task_id:
            continue
        if event_type in {"child_launch", "child_launch_failed", "child_finish"} and not run_id:
            continue
        entry = rows.setdefault(
            task_id,
            {
                "last_event_at": "",
                "run_id": None,
                "run_status": None,
                "child_agent": None,
                "workspace_mode": None,
                "task_branch": None,
                "target_branch": None,
                "primary_worktree": None,
                "pid": None,
                "started_at": None,
                "finished_at": None,
                "run_dir_href": None,
                "prompt_href": None,
                "thread_href": None,
                "stdout_href": None,
                "stderr_href": None,
                "metadata_href": None,
                "diff_href": None,
                "diffstat_href": None,
                "model_response": None,
                "model_response_truncated": False,
                "branch_ahead": None,
                "landed": None,
                "land_error": None,
                "landed_commit": None,
                "landed_commit_short": None,
                "landed_commit_url": None,
                "landed_commit_message": None,
            },
        )
        entry["last_event_at"] = str(event.get("at") or entry["last_event_at"])
        if run_id:
            entry["run_id"] = run_id
        if payload.get("child_agent"):
            entry["child_agent"] = payload.get("child_agent")
        if payload.get("workspace_mode"):
            entry["workspace_mode"] = payload.get("workspace_mode")
        if "branch_ahead" in payload:
            entry["branch_ahead"] = bool(payload.get("branch_ahead"))
        if "landed" in payload:
            entry["landed"] = bool(payload.get("landed"))
        if payload.get("branch"):
            entry["task_branch"] = payload.get("branch")
        if payload.get("target_branch"):
            entry["target_branch"] = payload.get("target_branch")
        if payload.get("primary_worktree"):
            entry["primary_worktree"] = payload.get("primary_worktree")
        if payload.get("land_error"):
            entry["land_error"] = payload.get("land_error")
        if event_type == "worktree_start":
            entry["run_status"] = entry.get("run_status") or "prepared"
        elif event_type == "worktree_land":
            entry["finished_at"] = str(event.get("at") or "")
            if payload.get("landed_commit"):
                entry["landed"] = True
                entry["landed_commit"] = str(payload.get("landed_commit"))
        elif event_type == "child_launch":
            entry["run_status"] = "running"
            entry["pid"] = payload.get("pid")
            entry["started_at"] = str(event.get("at") or "")
        elif event_type == "child_launch_failed":
            entry["run_status"] = "launch-failed"
            entry["finished_at"] = str(event.get("at") or "")
        elif event_type == "child_finish":
            if payload.get("missing_process"):
                entry["run_status"] = "interrupted"
            elif payload.get("land_error"):
                entry["run_status"] = "blocked"
            elif payload.get("exit_code") not in {0, None}:
                entry["run_status"] = "failed"
            else:
                entry["run_status"] = str(payload.get("final_task_status") or "finished")
            entry["finished_at"] = str(event.get("at") or "")
            if payload.get("landed_commit"):
                entry["landed"] = True
            if payload.get("landed_commit"):
                entry["landed_commit"] = str(payload.get("landed_commit"))

        run_dir = _find_run_dir(paths, run_id) if run_id else None
        entry.update(_child_artifacts(paths, run_dir, task_id))
        if event_type == "worktree_land":
            direct_artifacts = _direct_land_artifacts(paths, payload)
            if direct_artifacts.get("diff_href"):
                entry["diff_href"] = direct_artifacts["diff_href"]
            if direct_artifacts.get("diffstat_href"):
                entry["diffstat_href"] = direct_artifacts["diffstat_href"]

    github_repo_url = _github_repo_url(paths.project_root)
    for entry in rows.values():
        if entry.get("run_status") == "running" and not _pid_alive(entry.get("pid")):
            entry["run_status"] = "interrupted"
            entry["finished_at"] = entry.get("finished_at") or entry.get("last_event_at")
        landed_commit = _text_label(entry.get("landed_commit"))
        if landed_commit:
            entry["landed_commit_short"] = _short_commit(landed_commit)
            entry["landed_commit_url"] = f"{github_repo_url}/commit/{landed_commit}" if github_repo_url else None
        entry["elapsed_seconds"] = _duration_seconds(_parse_iso(entry.get("started_at")), _parse_iso(entry.get("finished_at")))
        entry["elapsed_label"] = _format_duration(entry.get("elapsed_seconds"))
    return rows


def _build_thread_snapshot_rows(paths: BlackdogPaths) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rows: list[dict[str, Any]] = []
    task_index: dict[str, list[dict[str, Any]]] = {}
    for thread in list_threads(paths):
        thread_id = str(thread.get("thread_id") or "")
        row = {
            "id": thread_id,
            "title": str(thread.get("title") or ""),
            "status": str(thread.get("status") or "open"),
            "created_at": str(thread.get("created_at") or ""),
            "created_by": str(thread.get("created_by") or ""),
            "updated_at": str(thread.get("updated_at") or thread.get("created_at") or ""),
            "entry_count": int(thread.get("entry_count") or 0),
            "user_entry_count": int(thread.get("user_entry_count") or 0),
            "assistant_entry_count": int(thread.get("assistant_entry_count") or 0),
            "system_entry_count": int(thread.get("system_entry_count") or 0),
            "latest_entry_at": thread.get("latest_entry_at"),
            "latest_entry_role": thread.get("latest_entry_role"),
            "latest_entry_actor": thread.get("latest_entry_actor"),
            "latest_entry_preview": thread.get("latest_entry_preview") or "",
            "task_ids": list(thread.get("task_ids") or []),
            "thread_dir_href": _artifact_href(paths, thread.get("thread_dir"), must_exist=True),
            "thread_file_href": _artifact_href(paths, thread.get("thread_file"), must_exist=True),
            "entries_href": _artifact_href(paths, thread.get("entries_file"), must_exist=True),
        }
        rows.append(row)
        task_summary = {
            "id": row["id"],
            "title": row["title"],
            "updated_at": row["updated_at"],
            "entry_count": row["entry_count"],
            "latest_entry_preview": row["latest_entry_preview"],
            "entries_href": row["entries_href"],
            "thread_file_href": row["thread_file_href"],
        }
        for task_id in row["task_ids"]:
            task_index.setdefault(str(task_id), []).append(task_summary)
    return rows, task_index


def _build_unattended_tuning(profile: RepoProfile) -> dict[str, Any]:
    analysis = build_tune_analysis(profile)
    registry = load_tracked_installs(profile.paths)
    repos = registry.get("repos") if isinstance(registry.get("repos"), list) else []
    severity_order = {"high": 0, "medium": 1, "low": 2}
    severity_counts = {"high": 0, "medium": 0, "low": 0}
    focus_counts: dict[str, int] = {}
    observed_hosts = 0
    host_rows: list[dict[str, Any]] = []

    for row in repos:
        if not isinstance(row, dict):
            continue
        observation = row.get("last_observation") if isinstance(row.get("last_observation"), dict) else {}
        counts = observation.get("counts") if isinstance(observation.get("counts"), dict) else {}
        findings = observation.get("host_integration_findings") if isinstance(observation.get("host_integration_findings"), list) else []
        observed_at = _text_label(observation.get("at"))
        tune_focus = _text_label(observation.get("tune_focus"))
        if observed_at:
            observed_hosts += 1
        if tune_focus:
            focus_counts[tune_focus] = focus_counts.get(tune_focus, 0) + 1

        finding_rows = [finding for finding in findings if isinstance(finding, dict)]
        for finding in finding_rows:
            severity = _text_label(finding.get("severity"))
            if severity in severity_counts:
                severity_counts[severity] += 1
        top_finding = (
            sorted(
                finding_rows,
                key=lambda finding: (
                    severity_order.get(str(finding.get("severity") or "").strip().lower(), 99),
                    str(finding.get("category") or ""),
                    str(finding.get("finding") or ""),
                ),
            )[0]
            if finding_rows
            else None
        )

        host_rows.append(
            {
                "project_name": str(row.get("project_name") or Path(str(row.get("project_root") or "")).name or "Unknown host"),
                "project_root": str(row.get("project_root") or ""),
                "observed_at": observed_at,
                "tune_focus": tune_focus,
                "tune_summary": _text_label(observation.get("tune_summary")),
                "counts": {
                    "ready": int(counts.get("ready") or 0),
                    "claimed": int(counts.get("claimed") or 0),
                    "waiting": int(counts.get("waiting") or 0),
                    "done": int(counts.get("done") or 0),
                },
                "finding_total": len(finding_rows),
                "top_finding": top_finding,
            }
        )

    host_rows.sort(
        key=lambda row: (
            0 if row.get("observed_at") else 1,
            -int(row.get("finding_total") or 0),
            severity_order.get(
                str(((row.get("top_finding") or {}).get("severity") or "")).strip().lower(),
                99,
            ),
            str(row.get("project_name") or "").lower(),
        )
    )

    return {
        "recommendation": analysis.get("recommendation") or {},
        "coverage_gaps": list(analysis.get("coverage_gaps") or []),
        "time": dict((analysis.get("categories") or {}).get("time") or {}),
        "missteps": dict((analysis.get("categories") or {}).get("missteps") or {}),
        "calibration": dict((analysis.get("categories") or {}).get("calibration") or {}),
        "tracked_repo_count": len(host_rows),
        "observed_repo_count": observed_hosts,
        "stale_repo_count": max(0, len(host_rows) - observed_hosts),
        "finding_severity_counts": severity_counts,
        "focus_counts": [
            {"focus": focus, "count": count}
            for focus, count in sorted(focus_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "hosts": host_rows,
    }


def _title_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("-", " ").replace("_", " ").title()


def _text_label(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _count_label(count: int, singular: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def _short_commit(value: Any, *, length: int = 12) -> str | None:
    text = _text_label(value)
    if text is None:
        return None
    return text[:length]


def _latest_task_with_timestamp(
    tasks: list[dict[str, Any]],
    fields: tuple[str, ...],
) -> tuple[dict[str, Any] | None, str | None]:
    latest_task: dict[str, Any] | None = None
    latest_field: str | None = None
    latest_value = ""
    for task in tasks:
        for field in fields:
            value = _text_label(task.get(field))
            if not value:
                continue
            if value > latest_value:
                latest_task = task
                latest_field = field
                latest_value = value
            break
    return latest_task, latest_field


def _branch_summary(contract: dict[str, Any], tasks: list[dict[str, Any]]) -> str | None:
    latest_task, _ = _latest_task_with_timestamp(tasks, ("latest_run_at", "latest_result_at", "completed_at", "claimed_at"))
    branch = (
        _text_label((latest_task or {}).get("task_branch"))
        or _text_label(contract.get("current_branch"))
        or _text_label(contract.get("target_branch"))
    )
    target = _text_label((latest_task or {}).get("target_branch")) or _text_label(contract.get("target_branch"))
    if branch and target and branch != target:
        return f"{branch} -> {target}"
    return branch or target


def _latest_run_summary(tasks: list[dict[str, Any]]) -> str:
    latest_task, source = _latest_task_with_timestamp(
        tasks,
        ("latest_run_at", "latest_result_at", "completed_at", "claimed_at"),
    )
    if latest_task is None or source is None:
        return "No recorded work yet"

    task_id = _text_label(latest_task.get("id")) or "Unknown task"
    actor = _text_label(latest_task.get("child_agent")) or _text_label(latest_task.get("claimed_by"))
    elapsed = _text_label(latest_task.get("run_elapsed_label"))
    if source == "latest_run_at":
        parts = [
            task_id,
            _title_label(latest_task.get("latest_run_status") or "running"),
            actor,
            elapsed,
        ]
        return " · ".join(part for part in parts if part)
    if source == "latest_result_at":
        parts = [
            task_id,
            f"result {_title_label(latest_task.get('latest_result_status') or 'recorded').lower()}",
            actor,
        ]
        return " · ".join(part for part in parts if part)
    if source == "completed_at":
        return " · ".join(part for part in (task_id, "completed", actor) if part)
    return " · ".join(part for part in (task_id, "claimed", actor) if part)


def _latest_event_at(
    events: list[dict[str, Any]],
    *,
    event_type: str | None = None,
    prefer_oldest: bool = False,
) -> str | None:
    chosen: datetime | None = None
    chosen_text: str | None = None
    for event in events:
        if event_type and str(event.get("type") or "") != event_type:
            continue
        parsed = _parse_iso(event.get("at"))
        if parsed is None:
            continue
        if chosen is None:
            chosen = parsed
            chosen_text = str(event.get("at") or "")
            continue
        if prefer_oldest and parsed < chosen:
            chosen = parsed
            chosen_text = str(event.get("at") or "")
        elif not prefer_oldest and parsed > chosen:
            chosen = parsed
            chosen_text = str(event.get("at") or "")
    return chosen_text


def _duration_label(start_at: Any, now: datetime | None) -> str:
    started_at = _parse_iso(start_at)
    if started_at is None:
        return "0s"
    if now is None:
        return "0s"
    return _format_duration(_duration_seconds(started_at, now)) or "0s"


def _build_hero_highlights(
    *,
    contract: dict[str, Any],
    headers: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> dict[str, str]:
    total_task_seconds = sum(int(task.get("total_compute_seconds") or 0) for task in tasks)
    active_task_seconds = sum(int(task.get("active_compute_seconds") or 0) for task in tasks)
    completed_task_seconds = sum(
        int(task.get("total_compute_seconds") or 0)
        for task in tasks
        if str(task.get("status") or "") == "done"
    )
    completed_task_samples = [
        int(task.get("total_compute_seconds") or 0)
        for task in tasks
        if str(task.get("status") or "") == "done" and int(task.get("total_compute_seconds") or 0) > 0
    ]
    average_completed_seconds = (
        round(sum(completed_task_samples) / len(completed_task_samples)) if completed_task_samples else None
    )
    return {
        "branch": _branch_summary(contract, tasks) or "",
        "commit": _short_commit(headers.get("Target commit")) or "",
        "latest_run": _latest_run_summary(tasks),
        "active_task_time": _format_duration(active_task_seconds) or "0s",
        "completed_task_time": _format_duration(completed_task_seconds) or "0s",
        "average_completed_task_time": _format_duration(average_completed_seconds) or "0s",
        "total_task_time": _format_duration(total_task_seconds) or "0s",
    }


def _actor_role(actor: Any) -> str:
    normalized = str(actor or "").strip().lower()
    if not normalized or normalized == "blackdog":
        return "system"
    if "supervisor" in normalized:
        return "supervisor"
    return "agent"


def _latest_activity(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not events:
        return None
    latest = max(events, key=lambda row: str(row.get("at") or ""))
    actor = str(latest.get("actor") or "")
    payload = latest.get("payload") if isinstance(latest.get("payload"), dict) else {}
    summary = (
        str(payload.get("note") or "").strip()
        or str(payload.get("title") or "").strip()
        or str(payload.get("status") or "").strip()
        or _title_label(latest.get("type"))
    )
    return {
        "at": latest.get("at"),
        "actor": actor,
        "actor_role": _actor_role(actor),
        "type": latest.get("type"),
        "type_label": _title_label(latest.get("type")),
        "task_id": latest.get("task_id"),
        "summary": summary,
    }


def _operator_status(task_row: dict[str, Any]) -> dict[str, str]:
    task_status = str(task_row.get("status") or "")
    run_status = str(task_row.get("latest_run_status") or "")
    result_status = str(task_row.get("latest_result_status") or "")
    detail = str(task_row.get("detail") or "").strip()

    if task_status == "done":
        return {
            "operator_status": "Complete",
            "operator_status_key": "complete",
            "operator_status_detail": str(task_row.get("completed_at") or detail or "Task completed"),
        }

    if run_status == "running":
        actor = str(task_row.get("child_agent") or task_row.get("claimed_by") or "agent").strip()
        run_detail = f"{actor} running" if actor else "Active child run"
        return {
            "operator_status": "Running",
            "operator_status_key": "running",
            "operator_status_detail": run_detail,
        }

    if run_status in {"failed", "launch-failed", "interrupted"}:
        run_detail = {
            "failed": "Child run failed",
            "launch-failed": "Child launch failed",
            "interrupted": "Child run interrupted",
        }[run_status]
        return {
            "operator_status": "Failed",
            "operator_status_key": "failed",
            "operator_status_detail": run_detail,
        }

    if run_status == "blocked":
        if task_row.get("latest_run_branch_ahead") and not task_row.get("latest_run_landed"):
            return {
                "operator_status": "Failed to land",
                "operator_status_key": "blocked",
                "operator_status_detail": str(task_row.get("latest_run_land_error") or "Landing blocked by the target branch state"),
            }
        return {
            "operator_status": "Blocked",
            "operator_status_key": "blocked",
            "operator_status_detail": "Landing blocked by the target branch state",
        }

    if result_status == "blocked":
        return {
            "operator_status": "Blocked",
            "operator_status_key": "blocked",
            "operator_status_detail": str(task_row.get("latest_result_preview") or "Latest result is blocked"),
        }

    if task_status == "approval":
        return {
            "operator_status": "Blocked",
            "operator_status_key": "blocked",
            "operator_status_detail": detail or "Approval required",
        }

    if task_status == "high-risk":
        return {
            "operator_status": "Blocked",
            "operator_status_key": "blocked",
            "operator_status_detail": detail or "High-risk task",
        }

    if task_status == "claimed" or run_status == "prepared":
        owner = str(task_row.get("claimed_by") or task_row.get("child_agent") or "").strip()
        claimed_detail = f"Claimed by {owner}" if owner else "Task claimed"
        return {
            "operator_status": "Claimed",
            "operator_status_key": "claimed",
            "operator_status_detail": claimed_detail,
        }

    if task_status == "waiting":
        return {
            "operator_status": "Waiting",
            "operator_status_key": "waiting",
            "operator_status_detail": detail or "Waiting on dependency or wave gate",
        }

    return {
        "operator_status": "Ready",
        "operator_status_key": "ready",
        "operator_status_detail": detail or "Claimable now",
    }


def _landing_status_chip(task_row: dict[str, Any]) -> dict[str, str] | None:
    if task_row.get("latest_run_landed"):
        return {
            "label": "Landed",
            "key": "landed",
            "href": str(task_row.get("landed_commit_url") or ""),
        }
    if task_row.get("latest_run_status") == "blocked" and task_row.get("operator_status_key") != "complete":
        return {
            "label": "Failed to land",
            "key": "failed-to-land",
        }
    return None


def _dialog_status_chips(task_row: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_chip(label: str, key: str, href: str | None = None) -> None:
        normalized = str(key or "").strip().lower() or str(label or "").strip().lower()
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        chip: dict[str, Any] = {"label": str(label), "key": str(key)}
        if href:
            chip["href"] = str(href)
        rows.append(chip)

    current = str(task_row.get("operator_status_key") or "ready").strip().lower()
    task_status = str(task_row.get("status") or "").strip().lower()
    if current == "running" and (task_status == "claimed" or task_row.get("claimed_by")):
        add_chip("Claimed", "claimed")
    add_chip(str(task_row.get("operator_status") or "Ready"), str(task_row.get("operator_status_key") or "ready"))
    landing_chip = _landing_status_chip(task_row)
    if landing_chip is not None:
        add_chip(
            str(landing_chip["label"]),
            str(landing_chip["key"]),
            str(landing_chip.get("href") or ""),
        )
    if current != "complete":
        covered = {
            "running": {"running", "claimed"},
            "claimed": {"prepared"},
            "blocked": {"blocked"},
            "failed": {"failed", "launch-failed", "interrupted"},
            "complete": {"finished", "done"},
        }.get(current, set())
        run_status = str(task_row.get("latest_run_status") or "").strip()
        if run_status and run_status not in covered:
            add_chip(_title_label(run_status), run_status)
        result_status = str(task_row.get("latest_result_status") or "").strip()
        if result_status and not (result_status == "blocked" and current == "blocked"):
            add_chip(_title_label(result_status), result_status)
    priority = str(task_row.get("priority") or "").strip()
    if priority:
        add_chip(priority, "subtle")
    return rows


def _card_status_chips(task_row: dict[str, Any]) -> list[dict[str, str]]:
    current = str(task_row.get("operator_status_key") or "ready").strip().lower()
    task_status = str(task_row.get("status") or "").strip().lower()
    rows: list[dict[str, str]] = []
    if current == "running" and (task_status == "claimed" or task_row.get("claimed_by")):
        rows.append({"label": "Claimed", "key": "claimed"})
    landing_chip = _landing_status_chip(task_row)
    if landing_chip is not None:
        rows.append({str(k): str(v) for k, v in landing_chip.items() if v is not None})
    rows.append(
        {
            "label": str(task_row.get("operator_status") or "Ready"),
            "key": str(task_row.get("operator_status_key") or "ready"),
        }
    )
    return rows


def _activity_message(event: dict[str, Any]) -> str:
    event_type = str(event.get("type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if event_type == "task_added":
        return "created"
    if event_type == "claim":
        return "claimed"
    if event_type == "release":
        note = str(payload.get("note") or "").strip()
        return f"released · {note}" if note else "released"
    if event_type == "complete":
        note = str(payload.get("note") or "").strip()
        return f"completed · {note}" if note else "completed"
    if event_type == "child_launch":
        return "run started"
    if event_type == "child_finish":
        if payload.get("missing_process"):
            return "run interrupted"
        if payload.get("land_error"):
            return "run blocked"
        if payload.get("exit_code") not in {0, None}:
            return "run failed"
        final_status = _title_label(payload.get("final_task_status") or "finished").lower()
        return f"run {final_status}"
    if event_type == "task_result":
        status = _title_label(payload.get("status") or "recorded").lower()
        return f"result {status}"
    return _title_label(event_type).lower()


def _build_task_timeline(events: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    timelines: dict[str, list[dict[str, str]]] = {}
    relevant = {"task_added", "claim", "release", "complete", "child_launch", "child_finish", "task_result"}
    for event in sorted(events, key=lambda row: str(row.get("at") or "")):
        event_type = str(event.get("type") or "")
        task_id = str(event.get("task_id") or "")
        if event_type not in relevant or not task_id:
            continue
        timelines.setdefault(task_id, []).append(
            {
                "type": event_type,
                "at": str(event.get("at") or ""),
                "actor": str(event.get("actor") or ""),
                "message": _activity_message(event),
            }
        )
    return timelines


def _task_links(task_row: dict[str, Any]) -> list[dict[str, str]]:
    ordered = [
        ("Commit", task_row.get("landed_commit_url")),
        ("Conversation", task_row.get("primary_conversation_entries_href")),
        ("Prompt", task_row.get("prompt_href")),
        ("Thread", task_row.get("thread_href")),
        ("Stdout", task_row.get("stdout_href")),
        ("Stderr", task_row.get("stderr_href")),
        ("Metadata", task_row.get("metadata_href")),
        ("Diff", task_row.get("diff_href")),
        ("Diffstat", task_row.get("diffstat_href")),
        ("Result", task_row.get("latest_result_href")),
        ("Result Dir", task_row.get("latest_result_dir_href")),
        ("Run", task_row.get("run_dir_href")),
    ]
    links: list[dict[str, str]] = []
    for label, href in ordered:
        if href:
            links.append({"label": label, "href": str(href)})
    return links


def _lane_task_positions(plan: dict[str, Any]) -> dict[str, dict[str, int]]:
    positions: dict[str, dict[str, int]] = {}
    for lane_index, lane in enumerate(plan.get("lanes", [])):
        task_ids = [str(item) for item in lane.get("task_ids", [])]
        if not task_ids:
            task_ids = [
                str(item.get("id") or "")
                for item in lane.get("tasks", [])
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            ]
        lane_size = len(task_ids)
        for task_index, task_id in enumerate(task_ids, start=1):
            positions[task_id] = {
                "lane_plan_index": lane_index,
                "lane_position": task_index,
                "lane_task_count": lane_size,
            }
    return positions


def _progress_for_task_rows(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {key: 0 for key in PROGRESS_STATUS_KEYS}
    for task in tasks:
        status_key = str(task.get("operator_status_key") or "ready").strip().lower() or "ready"
        if status_key not in counts:
            status_key = "ready"
        counts[status_key] += 1
    total = len(tasks)
    complete = counts["complete"]
    remaining = max(0, total - complete)
    percent = round((complete / total) * 100) if total else 0
    return {
        "counts": counts,
        "total": total,
        "complete": complete,
        "remaining": remaining,
        "percent": percent,
    }


def _last_supervisor_sweep_completed_count(events: list[dict[str, Any]]) -> int:
    last_removed_task_ids: list[str] = []
    for event in sorted(events, key=lambda row: str(row.get("at") or "")):
        if str(event.get("type") or "") != "supervisor_run_sweep":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        removed_task_ids = [
            str(task_id)
            for task_id in payload.get("removed_task_ids", [])
            if str(task_id).strip()
        ]
        last_removed_task_ids = removed_task_ids
    return len(last_removed_task_ids)


def _build_queue_status(task_rows: list[dict[str, Any]]) -> dict[str, int]:
    today = datetime.now().astimezone().date()
    running = 0
    waiting = 0
    blocked = 0
    completed_all_time = 0
    completed_today = 0

    for task in task_rows:
        status_key = str(task.get("operator_status_key") or "").strip().lower()
        if status_key == "running":
            running += 1
        elif status_key == "waiting":
            waiting += 1
        elif status_key in {"blocked", "failed"}:
            blocked += 1
        if str(task.get("status") or "") != "done":
            continue
        completed_all_time += 1
        completed_at = _parse_iso(task.get("completed_at"))
        if completed_at is not None and completed_at.date() == today:
            completed_today += 1

    return {
        "running": running,
        "waiting": waiting,
        "blocked": blocked,
        "last_sweep_completed": 0,
        "completed_today": completed_today,
        "completed_all_time": completed_all_time,
    }


def _build_objective_snapshot_rows(
    tasks: list[dict[str, Any]],
    base_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tasks_by_id = {str(task["id"]): task for task in tasks}
    objective_rows: list[dict[str, Any]] = []
    for row in base_rows:
        task_ids = [str(task_id) for task_id in row.get("task_ids", []) if str(task_id) in tasks_by_id]
        objective_tasks = [tasks_by_id[task_id] for task_id in task_ids]
        if not objective_tasks:
            continue
        progress = _progress_for_task_rows(objective_tasks)
        objective_rows.append(
            {
                "key": row.get("key"),
                "id": row.get("id"),
                "title": row.get("title"),
                "task_ids": task_ids,
                "active_task_ids": [
                    str(task["id"]) for task in objective_tasks if str(task.get("operator_status_key") or "") != "complete"
                ],
                "lane_ids": list(row.get("lane_ids") or []),
                "lane_titles": list(row.get("lane_titles") or []),
                "wave_ids": list(row.get("wave_ids") or []),
                "total": progress["total"],
                "done": progress["complete"],
                "remaining": progress["remaining"],
                "progress": progress,
            }
        )
    return objective_rows


def build_board_snapshot(
    profile: RepoProfile,
    *,
    focus_task_ids: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    snapshot = load_backlog(profile.paths, profile)
    state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
    events = load_events(profile.paths)
    messages = load_inbox(profile.paths)
    results = load_task_results(profile.paths)
    runtime_snapshot = build_runtime_snapshot(
        profile,
        snapshot,
        state,
        messages=messages,
        results=results,
        focus_task_ids=focus_task_ids,
    )
    task_activity = _build_task_activity(profile.paths, state, events, results)
    task_lifecycle = _build_task_lifecycle(events)
    task_timeline = _build_task_timeline(events)
    task_results = _build_result_index(profile.paths, results)
    task_runs = _build_task_run_artifacts(profile.paths, events)
    threads, task_threads = _build_thread_snapshot_rows(profile.paths)
    unattended_tuning = _build_unattended_tuning(profile)
    github_repo_url = _github_repo_url(profile.paths.project_root)
    task_branch_metadata = _git_branch_metadata_batch(
        profile.paths.project_root,
        [run.get("task_branch") for run in task_runs.values()],
        github_repo_url=github_repo_url,
    )
    landed_commit_metadata = _git_commit_metadata_batch(
        profile.paths.project_root,
        [run.get("landed_commit") for run in task_runs.values()],
        github_repo_url=github_repo_url,
    )
    plan = runtime_snapshot["plan"]
    lane_positions = _lane_task_positions(plan)
    core_task_rows = {str(row["id"]): row for row in runtime_snapshot["tasks"]}
    visible_task_ids = set(core_task_rows)
    focused = str((runtime_snapshot.get("workset") or {}).get("visibility") or "") == "focused"
    tasks: list[dict[str, Any]] = []
    graph_edges: list[dict[str, str]] = []
    ordered_tasks = sorted(
        (
            task
            for task in snapshot.tasks.values()
            if task.id in visible_task_ids
        ),
        key=lambda task: (
            task.wave if task.wave is not None else 9999,
            task.lane_order if task.lane_order is not None else 9999,
            task.lane_position if task.lane_position is not None else 9999,
            task.id,
        ),
    )
    for task in ordered_tasks:
        activity = task_activity.get(task.id, _empty_task_activity())
        lifecycle = task_lifecycle.get(task.id, {})
        result_info = task_results.get(task.id, {})
        run_info = task_runs.get(task.id, {})
        conversation_threads = task_threads.get(task.id, [])
        lane_info = lane_positions.get(task.id, {})
        task_branch = _text_label(run_info.get("task_branch"))
        landed_commit_ref = _text_label(run_info.get("landed_commit"))
        task_commit = task_branch_metadata.get(task_branch or "")
        landed_commit = landed_commit_metadata.get(landed_commit_ref or "")
        activity_rows = list(task_timeline.get(task.id, []))
        created_at = str(lifecycle.get("created_at") or "")
        updated_at = str(lifecycle.get("updated_at") or "")
        if activity_rows:
            created_at = created_at or str(activity_rows[0].get("at") or "")
            updated_at = updated_at or str(activity_rows[-1].get("at") or "")
        task_row = {
            **core_task_rows[task.id],
            "created_at": created_at or None,
            "updated_at": _latest_timestamp(
                updated_at,
                activity.get("claimed_at"),
                activity.get("completed_at"),
                activity.get("released_at"),
                result_info.get("latest_result_at") or activity.get("latest_result_at"),
                run_info.get("last_event_at"),
            )
            or created_at
            or None,
            "lane_plan_index": lane_info.get("lane_plan_index", task.lane_order if task.lane_order is not None else 9999),
            "lane_position": lane_info.get("lane_position"),
            "lane_task_count": lane_info.get("lane_task_count"),
            "activity": activity_rows,
            "claimed_by": activity.get("claimed_by"),
            "claimed_at": activity.get("claimed_at"),
            "completed_at": activity.get("completed_at"),
            "released_at": activity.get("released_at"),
            "active_compute_seconds": activity.get("active_compute_seconds"),
            "active_compute_label": activity.get("active_compute_label"),
            "total_compute_seconds": activity.get("total_compute_seconds"),
            "total_compute_label": activity.get("total_compute_label"),
            "latest_result_status": result_info.get("latest_result_status") or activity.get("latest_result_status"),
            "latest_result_at": result_info.get("latest_result_at") or activity.get("latest_result_at"),
            "latest_result_run_id": result_info.get("latest_result_run_id"),
            "latest_result_actor": result_info.get("latest_result_actor"),
            "latest_result_href": result_info.get("latest_result_href") or activity.get("latest_result_href"),
            "latest_result_dir_href": result_info.get("latest_result_dir_href"),
            "latest_result_preview": result_info.get("latest_result_preview"),
            "latest_result_what_changed": result_info.get("latest_result_what_changed") or [],
            "latest_result_validation": result_info.get("latest_result_validation") or [],
            "latest_result_residual": result_info.get("latest_result_residual") or [],
            "latest_result_needs_user_input": bool(result_info.get("latest_result_needs_user_input")),
            "latest_result_task_shaping_telemetry": result_info.get("latest_result_task_shaping_telemetry") or {},
            "result_count": int(result_info.get("result_count") or 0),
            "latest_run_id": run_info.get("run_id"),
            "latest_run_status": run_info.get("run_status"),
            "latest_run_branch_ahead": run_info.get("branch_ahead"),
            "latest_run_landed": run_info.get("landed"),
            "latest_run_land_error": run_info.get("land_error"),
            "latest_run_at": run_info.get("last_event_at"),
            "run_dir_href": run_info.get("run_dir_href"),
            "prompt_href": run_info.get("prompt_href"),
            "thread_href": run_info.get("thread_href"),
            "stdout_href": run_info.get("stdout_href"),
            "stderr_href": run_info.get("stderr_href"),
            "metadata_href": run_info.get("metadata_href"),
            "diff_href": run_info.get("diff_href"),
            "diffstat_href": run_info.get("diffstat_href"),
            "workspace_mode": run_info.get("workspace_mode"),
            "task_branch": run_info.get("task_branch"),
            "target_branch": run_info.get("target_branch"),
            "child_agent": run_info.get("child_agent"),
            "run_elapsed_seconds": run_info.get("elapsed_seconds"),
            "run_elapsed_label": run_info.get("elapsed_label"),
            "model_response": run_info.get("model_response"),
            "model_response_html": _render_markdown_html(run_info.get("model_response")),
            "model_response_truncated": bool(run_info.get("model_response_truncated")),
            "landed_commit": run_info.get("landed_commit"),
            "landed_commit_short": (landed_commit or {}).get("commit_short") or run_info.get("landed_commit_short"),
            "landed_commit_url": (landed_commit or {}).get("commit_url") or run_info.get("landed_commit_url"),
            "landed_commit_message": (landed_commit or {}).get("commit_message") or run_info.get("landed_commit_message"),
            "task_commit": (task_commit or {}).get("commit"),
            "task_commit_short": (task_commit or {}).get("commit_short"),
            "task_commit_url": (task_commit or {}).get("commit_url"),
            "task_commit_subject": (task_commit or {}).get("commit_subject"),
            "task_commit_author": (task_commit or {}).get("commit_author"),
            "task_commit_at": (task_commit or {}).get("commit_at"),
            "task_commit_message": (task_commit or {}).get("commit_message"),
            "landed_commit_subject": (landed_commit or {}).get("commit_subject"),
            "landed_commit_author": (landed_commit or {}).get("commit_author"),
            "landed_commit_at": (landed_commit or {}).get("commit_at"),
            "conversation_threads": conversation_threads,
            "conversation_thread_ids": [str(row.get("id") or "") for row in conversation_threads],
            "conversation_thread_count": len(conversation_threads),
            "primary_conversation_thread_id": conversation_threads[0]["id"] if conversation_threads else None,
            "primary_conversation_thread_title": conversation_threads[0]["title"] if conversation_threads else None,
            "primary_conversation_entries_href": conversation_threads[0].get("entries_href") if conversation_threads else None,
            "primary_conversation_file_href": conversation_threads[0].get("thread_file_href") if conversation_threads else None,
        }
        task_row.update(_operator_status(task_row))
        task_row["card_status_chips"] = _card_status_chips(task_row)
        task_row["dialog_status_chips"] = _dialog_status_chips(task_row)
        task_row["links"] = _task_links(task_row)
        tasks.append(task_row)
        for predecessor_id in task.predecessor_ids:
            if predecessor_id in visible_task_ids:
                graph_edges.append({"from": predecessor_id, "to": task.id})

    focus_tasks = tasks
    recent_results = []
    for row in results:
        task_id = str(row.get("task_id") or "")
        if task_id and task_id not in visible_task_ids:
            continue
        recent_results.append(
            {
                "task_id": task_id,
                "status": row.get("status"),
                "actor": row.get("actor"),
                "recorded_at": row.get("recorded_at"),
                "result_file": row.get("result_file"),
                "result_href": _artifact_href(profile.paths, row.get("result_file"), must_exist=True),
                "preview": _result_preview(row),
            }
        )
        if len(recent_results) >= 10:
            break
    open_messages = [row for row in runtime_snapshot["open_messages"][:10]]
    board_tasks = [row for row in tasks if row.get("lane_id")]
    active_tasks = [
        row
        for row in tasks
        if row["status"] == "claimed" or row.get("latest_run_status") in {"prepared", "running", "interrupted"}
    ]
    active_tasks.sort(
        key=lambda row: (
            row.get("wave") if row.get("wave") is not None else 9999,
            row.get("lane_plan_index") if row.get("lane_plan_index") is not None else 9999,
            row.get("lane_position") if row.get("lane_position") is not None else 9999,
            str(row["id"]),
        )
    )
    workspace_contract = worktree_contract(profile)
    headers = dict(runtime_snapshot["headers"])
    generated_at = now_iso()
    supervisor_last_checked_at = _latest_supervisor_check_at(profile)
    last_checked_at = _latest_timestamp(supervisor_last_checked_at, generated_at) or generated_at
    filtered_events = [
        row
        for row in events
        if not str(row.get("task_id") or "").strip() or str(row.get("task_id") or "") in visible_task_ids
    ]
    content_updated_at = _latest_timestamp(*[row.get("at") for row in filtered_events]) or generated_at
    last_activity = _latest_activity(filtered_events)
    recent_events = filtered_events[-10:]
    threads = [
        row
        for row in threads
        if not row["task_ids"] or any(str(task_id) in visible_task_ids for task_id in row["task_ids"])
    ]

    return {
        "schema_version": BOARD_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "content_updated_at": content_updated_at,
        "last_checked_at": last_checked_at,
        "supervisor_last_checked_at": supervisor_last_checked_at,
        "project_name": runtime_snapshot["project_name"],
        "project_root": runtime_snapshot["project_root"],
        "control_dir": runtime_snapshot["control_dir"],
        "profile_file": runtime_snapshot["profile_file"],
        "runtime_snapshot": runtime_snapshot,
        "workspace_contract": workspace_contract,
        "headers": headers,
        "hero_highlights": _build_hero_highlights(
            contract=workspace_contract,
            headers=headers,
            tasks=focus_tasks,
        ),
        "last_activity": last_activity,
        "counts": runtime_snapshot["counts"],
        "total": runtime_snapshot["total"],
        "queue_status": {
            **_build_queue_status(tasks),
            "last_sweep_completed": _last_supervisor_sweep_completed_count(events),
        },
        "push_objective": runtime_snapshot["push_objective"],
        "objectives": runtime_snapshot["objectives"],
        "focus_task_ids": sorted(visible_task_ids) if focused else [],
        "next_rows": runtime_snapshot["next_rows"],
        "open_messages": open_messages,
        "recent_results": recent_results,
        "recent_events": recent_events,
        "plan": plan,
        "threads": threads,
        "unattended_tuning": unattended_tuning,
        "tasks": tasks,
        "board_tasks": board_tasks,
        "graph": {
            "tasks": tasks,
            "edges": graph_edges,
        },
        "active_tasks": active_tasks,
        "links": {
            "backlog": _artifact_href(profile.paths, profile.paths.backlog_file, must_exist=True),
            "state": _artifact_href(profile.paths, profile.paths.state_file, must_exist=True),
            "html": _artifact_href(profile.paths, profile.paths.html_file),
            "events": _artifact_href(profile.paths, profile.paths.events_file, must_exist=True),
            "inbox": _artifact_href(profile.paths, profile.paths.inbox_file, must_exist=True),
            "results": _artifact_href(profile.paths, profile.paths.results_dir, must_exist=True),
            "threads": _artifact_href(profile.paths, profile.paths.threads_dir, must_exist=True),
        },
        "grouping_guide": [
            {
                "name": "workset",
                "meaning": "The preferred planning and visibility boundary. A workset owns the visible task slice, task DAG, and target branch for the current view.",
            },
            {
                "name": "task",
                "meaning": "The executable unit. Claims, results, completion, and dependencies are tracked at task level.",
            },
            {
                "name": "task attempt",
                "meaning": "One concrete execution of one task by one actor in one workspace. Attempts carry prompt, result, and landing lineage.",
            },
            {
                "name": "workset execution",
                "meaning": "The coordination object above task attempts. Current product artifacts still expose it through run-shaped compatibility fields such as run_id and supervisor-runs.",
            },
            {
                "name": "epic",
                "meaning": "A legacy thematic grouping projection for related tasks. Epics remain readable for compatibility and reporting, not as preferred planning truth.",
            },
            {
                "name": "lane",
                "meaning": "A legacy ordered slot in the active execution map. Lanes are compatibility layout metadata, not executable state objects.",
            },
            {
                "name": "wave",
                "meaning": "A legacy concurrency bucket for lanes. Waves are reused and compacted between executions; they are scheduler gates, not historical identities.",
            },
        ],
    }


def _snapshot_json(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, indent=2, sort_keys=True).replace("</", "<\\/")


def render_static_html(snapshot: dict[str, Any], output_path: Path) -> str:
    title = html_lib.escape(str(snapshot["project_name"]))
    stylesheet = _ui_stylesheet()
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__BLACKDOG_PAGE_TITLE__</title>
  <style>
__BLACKDOG_STYLES__
  </style>
</head>
<body>
  <script id="blackdog-snapshot" type="application/json">__BLACKDOG_SNAPSHOT__</script>
  <div class="page">
    <div class="page-shell">
      <section id="top-band" class="panel-row panel-row-top" data-panel="hero">
        <article id="hero-panel" class="panel control-panel">
          <div class="hero-head">
            <div class="brand-lockup">
              <h1 id="project-name">__BLACKDOG_TITLE__</h1>
              <p id="product-name" class="brand-subtitle">blackdog backlog</p>
            </div>
            <div id="repo-root-badge" class="repo-root-badge"></div>
          </div>
          <div id="hero-meta-line" class="meta-line"></div>
          <div id="hero-reload-controls" class="hero-controls"></div>
          <p id="hero-note" class="hero-note"></p>
          <div class="progress-cluster progress-cluster-hero">
            <div class="progress-copy progress-copy-hero">
              <span id="hero-progress-detail" class="progress-detail"></span>
            </div>
            <div id="hero-progress" class="progress-slot"></div>
          </div>
          <div id="hero-links" class="link-row"></div>
        </article>
        <aside id="status-panel" class="panel status-panel">
          <h2>Status</h2>
          <div id="queue-stats" class="stats"></div>
          <div class="status-next">
            <p class="status-next-head">Next in line</p>
            <div id="status-next-lines" class="status-next-lines"></div>
          </div>
        </aside>
      </section>

      <section id="tuning-band" class="panel-row panel-row-single" data-panel="tuning">
        <section id="tuning-panel" class="panel board-panel">
          <div class="section-head section-head-inline">
            <div>
              <h2>Unattended Tuning</h2>
              <p id="tuning-copy" class="section-copy"></p>
            </div>
            <span id="tuning-summary" class="section-meta"></span>
          </div>
          <div id="tuning-stats" class="stats tuning-stats"></div>
          <div id="tuning-focuses" class="reader-statuses"></div>
          <div id="tuning-hosts" class="tuning-hosts"></div>
        </section>
      </section>

      <section id="middle-band" class="panel-row panel-row-middle">
        <section id="execution-panel" class="panel board-panel" data-panel="execution">
          <div class="section-head backlog-head">
            <div>
              <h2>Execution Map</h2>
            </div>
            <div class="toolbar-topline">
              <span id="board-summary" class="section-meta"></span>
              <a id="inbox-link" class="text-link" href="#">Inbox JSON</a>
            </div>
          </div>
          <div id="lane-board" class="lane-board"></div>
        </section>

        <section id="completed-panel" class="panel result-panel" data-panel="completed">
          <div class="section-head">
            <div>
              <h2>Completed Tasks</h2>
              <p id="completed-copy" class="section-copy"></p>
            </div>
            <span id="completed-summary" class="section-meta"></span>
          </div>
          <div id="completed-history-scroll" class="result-history"></div>
        </section>
      </section>
    </div>
  </div>

  <dialog id="reader-dialog">
    <article class="reader">
      <div class="reader-head">
        <div>
          <p id="reader-context" class="reader-context"></p>
          <h2 id="reader-title"></h2>
        </div>
        <form method="dialog">
          <button class="close-button" type="submit">Close</button>
        </form>
      </div>
      <div id="reader-statuses" class="reader-statuses"></div>
      <div id="reader-links" class="reader-links"></div>
      <div id="reader-grid" class="detail-grid"></div>
    </article>
  </dialog>

  <script>
    const snapshot = JSON.parse(document.getElementById("blackdog-snapshot").textContent);
    const runtimeSnapshot = snapshot.runtime_snapshot || {};
    const allTasks = Array.isArray(snapshot.tasks) ? snapshot.tasks.slice() : [];
    const allTasksById = new Map(allTasks.map((task) => [String(task.id), task]));
    const lanePlan = Array.isArray(runtimeSnapshot.plan?.lanes)
      ? runtimeSnapshot.plan.lanes.slice()
      : Array.isArray(snapshot.plan?.lanes)
        ? snapshot.plan.lanes.slice()
        : [];
    const focusTaskIds = new Set(
      Array.isArray(snapshot.focus_task_ids) ? snapshot.focus_task_ids.map((taskId) => String(taskId)) : []
    );
    const boardTasks = Array.isArray(snapshot.board_tasks)
      ? snapshot.board_tasks.filter((task) => normalizeStatus(task.operator_status_key) !== "complete")
      : allTasks.filter((task) => task.lane_id && normalizeStatus(task.operator_status_key) !== "complete");
    const focusTasks = focusTaskIds.size
      ? allTasks.filter((task) => focusTaskIds.has(String(task.id)))
      : boardTasks.length
        ? boardTasks
        : allTasks;
    const trackedTasks = focusTasks.length ? focusTasks : allTasks;
    const completedTasks = allTasks
      .filter((task) => normalizeStatus(task.operator_status_key) === "complete")
      .sort((left, right) => completionEpoch(right) - completionEpoch(left));
    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function normalizeStatus(value) {
      return String(value || "").trim().toLowerCase().replaceAll(" ", "-");
    }

    function chip(label, key, href) {
      if (!label) {
        return "";
      }
      const normalized = normalizeStatus(key || label) || "subtle";
      if (href) {
        return `<a class="chip chip-${escapeHtml(normalized)} chip-link" href="${escapeHtml(href)}">${escapeHtml(label)}</a>`;
      }
      return `<span class="chip chip-${escapeHtml(normalized)}">${escapeHtml(label)}</span>`;
    }

    function textLink(label, href) {
      if (!href) {
        return "";
      }
      return `<a class="text-link" href="${escapeHtml(href)}">${escapeHtml(label)}</a>`;
    }

    function interactiveCardAttributes(taskId) {
      if (!taskId) {
        return "";
      }
      return ` data-task-id="${escapeHtml(taskId)}" role="button" tabindex="0"`;
    }

    function formatTimestamp(value) {
      if (!value) {
        return "";
      }
      const parsed = Date.parse(String(value));
      if (Number.isNaN(parsed)) {
        return String(value);
      }
      return new Date(parsed).toLocaleString();
    }

    function globalLinks() {
      const links = snapshot.links || {};
      return [
        ["Backlog", links.backlog],
        ["State", links.state],
        ["Events", links.events],
        ["Results", links.results]
      ];
    }

    function countStatuses(tasks) {
      const counts = { total: tasks.length, ready: 0, running: 0, claimed: 0, waiting: 0, blocked: 0, failed: 0, complete: 0 };
      for (const task of tasks) {
        const key = normalizeStatus(task.operator_status_key || "ready");
        counts[key] = (counts[key] || 0) + 1;
      }
      return counts;
    }

    function progressMetrics(tasks) {
      const counts = countStatuses(tasks);
      const total = Number(counts.total || 0);
      const complete = Number(counts.complete || 0);
      const remaining = Math.max(0, total - complete);
      const percent = total ? Math.max(0, Math.min(100, Math.round((complete / total) * 100))) : 0;
      return { counts, total, complete, remaining, percent };
    }

    function renderProgressBar(progress, className = "") {
      const safeClassName = className ? ` ${escapeHtml(className)}` : "";
      return `
        <div class="progress-bar${safeClassName}" aria-hidden="true">
          <span class="progress-fill" data-progress="${escapeHtml(progress.percent)}"></span>
        </div>
      `;
    }

    function applyProgressBars(root = document) {
      root.querySelectorAll(".progress-fill[data-progress]").forEach((node) => {
        const value = Number(node.getAttribute("data-progress") || "0");
        const clamped = Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
        node.style.width = `${clamped}%`;
      });
    }

    function taskSummary(task) {
      return task.latest_result_preview || task.operator_status_detail || task.detail || task.safe_first_slice || "";
    }

    function laneRows(tasks) {
      const rows = new Map();
      lanePlan.forEach((lane, index) => {
        rows.set(String(lane.id), {
          id: String(lane.id),
          title: lane.title || "Unplanned",
          wave: lane.wave,
          plan_index: index,
          tasks: []
        });
      });
      for (const task of tasks) {
        const key = String(task.lane_id || `lane:${task.id}`);
        if (!rows.has(key)) {
          rows.set(key, {
            id: key,
            title: task.lane_title || "Unplanned",
            wave: task.wave,
            plan_index: Number(task.lane_plan_index ?? 9999),
            tasks: []
          });
        }
        rows.get(key).tasks.push(task);
      }
      return Array.from(rows.values())
        .filter((lane) => lane.tasks.length)
        .map((lane) => ({
          ...lane,
          tasks: lane.tasks.sort((left, right) => {
            const leftPosition = left.lane_position == null ? 9999 : Number(left.lane_position);
            const rightPosition = right.lane_position == null ? 9999 : Number(right.lane_position);
            if (leftPosition !== rightPosition) {
              return leftPosition - rightPosition;
            }
            return String(left.id).localeCompare(String(right.id));
          })
        }))
        .sort((left, right) => {
          const leftWave = left.wave == null ? 9999 : Number(left.wave);
          const rightWave = right.wave == null ? 9999 : Number(right.wave);
          if (leftWave !== rightWave) {
            return leftWave - rightWave;
          }
          const leftPlan = left.plan_index == null ? 9999 : Number(left.plan_index);
          const rightPlan = right.plan_index == null ? 9999 : Number(right.plan_index);
          if (leftPlan !== rightPlan) {
            return leftPlan - rightPlan;
          }
          return String(left.title).localeCompare(String(right.title));
        });
    }

    function groupedWaveRows(tasks) {
      const waves = new Map();
      for (const lane of laneRows(tasks)) {
        const waveId = lane.wave == null ? "Unassigned" : `Wave ${lane.wave}`;
        if (!waves.has(waveId)) {
          waves.set(waveId, { id: waveId, wave: lane.wave, lanes: [] });
        }
        waves.get(waveId).lanes.push(lane);
      }
      return Array.from(waves.values()).sort((left, right) => {
        const leftWave = left.wave == null ? 9999 : Number(left.wave);
        const rightWave = right.wave == null ? 9999 : Number(right.wave);
        return leftWave - rightWave;
      });
    }

    function pluralize(count, noun) {
      return `${count} ${noun}${count === 1 ? "" : "s"}`;
    }

    function nextRows() {
      const next = Array.isArray(runtimeSnapshot.next_rows)
        ? runtimeSnapshot.next_rows.slice(0, 2)
        : Array.isArray(snapshot.next_rows)
          ? snapshot.next_rows.slice(0, 2)
          : [];
      if (next.length) {
        return next;
      }
      return trackedTasks
        .filter((task) => normalizeStatus(task.operator_status_key) === "ready")
        .slice(0, 2)
        .map((task) => ({
          id: task.id,
          title: task.title,
          lane: task.lane_title,
          wave: task.wave,
          risk: task.risk,
        }));
    }

    function nextLine(row) {
      const taskId = row?.id ? String(row.id) : "";
      const meta = [
        row?.lane ? `Lane ${row.lane}` : "",
        row?.wave != null ? `Wave ${row.wave}` : "",
        row?.risk ? `Risk ${row.risk}` : "",
      ].filter(Boolean).join(" · ");
      return `
        <div class="status-next-line"${interactiveCardAttributes(taskId)}>
          <strong>${escapeHtml(taskId ? `${taskId} ${row.title || ""}` : row?.title || "No queued work")}</strong>
          <span>${escapeHtml(meta || "No additional scheduling detail")}</span>
        </div>
      `;
    }

    function taskTone(task) {
      return normalizeStatus(task.operator_status_key || "ready") || "ready";
    }

    function renderStatusChipRows(rows) {
      if (!Array.isArray(rows) || !rows.length) {
        return "";
      }
      return rows.map((row) => chip(row.label, row.key, row.href)).join("");
    }

    function renderTaskLinks(task) {
      const links = Array.isArray(task.links) ? task.links : [];
      return links.map((row) => textLink(row.label, row.href)).join("");
    }

    function renderMetaItem(label, value, options = {}) {
      if (!value) {
        return "";
      }
      const valueClass = options.mono ? "meta-value meta-value-mono" : "meta-value";
      return `
        <span class="meta-item">
          <span class="meta-label">${escapeHtml(label)}:</span>
          <span class="${valueClass}">${escapeHtml(value)}</span>
        </span>
      `;
    }

    function heroProgressSummary(progress) {
      if (!progress.total) {
        return "No tracked tasks";
      }
      const noun = progress.total === 1 ? "task" : "tasks";
      return `${progress.complete}/${progress.total} ${noun} complete`;
    }

    const AUTO_RELOAD_INTERVAL_SECONDS = 30;
    const AUTO_RELOAD_STORAGE_KEY = "blackdog:autoReloadEnabled";
    let autoReloadEnabled = false;
    let autoReloadCountdownSeconds = AUTO_RELOAD_INTERVAL_SECONDS;
    let autoReloadTimerId = null;

    function readAutoReloadPreference() {
      try {
        return window.localStorage.getItem(AUTO_RELOAD_STORAGE_KEY) === "true";
      } catch (error) {
        return false;
      }
    }

    function writeAutoReloadPreference(enabled) {
      try {
        if (enabled) {
          window.localStorage.setItem(AUTO_RELOAD_STORAGE_KEY, "true");
        } else {
          window.localStorage.removeItem(AUTO_RELOAD_STORAGE_KEY);
        }
      } catch (error) {
      }
    }

    function stopAutoReloadTimer() {
      if (autoReloadTimerId == null) {
        return;
      }
      window.clearInterval(autoReloadTimerId);
      autoReloadTimerId = null;
    }

    function autoReloadStatusText() {
      if (!autoReloadEnabled) {
        return `Manual mode · ${AUTO_RELOAD_INTERVAL_SECONDS}s cycle`;
      }
      return `Next refresh in ${autoReloadCountdownSeconds}s`;
    }

    function renderAutoReloadControls() {
      const container = document.getElementById("hero-reload-controls");
      if (!container) {
        return;
      }
      const buttonClass = autoReloadEnabled ? "reload-toggle is-active" : "reload-toggle";
      const buttonLabel = autoReloadEnabled ? "Auto-reload on" : "Auto-reload off";
      container.innerHTML = `
        <button type="button" id="auto-reload-toggle" class="${buttonClass}" aria-pressed="${autoReloadEnabled ? "true" : "false"}">
          ${escapeHtml(buttonLabel)}
        </button>
        <span class="reload-status">${escapeHtml(autoReloadStatusText())}</span>
      `;
      const toggle = document.getElementById("auto-reload-toggle");
      if (toggle) {
        toggle.addEventListener("click", () => {
          autoReloadEnabled = !autoReloadEnabled;
          writeAutoReloadPreference(autoReloadEnabled);
          startAutoReloadTimer();
        });
      }
    }

    function startAutoReloadTimer() {
      stopAutoReloadTimer();
      autoReloadCountdownSeconds = AUTO_RELOAD_INTERVAL_SECONDS;
      renderAutoReloadControls();
      if (!autoReloadEnabled) {
        return;
      }
      autoReloadTimerId = window.setInterval(() => {
        autoReloadCountdownSeconds = Math.max(0, autoReloadCountdownSeconds - 1);
        if (autoReloadCountdownSeconds <= 0) {
          stopAutoReloadTimer();
          window.location.reload();
          return;
        }
        renderAutoReloadControls();
      }, 1000);
    }

    function renderHeader() {
      const heroHighlights = snapshot.hero_highlights || {};
      const headers = runtimeSnapshot.headers || snapshot.headers || {};
      const activity = snapshot.last_activity || {};
      const overallProgress = progressMetrics(trackedTasks);
      const repoRoot = runtimeSnapshot.project_root || snapshot.project_root || headers["Repo root"] || "";
      const metaItems = [
        renderMetaItem("Active Branch", heroHighlights.branch || headers["Target branch"] || "", { mono: true }),
        renderMetaItem("Commit", heroHighlights.commit || headers["Target commit"] || "", { mono: true }),
        renderMetaItem("Active task time", heroHighlights.active_task_time || "0s"),
        renderMetaItem("Completed task time", heroHighlights.completed_task_time || "0s"),
        renderMetaItem("Average completed task", heroHighlights.average_completed_task_time || "0s"),
        renderMetaItem("Total task time", heroHighlights.total_task_time || "0s"),
      ].filter(Boolean);
      document.getElementById("repo-root-badge").innerHTML = repoRoot
        ? `
          <span class="repo-root-label">Repo directory</span>
          <span class="repo-root-value">${escapeHtml(repoRoot)}</span>
        `
        : "";
      document.getElementById("hero-meta-line").innerHTML = metaItems
        .join("");
      document.getElementById("hero-note").textContent = activity.summary
        ? `Latest activity: ${activity.task_id ? `${activity.task_id} ` : ""}${activity.summary}`
        : "Snapshot follows committed backlog state and recorded task results.";
      document.getElementById("hero-progress-detail").textContent = heroProgressSummary(overallProgress);
      document.getElementById("hero-progress").innerHTML = renderProgressBar(overallProgress, "progress-hero");
      applyProgressBars(document.getElementById("hero-progress"));
      document.getElementById("hero-links").innerHTML = globalLinks()
        .map(([label, href]) => textLink(label, href))
        .join("");
    }

    function renderStatusPanel() {
      const panelCounts = snapshot.queue_status || {};
      const counts = countStatuses(trackedTasks);
      const stats = [
        ["Running", Number(panelCounts.running != null ? panelCounts.running : (counts.running || 0))],
        ["Waiting", Number(panelCounts.waiting != null ? panelCounts.waiting : (counts.waiting || 0))],
        ["Blocked", Number(panelCounts.blocked != null ? panelCounts.blocked : ((counts.blocked || 0) + (counts.failed || 0)))],
        ["Last sweep completed", Number(panelCounts.last_sweep_completed || 0)],
        ["Completed today", Number(panelCounts.completed_today || 0)],
        ["Completed all-time", Number(panelCounts.completed_all_time || 0)],
      ];
      document.getElementById("queue-stats").innerHTML = stats.map(([label, value]) => `
        <div class="stat-card">
          <span class="stat-label">${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `).join("");
      const lines = nextRows();
      document.getElementById("status-next-lines").innerHTML = lines.length
        ? lines.map(nextLine).join("")
        : `<div class="status-next-line"><strong>No queued work</strong><span>All active lanes are waiting on completion.</span></div>`;
    }

    function renderUnattendedTuningPanel() {
      const tuning = snapshot.unattended_tuning || {};
      const recommendation = tuning.recommendation || {};
      const time = tuning.time || {};
      const missteps = tuning.missteps || {};
      const severityCounts = tuning.finding_severity_counts || {};
      const hosts = Array.isArray(tuning.hosts) ? tuning.hosts : [];
      const focusRows = Array.isArray(tuning.focus_counts) ? tuning.focus_counts : [];
      const stats = [
        ["Recorded tasks", Number(time.tasks_with_recorded_compute || 0)],
        ["Timing samples", Number(time.estimated_time_samples || 0)],
        ["Retries", Number(missteps.retry_total || 0)],
        ["Landing failures", Number(missteps.landing_failures || 0)],
        ["Observed hosts", Number(tuning.observed_repo_count || 0)],
        ["High severity", Number(severityCounts.high || 0)],
      ];

      document.getElementById("tuning-copy").textContent = recommendation.summary || "No unattended tuning summary is available yet.";
      document.getElementById("tuning-summary").textContent = `${Number(tuning.observed_repo_count || 0)}/${Number(tuning.tracked_repo_count || 0)} tracked hosts observed`;
      document.getElementById("tuning-stats").innerHTML = stats.map(([label, value]) => `
        <div class="stat-card">
          <span class="stat-label">${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `).join("");

      const focusChips = focusRows.length
        ? focusRows.map((row) => chip(`${row.focus} (${row.count})`, "subtle"))
        : [chip(recommendation.focus || "no-focus", "subtle")];
      document.getElementById("tuning-focuses").innerHTML = focusChips.join("");

      document.getElementById("tuning-hosts").innerHTML = hosts.length
        ? hosts.map((host) => {
            const counts = host.counts || {};
            const finding = host.top_finding || {};
            const hostMeta = [
              host.observed_at ? `Observed ${formatTimestamp(host.observed_at)}` : "Observation pending",
              host.tune_focus ? `Focus ${host.tune_focus}` : "",
              `Ready ${Number(counts.ready || 0)}`,
              `Claimed ${Number(counts.claimed || 0)}`,
              `Waiting ${Number(counts.waiting || 0)}`,
              `Done ${Number(counts.done || 0)}`,
            ].filter(Boolean);
            return `
              <article class="tuning-host-card">
                <div class="result-top">
                  <div>
                    <h3>${escapeHtml(host.project_name || "Unknown host")}</h3>
                    <p class="task-summary">${escapeHtml(host.tune_summary || "No tune summary recorded.")}</p>
                  </div>
                  <div class="result-chips">
                    ${host.finding_total ? chip(`${host.finding_total} findings`, finding.severity || "subtle") : chip("No findings", "complete")}
                  </div>
                </div>
                <div class="task-meta">${hostMeta.map((row) => `<span>${escapeHtml(row)}</span>`).join("")}</div>
                ${host.project_root ? `<p class="mono">${escapeHtml(host.project_root)}</p>` : ""}
                ${finding.finding ? `<p class="task-summary">${escapeHtml(finding.finding)}</p>` : ""}
              </article>
            `;
          }).join("")
        : `<div class="status-next-line"><strong>No tracked host observations</strong><span>Run installs observe after host updates to populate unattended tuning data.</span></div>`;
    }

    function detailBlock(label, content, options = {}) {
      if (!content) {
        return "";
      }
      const className = options.wide ? "detail-block wide" : "detail-block";
      return `
        <section class="${className}">
          <strong>${escapeHtml(label)}</strong>
          ${content}
        </section>
      `;
    }

    function listBlock(items) {
      if (!Array.isArray(items) || !items.length) {
        return "";
      }
      return `<ul>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
    }

    function paragraphBlock(text) {
      if (!text) {
        return "";
      }
      return `<p>${escapeHtml(text)}</p>`;
    }

    function preBlock(text, className = "detail-pre") {
      if (!text) {
        return "";
      }
      return `<pre class="${escapeHtml(className)}">${escapeHtml(text)}</pre>`;
    }

    function formatActivityTimestamp(value) {
      if (!value) {
        return "";
      }
      const parsed = Date.parse(String(value));
      if (Number.isNaN(parsed)) {
        return String(value);
      }
      const row = new Date(parsed);
      const pad = (item) => String(item).padStart(2, "0");
      return `${row.getFullYear()}-${pad(row.getMonth() + 1)}-${pad(row.getDate())} ${pad(row.getHours())}:${pad(row.getMinutes())}:${pad(row.getSeconds())}`;
    }

    function detailList(entries) {
      const rows = entries.filter(Boolean);
      if (!rows.length) {
        return "";
      }
      return `<ul>${rows.map((row) => `<li>${escapeHtml(row)}</li>`).join("")}</ul>`;
    }

    function commitBlock(task) {
      const parts = [];
      const commitEntry = (label, commit, shortCommit, url, message) => {
        if (!commit && !message) {
          return "";
        }
        const labelText = shortCommit || commit || "";
        const chunk = [];
        if (url) {
          chunk.push(`<a class="text-link mono" href="${escapeHtml(url)}">${escapeHtml(labelText ? `${label} ${labelText}` : label)}</a>`);
        } else if (commit) {
          chunk.push(`<p class="mono">${escapeHtml(labelText ? `${label} ${labelText}` : commit)}</p>`);
        }
        if (message) {
          chunk.push(preBlock(message, "detail-pre detail-pre-compact"));
        }
        return chunk.join("");
      };
      parts.push(commitEntry("Task Commit", task.task_commit, task.task_commit_short, task.task_commit_url, task.task_commit_message));
      parts.push(commitEntry("Landed Commit", task.landed_commit, task.landed_commit_short, task.landed_commit_url, task.landed_commit_message));
      return parts.filter(Boolean).join("");
    }

    function activityList(entries) {
      const rows = entries.filter((row) => row && (row.at || row.actor || row.message));
      if (!rows.length) {
        return "";
      }
      return `
        <div class="activity-list">
          ${rows.map((row) => `
            <div class="activity-row">
              <span class="mono">${escapeHtml(formatActivityTimestamp(row.at))}</span>
              <span class="mono">${escapeHtml(row.actor || "")}</span>
              <span>${escapeHtml(row.message || "")}</span>
            </div>
          `).join("")}
        </div>
      `;
    }

    function renderTaskCard(task) {
      const tone = taskTone(task);
      const laneMeta = [
        task.lane_title || "",
        task.wave != null ? `Wave ${task.wave}` : "",
        task.total_compute_label ? `Compute ${task.total_compute_label}` : "",
        task.created_at ? `Created ${formatActivityTimestamp(task.created_at)}` : "",
        task.updated_at ? `Updated ${formatActivityTimestamp(task.updated_at)}` : "",
        task.claimed_at ? `Claimed ${formatActivityTimestamp(task.claimed_at)}` : "",
        task.task_commit_short ? `Commit ${task.task_commit_short}` : task.landed_commit_short ? `Commit ${task.landed_commit_short}` : "",
      ].filter(Boolean);
      const dependency = Array.isArray(task.predecessor_ids) && task.predecessor_ids.length
        ? `Depends on ${task.predecessor_ids.join(", ")}`
        : "Lane opener";
      return `
        <article class="task-card tone-${escapeHtml(tone)}"${interactiveCardAttributes(task.id)}>
          <div class="task-card-top">
            <div class="task-id-group">
              <span class="task-code">${escapeHtml(task.id)}</span>
              ${renderStatusChipRows(task.card_status_chips)}
            </div>
            ${task.priority ? `<span class="mini-chip">${escapeHtml(task.priority)}</span>` : ""}
          </div>
          <h3 class="task-title">${escapeHtml(task.title)}</h3>
          <p class="task-summary">${escapeHtml(taskSummary(task) || "No current summary recorded.")}</p>
          <div class="task-meta">${laneMeta.map((row) => `<span>${escapeHtml(row)}</span>`).join("")}</div>
          <div class="task-dependency">${escapeHtml(dependency)}</div>
        </article>
      `;
    }

    function renderLaneColumn(lane) {
      return `
        <section class="lane-column" data-lane-id="${escapeHtml(lane.id)}">
          <div class="lane-head">
            <span class="lane-phase">${escapeHtml(lane.wave == null ? "Unassigned" : `Wave ${lane.wave}`)}</span>
            <h3>${escapeHtml(lane.title)}</h3>
            <div class="lane-meta">
              <span>${escapeHtml(pluralize(lane.tasks.length, "task"))}</span>
            </div>
          </div>
          <div class="lane-stack">
            ${lane.tasks.map(renderTaskCard).join("")}
          </div>
        </section>
      `;
    }

    function renderExecutionMap() {
      const waveRows = groupedWaveRows(boardTasks);
      document.getElementById("board-summary").textContent = boardTasks.length
        ? `${pluralize(waveRows.length, "wave")} · ${pluralize(laneRows(boardTasks).length, "lane")} · ${pluralize(boardTasks.length, "task")}`
        : "No active execution map";
      document.getElementById("lane-board").innerHTML = waveRows.length
        ? waveRows.map((waveRow) => `
            <section class="wave-section">
              <div class="wave-head">
                <h3>${escapeHtml(waveRow.id)}</h3>
                <div class="wave-meta">
                  <span>${escapeHtml(pluralize(waveRow.lanes.length, "lane"))}</span>
                  <span>${escapeHtml(pluralize(waveRow.lanes.reduce((count, lane) => count + lane.tasks.length, 0), "task"))}</span>
                </div>
              </div>
              <div class="wave-grid">
                ${waveRow.lanes.map(renderLaneColumn).join("")}
              </div>
            </section>
          `).join("")
        : `<div class="empty">No active lanes remain in the current execution map.</div>`;
      document.getElementById("inbox-link").href = snapshot.links?.inbox || "#";
    }

    function completionStamp(task) {
      return task.completed_at || task.latest_result_at || task.latest_run_at || "";
    }

    function completionEpoch(task) {
      const value = completionStamp(task);
      const parsed = Date.parse(String(value || ""));
      return Number.isNaN(parsed) ? 0 : parsed;
    }

    function renderCompletedCard(task) {
      const meta = [
        completionStamp(task) ? formatTimestamp(completionStamp(task)) : "",
        task.latest_result_actor ? `Actor ${task.latest_result_actor}` : "",
        task.total_compute_label ? `Compute ${task.total_compute_label}` : "",
      ].filter(Boolean);
      return `
        <article class="result-card tone-complete"${interactiveCardAttributes(task.id)}>
          <div class="result-top">
            <span class="task-code">${escapeHtml(task.id)}</span>
            <div class="result-chips">
              ${renderStatusChipRows(task.card_status_chips)}
            </div>
          </div>
          <h3 class="result-title">${escapeHtml(task.title)}</h3>
          <p>${escapeHtml(taskSummary(task) || "Completed task with no additional summary.")}</p>
          <div class="result-meta">${meta.map((row) => `<span>${escapeHtml(row)}</span>`).join("")}</div>
        </article>
      `;
    }

    function renderCompletedPanel() {
      document.getElementById("completed-copy").textContent =
        "Completed work stays visible here.";
      const visibleCompleted = completedTasks.slice(0, 60);
      document.getElementById("completed-summary").textContent = visibleCompleted.length
        ? `Showing ${visibleCompleted.length} of ${completedTasks.length}`
        : "No completed tasks";
      document.getElementById("completed-history-scroll").innerHTML = visibleCompleted.length
        ? `<div class="results-grid">${visibleCompleted.map(renderCompletedCard).join("")}</div>`
        : `<div class="empty">Completed tasks will appear here once work lands.</div>`;
    }

    function openTaskReader(taskId) {
      const task = allTasks.find((row) => String(row.id) === String(taskId));
      if (!task) {
        return;
      }
      document.getElementById("reader-context").textContent =
        `${task.lane_title || "No lane"} · ${task.epic_title || "No epic"}`;
      document.getElementById("reader-title").textContent = `${task.id} ${task.title}`;
      document.getElementById("reader-statuses").innerHTML = renderStatusChipRows(task.dialog_status_chips);
      document.getElementById("reader-links").innerHTML = renderTaskLinks(task);

      const activityRows = Array.isArray(task.activity) ? task.activity : [];
      const sequenceRows = [
        task.lane_position && task.lane_task_count ? `Step ${task.lane_position} of ${task.lane_task_count} in ${task.lane_title || "lane"}` : "",
        Array.isArray(task.predecessor_ids) && task.predecessor_ids.length ? `Depends on ${task.predecessor_ids.join(", ")}` : "Lane opener"
      ];

      const runtimeRows = [
        task.created_at ? `Created: ${task.created_at}` : "",
        task.updated_at ? `Updated: ${task.updated_at}` : "",
        task.claimed_at ? `Claimed: ${task.claimed_at}` : "",
        task.task_commit_short ? `Task commit: ${task.task_commit_short}` : "",
        task.landed_commit_short ? `Landed commit: ${task.landed_commit_short}` : "",
        task.operator_status_detail ? `Current detail: ${task.operator_status_detail}` : "",
        task.child_agent ? `Child agent: ${task.child_agent}` : "",
        task.target_branch ? `Branch path: ${task.task_branch || "task"} -> ${task.target_branch}` : "",
        task.workspace_mode ? `Workspace mode: ${task.workspace_mode}` : "",
        task.total_compute_label ? `Total compute: ${task.total_compute_label}` : ""
      ];

      document.getElementById("reader-grid").innerHTML = [
        detailBlock("Summary", paragraphBlock(taskSummary(task))),
        detailBlock("What Changed", listBlock(task.latest_result_what_changed), { wide: true }),
        detailBlock("Activity", activityList(activityRows), { wide: true }),
        detailBlock("Sequence", detailList(sequenceRows)),
        detailBlock("Safe First Slice", paragraphBlock(task.safe_first_slice)),
        detailBlock("Runtime", detailList(runtimeRows)),
        detailBlock("Model Response", task.model_response_html || preBlock(task.model_response), { wide: true }),
        detailBlock("Landed Commit", commitBlock(task), { wide: true }),
        detailBlock("Why", paragraphBlock(task.why)),
        detailBlock("Evidence", paragraphBlock(task.evidence)),
        detailBlock("Paths", listBlock(task.paths)),
        detailBlock("Checks", listBlock(task.checks)),
        detailBlock("Docs", listBlock(task.docs)),
        detailBlock("Validation", listBlock(task.latest_result_validation), { wide: true }),
        detailBlock("Residual", listBlock(task.latest_result_residual), { wide: true })
      ].filter(Boolean).join("");
      document.getElementById("reader-dialog").showModal();
    }

    function wireStaticEvents() {
      document.addEventListener("click", (event) => {
        const taskCard = event.target.closest("[data-task-id]");
        if (taskCard && !event.target.closest("a, button")) {
          openTaskReader(taskCard.getAttribute("data-task-id"));
          return;
        }
      });

      document.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") {
          return;
        }
        const taskCard = event.target.closest("[data-task-id]");
        if (!taskCard || event.target.closest("a, button")) {
          return;
        }
        event.preventDefault();
        openTaskReader(taskCard.getAttribute("data-task-id"));
      });
    }

    renderHeader();
    autoReloadEnabled = readAutoReloadPreference();
    startAutoReloadTimer();
    renderStatusPanel();
    renderUnattendedTuningPanel();
    renderExecutionMap();
    renderCompletedPanel();
    wireStaticEvents();
  </script>
</body>
</html>
"""
    html = (
        template.replace("__BLACKDOG_PAGE_TITLE__", f"{title} blackdog backlog")
        .replace("__BLACKDOG_TITLE__", title)
        .replace("__BLACKDOG_STYLES__", stylesheet)
        .replace("__BLACKDOG_SNAPSHOT__", _snapshot_json(snapshot))
    )
    output_path.write_text(html, encoding="utf-8")
    return html
