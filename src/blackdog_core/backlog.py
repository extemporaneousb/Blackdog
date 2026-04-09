from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from typing import Any
import importlib
import json
import re
import subprocess
import textwrap

from .profile import RepoProfile, BlackdogPaths, slugify
from .state import (
    APPROVAL_STATUS_DONE,
    APPROVAL_STATUS_PENDING,
    CLAIM_STATUS_CLAIMED,
    CLAIM_STATUS_DONE,
    approval_is_satisfied,
    atomic_write_text,
    claim_is_active,
    claim_is_done,
    load_events,
    load_inbox,
    load_state,
    load_task_results,
    locked_path,
    normalize_approval_entry,
    normalize_claim_entry,
    save_state,
)


TASK_BLOCK_RE = re.compile(r"```json backlog-task\n(.*?)\n```", re.S)
PLAN_BLOCK_RE = re.compile(r"```json backlog-plan\n(.*?)\n```", re.S)
HEADER_RE = re.compile(
    r"^(Project|Repo root|Generated|Target branch|Target commit|Profile|State file|Events file|Inbox file|Results dir|HTML file):\s+`?(.*?)`?\s*$"
)
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)
TASK_SECTION_RE = re.compile(
    r"^###\s+([A-Z0-9]+-[0-9a-f]+)\s+-\s+(.+?)\n(?P<body>.*?)(?=^###\s+[A-Z0-9]+-[0-9a-f]+\s+-|\Z)",
    re.S | re.M,
)
FENCE_END_RE = re.compile(r"^```\s*$")

VALID_PRIORITIES = {"P1", "P2", "P3"}
VALID_RISKS = {"low", "medium", "high"}
VALID_EFFORTS = {"S", "M", "L"}
PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
EFFORT_ORDER = {"S": 0, "M": 1, "L": 2}
UNASSIGNED_OBJECTIVE_KEY = "__unassigned__"
UNASSIGNED_OBJECTIVE_TITLE = "Unassigned"
TASK_SHAPING_DEFAULTS = {
    "estimated_elapsed_minutes": None,
    "estimated_active_minutes": None,
    "estimated_touched_paths": [],
    "estimated_validation_minutes": None,
    "estimated_worktrees": 1,
    "estimated_handoffs": 0,
    "parallelizable_groups": 0,
}
EFFORT_TASK_SHAPING_BASELINES = {
    "S": {
        "estimated_elapsed_minutes": 30,
        "estimated_active_minutes": 20,
        "estimated_validation_minutes": 5,
    },
    "M": {
        "estimated_elapsed_minutes": 90,
        "estimated_active_minutes": 55,
        "estimated_validation_minutes": 15,
    },
    "L": {
        "estimated_elapsed_minutes": 180,
        "estimated_active_minutes": 105,
        "estimated_validation_minutes": 25,
    },
}
RUNTIME_SNAPSHOT_SCHEMA_VERSION = 1


class BacklogError(RuntimeError):
    pass


def _coerce_optional_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise BacklogError(f"task_shaping.{field} must be a non-negative integer or null")
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise BacklogError(f"task_shaping.{field} must be a non-negative integer or null")
        candidate = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            candidate = int(text)
        except ValueError as exc:
            raise BacklogError(f"task_shaping.{field} must be a non-negative integer or null") from exc
    else:
        raise BacklogError(f"task_shaping.{field} must be a non-negative integer or null")
    if candidate < 0:
        raise BacklogError(f"task_shaping.{field} must be a non-negative integer or null")
    return candidate


def _coerce_task_shaping_touched_paths(value: Any, fallback_paths: list[str]) -> list[str]:
    if value is None:
        raw_values: list[Any] = list(fallback_paths)
    elif isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, (list, tuple)):
        raw_values = list(value)
    else:
        raise BacklogError("task_shaping.estimated_touched_paths must be an array of paths or null")

    normalized = []
    for item in raw_values:
        if not isinstance(item, str):
            raise BacklogError("task_shaping.estimated_touched_paths must contain non-empty strings")
        text = item.strip()
        if text:
            normalized.append(text)
    return _unique_ordered(normalized)


def _coerce_task_shaping(task_shaping: dict[str, Any] | None, *, fallback_paths: list[str]) -> dict[str, Any]:
    if task_shaping is None:
        payload = {}
    elif isinstance(task_shaping, dict):
        payload = dict(task_shaping)
    else:
        raise BacklogError("task_shaping must be an object")
    touched_paths = _coerce_task_shaping_touched_paths(
        payload.get("estimated_touched_paths"),
        fallback_paths=fallback_paths,
    )
    return {
        "estimated_elapsed_minutes": _coerce_optional_int(
            payload.get("estimated_elapsed_minutes"), field="estimated_elapsed_minutes"
        ),
        "estimated_active_minutes": _coerce_optional_int(
            payload.get("estimated_active_minutes"), field="estimated_active_minutes"
        ),
        "estimated_touched_paths": touched_paths,
        "estimated_validation_minutes": _coerce_optional_int(
            payload.get("estimated_validation_minutes"), field="estimated_validation_minutes"
        ),
        "estimated_worktrees": _coerce_optional_int(payload.get("estimated_worktrees"), field="estimated_worktrees")
        if payload.get("estimated_worktrees") is not None
        else int(TASK_SHAPING_DEFAULTS["estimated_worktrees"]),
        "estimated_handoffs": _coerce_optional_int(payload.get("estimated_handoffs"), field="estimated_handoffs")
        if payload.get("estimated_handoffs") is not None
        else int(TASK_SHAPING_DEFAULTS["estimated_handoffs"]),
        "parallelizable_groups": _coerce_optional_int(payload.get("parallelizable_groups"), field="parallelizable_groups")
        if payload.get("parallelizable_groups") is not None
        else int(TASK_SHAPING_DEFAULTS["parallelizable_groups"]),
        **{key: value for key, value in payload.items() if key not in TASK_SHAPING_DEFAULTS},
    }


def _rounded_task_minutes(value: int | float | None) -> int | None:
    if value is None:
        return None
    return max(5, int(round(float(value) / 5.0) * 5))


def _median_int(values: list[int | float | None]) -> int | None:
    filtered = sorted(int(value) for value in values if value is not None)
    if not filtered:
        return None
    middle = len(filtered) // 2
    if len(filtered) % 2:
        return filtered[middle]
    return int(round((filtered[middle - 1] + filtered[middle]) / 2.0))


@dataclass(frozen=True)
class TaskNarrative:
    why: str
    evidence: str
    affected_paths: tuple[str, ...]


@dataclass(frozen=True)
class BacklogTask:
    payload: dict[str, Any]
    narrative: TaskNarrative
    epic_title: str | None
    lane_id: str | None
    lane_title: str | None
    wave: int | None
    lane_order: int | None
    lane_position: int | None
    predecessor_ids: tuple[str, ...]

    @property
    def id(self) -> str:
        return str(self.payload["id"])

    @property
    def title(self) -> str:
        return str(self.payload["title"])


@dataclass(frozen=True)
class BacklogSnapshot:
    raw_text: str
    headers: dict[str, str]
    sections: dict[str, list[str]]
    tasks: dict[str, BacklogTask]
    plan: dict[str, Any]


