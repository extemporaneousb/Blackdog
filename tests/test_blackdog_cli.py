from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from blackdog import backlog as backlog_module
from blackdog import store as store_module
from blackdog.backlog import load_backlog, render_backlog_plan_block, render_task_section
from blackdog.cli import main as blackdog_main
from blackdog.config import load_profile, render_default_profile
from blackdog.skill_cli import main as blackdog_skill_main
from blackdog.store import append_jsonl, atomic_write_text, load_events, load_inbox, load_jsonl, record_task_result, resolve_message, save_state, send_message
from blackdog.supervisor import _build_child_prompt, _resolved_launch_command, _write_run_status
from blackdog.ui import build_ui_snapshot, render_static_html
from blackdog.worktree import WorktreeSpec


def cli_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC) if not existing else f"{SRC}:{existing}"
    return env


def run_cli(*args: str) -> int:
    return blackdog_main(list(args))


def run_skill_cli(*args: str) -> int:
    return blackdog_skill_main(list(args))


def wait_for_file(path: Path, *, timeout: float = 5.0) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return path
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {path}")


def wait_for_glob(root: Path, pattern: str, *, timeout: float = 5.0) -> Path:
    deadline = time.time() + timeout
    while time.time() < deadline:
        matches = sorted(root.glob(pattern))
        if matches:
            return matches[0]
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {pattern} under {root}")


def wait_for_json(path: Path, predicate, *, timeout: float = 5.0) -> dict[str, object]:
    deadline = time.time() + timeout
    last_payload: dict[str, object] | None = None
    while time.time() < deadline:
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.05)
                continue
            last_payload = payload
            if predicate(payload):
                return payload
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for JSON predicate on {path}; last payload={last_payload}")


def html_snapshot(path: Path) -> dict[str, object]:
    html = path.read_text(encoding="utf-8")
    match = re.search(r'<script id="blackdog-snapshot" type="application/json">(.*?)</script>', html, re.S)
    if match is None:
        raise AssertionError(f"Could not find embedded snapshot in {path}")
    return json.loads(match.group(1))


