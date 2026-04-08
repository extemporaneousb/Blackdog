from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha1
from pathlib import Path
from typing import Any
import json
import re
import subprocess
import textwrap

from .config import Profile, ProjectPaths, slugify
from .store import (
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
PROMPT_COMPLEXITY_PROFILES = {
    "low": {
        "label": "Low",
        "summary": "Keep the prompt lean and execution-biased for bounded work.",
        "doc_limit": 2,
        "include_why": False,
        "include_evidence": False,
        "include_task_shaping": False,
        "include_validation": True,
        "include_prompt_tuning": False,
    },
    "medium": {
        "label": "Medium",
        "summary": "Carry enough repo context to avoid avoidable retries without bloating the prompt.",
        "doc_limit": 4,
        "include_why": True,
        "include_evidence": True,
        "include_task_shaping": True,
        "include_validation": True,
        "include_prompt_tuning": True,
    },
    "high": {
        "label": "High",
        "summary": "Front-load repo policy, routed docs, validation, and tuning context for multi-surface work.",
        "doc_limit": 8,
        "include_why": True,
        "include_evidence": True,
        "include_task_shaping": True,
        "include_validation": True,
        "include_prompt_tuning": True,
    },
}
CORE_EXPORT_SCHEMA_VERSION = 1


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
class TaskInfo:
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
    tasks: dict[str, TaskInfo]
    plan: dict[str, Any]


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


def make_task_id(profile: Profile, *, bucket: str, title: str, paths: list[str]) -> str:
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


def validate_task_payload(task: dict[str, Any], profile: Profile) -> None:
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


def load_backlog(paths: ProjectPaths, profile: Profile) -> BacklogSnapshot:
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
    tasks: dict[str, TaskInfo] = {}
    for payload in _extract_json_blocks(text, "backlog-task"):
        validate_task_payload(payload, profile)
        task_id = str(payload["id"])
        lane_id, lane_title, wave, lane_order, lane_position, predecessors = lane_positions.get(
            task_id,
            (None, None, None, None, None, ()),
        )
        tasks[task_id] = TaskInfo(
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


def approval_satisfied(task: TaskInfo, state: dict[str, Any]) -> bool:
    if not bool(task.payload.get("requires_approval")):
        return True
    entry = state.get("approval_tasks", {}).get(task.id)
    return approval_is_satisfied(entry)


def active_claim_owner(task_id: str, state: dict[str, Any]) -> str | None:
    entry = state.get("task_claims", {}).get(task_id)
    if not isinstance(entry, dict) or not claim_is_active(entry):
        return None
    return str(entry.get("claimed_by") or "another-agent")


def blocking_reason(task: TaskInfo, snapshot: BacklogSnapshot, state: dict[str, Any], *, allow_high_risk: bool) -> str | None:
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


def classify_task_status(task: TaskInfo, snapshot: BacklogSnapshot, state: dict[str, Any], *, allow_high_risk: bool) -> tuple[str, str]:
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


def sync_state_for_backlog(state: dict[str, Any], snapshot: BacklogSnapshot) -> dict[str, Any]:
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
    return state


def next_runnable_tasks(
    snapshot: BacklogSnapshot,
    state: dict[str, Any],
    *,
    allow_high_risk: bool,
    limit: int,
) -> list[TaskInfo]:
    unfinished = [task for task in snapshot.tasks.values() if not task_done(task.id, state)]
    if not unfinished:
        return []
    planned = [task for task in unfinished if task.wave is not None]
    if planned:
        active_wave = min(int(task.wave) for task in planned)
        candidates = [task for task in planned if int(task.wave) == active_wave]
        first_by_lane: list[TaskInfo] = []
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


def build_view_model(
    profile: Profile,
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


def build_plan_view(
    profile: Profile,
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


def build_core_export(
    profile: Profile,
    snapshot: BacklogSnapshot,
    state: dict[str, Any],
    *,
    messages: list[dict[str, Any]] | None = None,
    results: list[dict[str, Any]] | None = None,
    allow_high_risk: bool = False,
) -> dict[str, Any]:
    message_rows = messages if messages is not None else load_inbox(profile.paths)
    result_rows = results if results is not None else load_task_results(profile.paths)
    summary = build_view_model(
        profile,
        snapshot,
        state,
        events=[],
        messages=message_rows,
        results=result_rows,
        allow_high_risk=allow_high_risk,
    )
    plan = build_plan_view(profile, snapshot, state, allow_high_risk=allow_high_risk)
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
        "schema_version": CORE_EXPORT_SCHEMA_VERSION,
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


def _apply_runtime_headers(text: str, profile: Profile) -> str:
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


def refresh_backlog_headers(profile: Profile) -> None:
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
    profile: Profile,
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


def _task_context_metrics(task: TaskInfo | None, runtime: dict[str, Any]) -> dict[str, Any]:
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


def build_prompt_profiles(profile: Profile, *, analysis: dict[str, Any]) -> dict[str, Any]:
    try:
        skill_doc_paths = [
            str((profile.paths.skill_dir / "SKILL.md").resolve().relative_to(profile.paths.project_root.resolve())),
            str(
                (profile.paths.skill_dir / "agents" / "openai.yaml")
                .resolve()
                .relative_to(profile.paths.project_root.resolve())
            ),
        ]
    except ValueError:
        skill_doc_paths = [
            str(profile.paths.skill_dir / "SKILL.md"),
            str(profile.paths.skill_dir / "agents" / "openai.yaml"),
        ]
    base_docs = _unique_ordered(
        [
            "AGENTS.md",
            *profile.doc_routing_defaults,
            *skill_doc_paths,
            "docs/INTEGRATION.md",
        ]
    )
    validation_commands = list(profile.validation_commands)
    focus = analysis["recommendation"]["focus"]
    focus_summary = analysis["recommendation"]["summary"]
    calibration = analysis.get("calibration", {})
    by_effort = calibration.get("by_effort", {})
    calibrated_defaults = {
        effort: {
            "estimated_elapsed_minutes": stats.get("seeded_elapsed_minutes"),
            "estimated_active_minutes": stats.get("seeded_active_minutes"),
            "estimated_validation_minutes": stats.get("seeded_validation_minutes"),
            "sample_size": stats.get("completed_sample_size", 0),
        }
        for effort, stats in by_effort.items()
    }
    profiles: dict[str, Any] = {}
    for name, config in PROMPT_COMPLEXITY_PROFILES.items():
        routed_docs = base_docs[: config["doc_limit"]]
        sections = ["Goal", "Repo contract", "Target paths"]
        if config["include_why"]:
            sections.append("Why it matters")
        if config["include_evidence"]:
            sections.append("Evidence")
        if config["include_task_shaping"]:
            sections.append("Task-shaping expectations")
        if config["include_validation"]:
            sections.append("Validation")
        if config["include_prompt_tuning"]:
            sections.append("Prompt-tuning focus")
        profiles[name] = {
            "complexity": name,
            "label": config["label"],
            "summary": config["summary"],
            "routed_docs": routed_docs,
            "validation_commands": validation_commands if config["include_validation"] else [],
            "recommended_sections": sections,
            "focus": {
                "category": focus,
                "summary": focus_summary,
            },
            "prompt_strategy": {
                "keep_context_compact": bool(name == "low"),
                "require_explicit_estimates": focus in {"task_shaping_coverage", "task_time_calibration"},
                "require_result_runtime_capture": True,
                "prefer_repo_routed_docs": True,
            },
            "calibrated_task_shape_defaults": calibrated_defaults,
            "context_budget": {
                "doc_limit": config["doc_limit"],
                "include_task_shaping": config["include_task_shaping"],
                "include_validation": config["include_validation"],
            },
        }
    return profiles


def build_prompt_improvement(profile: Profile, *, prompt_text: str, complexity: str, analysis: dict[str, Any]) -> dict[str, Any]:
    if complexity not in PROMPT_COMPLEXITY_PROFILES:
        raise BacklogError(f"Unsupported prompt complexity: {complexity}")
    prompt = prompt_text.strip()
    if not prompt:
        raise BacklogError("Prompt text is required for prompt tuning.")
    profiles = build_prompt_profiles(profile, analysis=analysis)
    selected = profiles[complexity]
    lines = [
        f"You are working in the {profile.project_name} repo under the local Blackdog contract.",
        "Use the repo-local Blackdog CLI when available and keep kept implementation changes in a branch-backed task worktree.",
        "",
        f"Complexity profile: {selected['label']}",
        selected["summary"],
        "",
        "Repo-specific operating rules:",
        "- Review the routed docs before kept edits.",
        "- If implementation is needed, start with `blackdog worktree preflight` and move to a task worktree before editing.",
        "- Prefer the local backlog/runtime contract over ad hoc coordination.",
    ]
    if selected["routed_docs"]:
        lines.extend(["", "Routed docs:"])
        lines.extend(f"- {item}" for item in selected["routed_docs"])
    if selected["validation_commands"]:
        lines.extend(["", "Validation defaults:"])
        lines.extend(f"- {item}" for item in selected["validation_commands"])
    calibrated_defaults = selected.get("calibrated_task_shape_defaults") or {}
    if calibrated_defaults:
        lines.extend(["", "Calibrated task-shaping defaults by effort:"])
        for effort in ("S", "M", "L"):
            defaults = calibrated_defaults.get(effort) or {}
            elapsed = defaults.get("estimated_elapsed_minutes")
            active = defaults.get("estimated_active_minutes")
            validation = defaults.get("estimated_validation_minutes")
            sample_size = defaults.get("sample_size")
            if elapsed is None or active is None:
                continue
            lines.append(
                f"- {effort}: elapsed {elapsed}m, active {active}m, validation {validation}m, sample size {sample_size}"
            )
    lines.extend(["", "Required prompt sections:"])
    lines.extend(f"- {item}" for item in selected["recommended_sections"])
    lines.extend(
        [
            "",
            "Prompt-tuning focus:",
            f"- {selected['focus']['category']}: {selected['focus']['summary']}",
            f"- Require explicit estimate and runtime capture: {selected['prompt_strategy']['require_explicit_estimates']}",
            "",
            "User request:",
            prompt,
        ]
    )
    improved_prompt = "\n".join(lines).strip()
    return {
        "project_name": profile.project_name,
        "complexity": complexity,
        "original_prompt": prompt,
        "improved_prompt": improved_prompt,
        "prompt_profile": selected,
        "tune_focus": analysis["recommendation"],
        "tuning_categories": analysis["categories"],
        "context_budget": {
            "doc_limit": selected["context_budget"]["doc_limit"],
            "validation_count": len(selected["validation_commands"]),
            "routed_doc_count": len(selected["routed_docs"]),
            "packet_bytes": len(improved_prompt.encode("utf-8")),
        },
    }


def build_tune_analysis(profile: Profile) -> dict[str, Any]:
    with locked_path(profile.paths.backlog_file):
        snapshot = load_backlog(profile.paths, profile)
    state = sync_state_for_backlog(load_state(profile.paths.state_file), snapshot)
    events = load_events(profile.paths)
    results = load_task_results(profile.paths)
    runtime_rows = _task_runtime_rows(snapshot=snapshot, state=state, events=events, results=results)
    calibration = _build_task_shaping_calibration(snapshot=snapshot, state=state, events=events, results=results)

    result_files = len(results)
    tasks_with_recorded_compute = sum(1 for row in runtime_rows.values() if row.get("actual_task_seconds") is not None)
    completed_tasks_with_recorded_compute = sum(
        1
        for task_id, row in runtime_rows.items()
        if row.get("actual_task_seconds") is not None and task_done(task_id, state)
    )
    results_with_actual_task_telemetry = sum(1 for row in results if _result_has_actual_task_telemetry(row))
    total_task_minutes = sum(int(row.get("actual_task_minutes") or 0) for row in runtime_rows.values())
    completed_task_minutes = [
        int(row.get("actual_task_minutes") or 0)
        for task_id, row in runtime_rows.items()
        if row.get("actual_task_minutes") is not None and task_done(task_id, state)
    ]
    average_completed_task_minutes = (
        round(sum(completed_task_minutes) / len(completed_task_minutes)) if completed_task_minutes else None
    )

    sample_rows: list[dict[str, Any]] = []
    for task in snapshot.tasks.values():
        runtime = runtime_rows.get(task.id)
        if runtime is None:
            continue
        actual_minutes = runtime.get("actual_task_minutes")
        estimate_minutes = runtime.get("estimated_active_minutes")
        if estimate_minutes is None:
            estimate_minutes = runtime.get("estimated_elapsed_minutes")
        if actual_minutes is None or estimate_minutes is None:
            continue
        delta_minutes = int(actual_minutes) - int(estimate_minutes)
        sample_rows.append(
            {
                "task_id": task.id,
                "title": task.title,
                "estimate_minutes": int(estimate_minutes),
                "actual_minutes": int(actual_minutes),
                "delta_minutes": delta_minutes,
            }
        )

    mean_absolute_error = (
        round(sum(abs(row["delta_minutes"]) for row in sample_rows) / len(sample_rows), 2) if sample_rows else None
    )
    underestimated_tasks = sum(1 for row in sample_rows if row["delta_minutes"] > 0)
    overestimated_tasks = sum(1 for row in sample_rows if row["delta_minutes"] < 0)
    retry_total = sum(int(row.get("actual_retry_count") or 0) for row in runtime_rows.values())
    reclaim_total = sum(int(row.get("actual_reclaim_count") or 0) for row in runtime_rows.values())
    landing_failures = sum(int(row.get("landing_failures") or 0) for row in runtime_rows.values())
    context_rows = [_task_context_metrics(task, runtime_rows.get(task.id, _task_runtime_row(task.id))) for task in snapshot.tasks.values()]
    completed_context_rows = [
        _task_context_metrics(task, runtime_rows.get(task.id, _task_runtime_row(task.id)))
        for task in snapshot.tasks.values()
        if task_done(task.id, state)
    ]

    def _average(values: list[int | float | None]) -> float | None:
        filtered = [float(value) for value in values if value is not None]
        if not filtered:
            return None
        return round(sum(filtered) / len(filtered), 2)

    coverage_gaps: list[str] = []
    if tasks_with_recorded_compute and len(sample_rows) < max(5, tasks_with_recorded_compute // 4):
        coverage_gaps.append("Most completed tasks still lack both an estimate snapshot and a comparable actual task-time sample.")
    if result_files and results_with_actual_task_telemetry < max(5, result_files // 4):
        coverage_gaps.append("Most structured results still lack explicit actual task-time telemetry fields.")
    calibration_ready = len(sample_rows) >= max(3, tasks_with_recorded_compute // 5) if tasks_with_recorded_compute else False

    enough_data = bool(tasks_with_recorded_compute or retry_total or landing_failures)
    if not enough_data:
        recommendation = {
            "focus": "collect_task_time_history",
            "summary": "Collect claim-derived task-time history before attempting backlog tuning.",
        }
    elif coverage_gaps:
        recommendation = {
            "focus": "task_shaping_coverage",
            "summary": "Increase estimate and actual task-time coverage so tune can compare more completed work with the same contract.",
        }
    elif not calibration_ready:
        recommendation = {
            "focus": "task_time_calibration",
            "summary": "Coverage is improving, but Blackdog still needs more comparable estimate-vs-actual samples before tightening prompt defaults.",
        }
    elif mean_absolute_error is not None and mean_absolute_error >= 10:
        recommendation = {
            "focus": "task_time_calibration",
            "summary": "Recorded task time is diverging from stored estimates; recalibrate active-minute defaults against the task history.",
        }
    elif landing_failures:
        recommendation = {
            "focus": "landing_failures",
            "summary": "Tune the WTAM flow around repeated landing failures before expanding more parallel work.",
        }
    elif retry_total:
        recommendation = {
            "focus": "retry_pressure",
            "summary": "Tune task boundaries and queue sequencing to reduce repeat attempts on the same task ids.",
        }
    else:
        recommendation = {
            "focus": "backlog_health",
            "summary": "Task-time history is stable enough to review for the next smaller calibration win.",
        }

    categories = {
        "time": {
            "tasks_with_recorded_compute": tasks_with_recorded_compute,
            "completed_tasks_with_recorded_compute": completed_tasks_with_recorded_compute,
            "estimated_time_samples": len(sample_rows),
            "total_task_minutes": total_task_minutes,
            "average_completed_task_minutes": average_completed_task_minutes,
            "timing_mean_absolute_error_minutes": mean_absolute_error,
            "underestimated_tasks": underestimated_tasks,
            "overestimated_tasks": overestimated_tasks,
        },
        "missteps": {
            "retry_total": retry_total,
            "reclaim_total": reclaim_total,
            "landing_failures": landing_failures,
            "tasks_with_missteps": sum(1 for row in runtime_rows.values() if int(row.get("actual_retry_count") or 0) or int(row.get("actual_reclaim_count") or 0) or int(row.get("landing_failures") or 0)),
        },
        "document_use_value": {
            "completed_tasks_with_routed_docs": sum(1 for row in completed_context_rows if int(row.get("context_doc_count") or 0) > 0),
            "average_routed_doc_count": _average([row.get("context_doc_count") for row in completed_context_rows]),
            "positive_proxy_tasks": sum(1 for row in completed_context_rows if int(row.get("document_routing_value_score") or 0) >= 2),
            "average_document_routing_value_score": _average([row.get("document_routing_value_score") for row in completed_context_rows]),
        },
        "context_efficiency": {
            "tasks_with_context_metrics": len(context_rows),
            "tasks_with_estimate_context": sum(1 for row in context_rows if int(row.get("context_estimate_field_count") or 0) > 0),
            "average_context_packet_score": _average([row.get("context_packet_score") for row in context_rows]),
            "average_context_packet_bytes": _average([row.get("context_packet_bytes") for row in context_rows]),
            "average_context_efficiency_ratio": _average([row.get("context_efficiency_ratio") for row in completed_context_rows]),
        },
        "calibration": {
            "calibration_ready": calibration_ready,
            "default_active_ratio": calibration.get("default_active_ratio"),
            "effort_profiles": calibration.get("by_effort", {}),
        },
    }

    return {
        "enough_data": enough_data,
        "result_files": result_files,
        "tasks_with_recorded_compute": tasks_with_recorded_compute,
        "completed_tasks_with_recorded_compute": completed_tasks_with_recorded_compute,
        "results_with_actual_task_telemetry": results_with_actual_task_telemetry,
        "estimated_time_samples": len(sample_rows),
        "total_task_minutes": total_task_minutes,
        "average_completed_task_minutes": average_completed_task_minutes,
        "timing_mean_absolute_error_minutes": mean_absolute_error,
        "underestimated_tasks": underestimated_tasks,
        "overestimated_tasks": overestimated_tasks,
        "retry_total": retry_total,
        "reclaim_total": reclaim_total,
        "landing_failures": landing_failures,
        "calibration_ready": calibration_ready,
        "calibration": calibration,
        "coverage_gaps": coverage_gaps,
        "categories": categories,
        "recommendation": recommendation,
        "top_timing_outliers": sorted(sample_rows, key=lambda row: abs(row["delta_minutes"]), reverse=True)[:3],
    }


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


def _tune_task_payload(profile: Profile) -> dict[str, Any]:
    analysis = build_tune_analysis(profile)
    paths = _unique_ordered(
        [
            str(profile.paths.backlog_file),
            str(profile.paths.state_file),
            str(profile.paths.events_file),
            str(profile.paths.inbox_file),
            str(profile.paths.results_dir),
            str(profile.paths.profile_file),
            str(profile.paths.html_file),
            str(profile.paths.skill_dir / "SKILL.md"),
            str(profile.paths.skill_dir / "agents/openai.yaml"),
        ]
    )
    try:
        skill_docs = [
            str((profile.paths.skill_dir / "SKILL.md").resolve().relative_to(profile.paths.project_root.resolve())),
            str(
                (profile.paths.skill_dir / "agents" / "openai.yaml")
                .resolve()
                .relative_to(profile.paths.project_root.resolve())
            ),
        ]
    except ValueError:
        skill_docs = [
            str(profile.paths.skill_dir / "SKILL.md"),
            str(profile.paths.skill_dir / "agents" / "openai.yaml"),
        ]
    docs = _unique_ordered(
        [
            "AGENTS.md",
            *skill_docs,
            "blackdog.toml",
            "docs/CLI.md",
            "docs/FILE_FORMATS.md",
            "docs/INTEGRATION.md",
            *profile.doc_routing_defaults,
        ]
    )
    evidence_bits = [
        f"{analysis['tasks_with_recorded_compute']} tasks with recorded task time",
        f"{analysis['estimated_time_samples']} estimate-vs-actual timing samples",
        f"{analysis['retry_total']} retries",
        f"{analysis['landing_failures']} landing failures",
        f"{analysis['results_with_actual_task_telemetry']}/{analysis['result_files']} results with explicit actual telemetry",
    ]
    safe_first_slice = [
        (
            "1. Review the recorded runtime history for "
            f"{analysis['tasks_with_recorded_compute']} task(s) with task-time data "
            f"and {analysis['estimated_time_samples']} comparable estimate samples."
        ),
    ]
    if analysis["coverage_gaps"]:
        safe_first_slice.append(
            "2. Confirm the current coverage gaps and make sure new result rows preserve both estimate snapshots and actual task time."
        )
    else:
        safe_first_slice.append(
            "2. Compare recorded task time against the current estimate contract and identify the largest recurrent mismatch."
        )
    safe_first_slice.append(
        "3. Draft one concrete tuning task focused on "
        f"{analysis['recommendation']['focus'].replace('_', ' ')}: {analysis['recommendation']['summary']}"
    )
    return {
        "title": "Auto-tune runtime contract and backlog health",
        "bucket": "skills",
        "priority": "P1",
        "risk": "low",
        "effort": "S",
        "paths": paths,
        "checks": list(profile.validation_commands),
        "docs": docs,
        "domains": list(profile.domains),
        "packages": [],
        "objective": "TUNING",
        "why": (
            "Analyze the recorded task-time history, runtime outcomes, and the local profile/skill contract, "
            "then propose the next highest-confidence tuning slice."
        ),
        "evidence": (
            "Runtime history currently shows "
            + ", ".join(evidence_bits)
            + ". Use this task to turn those signals into one concrete repo-local tuning task."
        ),
        "safe_first_slice": "\n".join(safe_first_slice),
        "requires_approval": False,
        "approval_reason": "",
        "epic_title": "Self-tuning queue",
        "epic_id": "epic-blackdog-tune",
        "lane_title": "Self-tuning lane",
        "lane_id": "lane-blackdog-tune",
        "wave": 0,
    }


def seed_tune_task(profile: Profile) -> tuple[dict[str, Any], bool]:
    template = _tune_task_payload(profile)
    target_task_id = make_task_id(profile, bucket=template["bucket"], title=template["title"], paths=template["paths"])
    with locked_path(profile.paths.backlog_file):
        snapshot = load_backlog(profile.paths, profile)
        existing = snapshot.tasks.get(target_task_id)
        if existing is not None:
            return existing.payload, False

    try:
        payload = add_task(
            profile,
            title=template["title"],
            bucket=template["bucket"],
            priority=template["priority"],
            risk=template["risk"],
            effort=template["effort"],
            why=template["why"],
            evidence=template["evidence"],
            safe_first_slice=template["safe_first_slice"],
            paths=template["paths"],
            checks=template["checks"],
            docs=template["docs"],
            domains=template["domains"],
            packages=template["packages"],
            affected_paths=template["paths"],
            task_shaping=None,
            objective=template["objective"],
            requires_approval=template["requires_approval"],
            approval_reason=template["approval_reason"],
            epic_id=template["epic_id"],
            epic_title=template["epic_title"],
            lane_id=template["lane_id"],
            lane_title=template["lane_title"],
            wave=template["wave"],
        )
    except BacklogError:
        with locked_path(profile.paths.backlog_file):
            snapshot = load_backlog(profile.paths, profile)
            existing = snapshot.tasks.get(target_task_id)
            if existing is not None:
                return existing.payload, False
        raise

    return payload, True


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


def sweep_completed_tasks(profile: Profile) -> dict[str, Any]:
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
    profile: Profile,
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
    profile: Profile,
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


def remove_task(profile: Profile, *, task_id: str) -> dict[str, Any]:
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
    profile: Profile,
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