@dataclass(frozen=True)
class RuntimeArtifacts:
    backlog: BacklogSnapshot
    state: dict[str, Any]
    events: list[dict[str, Any]]
    inbox: list[dict[str, Any]]
    results: list[dict[str, Any]]
    reconcile_report: dict[str, Any]
    strict_validation: dict[str, Any] | None = None


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run_git(project_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise BacklogError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def current_branch(project_root: Path) -> str:
    try:
        return run_git(project_root, "rev-parse", "--abbrev-ref", "HEAD")
    except BacklogError:
        try:
            return run_git(project_root, "symbolic-ref", "--short", "HEAD")
        except BacklogError:
            return "unknown"


def current_commit(project_root: Path) -> str:
    try:
        return run_git(project_root, "rev-parse", "HEAD")
    except BacklogError:
        return "uncommitted"


def make_task_id(profile: RepoProfile, *, bucket: str, title: str, paths: list[str]) -> str:
    normalized_paths = sorted({path.strip() for path in paths if path.strip()})
    payload = "\n".join([bucket.strip(), " ".join(title.split()), *normalized_paths]).encode("utf-8")
    digest = sha1(payload).hexdigest()[: profile.id_digest_length]
    return f"{profile.id_prefix}-{digest}"


def _extract_json_blocks(text: str, fence_name: str) -> list[dict[str, Any]]:
    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    idx = 0
    fence_re = re.compile(rf"^```json\s+{re.escape(fence_name)}\s*$")
    while idx < len(lines):
        if not fence_re.match(lines[idx]):
            idx += 1
            continue
        idx += 1
        block_lines: list[str] = []
        while idx < len(lines) and not FENCE_END_RE.match(lines[idx]):
            block_lines.append(lines[idx])
            idx += 1
        if idx >= len(lines):
            raise BacklogError(f"Unterminated {fence_name} block")
        idx += 1
        raw = "\n".join(block_lines).strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BacklogError(f"Invalid JSON in {fence_name} block: {exc}") from exc
        if not isinstance(payload, dict):
            raise BacklogError(f"{fence_name} block payload must be an object")
        blocks.append(payload)
    return blocks


def _parse_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in text.splitlines()[:40]:
        match = HEADER_RE.match(line.strip())
        if match:
            headers[match.group(1)] = match.group(2)
    return headers


def _extract_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        match = SECTION_RE.match(raw)
        if match:
            current = match.group(1).strip()
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(raw.rstrip())
    for key, values in list(sections.items()):
        start = 0
        end = len(values)
        while start < end and not values[start].strip():
            start += 1
        while end > start and not values[end - 1].strip():
            end -= 1
        sections[key] = values[start:end]
    return sections


def _parse_narratives(text: str) -> dict[str, TaskNarrative]:
    narratives: dict[str, TaskNarrative] = {}
    for match in TASK_SECTION_RE.finditer(text):
        task_id = match.group(1)
        body = match.group("body").split("```json backlog-task", 1)[0]
        why = ""
        evidence = ""
        affected_paths: list[str] = []
        for raw in body.splitlines():
            line = raw.strip()
            if line.startswith("Why it matters:"):
                why = line.partition(":")[2].strip()
            elif line.startswith("Evidence:"):
                evidence = line.partition(":")[2].strip()
            elif line.startswith("Affected paths:"):
                suffix = line.partition(":")[2].strip()
                affected_paths = [part.strip(" `.") for part in suffix.split(",") if part.strip()]
        narratives[task_id] = TaskNarrative(
            why=why,
            evidence=evidence,
            affected_paths=tuple(affected_paths),
        )
    return narratives


def validate_task_payload(task: dict[str, Any], profile: RepoProfile) -> None:
    required = {
        "id",
        "title",
        "bucket",
        "priority",
        "risk",
        "effort",
        "paths",
        "checks",
        "docs",
        "requires_approval",
        "approval_reason",
        "safe_first_slice",
    }
    missing = sorted(required - set(task))
    if missing:
        raise BacklogError(f"Task payload missing required keys: {missing}")
    if not str(task["id"]).strip():
        raise BacklogError("Task id must be non-empty")
    if task["bucket"] not in profile.buckets:
        raise BacklogError(f"Unknown bucket {task['bucket']!r}; allowed: {sorted(profile.buckets)}")
    if task["priority"] not in VALID_PRIORITIES:
        raise BacklogError(f"Unknown priority {task['priority']!r}")
    if task["risk"] not in VALID_RISKS:
        raise BacklogError(f"Unknown risk {task['risk']!r}")
    if task["effort"] not in VALID_EFFORTS:
        raise BacklogError(f"Unknown effort {task['effort']!r}")
    for key in ("paths", "checks", "docs"):
        value = task[key]
        if isinstance(value, str):
            task[key] = [value]
            value = task[key]
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise BacklogError(f"{key} must be a list of non-empty strings")
    if "domains" in task:
        domains = task["domains"]
        if isinstance(domains, str):
            task["domains"] = [domains]
            domains = task["domains"]
        if not isinstance(domains, list):
            raise BacklogError("domains must be a list when present")
    if task["requires_approval"] and not str(task["approval_reason"]).strip():
        raise BacklogError("approval_reason is required when requires_approval=true")
    if not str(task["safe_first_slice"]).strip():
        raise BacklogError("safe_first_slice must be non-empty")
    task["task_shaping"] = _coerce_task_shaping(
        task.get("task_shaping"),
        fallback_paths=task["paths"],
    )


def validate_plan_payload(plan: dict[str, Any], *, task_ids: set[str]) -> None:
    if not isinstance(plan, dict):
        raise BacklogError("Plan payload must be an object")
    lanes = plan.get("lanes")
    epics = plan.get("epics")
    if not isinstance(lanes, list):
        raise BacklogError("Plan lanes must be a list")
    if not isinstance(epics, list):
        raise BacklogError("Plan epics must be a list")
    seen_lane_ids: set[str] = set()
    seen_epic_ids: set[str] = set()
    assigned_tasks: set[str] = set()
    for epic in epics:
        if not isinstance(epic, dict):
            raise BacklogError("Epic entries must be objects")
        epic_id = str(epic.get("id") or "").strip()
        if not epic_id or epic_id in seen_epic_ids:
            raise BacklogError(f"Invalid or duplicate epic id: {epic_id!r}")
        seen_epic_ids.add(epic_id)
        for task_id in epic.get("task_ids", []):
            if task_id not in task_ids:
                raise BacklogError(f"Epic {epic_id} references unknown task {task_id}")
    for lane in lanes:
        if not isinstance(lane, dict):
            raise BacklogError("Lane entries must be objects")
        lane_id = str(lane.get("id") or "").strip()
        if not lane_id or lane_id in seen_lane_ids:
            raise BacklogError(f"Invalid or duplicate lane id: {lane_id!r}")
        seen_lane_ids.add(lane_id)
        try:
            int(lane["wave"])
        except Exception as exc:
            raise BacklogError(f"Lane {lane_id} has invalid wave") from exc
        for task_id in lane.get("task_ids", []):
            if task_id not in task_ids:
                raise BacklogError(f"Lane {lane_id} references unknown task {task_id}")
            if task_id in assigned_tasks:
                raise BacklogError(f"Task {task_id} is assigned to multiple lanes")
            assigned_tasks.add(task_id)


def load_backlog(paths: BlackdogPaths, profile: RepoProfile) -> BacklogSnapshot:
    if not paths.backlog_file.exists():
        raise BacklogError(f"Backlog file does not exist: {paths.backlog_file}")
    text = paths.backlog_file.read_text(encoding="utf-8")
    headers = _parse_headers(text)
    plan_blocks = _extract_json_blocks(text, "backlog-plan")
    if len(plan_blocks) > 1:
        raise BacklogError("Expected at most one backlog-plan block")
    plan = plan_blocks[0] if plan_blocks else {"epics": [], "lanes": []}
    narratives = _parse_narratives(text)
    epics = {str(task_id): str(epic.get("title") or "") for epic in plan.get("epics", []) for task_id in epic.get("task_ids", [])}
    lane_positions: dict[str, tuple[str, str, int, int, int, tuple[str, ...]]] = {}
    lane_order_by_id = {str(lane.get("id")): idx for idx, lane in enumerate(plan.get("lanes", []))}
    for lane in sorted(
        plan.get("lanes", []),
        key=lambda item: (int(item.get("wave", 0)), lane_order_by_id.get(str(item.get("id")), 0)),
    ):
        lane_id = str(lane.get("id") or "")
        lane_title = str(lane.get("title") or "")
        wave = int(lane.get("wave", 0))
        task_ids = tuple(str(task_id) for task_id in lane.get("task_ids", []))
        for index, task_id in enumerate(task_ids):
            lane_positions[task_id] = (
                lane_id,
                lane_title,
                wave,
                lane_order_by_id.get(lane_id, 0),
                index,
                task_ids[:index],
            )
    tasks: dict[str, BacklogTask] = {}
    for payload in _extract_json_blocks(text, "backlog-task"):
        validate_task_payload(payload, profile)
        task_id = str(payload["id"])
        lane_id, lane_title, wave, lane_order, lane_position, predecessors = lane_positions.get(
            task_id,
            (None, None, None, None, None, ()),
        )
        tasks[task_id] = BacklogTask(
            payload=payload,
            narrative=narratives.get(task_id, TaskNarrative("", "", ())),
            epic_title=epics.get(task_id),
            lane_id=lane_id,
            lane_title=lane_title,
            wave=wave,
            lane_order=lane_order,
            lane_position=lane_position,
            predecessor_ids=tuple(predecessors),
        )
    validate_plan_payload(plan, task_ids=set(tasks))
    return BacklogSnapshot(
        raw_text=text,
        headers=headers,
        sections=_extract_sections(text),
        tasks=tasks,
        plan=plan,
    )


def task_done(task_id: str, state: dict[str, Any]) -> bool:
    entry = state.get("task_claims", {}).get(task_id)
    return claim_is_done(entry)


def approval_satisfied(task: BacklogTask, state: dict[str, Any]) -> bool:
    if not bool(task.payload.get("requires_approval")):
        return True
    entry = state.get("approval_tasks", {}).get(task.id)
    return approval_is_satisfied(entry)


def active_claim_owner(task_id: str, state: dict[str, Any]) -> str | None:
    entry = state.get("task_claims", {}).get(task_id)
    if not isinstance(entry, dict) or not claim_is_active(entry):
        return None
    return str(entry.get("claimed_by") or "another-agent")


def blocking_reason(task: BacklogTask, snapshot: BacklogSnapshot, state: dict[str, Any], *, allow_high_risk: bool) -> str | None:
    if task_done(task.id, state):
        return "already done"
    owner = active_claim_owner(task.id, state)
    if owner:
        return f"claimed by {owner}"
    if not approval_satisfied(task, state):
        return "approval required"
    if task.wave is not None:
        for lane in snapshot.plan.get("lanes", []):
            if int(lane.get("wave", 0)) >= task.wave:
                continue
            for earlier_id in lane.get("task_ids", []):
                if not task_done(str(earlier_id), state):
                    return f"waiting for lower-wave task {earlier_id}"
    for predecessor_id in task.predecessor_ids:
        if not task_done(predecessor_id, state):
            return f"waiting for predecessor {predecessor_id}"
    if str(task.payload.get("risk") or "").strip() == "high" and not allow_high_risk:
        return "high-risk item"
    return None


def classify_task_status(task: BacklogTask, snapshot: BacklogSnapshot, state: dict[str, Any], *, allow_high_risk: bool) -> tuple[str, str]:
    if task_done(task.id, state):
        entry = state.get("task_claims", {}).get(task.id) or {}
        detail = str(entry.get("completed_by") or "?")
        completed_at = str(entry.get("completed_at") or "?")
        return CLAIM_STATUS_DONE, f"{detail} @ {completed_at}"
    owner = active_claim_owner(task.id, state)
    if owner:
        return CLAIM_STATUS_CLAIMED, f"{owner} claimed the task"
    blocker = blocking_reason(task, snapshot, state, allow_high_risk=allow_high_risk)
    if blocker == "approval required":
        return "approval", blocker
    if blocker == "high-risk item":
        return "high-risk", blocker
    if blocker:
        return "waiting", blocker
    return "ready", "claimable now"


def reconcile_state_for_backlog(state: dict[str, Any], snapshot: BacklogSnapshot) -> tuple[dict[str, Any], dict[str, Any]]:
    before = deepcopy(state if isinstance(state, dict) else {})
    if not isinstance(state, dict):
        state = {}
    approvals = state.setdefault("approval_tasks", {})
    claims = state.setdefault("task_claims", {})
    if not isinstance(approvals, dict):
        approvals = {}
        state["approval_tasks"] = approvals
    if not isinstance(claims, dict):
        claims = {}
        state["task_claims"] = claims
    seen_date = datetime.now().astimezone().date().isoformat()
    active_task_ids = set(snapshot.tasks)
    for task_id in list(approvals):
        task = snapshot.tasks.get(task_id)
        if task is None or not bool(task.payload.get("requires_approval")):
            approvals.pop(task_id, None)
    for task_id in list(claims):
        if task_id not in active_task_ids:
            claims.pop(task_id, None)
    for task in snapshot.tasks.values():
        claim_entry = claims.get(task.id)
        if isinstance(claim_entry, dict):
            normalized_claim = dict(claim_entry)
            normalized_claim["title"] = task.title
            normalized_claim["bucket"] = task.payload["bucket"]
            normalized_claim["paths"] = task.payload["paths"]
            normalized_claim["priority"] = task.payload["priority"]
            normalized_claim["risk"] = task.payload["risk"]
            claims[task.id] = normalize_claim_entry(task.id, normalized_claim, state_file=Path("<memory>"))
        if not bool(task.payload.get("requires_approval")):
            continue
        entry = approvals.get(task.id) or {}
        if not isinstance(entry, dict):
            entry = {}
        entry.setdefault("status", APPROVAL_STATUS_PENDING)
        entry.setdefault("first_seen", seen_date)
        entry["last_seen"] = seen_date
        entry["title"] = task.title
        entry["bucket"] = task.payload["bucket"]
        entry["paths"] = task.payload["paths"]
        entry["approval_reason"] = task.payload["approval_reason"]
        if task_done(task.id, state):
            entry["status"] = APPROVAL_STATUS_DONE
        approvals[task.id] = normalize_approval_entry(task.id, entry, state_file=Path("<memory>"))
    before_approvals = before.get("approval_tasks") if isinstance(before.get("approval_tasks"), dict) else {}
    before_claims = before.get("task_claims") if isinstance(before.get("task_claims"), dict) else {}
    runtime_claim_fields = {
        "claimed_pid",
        "claimed_process_missing_scans",
        "claimed_process_last_seen_at",
        "claimed_process_last_checked_at",
    }
    report = {
        "state_reconciled": before != state,
        "approval_rows": len(approvals),
        "claim_rows": len(claims),
        "active_claims": sum(1 for entry in claims.values() if claim_is_active(entry)),
        "done_claims": sum(1 for entry in claims.values() if claim_is_done(entry)),
        "pruned_approval_rows": len(set(before_approvals) - set(approvals)),
        "pruned_claim_rows": len(set(before_claims) - set(claims)),
        "seeded_approval_rows": len(set(approvals) - set(before_approvals)),
        "promoted_done_approvals": sum(
            1
            for task_id, entry in approvals.items()
            if str(entry.get("status") or "") == APPROVAL_STATUS_DONE
            and str((before_approvals.get(task_id) or {}).get("status") or "") != APPROVAL_STATUS_DONE
        ),
        "updated_claim_rows": sum(
            1
            for task_id in set(before_claims) & set(claims)
            if before_claims.get(task_id) != claims.get(task_id)
        ),
        "claim_runtime_fields_dropped": sum(
            1
            for task_id in set(before_claims) & set(claims)
            if any(field in (before_claims.get(task_id) or {}) for field in runtime_claim_fields)
            and all(field not in (claims.get(task_id) or {}) for field in runtime_claim_fields)
        ),
    }
    return state, report


def sync_state_for_backlog(state: dict[str, Any], snapshot: BacklogSnapshot) -> dict[str, Any]:
    reconciled, _ = reconcile_state_for_backlog(state, snapshot)
    return reconciled


def _strict_runtime_validation(
    snapshot: BacklogSnapshot,
    *,
    events: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    task_ids = set(snapshot.tasks)
    task_result_event_keys: set[tuple[str, str, str]] = set()
    task_result_events = 0

    for message in messages:
        task_id = str(message.get("task_id") or "").strip()
        if task_id and task_id not in task_ids:
            issues.append(
                {
                    "kind": "inbox_unknown_task",
                    "task_id": task_id,
                    "message_id": str(message.get("message_id") or ""),
                    "status": str(message.get("status") or ""),
                }
            )

    for event in events:
        if str(event.get("type") or "") != "task_result":
            continue
        task_result_events += 1
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        task_id = str(event.get("task_id") or "").strip()
        run_id = str(payload.get("run_id") or "").strip()
        result_file = str(payload.get("result_file") or "").strip()
        if not run_id or not result_file:
            issues.append(
                {
                    "kind": "task_result_event_missing_fields",
                    "task_id": task_id,
                    "run_id": run_id,
                    "result_file": result_file,
                }
            )
            continue
        result_path = Path(result_file)
        if not result_path.exists():
            issues.append(
                {
                    "kind": "task_result_event_missing_file",
                    "task_id": task_id,
                    "run_id": run_id,
                    "result_file": result_file,
                }
            )
            continue
        if task_id and result_path.parent.name != task_id:
            issues.append(
                {
                    "kind": "task_result_event_task_mismatch",
                    "task_id": task_id,
                    "run_id": run_id,
                    "result_file": result_file,
                }
            )
            continue
        task_result_event_keys.add((task_id, run_id, result_file))

    for row in results:
        task_id = str(row.get("task_id") or "").strip()
        run_id = str(row.get("run_id") or "").strip()
        result_file = str(row.get("result_file") or "").strip()
        if task_id and task_id not in task_ids:
            issues.append(
                {
                    "kind": "result_unknown_task",
                    "task_id": task_id,
                    "run_id": run_id,
                    "result_file": result_file,
                }
            )
        if result_file:
            result_path = Path(result_file)
            if task_id and result_path.parent.name != task_id:
                issues.append(
                    {
                        "kind": "result_file_task_mismatch",
                        "task_id": task_id,
                        "run_id": run_id,
                        "result_file": result_file,
                    }
                )
        if (task_id, run_id, result_file) not in task_result_event_keys:
            issues.append(
                {
                    "kind": "result_missing_task_result_event",
                    "task_id": task_id,
                    "run_id": run_id,
                    "result_file": result_file,
                }
            )

    issues.sort(
        key=lambda row: (
            str(row.get("kind") or ""),
            str(row.get("task_id") or ""),
            str(row.get("message_id") or ""),
            str(row.get("run_id") or ""),
            str(row.get("result_file") or ""),
        )
    )
    return {
        "task_result_events": task_result_events,
        "issue_count": len(issues),
        "issue_count_by_kind": dict(Counter(str(row.get("kind") or "unknown") for row in issues)),
        "issues": issues,
    }


def _strict_validation_error(report: dict[str, Any]) -> str:
    issues = list(report.get("issues") or [])
    preview: list[str] = []
    for issue in issues[:3]:
        detail = (
            str(issue.get("message_id") or "")
            or str(issue.get("task_id") or "")
            or str(issue.get("result_file") or "")
            or str(issue.get("run_id") or "")
            or "unknown"
        )
        preview.append(f"{issue.get('kind')} ({detail})")
    suffix = f"; +{len(issues) - len(preview)} more" if len(issues) > len(preview) else ""
    return f"Strict validation failed with {len(issues)} issue(s): {'; '.join(preview)}{suffix}"


def load_runtime_artifacts(
    profile: RepoProfile,
    *,
    snapshot: BacklogSnapshot | None = None,
    event_limit: int | None = None,
    strict_validate: bool = False,
) -> RuntimeArtifacts:
    snapshot = snapshot or load_backlog(profile.paths, profile)
    original_state = load_state(profile.paths.state_file)
    state, reconcile = reconcile_state_for_backlog(deepcopy(original_state), snapshot)
    if state != original_state:
        save_state(profile.paths.state_file, state)
    events = load_events(profile.paths, limit=event_limit)
    messages = load_inbox(profile.paths)
    results = load_task_results(profile.paths)
    strict_validation = None
    if strict_validate:
        strict_validation = _strict_runtime_validation(
            snapshot,
            events=load_events(profile.paths),
            messages=messages,
            results=results,
        )
        if strict_validation["issue_count"]:
            raise BacklogError(_strict_validation_error(strict_validation))
    return RuntimeArtifacts(
        backlog=snapshot,
        state=state,
        events=events,
        inbox=messages,
        results=results,
        reconcile_report=reconcile,
        strict_validation=strict_validation,
    )


def next_runnable_tasks(
    snapshot: BacklogSnapshot,
    state: dict[str, Any],
    *,
    allow_high_risk: bool,
    limit: int,
) -> list[BacklogTask]:
    unfinished = [task for task in snapshot.tasks.values() if not task_done(task.id, state)]
    if not unfinished:
        return []
    planned = [task for task in unfinished if task.wave is not None]
    if planned:
        active_wave = min(int(task.wave) for task in planned)
        candidates = [task for task in planned if int(task.wave) == active_wave]
        first_by_lane: list[BacklogTask] = []
        seen_lanes: set[str] = set()
        for task in sorted(candidates, key=lambda item: (int(item.lane_order or 0), item.id)):
            lane_id = str(task.lane_id)
            if lane_id in seen_lanes:
                continue
            if blocking_reason(task, snapshot, state, allow_high_risk=allow_high_risk) is None:
                first_by_lane.append(task)
                seen_lanes.add(lane_id)
        return first_by_lane[:limit]
    ready = [
        task
        for task in sorted(
            unfinished,
            key=lambda item: (
                PRIORITY_ORDER[str(item.payload.get("priority") or "P3")],
                RISK_ORDER[str(item.payload.get("risk") or "high")],
                EFFORT_ORDER[str(item.payload.get("effort") or "L")],
                item.id,
            ),
        )
        if blocking_reason(task, snapshot, state, allow_high_risk=allow_high_risk) is None
    ]
    return ready[:limit]


def _section_items(lines: list[str]) -> list[str]:
    items: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        if re.match(r"^\d+\.\s+", line):
            items.append(re.sub(r"^\d+\.\s+", "", line))
        elif line.startswith("- "):
            items.append(line[2:].strip())
    return items


def _parse_objectives(snapshot: BacklogSnapshot) -> list[dict[str, str]]:
    raw_items = _section_items(snapshot.sections.get("Objectives", []))
    output: list[dict[str, str]] = []
    for index, item in enumerate(raw_items, start=1):
        if ":" in item:
            objective_id, _, title = item.partition(":")
            output.append({"id": objective_id.strip() or f"OBJ-{index}", "title": title.strip()})
        else:
            output.append({"id": f"OBJ-{index}", "title": item})
    return output


def _objective_row_key(objective_id: str) -> str:
    normalized = objective_id.strip()
    return normalized or UNASSIGNED_OBJECTIVE_KEY


def _latest_result_index(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest_by_task: dict[str, dict[str, Any]] = {}
    for row in results:
        task_id = str(row.get("task_id") or "").strip()
        if not task_id or task_id in latest_by_task:
            continue
        latest_by_task[task_id] = {
            "status": row.get("status"),
            "recorded_at": row.get("recorded_at"),
            "actor": row.get("actor"),
        }
    return latest_by_task


def build_runtime_summary(
    profile: RepoProfile,
    snapshot: BacklogSnapshot,
    state: dict[str, Any],
    *,
    events: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    results: list[dict[str, Any]],
    allow_high_risk: bool = False,
) -> dict[str, Any]:
    counts = {"ready": 0, "claimed": 0, "done": 0, "approval": 0, "high-risk": 0, "waiting": 0}
    defined_objectives = _parse_objectives(snapshot)
    objective_rows: dict[str, dict[str, Any]] = {
        _objective_row_key(row["id"]): {
            "key": _objective_row_key(row["id"]),
            "id": row["id"],
            "title": row["title"],
            "task_ids": [],
            "lane_ids": [],
            "lane_titles": [],
            "wave_ids": [],
            "total": 0,
            "done": 0,
            "sort_order": index,
        }
        for index, row in enumerate(defined_objectives)
    }
    include_unlisted_objectives = not objective_rows
    tasks_by_lane: list[dict[str, Any]] = []
    tasks_sorted = sorted(
        snapshot.tasks.values(),
        key=lambda task: (
            task.wave if task.wave is not None else 9999,
            task.lane_order if task.lane_order is not None else 9999,
            task.lane_position if task.lane_position is not None else 9999,
            task.id,
        ),
    )
    for task in tasks_sorted:
        status, detail = classify_task_status(task, snapshot, state, allow_high_risk=allow_high_risk)
        counts[status] += 1
        objective_id = str(task.payload.get("objective") or "").strip()
        objective_key = _objective_row_key(objective_id)
        objective_row = objective_rows.get(objective_key)
        if objective_row is None and include_unlisted_objectives and objective_id:
            objective_row = objective_rows.setdefault(
                objective_key,
                {
                    "key": objective_key,
                    "id": objective_id,
                    "title": objective_id,
                    "task_ids": [],
                    "lane_ids": [],
                    "lane_titles": [],
                    "wave_ids": [],
                    "total": 0,
                    "done": 0,
                    "sort_order": len(objective_rows),
                },
            )
        if objective_row is not None:
            objective_row["task_ids"].append(task.id)
            objective_row["total"] += 1
            if status == "done":
                objective_row["done"] += 1
            lane_title = task.lane_title or "Unplanned"
            if task.lane_id and task.lane_id not in objective_row["lane_ids"]:
                objective_row["lane_ids"].append(task.lane_id)
            if lane_title not in objective_row["lane_titles"]:
                objective_row["lane_titles"].append(lane_title)
            if task.wave is not None and task.wave not in objective_row["wave_ids"]:
                objective_row["wave_ids"].append(task.wave)
        tasks_by_lane.append(
            {
                "id": task.id,
                "title": task.title,
                "status": status,
                "detail": detail,
                "lane_title": task.lane_title or "Unplanned",
                "lane_id": task.lane_id or "unplanned",
                "wave": task.wave,
                "risk": task.payload["risk"],
                "priority": task.payload["priority"],
                "objective": objective_id,
                "domains": task.payload.get("domains", []),
                "safe_first_slice": task.payload["safe_first_slice"],
                "task_shaping": task.payload["task_shaping"],
            }
        )
    lanes: dict[str, dict[str, Any]] = {}
    for row in tasks_by_lane:
        lane = lanes.setdefault(
            row["lane_id"],
            {"id": row["lane_id"], "title": row["lane_title"], "wave": row["wave"], "tasks": []},
        )
        lane["tasks"].append(row)
    ordered_lanes = sorted(lanes.values(), key=lambda row: ((row["wave"] if row["wave"] is not None else 9999), row["title"]))
    ordered_objective_rows = []
    for row in sorted(objective_rows.values(), key=lambda value: (int(value["sort_order"]), str(value["title"]))):
        remaining = max(0, int(row["total"]) - int(row["done"]))
        objective_row = {
            "key": row["key"],
            "id": row["id"],
            "title": row["title"],
            "task_ids": list(row["task_ids"]),
            "lane_ids": list(row["lane_ids"]),
            "lane_titles": list(row["lane_titles"]),
            "wave_ids": list(row["wave_ids"]),
            "total": int(row["total"]),
            "done": int(row["done"]),
            "remaining": remaining,
        }
        if objective_row["task_ids"]:
            ordered_objective_rows.append(objective_row)
    next_rows = [
        {"id": task.id, "title": task.title, "lane": task.lane_title, "wave": task.wave, "risk": task.payload["risk"]}
        for task in next_runnable_tasks(snapshot, state, allow_high_risk=allow_high_risk, limit=8)
    ]
    return {
        "project_name": profile.project_name,
        "headers": snapshot.headers,
        "counts": counts,
        "total": len(snapshot.tasks),
        "next_rows": next_rows,
        "lanes": ordered_lanes,
        "objectives": [
            {"id": row["id"], "title": row["title"], "total": row["total"], "done": row["done"]}
            for row in ordered_objective_rows
        ],
        "objective_rows": ordered_objective_rows,
        "open_messages": [row for row in messages if row.get("status") == "open"],
        "recent_events": events[-10:],
        "recent_results": results[:5],
        "push_objective": _section_items(snapshot.sections.get("Push Objective", [])),
        "release_gates": _section_items(snapshot.sections.get("Release Gates", [])),
    }


def summary_open_messages(
    snapshot: BacklogSnapshot,
    state: dict[str, Any],
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for message in messages:
        if message.get("status") != "open":
            continue
        task_id = str(message.get("task_id") or "").strip()
        if task_id:
            if task_id not in snapshot.tasks:
                continue
            if task_done(task_id, state):
                continue
        sender = str(message.get("sender") or "").strip()
        recipient = str(message.get("recipient") or "").strip()
        tags = {str(tag).strip().lower() for tag in message.get("tags") or []}
        if sender == "blackdog" and (recipient == "supervisor" or recipient.startswith("supervisor/")):
            continue
        if "supervisor-run" in tags:
            continue
        output.append(message)
    return output


def build_plan_snapshot(
    profile: RepoProfile,
    snapshot: BacklogSnapshot,
    state: dict[str, Any],
    *,
    allow_high_risk: bool = False,
) -> dict[str, Any]:
    task_status: dict[str, tuple[str, str]] = {
        task.id: classify_task_status(task, snapshot, state, allow_high_risk=allow_high_risk)
        for task in snapshot.tasks.values()
    }
    lanes: list[dict[str, Any]] = []
    waves: dict[int, dict[str, Any]] = {}
    for lane in snapshot.plan.get("lanes", []):
        lane_id = str(lane.get("id") or "")
        lane_title = str(lane.get("title") or "")
        wave = int(lane.get("wave", 0))
        lane_tasks: list[dict[str, Any]] = []
        status_counts = {"ready": 0, "claimed": 0, "done": 0, "approval": 0, "high-risk": 0, "waiting": 0}
        for task_id in [str(item) for item in lane.get("task_ids", [])]:
            task = snapshot.tasks[task_id]
            status, detail = task_status[task_id]
            status_counts[status] += 1
            lane_tasks.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "status": status,
                    "detail": detail,
                    "epic_title": task.epic_title or "Unplanned",
                    "priority": task.payload["priority"],
                    "risk": task.payload["risk"],
                    "task_shaping": task.payload["task_shaping"],
                }
            )
        lanes.append(
            {
                "id": lane_id,
                "title": lane_title,
                "wave": wave,
                "task_count": len(lane_tasks),
                "status_counts": status_counts,
                "tasks": lane_tasks,
            }
        )
        wave_entry = waves.setdefault(
            wave,
            {"wave": wave, "lane_count": 0, "task_count": 0, "lane_ids": [], "task_ids": []},
        )
        wave_entry["lane_count"] += 1
        wave_entry["task_count"] += len(lane_tasks)
        wave_entry["lane_ids"].append(lane_id)
        wave_entry["task_ids"].extend(task["id"] for task in lane_tasks)

    epics: list[dict[str, Any]] = []
    for epic in snapshot.plan.get("epics", []):
        epic_id = str(epic.get("id") or "")
        epic_title = str(epic.get("title") or "")
        task_ids = [str(item) for item in epic.get("task_ids", [])]
        lane_ids = sorted({str(snapshot.tasks[task_id].lane_id or "unplanned") for task_id in task_ids})
        wave_values = sorted({int(snapshot.tasks[task_id].wave or 0) for task_id in task_ids})
        status_counts = {"ready": 0, "claimed": 0, "done": 0, "approval": 0, "high-risk": 0, "waiting": 0}
        for task_id in task_ids:
            status, _ = task_status[task_id]
            status_counts[status] += 1
        epics.append(
            {
                "id": epic_id,
                "title": epic_title,
                "task_count": len(task_ids),
                "lane_count": len(lane_ids),
                "waves": wave_values,
                "task_ids": task_ids,
                "lane_ids": lane_ids,
                "status_counts": status_counts,
            }
        )

    ordered_lanes = sorted(lanes, key=lambda row: (int(row["wave"]), row["title"], row["id"]))
    ordered_waves = sorted(waves.values(), key=lambda row: int(row["wave"]))
    return {
        "project_name": profile.project_name,
        "counts": {
            "tasks": len(snapshot.tasks),
            "epics": len(epics),
            "lanes": len(ordered_lanes),
            "waves": len(ordered_waves),
        },
        "epics": epics,
        "lanes": ordered_lanes,
        "waves": ordered_waves,
    }


def build_runtime_snapshot(
    profile: RepoProfile,
    snapshot: BacklogSnapshot,
    state: dict[str, Any],
    *,
    messages: list[dict[str, Any]] | None = None,
    results: list[dict[str, Any]] | None = None,
    allow_high_risk: bool = False,
) -> dict[str, Any]:
    message_rows = messages if messages is not None else load_inbox(profile.paths)
    result_rows = results if results is not None else load_task_results(profile.paths)
    summary = build_runtime_summary(
        profile,
        snapshot,
        state,
        events=[],
        messages=message_rows,
        results=result_rows,
        allow_high_risk=allow_high_risk,
    )
    plan = build_plan_snapshot(profile, snapshot, state, allow_high_risk=allow_high_risk)
    objective_titles = {
        str(row.get("id") or ""): str(row.get("title") or "")
        for row in summary.get("objectives", [])
        if str(row.get("id") or "").strip()
    }
    latest_results = _latest_result_index(result_rows)
    tasks: list[dict[str, Any]] = []
    ordered_tasks = sorted(
        snapshot.tasks.values(),
        key=lambda task: (
            task.wave if task.wave is not None else 9999,
            task.lane_order if task.lane_order is not None else 9999,
            task.lane_position if task.lane_position is not None else 9999,
            task.id,
        ),
    )
    for task in ordered_tasks:
        status, detail = classify_task_status(task, snapshot, state, allow_high_risk=allow_high_risk)
        claim_entry = state.get("task_claims", {}).get(task.id) or {}
        approval_entry = state.get("approval_tasks", {}).get(task.id) or {}
        result_info = latest_results.get(task.id, {})
        objective_id = str(task.payload.get("objective") or "").strip()
        tasks.append(
            {
                "id": task.id,
                "title": task.title,
                "status": status,
                "detail": detail,
                "bucket": task.payload["bucket"],
                "priority": task.payload["priority"],
                "risk": task.payload["risk"],
                "effort": task.payload["effort"],
                "objective": objective_id,
                "objective_title": objective_titles.get(objective_id) or objective_id or UNASSIGNED_OBJECTIVE_TITLE,
                "epic_title": task.epic_title,
                "lane_id": task.lane_id,
                "lane_title": task.lane_title,
                "wave": task.wave,
                "domains": list(task.payload.get("domains", [])),
                "safe_first_slice": task.payload["safe_first_slice"],
                "why": str(task.payload.get("why") or task.narrative.why or ""),
                "evidence": str(task.payload.get("evidence") or task.narrative.evidence or ""),
                "paths": list(task.payload.get("paths") or []),
                "checks": list(task.payload.get("checks") or []),
                "docs": list(task.payload.get("docs") or []),
                "task_shaping": task.payload.get("task_shaping"),
                "predecessor_ids": list(task.predecessor_ids),
                "requires_approval": bool(task.payload.get("requires_approval")),
                "approval_reason": str(task.payload.get("approval_reason") or ""),
                "approval_status": str(approval_entry.get("status") or "not_required"),
                "claim_status": str(claim_entry.get("status") or "absent"),
                "claimed_by": claim_entry.get("claimed_by"),
                "claimed_at": claim_entry.get("claimed_at"),
                "released_by": claim_entry.get("released_by"),
                "released_at": claim_entry.get("released_at"),
                "release_note": claim_entry.get("release_note"),
                "completed_by": claim_entry.get("completed_by"),
                "completed_at": claim_entry.get("completed_at"),
                "completion_note": claim_entry.get("completion_note"),
                "latest_result_status": result_info.get("status"),
                "latest_result_at": result_info.get("recorded_at"),
                "latest_result_actor": result_info.get("actor"),
            }
        )
    return {
        "schema_version": RUNTIME_SNAPSHOT_SCHEMA_VERSION,
        "generated_at": now_iso(),
        "project_name": profile.project_name,
        "project_root": str(profile.paths.project_root),
        "control_dir": str(profile.paths.control_dir),
        "profile_file": str(profile.paths.profile_file),
        "headers": dict(snapshot.headers),
        "counts": summary["counts"],
        "total": summary["total"],
        "push_objective": summary["push_objective"],
        "release_gates": summary["release_gates"],
        "objectives": summary["objectives"],
        "next_rows": summary["next_rows"],
        "open_messages": summary["open_messages"],
        "plan": plan,
        "tasks": tasks,
    }


def render_plan_text(view: dict[str, Any]) -> str:
    lines = [
        f"Project: {view['project_name']}",
        f"Plan: {view['counts']['epics']} epics | {view['counts']['lanes']} lanes | {view['counts']['waves']} waves | {view['counts']['tasks']} tasks",
        "",
        "Waves:",
    ]
    if view["waves"]:
        for wave in view["waves"]:
            lines.append(
                f"- Wave {wave['wave']}: {wave['lane_count']} lane(s) | {wave['task_count']} task(s)"
            )
    else:
        lines.append("- No waves defined.")

    lines.extend(["", "Epics:"])
    if view["epics"]:
        for epic in view["epics"]:
            wave_text = ", ".join(str(item) for item in epic["waves"]) or "unplanned"
            lines.append(
                f"- {epic['title']} ({epic['id']}): {epic['task_count']} task(s) | {epic['lane_count']} lane(s) | wave(s) {wave_text}"
            )
    else:
        lines.append("- No epics defined.")

    lines.extend(["", "Lanes:"])
    if view["lanes"]:
        for lane in view["lanes"]:
            lines.append(f"- Wave {lane['wave']} | {lane['title']} ({lane['id']}): {lane['task_count']} task(s)")
            for task in lane["tasks"]:
                lines.append(f"  - {task['id']} [{task['status']}] {task['title']}")
    else:
        lines.append("- No lanes defined.")
    return "\n".join(lines) + "\n"


def render_summary_text(view: dict[str, Any]) -> str:
    lines = [
        f"Project: {view['project_name']}",
        f"Tasks: {view['total']} total | ready {view['counts']['ready']} | claimed {view['counts']['claimed']} | done {view['counts']['done']} | approval {view['counts']['approval']} | waiting {view['counts']['waiting']} | high-risk {view['counts']['high-risk']}",
        "",
        "Next runnable tasks:",
    ]
    if view["next_rows"]:
        for row in view["next_rows"]:
            lines.append(f"- {row['id']} [{row['risk']}] {row['title']}")
    else:
        lines.append("- No runnable tasks.")
    if view["open_messages"]:
        lines.extend(["", "Open inbox messages:"])
        for message in view["open_messages"][:5]:
            lines.append(
                f"- {message['message_id']} {message['sender']} -> {message['recipient']} [{message['kind']}] {message['body']}"
            )
    return "\n".join(lines) + "\n"


def render_backlog_task_block(task_payload: dict[str, Any]) -> str:
    return "```json backlog-task\n" + json.dumps(task_payload, indent=2, sort_keys=False) + "\n```"


def render_backlog_plan_block(plan: dict[str, Any]) -> str:
    return "```json backlog-plan\n" + json.dumps(plan, indent=2, sort_keys=False) + "\n```"


def render_task_section(task_payload: dict[str, Any], *, why: str, evidence: str, affected_paths: list[str]) -> str:
    affected = ", ".join(f"`{path}`" for path in affected_paths)
    return "\n".join(
        [
            f"### {task_payload['id']} - {task_payload['title']}",
            "",
            f"Why it matters: {why}",
            f"Evidence: {evidence}",
            f"Affected paths: {affected}.",
            "",
            render_backlog_task_block(task_payload),
        ]
    )


def _replace_header(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}:\s+.*$", re.M)
    replacement = f"{key}: `{value}`"
    if pattern.search(text):
        return pattern.sub(replacement, text, count=1)
    return replacement + "\n" + text


def _apply_runtime_headers(text: str, profile: RepoProfile) -> str:
    header_values = {
        "Project": profile.project_name,
        "Repo root": str(profile.paths.project_root),
        "Generated": now_iso(),
        "Target branch": current_branch(profile.paths.project_root),
        "Target commit": current_commit(profile.paths.project_root),
        "Profile": str(profile.paths.profile_file),
        "State file": str(profile.paths.state_file),
        "Events file": str(profile.paths.events_file),
        "Inbox file": str(profile.paths.inbox_file),
        "Results dir": str(profile.paths.results_dir),
        "HTML file": str(profile.paths.html_file),
    }
    updated = text
    for key, value in header_values.items():
        updated = _replace_header(updated, key, value)
    return updated


def refresh_backlog_headers(profile: RepoProfile) -> None:
    if not profile.paths.backlog_file.exists():
        return
    with locked_path(profile.paths.backlog_file):
        if not profile.paths.backlog_file.exists():
            return
        text = profile.paths.backlog_file.read_text(encoding="utf-8")
        updated = _apply_runtime_headers(text, profile)
        if updated != text:
            atomic_write_text(profile.paths.backlog_file, updated)


def _parse_runtime_iso(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _rounded_minutes(seconds: int | None) -> int | None:
    if seconds is None:
        return None
    return max(0, int(round(seconds / 60.0)))


def _task_runtime_row(task_id: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "claim_count": 0,
        "claim_actors": set(),
        "result_count": 0,
        "total_task_seconds": 0,
        "active_task_seconds": None,
        "worktree_keys": set(),
        "run_attempt_keys": set(),
        "landing_failures": 0,
        "estimated_active_minutes": None,
        "estimated_elapsed_minutes": None,
        "latest_result": None,
        "latest_result_has_actual_task_telemetry": False,
    }


def _runtime_int(value: Any) -> int | None:
    try:
        return _coerce_optional_int(value, field="runtime")
    except BacklogError:
        return None


def _result_has_actual_task_telemetry(row: dict[str, Any]) -> bool:
    telemetry = row.get("task_shaping_telemetry") if isinstance(row.get("task_shaping_telemetry"), dict) else {}
    for key in ("actual_task_minutes", "actual_task_seconds", "actual_active_minutes", "actual_elapsed_minutes"):
        if _runtime_int(telemetry.get(key)) is not None:
            return True
    return False


def _runtime_target_branch(
    *,
    task_id: str,
    events: list[dict[str, Any]],
    fallback: str | None,
) -> str | None:
    for event in sorted(events, key=lambda row: str(row.get("at") or ""), reverse=True):
        if str(event.get("task_id") or "") != task_id:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        target_branch = str(payload.get("target_branch") or "").strip()
        if target_branch:
            return target_branch
    return fallback


def _runtime_changed_paths(cwd: Path, *, target_branch: str | None) -> list[str]:
    commands: list[tuple[str, ...]] = [
        ("diff", "--name-only"),
        ("diff", "--name-only", "--cached"),
        ("ls-files", "--others", "--exclude-standard"),
    ]
    if target_branch:
        commands.append(("diff", "--name-only", f"{target_branch}...HEAD"))
    changed: list[str] = []
    for args in commands:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines():
            item = line.strip()
            if item:
                changed.append(item)
    return _unique_ordered(changed)


def _task_runtime_rows(
    *,
    snapshot: BacklogSnapshot,
    state: dict[str, Any],
    events: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    open_claims: dict[str, datetime] = {}
    latest_results: dict[str, dict[str, Any]] = {}

    for row in results:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        current = rows.setdefault(task_id, _task_runtime_row(task_id))
        current["result_count"] += 1
        if task_id not in latest_results:
            latest_results[task_id] = row
            current["latest_result"] = row
            current["latest_result_has_actual_task_telemetry"] = _result_has_actual_task_telemetry(row)

    for event in sorted(events, key=lambda row: str(row.get("at") or "")):
        task_id = str(event.get("task_id") or "")
        if not task_id:
            continue
        current = rows.setdefault(task_id, _task_runtime_row(task_id))
        event_type = str(event.get("type") or "")
        event_at = _parse_runtime_iso(event.get("at"))
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "claim" and event_at is not None:
            current["claim_count"] += 1
            actor = str(event.get("actor") or "").strip()
            if actor:
                current["claim_actors"].add(actor)
            open_claims[task_id] = event_at
        elif event_type in {"release", "complete"}:
            started_at = open_claims.pop(task_id, None)
            if started_at is not None and event_at is not None:
                current["total_task_seconds"] += max(0, int((event_at - started_at).total_seconds()))
        if event_type == "worktree_start":
            branch = str(payload.get("branch") or "").strip()
            worktree_path = str(payload.get("worktree_path") or "").strip()
            if branch:
                current["worktree_keys"].add(branch)
            elif worktree_path:
                current["worktree_keys"].add(worktree_path)
        elif event_type in {"child_launch", "child_launch_failed"}:
            run_id = str(payload.get("run_id") or event.get("event_id") or "").strip()
            if run_id:
                current["run_attempt_keys"].add(run_id)
            workspace = str(payload.get("workspace") or "").strip()
            branch = str(payload.get("branch") or "").strip()
            if branch:
                current["worktree_keys"].add(branch)
            elif workspace:
                current["worktree_keys"].add(workspace)
        elif event_type in {"child_finish", "worktree_land"} and payload.get("land_error"):
            current["landing_failures"] += 1

    for task_id, entry in state.get("task_claims", {}).items():
        if not isinstance(entry, dict):
            continue
        current = rows.setdefault(str(task_id), _task_runtime_row(str(task_id)))
        status = str(entry.get("status") or "")
        claimed_at = _parse_runtime_iso(entry.get("claimed_at"))
        if status == CLAIM_STATUS_CLAIMED and claimed_at is not None:
            current["active_task_seconds"] = max(0, int((datetime.now().astimezone() - claimed_at).total_seconds()))

    for task in snapshot.tasks.values():
        current = rows.setdefault(task.id, _task_runtime_row(task.id))
        latest_result = latest_results.get(task.id)
        latest_telemetry = (
            latest_result.get("task_shaping_telemetry")
            if isinstance((latest_result or {}).get("task_shaping_telemetry"), dict)
            else {}
        )
        task_shaping = task.payload.get("task_shaping") if isinstance(task.payload.get("task_shaping"), dict) else {}
        current["estimated_active_minutes"] = _runtime_int(
            latest_telemetry.get("estimated_active_minutes")
            if latest_telemetry.get("estimated_active_minutes") is not None
            else task_shaping.get("estimated_active_minutes")
        )
        current["estimated_elapsed_minutes"] = _runtime_int(
            latest_telemetry.get("estimated_elapsed_minutes")
            if latest_telemetry.get("estimated_elapsed_minutes") is not None
            else task_shaping.get("estimated_elapsed_minutes")
        )
        total_task_seconds = int(current.get("total_task_seconds") or 0)
        active_task_seconds = (
            int(current["active_task_seconds"]) if current.get("active_task_seconds") is not None else None
        )
        if active_task_seconds is not None:
            total_task_seconds += active_task_seconds
        current["total_task_seconds"] = total_task_seconds
        current["actual_task_seconds"] = total_task_seconds if current["claim_count"] or total_task_seconds else None
        current["actual_task_minutes"] = _rounded_minutes(current["actual_task_seconds"])
        current["actual_reclaim_count"] = max(0, int(current["claim_count"]) - 1)
        current["actual_worktrees_used"] = len(current["worktree_keys"]) or (1 if current["claim_count"] else 0)
        current["actual_retry_count"] = max(0, len(current["run_attempt_keys"]) - 1)
        current["actual_handoffs"] = max(0, len(current["claim_actors"]) - 1)
    return rows


def _build_task_shaping_calibration(
    *,
    snapshot: BacklogSnapshot,
    state: dict[str, Any],
    events: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    runtime_rows = _task_runtime_rows(snapshot=snapshot, state=state, events=events, results=results)
    ratio_samples: list[float] = []
    effort_profiles: dict[str, dict[str, Any]] = {}

    for effort in sorted(VALID_EFFORTS, key=lambda item: EFFORT_ORDER[item]):
        actual_samples: list[int] = []
        estimated_elapsed_samples: list[int] = []
        estimated_active_samples: list[int] = []
        validation_samples: list[int] = []
        path_count_samples: list[int] = []
        check_count_samples: list[int] = []
        doc_count_samples: list[int] = []

        for task in snapshot.tasks.values():
            if str(task.payload.get("effort") or "") != effort or not task_done(task.id, state):
                continue
            runtime = runtime_rows.get(task.id, _task_runtime_row(task.id))
            task_shaping = task.payload.get("task_shaping") if isinstance(task.payload.get("task_shaping"), dict) else {}
            actual_minutes = _runtime_int(runtime.get("actual_task_minutes"))
            estimated_elapsed = _runtime_int(
                runtime.get("estimated_elapsed_minutes")
                if runtime.get("estimated_elapsed_minutes") is not None
                else task_shaping.get("estimated_elapsed_minutes")
            )
            estimated_active = _runtime_int(
                runtime.get("estimated_active_minutes")
                if runtime.get("estimated_active_minutes") is not None
                else task_shaping.get("estimated_active_minutes")
            )
            validation_minutes = _runtime_int(task_shaping.get("estimated_validation_minutes"))
            if actual_minutes is not None:
                actual_samples.append(actual_minutes)
            if estimated_elapsed is not None:
                estimated_elapsed_samples.append(estimated_elapsed)
            if estimated_active is not None:
                estimated_active_samples.append(estimated_active)
            if validation_minutes is not None:
                validation_samples.append(validation_minutes)
            path_count_samples.append(len(task.payload.get("paths", [])))
            check_count_samples.append(len(task.payload.get("checks", [])))
            doc_count_samples.append(len(task.payload.get("docs", [])))
            if estimated_elapsed and estimated_active:
                ratio_samples.append(max(0.1, min(1.0, float(estimated_active) / float(estimated_elapsed))))

        baseline = EFFORT_TASK_SHAPING_BASELINES[effort]
        seeded_elapsed = _rounded_task_minutes(
            _median_int(
                [
                    _median_int(actual_samples),
                    _median_int(estimated_elapsed_samples),
                    int(baseline["estimated_elapsed_minutes"]),
                ]
            )
        )
        if seeded_elapsed is None:
            seeded_elapsed = int(baseline["estimated_elapsed_minutes"])
        effort_profiles[effort] = {
            "completed_sample_size": len(actual_samples),
            "estimate_sample_size": len(estimated_elapsed_samples),
            "median_actual_task_minutes": _median_int(actual_samples),
            "median_estimated_elapsed_minutes": _median_int(estimated_elapsed_samples),
            "median_estimated_active_minutes": _median_int(estimated_active_samples),
            "median_validation_minutes": _median_int(validation_samples),
            "median_path_count": _median_int(path_count_samples) or 1,
            "median_check_count": _median_int(check_count_samples) or 1,
            "median_doc_count": _median_int(doc_count_samples) or 0,
            "seeded_elapsed_minutes": seeded_elapsed,
        }

    default_active_ratio = _median_int([round(sample * 100) for sample in ratio_samples])
    active_ratio = (
        max(0.1, min(1.0, float(default_active_ratio) / 100.0))
        if default_active_ratio is not None
        else 0.62
    )
    for effort, stats in effort_profiles.items():
        baseline = EFFORT_TASK_SHAPING_BASELINES[effort]
        seeded_elapsed = int(stats["seeded_elapsed_minutes"])
        seeded_active = _rounded_task_minutes(
            _median_int(
                [
                    stats["median_estimated_active_minutes"],
                    int(baseline["estimated_active_minutes"]),
                    _rounded_task_minutes(seeded_elapsed * active_ratio),
                ]
            )
        )
        if seeded_active is None:
            seeded_active = int(baseline["estimated_active_minutes"])
        seeded_validation = _rounded_task_minutes(
            _median_int(
                [
                    stats["median_validation_minutes"],
                    int(baseline["estimated_validation_minutes"]),
                    _rounded_task_minutes(max(5, seeded_elapsed * 0.15)),
                ]
            )
        )
        if seeded_validation is None:
            seeded_validation = int(baseline["estimated_validation_minutes"])
        stats["seeded_active_minutes"] = min(seeded_elapsed, seeded_active)
        stats["seeded_validation_minutes"] = seeded_validation

    return {
        "default_active_ratio": round(active_ratio, 2),
        "by_effort": effort_profiles,
    }


def _seed_task_shaping_from_calibration(
    task_shaping: dict[str, Any] | None,
    *,
    effort: str,
    risk: str,
    paths: list[str],
    checks: list[str],
    docs: list[str],
    domains: list[str],
    calibration: dict[str, Any],
) -> dict[str, Any]:
    seeded = _coerce_task_shaping(task_shaping, fallback_paths=paths)
    stats = calibration.get("by_effort", {}).get(effort, {})
    baseline = EFFORT_TASK_SHAPING_BASELINES[effort]

    seeded_elapsed = int(stats.get("seeded_elapsed_minutes") or baseline["estimated_elapsed_minutes"])
    seeded_active = int(stats.get("seeded_active_minutes") or baseline["estimated_active_minutes"])
    seeded_validation = int(stats.get("seeded_validation_minutes") or baseline["estimated_validation_minutes"])
    median_path_count = max(1, int(stats.get("median_path_count") or 1))
    median_check_count = max(1, int(stats.get("median_check_count") or 1))
    median_doc_count = max(0, int(stats.get("median_doc_count") or 0))

    complexity_multiplier = 1.0
    if len(paths) > median_path_count:
        complexity_multiplier += min(len(paths) - median_path_count, 4) * 0.12
    if len(checks) > median_check_count:
        complexity_multiplier += min(len(checks) - median_check_count, 3) * 0.08
    if len(docs) > median_doc_count:
        complexity_multiplier += min(len(docs) - median_doc_count, 3) * 0.04
    if len(domains) > 2:
        complexity_multiplier += min(len(domains) - 2, 3) * 0.05
    if risk == "high":
        complexity_multiplier += 0.15

    if seeded.get("estimated_elapsed_minutes") is None:
        seeded["estimated_elapsed_minutes"] = _rounded_task_minutes(seeded_elapsed * complexity_multiplier)
    if seeded.get("estimated_active_minutes") is None:
        seeded["estimated_active_minutes"] = min(
            int(seeded["estimated_elapsed_minutes"]),
            int(_rounded_task_minutes(seeded_active * complexity_multiplier) or seeded_active),
        )
    if seeded.get("estimated_validation_minutes") is None:
        validation_floor = max(5, len(checks) * 5)
        seeded["estimated_validation_minutes"] = max(
            validation_floor,
            int(_rounded_task_minutes(seeded_validation * max(1.0, complexity_multiplier - 0.05)) or seeded_validation),
        )
    seeded.setdefault("estimate_source", "history" if int(stats.get("completed_sample_size") or 0) else "defaults")
    seeded.setdefault("estimate_basis_effort", effort)
    seeded.setdefault("estimate_basis_sample_size", int(stats.get("completed_sample_size") or 0))
    return seeded


def enrich_result_task_shaping_telemetry(
    profile: RepoProfile,
    *,
    task_id: str,
    task_shaping_telemetry: dict[str, Any] | None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    with locked_path(profile.paths.backlog_file):
        snapshot = load_backlog(profile.paths, profile)
    state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
    events = load_events(profile.paths)
    results = load_task_results(profile.paths)
    runtime_rows = _task_runtime_rows(snapshot=snapshot, state=state, events=events, results=results)
    runtime = runtime_rows.get(task_id, _task_runtime_row(task_id))
    task = snapshot.tasks.get(task_id)
    task_shaping = task.payload.get("task_shaping") if task and isinstance(task.payload.get("task_shaping"), dict) else {}
    merged = dict(task_shaping_telemetry or {})

    for key in (
        "estimated_elapsed_minutes",
        "estimated_active_minutes",
        "estimated_touched_paths",
        "estimated_validation_minutes",
        "estimated_worktrees",
        "estimated_handoffs",
        "parallelizable_groups",
    ):
        if key not in merged and task_shaping.get(key) is not None:
            merged[key] = task_shaping.get(key)

    if runtime.get("actual_task_seconds") is not None:
        merged.setdefault("actual_task_seconds", runtime["actual_task_seconds"])
        merged.setdefault("actual_task_minutes", runtime["actual_task_minutes"])
        merged.setdefault("actual_active_minutes", runtime["actual_task_minutes"])
        merged.setdefault("actual_elapsed_minutes", runtime["actual_task_minutes"])
    merged.setdefault("claim_count", runtime.get("claim_count", 0))
    merged.setdefault("actual_reclaim_count", runtime.get("actual_reclaim_count", 0))
    merged.setdefault("actual_worktrees_used", runtime.get("actual_worktrees_used", 0))
    merged.setdefault("actual_retry_count", runtime.get("actual_retry_count", 0))
    merged.setdefault("actual_handoffs", runtime.get("actual_handoffs", 0))
    merged.setdefault("actual_landing_failures", runtime.get("landing_failures", 0))

    if cwd is not None:
        target_branch = _runtime_target_branch(
            task_id=task_id,
            events=events,
            fallback=snapshot.headers.get("Target branch"),
        )
        changed_paths = _runtime_changed_paths(cwd, target_branch=target_branch)
        if changed_paths:
            merged.setdefault("changed_paths", changed_paths)
            merged.setdefault("actual_touched_paths", changed_paths)
            merged.setdefault("actual_touched_path_count", len(changed_paths))

    context_metrics = _task_context_metrics(task, runtime)
    for key, value in context_metrics.items():
        if value is None:
            continue
        merged.setdefault(key, value)

    estimate_minutes = _runtime_int(merged.get("estimated_elapsed_minutes"))
    actual_minutes = _runtime_int(merged.get("actual_elapsed_minutes"))
    if estimate_minutes is not None and actual_minutes is not None:
        merged.setdefault("estimate_delta_minutes", int(actual_minutes) - int(estimate_minutes))
        if estimate_minutes > 0:
            merged.setdefault("estimate_accuracy_ratio", round(float(actual_minutes) / float(estimate_minutes), 2))

    return merged


def _task_context_metrics(task: BacklogTask | None, runtime: dict[str, Any]) -> dict[str, Any]:
    payload = task.payload if task else {}
    task_shaping = payload.get("task_shaping") if isinstance(payload.get("task_shaping"), dict) else {}
    paths = [str(item).strip() for item in payload.get("paths", []) if str(item).strip()]
    docs = [str(item).strip() for item in payload.get("docs", []) if str(item).strip()]
    checks = [str(item).strip() for item in payload.get("checks", []) if str(item).strip()]
    domains = [str(item).strip() for item in payload.get("domains", []) if str(item).strip()]
    objective = str(payload.get("objective") or "").strip()
    why = str(getattr(getattr(task, "narrative", None), "why", "") or "").strip()
    evidence = str(getattr(getattr(task, "narrative", None), "evidence", "") or "").strip()
    safe_first_slice = str(payload.get("safe_first_slice") or "").strip()
    estimate_field_count = sum(
        1
        for key in (
            "estimated_elapsed_minutes",
            "estimated_active_minutes",
            "estimated_touched_paths",
            "estimated_validation_minutes",
            "estimated_worktrees",
            "estimated_handoffs",
            "parallelizable_groups",
        )
        if task_shaping.get(key) not in (None, [], {})
    )
    context_packet = {
        "objective": objective,
        "why": why,
        "evidence": evidence,
        "safe_first_slice": safe_first_slice,
        "docs": docs,
        "checks": checks,
        "paths": paths,
        "domains": domains,
        "task_shaping": {
            key: task_shaping.get(key)
            for key in (
                "estimated_elapsed_minutes",
                "estimated_active_minutes",
                "estimated_validation_minutes",
                "estimated_worktrees",
                "estimated_handoffs",
                "parallelizable_groups",
            )
        },
    }
    packet_bytes = len(json.dumps(context_packet, sort_keys=True))
    misstep_total = (
        int(runtime.get("actual_reclaim_count") or 0)
        + int(runtime.get("actual_retry_count") or 0)
        + int(runtime.get("landing_failures") or runtime.get("actual_landing_failures") or 0)
    )
    actual_touched_path_count = runtime.get("actual_touched_path_count")
    context_efficiency_ratio = None
    if actual_touched_path_count is not None and paths:
        context_efficiency_ratio = round(int(actual_touched_path_count) / max(len(paths), 1), 2)

    estimate_minutes = runtime.get("estimated_active_minutes")
    if estimate_minutes is None:
        estimate_minutes = runtime.get("estimated_elapsed_minutes")
    if estimate_minutes is None:
        estimate_minutes = task_shaping.get("estimated_active_minutes")
    if estimate_minutes is None:
        estimate_minutes = task_shaping.get("estimated_elapsed_minutes")
    actual_minutes = runtime.get("actual_task_minutes")
    document_routing_value_score = 0
    if docs:
        document_routing_value_score += 1
    if docs and misstep_total == 0:
        document_routing_value_score += 1
    if docs and actual_minutes is not None and estimate_minutes is not None and abs(int(actual_minutes) - int(estimate_minutes)) <= 15:
        document_routing_value_score += 1

    context_packet_score = (
        min(len(docs), 3)
        + min(len(checks), 2)
        + min(len(paths), 3)
        + min(len(domains), 2)
        + (1 if objective else 0)
        + (1 if why else 0)
        + (1 if evidence else 0)
        + (1 if safe_first_slice else 0)
        + min(estimate_field_count, 3)
    )
    return {
        "context_doc_count": len(docs),
        "context_check_count": len(checks),
        "context_path_count": len(paths),
        "context_domain_count": len(domains),
        "context_has_objective": bool(objective),
        "context_has_why": bool(why),
        "context_has_evidence": bool(evidence),
        "context_has_safe_first_slice": bool(safe_first_slice),
        "context_estimate_field_count": estimate_field_count,
        "context_packet_score": context_packet_score,
        "context_packet_bytes": packet_bytes,
        "misstep_total": misstep_total,
        "document_routing_value_score": document_routing_value_score,
        "context_efficiency_ratio": context_efficiency_ratio,
    }


def _proper_tuning_module():
    return importlib.import_module("blackdog.tuning")


def build_prompt_profiles(profile: RepoProfile, *, analysis: dict[str, Any]) -> dict[str, Any]:
    return _proper_tuning_module().build_prompt_profiles(profile, analysis=analysis)


def build_prompt_improvement(profile: RepoProfile, *, prompt_text: str, complexity: str, analysis: dict[str, Any]) -> dict[str, Any]:
    return _proper_tuning_module().build_prompt_improvement(
        profile,
        prompt_text=prompt_text,
        complexity=complexity,
        analysis=analysis,
    )


def build_tune_analysis(profile: RepoProfile) -> dict[str, Any]:
    return _proper_tuning_module().build_tune_analysis(profile)


def _unique_ordered(items: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in items:
        item = str(raw).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _tune_task_payload(profile: RepoProfile) -> dict[str, Any]:
    return _proper_tuning_module()._tune_task_payload(profile, analysis_builder=build_tune_analysis)


def seed_tune_task(profile: RepoProfile) -> tuple[dict[str, Any], bool]:
    return _proper_tuning_module().seed_tune_task(profile, tune_task_payload_builder=_tune_task_payload)


def compact_active_plan(snapshot: BacklogSnapshot, state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    original_plan = snapshot.plan if isinstance(snapshot.plan, dict) else {"epics": [], "lanes": []}
    active_task_ids = {task_id for task_id in snapshot.tasks if not task_done(task_id, state)}
    removed_task_ids: list[str] = []
    removed_lane_ids: list[str] = []
    removed_epic_ids: list[str] = []
    seen_removed_tasks: set[str] = set()

    lanes: list[dict[str, Any]] = []
    for lane in original_plan.get("lanes", []):
        if not isinstance(lane, dict):
            continue
        lane_task_ids = [str(task_id) for task_id in lane.get("task_ids", [])]
        kept_task_ids = [task_id for task_id in lane_task_ids if task_id in active_task_ids]
        for task_id in lane_task_ids:
            if task_id in active_task_ids or task_id in seen_removed_tasks:
                continue
            seen_removed_tasks.add(task_id)
            removed_task_ids.append(task_id)
        if not kept_task_ids:
            lane_id = str(lane.get("id") or "").strip()
            if lane_id:
                removed_lane_ids.append(lane_id)
            continue
        updated_lane = dict(lane)
        updated_lane["task_ids"] = kept_task_ids
        lanes.append(updated_lane)

    wave_values = sorted({int(lane.get("wave", 0)) for lane in lanes})
    wave_map = {wave: index for index, wave in enumerate(wave_values)}
    for lane in lanes:
        lane["wave"] = wave_map[int(lane.get("wave", 0))]

    epics: list[dict[str, Any]] = []
    for epic in original_plan.get("epics", []):
        if not isinstance(epic, dict):
            continue
        task_ids = [str(task_id) for task_id in epic.get("task_ids", []) if str(task_id) in active_task_ids]
        if not task_ids:
            epic_id = str(epic.get("id") or "").strip()
            if epic_id:
                removed_epic_ids.append(epic_id)
            continue
        updated_epic = dict(epic)
        updated_epic["task_ids"] = task_ids
        epics.append(updated_epic)

    plan = {"epics": epics, "lanes": lanes}
    validate_plan_payload(plan, task_ids=active_task_ids)
    return plan, {
        "changed": plan != original_plan,
        "active_task_ids": sorted(active_task_ids),
        "removed_task_ids": removed_task_ids,
        "removed_lane_ids": removed_lane_ids,
        "removed_epic_ids": removed_epic_ids,
        "wave_map": {str(source): target for source, target in wave_map.items()},
    }


def sweep_completed_tasks(profile: RepoProfile) -> dict[str, Any]:
    with locked_path(profile.paths.backlog_file):
        snapshot = load_backlog(profile.paths, profile)
        state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
        save_state(profile.paths.state_file, state)
        plan, meta = compact_active_plan(snapshot, state)
        if meta["changed"]:
            updated = _apply_runtime_headers(snapshot.raw_text, profile)
            updated = _replace_plan_block(updated, plan)
            atomic_write_text(profile.paths.backlog_file, updated)
        return {
            "changed": bool(meta["changed"]),
            "plan": plan,
            "active_task_ids": meta["active_task_ids"],
            "removed_task_ids": meta["removed_task_ids"],
            "removed_lane_ids": meta["removed_lane_ids"],
            "removed_epic_ids": meta["removed_epic_ids"],
            "wave_map": meta["wave_map"],
        }


def _replace_plan_block(text: str, plan: dict[str, Any]) -> str:
    block = render_backlog_plan_block(plan)
    if PLAN_BLOCK_RE.search(text):
        return PLAN_BLOCK_RE.sub(block.rstrip(), text, count=1)
    marker = "## Lane Plan"
    index = text.find(marker)
    if index == -1:
        raise BacklogError("Could not find ## Lane Plan section")
    insert_at = text.find("\n", index)
    if insert_at == -1:
        insert_at = len(text)
    return text[: insert_at + 1] + "\n" + block + "\n" + text[insert_at + 1 :]


def _ensure_plan_entry(plan: dict[str, Any], *, kind: str, entry_id: str, title: str, wave: int | None = None) -> dict[str, Any]:
    collection = plan["epics"] if kind == "epic" else plan["lanes"]
    for entry in collection:
        if str(entry.get("id")) == entry_id:
            if not entry.get("title"):
                entry["title"] = title
            if kind == "lane" and wave is not None:
                entry["wave"] = int(entry.get("wave", wave))
            return entry
    created = {"id": entry_id, "title": title, "task_ids": []}
    if kind == "lane":
        created["wave"] = 0 if wave is None else wave
    collection.append(created)
    return created


def _prune_empty_plan_entries(plan: dict[str, Any]) -> dict[str, Any]:
    plan["epics"] = [
        entry
        for entry in plan.get("epics", [])
        if isinstance(entry, dict) and entry.get("task_ids")
    ]
    plan["lanes"] = [
        entry
        for entry in plan.get("lanes", [])
        if isinstance(entry, dict) and entry.get("task_ids")
    ]
    return plan


def _remove_task_section(text: str, task_id: str) -> str:
    task_section_re = re.compile(
        rf"^###\s+{re.escape(task_id)}\s+-\s+.+?(?=^###\s+[A-Z0-9]+-[0-9a-f]+\s+-|\Z)",
        re.S | re.M,
    )
    updated, removed = task_section_re.subn("", text, count=1)
    if removed != 1:
        raise BacklogError(f"Could not find task section for {task_id}")
    return updated.rstrip() + "\n"


def _replace_task_section(text: str, task_id: str, section: str) -> str:
    task_section_re = re.compile(
        rf"^###\s+{re.escape(task_id)}\s+-\s+.+?(?=^###\s+[A-Z0-9]+-[0-9a-f]+\s+-|\Z)",
        re.S | re.M,
    )
    updated, replaced = task_section_re.subn(section.rstrip() + "\n", text, count=1)
    if replaced != 1:
        raise BacklogError(f"Could not find task section for {task_id}")
    return updated.rstrip() + "\n"


def _plan_entry_for_task(plan: dict[str, Any], *, collection: str, task_id: str) -> dict[str, Any] | None:
    for entry in plan.get(collection, []):
        if not isinstance(entry, dict):
            continue
        task_ids = [str(value) for value in entry.get("task_ids", [])]
        if task_id in task_ids:
            return entry
    return None


def add_task(
    profile: RepoProfile,
    *,
    title: str,
    bucket: str,
    priority: str,
    risk: str,
    effort: str,
    why: str,
    evidence: str,
    safe_first_slice: str,
    paths: list[str],
    checks: list[str],
    docs: list[str],
    domains: list[str],
    packages: list[str],
    affected_paths: list[str],
    task_shaping: dict[str, Any] | None,
    objective: str,
    requires_approval: bool,
    approval_reason: str,
    epic_id: str | None,
    epic_title: str | None,
    lane_id: str | None,
    lane_title: str | None,
    wave: int | None,
) -> dict[str, Any]:
    with locked_path(profile.paths.backlog_file):
        snapshot = load_backlog(profile.paths, profile)
        state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
        events = load_events(profile.paths)
        results = load_task_results(profile.paths)
        calibration = _build_task_shaping_calibration(snapshot=snapshot, state=state, events=events, results=results)
        task_id = make_task_id(profile, bucket=bucket, title=title, paths=paths)
        if task_id in snapshot.tasks:
            raise BacklogError(f"Task id already exists: {task_id}")
        payload = {
            "id": task_id,
            "title": title,
            "bucket": bucket,
            "priority": priority,
            "risk": risk,
            "effort": effort,
            "packages": packages,
            "paths": paths,
            "checks": checks or list(profile.validation_commands),
            "docs": docs or list(profile.doc_routing_defaults),
            "objective": objective,
            "task_shaping": _seed_task_shaping_from_calibration(
                task_shaping,
                effort=effort,
                risk=risk,
                paths=paths,
                checks=checks or list(profile.validation_commands),
                docs=docs or list(profile.doc_routing_defaults),
                domains=domains,
                calibration=calibration,
            ),
            "domains": domains,
            "requires_approval": requires_approval,
            "approval_reason": approval_reason,
            "safe_first_slice": safe_first_slice,
        }
        validate_task_payload(payload, profile)
        plan = json.loads(json.dumps(snapshot.plan))
        if not isinstance(plan.get("epics"), list):
            plan["epics"] = []
        if not isinstance(plan.get("lanes"), list):
            plan["lanes"] = []
        resolved_epic_title = epic_title or "Unplanned"
        resolved_epic_id = epic_id or f"epic-{slugify(resolved_epic_title)}"
        resolved_lane_title = lane_title or "Unplanned"
        resolved_lane_id = lane_id or f"lane-{slugify(resolved_lane_title)}"
        resolved_wave = wave
        if resolved_wave is None:
            existing_waves = [int(item.get("wave", 0)) for item in plan["lanes"]]
            resolved_wave = max(existing_waves, default=0)
        epic_entry = _ensure_plan_entry(plan, kind="epic", entry_id=resolved_epic_id, title=resolved_epic_title)
        lane_entry = _ensure_plan_entry(
            plan,
            kind="lane",
            entry_id=resolved_lane_id,
            title=resolved_lane_title,
            wave=resolved_wave,
        )
        epic_entry["task_ids"].append(task_id)
        lane_entry["task_ids"].append(task_id)
        validate_plan_payload(plan, task_ids={*snapshot.tasks.keys(), task_id})

        updated = _apply_runtime_headers(snapshot.raw_text, profile)
        updated = _replace_plan_block(updated, plan)
        updated = updated.rstrip() + "\n\n" + render_task_section(
            payload,
            why=why,
            evidence=evidence,
            affected_paths=affected_paths or paths,
        ).rstrip() + "\n"
        atomic_write_text(profile.paths.backlog_file, updated)
        return payload


def update_task(
    profile: RepoProfile,
    *,
    task_id: str,
    title: str | None = None,
    bucket: str | None = None,
    priority: str | None = None,
    risk: str | None = None,
    effort: str | None = None,
    why: str | None = None,
    evidence: str | None = None,
    safe_first_slice: str | None = None,
    paths: list[str] | None = None,
    checks: list[str] | None = None,
    docs: list[str] | None = None,
    domains: list[str] | None = None,
    packages: list[str] | None = None,
    affected_paths: list[str] | None = None,
    task_shaping: dict[str, Any] | None = None,
    objective: str | None = None,
    requires_approval: bool | None = None,
    approval_reason: str | None = None,
    epic_id: str | None = None,
    epic_title: str | None = None,
    lane_id: str | None = None,
    lane_title: str | None = None,
    wave: int | None = None,
) -> dict[str, Any]:
    with locked_path(profile.paths.backlog_file):
        snapshot = load_backlog(profile.paths, profile)
        task = snapshot.tasks.get(task_id)
        if task is None:
            raise BacklogError(f"Unknown task id: {task_id}")
        state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
        claim_entry = state.get("task_claims", {}).get(task_id) or {}
        if claim_entry:
            raise BacklogError(f"Task {task_id} already has execution state and cannot be edited in place")
        task_results = [row for row in load_task_results(profile.paths) if str(row.get("task_id") or "") == task_id]
        if task_results:
            raise BacklogError(f"Task {task_id} already has recorded results and cannot be edited in place")

        existing_payload = json.loads(json.dumps(task.payload))
        updated_payload = dict(existing_payload)
        if title is not None:
            updated_payload["title"] = title
        if bucket is not None:
            updated_payload["bucket"] = bucket
        if priority is not None:
            updated_payload["priority"] = priority
        if risk is not None:
            updated_payload["risk"] = risk
        if effort is not None:
            updated_payload["effort"] = effort
        if safe_first_slice is not None:
            updated_payload["safe_first_slice"] = safe_first_slice
        if paths is not None:
            updated_payload["paths"] = list(paths)
        if checks is not None:
            updated_payload["checks"] = list(checks)
        if docs is not None:
            updated_payload["docs"] = list(docs)
        if domains is not None:
            updated_payload["domains"] = list(domains)
        if packages is not None:
            updated_payload["packages"] = list(packages)
        if objective is not None:
            updated_payload["objective"] = objective
        if requires_approval is not None:
            updated_payload["requires_approval"] = requires_approval
        if approval_reason is not None:
            updated_payload["approval_reason"] = approval_reason

        existing_task_shaping = existing_payload.get("task_shaping")
        merged_task_shaping = dict(existing_task_shaping) if isinstance(existing_task_shaping, dict) else {}
        if task_shaping is not None:
            merged_task_shaping.update(task_shaping)
        updated_payload["task_shaping"] = _coerce_task_shaping(
            merged_task_shaping,
            fallback_paths=list(updated_payload.get("paths") or []),
        )
        validate_task_payload(updated_payload, profile)

        plan = json.loads(json.dumps(snapshot.plan))
        existing_epic = _plan_entry_for_task(plan, collection="epics", task_id=task_id) or {}
        existing_lane = _plan_entry_for_task(plan, collection="lanes", task_id=task_id) or {}
        for collection_name in ("epics", "lanes"):
            for entry in plan.get(collection_name, []):
                if not isinstance(entry, dict):
                    continue
                entry["task_ids"] = [str(value) for value in entry.get("task_ids", []) if str(value) != task_id]
        plan = _prune_empty_plan_entries(plan)

        resolved_epic_title = epic_title or str(existing_epic.get("title") or task.epic_title or "Unplanned")
        resolved_epic_id = epic_id or str(existing_epic.get("id") or f"epic-{slugify(resolved_epic_title)}")
        resolved_lane_title = lane_title or str(existing_lane.get("title") or task.lane_title or "Unplanned")
        resolved_lane_id = lane_id or str(existing_lane.get("id") or f"lane-{slugify(resolved_lane_title)}")
        resolved_wave = (
            wave
            if wave is not None
            else int(existing_lane.get("wave", task.wave if task.wave is not None else 0))
        )

        epic_entry = _ensure_plan_entry(plan, kind="epic", entry_id=resolved_epic_id, title=resolved_epic_title)
        lane_entry = _ensure_plan_entry(
            plan,
            kind="lane",
            entry_id=resolved_lane_id,
            title=resolved_lane_title,
            wave=resolved_wave,
        )
        epic_entry["task_ids"].append(task_id)
        lane_entry["task_ids"].append(task_id)
        validate_plan_payload(plan, task_ids=set(snapshot.tasks))

        resolved_why = why if why is not None else task.narrative.why
        resolved_evidence = evidence if evidence is not None else task.narrative.evidence
        resolved_affected_paths = (
            list(affected_paths)
            if affected_paths is not None
            else list(task.narrative.affected_paths) or list(updated_payload.get("paths") or [])
        )

        updated = _apply_runtime_headers(snapshot.raw_text, profile)
        updated = _replace_plan_block(updated, plan)
        updated = _replace_task_section(
            updated,
            task_id,
            render_task_section(
                updated_payload,
                why=resolved_why,
                evidence=resolved_evidence,
                affected_paths=resolved_affected_paths,
            ),
        )
        atomic_write_text(profile.paths.backlog_file, updated)

    refreshed_snapshot = load_backlog(profile.paths, profile)
    state = sync_state_for_backlog(state, refreshed_snapshot)
    if not bool(updated_payload.get("requires_approval")):
        state.setdefault("approval_tasks", {}).pop(task_id, None)
    save_state(profile.paths.state_file, state)
    return updated_payload


def remove_task(profile: RepoProfile, *, task_id: str) -> dict[str, Any]:
    with locked_path(profile.paths.backlog_file):
        snapshot = load_backlog(profile.paths, profile)
        task = snapshot.tasks.get(task_id)
        if task is None:
            raise BacklogError(f"Unknown task id: {task_id}")
        state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
        claim_entry = state.get("task_claims", {}).get(task_id) or {}
        if claim_is_active(claim_entry):
            raise BacklogError(f"Task {task_id} is currently claimed by {claim_entry.get('claimed_by')}")
        if str(claim_entry.get("status") or "") == CLAIM_STATUS_DONE:
            raise BacklogError(f"Task {task_id} is already completed and cannot be removed")
        task_results = [row for row in load_task_results(profile.paths) if str(row.get("task_id") or "") == task_id]
        if task_results:
            raise BacklogError(f"Task {task_id} already has recorded results and cannot be removed")

        plan = json.loads(json.dumps(snapshot.plan))
        for collection_name in ("epics", "lanes"):
            for entry in plan.get(collection_name, []):
                entry["task_ids"] = [
                    str(value)
                    for value in entry.get("task_ids", [])
                    if str(value) != task_id
                ]
        plan = _prune_empty_plan_entries(plan)
        validate_plan_payload(plan, task_ids=set(snapshot.tasks) - {task_id})

        updated = _apply_runtime_headers(snapshot.raw_text, profile)
        updated = _replace_plan_block(updated, plan)
        updated = _remove_task_section(updated, task_id)
        atomic_write_text(profile.paths.backlog_file, updated)

    state.setdefault("approval_tasks", {}).pop(task_id, None)
    state.setdefault("task_claims", {}).pop(task_id, None)
    save_state(profile.paths.state_file, state)
    return {"id": task_id, "title": task.title}


def render_initial_backlog(
    profile: RepoProfile,
    *,
    objectives: list[str] | None = None,
    push_objective: list[str] | None = None,
    non_negotiables: list[str] | None = None,
    evidence_requirements: list[str] | None = None,
    release_gates: list[str] | None = None,
) -> str:
    objectives = objectives or [
        "OBJ-1: Maintain a repo-scoped backlog core",
        "OBJ-2: Keep AI-agent interaction structured and local",
        "OBJ-3: Preserve simple local usage before scale",
    ]
    push_objective = push_objective or [
        "Stand up a local, skill-aware backlog system with structured state, events, inbox messages, task results, and HTML status views.",
    ]
    non_negotiables = non_negotiables or [
        "Backlog state transitions should flow through the CLI, not through ad hoc file edits.",
        "Skills should remain thin adapters over repo-local code and configuration.",
    ]
    evidence_requirements = evidence_requirements or [
        "Every completed task should record structured results.",
        "Schema or CLI changes should update docs and tests in the same repo change.",
    ]
    release_gates = release_gates or [
        "The repo can scaffold a project, add tasks, claim tasks, complete tasks, record results, send inbox messages, and render HTML.",
    ]
    lines = [
        "# Blackdog Backlog",
        "",
        f"Project: `{profile.project_name}`",
        f"Repo root: `{profile.paths.project_root}`",
        f"Generated: `{now_iso()}`",
        f"Target branch: `{current_branch(profile.paths.project_root)}`",
        f"Target commit: `{current_commit(profile.paths.project_root)}`",
        f"Profile: `{profile.paths.profile_file}`",
        f"State file: `{profile.paths.state_file}`",
        f"Events file: `{profile.paths.events_file}`",
        f"Inbox file: `{profile.paths.inbox_file}`",
        f"Results dir: `{profile.paths.results_dir}`",
        f"HTML file: `{profile.paths.html_file}`",
        "",
        "## Objectives",
        "",
        *[f"- {item}" for item in objectives],
        "",
        "## Push Objective",
        "",
        *[f"- {item}" for item in push_objective],
        "",
        "## Non-Negotiables",
        "",
        *[f"- {item}" for item in non_negotiables],
        "",
        "## Evidence Requirements",
        "",
        *[f"- {item}" for item in evidence_requirements],
        "",
        "## Release Gates",
        "",
        *[f"- {item}" for item in release_gates],
        "",
        "## Recent Work Snapshot",
        "",
        "- Initial backlog scaffold created.",
        "",
        "## Alignment Notes",
        "",
        "- This backlog is optimized for local AI-assisted development and repo-scoped coordination.",
        "",
        "## Inventory Map",
        "",
        "- Use `blackdog bootstrap` to stand up a repo, `blackdog` for durable state transitions, and the configured control root for the local backlog artifact set.",
        "",
        "## Epic Map",
        "",
        "- Add epics as the backlog matures.",
        "",
        "## Ranked Top 3",
        "",
        "- Add tasks, then keep this list current.",
        "",
        "## Lane Plan",
        "",
        "- Add lanes and waves as tasks are introduced.",
        "",
        render_backlog_plan_block({"epics": [], "lanes": []}),
        "",
    ]
    return "\n".join(lines)
