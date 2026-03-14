from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha1
from html import escape as html_escape
from pathlib import Path
from typing import Any
import json
import re
import subprocess
import textwrap

from .config import Profile, ProjectPaths, slugify
from .store import claim_is_active, parse_datetime


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


class BacklogError(RuntimeError):
    pass


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
    lane_positions: dict[str, tuple[str, str, int, int, tuple[str, ...]]] = {}
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
            lane_positions[task_id] = (lane_id, lane_title, wave, lane_order_by_id.get(lane_id, 0), task_ids[:index])
    tasks: dict[str, TaskInfo] = {}
    for payload in _extract_json_blocks(text, "backlog-task"):
        validate_task_payload(payload, profile)
        task_id = str(payload["id"])
        lane_id, lane_title, wave, lane_order, predecessors = lane_positions.get(task_id, (None, None, None, None, ()))
        tasks[task_id] = TaskInfo(
            payload=payload,
            narrative=narratives.get(task_id, TaskNarrative("", "", ())),
            epic_title=epics.get(task_id),
            lane_id=lane_id,
            lane_title=lane_title,
            wave=wave,
            lane_order=lane_order,
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
    return bool(isinstance(entry, dict) and entry.get("status") == "done")


def approval_satisfied(task: TaskInfo, state: dict[str, Any]) -> bool:
    if not bool(task.payload.get("requires_approval")):
        return True
    entry = state.get("approval_tasks", {}).get(task.id)
    return bool(isinstance(entry, dict) and str(entry.get("status") or "").strip() in {"approved", "done"})


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
        return "done", f"{detail} @ {completed_at}"
    owner = active_claim_owner(task.id, state)
    if owner:
        entry = state.get("task_claims", {}).get(task.id) or {}
        return "claimed", f"{owner} until {entry.get('claim_expires_at') or '?'}"
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
    seen_date = datetime.now().astimezone().date().isoformat()
    for task in snapshot.tasks.values():
        if not bool(task.payload.get("requires_approval")):
            continue
        entry = approvals.get(task.id) or {}
        if not isinstance(entry, dict):
            entry = {}
        entry.setdefault("status", "pending")
        entry.setdefault("first_seen", seen_date)
        entry["last_seen"] = seen_date
        entry["title"] = task.title
        entry["bucket"] = task.payload["bucket"]
        entry["paths"] = task.payload["paths"]
        entry["approval_reason"] = task.payload["approval_reason"]
        approvals[task.id] = entry
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
    objective_rows: dict[str, dict[str, Any]] = {row["id"]: {"id": row["id"], "title": row["title"], "total": 0, "done": 0} for row in _parse_objectives(snapshot)}
    tasks_by_lane: list[dict[str, Any]] = []
    tasks_sorted = sorted(snapshot.tasks.values(), key=lambda task: ((task.wave or 9999), (task.lane_order or 9999), task.id))
    for task in tasks_sorted:
        status, detail = classify_task_status(task, snapshot, state, allow_high_risk=allow_high_risk)
        counts[status] += 1
        objective_id = str(task.payload.get("objective") or "")
        if objective_id:
            objective_rows.setdefault(objective_id, {"id": objective_id, "title": objective_id, "total": 0, "done": 0})
            objective_rows[objective_id]["total"] += 1
            if status == "done":
                objective_rows[objective_id]["done"] += 1
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
        "objectives": list(objective_rows.values()),
        "open_messages": [row for row in messages if row.get("status") == "open"],
        "recent_events": events[-10:],
        "recent_results": results[:5],
        "push_objective": _section_items(snapshot.sections.get("Push Objective", [])),
    }


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