def wait_for_html_snapshot(path: Path, predicate, *, timeout: float = 5.0) -> dict[str, object]:
    deadline = time.time() + timeout
    last_payload: dict[str, object] | None = None
    while time.time() < deadline:
        if path.exists():
            try:
                payload = html_snapshot(path)
            except (AssertionError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            last_payload = payload
            if predicate(payload):
                return payload
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for HTML snapshot predicate on {path}; last payload={last_payload}")


def task_ids_by_title(root: Path) -> dict[str, str]:
    profile = load_profile(root)
    snapshot = load_backlog(profile.paths, profile)
    return {task.title: task.id for task in snapshot.tasks.values()}


def install_exec_launcher(root: Path, script_body: str, *, commit_message: str) -> Path:
    launcher_script = root / "codex"
    launcher_script.write_text(script_body.strip() + "\n", encoding="utf-8")
    launcher_script.chmod(0o755)
    profile_text = (root / "blackdog.toml").read_text(encoding="utf-8").replace(
        'launch_command = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]',
        f'launch_command = ["{launcher_script}", "exec", "--dangerously-bypass-approvals-and-sandbox"]',
    )
    (root / "blackdog.toml").write_text(profile_text, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "blackdog.toml", "codex"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", commit_message],
        check=True,
        capture_output=True,
        text=True,
    )
    return launcher_script


def remove_task_from_backlog(root: Path, task_id: str) -> None:
    profile = load_profile(root)
    snapshot = load_backlog(profile.paths, profile)
    plan = json.loads(json.dumps(snapshot.plan))
    for collection_name in ("epics", "lanes"):
        for entry in plan.get(collection_name, []):
            entry["task_ids"] = [str(value) for value in entry.get("task_ids", []) if str(value) != task_id]

    plan_block = "```json backlog-plan\n" + json.dumps(plan, indent=2, sort_keys=False) + "\n```"
    updated = re.sub(r"```json backlog-plan\n.*?\n```", plan_block.rstrip(), snapshot.raw_text, count=1, flags=re.S)
    task_section_re = re.compile(
        rf"^###\s+{re.escape(task_id)}\s+-\s+.+?(?=^###\s+[A-Z0-9]+-[0-9a-f]+\s+-|\Z)",
        re.S | re.M,
    )
    updated = task_section_re.sub("", updated, count=1).rstrip() + "\n"
    profile.paths.backlog_file.write_text(updated, encoding="utf-8")


def seed_large_runtime_fixture(root: Path, *, task_count: int = 360, lane_count: int = 30):
    run_cli("init", "--project-root", str(root), "--project-name", "Scale Demo")
    profile = load_profile(root)
    paths = profile.paths
    task_ids = [f"{profile.id_prefix}-{index:010x}" for index in range(task_count)]
    backlog_text = paths.backlog_file.read_text(encoding="utf-8")
    plan = {
        "epics": [
            {
                "id": "epic-scale-regression",
                "title": "Scale Regression",
                "task_ids": list(task_ids),
            }
        ],
        "lanes": [],
    }
    task_sections: list[str] = []
    tasks_per_lane = max(1, (task_count + lane_count - 1) // lane_count)
    for lane_index in range(lane_count):
        lane_task_ids = task_ids[lane_index * tasks_per_lane : (lane_index + 1) * tasks_per_lane]
        if not lane_task_ids:
            break
        plan["lanes"].append(
            {
                "id": f"lane-scale-{lane_index:02d}",
                "title": f"Scale Lane {lane_index:02d}",
                "task_ids": lane_task_ids,
                "wave": lane_index // 6,
            }
        )
    for index, task_id in enumerate(task_ids):
        title = f"Synthetic scale task {index:03d}"
        payload = {
            "id": task_id,
            "title": title,
            "bucket": "html" if index % 3 == 0 else "core",
            "priority": "P1" if index % 7 == 0 else "P2",
            "risk": "medium" if index % 5 else "low",
            "effort": "M" if index % 4 else "S",
            "packages": [],
            "paths": [
                f"src/scale/module_{index % 12:02d}.py",
                f"docs/scale/guide_{index % 8:02d}.md",
                f"tests/fixtures/scale_{index % 5:02d}.json",
            ],
            "checks": ["PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'"],
            "docs": ["AGENTS.md", "docs/CLI.md", "docs/FILE_FORMATS.md"],
            "objective": f"OBJ-{(index % 3) + 1}",
            "domains": ["html", "results", "events"],
            "requires_approval": False,
            "approval_reason": "",
            "safe_first_slice": "Profile one narrow slice against a large synthetic backlog.",
        }
        why = (
            "Synthetic scale coverage should reflect a backlog large enough to exercise parsing, plan grouping, "
            "and UI snapshot assembly under realistic repository-sized task inventories. "
            "This text is intentionally long enough to make the backlog markdown file meaningfully large."
        )
        evidence = (
            "The benchmark fixture populates many lanes, results, events, and inbox rows so the test covers the "
            "real code paths used by snapshot generation and runtime append handling rather than a toy shortcut."
        )
        task_sections.append(
            render_task_section(
                payload,
                why=why,
                evidence=evidence,
                affected_paths=list(payload["paths"]),
            )
        )
    updated = re.sub(
        r"```json backlog-plan\n.*?\n```",
        render_backlog_plan_block(plan).rstrip(),
        backlog_text,
        count=1,
        flags=re.S,
    )
    paths.backlog_file.write_text(updated.rstrip() + "\n\n" + "\n\n".join(task_sections) + "\n", encoding="utf-8")

    now = datetime.now().astimezone()
    state = {"schema_version": 1, "approval_tasks": {}, "task_claims": {}}
    events_rows: list[dict[str, object]] = []
    inbox_rows: list[dict[str, object]] = []
    for index, task_id in enumerate(task_ids):
        title = f"Synthetic scale task {index:03d}"
        claimed_at = (now - timedelta(minutes=task_count - index + 5)).isoformat(timespec="seconds")
        completed_at = (now - timedelta(minutes=task_count - index)).isoformat(timespec="seconds")
        released_at = (now - timedelta(minutes=task_count - index + 1)).isoformat(timespec="seconds")
        claim_entry = {
            "title": title,
            "claimed_by": f"scale-bot-{index % 4}",
            "claimed_at": claimed_at,
            "claim_expires_at": (now + timedelta(hours=2)).isoformat(timespec="seconds"),
            "bucket": "html" if index % 3 == 0 else "core",
            "priority": "P1" if index % 7 == 0 else "P2",
            "risk": "medium" if index % 5 else "low",
            "paths": [f"src/scale/module_{index % 12:02d}.py"],
        }
        if index % 4 == 0:
            claim_entry.update({"status": "done", "completed_at": completed_at, "completed_by": "scale-bot"})
        elif index % 4 == 1:
            claim_entry.update({"status": "claimed"})
        elif index % 4 == 2:
            claim_entry.update({"status": "released", "released_at": released_at})
        else:
            claim_entry = None
        if claim_entry is not None:
            state["task_claims"][task_id] = claim_entry

        result_recorded_at = (now - timedelta(minutes=index)).isoformat(timespec="seconds")
        result_payload = {
            "schema_version": 1,
            "task_id": task_id,
            "recorded_at": result_recorded_at,
            "actor": f"scale-bot-{index % 4}",
            "run_id": f"run-{index:06x}",
            "status": "success" if index % 6 else "partial",
            "what_changed": [
                "Synthesized one representative result entry for scale coverage.",
                "Recorded enough narrative detail to keep result payloads realistic.",
            ],
            "validation": [
                "synthetic-scale-fixture",
                "snapshot-regression",
            ],
            "residual": [
                "This fixture is intentionally synthetic and should only be used for performance guardrails.",
            ],
            "needs_user_input": False,
            "followup_candidates": [],
        }
        task_result_dir = paths.results_dir / task_id
        task_result_dir.mkdir(parents=True, exist_ok=True)
        result_file = task_result_dir / f"20260315-000000-{index:06x}.json"
        result_file.write_text(json.dumps(result_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        base_at = now - timedelta(minutes=task_count - index, seconds=index % 50)
        events_rows.append(
            {
                "event_id": f"evt-claim-{index:06x}",
                "type": "claim",
                "at": base_at.isoformat(timespec="seconds"),
                "actor": f"scale-bot-{index % 4}",
                "task_id": task_id,
                "payload": {"lease": "synthetic-scale"},
            }
        )
        events_rows.append(
            {
                "event_id": f"evt-result-{index:06x}",
                "type": "task_result",
                "at": (base_at + timedelta(seconds=15)).isoformat(timespec="seconds"),
                "actor": f"scale-bot-{index % 4}",
                "task_id": task_id,
                "payload": {
                    "status": result_payload["status"],
                    "run_id": result_payload["run_id"],
                    "result_file": str(result_file),
                    "needs_user_input": False,
                },
            }
        )
        if index % 4 == 0:
            events_rows.append(
                {
                    "event_id": f"evt-complete-{index:06x}",
                    "type": "complete",
                    "at": (base_at + timedelta(seconds=30)).isoformat(timespec="seconds"),
                    "actor": "scale-bot",
                    "task_id": task_id,
                    "payload": {"note": "synthetic completion"},
                }
            )
        elif index % 4 == 2:
            events_rows.append(
                {
                    "event_id": f"evt-release-{index:06x}",
                    "type": "release",
                    "at": (base_at + timedelta(seconds=30)).isoformat(timespec="seconds"),
                    "actor": "scale-bot",
                    "task_id": task_id,
                    "payload": {"reason": "synthetic release"},
                }
            )

    for index in range(max(90, task_count // 3)):
        message_id = f"msg-{index:06x}"
        task_id = task_ids[index % task_count]
        inbox_rows.append(
            {
                "action": "message",
                "message_id": message_id,
                "at": (now - timedelta(minutes=index)).isoformat(timespec="seconds"),
                "sender": "user" if index % 2 == 0 else "blackdog",
                "recipient": "supervisor" if index % 3 else "codex",
                "kind": "instruction" if index % 4 else "warning",
                "task_id": task_id,
                "reply_to": None,
                "tags": ["scale", "perf", f"lane-{index % lane_count:02d}"],
                "body": "Synthetic inbox message used to exercise replay and filtering under load.",
            }
        )
        if index % 3 == 0:
            inbox_rows.append(
                {
                    "action": "resolve",
                    "message_id": message_id,
                    "at": (now - timedelta(minutes=index - 1)).isoformat(timespec="seconds"),
                    "actor": "codex",
                    "note": "Synthetic resolution for replay coverage.",
                }
            )

    save_state(paths.state_file, state)
    paths.events_file.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in events_rows),
        encoding="utf-8",
    )
    paths.inbox_file.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in inbox_rows),
        encoding="utf-8",
    )
    return profile, task_ids


class BlackdogCliTests(unittest.TestCase):
    def runtime_paths(self):
        return load_profile(self.root).paths

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-b", "main", str(self.root)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "blackdog@example.com"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "Blackdog Test"], check=True, capture_output=True, text=True)
        (self.root / ".gitignore").write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", ".gitignore"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Initial test commit"],
            check=True,
            capture_output=True,
            text=True,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_init_add_and_summary(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        self.assertTrue((self.root / "blackdog.toml").exists())
        self.assertTrue(paths.backlog_file.exists())

        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Create first runnable task",
            "--bucket",
            "core",
            "--why",
            "The queue should contain one executable task.",
            "--evidence",
            "A new scaffold starts empty.",
            "--safe-first-slice",
            "Add a task and render the backlog view.",
            "--path",
            "README.md",
            "--epic-title",
            "Bootstrap",
            "--lane-title",
            "Bootstrap lane",
            "--wave",
            "0",
        )
        summary = subprocess.run(
            [sys.executable, "-m", "blackdog.cli", "summary", "--project-root", str(self.root), "--format", "json"],
            check=True,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        payload = json.loads(summary.stdout)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(len(payload["next_rows"]), 1)
        self.assertTrue(paths.html_file.exists())

    def test_atomic_write_text_preserves_last_complete_json_until_replace(self) -> None:
        state_file = self.root / "state.json"
        state_file.write_text('{"version": 1}\n', encoding="utf-8")
        replace_ready = threading.Event()
        allow_replace = threading.Event()

        def writer() -> None:
            atomic_write_text(
                state_file,
                '{"version": 2}\n',
                before_replace=lambda _temp_path: (replace_ready.set(), allow_replace.wait(timeout=5)),
            )

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(replace_ready.wait(timeout=5))
        self.assertEqual(json.loads(state_file.read_text(encoding="utf-8"))["version"], 1)
        allow_replace.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(json.loads(state_file.read_text(encoding="utf-8"))["version"], 2)

    def test_append_jsonl_preserves_last_complete_rows_until_replace(self) -> None:
        log_file = self.root / "events.jsonl"
        append_jsonl(log_file, {"event_id": "evt-1"})
        replace_ready = threading.Event()
        allow_replace = threading.Event()
        original_atomic_write = store_module.atomic_write_text

        def gated_atomic_write(path: Path, text: str, *, before_replace=None) -> None:
            def combined_before_replace(temp_path: Path) -> None:
                replace_ready.set()
                allow_replace.wait(timeout=5)
                if before_replace is not None:
                    before_replace(temp_path)

            original_atomic_write(path, text, before_replace=combined_before_replace)

        def writer() -> None:
            with patch("blackdog.store.atomic_write_text", side_effect=gated_atomic_write):
                append_jsonl(log_file, {"event_id": "evt-2"})

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(replace_ready.wait(timeout=5))
        self.assertEqual(load_jsonl(log_file), [{"event_id": "evt-1"}])
        allow_replace.set()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(load_jsonl(log_file), [{"event_id": "evt-1"}, {"event_id": "evt-2"}])

    def test_save_state_and_run_status_use_atomic_writes(self) -> None:
        state_file = self.root / "state.json"
        with patch("blackdog.store.atomic_write_text") as state_write:
            save_state(state_file, {"schema_version": 1, "approval_tasks": {}, "task_claims": {}})
        state_write.assert_called_once()

        status_file = self.root / "status.json"
        with patch("blackdog.supervisor.atomic_write_text") as status_write:
            _write_run_status(status_file, {"steps": []})
        status_write.assert_called_once()

    def test_scale_snapshot_render_and_atomic_event_append_stay_within_budget(self) -> None:
        profile, task_ids = seed_large_runtime_fixture(self.root)
        paths = profile.paths
        backlog_size = paths.backlog_file.stat().st_size
        events_size = paths.events_file.stat().st_size
        result_files = sorted(paths.results_dir.glob("*/*.json"))

        self.assertGreaterEqual(backlog_size, 200_000)
        self.assertGreaterEqual(events_size, 150_000)
        self.assertEqual(len(result_files), len(task_ids))

        started = time.perf_counter()
        snapshot = build_ui_snapshot(profile)
        snapshot_elapsed = time.perf_counter() - started

        started = time.perf_counter()
        render_static_html(snapshot, paths.html_file)
        render_elapsed = time.perf_counter() - started

        started = time.perf_counter()
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-scale-append",
                "type": "comment",
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "actor": "benchmark",
                "task_id": task_ids[-1],
                "payload": {"kind": "comment", "body": "Synthetic append after scale snapshot benchmark."},
            },
        )
        append_elapsed = time.perf_counter() - started

        self.assertEqual(snapshot["total"], len(task_ids))
        self.assertEqual(len(snapshot["tasks"]), len(task_ids))
        self.assertEqual(load_jsonl(paths.events_file)[-1]["event_id"], "evt-scale-append")
        self.assertLess(
            snapshot_elapsed,
            6.0,
            f"build_ui_snapshot took {snapshot_elapsed:.3f}s for backlog={backlog_size}B events={events_size}B results={len(result_files)}",
        )
        self.assertLess(
            render_elapsed,
            4.0,
            f"render_static_html took {render_elapsed:.3f}s for {len(task_ids)} tasks",
        )
        self.assertLess(
            append_elapsed,
            2.0,
            f"append_jsonl took {append_elapsed:.3f}s for events={events_size}B",
        )

    def test_add_task_serializes_overlapping_backlog_mutations(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        profile = load_profile(self.root)
        first_validate_entered = threading.Event()
        release_first_validate = threading.Event()
        validate_count = 0
        validate_count_lock = threading.Lock()
        errors: list[BaseException] = []
        original_validate = backlog_module.validate_task_payload

        def gated_validate(task: dict[str, object], task_profile) -> None:
            nonlocal validate_count
            original_validate(task, task_profile)
            with validate_count_lock:
                validate_count += 1
                current = validate_count
            if current == 1:
                first_validate_entered.set()
                if not release_first_validate.wait(timeout=5):
                    raise AssertionError("timed out waiting to release the first concurrent add")

        def add_slice(title: str) -> None:
            try:
                backlog_module.add_task(
                    profile,
                    title=title,
                    bucket="core",
                    priority="P2",
                    risk="medium",
                    effort="M",
                    why="Concurrent adds should preserve every task.",
                    evidence="Two overlapping add flows should not drop one task's plan/task update.",
                    safe_first_slice="Serialize the backlog rewrite around add_task.",
                    paths=["README.md"],
                    checks=[],
                    docs=[],
                    domains=[],
                    packages=[],
                    affected_paths=[],
                    objective="",
                    requires_approval=False,
                    approval_reason="",
                    epic_id=None,
                    epic_title="Concurrency",
                    lane_id=None,
                    lane_title="Concurrent adds",
                    wave=0,
                )
            except BaseException as exc:  # pragma: no cover - surfaced via assertion below
                errors.append(exc)

        with patch("blackdog.backlog.validate_task_payload", side_effect=gated_validate):
            first = threading.Thread(target=add_slice, args=("Overlap one",))
            second = threading.Thread(target=add_slice, args=("Overlap two",))
            first.start()
            self.assertTrue(first_validate_entered.wait(timeout=5))
            second.start()
            time.sleep(0.1)
            release_first_validate.set()
            first.join(timeout=5)
            second.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertFalse(errors, errors)
        snapshot = load_backlog(profile.paths, profile)
        titles = {task.title for task in snapshot.tasks.values()}
        self.assertIn("Overlap one", titles)
        self.assertIn("Overlap two", titles)

    def test_inbox_and_event_jsonl_writes_serialize_overlapping_updates(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        original_message = send_message(
            paths,
            sender="blackdog",
            recipient="supervisor",
            body="Initial message.",
            kind="instruction",
        )

        replace_ready = threading.Event()
        allow_replace = threading.Event()
        first_inbox_append_seen = threading.Event()
        errors: list[BaseException] = []
        original_append = store_module.append_jsonl

        def gated_append(path: Path, payload: dict[str, object]) -> None:
            if path == paths.inbox_file and not first_inbox_append_seen.is_set():
                first_inbox_append_seen.set()
                original_atomic_write = store_module.atomic_write_text

                def gated_atomic_write(target: Path, text: str, *, before_replace=None) -> None:
                    def combined_before_replace(temp_path: Path) -> None:
                        replace_ready.set()
                        allow_replace.wait(timeout=5)
                        if before_replace is not None:
                            before_replace(temp_path)

                    original_atomic_write(target, text, before_replace=combined_before_replace)

                with patch("blackdog.store.atomic_write_text", side_effect=gated_atomic_write):
                    original_append(path, payload)
                return
            original_append(path, payload)

        def send_followup() -> None:
            try:
                send_message(
                    paths,
                    sender="supervisor",
                    recipient="codex",
                    body="Follow-up message.",
                    kind="warning",
                )
            except BaseException as exc:  # pragma: no cover - surfaced via assertion below
                errors.append(exc)

        def resolve_original() -> None:
            try:
                resolve_message(paths, message_id=original_message["message_id"], actor="codex", note="Handled.")
            except BaseException as exc:  # pragma: no cover - surfaced via assertion below
                errors.append(exc)

        with patch("blackdog.store.append_jsonl", side_effect=gated_append):
            sender_thread = threading.Thread(target=send_followup)
            resolve_thread = threading.Thread(target=resolve_original)
            sender_thread.start()
            self.assertTrue(replace_ready.wait(timeout=5))
            resolve_thread.start()
            time.sleep(0.1)
            allow_replace.set()
            sender_thread.join(timeout=5)
            resolve_thread.join(timeout=5)

        self.assertFalse(sender_thread.is_alive())
        self.assertFalse(resolve_thread.is_alive())
        self.assertFalse(errors, errors)

        inbox_rows = load_inbox(paths)
        self.assertEqual(len(inbox_rows), 2)
        status_by_id = {row["message_id"]: row["status"] for row in inbox_rows}
        self.assertEqual(status_by_id[original_message["message_id"]], "resolved")
        self.assertIn("open", status_by_id.values())

        event_types = [row["type"] for row in load_events(paths)]
        self.assertEqual(event_types.count("message"), 2)
        self.assertEqual(event_types.count("message_resolved"), 1)

    def test_bootstrap_creates_skill_and_is_idempotent(self) -> None:
        payload = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "bootstrap",
                    "--project-root",
                    str(self.root),
                    "--project-name",
                    "Bootstrap Demo",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(Path(payload["project_root"]).resolve(), self.root.resolve())
        paths = self.runtime_paths()
        self.assertTrue((self.root / "blackdog.toml").exists())
        self.assertTrue(paths.backlog_file.exists())
        self.assertTrue((self.root / ".codex/skills/blackdog/SKILL.md").exists())

        second_payload = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "bootstrap",
                    "--project-root",
                    str(self.root),
                    "--project-name",
                    "Bootstrap Demo",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(second_payload["skill_file"], payload["skill_file"])

    def test_default_supervisor_launcher_prefers_desktop_exec_binary(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        desktop_launcher = self.root / "codex"
        desktop_launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        desktop_launcher.chmod(0o755)

        profile = load_profile(self.root)
        with patch("blackdog.supervisor.DESKTOP_CODEX_BINARY", desktop_launcher):
            command = _resolved_launch_command(profile)

        self.assertEqual(command[0], str(desktop_launcher))
        self.assertEqual(command[1:], ["exec", "--dangerously-bypass-approvals-and-sandbox"])

    def test_load_profile_uses_git_control_root(self) -> None:
        (self.root / "blackdog.toml").write_text(render_default_profile("Demo"), encoding="utf-8")

        paths = load_profile(self.root).paths

        self.assertEqual(paths.control_dir, (self.root / ".git/blackdog").resolve())
        self.assertEqual(paths.supervisor_runs_dir, (self.root / ".git/blackdog/supervisor-runs").resolve())

    def test_render_refreshes_backlog_headers_to_control_root_paths(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        backlog_text = paths.backlog_file.read_text(encoding="utf-8").replace(
            str(paths.state_file),
            "/tmp/stale/backlog-state.json",
        )
        paths.backlog_file.write_text(backlog_text, encoding="utf-8")

        run_cli("render", "--project-root", str(self.root), "--actor", "tester")

        summary = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "summary", "--project-root", str(self.root), "--format", "json"],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(summary["headers"]["State file"], str(paths.state_file))

    def test_backlog_namespace_commands_create_remove_and_reset_runtime_sets(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()

        created = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "backlog", "new", "--project-root", str(self.root), "Test Me"],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        named_dir = Path(created["backlog_dir"])
        self.assertEqual(named_dir, (paths.control_dir / "backlogs/test-me").resolve())
        self.assertTrue((named_dir / "backlog.md").exists())

        (paths.control_dir / "runtime-noise.log").write_text("noise\n", encoding="utf-8")
        (named_dir / "keep.txt").write_text("named backlog\n", encoding="utf-8")

        reset_payload = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "backlog", "reset", "--project-root", str(self.root)],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(Path(reset_payload["backlog_dir"]).resolve(), paths.backlog_dir.resolve())
        self.assertTrue(paths.backlog_file.exists())
        self.assertFalse((paths.control_dir / "runtime-noise.log").exists())
        self.assertTrue(named_dir.exists())
        self.assertTrue((named_dir / "keep.txt").exists())

        removed = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "backlog", "remove", "--project-root", str(self.root), "Test Me"],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(Path(removed["removed"]).resolve(), named_dir)
        self.assertFalse(named_dir.exists())

        subprocess.run(
            [sys.executable, "-m", "blackdog.cli", "backlog", "new", "--project-root", str(self.root), "scratch"],
            check=True,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        subprocess.run(
            [sys.executable, "-m", "blackdog.cli", "backlog", "reset", "--project-root", str(self.root), "--purge-named"],
            check=True,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        self.assertFalse((paths.control_dir / "backlogs").exists())

    def test_claim_complete_and_result_record(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        for title in ("First task", "Second task"):
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--bucket",
                "core",
                "--why",
                "Need ordered work.",
                "--evidence",
                "Testing lane sequencing.",
                "--safe-first-slice",
                "Add one narrow task.",
                "--path",
                "README.md",
                "--epic-title",
                "Bootstrap",
                "--lane-title",
                "Serial lane",
                "--wave",
                "0",
            )

        next_payload = subprocess.run(
            [sys.executable, "-m", "blackdog.cli", "next", "--project-root", str(self.root), "--format", "json"],
            check=True,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        rows = json.loads(next_payload.stdout)
        first_id = rows[0]["id"]

        run_cli("claim", "--project-root", str(self.root), "--agent", "agent/a", "--id", first_id)
        run_cli("complete", "--project-root", str(self.root), "--agent", "agent/a", "--id", first_id, "--note", "done")
        run_cli(
            "result",
            "record",
            "--project-root",
            str(self.root),
            "--id",
            first_id,
            "--actor",
            "agent/a",
            "--status",
            "success",
            "--what-changed",
            "Added the first slice.",
            "--validation",
            "make test",
            "--residual",
            "Second task still open.",
            "--followup",
            "Continue the lane.",
        )

        events = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "events", "--project-root", str(self.root), "--id", first_id],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        event_types = {row["type"] for row in events}
        self.assertIn("claim", event_types)
        self.assertIn("complete", event_types)
        self.assertIn("task_result", event_types)

    def test_inbox_and_skill_generation(self) -> None:
        run_skill_cli("new", "backlog", "--project-root", str(self.root), "--project-name", "Inbox Demo")
        run_cli(
            "inbox",
            "send",
            "--project-root",
            str(self.root),
            "--sender",
            "user",
            "--recipient",
            "supervisor",
            "--kind",
            "instruction",
            "--body",
            "Pause after the next claim.",
        )
        messages = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "inbox", "list", "--project-root", str(self.root), "--recipient", "supervisor"],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(len(messages), 1)
        message_id = messages[0]["message_id"]
        run_cli(
            "inbox",
            "resolve",
            "--project-root",
            str(self.root),
            "--message-id",
            message_id,
            "--actor",
            "supervisor",
            "--note",
            "Acknowledged.",
        )
        resolved = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "inbox", "list", "--project-root", str(self.root), "--status", "resolved"],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(len(resolved), 1)
        self.assertTrue((self.root / ".codex/skills/blackdog/SKILL.md").exists())

    def test_skill_refresh_regenerates_existing_project_skill(self) -> None:
        run_skill_cli("new", "backlog", "--project-root", str(self.root), "--project-name", "Inbox Demo")
        profile_text = (self.root / "blackdog.toml").read_text(encoding="utf-8").replace(
            '"PYTHONPATH=src python3 -m unittest discover -s tests -p \'test_*.py\'"',
            '"make test"',
        )
        (self.root / "blackdog.toml").write_text(profile_text, encoding="utf-8")
        ve_bin = self.root / ".VE" / "bin"
        ve_bin.mkdir(parents=True)
        for name in ("blackdog", "blackdog-skill"):
            script = ve_bin / name
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)
        skill_file = self.root / ".codex/skills/blackdog/SKILL.md"
        skill_file.write_text("stale skill text\n", encoding="utf-8")

        payload = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.skill_cli",
                    "refresh",
                    "backlog",
                    "--project-root",
                    str(self.root),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )

        self.assertEqual(Path(payload["skill_file"]).resolve(), skill_file.resolve())
        refreshed_text = skill_file.read_text(encoding="utf-8")
        self.assertIn("Use the project-local Blackdog backlog contract", refreshed_text)
        self.assertIn(str((ve_bin / "blackdog").resolve()), refreshed_text)
        self.assertIn(str((ve_bin / "blackdog-skill").resolve()), refreshed_text)
        self.assertIn("Control root:", refreshed_text)
        self.assertIn("`make test`", refreshed_text)
        self.assertIn("Before any repo edit you intend to keep", refreshed_text)
        self.assertIn("## Docs to Review", refreshed_text)
        self.assertIn("`AGENTS.md`", refreshed_text)
        self.assertIn("doc_routing_defaults", refreshed_text)
        self.assertIn("Blackdog uses branch-backed task worktrees for kept implementation changes.", refreshed_text)
        self.assertIn("refresh backlog", refreshed_text)

    def test_plan_summary_reports_epics_lanes_and_waves(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Plan slice one",
            "--bucket",
            "core",
            "--why",
            "Need a first planned task.",
            "--evidence",
            "The plan command should report epics and waves.",
            "--safe-first-slice",
            "Add one task in wave zero.",
            "--path",
            "README.md",
            "--epic-title",
            "Epic Alpha",
            "--lane-title",
            "Lane Alpha",
            "--wave",
            "0",
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Plan slice two",
            "--bucket",
            "docs",
            "--why",
            "Need a second planned task.",
            "--evidence",
            "The plan command should report later waves too.",
            "--safe-first-slice",
            "Add one task in wave one.",
            "--path",
            "docs/CLI.md",
            "--epic-title",
            "Epic Beta",
            "--lane-title",
            "Lane Beta",
            "--wave",
            "1",
        )

        plan_json = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "plan", "--project-root", str(self.root), "--format", "json"],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(plan_json["counts"]["epics"], 2)
        self.assertEqual(plan_json["counts"]["lanes"], 2)
        self.assertEqual(plan_json["counts"]["waves"], 2)
        self.assertEqual(plan_json["waves"][0]["wave"], 0)
        self.assertEqual(plan_json["waves"][1]["wave"], 1)
        self.assertEqual(plan_json["epics"][0]["task_count"], 1)
        self.assertEqual(plan_json["lanes"][0]["task_count"], 1)

        plan_text = subprocess.run(
            [sys.executable, "-m", "blackdog.cli", "plan", "--project-root", str(self.root)],
            check=True,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        ).stdout
        self.assertIn("Plan: 2 epics | 2 lanes | 2 waves | 2 tasks", plan_text)
        self.assertIn("Wave 0 | Lane Alpha", plan_text)
        self.assertIn("Wave 1 | Lane Beta", plan_text)

    def test_worktree_preflight_and_start_create_branch_backed_worktree(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Worktree lifecycle slice",
            "--bucket",
            "core",
            "--why",
            "Implementation work should happen in a branch-backed worktree.",
            "--evidence",
            "Blackdog should expose a WTAM start entrypoint.",
            "--safe-first-slice",
            "Create one task worktree from the primary branch.",
            "--path",
            "src/blackdog/worktree.py",
            "--epic-title",
            "WTAM",
            "--lane-title",
            "Lifecycle",
            "--wave",
            "0",
        )
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Checkpoint backlog state for worktree test"],
            check=True,
            capture_output=True,
            text=True,
        )
        task_id = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "next", "--project-root", str(self.root), "--format", "json"],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )[0]["id"]

        preflight = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "worktree", "preflight", "--project-root", str(self.root), "--format", "json"],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(preflight["worktree_model"], "branch-backed")
        self.assertTrue(preflight["current_is_primary"])
        self.assertFalse(preflight["worktrees_dir_inside_repo"])
        self.assertEqual(preflight["workspace_mode"], "git-worktree")
        self.assertEqual(preflight["target_branch"], "main")
        self.assertEqual(Path(preflight["project_root"]).resolve(), self.root.resolve())
        self.assertEqual(Path(preflight["cwd"]).resolve(), self.root.resolve())
        self.assertIn(".VE is unversioned", preflight["workspace_contract"]["ve_expectation"])

        preflight_text = subprocess.run(
            [sys.executable, "-m", "blackdog.cli", "worktree", "preflight", "--project-root", str(self.root)],
            check=True,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        ).stdout
        self.assertIn("[blackdog-worktree] workspace mode: git-worktree", preflight_text)
        self.assertIn("[blackdog-worktree] target branch: main", preflight_text)
        self.assertIn(f"[blackdog-worktree] project root: {self.root.resolve()}", preflight_text)
        self.assertIn(f"[blackdog-worktree] current worktree: {self.root.resolve()}", preflight_text)
        self.assertIn("[blackdog-worktree] .VE rule:", preflight_text)

        with tempfile.TemporaryDirectory() as worktree_parent:
            expected_path = Path(worktree_parent) / "task-worktree"
            created = json.loads(
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "blackdog.cli",
                        "worktree",
                        "start",
                        "--project-root",
                        str(self.root),
                        "--id",
                        task_id,
                        "--path",
                        str(expected_path),
                        "--format",
                        "json",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=cli_env(),
                    cwd=self.root,
                ).stdout
            )
            worktree_path = Path(created["worktree_path"])
            self.assertTrue(worktree_path.exists())
            self.assertTrue((worktree_path / ".git").is_file())
            self.assertTrue(created["branch"].startswith("agent/"))
            self.assertFalse(str(worktree_path).startswith(str(self.root.resolve()) + os.sep))

            worktree_branch = subprocess.run(
                ["git", "-C", str(worktree_path), "rev-parse", "--abbrev-ref", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(worktree_branch, created["branch"])

            delegated_preflight = json.loads(
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "blackdog.cli",
                        "worktree",
                        "preflight",
                        "--project-root",
                        str(self.root),
                        "--format",
                        "json",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=cli_env(),
                    cwd=worktree_path,
                ).stdout
            )
            self.assertFalse(delegated_preflight["current_is_primary"])
            self.assertEqual(Path(delegated_preflight["project_root"]).resolve(), self.root.resolve())
            self.assertEqual(Path(delegated_preflight["cwd"]).resolve(), worktree_path.resolve())
            self.assertEqual(Path(delegated_preflight["current_worktree"]).resolve(), worktree_path.resolve())

            events = json.loads(
                subprocess.run(
                    [sys.executable, "-m", "blackdog.cli", "events", "--project-root", str(self.root)],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=cli_env(),
                    cwd=self.root,
                ).stdout
            )
            self.assertIn("worktree_start", {row["type"] for row in events})

            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "worktree",
                    "cleanup",
                    "--project-root",
                    str(self.root),
                    "--path",
                    str(worktree_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            )
            self.assertFalse(worktree_path.exists())

    def test_worktree_land_fast_forwards_and_cleans_up(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Land branch-backed worktree",
            "--bucket",
            "core",
            "--why",
            "The lifecycle needs a single-command land step.",
            "--evidence",
            "Blackdog should fast-forward a task branch into the primary branch.",
            "--safe-first-slice",
            "Create, commit, and land one task branch.",
            "--path",
            "src/blackdog/worktree.py",
            "--epic-title",
            "WTAM",
            "--lane-title",
            "Lifecycle",
            "--wave",
            "0",
        )
        subprocess.run(["git", "-C", str(self.root), "add", "-A"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Checkpoint backlog state for worktree landing"],
            check=True,
            capture_output=True,
            text=True,
        )
        task_id = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "next", "--project-root", str(self.root), "--format", "json"],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )[0]["id"]

        with tempfile.TemporaryDirectory() as worktree_parent:
            expected_path = Path(worktree_parent) / "task-worktree"
            created = json.loads(
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "blackdog.cli",
                        "worktree",
                        "start",
                        "--project-root",
                        str(self.root),
                        "--id",
                        task_id,
                        "--path",
                        str(expected_path),
                        "--format",
                        "json",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=cli_env(),
                    cwd=self.root,
                ).stdout
            )
            worktree_path = Path(created["worktree_path"])
            branch = str(created["branch"])
            feature_file = worktree_path / "feature.txt"
            feature_file.write_text("landed from worktree\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(worktree_path), "add", "feature.txt"], check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "-C", str(worktree_path), "commit", "-m", "Add feature from task worktree"],
                check=True,
                capture_output=True,
                text=True,
            )

            landed = json.loads(
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "blackdog.cli",
                        "worktree",
                        "land",
                        "--project-root",
                        str(self.root),
                        "--branch",
                        branch,
                        "--cleanup",
                        "--format",
                        "json",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=cli_env(),
                    cwd=self.root,
                ).stdout
            )

            self.assertEqual(landed["branch"], branch)
            self.assertEqual((self.root / "feature.txt").read_text(encoding="utf-8"), "landed from worktree\n")
            self.assertFalse(worktree_path.exists())

            branch_check = subprocess.run(
                ["git", "-C", str(self.root), "show-ref", "--verify", f"refs/heads/{branch}"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(branch_check.returncode, 0)

            events = json.loads(
                subprocess.run(
                    [sys.executable, "-m", "blackdog.cli", "events", "--project-root", str(self.root)],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=cli_env(),
                    cwd=self.root,
                ).stdout
            )
            self.assertIn("worktree_land", {row["type"] for row in events})

    def test_snapshot_reports_graph_and_static_contract(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        for title in ("UI slice one", "UI slice two"):
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--bucket",
                "html",
                "--why",
                "Need a visible task graph.",
                "--evidence",
                "The UI snapshot should expose lane order as graph edges.",
                "--safe-first-slice",
                "Render one graph node in a readonly UI.",
                "--path",
                "src/blackdog/ui.py",
                "--epic-title",
                "UI",
                "--lane-title",
                "Live lane",
                "--wave",
                "0",
            )

        snapshot = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "snapshot", "--project-root", str(self.root)],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )

        task_ids = {task["title"]: task["id"] for task in snapshot["graph"]["tasks"]}
        self.assertEqual(snapshot["schema_version"], 4)
        self.assertEqual(Path(snapshot["project_root"]).resolve(), self.root.resolve())
        self.assertEqual(Path(snapshot["control_dir"]).resolve(), self.runtime_paths().control_dir.resolve())
        self.assertEqual(snapshot["headers"]["Target branch"], "main")
        self.assertRegex(snapshot["headers"]["Target commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(snapshot["graph"]["edges"], [{"from": task_ids["UI slice one"], "to": task_ids["UI slice two"]}])
        self.assertEqual(snapshot["links"]["backlog"], "backlog.md")
        self.assertEqual(snapshot["links"]["results"], "task-results")
        self.assertEqual(snapshot["last_activity"]["actor"], "blackdog")
        self.assertEqual(snapshot["last_activity"]["actor_role"], "system")
        self.assertEqual(snapshot["graph"]["tasks"][0]["operator_status"], "Ready")
        self.assertEqual(snapshot["graph"]["tasks"][1]["operator_status"], "Waiting")
        self.assertEqual(snapshot["graph"]["tasks"][0]["lane_position"], 1)
        self.assertEqual(snapshot["graph"]["tasks"][1]["lane_position"], 2)
        self.assertEqual(snapshot["graph"]["tasks"][0]["lane_task_count"], 2)
        self.assertIn("scheduler gate", snapshot["grouping_guide"][-1]["meaning"])

    def test_snapshot_models_objective_rows_with_progress_summaries(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Objective slice one",
            "--bucket",
            "html",
            "--objective",
            "OBJ-1",
            "--why",
            "The board should lead with objectives instead of only lane groupings.",
            "--evidence",
            "Objective-tagged task rows need stable snapshot data before the renderer can pivot.",
            "--safe-first-slice",
            "Model one objective row with active and waiting work.",
            "--path",
            "src/blackdog/ui.py",
            "--epic-title",
            "Objective board",
            "--lane-title",
            "Objective lane",
            "--wave",
            "0",
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Objective slice two",
            "--bucket",
            "html",
            "--objective",
            "OBJ-1",
            "--why",
            "The row needs more than one task to expose progress detail.",
            "--evidence",
            "A second task creates the waiting state inside the same objective.",
            "--safe-first-slice",
            "Keep the second objective task in the same lane for deterministic ordering.",
            "--path",
            "src/blackdog/ui.py",
            "--epic-title",
            "Objective board",
            "--lane-title",
            "Objective lane",
            "--wave",
            "0",
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Second objective slice",
            "--bucket",
            "html",
            "--objective",
            "OBJ-2",
            "--why",
            "A completed objective verifies the summary counts.",
            "--evidence",
            "The snapshot should surface completed objective progress separately from active work.",
            "--safe-first-slice",
            "Complete one task in a second objective.",
            "--path",
            "src/blackdog/ui.py",
            "--epic-title",
            "Objective board",
            "--lane-title",
            "Objective lane two",
            "--wave",
            "0",
        )

        task_ids = task_ids_by_title(self.root)
        first_task = task_ids["Objective slice one"]
        second_task = task_ids["Objective slice two"]
        completed_task = task_ids["Second objective slice"]

        run_cli("claim", "--project-root", str(self.root), "--agent", "agent/a", "--id", first_task)
        run_cli("claim", "--project-root", str(self.root), "--agent", "agent/b", "--id", completed_task)
        run_cli("complete", "--project-root", str(self.root), "--agent", "agent/b", "--id", completed_task, "--note", "done")

        snapshot = build_ui_snapshot(load_profile(self.root))
        objective_rows = {row["id"]: row for row in snapshot["objective_rows"]}
        task_rows = {row["id"]: row for row in snapshot["tasks"]}

        self.assertEqual(list(objective_rows), ["OBJ-1", "OBJ-2"])
        self.assertEqual(task_rows[first_task]["objective_title"], "Maintain a repo-scoped backlog core")
        self.assertEqual(task_rows[completed_task]["objective_title"], "Keep AI-agent interaction structured and local")
        self.assertEqual(objective_rows["OBJ-1"]["task_ids"], [first_task, second_task])
        self.assertEqual(objective_rows["OBJ-1"]["active_task_ids"], [first_task, second_task])
        self.assertEqual(objective_rows["OBJ-1"]["lane_titles"], ["Objective lane"])
        self.assertEqual(objective_rows["OBJ-1"]["progress"]["counts"]["claimed"], 1)
        self.assertEqual(objective_rows["OBJ-1"]["progress"]["counts"]["waiting"], 1)
        self.assertEqual(objective_rows["OBJ-1"]["remaining"], 2)
        self.assertEqual(objective_rows["OBJ-2"]["task_ids"], [completed_task])
        self.assertEqual(objective_rows["OBJ-2"]["active_task_ids"], [])
        self.assertEqual(objective_rows["OBJ-2"]["done"], 1)
        self.assertEqual(objective_rows["OBJ-2"]["progress"]["counts"]["complete"], 1)
        self.assertEqual(objective_rows["OBJ-2"]["remaining"], 0)

        run_cli("render", "--project-root", str(self.root), "--actor", "tester")
        html = paths.html_file.read_text(encoding="utf-8")
        self.assertIn("Backlog Control", html)
        self.assertIn('id="hero-panel"', html)
        self.assertIn('id="queue-stats"', html)
        self.assertIn('id="objectives-panel"', html)
        self.assertIn('id="objective-cards"', html)
        self.assertIn('id="overview-panel"', html)
        self.assertIn('id="domains-panel"', html)
        self.assertIn("Release Gates", html)
        self.assertIn('document.getElementById("objective-cards").innerHTML = objectiveRows.length', html)
        self.assertIn('class="objective-card"${interactiveCardAttributes(leadTaskId)}', html)
        self.assertNotIn('id="backlog-panel"', html)

    def test_snapshot_exposes_active_tasks_filters_messages_and_interrupts_empty_runs(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        for title in ("Operator slice one", "Operator slice two"):
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--bucket",
                "html",
                "--why",
                "Operators need to understand active backlog work.",
                "--evidence",
                "The static backlog view should separate activity, inbox controls, and result history.",
                "--safe-first-slice",
                "Add snapshot fields for active tasks and filtered inbox messages.",
                "--path",
                "src/blackdog/ui.py",
                "--epic-title",
                "UI",
                "--lane-title",
                "Operator lane",
                "--wave",
                "0",
            )

        task_ids = task_ids_by_title(self.root)
        first_task = task_ids["Operator slice one"]
        second_task = task_ids["Operator slice two"]
        run_cli(
            "claim",
            "--project-root",
            str(self.root),
            "--agent",
            "supervisor/child-01",
            "--id",
            first_task,
        )
        record_task_result(
            paths,
            task_id=first_task,
            actor="supervisor/child-01",
            status="success",
            what_changed=["Captured active-task result metadata."],
            validation=["snapshot-check"],
            residual=[],
            needs_user_input=False,
            followup_candidates=[],
            run_id="result-noise",
        )
        send_message(
            paths,
            sender="supervisor",
            recipient="supervisor/child-01",
            body="Execute the current operator slice.",
            kind="instruction",
            task_id=first_task,
            tags=["supervisor-run", "git-worktree"],
        )
        send_message(
            paths,
            sender="blackdog",
            recipient="supervisor",
            body="Primary worktree needs attention.",
            kind="warning",
            task_id=first_task,
            tags=["dirty-primary", "land"],
        )

        interrupted_run_dir = paths.supervisor_runs_dir / "20260313-120000-stalerun1"
        interrupted_run_dir.mkdir(parents=True)
        live_task_dir = paths.supervisor_runs_dir / "20260314-120000-liverun1" / first_task
        live_task_dir.mkdir(parents=True)
        for filename in ("prompt.txt", "stdout.log", "stderr.log"):
            (live_task_dir / filename).write_text(f"{filename}\n", encoding="utf-8")

        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-interrupted-run",
                "type": "supervisor_run_started",
                "at": "2026-03-13T12:00:00-07:00",
                "actor": "supervisor",
                "task_id": None,
                "payload": {
                    "run_id": "stalerun1",
                    "workspace_mode": "git-worktree",
                    "task_ids": [first_task],
                },
            },
        )
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-live-run",
                "type": "supervisor_run_started",
                "at": "2026-03-14T12:00:00-07:00",
                "actor": "supervisor",
                "task_id": None,
                "payload": {
                    "run_id": "liverun1",
                    "workspace_mode": "git-worktree",
                    "task_ids": [first_task],
                },
            },
        )
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-live-worktree",
                "type": "worktree_start",
                "at": "2026-03-14T12:00:03-07:00",
                "actor": "supervisor",
                "task_id": first_task,
                "payload": {
                    "run_id": "liverun1",
                    "child_agent": "supervisor/child-01",
                    "branch": "agent/operator-slice-one-liverun1",
                    "target_branch": "main",
                    "worktree_path": str(live_task_dir.parent),
                    "primary_worktree": str(self.root),
                },
            },
        )
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-live-child",
                "type": "child_launch",
                "at": "2026-03-14T12:00:05-07:00",
                "actor": "supervisor",
                "task_id": first_task,
                "payload": {
                    "run_id": "liverun1",
                    "child_agent": "supervisor/child-01",
                    "workspace": str(live_task_dir.parent),
                    "pid": os.getpid(),
                },
            },
        )

        snapshot = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "snapshot", "--project-root", str(self.root)],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )

        graph_tasks = {task["id"]: task for task in snapshot["graph"]["tasks"]}
        open_recipients = {row["recipient"] for row in snapshot["open_messages"]}
        expected_commit = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()[:12]

        self.assertEqual(open_recipients, {"supervisor", "supervisor/child-01"})
        self.assertEqual(snapshot["links"]["inbox"], "inbox.jsonl")
        self.assertEqual(len(snapshot["board_tasks"]), 2)
        self.assertEqual({row["lane_title"] for row in snapshot["board_tasks"]}, {"Operator lane"})
        self.assertEqual(snapshot["workspace_contract"]["target_branch"], "main")
        self.assertIn(".VE is unversioned", snapshot["workspace_contract"]["ve_expectation"])
        self.assertEqual(snapshot["hero_highlights"]["branch"], "agent/operator-slice-one-liverun1 -> main")
        self.assertEqual(snapshot["hero_highlights"]["commit"], expected_commit)
        self.assertIn(first_task, snapshot["hero_highlights"]["latest_run"])
        self.assertIn("Running", snapshot["hero_highlights"]["latest_run"])
        self.assertIn("supervisor/child-01", snapshot["hero_highlights"]["latest_run"])
        self.assertIn("1 active task", snapshot["hero_highlights"]["time_on_task"])
        self.assertIn("across 1 task", snapshot["hero_highlights"]["time_on_task"])
        self.assertEqual(snapshot["active_tasks"][0]["id"], first_task)
        self.assertEqual(snapshot["active_tasks"][0]["target_branch"], "main")
        self.assertEqual(snapshot["active_tasks"][0]["prompt_href"], f"supervisor-runs/20260314-120000-liverun1/{first_task}/prompt.txt")
        self.assertEqual(snapshot["active_tasks"][0]["latest_run_status"], "running")
        self.assertEqual(graph_tasks[first_task]["latest_result_status"], "success")
        self.assertEqual(graph_tasks[first_task]["operator_status"], "Running")
        self.assertEqual(
            [row["key"] for row in graph_tasks[first_task]["card_status_chips"]],
            ["claimed", "running"],
        )
        self.assertEqual(
            [row["key"] for row in graph_tasks[first_task]["dialog_status_chips"][:2]],
            ["claimed", "running"],
        )
        self.assertEqual(graph_tasks[second_task]["operator_status"], "Waiting")
        self.assertTrue(any(row["message"] == "claimed" for row in graph_tasks[first_task]["activity"]))
        self.assertTrue(any(row["message"] == "result success" for row in graph_tasks[first_task]["activity"]))
        self.assertIsNotNone(graph_tasks[first_task]["active_compute_label"])
        self.assertGreaterEqual(int(graph_tasks[first_task]["total_compute_seconds"]), 0)
        self.assertEqual(graph_tasks[second_task]["predecessor_ids"], [first_task])
        self.assertEqual(graph_tasks[first_task]["run_dir_href"], f"supervisor-runs/20260314-120000-liverun1/{first_task}")
        run_cli("render", "--project-root", str(self.root), "--actor", "tester")
        html = paths.html_file.read_text(encoding="utf-8")
        self.assertNotIn('style="', html)
        self.assertNotIn("EventSource(", html)
        self.assertNotIn("fetch(", html)
        self.assertIn('id="blackdog-snapshot"', html)
        self.assertIn('id="hero-panel"', html)
        self.assertIn("Backlog Control", html)
        self.assertIn('id="hero-meta-line"', html)
        self.assertIn('id="hero-links"', html)
        self.assertIn('id="queue-stats"', html)
        self.assertIn('id="overview-panel"', html)
        self.assertIn('id="overview-cards"', html)
        self.assertIn('id="domains-panel"', html)
        self.assertIn('id="domain-chips"', html)
        self.assertIn('id="hero-progress"', html)
        self.assertNotIn('id="hero-meta-grid"', html)
        self.assertNotIn('data-hero-section="workspace"', html)
        self.assertNotIn('data-hero-section="board-snapshot"', html)
        self.assertNotIn('data-hero-section="artifacts"', html)
        self.assertNotIn('id="backlog-panel"', html)
        self.assertNotIn('id="board-summary"', html)
        self.assertNotIn('id="status-legend"', html)
        self.assertNotIn('id="task-search"', html)
        self.assertNotIn("renderStats()", html)
        self.assertIn("function renderProgressBar(progress, className = \"\")", html)
        self.assertIn("function applyProgressBars(root = document)", html)
        self.assertIn("function interactiveCardAttributes(taskId)", html)
        self.assertIn("function renderQueueHealth()", html)
        self.assertIn("function renderOverviewCards()", html)
        self.assertIn("function renderDomainChips()", html)
        self.assertIn('["Backlog Source", links.backlog]', html)
        self.assertIn('["State", links.state]', html)
        self.assertIn("What We Are Doing", html)
        self.assertIn("What's Next", html)
        self.assertIn("Release Gates", html)
        self.assertIn('class="objective-card"${interactiveCardAttributes(leadTaskId)}', html)
        self.assertIn('class="compact-card"${interactiveCardAttributes(taskId)}', html)
        self.assertIn('role="button" tabindex="0"', html)
        self.assertIn('document.addEventListener("keydown", (event) => {', html)
        self.assertIn("const heroHighlights = snapshot.hero_highlights || {};", html)
        self.assertNotIn("Git head", html)
        self.assertNotIn("Blackdog runtime", html)
        self.assertIn('document.getElementById("hero-meta-line").innerHTML = metaItems', html)
        self.assertIn('document.getElementById("hero-links").innerHTML = globalLinks()', html)
        self.assertIn('document.getElementById("queue-stats").innerHTML = stats.map', html)
        self.assertIn('document.getElementById("hero-progress").innerHTML = renderProgressBar(overallProgress, "progress-hero");', html)
        self.assertIn('data-progress="${escapeHtml(progress.percent)}"', html)
        self.assertIn("supervisor-runs/20260314-120000-liverun1", html)
        self.assertIn("Time on task ${heroHighlights.time_on_task}", html)
        self.assertNotIn("Search and status filters apply only to the execution map.", html)

    def test_snapshot_dialog_status_chips_ignore_stale_blocked_run_after_completion(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Completed status cleanup",
            "--bucket",
            "html",
            "--priority",
            "P2",
            "--why",
            "Completed tasks should not read as blocked in the dialog.",
            "--evidence",
            "A stale blocked child run should stay historical once the task is done.",
            "--safe-first-slice",
            "Prefer current task status over stale run status in the UI snapshot.",
            "--path",
            "src/blackdog/ui.py",
            "--epic-title",
            "UI",
            "--lane-title",
            "Dialog status cleanup",
            "--wave",
            "0",
        )
        task_id = task_ids_by_title(self.root)["Completed status cleanup"]

        run_cli("claim", "--project-root", str(self.root), "--agent", "agent/a", "--id", task_id)
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-task-blocked-run",
                "type": "child_finish",
                "at": "2026-03-15T10:00:00-07:00",
                "actor": "supervisor",
                "task_id": task_id,
                "payload": {
                    "run_id": "blockedrun1",
                    "land_error": "dirty target branch",
                },
            },
        )
        record_task_result(
            paths,
            task_id=task_id,
            actor="agent/a",
            status="success",
            what_changed=["Completed the UI cleanup after the blocked run."],
            validation=["status-cleanup"],
            residual=[],
            needs_user_input=False,
            followup_candidates=[],
        )
        run_cli("complete", "--project-root", str(self.root), "--agent", "agent/a", "--id", task_id, "--note", "done")

        snapshot = build_ui_snapshot(load_profile(self.root))
        task = next(row for row in snapshot["tasks"] if row["id"] == task_id)
        chip_keys = [row["key"] for row in task["dialog_status_chips"]]

        self.assertEqual(task["operator_status"], "Complete")
        self.assertEqual(chip_keys, ["complete", "subtle"])
        self.assertEqual(task["latest_run_status"], "blocked")

    def test_supervise_status_reports_run_controls_ready_tasks_and_recent_results(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        for title in ("Status task one", "Status task two"):
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--bucket",
                "cli",
                "--why",
                "Need a compact supervisor inspection surface.",
                "--evidence",
                "Chat needs run state, controls, ready tasks, and recent child results.",
                "--safe-first-slice",
                "Print a readonly supervisor status summary.",
                "--path",
                "src/blackdog/cli.py",
                "--epic-title",
                "Supervisor interface",
                "--lane-title",
                "Chat investigation",
                "--wave",
                "0",
            )
        task_ids = task_ids_by_title(self.root)
        stop_message = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "inbox",
                    "send",
                    "--project-root",
                    str(self.root),
                    "--sender",
                    "user",
                    "--recipient",
                    "supervisor",
                    "--kind",
                    "instruction",
                    "--tag",
                    "stop",
                    "--body",
                    "stop after the current child work drains",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "inbox",
                "send",
                "--project-root",
                str(self.root),
                "--sender",
                "user",
                "--recipient",
                "supervisor",
                "--kind",
                "instruction",
                "--body",
                "this should not count as a control message",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        run_dir = self.runtime_paths().supervisor_runs_dir / "20260313-120000-abcd1234"
        run_dir.mkdir(parents=True)
        status_file = run_dir / "status.json"
        status_file.write_text(
            json.dumps(
                {
                    "run_id": "abcd1234",
                    "actor": "supervisor",
                    "workspace_mode": "git-worktree",
                    "poll_interval_seconds": 1.0,
                    "draining": True,
                    "run_dir": str(run_dir),
                    "status_file": str(status_file),
                    "supervisor_pid": os.getpid(),
                    "steps": [
                        {
                            "index": 1,
                            "at": "2026-03-13T12:00:00-07:00",
                            "status": "draining",
                            "ready_task_ids": [task_ids["Status task one"], task_ids["Status task two"]],
                            "running_task_ids": [task_ids["Status task one"]],
                            "open_message_ids": [stop_message["message_id"]],
                            "control_message_id": stop_message["message_id"],
                        }
                    ],
                    "completed_at": "2026-03-13T12:00:05-07:00",
                    "final_status": "stopped",
                    "stopped_by_message_id": stop_message["message_id"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "result",
                "record",
                "--project-root",
                str(self.root),
                "--id",
                task_ids["Status task one"],
                "--actor",
                "supervisor/child-01",
                "--status",
                "success",
                "--what-changed",
                "child completed the first status task",
                "--validation",
                "status-smoke",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "result",
                "record",
                "--project-root",
                str(self.root),
                "--id",
                task_ids["Status task two"],
                "--actor",
                "other-agent",
                "--status",
                "partial",
                "--what-changed",
                "unrelated result should be filtered out",
                "--validation",
                "status-smoke",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )

        payload = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "supervise",
                    "status",
                    "--project-root",
                    str(self.root),
                    "--actor",
                    "supervisor",
                    "--format",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )

        self.assertEqual(payload["actor"], "supervisor")
        self.assertEqual(payload["latest_run"]["run_id"], "abcd1234")
        self.assertEqual(payload["latest_run"]["status_file"], str(status_file))
        self.assertEqual(payload["latest_run"]["status"], "stopped")
        self.assertEqual(payload["workspace_contract"]["workspace_mode"], "git-worktree")
        self.assertEqual(payload["workspace_contract"]["target_branch"], "main")
        self.assertIsInstance(payload["workspace_contract"]["primary_dirty"], bool)
        self.assertIn(".VE is unversioned", payload["workspace_contract"]["ve_expectation"])
        self.assertEqual(payload["control_action"], {"action": "stop", "message_id": stop_message["message_id"]})
        self.assertEqual([row["message_id"] for row in payload["open_control_messages"]], [stop_message["message_id"]])
        self.assertEqual([row["title"] for row in payload["ready_tasks"]], ["Status task one"])
        self.assertEqual(len(payload["recent_results"]), 1)
        self.assertEqual(payload["recent_results"][0]["task_id"], task_ids["Status task one"])
        self.assertEqual(payload["recent_results"][0]["actor"], "supervisor/child-01")

        text_output = subprocess.run(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "status",
                "--project-root",
                str(self.root),
                "--actor",
                "supervisor",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        ).stdout
        self.assertIn("Latest run: stopped | abcd1234 | steps 1 | workspace git-worktree", text_output)
        self.assertIn("WTAM contract: git-worktree -> main | primary ", text_output)
        self.assertIn(".VE rule: .VE is unversioned", text_output)
        self.assertIn(f"Run control: stop via {stop_message['message_id']}", text_output)
        self.assertIn("Recent child-run results:", text_output)

    def test_supervise_run_sweeps_completed_tasks_and_compacts_waves(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Completed task from prior run",
            "--bucket",
            "core",
            "--why",
            "Need to verify that a new run cleans old completed work out of the execution map.",
            "--evidence",
            "Completed tasks should leave the active plan when the next supervisor run starts.",
            "--safe-first-slice",
            "Mark one task complete, then start another run and check the compacted plan.",
            "--path",
            "README.md",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Sweep done lane",
            "--wave",
            "0",
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Approval-gated remaining task",
            "--bucket",
            "core",
            "--why",
            "Need one unfinished task that remains in the plan after the sweep.",
            "--evidence",
            "The run should compact its wave to zero after the completed lane is removed.",
            "--safe-first-slice",
            "Leave one approval-gated task unfinished and start the run.",
            "--path",
            "README.md",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Sweep waiting lane",
            "--wave",
            "1",
            "--requires-approval",
            "--approval-reason",
            "Hold this task for a later run.",
        )
        ids = task_ids_by_title(self.root)
        completed_task_id = ids["Completed task from prior run"]
        waiting_task_id = ids["Approval-gated remaining task"]
        run_cli("claim", "--project-root", str(self.root), "--agent", "agent/a", "--id", completed_task_id)
        record_task_result(
            self.runtime_paths(),
            task_id=completed_task_id,
            actor="agent/a",
            status="success",
            what_changed=["Completed the first lane before the next supervisor run."],
            validation=["sweep-smoke"],
            residual=[],
            needs_user_input=False,
            followup_candidates=[],
        )
        run_cli("complete", "--project-root", str(self.root), "--agent", "agent/a", "--id", completed_task_id, "--note", "done")

        before_snapshot = build_ui_snapshot(load_profile(self.root))
        self.assertEqual([row["id"] for row in before_snapshot["board_tasks"]], [completed_task_id, waiting_task_id])

        payload = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "supervise",
                    "run",
                    "--project-root",
                    str(self.root),
                    "--poll-interval-seconds",
                    "0",
                    "--format",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )

        self.assertEqual(payload["final_status"], "idle")
        plan_snapshot = load_backlog(load_profile(self.root).paths, load_profile(self.root))
        self.assertEqual(len(plan_snapshot.plan["lanes"]), 1)
        self.assertEqual(plan_snapshot.plan["lanes"][0]["task_ids"], [waiting_task_id])
        self.assertEqual(plan_snapshot.plan["lanes"][0]["wave"], 0)
        after_snapshot = build_ui_snapshot(load_profile(self.root))
        self.assertEqual([row["id"] for row in after_snapshot["board_tasks"]], [waiting_task_id])
        self.assertEqual(after_snapshot["board_tasks"][0]["wave"], 0)

    def test_supervise_run_exits_idle_immediately_when_backlog_is_empty(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "run",
                "--project-root",
                str(self.root),
                "--actor",
                "supervisor",
                "--format",
                "json",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
            timeout=5,
        )

        payload = json.loads(completed.stdout)
        self.assertEqual(payload["final_status"], "idle")
        self.assertEqual(payload["children"], [])

        status_file = sorted(paths.supervisor_runs_dir.glob("*/status.json"))[-1]
        latest_run = json.loads(status_file.read_text(encoding="utf-8"))
        self.assertEqual(latest_run["final_status"], "idle")

    def test_render_writes_static_html_with_embedded_snapshot(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        html_path = self.runtime_paths().html_file
        initial_html = html_path.read_text(encoding="utf-8")
        self.assertIn('id="blackdog-snapshot"', initial_html)
        self.assertNotIn("EventSource(", initial_html)
        self.assertNotIn("fetch(", initial_html)

        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Static UI task",
            "--bucket",
            "html",
            "--objective",
            "OBJ-1",
            "--domain",
            "html",
            "--domain",
            "docs",
            "--why",
            "Need one task to prove render writes a static HTML page.",
            "--evidence",
            "The rendered file should embed updated JSON instead of depending on a web server.",
            "--safe-first-slice",
            "Append one task and regenerate backlog-index.html.",
            "--path",
            "src/blackdog/ui.py",
            "--epic-title",
            "UI",
            "--lane-title",
            "Static lane",
            "--wave",
            "0",
        )

        updated_html = html_path.read_text(encoding="utf-8")
        rendered_snapshot = html_snapshot(html_path)
        self.assertIn("Static UI task", updated_html)
        self.assertIn("blackdog-snapshot", updated_html)
        self.assertIn("backlog.md", updated_html)
        self.assertIn("task-results", updated_html)
        self.assertEqual([row["id"] for row in rendered_snapshot["objective_rows"]], ["OBJ-1"])
        self.assertEqual(rendered_snapshot["tasks"][0]["domains"], ["html", "docs"])
        self.assertIn('id="hero-panel"', updated_html)
        self.assertIn("Queue Health", updated_html)
        self.assertIn("Branch", updated_html)
        self.assertIn("Commit", updated_html)
        self.assertIn("Latest run", updated_html)
        self.assertIn("Time on task", updated_html)
        self.assertNotIn("Git head", updated_html)
        self.assertNotIn("Blackdog runtime", updated_html)
        self.assertIn("Backlog Control", updated_html)
        self.assertIn('id="hero-meta-line"', updated_html)
        self.assertIn('id="hero-links"', updated_html)
        self.assertIn('id="queue-stats"', updated_html)
        self.assertNotIn('id="hero-meta-grid"', updated_html)
        self.assertIn('id="objectives-panel"', updated_html)
        self.assertIn('id="objective-cards"', updated_html)
        self.assertIn('id="overview-panel"', updated_html)
        self.assertIn('id="overview-cards"', updated_html)
        self.assertIn('id="domains-panel"', updated_html)
        self.assertIn('id="domain-chips"', updated_html)
        self.assertNotIn('id="backlog-panel"', updated_html)
        self.assertNotIn('id="board-summary"', updated_html)
        self.assertIn("What We Are Doing", updated_html)
        self.assertIn("What's Next", updated_html)
        self.assertIn("Release Gates", updated_html)
        self.assertNotIn("Search and status filters apply only to the execution map.", updated_html)
        self.assertIn("const heroHighlights = snapshot.hero_highlights || {};", updated_html)
        self.assertIn('class="text-link"', updated_html)
        self.assertNotIn('id="task-search"', updated_html)
        self.assertNotIn('id="stats"', updated_html)
        self.assertNotIn("renderStats()", updated_html)
        self.assertIn('id="hero-progress"', updated_html)
        self.assertIn("function renderProgressBar(progress, className = \"\")", updated_html)
        self.assertIn("function applyProgressBars(root = document)", updated_html)
        self.assertIn("function interactiveCardAttributes(taskId)", updated_html)
        self.assertIn("function renderQueueHealth()", updated_html)
        self.assertIn("function renderOverviewCards()", updated_html)
        self.assertIn("function renderDomainChips()", updated_html)
        self.assertIn('class="objective-card"${interactiveCardAttributes(leadTaskId)}', updated_html)
        self.assertIn('class="compact-card"${interactiveCardAttributes(taskId)}', updated_html)
        self.assertIn('role="button" tabindex="0"', updated_html)
        self.assertIn('document.addEventListener("keydown", (event) => {', updated_html)
        self.assertIn('document.getElementById("hero-progress").innerHTML = renderProgressBar(overallProgress, "progress-hero");', updated_html)
        self.assertIn('document.getElementById("objective-cards").innerHTML = objectiveRows.length', updated_html)
        self.assertIn('document.getElementById("overview-cards").innerHTML = [', updated_html)
        self.assertIn('document.getElementById("domain-chips").innerHTML = domains.length', updated_html)
        self.assertIn('document.getElementById("queue-stats").innerHTML = stats.map', updated_html)
        self.assertIn('data-progress="${escapeHtml(progress.percent)}"', updated_html)
        self.assertIn('class="progress-slot"', updated_html)
        self.assertIn(".progress-fill {", updated_html)
        self.assertIn(".progress-hero .progress-fill {", updated_html)
        self.assertNotIn('id="completed-history-panel"', updated_html)
        self.assertNotIn("<h2>Completed Tasks</h2>", updated_html)
        self.assertNotIn('id="completed-history-scroll"', updated_html)
        self.assertNotIn("const COMPLETED_HISTORY_LIMIT = 30;", updated_html)
        self.assertNotIn("Showing latest ${visibleCount} of ${completed.length} completed", updated_html)
        self.assertNotIn("completed.slice(0, COMPLETED_HISTORY_LIMIT)", updated_html)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", updated_html)
        self.assertIn("grid-template-columns: minmax(0, 1.4fr) minmax(320px, 0.9fr);", updated_html)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr));", updated_html)
        self.assertNotIn('grid-template-areas: "hero backlog history";', updated_html)
        self.assertNotIn("max-height: calc(100vh - 48px);", updated_html)
        self.assertNotIn('class="link-pill"', updated_html)
        self.assertNotIn('class="artifact-link"', updated_html)
        self.assertNotIn('<span class="pill">', updated_html)
        self.assertNotIn('target="_blank"', updated_html)
        self.assertIn(".tone-complete { border-left: 6px solid var(--complete-fg); background: linear-gradient(", updated_html)
        self.assertIn('id="reader-dialog"', updated_html)
        self.assertIn('document.getElementById("reader-links").innerHTML = renderTaskLinks(task);', updated_html)
        self.assertIn('openTaskReader(taskCard.getAttribute("data-task-id"));', updated_html)
        self.assertNotIn('artifact-row">${renderTaskLinks(task)}</div>', updated_html)
        self.assertNotIn("<h2>Backlog</h2>", updated_html)

    def test_build_child_prompt_prefers_repo_local_ve_blackdog(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Prompt slice",
            "--bucket",
            "cli",
            "--why",
            "Need one task to inspect the child prompt.",
            "--evidence",
            "The prompt should use the repo-local CLI when .VE exists.",
            "--safe-first-slice",
            "Generate a prompt for one runnable task.",
            "--path",
            "src/blackdog/supervisor.py",
            "--epic-title",
            "Prompting",
            "--lane-title",
            "CLI path",
            "--wave",
            "0",
        )
        with tempfile.TemporaryDirectory() as worktree_parent:
            workspace = Path(worktree_parent) / "prompt-worktree"
            subprocess.run(
                ["git", "-C", str(self.root), "worktree", "add", str(workspace), "-b", "agent/prompt-local-cli", "main"],
                check=True,
                capture_output=True,
                text=True,
            )
            try:
                ve_bin = workspace / ".VE" / "bin"
                ve_bin.mkdir(parents=True)
                blackdog_script = ve_bin / "blackdog"
                blackdog_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                blackdog_script.chmod(0o755)

                profile = load_profile(self.root)
                snapshot = load_backlog(profile.paths, profile)
                task = next(iter(snapshot.tasks.values()))
                base_commit = subprocess.run(
                    ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                prompt = _build_child_prompt(
                    profile,
                    task,
                    child_agent="supervisor/child-01",
                    workspace_mode="git-worktree",
                    workspace=workspace,
                    worktree_spec=WorktreeSpec(
                        task_id=task.id,
                        task_title=task.title,
                        task_slug="prompt-local-cli",
                        branch="agent/prompt-local-cli",
                        base_ref="main",
                        base_commit=base_commit,
                        target_branch="main",
                        worktree_path=str(workspace),
                        primary_worktree=str(self.root),
                        current_worktree=str(self.root),
                    ),
                )
            finally:
                subprocess.run(
                    ["git", "-C", str(self.root), "worktree", "remove", "--force", str(workspace)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "-C", str(self.root), "branch", "-D", "agent/prompt-local-cli"],
                    check=True,
                    capture_output=True,
                    text=True,
                )

        self.assertIn(str(blackdog_script.resolve()), prompt)
        self.assertIn(".VE is unversioned and bound to this worktree path", prompt)
        self.assertNotIn("PYTHONPATH=src python3 -m blackdog.cli", prompt)

    def test_build_child_prompt_does_not_reuse_primary_worktree_ve_for_branch_backed_runs(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Prompt worktree slice",
            "--bucket",
            "cli",
            "--why",
            "Need one task to inspect branch-backed prompt wording.",
            "--evidence",
            "The prompt should not tell child worktrees to use another worktree's .VE.",
            "--safe-first-slice",
            "Generate a prompt for one branch-backed child workspace.",
            "--path",
            "src/blackdog/supervisor.py",
            "--epic-title",
            "Prompting",
            "--lane-title",
            "WTAM",
            "--wave",
            "0",
        )
        ve_bin = self.root / ".VE" / "bin"
        ve_bin.mkdir(parents=True)
        blackdog_script = ve_bin / "blackdog"
        blackdog_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        blackdog_script.chmod(0o755)

        with tempfile.TemporaryDirectory() as worktree_parent:
            workspace = Path(worktree_parent) / "prompt-worktree"
            subprocess.run(
                ["git", "-C", str(self.root), "worktree", "add", str(workspace), "-b", "agent/prompt-worktree", "main"],
                check=True,
                capture_output=True,
                text=True,
            )
            try:
                profile = load_profile(self.root)
                snapshot = load_backlog(profile.paths, profile)
                task = next(iter(snapshot.tasks.values()))
                base_commit = subprocess.run(
                    ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                prompt = _build_child_prompt(
                    profile,
                    task,
                    child_agent="supervisor/child-01",
                    workspace_mode="git-worktree",
                    workspace=workspace,
                    worktree_spec=WorktreeSpec(
                        task_id=task.id,
                        task_title=task.title,
                        task_slug="prompt-worktree",
                        branch="agent/prompt-worktree",
                        base_ref="main",
                        base_commit=base_commit,
                        target_branch="main",
                        worktree_path=str(workspace),
                        primary_worktree=str(self.root),
                        current_worktree=str(self.root),
                    ),
                )
            finally:
                subprocess.run(
                    ["git", "-C", str(self.root), "worktree", "remove", "--force", str(workspace)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                subprocess.run(
                    ["git", "-C", str(self.root), "branch", "-D", "agent/prompt-worktree"],
                    check=True,
                    capture_output=True,
                    text=True,
                )

        self.assertNotIn(str(blackdog_script.resolve()), prompt)
        self.assertIn("This workspace does not currently have", prompt)
        self.assertIn("do not reuse another worktree's .VE", prompt)
        self.assertIn("`./.VE/bin/blackdog inbox list", prompt)

    def test_supervise_run_launches_child_command_in_worktree(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        launcher_script = install_exec_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if args == ["--help"]:
        print("Commands:\\n  exec")
        return 0
    if not args or args[0] != "exec":
        print("expected exec launcher", file=sys.stderr)
        return 2
    prompt = args[-1]
    project_root = Path(os.environ["BLACKDOG_PROJECT_ROOT"])
    task_id = os.environ["BLACKDOG_TASK_ID"]
    actor = os.environ["BLACKDOG_AGENT_NAME"]
    run_dir = Path(os.environ["BLACKDOG_RUN_DIR"])
    run_dir.joinpath("prompt-copy.txt").write_text(prompt, encoding="utf-8")
    Path("feature.txt").write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], check=True)
    subprocess.run(["git", "commit", "-m", f"Land {task_id} from child run"], check=True)
    env = os.environ.copy()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "blackdog.cli",
            "result",
            "record",
            "--project-root",
            str(project_root),
            "--id",
            task_id,
            "--actor",
            actor,
            "--status",
            "success",
            "--what-changed",
            f"child ran in {Path.cwd()}",
            "--validation",
            "fake-child",
        ],
        check=True,
        env=env,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint launcher for supervisor worktree test",
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Supervisor target task",
            "--bucket",
            "core",
            "--why",
            "Need one runnable task for supervisor execution.",
            "--evidence",
            "The supervisor should launch a child process in a dedicated workspace.",
            "--safe-first-slice",
            "Run a fake child process that records a result and completes the task.",
            "--path",
            "README.md",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Launch lane",
            "--wave",
            "0",
        )

        task_id = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "next", "--project-root", str(self.root), "--format", "json"],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )[0]["id"]
        run_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "run",
                "--project-root",
                str(self.root),
                "--id",
                task_id,
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        self.assertEqual(run_result.returncode, 0, run_result.stderr)
        payload = json.loads(run_result.stdout)
        self.assertEqual(payload["children"][0]["task_id"], task_id)
        self.assertEqual(payload["children"][0]["exit_code"], 0)
        self.assertFalse(payload["children"][0]["timed_out"])
        self.assertEqual(payload["children"][0]["workspace_mode"], "git-worktree")
        self.assertEqual(payload["children"][0]["launch_command"][0], str(launcher_script))
        workspace = Path(payload["children"][0]["workspace"])
        prompt_text = Path(payload["children"][0]["prompt_file"]).read_text(encoding="utf-8")
        child_run_dir = Path(payload["children"][0]["prompt_file"]).parent
        self.assertFalse(workspace.exists())
        self.assertIn("Treat committed repo state as the baseline for this task", prompt_text)
        self.assertIn("Primary-worktree landing gate:", prompt_text)
        self.assertIn(".VE is unversioned and bound to this worktree path", prompt_text)
        self.assertIn("Do not run `blackdog claim` for this task again.", prompt_text)
        self.assertIn("Prefer Blackdog CLI output over direct reads of raw state files", prompt_text)
        self.assertIn("Commit your code changes on that task branch", prompt_text)
        self.assertIn("Do not run `./.VE/bin/blackdog complete` for this task from a branch-backed child run", prompt_text)
        paths = self.runtime_paths()
        state = json.loads(paths.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][task_id]["status"], "done")
        self.assertEqual((self.root / "feature.txt").read_text(encoding="utf-8"), task_id + "\n")
        self.assertIsNotNone(payload["children"][0]["task_branch"])
        self.assertIsNone(payload["children"][0]["land_error"])
        self.assertIsNotNone(payload["children"][0]["land_result"])
        self.assertTrue((child_run_dir / "changes.diff").exists())
        self.assertTrue((child_run_dir / "changes.stat.txt").exists())
        result_files = sorted((paths.results_dir / task_id).glob("*.json"))
        self.assertTrue(result_files)
        branch_check = subprocess.run(
            ["git", "-C", str(self.root), "show-ref", "--verify", f"refs/heads/{payload['children'][0]['task_branch']}"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(branch_check.returncode, 0)
        resolved_messages = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "inbox",
                    "list",
                    "--project-root",
                    str(self.root),
                    "--recipient",
                    "supervisor/child-01",
                    "--status",
                    "resolved",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(len(resolved_messages), 1)

    def test_supervise_run_times_out_child_and_releases_task(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        install_exec_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import sys
import time


def main() -> int:
    args = sys.argv[1:]
    if args == ["--help"]:
        print("Commands:\\n  exec")
        return 0
    if not args or args[0] != "exec":
        print("expected exec launcher", file=sys.stderr)
        return 2
    time.sleep(10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint launcher for supervisor timeout test",
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Timed out child task",
            "--bucket",
            "core",
            "--why",
            "Need to prove timed out child runs release their claims and record evidence.",
            "--evidence",
            "A child that exceeds the supervisor deadline should be blocked and released.",
            "--safe-first-slice",
            "Run one child longer than the timeout window.",
            "--path",
            "README.md",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Timeouts",
            "--wave",
            "0",
        )
        task_id = task_ids_by_title(self.root)["Timed out child task"]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "run",
                "--project-root",
                str(self.root),
                "--id",
                task_id,
                "--timeout-seconds",
                "1",
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        child = payload["children"][0]
        self.assertTrue(child["timed_out"])
        self.assertEqual(child["final_task_status"], "released")
        self.assertIsNone(child["land_result"])
        self.assertIsNone(child["land_error"])
        self.assertTrue(Path(child["workspace"]).exists())

        state = json.loads(paths.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][task_id]["status"], "released")

        result_files = sorted((paths.results_dir / task_id).glob("*.json"))
        self.assertTrue(result_files)
        result_payload = json.loads(result_files[-1].read_text(encoding="utf-8"))
        self.assertEqual(result_payload["status"], "blocked")
        self.assertIn("Timed out before the supervisor deadline", result_payload["validation"])
        rendered_snapshot = wait_for_html_snapshot(
            paths.html_file,
            lambda payload: any(
                row.get("id") == task_id and row.get("latest_run_status") == "timed-out"
                for row in payload.get("tasks", [])
            ),
            timeout=5,
        )
        rendered_task = next(row for row in rendered_snapshot["tasks"] if row["id"] == task_id)
        self.assertEqual(rendered_task["operator_status"], "Failed")
        self.assertNotIn(task_id, [row["id"] for row in rendered_snapshot["active_tasks"]])
        self.assertIn(task_id, [row["id"] for row in rendered_snapshot["board_tasks"]])

    def test_supervise_run_releases_task_after_clean_child_exit_without_completion(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        install_exec_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import sys


def main() -> int:
    args = sys.argv[1:]
    if args == ["--help"]:
        print("Commands:\\n  exec")
        return 0
    if not args or args[0] != "exec":
        print("expected exec launcher", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint launcher for clean child exit test",
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Clean exit child task",
            "--bucket",
            "core",
            "--why",
            "Need to prove clean child exits still release unfinished tasks.",
            "--evidence",
            "A child that exits 0 without landing work should record a partial result and release the claim.",
            "--safe-first-slice",
            "Run one child that exits successfully without writing a result or commit.",
            "--path",
            "README.md",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Cleanup",
            "--wave",
            "0",
        )
        task_id = task_ids_by_title(self.root)["Clean exit child task"]
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "run",
                "--project-root",
                str(self.root),
                "--id",
                task_id,
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        child = payload["children"][0]
        self.assertFalse(child["timed_out"])
        self.assertEqual(child["exit_code"], 0)
        self.assertEqual(child["final_task_status"], "released")
        self.assertIsNone(child["land_result"])
        self.assertIsNone(child["land_error"])
        self.assertTrue(Path(child["workspace"]).exists())

        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][task_id]["status"], "released")

        result_files = sorted((self.runtime_paths().results_dir / task_id).glob("*.json"))
        self.assertTrue(result_files)
        result_payload = json.loads(result_files[-1].read_text(encoding="utf-8"))
        self.assertEqual(result_payload["status"], "partial")
        self.assertIn("Child run exited cleanly but did not complete the task.", result_payload["residual"])

    def test_supervise_run_blocks_on_dirty_primary_worktree(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        install_exec_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if args == ["--help"]:
        print("Commands:\\n  exec")
        return 0
    if not args or args[0] != "exec":
        print("expected exec launcher", file=sys.stderr)
        return 2
    project_root = Path(os.environ["BLACKDOG_PROJECT_ROOT"])
    task_id = os.environ["BLACKDOG_TASK_ID"]
    actor = os.environ["BLACKDOG_AGENT_NAME"]
    Path("dirty-landed.txt").write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", "dirty-landed.txt"], check=True)
    subprocess.run(["git", "commit", "-m", f"Land {task_id} from dirty primary child"], check=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "blackdog.cli",
            "result",
            "record",
            "--project-root",
            str(project_root),
            "--id",
            task_id,
            "--actor",
            actor,
            "--status",
            "success",
            "--what-changed",
            f"child completed {task_id}",
            "--validation",
            "fake-dirty-child",
        ],
        check=True,
        env=os.environ.copy(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint dirty primary launcher for supervisor contract-violation test",
        )

        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Materialize dirty child workspace",
            "--bucket",
            "integration",
            "--why",
            "The supervisor should expose current repo changes to git-worktree child runs.",
            "--evidence",
            "A child worktree created from HEAD alone misses live tracked and untracked edits.",
            "--safe-first-slice",
            "Launch one child that validates modified, deleted, and untracked files in its workspace.",
            "--path",
            "tracked.txt",
            "--path",
            "deleted.txt",
            "--path",
            "untracked.txt",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Workspace lane",
            "--wave",
            "0",
        )
        (self.root / "dirty.txt").write_text("dirty primary worktree\n", encoding="utf-8")

        task_id = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "next", "--project-root", str(self.root), "--format", "json"],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )[0]["id"]
        payload = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "supervise",
                    "run",
                    "--project-root",
                    str(self.root),
                    "--id",
                    task_id,
                    "--format",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )

        self.assertEqual(payload["children"][0]["exit_code"], 0)
        self.assertIsNone(payload["children"][0]["launch_error"])
        self.assertIsNone(payload["children"][0]["land_result"])
        self.assertIn("dirty primary worktree contract violation", payload["children"][0]["land_error"])
        self.assertTrue((self.root / "dirty.txt").exists())
        self.assertFalse((self.root / "dirty-landed.txt").exists())
        self.assertTrue(Path(payload["children"][0]["workspace"]).exists())
        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][task_id]["status"], "released")
        branch_check = subprocess.run(
            ["git", "-C", str(self.root), "show-ref", "--verify", f"refs/heads/{payload['children'][0]['task_branch']}"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(branch_check.returncode, 0)
        stash_list = subprocess.run(
            ["git", "-C", str(self.root), "stash", "list"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(stash_list.strip(), "")
        result_files = sorted((self.runtime_paths().results_dir / task_id).glob("*.json"))
        self.assertGreaterEqual(len(result_files), 2)
        results = [json.loads(path.read_text(encoding="utf-8")) for path in result_files]
        blocked_results = [row for row in results if row["actor"] == "supervisor" and row["status"] == "blocked"]
        self.assertEqual(len(blocked_results), 1)
        self.assertTrue(blocked_results[0]["needs_user_input"])
        self.assertIn(
            "Clean up or land the primary worktree changes in the primary checkout.",
            blocked_results[0]["followup_candidates"],
        )
        open_messages = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "inbox",
                    "list",
                    "--project-root",
                    str(self.root),
                    "--recipient",
                    "supervisor",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertTrue(
            any("Blackdog will not auto-stash the primary checkout." in row["body"] for row in open_messages)
        )

    def test_supervise_run_drains_multiple_tasks(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        launcher_script = self.root / "codex"
        launcher_script.write_text(
            """
#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if args == ["--help"]:
        print("Commands:\\n  exec")
        return 0
    if not args or args[0] != "exec":
        print("expected exec launcher", file=sys.stderr)
        return 2
    project_root = Path(os.environ["BLACKDOG_PROJECT_ROOT"])
    task_id = os.environ["BLACKDOG_TASK_ID"]
    actor = os.environ["BLACKDOG_AGENT_NAME"]
    file_name = f"run-{task_id}.txt"
    Path(file_name).write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", file_name], check=True)
    subprocess.run(["git", "commit", "-m", f"Commit {task_id} from run child"], check=True)
    env = os.environ.copy()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "blackdog.cli",
            "result",
            "record",
            "--project-root",
            str(project_root),
            "--id",
            task_id,
            "--actor",
            actor,
            "--status",
            "success",
            "--what-changed",
            f"run child completed {task_id}",
            "--validation",
            "fake-run-child",
        ],
        check=True,
        env=env,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""".strip()
            + "\n",
            encoding="utf-8",
        )
        launcher_script.chmod(0o755)
        for title in ("Run task one", "Run task two"):
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--bucket",
                "core",
                "--why",
                "Need the supervisor run to keep draining work until idle.",
                "--evidence",
                "One run should pick up later-ready work without requiring another command.",
                "--safe-first-slice",
                "Run one child at a time until both tasks are done.",
                "--path",
                "README.md",
                "--epic-title",
                "Supervisor",
                "--lane-title",
                "Run lane",
                "--wave",
                "0",
            )
        profile_text = (self.root / "blackdog.toml").read_text(encoding="utf-8")
        profile_text = profile_text.replace(
            'launch_command = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]',
            f'launch_command = ["{launcher_script}", "exec", "--dangerously-bypass-approvals-and-sandbox"]',
        )
        (self.root / "blackdog.toml").write_text(profile_text, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "blackdog.toml", "codex"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Checkpoint run launcher for supervisor landing test"],
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "supervise",
                    "run",
                    "--project-root",
                    str(self.root),
                    "--count",
                    "1",
                    "--poll-interval-seconds",
                    "0",
                    "--format",
                    "json",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )

        self.assertEqual(payload["final_status"], "idle")
        expected_ids = task_ids_by_title(self.root)
        self.assertEqual([child["task_id"] for child in payload["children"]], [expected_ids["Run task one"], expected_ids["Run task two"]])
        self.assertGreaterEqual(len(payload["steps"]), 3)
        for child in payload["children"]:
            self.assertIsNotNone(child["land_result"])
            self.assertIsNone(child["land_error"])
        status_file = Path(payload["status_file"])
        self.assertTrue(status_file.exists())
        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        done_count = sum(1 for entry in state["task_claims"].values() if entry.get("status") == "done")
        self.assertEqual(done_count, 2)

    def test_supervise_run_drains_after_stop_message(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        for title in ("Boundary task one", "Boundary task two"):
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--bucket",
                "core",
                "--why",
                "Need to test stop semantics while a run is active.",
                "--evidence",
                "The supervisor run should drain active work and avoid launching the next task after stop.",
                "--safe-first-slice",
                "Hold the first child open, send stop, then release it.",
                "--path",
                "README.md",
                "--epic-title",
                "Supervisor",
                "--lane-title",
                "Boundary lane",
                "--wave",
                "0",
            )
        task_ids = task_ids_by_title(self.root)
        first_task_id = task_ids["Boundary task one"]
        sync_dir = self.runtime_paths().control_dir / "run-stop-sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        (sync_dir / f"gate-{first_task_id}.txt").write_text("hold\n", encoding="utf-8")
        install_exec_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if args == ["--help"]:
        print("Commands:\\n  exec")
        return 0
    if not args or args[0] != "exec":
        print("expected exec launcher", file=sys.stderr)
        return 2
    project_root = Path(os.environ["BLACKDOG_PROJECT_ROOT"])
    task_id = os.environ["BLACKDOG_TASK_ID"]
    actor = os.environ["BLACKDOG_AGENT_NAME"]
    sync_dir = project_root / ".git" / "blackdog" / "run-stop-sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    started_file = sync_dir / f"started-{task_id}.txt"
    gate_file = sync_dir / f"gate-{task_id}.txt"
    release_file = sync_dir / f"release-{task_id}.txt"
    started_file.write_text("started\\n", encoding="utf-8")
    if gate_file.exists():
        deadline = time.time() + 10
        while time.time() < deadline and not release_file.exists():
            time.sleep(0.05)
        if not release_file.exists():
            print(f"timed out waiting for release of {task_id}", file=sys.stderr)
            return 3
    file_name = f"boundary-{task_id}.txt"
    Path(file_name).write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", file_name], check=True)
    subprocess.run(["git", "commit", "-m", f"Commit {task_id} from boundary child"], check=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "blackdog.cli",
            "result",
            "record",
            "--project-root",
            str(project_root),
            "--id",
            task_id,
            "--actor",
            actor,
            "--status",
            "success",
            "--what-changed",
            f"boundary child completed {task_id}",
            "--validation",
            "fake-boundary-child",
        ],
        check=True,
        env=os.environ.copy(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint gated launcher for stop test",
            )

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "run",
                "--project-root",
                str(self.root),
                "--actor",
                "supervisor",
                "--count",
                "1",
                "--poll-interval-seconds",
                "0.2",
                "--format",
                "json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        try:
            wait_for_file(sync_dir / f"started-{first_task_id}.txt", timeout=10)
            run_cli(
                "inbox",
                "send",
                "--project-root",
                str(self.root),
                "--sender",
                "user",
                "--recipient",
                "supervisor",
                "--kind",
                "instruction",
                "--tag",
                "stop",
                "--body",
                "stop after the current task drains",
            )
            (sync_dir / f"release-{first_task_id}.txt").write_text("release\n", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)
        payload = json.loads(stdout)
        self.assertEqual(payload["final_status"], "stopped")
        self.assertIn("stopped_by_message_id", payload)
        self.assertEqual(len(payload["children"]), 1)
        self.assertEqual(payload["children"][0]["task_id"], first_task_id)
        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        done_count = sum(1 for entry in state["task_claims"].values() if entry.get("status") == "done")
        self.assertEqual(done_count, 1)
        ready_rows = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "next", "--project-root", str(self.root), "--format", "json"],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(len(ready_rows), 1)

    def test_supervise_run_picks_up_task_added_while_current_task_runs(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Current running task",
            "--bucket",
            "core",
            "--why",
            "Need to prove the run rereads backlog state after a running task finishes.",
            "--evidence",
            "A downstream task added during the active child run should be claimed before the run exits.",
            "--safe-first-slice",
            "Hold one child run open, add a downstream task, then release the child.",
            "--path",
            "README.md",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Dynamic lane",
            "--wave",
            "0",
        )
        current_task_id = task_ids_by_title(self.root)["Current running task"]
        sync_dir = self.runtime_paths().control_dir / "run-sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        (sync_dir / f"gate-{current_task_id}.txt").write_text("hold\n", encoding="utf-8")
        install_exec_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if args == ["--help"]:
        print("Commands:\\n  exec")
        return 0
    if not args or args[0] != "exec":
        print("expected exec launcher", file=sys.stderr)
        return 2
    project_root = Path(os.environ["BLACKDOG_PROJECT_ROOT"])
    task_id = os.environ["BLACKDOG_TASK_ID"]
    actor = os.environ["BLACKDOG_AGENT_NAME"]
    sync_dir = project_root / ".git" / "blackdog" / "run-sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    started_file = sync_dir / f"started-{task_id}.txt"
    gate_file = sync_dir / f"gate-{task_id}.txt"
    release_file = sync_dir / f"release-{task_id}.txt"
    started_file.write_text("started\\n", encoding="utf-8")
    if gate_file.exists():
        deadline = time.time() + 10
        while time.time() < deadline and not release_file.exists():
            time.sleep(0.05)
        if not release_file.exists():
            print(f"timed out waiting for release of {task_id}", file=sys.stderr)
            return 3
    file_name = f"dynamic-{task_id}.txt"
    Path(file_name).write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", file_name], check=True)
    subprocess.run(["git", "commit", "-m", f"Commit {task_id} after dynamic backlog update"], check=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "blackdog.cli",
            "result",
            "record",
            "--project-root",
            str(project_root),
            "--id",
            task_id,
            "--actor",
            actor,
            "--status",
            "success",
            "--what-changed",
            f"completed {task_id} after dynamic backlog update",
            "--validation",
            "fake-dynamic-child",
        ],
        check=True,
        env=os.environ.copy(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint gated launcher for dynamic add test",
        )

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "run",
                "--project-root",
                str(self.root),
                "--actor",
                "supervisor",
                "--count",
                "1",
                "--poll-interval-seconds",
                "0",
                "--format",
                "json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        try:
            wait_for_file(sync_dir / f"started-{current_task_id}.txt", timeout=10)
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                "Task added during active run",
                "--bucket",
                "core",
                "--why",
                "Need to prove the run does not stop after the current child finishes.",
                "--evidence",
                "A downstream task added during execution should be launched before the run drains.",
                "--safe-first-slice",
                "Append one task to the current lane while the first child is still running.",
                "--path",
                "README.md",
                "--epic-title",
                "Supervisor",
                "--lane-title",
                "Dynamic lane",
                "--wave",
                "0",
            )
            downstream_task_id = task_ids_by_title(self.root)["Task added during active run"]
            (sync_dir / f"release-{current_task_id}.txt").write_text("release\n", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)

        payload = json.loads(stdout)
        self.assertEqual(payload["final_status"], "idle")
        self.assertEqual([child["task_id"] for child in payload["children"]], [current_task_id, downstream_task_id])
        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][current_task_id]["status"], "done")
        self.assertEqual(state["task_claims"][downstream_task_id]["status"], "done")

    def test_supervise_run_picks_up_task_approved_while_current_task_runs(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Current running task",
            "--bucket",
            "core",
            "--why",
            "Need to prove an active run notices approval decisions before it drains.",
            "--evidence",
            "A task approved while another child is running should launch before the run exits.",
            "--safe-first-slice",
            "Hold one child run open, approve the downstream task, then release the child.",
            "--path",
            "README.md",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Approval lane",
            "--wave",
            "0",
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Approval-gated downstream task",
            "--bucket",
            "core",
            "--why",
            "Need to prove approval decisions unblock queued work during an active run.",
            "--evidence",
            "The supervisor should pick up the task after approval instead of returning idle first.",
            "--safe-first-slice",
            "Approve the queued task while the first child is still running.",
            "--path",
            "README.md",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Approval lane",
            "--wave",
            "0",
            "--requires-approval",
            "--approval-reason",
            "Explicit operator approval is required before this task can start.",
        )
        ids = task_ids_by_title(self.root)
        current_task_id = ids["Current running task"]
        downstream_task_id = ids["Approval-gated downstream task"]
        paths = self.runtime_paths()
        sync_dir = paths.control_dir / "run-approval-sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        (sync_dir / f"gate-{current_task_id}.txt").write_text("hold\n", encoding="utf-8")
        install_exec_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if args == ["--help"]:
        print("Commands:\\n  exec")
        return 0
    if not args or args[0] != "exec":
        print("expected exec launcher", file=sys.stderr)
        return 2
    project_root = Path(os.environ["BLACKDOG_PROJECT_ROOT"])
    task_id = os.environ["BLACKDOG_TASK_ID"]
    actor = os.environ["BLACKDOG_AGENT_NAME"]
    sync_dir = project_root / ".git" / "blackdog" / "run-approval-sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    started_file = sync_dir / f"started-{task_id}.txt"
    gate_file = sync_dir / f"gate-{task_id}.txt"
    release_file = sync_dir / f"release-{task_id}.txt"
    started_file.write_text("started\\n", encoding="utf-8")
    if gate_file.exists():
        deadline = time.time() + 10
        while time.time() < deadline and not release_file.exists():
            time.sleep(0.05)
        if not release_file.exists():
            print(f"timed out waiting for release of {task_id}", file=sys.stderr)
            return 3
    file_name = f"approval-{task_id}.txt"
    Path(file_name).write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", file_name], check=True)
    subprocess.run(["git", "commit", "-m", f"Commit {task_id} after approval update"], check=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "blackdog.cli",
            "result",
            "record",
            "--project-root",
            str(project_root),
            "--id",
            task_id,
            "--actor",
            actor,
            "--status",
            "success",
            "--what-changed",
            f"completed {task_id} after approval update",
            "--validation",
            "fake-approval-child",
        ],
        check=True,
        env=os.environ.copy(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint gated launcher for approval test",
        )

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "run",
                "--project-root",
                str(self.root),
                "--actor",
                "supervisor",
                "--count",
                "1",
                "--poll-interval-seconds",
                "0",
                "--format",
                "json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        try:
            wait_for_file(sync_dir / f"started-{current_task_id}.txt", timeout=10)
            run_cli(
                "decide",
                "--project-root",
                str(self.root),
                "--id",
                downstream_task_id,
                "--agent",
                "operator",
                "--decision",
                "approved",
                "--note",
                "Approved while the current child run is still active.",
            )
            (sync_dir / f"release-{current_task_id}.txt").write_text("release\n", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)

        payload = json.loads(stdout)
        self.assertEqual(payload["final_status"], "idle")
        self.assertEqual([child["task_id"] for child in payload["children"]], [current_task_id, downstream_task_id])
        state = json.loads(paths.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][current_task_id]["status"], "done")
        self.assertEqual(state["task_claims"][downstream_task_id]["status"], "done")
        self.assertEqual(state["approval_tasks"][downstream_task_id]["status"], "done")

        rendered_snapshot = html_snapshot(paths.html_file)
        board_ids = [row["id"] for row in rendered_snapshot["board_tasks"]]
        self.assertEqual(board_ids, [current_task_id, downstream_task_id])
        rendered_tasks = {row["id"]: row for row in rendered_snapshot["tasks"]}
        self.assertEqual(rendered_tasks[current_task_id]["operator_status"], "Complete")
        self.assertEqual(rendered_tasks[downstream_task_id]["operator_status"], "Complete")

    def test_supervise_run_refreshes_static_html_while_child_is_running(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Render while child runs",
            "--bucket",
            "html",
            "--why",
            "Need the static operator view to stay current during an active child run.",
            "--evidence",
            "The backlog index should show a running task before the child completes.",
            "--safe-first-slice",
            "Hold a child run open and wait for the rendered HTML snapshot to flip to running.",
            "--path",
            "src/blackdog/supervisor.py",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Rendered heartbeats",
            "--wave",
            "0",
        )
        task_id = task_ids_by_title(self.root)["Render while child runs"]
        paths = self.runtime_paths()
        sync_dir = paths.control_dir / "render-sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        initial_snapshot = html_snapshot(paths.html_file)
        install_exec_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if args == ["--help"]:
        print("Commands:\\n  exec")
        return 0
    if not args or args[0] != "exec":
        print("expected exec launcher", file=sys.stderr)
        return 2
    project_root = Path(os.environ["BLACKDOG_PROJECT_ROOT"])
    task_id = os.environ["BLACKDOG_TASK_ID"]
    actor = os.environ["BLACKDOG_AGENT_NAME"]
    sync_dir = project_root / ".git" / "blackdog" / "render-sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    started_file = sync_dir / f"started-{task_id}.txt"
    release_file = sync_dir / f"release-{task_id}.txt"
    started_file.write_text("started\\n", encoding="utf-8")
    deadline = time.time() + 10
    while time.time() < deadline and not release_file.exists():
        time.sleep(0.05)
    if not release_file.exists():
        print(f"timed out waiting for release of {task_id}", file=sys.stderr)
        return 3
    Path("render-running.txt").write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", "render-running.txt"], check=True)
    subprocess.run(["git", "commit", "-m", f"Commit {task_id} after render refresh"], check=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "blackdog.cli",
            "result",
            "record",
            "--project-root",
            str(project_root),
            "--id",
            task_id,
            "--actor",
            actor,
            "--status",
            "success",
            "--what-changed",
            f"render refresh child completed {task_id}",
            "--validation",
            "fake-render-refresh-child",
        ],
        check=True,
        env=os.environ.copy(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint gated launcher for render refresh test",
        )

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "run",
                "--project-root",
                str(self.root),
                "--actor",
                "supervisor",
                "--count",
                "1",
                "--poll-interval-seconds",
                "0",
                "--format",
                "json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        try:
            wait_for_file(sync_dir / f"started-{task_id}.txt", timeout=10)
            refreshed_snapshot = wait_for_html_snapshot(
                paths.html_file,
                lambda payload: any(
                    row.get("id") == task_id and row.get("latest_run_status") == "running"
                    for row in payload.get("active_tasks", [])
                ),
                timeout=10,
            )
            active_rows = [row for row in refreshed_snapshot["active_tasks"] if row["id"] == task_id]
            self.assertEqual(len(active_rows), 1)
            self.assertEqual(active_rows[0]["operator_status"], "Running")
            self.assertFalse((sync_dir / f"release-{task_id}.txt").exists())
            (sync_dir / f"release-{task_id}.txt").write_text("release\n", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)

        payload = json.loads(stdout)
        self.assertEqual(payload["final_status"], "idle")
        self.assertEqual([child["task_id"] for child in payload["children"]], [task_id])

    def test_supervise_run_skips_removed_downstream_task_and_runs_next_available(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        for title in ("Current running task", "Removed downstream task", "Remaining downstream task"):
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--bucket",
                "core",
                "--why",
                "Need to prove the run rereads backlog state after downstream removal.",
                "--evidence",
                "A removed task should disappear from the next runnable selection.",
                "--safe-first-slice",
                "Hold one child run open, remove a downstream task, then release the child.",
                "--path",
                "README.md",
                "--epic-title",
                "Supervisor",
                "--lane-title",
                "Dynamic lane",
                "--wave",
                "0",
            )
        ids = task_ids_by_title(self.root)
        current_task_id = ids["Current running task"]
        removed_task_id = ids["Removed downstream task"]
        remaining_task_id = ids["Remaining downstream task"]
        sync_dir = self.runtime_paths().control_dir / "run-sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        (sync_dir / f"gate-{current_task_id}.txt").write_text("hold\n", encoding="utf-8")
        install_exec_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if args == ["--help"]:
        print("Commands:\\n  exec")
        return 0
    if not args or args[0] != "exec":
        print("expected exec launcher", file=sys.stderr)
        return 2
    project_root = Path(os.environ["BLACKDOG_PROJECT_ROOT"])
    task_id = os.environ["BLACKDOG_TASK_ID"]
    actor = os.environ["BLACKDOG_AGENT_NAME"]
    sync_dir = project_root / ".git" / "blackdog" / "run-sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    started_file = sync_dir / f"started-{task_id}.txt"
    gate_file = sync_dir / f"gate-{task_id}.txt"
    release_file = sync_dir / f"release-{task_id}.txt"
    started_file.write_text("started\\n", encoding="utf-8")
    if gate_file.exists():
        deadline = time.time() + 10
        while time.time() < deadline and not release_file.exists():
            time.sleep(0.05)
        if not release_file.exists():
            print(f"timed out waiting for release of {task_id}", file=sys.stderr)
            return 3
    file_name = f"dynamic-{task_id}.txt"
    Path(file_name).write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", file_name], check=True)
    subprocess.run(["git", "commit", "-m", f"Commit {task_id} after dynamic backlog update"], check=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "blackdog.cli",
            "result",
            "record",
            "--project-root",
            str(project_root),
            "--id",
            task_id,
            "--actor",
            actor,
            "--status",
            "success",
            "--what-changed",
            f"completed {task_id} after dynamic backlog update",
            "--validation",
            "fake-dynamic-child",
        ],
        check=True,
        env=os.environ.copy(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint gated launcher for dynamic remove test",
        )

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "run",
                "--project-root",
                str(self.root),
                "--actor",
                "supervisor",
                "--count",
                "1",
                "--poll-interval-seconds",
                "0",
                "--format",
                "json",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        try:
            wait_for_file(sync_dir / f"started-{current_task_id}.txt", timeout=10)
            remove_task_from_backlog(self.root, removed_task_id)
            self.assertNotIn("Removed downstream task", task_ids_by_title(self.root))
            (sync_dir / f"release-{current_task_id}.txt").write_text("release\n", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)

        payload = json.loads(stdout)
        self.assertEqual(payload["final_status"], "idle")
        self.assertEqual([child["task_id"] for child in payload["children"]], [current_task_id, remaining_task_id])
        executed_task_ids = [child["task_id"] for child in payload["children"]]
        self.assertNotIn(removed_task_id, executed_task_ids)
        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][current_task_id]["status"], "done")
        self.assertEqual(state["task_claims"][remaining_task_id]["status"], "done")
        self.assertNotIn(removed_task_id, state["task_claims"])