def render_html(view: dict[str, Any], output_path: Path) -> None:
    lane_html = []
    for lane in view["lanes"]:
        cards = []
        for task in lane["tasks"]:
            domains = "".join(f"<span class=\"chip\">{html_escape(str(item))}</span>" for item in task["domains"])
            cards.append(
                f"""
                <article class="task-card task-{html_escape(task['status'])}">
                  <div class="task-top">
                    <code>{html_escape(task['id'])}</code>
                    <button type="button" class="copy-button" data-copy="{html_escape(task['id'])}">Copy</button>
                  </div>
                  <h3>{html_escape(task['title'])}</h3>
                  <p class="meta">{html_escape(task['status'])} | {html_escape(task['detail'])}</p>
                  <p>{html_escape(task['safe_first_slice'])}</p>
                  <div class="chips">{domains}</div>
                </article>
                """
            )
        lane_html.append(
            f"""
            <section class="lane">
              <div class="lane-head">
                <span class="eyebrow">Wave {html_escape(str(lane['wave'] if lane['wave'] is not None else 'unplanned'))}</span>
                <h2>{html_escape(lane['title'])}</h2>
              </div>
              {''.join(cards) or '<p>No tasks in this lane.</p>'}
            </section>
            """
        )

    objective_html = "".join(
        f"""
        <article class="objective">
          <span class="eyebrow">{html_escape(row['id'])}</span>
          <strong>{html_escape(row['title'])}</strong>
          <span>{row['done']}/{row['total']}</span>
        </article>
        """
        for row in view["objectives"]
    ) or "<article class=\"objective\"><strong>No objectives tagged yet</strong></article>"

    next_html = "".join(
        f"<li><code>{html_escape(row['id'])}</code> {html_escape(row['title'])}</li>" for row in view["next_rows"]
    ) or "<li>No runnable tasks.</li>"
    inbox_html = "".join(
        f"<li><code>{html_escape(row['message_id'])}</code> {html_escape(row['sender'])} -> {html_escape(row['recipient'])}: {html_escape(row['body'])}</li>"
        for row in view["open_messages"][:6]
    ) or "<li>No open messages.</li>"
    result_html = "".join(
        f"<li><code>{html_escape(str(row.get('task_id') or '?'))}</code> {html_escape(str(row.get('status') or '?'))}</li>"
        for row in view["recent_results"][:6]
    ) or "<li>No task results yet.</li>"
    push_lines = "".join(f"<li>{html_escape(item)}</li>" for item in view["push_objective"]) or "<li>No push objective written yet.</li>"
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_escape(view['project_name'])} Backlog</title>
  <style>
    :root {{
      --bg: #f7f1ea;
      --panel: rgba(255, 252, 248, 0.95);
      --ink: #201913;
      --muted: #6d635b;
      --line: rgba(57, 43, 28, 0.14);
      --ready: #9c5a13;
      --claimed: #155fc1;
      --done: #12724a;
      --waiting: #7b8088;
      --approval: #7b4bb7;
      --risk: #a12626;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, rgba(208, 168, 97, 0.2), transparent 28%),
        linear-gradient(180deg, #fbf7f1 0%, var(--bg) 100%);
    }}
    .page {{ max-width: 1500px; margin: 0 auto; padding: 28px; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 20px;
      margin-bottom: 18px;
    }}
    .hero {{ display: grid; grid-template-columns: 1.4fr 0.9fr; gap: 18px; }}
    .stats {{ display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }}
    .stat {{ border: 1px solid var(--line); border-radius: 16px; padding: 12px; background: rgba(255,255,255,0.72); }}
    .stat strong {{ display: block; font-size: 1.6rem; }}
    .eyebrow {{ color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.75rem; }}
    .objectives {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
    .objective {{ border: 1px solid var(--line); border-radius: 18px; padding: 14px; background: rgba(255,255,255,0.72); display: grid; gap: 6px; }}
    .layout {{ display: grid; grid-template-columns: 0.95fr 1.55fr; gap: 18px; }}
    .lane-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: 14px; }}
    .lane {{ border: 1px solid var(--line); border-radius: 20px; padding: 16px; background: rgba(255,255,255,0.72); }}
    .task-card {{ border: 1px solid var(--line); border-radius: 18px; padding: 14px; background: rgba(255,255,255,0.86); margin-top: 12px; }}
    .task-ready {{ border-color: rgba(156, 90, 19, 0.35); }}
    .task-claimed {{ border-color: rgba(21, 95, 193, 0.35); }}
    .task-done {{ border-color: rgba(18, 114, 74, 0.35); }}
    .task-waiting, .task-high-risk {{ border-color: rgba(123, 128, 136, 0.35); }}
    .task-approval {{ border-color: rgba(123, 75, 183, 0.35); }}
    .task-top {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; }}
    .meta {{ color: var(--muted); font-size: 0.92rem; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
    .chip {{ display: inline-flex; padding: 4px 8px; border: 1px solid var(--line); border-radius: 999px; font-size: 0.8rem; }}
    ul {{ padding-left: 20px; }}
    code {{ background: #f3ede4; border-radius: 4px; padding: 0.1rem 0.3rem; }}
    .copy-button {{
      border: 1px solid var(--line);
      background: white;
      border-radius: 999px;
      padding: 6px 10px;
      cursor: pointer;
    }}
    @media (max-width: 900px) {{
      .hero, .layout, .objectives {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <section class="panel hero">
      <div>
        <span class="eyebrow">Blackdog backlog</span>
        <h1>{html_escape(view['project_name'])}</h1>
        <p>{html_escape(" ".join(view['push_objective'][:2]) or "Structured backlog control for local AI-assisted development.")}</p>
        <div class="stats">
          <div class="stat"><span class="eyebrow">Total</span><strong>{view['total']}</strong></div>
          <div class="stat"><span class="eyebrow">Ready</span><strong>{view['counts']['ready']}</strong></div>
          <div class="stat"><span class="eyebrow">Claimed</span><strong>{view['counts']['claimed']}</strong></div>
          <div class="stat"><span class="eyebrow">Done</span><strong>{view['counts']['done']}</strong></div>
          <div class="stat"><span class="eyebrow">Approval</span><strong>{view['counts']['approval']}</strong></div>
          <div class="stat"><span class="eyebrow">Open inbox</span><strong>{len(view['open_messages'])}</strong></div>
        </div>
      </div>
      <div>
        <h2>Next Runnable</h2>
        <ul>{next_html}</ul>
        <h2>Push Objective</h2>
        <ul>{push_lines}</ul>
      </div>
    </section>
    <section class="panel">
      <h2>Objectives</h2>
      <div class="objectives">{objective_html}</div>
    </section>
    <div class="layout">
      <section class="panel">
        <h2>Inbox</h2>
        <ul>{inbox_html}</ul>
        <h2>Recent Results</h2>
        <ul>{result_html}</ul>
      </section>
      <section class="panel">
        <h2>Lane View</h2>
        <div class="lane-grid">{''.join(lane_html)}</div>
      </section>
    </div>
  </div>
  <script>
    document.querySelectorAll('[data-copy]').forEach((button) => {{
      button.addEventListener('click', async () => {{
        const value = button.getAttribute('data-copy') || '';
        const original = button.textContent;
        try {{
          await navigator.clipboard.writeText(value);
          button.textContent = 'Copied';
        }} catch (error) {{
          button.textContent = 'Copy failed';
        }}
        setTimeout(() => {{
          button.textContent = original;
        }}, 1200);
      }});
    }});
  </script>
</body>
</html>
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(body, encoding="utf-8")


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
    text = profile.paths.backlog_file.read_text(encoding="utf-8")
    updated = _apply_runtime_headers(text, profile)
    if updated != text:
        profile.paths.backlog_file.write_text(updated, encoding="utf-8")


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
    objective: str,
    requires_approval: bool,
    approval_reason: str,
    epic_id: str | None,
    epic_title: str | None,
    lane_id: str | None,
    lane_title: str | None,
    wave: int | None,
) -> dict[str, Any]:
    snapshot = load_backlog(profile.paths, profile)
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
    profile.paths.backlog_file.write_text(updated, encoding="utf-8")
    return payload


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
