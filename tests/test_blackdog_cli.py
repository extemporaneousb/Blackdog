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
from blackdog.store import (
    append_jsonl,
    atomic_write_text,
    load_events,
    load_inbox,
    load_jsonl,
    load_task_results,
    record_task_result,
    resolve_message,
    save_state,
    send_message,
)
from blackdog.supervisor import _build_child_prompt, _resolved_launch_command, _write_run_status
from blackdog.ui import UI_SNAPSHOT_SCHEMA_VERSION, build_ui_snapshot, render_static_html
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


def install_model_backed_supervisor_launcher(
    root: Path,
    script_body: str,
    *,
    commit_message: str,
    use_low_effort: bool = True,
) -> Path:
    launcher_script = root / "codex"
    launcher_script.write_text(script_body.strip() + "\n", encoding="utf-8")
    launcher_script.chmod(0o755)
    launch_args = ["exec", "--dangerously-bypass-approvals-and-sandbox"]
    if use_low_effort:
        launch_args.extend(["--effort", "low"])
    profile_text = (root / "blackdog.toml").read_text(encoding="utf-8")
    rendered_launch_command = ", ".join(json.dumps(arg) for arg in [str(launcher_script), *launch_args])
    profile_text, replaced = re.subn(
        r"^\s*launch_command = \[.*\]",
        f"launch_command = [{rendered_launch_command}]",
        profile_text,
        count=1,
        flags=re.M,
    )
    if replaced != 1:
        raise AssertionError("blackdog.toml does not include launch_command")
    (root / "blackdog.toml").write_text(profile_text, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "blackdog.toml", "codex"], check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", commit_message],
        check=True,
        capture_output=True,
        text=True,
    )
    return launcher_script


def install_dirty_primary_recovery_launcher(root: Path, *, commit_message: str) -> Path:
    return install_exec_launcher(
        root,
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
        commit_message=commit_message,
    )


def install_blocking_dirty_primary_launcher(root: Path, *, commit_message: str, primary_mode: str) -> Path:
    if primary_mode not in {"matching", "unrelated"}:
        raise AssertionError(f"unsupported primary_mode: {primary_mode}")
    flag_file = root / ".git" / "blackdog" / "dirty-primary-once"
    flag_file.parent.mkdir(parents=True, exist_ok=True)
    flag_file.write_text("dirty once\n", encoding="utf-8")
    primary_write = (
        '        (primary_root / "dirty-landed.txt").write_text(task_id + "\\n", encoding="utf-8")\n'
        if primary_mode == "matching"
        else '        (primary_root / "dirty.txt").write_text("dirty primary worktree\\n", encoding="utf-8")\n'
    )
    return install_exec_launcher(
        root,
        f"""
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
    primary_root = Path(os.environ["BLACKDOG_PRIMARY_WORKTREE"])
    flag_file = project_root / ".git" / "blackdog" / "dirty-primary-once"
    task_id = os.environ["BLACKDOG_TASK_ID"]
    actor = os.environ["BLACKDOG_AGENT_NAME"]
    Path("dirty-landed.txt").write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", "dirty-landed.txt"], check=True)
    subprocess.run(["git", "commit", "-m", f"Land {{task_id}} from dirty primary child"], check=True)
    if flag_file.exists():
{primary_write.rstrip()}
        flag_file.unlink()
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
            f"child completed {{task_id}}",
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
        commit_message=commit_message,
    )


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

    def test_tune_command_creates_stable_self_tuning_task(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        profile = load_profile(self.root)

        tune_payload = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "tune", "--project-root", str(self.root)],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(tune_payload["title"], "Auto-tune runtime contract and backlog health")
        self.assertEqual(tune_payload["bucket"], "skills")
        self.assertEqual(tune_payload["checks"], list(profile.validation_commands))
        self.assertIn("AGENTS.md", tune_payload["docs"])
        self.assertIn("docs/CLI.md", tune_payload["docs"])
        self.assertIn("docs/FILE_FORMATS.md", tune_payload["docs"])
        self.assertIn("docs/INTEGRATION.md", tune_payload["docs"])
        self.assertIn(".codex/skills/blackdog/SKILL.md", tune_payload["docs"])
        self.assertIn(".codex/skills/blackdog/agents/openai.yaml", tune_payload["docs"])
        self.assertIn("blackdog.toml", tune_payload["docs"])
        self.assertIn(str(paths.backlog_file), tune_payload["paths"])
        self.assertIn(str(paths.state_file), tune_payload["paths"])
        self.assertIn(str(paths.events_file), tune_payload["paths"])
        self.assertIn(str(paths.inbox_file), tune_payload["paths"])
        self.assertIn(str(paths.results_dir), tune_payload["paths"])
        self.assertIn(str(paths.profile_file), tune_payload["paths"])
        self.assertIn(str(paths.skill_dir / "SKILL.md"), tune_payload["paths"])
        self.assertIn("Review backlog history", tune_payload["safe_first_slice"])

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
        self.assertEqual(len(summary["next_rows"]), 1)
        self.assertEqual(summary["next_rows"][0]["id"], tune_payload["id"])
        self.assertEqual(summary["next_rows"][0]["title"], tune_payload["title"])
        self.assertIn(tune_payload["id"], {row["task_id"] for row in load_events(paths) if row["type"] == "task_added"})

        rerun_payload = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "tune", "--project-root", str(self.root)],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )
        self.assertEqual(rerun_payload["id"], tune_payload["id"])
        task_added_rows = [row for row in load_events(paths) if row["type"] == "task_added" and row.get("task_id") == tune_payload["id"]]
        self.assertEqual(len(task_added_rows), 1)

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

    def test_bootstrap_writes_baseline_agents_md_only_if_missing(self) -> None:
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

        agents_file = self.root / "AGENTS.md"
        self.assertTrue(agents_file.exists())
        agents_body = agents_file.read_text(encoding="utf-8")
        self.assertIn("# AGENTS", agents_body)
        self.assertIn("This repository was scaffolded with Blackdog.", agents_body)

        agents_file.write_text("# AGENTS\n\nHost-specific contract.\n", encoding="utf-8")
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
        self.assertEqual(agents_file.read_text(encoding="utf-8"), "# AGENTS\n\nHost-specific contract.\n")

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
        paths = self.runtime_paths()
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
        state = json.loads(paths.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][first_id]["status"], "claimed")
        self.assertNotIn("claim_expires_at", state["task_claims"][first_id])
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
        rendered = paths.html_file.read_text(encoding="utf-8")
        self.assertIn("Completed Tasks", rendered)
        self.assertIn(first_id, rendered)
        self.assertIn("Added the first slice.", rendered)
        self.assertIn('id="release-gates-panel"', rendered)

    def test_claim_records_reported_pid_without_a_lease_timeout(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Pid claim task",
            "--bucket",
            "core",
            "--why",
            "Need to prove direct claims can report a long-lived process without a lease timeout.",
            "--evidence",
            "The claim state and claim event should store the pid and omit any expiry timestamp.",
            "--safe-first-slice",
            "Claim one task with a pid and inspect state plus events.",
            "--path",
            "README.md",
            "--epic-title",
            "Claims",
            "--lane-title",
            "Pid reports",
            "--wave",
            "0",
        )
        task_id = task_ids_by_title(self.root)["Pid claim task"]

        claimed = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "claim",
                    "--project-root",
                    str(self.root),
                    "--agent",
                    "agent/a",
                    "--id",
                    task_id,
                    "--pid",
                    "4242",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )

        self.assertEqual(claimed, [{"id": task_id, "title": "Pid claim task", "claimed_pid": 4242}])
        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        claim_entry = state["task_claims"][task_id]
        self.assertEqual(claim_entry["status"], "claimed")
        self.assertEqual(claim_entry["claimed_pid"], 4242)
        self.assertEqual(claim_entry["claimed_process_missing_scans"], 0)
        self.assertNotIn("claim_expires_at", claim_entry)

        claim_events = [row for row in load_events(self.runtime_paths(), task_id=task_id) if row.get("type") == "claim"]
        self.assertEqual(len(claim_events), 1)
        self.assertEqual(claim_events[0]["payload"], {"claimed_pid": 4242})

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
            land_event = next(row for row in reversed(events) if row["type"] == "worktree_land")
            self.assertEqual(land_event["task_id"], task_id)
            self.assertEqual(land_event["payload"]["landed_commit"], landed["landed_commit"])

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
        self.assertEqual(snapshot["schema_version"], UI_SNAPSHOT_SCHEMA_VERSION)
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
        self.assertIn("Blackdog Backlog", html)
        self.assertIn('id="hero-panel"', html)
        self.assertIn('id="status-panel"', html)
        self.assertIn('id="queue-stats"', html)
        self.assertIn('id="middle-band"', html)
        self.assertIn('id="objectives-panel"', html)
        self.assertIn('id="objective-intro"', html)
        self.assertIn('id="objectives-table-body"', html)
        self.assertIn('id="release-gates-panel"', html)
        self.assertIn('id="release-gates-table-body"', html)
        self.assertIn('id="execution-panel"', html)
        self.assertIn('id="completed-panel"', html)
        self.assertIn('id="completed-history-scroll"', html)
        self.assertIn("Release Gates", html)
        self.assertIn('const activeObjectiveRows = objectiveRows.filter((row) => Array.isArray(row.active_task_ids) && row.active_task_ids.length);', html)
        self.assertIn('document.getElementById("objectives-table-body").innerHTML = activeObjectiveRows.length', html)
        self.assertIn('document.getElementById("release-gates-table-body").innerHTML = gateRows.length', html)
        self.assertIn("function renderObjectivesTable()", html)
        self.assertIn("function renderReleaseGatesPanel()", html)
        self.assertIn("function renderExecutionMap()", html)
        self.assertIn("function renderCompletedPanel()", html)
        self.assertIn("function groupedCompletedObjectives(tasks)", html)
        self.assertIn("completed-objective-group", html)
        self.assertNotIn('id="overview-panel"', html)
        self.assertNotIn('id="domains-panel"', html)
        self.assertNotIn('id="release-gates-list"', html)
        self.assertNotIn('class="eyebrow"', html)
        self.assertNotIn('hero-progress-label', html)

    def test_snapshot_includes_queue_status_counts(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        run_cli(
            "add",
            "--project-root", str(self.root),
            "--title", "Dependency root",
            "--bucket", "html",
            "--why", "A task can block its lane successor.",
            "--evidence", "Needed to expose waiting status in the queue counters.",
            "--safe-first-slice", "Keep task ordering stable inside one lane.",
            "--path", "src/blackdog/ui.py",
            "--epic-title", "Queue panel",
            "--lane-id", "queue-lane",
            "--lane-title", "Dependency lane",
            "--wave", "0",
        )
        run_cli(
            "add",
            "--project-root", str(self.root),
            "--title", "Dependent waiting task",
            "--bucket", "html",
            "--why", "The queue panel should show waiting.",
            "--evidence", "Blocking through lane predecessors drives waiting status.",
            "--safe-first-slice", "Keep the dependent task behind the dependency.",
            "--path", "src/blackdog/ui.py",
            "--epic-title", "Queue panel",
            "--lane-id", "queue-lane",
            "--lane-title", "Dependency lane",
            "--wave", "0",
        )
        run_cli(
            "add",
            "--project-root", str(self.root),
            "--title", "Running task",
            "--bucket", "html",
            "--why", "The queue panel should show running status from active child runs.",
            "--evidence", "Live runs should be tracked independently from claimed tasks.",
            "--safe-first-slice", "Expose an actively running operator status for a slice.",
            "--path", "src/blackdog/ui.py",
            "--epic-title", "Queue panel",
            "--lane-title", "Running lane",
            "--wave", "0",
        )
        run_cli(
            "add",
            "--project-root", str(self.root),
            "--title", "Blocked task",
            "--bucket", "html",
            "--why", "The queue panel should show blocked status.",
            "--evidence", "Stalled runs surface as blocked in the operator panel.",
            "--safe-first-slice", "Expose blocked status through child artifacts.",
            "--path", "src/blackdog/ui.py",
            "--epic-title", "Queue panel",
            "--lane-title", "Blocked lane",
            "--wave", "0",
        )
        run_cli(
            "add",
            "--project-root", str(self.root),
            "--title", "Completed today",
            "--bucket", "html",
            "--why", "The queue panel should show completed today counts.",
            "--evidence", "Completed tasks contribute to history and completion counters.",
            "--safe-first-slice", "Collect all-time and daily completion metrics.",
            "--path", "src/blackdog/ui.py",
            "--epic-title", "Queue panel",
            "--lane-title", "Complete lane",
            "--wave", "0",
        )
        task_ids = task_ids_by_title(self.root)
        running_task = task_ids["Running task"]
        blocked_task = task_ids["Blocked task"]
        completed_task = task_ids["Completed today"]

        run_cli("complete", "--project-root", str(self.root), "--agent", "agent/owner", "--id", completed_task, "--note", "completed")
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-queue-running",
                "type": "child_launch",
                "at": "2026-03-19T10:10:00-07:00",
                "actor": "supervisor",
                "task_id": running_task,
                "payload": {
                    "run_id": "queuesweep1",
                    "child_agent": "supervisor/child-01",
                    "workspace": str(paths.supervisor_runs_dir / "20260319-101000-queuesweep1" / running_task),
                    "pid": os.getpid(),
                },
            },
        )
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-queue-blocked",
                "type": "child_finish",
                "at": "2026-03-19T10:12:00-07:00",
                "actor": "supervisor",
                "task_id": blocked_task,
                "payload": {
                    "run_id": "queuesweep1",
                    "child_agent": "supervisor/child-02",
                    "land_error": "blocked by temporary environment",
                },
            },
        )
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-queue-sweep",
                "type": "supervisor_run_sweep",
                "at": "2026-03-19T10:15:00-07:00",
                "actor": "supervisor",
                "task_id": None,
                "payload": {
                    "run_id": "queuesweep1",
                    "removed_task_ids": [completed_task],
                    "removed_lane_ids": ["queue-complete-lane"],
                    "removed_epic_ids": [],
                    "wave_map": {"0": 0},
                },
            },
        )

        snapshot = build_ui_snapshot(load_profile(self.root))
        status = snapshot["queue_status"]

        self.assertEqual(status["running"], 1)
        self.assertEqual(status["waiting"], 1)
        self.assertEqual(status["blocked"], 1)
        self.assertEqual(status["completed_today"], 1)
        self.assertEqual(status["completed_all_time"], 1)
        self.assertEqual(status["last_sweep_completed"], 1)

        run_cli("render", "--project-root", str(self.root), "--actor", "tester")
        rendered_html = paths.html_file.read_text(encoding="utf-8")
        rendered_snapshot = html_snapshot(paths.html_file)
        self.assertEqual(snapshot["queue_status"], rendered_snapshot["queue_status"])
        status_function = re.search(
            r"function renderStatusPanel\(\) \{[\s\S]*?const stats = \[([\s\S]*?)\];",
            rendered_html,
        )
        self.assertIsNotNone(status_function)
        stats_body = status_function.group(1)
        labels = [
            "Running",
            "Waiting",
            "Blocked",
            "Last sweep completed",
            "Completed today",
            "Completed all-time",
        ]
        positions = [stats_body.index(f'["{label}"') for label in labels]
        self.assertEqual(positions, sorted(positions))

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
        self.assertIn("Blackdog Backlog", html)
        self.assertIn('id="status-panel"', html)
        self.assertIn('id="hero-meta-line"', html)
        self.assertIn('id="hero-links"', html)
        self.assertIn('id="queue-stats"', html)
        self.assertIn('id="middle-band"', html)
        self.assertIn('id="objectives-panel"', html)
        self.assertIn('id="objective-intro"', html)
        self.assertIn('id="objectives-table-body"', html)
        self.assertIn('id="release-gates-panel"', html)
        self.assertIn('id="release-gates-table-body"', html)
        self.assertIn('id="execution-panel"', html)
        self.assertIn('id="completed-panel"', html)
        self.assertIn('id="completed-history-scroll"', html)
        self.assertIn('id="hero-progress"', html)
        self.assertNotIn('id="task-search"', html)
        self.assertNotIn("renderStats()", html)
        self.assertIn("function renderProgressBar(progress, className = \"\")", html)
        self.assertIn("function applyProgressBars(root = document)", html)
        self.assertIn("function interactiveCardAttributes(taskId)", html)
        self.assertIn("function renderStatusPanel()", html)
        self.assertIn("function renderObjectivesTable()", html)
        self.assertIn("function renderReleaseGatesPanel()", html)
        self.assertIn("function renderExecutionMap()", html)
        self.assertIn("function renderCompletedPanel()", html)
        self.assertIn('["Backlog", links.backlog]', html)
        self.assertIn('["State", links.state]', html)
        self.assertIn("<h2>Status</h2>", html)
        self.assertIn("Execution Map", html)
        self.assertIn("Completed Tasks", html)
        self.assertIn("Release Gates", html)
        self.assertIn('data-objective-id="${escapeHtml(objective.id || objective.key || "objective")}"', html)
        self.assertIn('role="button" tabindex="0"', html)
        self.assertIn('document.addEventListener("keydown", (event) => {', html)
        self.assertIn("const heroHighlights = snapshot.hero_highlights || {};", html)
        self.assertNotIn("Git head", html)
        self.assertNotIn("Blackdog runtime", html)
        self.assertIn('document.getElementById("hero-meta-line").innerHTML = metaItems', html)
        self.assertIn('document.getElementById("hero-links").innerHTML = globalLinks()', html)
        self.assertIn('document.getElementById("queue-stats").innerHTML = stats.map', html)
        self.assertIn('document.getElementById("hero-progress").innerHTML = renderProgressBar(overallProgress, "progress-hero");', html)
        self.assertIn('document.getElementById("status-next-lines").innerHTML = lines.length', html)
        self.assertIn('document.getElementById("release-gates-table-body").innerHTML = gateRows.length', html)
        self.assertIn('document.getElementById("completed-history-scroll").innerHTML = grouped.length', html)
        self.assertIn('data-progress="${escapeHtml(progress.percent)}"', html)
        self.assertIn("supervisor-runs/20260314-120000-liverun1", html)
        self.assertIn('document.getElementById("hero-progress-detail").textContent = heroProgressSummary(overallProgress);', html)
        self.assertIn('document.getElementById("objective-intro").textContent =', html)
        self.assertIn('renderMetaItem("Active Branch"', html)
        self.assertNotIn('id="release-gates-list"', html)
        self.assertNotIn('class="eyebrow"', html)

    def test_completed_task_reader_exposes_model_response_and_commit_details(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        subprocess.run(
            ["git", "-C", str(self.root), "remote", "add", "origin", "https://github.com/example/blackdog-demo.git"],
            check=True,
            capture_output=True,
            text=True,
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Completed reader slice",
            "--bucket",
            "html",
            "--why",
            "Completed-task drill-in should show the recorded model response and landed commit metadata.",
            "--evidence",
            "Operators should not need to open stdout logs or resolve commit SHAs by hand.",
            "--safe-first-slice",
            "Render the completed-task reader from existing supervisor artifacts.",
            "--path",
            "src/blackdog/ui.py",
            "--epic-title",
            "Reader detail",
            "--lane-title",
            "Completed history",
            "--wave",
            "0",
        )
        task_id = task_ids_by_title(self.root)["Completed reader slice"]
        run_cli("claim", "--project-root", str(self.root), "--agent", "supervisor/child-01", "--id", task_id)

        commit_subject = "Record landed change for reader test"
        commit_body = "Expose landed commit metadata to the completed-task reader."
        landing_file = self.root / "reader-detail.txt"
        landing_file.write_text("landed change\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "reader-detail.txt"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", commit_subject, "-m", commit_body],
            check=True,
            capture_output=True,
            text=True,
        )
        landed_commit = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        paths = self.runtime_paths()
        run_id = "readerdemo"
        run_dir = paths.supervisor_runs_dir / f"20260317-120000-{run_id}" / task_id
        run_dir.mkdir(parents=True)
        model_response = (
            "1. What changed: threaded the completed-task reader through the child stdout artifact.\n"
            "2. Why: operators need the actual model response without leaving the board."
        )
        (run_dir / "stdout.log").write_text(model_response, encoding="utf-8")
        record_task_result(
            paths,
            task_id=task_id,
            actor="supervisor/child-01",
            status="success",
            what_changed=["Recorded completed-task reader output."],
            validation=["reader-detail-test"],
            residual=[],
            needs_user_input=False,
            followup_candidates=[],
            run_id=run_id,
        )
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-reader-child-finish",
                "type": "child_finish",
                "at": "2026-03-17T12:00:00-07:00",
                "actor": "supervisor",
                "task_id": task_id,
                "payload": {
                    "run_id": run_id,
                    "child_agent": "supervisor/child-01",
                    "branch": "agent/completed-reader-slice",
                    "target_branch": "main",
                    "final_task_status": "done",
                    "landed_commit": landed_commit,
                },
            },
        )
        run_cli("complete", "--project-root", str(self.root), "--agent", "supervisor/child-01", "--id", task_id, "--note", "done")

        snapshot = build_ui_snapshot(load_profile(self.root))
        task = next(row for row in snapshot["tasks"] if row["id"] == task_id)

        self.assertEqual(task["model_response"], model_response)
        self.assertFalse(task["model_response_truncated"])
        self.assertEqual(task["landed_commit"], landed_commit)
        self.assertEqual(task["landed_commit_short"], landed_commit[:12])
        self.assertEqual(task["landed_commit_url"], f"https://github.com/example/blackdog-demo/commit/{landed_commit}")
        self.assertIn(commit_subject, task["landed_commit_message"])
        self.assertIn(commit_body, task["landed_commit_message"])
        self.assertEqual(task["latest_result_what_changed"], ["Recorded completed-task reader output."])
        self.assertEqual(task["latest_result_validation"], ["reader-detail-test"])
        self.assertTrue(any(row["label"] == "Commit" for row in task["links"]))
        card_chip_keys = [row["key"] for row in task["card_status_chips"]]
        self.assertIn("landed", card_chip_keys)
        landed_chip = next(row for row in task["card_status_chips"] if row["key"] == "landed")
        self.assertEqual(landed_chip["label"], "Landed")
        self.assertEqual(landed_chip["href"], f"https://github.com/example/blackdog-demo/commit/{landed_commit}")

        run_cli("render", "--project-root", str(self.root), "--actor", "tester")
        html = paths.html_file.read_text(encoding="utf-8")
        self.assertIn("function preBlock(text, className = \"detail-pre\")", html)
        self.assertIn("function commitBlock(task)", html)
        self.assertIn('detailBlock("What Changed", listBlock(task.latest_result_what_changed), { wide: true })', html)
        self.assertIn('detailBlock("Validation", listBlock(task.latest_result_validation), { wide: true })', html)
        self.assertIn('detailBlock("Residual", listBlock(task.latest_result_residual), { wide: true })', html)
        self.assertNotIn('detailBlock("Latest Result Changes"', html)
        self.assertNotIn('detailBlock("Latest Result Validation"', html)
        self.assertNotIn('detailBlock("Latest Result Residual"', html)
        self.assertIn('detailBlock("Model Response", preBlock(task.model_response), { wide: true })', html)
        self.assertIn('detailBlock("Landed Commit", commitBlock(task), { wide: true })', html)

    def test_snapshot_completed_noop_completion_has_no_landed_badge(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "No-op completion task",
            "--bucket",
            "cli",
            "--why",
            "Need to verify no-op completions never show landed badges.",
            "--evidence",
            "A task completed without child-run changes should remain a no-op completion.",
            "--safe-first-slice",
            "Claim and complete a task without making any code changes.",
            "--path",
            "README.md",
            "--epic-title",
            "UI",
            "--lane-title",
            "Badges",
            "--wave",
            "0",
        )
        task_id = task_ids_by_title(self.root)["No-op completion task"]
        run_cli("claim", "--project-root", str(self.root), "--agent", "agent/noop", "--id", task_id)
        run_cli("complete", "--project-root", str(self.root), "--agent", "agent/noop", "--id", task_id, "--note", "done")

        snapshot = build_ui_snapshot(load_profile(self.root))
        task = next(row for row in snapshot["tasks"] if row["id"] == task_id)
        self.assertEqual(task["operator_status"], "Complete")
        self.assertEqual(task["operator_status_key"], "complete")
        self.assertFalse(task["latest_run_landed"])
        self.assertFalse(task["latest_run_branch_ahead"])
        card_chip_keys = [row["key"] for row in task["card_status_chips"]]
        self.assertNotIn("landed", card_chip_keys)
        self.assertIn("complete", card_chip_keys)

    def test_snapshot_direct_completed_landed_task_uses_worktree_land_metadata(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        subprocess.run(
            ["git", "-C", str(self.root), "remote", "add", "origin", "https://github.com/example/blackdog-demo.git"],
            check=True,
            capture_output=True,
            text=True,
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Direct landed task",
            "--bucket",
            "cli",
            "--why",
            "Direct completion flow should still show landed metadata.",
            "--evidence",
            "A landed direct task should render the same landed badge as a supervisor-completed task.",
            "--safe-first-slice",
            "Correlate direct worktree landing metadata back to the completed task card.",
            "--path",
            "src/blackdog/cli.py",
            "--epic-title",
            "UI",
            "--lane-title",
            "Badges",
            "--wave",
            "0",
        )
        task_id = task_ids_by_title(self.root)["Direct landed task"]
        run_cli("claim", "--project-root", str(self.root), "--agent", "agent/direct", "--id", task_id)

        commit_subject = "Record direct landed commit"
        commit_body = "Preserve landed metadata for direct completed tasks."
        landing_file = self.root / "direct-landed.txt"
        landing_file.write_text("direct landed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "direct-landed.txt"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", commit_subject, "-m", commit_body],
            check=True,
            capture_output=True,
            text=True,
        )
        landed_commit = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        paths = self.runtime_paths()
        branch = "agent/direct-landed-task"
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-direct-start",
                "type": "worktree_start",
                "at": "2026-03-18T17:12:20-07:00",
                "actor": "agent/direct",
                "task_id": task_id,
                "payload": {
                    "task_id": task_id,
                    "task_title": "Direct landed task",
                    "task_slug": "direct-landed-task",
                    "branch": branch,
                    "base_ref": "main",
                    "base_commit": "abc123",
                    "target_branch": "main",
                    "worktree_path": str(self.root / "wt-direct-landed"),
                    "primary_worktree": str(self.root),
                    "current_worktree": str(self.root / "wt-direct-landed"),
                },
            },
        )
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-direct-land",
                "type": "worktree_land",
                "at": "2026-03-18T17:51:30-07:00",
                "actor": "agent/direct",
                "task_id": None,
                "payload": {
                    "branch": branch,
                    "target_branch": "main",
                    "primary_worktree": str(self.root),
                    "target_worktree": str(self.root),
                    "landed_commit": landed_commit,
                    "cleanup": True,
                    "cleaned_worktree": str(self.root / "wt-direct-landed"),
                    "deleted_branch": True,
                    "removed_temporary_target": False,
                },
            },
        )
        run_cli("complete", "--project-root", str(self.root), "--agent", "agent/direct", "--id", task_id, "--note", "done")

        snapshot = build_ui_snapshot(load_profile(self.root))
        task = next(row for row in snapshot["tasks"] if row["id"] == task_id)

        self.assertTrue(task["latest_run_landed"])
        self.assertEqual(task["landed_commit"], landed_commit)
        self.assertEqual(task["landed_commit_short"], landed_commit[:12])
        self.assertEqual(task["landed_commit_url"], f"https://github.com/example/blackdog-demo/commit/{landed_commit}")
        self.assertIn(commit_subject, task["landed_commit_message"])
        self.assertIn(commit_body, task["landed_commit_message"])
        landed_chip = next(row for row in task["card_status_chips"] if row["key"] == "landed")
        self.assertEqual(landed_chip["label"], "Landed")
        self.assertEqual(landed_chip["href"], f"https://github.com/example/blackdog-demo/commit/{landed_commit}")

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

    def _seed_supervisor_recovery_fixtures(self, *, actor: str = "supervisor") -> dict[str, str]:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        run_ids = {
            "blocked": "20260313-101000-blocked01",
            "partial": "20260313-102000-partial02",
            "landed": "20260313-103000-landed03",
        }
        task_ids = {
            "blocked": "recover-blocked-task",
            "partial": "recover-partial-task",
            "landed": "recover-landed-task",
        }

        for key, run_id in run_ids.items():
            run_dir = paths.supervisor_runs_dir / run_id / task_ids[key]
            run_dir.mkdir(parents=True)
            status_file = run_dir.parent / "status.json"
            status_file.write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "actor": actor,
                        "workspace_mode": "git-worktree",
                        "poll_interval_seconds": 1.0,
                        "draining": False,
                    "run_dir": str(run_dir.parent),
                    "status_file": str(status_file),
                    "supervisor_pid": os.getpid(),
                    "steps": [
                            {
                                "index": 1,
                                "at": "2026-03-13T10:10:00-07:00",
                                "status": "complete",
                            }
                        ],
                        "completed_at": "2026-03-13T10:10:01-07:00",
                        "final_status": "finished",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            append_jsonl(
                paths.events_file,
                {
                    "event_id": f"evt-recover-start-{run_id}",
                    "type": "supervisor_run_started",
                    "at": "2026-03-13T10:10:00-07:00",
                    "actor": actor,
                    "task_id": None,
                    "payload": {
                        "run_id": run_id,
                        "workspace_mode": "git-worktree",
                        "task_ids": [task_ids[key]],
                    },
                },
            )
            append_jsonl(
                paths.events_file,
                {
                    "event_id": f"evt-recover-worktree-{run_id}",
                    "type": "worktree_start",
                    "at": "2026-03-13T10:10:01-07:00",
                    "actor": actor,
                    "task_id": task_ids[key],
                    "payload": {
                        "run_id": run_id,
                        "child_agent": f"{actor}/child-{key}",
                        "branch": f"agent/recover-{key}",
                        "target_branch": "main",
                        "worktree_path": str(run_dir.parent),
                        "primary_worktree": str(self.root),
                    },
                },
            )
            append_jsonl(
                paths.events_file,
                {
                    "event_id": f"evt-recover-launch-{run_id}",
                    "type": "child_launch",
                    "at": "2026-03-13T10:10:02-07:00",
                    "actor": actor,
                    "task_id": task_ids[key],
                    "payload": {
                        "run_id": run_id,
                        "child_agent": f"{actor}/child-{key}",
                        "workspace": str(run_dir.parent),
                        "pid": os.getpid(),
                    },
                },
            )

        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-recover-finish-blocked",
                "type": "child_finish",
                "at": "2026-03-13T10:10:03-07:00",
                "actor": actor,
                "task_id": task_ids["blocked"],
                "payload": {
                    "run_id": run_ids["blocked"],
                    "child_agent": f"{actor}/child-blocked",
                    "branch": "agent/recover-blocked",
                    "target_branch": "main",
                    "exit_code": 0,
                    "missing_process": False,
                    "final_task_status": "released",
                    "branch_ahead": True,
                    "landed": False,
                    "land_error": "dirty primary worktree contract violation: primary dirty paths.",
                },
            },
        )
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-recover-finish-partial",
                "type": "child_finish",
                "at": "2026-03-13T10:10:04-07:00",
                "actor": actor,
                "task_id": task_ids["partial"],
                "payload": {
                    "run_id": run_ids["partial"],
                    "child_agent": f"{actor}/child-partial",
                    "branch": "agent/recover-partial",
                    "target_branch": "main",
                    "exit_code": 1,
                    "missing_process": False,
                    "final_task_status": "partial",
                    "branch_ahead": False,
                    "landed": False,
                },
            },
        )
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-recover-finish-landed",
                "type": "child_finish",
                "at": "2026-03-13T10:10:05-07:00",
                "actor": actor,
                "task_id": task_ids["landed"],
                "payload": {
                    "run_id": run_ids["landed"],
                    "child_agent": f"{actor}/child-landed",
                    "branch": "agent/recover-landed",
                    "target_branch": "main",
                    "exit_code": 0,
                    "missing_process": False,
                    "final_task_status": "partial",
                    "branch_ahead": False,
                    "landed": True,
                },
            },
        )
        return {"blocked": run_ids["blocked"], "partial": run_ids["partial"], "landed": run_ids["landed"]}

    def _seed_supervisor_report_fixtures(self, *, actor: str = "supervisor") -> dict[str, Any]:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        run_ids = {
            "retry_a": "20260314-090000-retry-a",
            "retry_b": "20260314-091000-retry-b",
            "launch_fail": "20260314-092000-launch-fail",
            "land_fail": "20260314-093000-land-fail",
        }
        task_ids = {
            "retry": "report-retry-task",
            "launch_fail": "report-launch-fail-task",
            "land_fail": "report-land-fail-task",
        }

        def write_run_status(run_id: str, *, task_id: str, final_status: str) -> None:
            run_dir = paths.supervisor_runs_dir / run_id
            status_file = run_dir / "status.json"
            status_file.parent.mkdir(parents=True, exist_ok=True)
            status_file.write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "actor": actor,
                        "workspace_mode": "git-worktree",
                        "poll_interval_seconds": 1.0,
                        "draining": False,
                        "run_dir": str(run_dir),
                        "status_file": str(status_file),
                        "supervisor_pid": os.getpid(),
                        "steps": [
                            {"index": 1, "at": "2026-03-14T09:00:00-07:00", "status": "running"},
                            {"index": 2, "at": "2026-03-14T09:02:00-07:00", "status": final_status},
                        ],
                        "completed_at": "2026-03-14T09:02:00-07:00",
                        "final_status": final_status,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            append_jsonl(
                paths.events_file,
                {
                    "event_id": f"evt-report-start-{run_id}",
                    "type": "supervisor_run_started",
                    "at": "2026-03-14T09:00:00-07:00",
                    "actor": actor,
                    "task_id": None,
                    "payload": {
                        "run_id": run_id,
                        "workspace_mode": "git-worktree",
                        "task_ids": [task_id],
                    },
                },
            )

        def write_worktree_start(run_id: str, *, task_id: str) -> None:
            append_jsonl(
                paths.events_file,
                {
                    "event_id": f"evt-report-worktree-{run_id}",
                    "type": "worktree_start",
                    "at": "2026-03-14T09:00:10-07:00",
                    "actor": actor,
                    "task_id": task_id,
                    "payload": {
                        "run_id": run_id,
                        "child_agent": f"{actor}/child-{task_id}",
                        "branch": f"agent/{task_id}",
                        "target_branch": "main",
                        "workspace": str(paths.supervisor_runs_dir / run_id / task_id),
                    },
                },
            )

        write_run_status(run_ids["retry_a"], task_id=task_ids["retry"], final_status="finished")
        write_worktree_start(run_ids["retry_a"], task_id=task_ids["retry"])
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-report-launch-retry-a",
                "type": "child_launch",
                "at": "2026-03-14T09:00:20-07:00",
                "actor": actor,
                "task_id": task_ids["retry"],
                "payload": {
                    "run_id": run_ids["retry_a"],
                    "child_agent": f"{actor}/child-{task_ids['retry']}",
                    "workspace": str(paths.supervisor_runs_dir / run_ids["retry_a"] / task_ids["retry"]),
                    "pid": os.getpid(),
                },
            },
        )
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-report-finish-retry-a",
                "type": "child_finish",
                "at": "2026-03-14T09:00:30-07:00",
                "actor": actor,
                "task_id": task_ids["retry"],
                "payload": {
                    "run_id": run_ids["retry_a"],
                    "child_agent": f"{actor}/child-{task_ids['retry']}",
                    "branch": f"agent/{task_ids['retry']}",
                    "target_branch": "main",
                    "exit_code": 1,
                    "missing_process": False,
                    "final_task_status": "failed",
                    "branch_ahead": False,
                    "landed": False,
                },
            },
        )

        write_run_status(run_ids["retry_b"], task_id=task_ids["retry"], final_status="finished")
        write_worktree_start(run_ids["retry_b"], task_id=task_ids["retry"])
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-report-launch-retry-b",
                "type": "child_launch",
                "at": "2026-03-14T09:01:00-07:00",
                "actor": actor,
                "task_id": task_ids["retry"],
                "payload": {
                    "run_id": run_ids["retry_b"],
                    "child_agent": f"{actor}/child-{task_ids['retry']}-r2",
                    "workspace": str(paths.supervisor_runs_dir / run_ids["retry_b"] / task_ids["retry"]),
                    "pid": os.getpid(),
                },
            },
        )
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-report-finish-retry-b",
                "type": "child_finish",
                "at": "2026-03-14T09:01:10-07:00",
                "actor": actor,
                "task_id": task_ids["retry"],
                "payload": {
                    "run_id": run_ids["retry_b"],
                    "child_agent": f"{actor}/child-{task_ids['retry']}-r2",
                    "branch": f"agent/{task_ids['retry']}",
                    "target_branch": "main",
                    "exit_code": 0,
                    "missing_process": False,
                    "final_task_status": "done",
                    "branch_ahead": False,
                    "landed": True,
                },
            },
        )
        record_task_result(
            paths,
            task_id=task_ids["retry"],
            actor=actor,
            status="success",
            what_changed=["Completed retry pass."],
            validation=["unit"],
            residual=[],
            needs_user_input=False,
            followup_candidates=[],
            run_id=run_ids["retry_b"],
        )

        write_run_status(run_ids["launch_fail"], task_id=task_ids["launch_fail"], final_status="failed")
        write_worktree_start(run_ids["launch_fail"], task_id=task_ids["launch_fail"])
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-report-launch-failed-launch",
                "type": "child_launch_failed",
                "at": "2026-03-14T09:01:20-07:00",
                "actor": actor,
                "task_id": task_ids["launch_fail"],
                "payload": {
                    "run_id": run_ids["launch_fail"],
                    "error": "failed to execute child command",
                    "child_agent": f"{actor}/child-{task_ids['launch_fail']}",
                },
            },
        )

        write_run_status(run_ids["land_fail"], task_id=task_ids["land_fail"], final_status="finished")
        write_worktree_start(run_ids["land_fail"], task_id=task_ids["land_fail"])
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-report-launch-landfail",
                "type": "child_launch",
                "at": "2026-03-14T09:01:30-07:00",
                "actor": actor,
                "task_id": task_ids["land_fail"],
                "payload": {
                    "run_id": run_ids["land_fail"],
                    "child_agent": f"{actor}/child-{task_ids['land_fail']}",
                    "workspace": str(paths.supervisor_runs_dir / run_ids["land_fail"] / task_ids["land_fail"]),
                    "pid": os.getpid(),
                },
            },
        )
        append_jsonl(
            paths.events_file,
            {
                "event_id": "evt-report-finish-landfail",
                "type": "child_finish",
                "at": "2026-03-14T09:01:40-07:00",
                "actor": actor,
                "task_id": task_ids["land_fail"],
                "payload": {
                    "run_id": run_ids["land_fail"],
                    "child_agent": f"{actor}/child-{task_ids['land_fail']}",
                    "branch": f"agent/{task_ids['land_fail']}",
                    "target_branch": "main",
                    "exit_code": 0,
                    "missing_process": False,
                    "final_task_status": "released",
                    "branch_ahead": True,
                    "landed": False,
                    "land_error": "dirty primary worktree contract violation: primary dirty paths",
                },
            },
        )

        for run_id, task_id, with_prompt, with_stdout, with_stderr, with_metadata in [
            (run_ids["retry_a"], task_ids["retry"], True, False, False, False),
            (run_ids["retry_b"], task_ids["retry"], True, True, True, True),
            (run_ids["land_fail"], task_ids["land_fail"], True, True, True, True),
        ]:
            attempt_dir = paths.supervisor_runs_dir / run_id / task_id
            attempt_dir.mkdir(parents=True, exist_ok=True)
            if with_prompt:
                (attempt_dir / "prompt.txt").write_text("prompt", encoding="utf-8")
            if with_stdout:
                (attempt_dir / "stdout.log").write_text("stdout", encoding="utf-8")
            if with_stderr:
                (attempt_dir / "stderr.log").write_text("stderr", encoding="utf-8")
            if with_metadata:
                (attempt_dir / "metadata.json").write_text(
                    json.dumps({"prompt_hash": f"hash-{run_id}"}, indent=2), encoding="utf-8"
                )

        return {"run_ids": run_ids, "task_ids": task_ids}

    def test_supervise_report_json_output_includes_supervisor_metrics(self) -> None:
        fixtures = self._seed_supervisor_report_fixtures(actor="supervisor")
        payload = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "supervise",
                    "report",
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
        self.assertEqual(payload["summary"]["startup"]["attempts"], 4)
        self.assertEqual(payload["summary"]["startup"]["launch_failures"], 1)
        self.assertEqual(payload["summary"]["retry"]["retry_total"], 1)
        self.assertEqual(payload["summary"]["retry"]["retried_tasks"], [fixtures["task_ids"]["retry"]])
        self.assertEqual(payload["summary"]["output_shape"]["artifact_incomplete_attempts"], 2)
        self.assertEqual(payload["summary"]["landing"]["land_error_count"], 1)
        self.assertEqual(len(payload["runs"]), 4)
        self.assertIn("landing_failures", {row["category"] for row in payload["observations"]})
        self.assertIn("startup_friction", {row["category"] for row in payload["observations"]})
        limited_payload = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "supervise",
                    "report",
                    "--project-root",
                    str(self.root),
                    "--actor",
                    "supervisor",
                    "--run-limit",
                    "1",
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
        self.assertEqual(limited_payload["summary"]["runs_total"], 1)
        self.assertEqual(limited_payload["runs"][0]["run_id"], fixtures["run_ids"]["land_fail"])

    def test_supervise_report_text_output_includes_sections_and_attempts(self) -> None:
        fixtures = self._seed_supervisor_report_fixtures(actor="supervisor")
        text = subprocess.run(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "report",
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
        self.assertIn("Supervisor actor: supervisor", text)
        self.assertIn("Startup friction:", text)
        self.assertIn("Retry pressure:", text)
        self.assertIn("Output-shape consistency:", text)
        self.assertIn("Landing outcomes:", text)
        self.assertIn(fixtures["task_ids"]["retry"], text)
        self.assertIn(fixtures["task_ids"]["launch_fail"], text)
        self.assertIn(fixtures["task_ids"]["land_fail"], text)
        self.assertIn("landing errors=1", text)

    def test_supervise_recover_reports_structured_cases_in_json(self) -> None:
        fixture = self._seed_supervisor_recovery_fixtures(actor="supervisor")
        payload = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "supervise",
                    "recover",
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
        self.assertEqual(len(payload["runs"]), 3)
        cases = {row["task_id"]: row["case"] for row in payload["recoverable_cases"]}
        self.assertEqual(
            cases,
            {
                "recover-blocked-task": "blocked_by_dirty_primary",
                "recover-partial-task": "partial_run",
                "recover-landed-task": "landed_but_unfinished",
            },
        )
        self.assertEqual(
            {row["run_id"] for row in payload["recoverable_cases"]},
            {fixture["blocked"], fixture["partial"], fixture["landed"]},
        )
        for case in payload["recoverable_cases"]:
            self.assertEqual(case["severity"], "high")
            self.assertTrue(case["next_actions"])

    def test_supervise_recover_text_output(self) -> None:
        self._seed_supervisor_recovery_fixtures(actor="supervisor")
        text = subprocess.run(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "recover",
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
        self.assertIn("Supervisor actor: supervisor", text)
        self.assertIn("Recoverable cases: 3", text)
        self.assertIn("recover-blocked-task", text)
        self.assertIn("recover-partial-task", text)
        self.assertIn("recover-landed-task", text)

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
        self.assertIn('<h2>Status</h2>', updated_html)
        self.assertIn("Active Branch", updated_html)
        self.assertIn("Commit", updated_html)
        self.assertIn("Time on task", updated_html)
        self.assertIn("Last updated", updated_html)
        self.assertNotIn("Git head", updated_html)
        self.assertNotIn("Blackdog runtime", updated_html)
        self.assertIn("Blackdog Backlog", updated_html)
        self.assertIn('id="hero-meta-line"', updated_html)
        self.assertIn('id="hero-links"', updated_html)
        self.assertIn('id="queue-stats"', updated_html)
        self.assertIn('id="middle-band"', updated_html)
        self.assertIn('id="objectives-panel"', updated_html)
        self.assertIn('id="objective-intro"', updated_html)
        self.assertIn('id="objectives-table-body"', updated_html)
        self.assertIn('id="release-gates-panel"', updated_html)
        self.assertIn('id="release-gates-table-body"', updated_html)
        self.assertIn('id="execution-panel"', updated_html)
        self.assertIn('id="completed-panel"', updated_html)
        self.assertIn('id="completed-history-scroll"', updated_html)
        self.assertIn("Execution Map", updated_html)
        self.assertIn("Completed Tasks", updated_html)
        self.assertIn("Release Gates", updated_html)
        self.assertIn("const heroHighlights = snapshot.hero_highlights || {};", updated_html)
        self.assertIn('class="text-link"', updated_html)
        self.assertNotIn('id="task-search"', updated_html)
        self.assertNotIn('id="stats"', updated_html)
        self.assertNotIn("renderStats()", updated_html)
        self.assertIn('id="hero-progress"', updated_html)
        self.assertIn("function renderProgressBar(progress, className = \"\")", updated_html)
        self.assertIn("function applyProgressBars(root = document)", updated_html)
        self.assertIn("function interactiveCardAttributes(taskId)", updated_html)
        self.assertIn("function renderStatusPanel()", updated_html)
        self.assertIn("function renderObjectivesTable()", updated_html)
        self.assertIn("function renderReleaseGatesPanel()", updated_html)
        self.assertIn("function renderExecutionMap()", updated_html)
        self.assertIn("function renderCompletedPanel()", updated_html)
        self.assertIn('role="button" tabindex="0"', updated_html)
        self.assertIn('document.addEventListener("keydown", (event) => {', updated_html)
        self.assertIn('document.getElementById("hero-progress").innerHTML = renderProgressBar(overallProgress, "progress-hero");', updated_html)
        self.assertIn('document.getElementById("objective-intro").textContent =', updated_html)
        self.assertIn('const activeObjectiveRows = objectiveRows.filter((row) => Array.isArray(row.active_task_ids) && row.active_task_ids.length);', updated_html)
        self.assertIn('document.getElementById("objectives-table-body").innerHTML = activeObjectiveRows.length', updated_html)
        self.assertIn('document.getElementById("queue-stats").innerHTML = stats.map', updated_html)
        self.assertIn('document.getElementById("status-next-lines").innerHTML = lines.length', updated_html)
        self.assertIn('document.getElementById("release-gates-table-body").innerHTML = gateRows.length', updated_html)
        self.assertIn('document.getElementById("completed-history-scroll").innerHTML = grouped.length', updated_html)
        self.assertIn("function groupedCompletedObjectives(tasks)", updated_html)
        self.assertIn("completed-objective-group", updated_html)
        self.assertIn('data-progress="${escapeHtml(progress.percent)}"', updated_html)
        self.assertIn('class="progress-slot"', updated_html)
        self.assertIn(".progress-fill {", updated_html)
        self.assertIn(".progress-hero .progress-fill {", updated_html)
        self.assertIn('completedTasks.slice(0, 60)', updated_html)
        self.assertIn("width: min(1920px, calc(100vw - 36px));", updated_html)
        self.assertIn("grid-template-columns: minmax(0, 1.55fr) minmax(320px, 0.85fr);", updated_html)
        self.assertNotIn('grid-template-areas: "hero backlog history";', updated_html)
        self.assertNotIn("max-height: calc(100vh - 48px);", updated_html)
        self.assertNotIn('class="link-pill"', updated_html)
        self.assertNotIn('class="artifact-link"', updated_html)
        self.assertNotIn('<span class="pill">', updated_html)
        self.assertNotIn('target="_blank"', updated_html)
        self.assertNotIn('id="release-gates-list"', updated_html)
        self.assertNotIn('class="eyebrow"', updated_html)
        self.assertNotIn('hero-progress-label', updated_html)
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
                    protocol_command=workspace / "blackdog-child",
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
                    protocol_command=workspace / "blackdog-child",
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
        self.assertIn("`{}`".format(workspace / "blackdog-child"), prompt)

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
    protocol_cli = run_dir / "blackdog-child"
    protocol_probe = subprocess.run(
        [
            str(protocol_cli),
            "inbox",
            "list",
            "--project-root",
            str(project_root),
            "--recipient",
            actor,
            "--status",
            "open",
        ],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        check=False,
    )
    run_dir.joinpath("protocol-helper-check.txt").write_text(
        f"inbox_exit_code={protocol_probe.returncode}\\n",
        encoding="utf-8",
    )
    if protocol_probe.returncode != 0:
        return 3
    run_dir.joinpath("prompt-copy.txt").write_text(prompt, encoding="utf-8")
    Path("feature.txt").write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], check=True)
    subprocess.run(["git", "commit", "-m", f"Land {task_id} from child run"], check=True)
    helper_result = subprocess.run(
        [
            str(protocol_cli),
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
        env=os.environ.copy(),
        check=False,
    )
    return 0 if helper_result.returncode == 0 else 4


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
        self.assertFalse(payload["children"][0]["missing_process"])
        self.assertEqual(payload["children"][0]["workspace_mode"], "git-worktree")
        self.assertEqual(payload["children"][0]["launch_command"][0], str(launcher_script))
        workspace = Path(payload["children"][0]["workspace"])
        prompt_text = Path(payload["children"][0]["prompt_file"]).read_text(encoding="utf-8")
        child_run_dir = Path(payload["children"][0]["prompt_file"]).parent
        self.assertFalse(workspace.exists())
        self.assertIn("Treat committed repo state as the baseline for this task", prompt_text)
        self.assertIn("Primary-worktree landing gate:", prompt_text)
        self.assertIn(".VE is unversioned and bound to this worktree path", prompt_text)
        self.assertIn("Skip manual startup and completion steps like `blackdog worktree preflight`", prompt_text)
        self.assertIn("Prefer Blackdog CLI output over direct reads of raw state files", prompt_text)
        self.assertIn("Commit your code changes on that task branch", prompt_text)
        self.assertIn(f"Do not run `{child_run_dir / 'blackdog-child'} complete` for this task from a branch-backed child run", prompt_text)
        paths = self.runtime_paths()
        state = json.loads(paths.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][task_id]["status"], "done")
        self.assertEqual((self.root / "feature.txt").read_text(encoding="utf-8"), task_id + "\n")
        self.assertIsNotNone(payload["children"][0]["task_branch"])
        self.assertIsNone(payload["children"][0]["land_error"])
        self.assertIsNotNone(payload["children"][0]["land_result"])
        self.assertTrue((child_run_dir / "changes.diff").exists())
        self.assertTrue((child_run_dir / "changes.stat.txt").exists())
        metadata = json.loads((child_run_dir / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["task_id"], task_id)
        self.assertEqual(metadata["run_id"], payload["run_id"])
        self.assertIn("prompt_template_version", metadata)
        self.assertIn("prompt_template_hash", metadata)
        self.assertIn("prompt_hash", metadata)
        self.assertEqual(metadata["launch_command"][0], str(launcher_script))
        self.assertEqual(metadata["launch_command_strategy"], "profile")
        result_file = sorted((paths.results_dir / task_id).glob("*.json"))[-1]
        result_payload = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertIn("metadata", result_payload)
        self.assertEqual(result_payload["metadata"]["prompt_hash"], metadata["prompt_hash"])
        self.assertEqual(result_payload["metadata"]["prompt_template_version"], metadata["prompt_template_version"])
        self.assertTrue(result_file.exists())
        self.assertEqual(
            (child_run_dir / "protocol-helper-check.txt").read_text(encoding="utf-8").strip(),
            "inbox_exit_code=0",
        )
        branch_check = subprocess.run(
            ["git", "-C", str(self.root), "show-ref", "--verify", f"refs/heads/{payload['children'][0]['task_branch']}"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(branch_check.returncode, 0)
        child_finish_rows = [
            row
            for row in load_events(paths)
            if row.get("type") == "child_finish" and row.get("task_id") == task_id
        ]
        self.assertTrue(child_finish_rows)
        latest_child_finish = child_finish_rows[-1]
        self.assertEqual(latest_child_finish["payload"]["prompt_template_version"], metadata["prompt_template_version"])
        self.assertEqual(latest_child_finish["payload"]["prompt_hash"], metadata["prompt_hash"])
        self.assertEqual(latest_child_finish["payload"]["launch_command"], metadata["launch_command"])
        self.assertEqual(latest_child_finish["payload"]["launch_command_strategy"], metadata["launch_command_strategy"])
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

    def test_supervise_report_observations_reflect_missing_artifacts_in_run_bundle(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        launcher_script = install_exec_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


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
    run_dir = Path(os.environ["BLACKDOG_RUN_DIR"])
    protocol_cli = run_dir / "blackdog-child"

    Path("artifact.txt").write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", "artifact.txt"], check=True)
    subprocess.run(["git", "commit", "-m", f"Land {task_id} from child run"], check=True)

    if os.environ.get("BLACKDOG_TEST_DELETE_METADATA") == "1":
        metadata_path = run_dir / "metadata.json"
        if metadata_path.exists():
            metadata_path.unlink()
        run_dir.joinpath("metadata-deleted.txt").write_text("deleted\\n", encoding="utf-8")

    helper_result = subprocess.run(
        [
            str(protocol_cli),
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
            "fake-child-observation",
        ],
        env=os.environ.copy(),
        check=False,
    )
    return 0 if helper_result.returncode == 0 else 4


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint launcher for report artifact-observation test",
        )

        for title, lane_title in (
            ("Report complete run task", "Observation lane complete"),
            ("Report missing-artifact run task", "Observation lane incomplete"),
        ):
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--bucket",
                "integration",
                "--why",
                "Need a delegated child report that shows artifact-shape regressions.",
                "--evidence",
                "A missing run artifact should be visible in the supervisor report observations.",
                "--safe-first-slice",
                "Run one child with a full artifact bundle and one with a missing metadata artifact.",
                "--path",
                "artifact.txt",
                "--epic-title",
                "Supervisor",
                "--lane-title",
                lane_title,
                "--wave",
                "0",
            )

        complete_task_id = task_ids_by_title(self.root)["Report complete run task"]
        missing_task_id = task_ids_by_title(self.root)["Report missing-artifact run task"]

        complete_run = subprocess.run(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "run",
                "--project-root",
                str(self.root),
                "--id",
                complete_task_id,
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        self.assertEqual(complete_run.returncode, 0, complete_run.stderr)

        missing_meta_env = cli_env()
        missing_meta_env["BLACKDOG_TEST_DELETE_METADATA"] = "1"
        incomplete_run = subprocess.run(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "run",
                "--project-root",
                str(self.root),
                "--id",
                missing_task_id,
                "--format",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=missing_meta_env,
            cwd=self.root,
        )
        self.assertEqual(incomplete_run.returncode, 0, incomplete_run.stderr)

        report_payload = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "supervise",
                    "report",
                    "--project-root",
                    str(self.root),
                    "--actor",
                    "supervisor",
                    "--run-limit",
                    "2",
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
        self.assertEqual(report_payload["summary"]["output_shape"]["artifact_incomplete_attempts"], 1)
        self.assertEqual(report_payload["summary"]["runs_total"], 2)
        self.assertIn(
            "output_shape_consistency",
            {row["category"] for row in report_payload["observations"]},
        )
        attempts = {
            attempt["task_id"]: attempt for run in report_payload["runs"] for attempt in run["attempts"]
        }
        complete_attempt = attempts[complete_task_id]
        missing_attempt = attempts[missing_task_id]
        self.assertTrue(complete_attempt["artifact_complete"])
        self.assertTrue(complete_attempt["metadata_exists"])
        self.assertEqual(complete_attempt["artifact_count"], 4)
        self.assertFalse(missing_attempt["artifact_complete"])
        self.assertFalse(missing_attempt["metadata_exists"])
        self.assertEqual(missing_attempt["artifact_count"], 3)
        self.assertIn("missing artifacts", missing_attempt["output_shape_note"])

    def test_supervise_run_keeps_live_child_claimed_until_completion(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        sync_dir = paths.control_dir / "live-claim-sync"
        sync_dir.mkdir(parents=True, exist_ok=True)
        install_exec_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import subprocess
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
    project_root = Path(os.environ["BLACKDOG_PROJECT_ROOT"])
    task_id = os.environ["BLACKDOG_TASK_ID"]
    actor = os.environ["BLACKDOG_AGENT_NAME"]
    sync_dir = project_root / ".git" / "blackdog" / "live-claim-sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    started_file = sync_dir / f"started-{task_id}.txt"
    release_file = sync_dir / f"release-{task_id}.txt"
    started_file.write_text(str(os.getpid()), encoding="utf-8")
    deadline = time.time() + 10
    while time.time() < deadline and not release_file.exists():
        time.sleep(0.05)
    if not release_file.exists():
        print(f"timed out waiting for release of {task_id}", file=sys.stderr)
        return 3
    Path("live-claim.txt").write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", "live-claim.txt"], check=True)
    subprocess.run(["git", "commit", "-m", f"Commit {task_id} after live claim hold"], check=True)
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
            f"live claim child completed {task_id}",
            "--validation",
            "live-claim-child",
        ],
        check=True,
        env=os.environ.copy(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint launcher for live claim supervisor test",
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Live child task",
            "--bucket",
            "core",
            "--why",
            "Need to prove the supervisor leaves a live child claimed until it actually finishes.",
            "--evidence",
            "A live child should remain running with a non-expiring supervisor claim instead of being killed by a wall-clock deadline.",
            "--safe-first-slice",
            "Hold one child open long enough to inspect the claim state, then let it finish normally.",
            "--path",
            "README.md",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Live claims",
            "--wave",
            "0",
        )
        task_id = task_ids_by_title(self.root)["Live child task"]
        process = subprocess.Popen(
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
            started_file = wait_for_file(sync_dir / f"started-{task_id}.txt", timeout=10)
            child_pid = int(started_file.read_text(encoding="utf-8").strip())
            time.sleep(2)

            state = json.loads(paths.state_file.read_text(encoding="utf-8"))
            claim_entry = state["task_claims"][task_id]
            self.assertEqual(claim_entry["status"], "claimed")
            self.assertNotIn("claim_expires_at", claim_entry)
            self.assertEqual(claim_entry["claimed_pid"], child_pid)

            refreshed_snapshot = wait_for_html_snapshot(
                paths.html_file,
                lambda payload: any(
                    row.get("id") == task_id and row.get("latest_run_status") == "running"
                    for row in payload.get("active_tasks", [])
                ),
                timeout=10,
            )
            self.assertIn(task_id, [row["id"] for row in refreshed_snapshot["active_tasks"]])
            os.kill(child_pid, 0)

            (sync_dir / f"release-{task_id}.txt").write_text("release\n", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)

        payload = json.loads(stdout)
        child = payload["children"][0]
        self.assertEqual(child["task_id"], task_id)
        self.assertFalse(child["missing_process"])
        self.assertEqual(child["exit_code"], 0)
        self.assertEqual(child["final_task_status"], "done")
        self.assertIsNotNone(child["land_result"])
        self.assertIsNone(child["land_error"])
        self.assertFalse(Path(child["workspace"]).exists())

        state = json.loads(paths.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][task_id]["status"], "done")

    def test_supervise_run_releases_orphaned_claim_after_two_liveness_scans(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Orphaned claimed task",
            "--bucket",
            "core",
            "--why",
            "Need to prove the supervisor recovers a claim whose reported process is gone.",
            "--evidence",
            "A claimed task with a missing reported pid should survive one scan and be released on the second successive scan.",
            "--safe-first-slice",
            "Seed one claimed task with a fake pid, run two supervisor scans, and check that the second run releases it.",
            "--path",
            "README.md",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Live claims",
            "--wave",
            "0",
        )
        task_id = task_ids_by_title(self.root)["Orphaned claimed task"]
        save_state(
            paths.state_file,
            {
                "schema_version": 1,
                "approval_tasks": {},
                "task_claims": {
                    task_id: {
                        "status": "claimed",
                        "title": "Orphaned claimed task",
                        "claimed_by": "agent/a",
                        "claimed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "claimed_pid": 999999,
                        "claimed_process_missing_scans": 0,
                        "bucket": "core",
                        "paths": ["README.md"],
                        "priority": "P1",
                        "risk": "medium",
                    }
                },
            },
        )

        first = subprocess.run(
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
            check=False,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        first_state = json.loads(paths.state_file.read_text(encoding="utf-8"))
        first_claim = first_state["task_claims"][task_id]
        self.assertEqual(first_claim["status"], "claimed")
        self.assertEqual(first_claim["claimed_process_missing_scans"], 1)

        send_message(
            paths,
            sender="tester",
            recipient="supervisor",
            body="stop",
            kind="control",
            tags=["stop"],
        )
        second = subprocess.run(
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
            check=False,
            capture_output=True,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        second_payload = json.loads(second.stdout)
        self.assertEqual(second_payload["final_status"], "stopped")
        self.assertEqual(second_payload["children"], [])

        released_state = json.loads(paths.state_file.read_text(encoding="utf-8"))
        released_claim = released_state["task_claims"][task_id]
        self.assertEqual(released_claim["status"], "released")
        self.assertIn("missing in 2 successive liveness scans", released_claim["release_note"])
        self.assertNotIn("claimed_pid", released_claim)

        release_events = [
            row
            for row in load_events(paths, limit=20)
            if row.get("task_id") == task_id and row.get("type") == "release"
        ]
        self.assertTrue(release_events)
        self.assertIn("missing in 2 successive liveness scans", release_events[0]["payload"]["note"])

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
        self.assertFalse(child["missing_process"])
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
        install_blocking_dirty_primary_launcher(
            self.root,
            commit_message="Checkpoint dirty primary launcher for supervisor contract-violation test",
            primary_mode="unrelated",
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
        self.assertIs(payload["children"][0]["branch_ahead"], True)
        self.assertIs(payload["children"][0]["landed"], False)
        self.assertIn("dirty primary worktree contract violation", payload["children"][0]["land_error"])
        self.assertEqual(payload["recovery_actions"], [])
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
        self.assertFalse(blocked_results[0]["needs_user_input"])
        self.assertEqual(blocked_results[0]["followup_candidates"], [])
        task = next(row for row in build_ui_snapshot(load_profile(self.root))["tasks"] if row["id"] == task_id)
        self.assertEqual(task["operator_status"], "Failed to land")
        self.assertEqual(task["operator_status_key"], "blocked")
        self.assertEqual(task["latest_run_status"], "blocked")
        self.assertTrue(task["latest_run_branch_ahead"])
        self.assertFalse(task["latest_run_landed"])
        self.assertIn("dirty primary worktree contract violation", task["latest_run_land_error"])
        self.assertIn("failed-to-land", [row["key"] for row in task["card_status_chips"]])

    def test_supervise_run_recovers_by_stashing_and_landing_before_new_child(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        install_blocking_dirty_primary_launcher(
            self.root,
            commit_message="Checkpoint dirty primary recovery launcher for stash-before-launch test",
            primary_mode="unrelated",
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Blocked dirty-primary task",
            "--bucket",
            "integration",
            "--why",
            "A previously blocked branch should be recovered before new work launches.",
            "--evidence",
            "Launching another child while the primary checkout is dirty compounds the WTAM blockage.",
            "--safe-first-slice",
            "Block one landed child on dirty primary state, then recover it before a second child launch.",
            "--path",
            "dirty-landed.txt",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Recovery lane",
            "--wave",
            "0",
        )
        blocked_task_id = task_ids_by_title(self.root)["Blocked dirty-primary task"]
        first_payload = json.loads(
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
                    blocked_task_id,
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
        self.assertIn("dirty primary worktree contract violation", first_payload["children"][0]["land_error"])
        self.assertEqual(first_payload["recovery_actions"], [])

        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Fresh runnable task",
            "--bucket",
            "integration",
            "--why",
            "The supervisor should resume normal launches after recovering blocked state.",
            "--evidence",
            "The recovery gate should clear stale blocked state before new work begins.",
            "--safe-first-slice",
            "Launch a second child only after the blocked first child has been recovered.",
            "--path",
            "dirty-landed.txt",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Recovery lane",
            "--wave",
            "0",
        )
        next_task_id = task_ids_by_title(self.root)["Fresh runnable task"]
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
                    next_task_id,
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

        self.assertEqual([row["action"] for row in payload["recovery_actions"]], ["stash", "land"])
        self.assertEqual(len(payload["children"]), 1)
        self.assertEqual(payload["children"][0]["task_id"], next_task_id)
        self.assertEqual((self.root / "dirty-landed.txt").read_text(encoding="utf-8").strip(), next_task_id)
        self.assertFalse((self.root / "dirty.txt").exists())

        stash_list = subprocess.run(
            ["git", "-C", str(self.root), "stash", "list"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("blackdog recovery", stash_list)

        followup_title = f"Resolve supervisor recovery stash for {blocked_task_id}"
        self.assertIn(followup_title, task_ids_by_title(self.root))

        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][blocked_task_id]["status"], "done")
        self.assertEqual(state["task_claims"][next_task_id]["status"], "done")

        blocked_results = [row for row in load_task_results(self.runtime_paths(), task_id=blocked_task_id) if row["actor"] == "supervisor"]
        self.assertTrue(any(row["status"] == "success" for row in blocked_results))

    def test_supervise_run_recovers_matching_dirty_primary_by_commit(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        install_blocking_dirty_primary_launcher(
            self.root,
            commit_message="Checkpoint dirty primary recovery launcher for commit-before-launch test",
            primary_mode="matching",
        )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Commit-recover blocked task",
            "--bucket",
            "integration",
            "--why",
            "A matching primary dirty state should be committed as the recovered landing.",
            "--evidence",
            "If the primary checkout already matches the blocked branch tree, a recovery commit is safer than stashing.",
            "--safe-first-slice",
            "Block one child on matching dirty primary content, then recover it by committing the primary checkout.",
            "--path",
            "dirty-landed.txt",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Recovery lane",
            "--wave",
            "0",
        )
        blocked_task_id = task_ids_by_title(self.root)["Commit-recover blocked task"]
        first_payload = json.loads(
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
                    blocked_task_id,
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
        self.assertIn("dirty primary worktree contract violation", first_payload["children"][0]["land_error"])
        self.assertEqual(first_payload["recovery_actions"], [])

        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Post-recovery runnable task",
            "--bucket",
            "integration",
            "--why",
            "The supervisor should continue launching work after a recovery commit.",
            "--evidence",
            "A matched dirty-primary recovery should not leave the queue blocked.",
            "--safe-first-slice",
            "Commit the matching recovery state, then launch a fresh child task.",
            "--path",
            "dirty-landed.txt",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Recovery lane",
            "--wave",
            "0",
        )
        next_task_id = task_ids_by_title(self.root)["Post-recovery runnable task"]
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
                    next_task_id,
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

        self.assertEqual([row["action"] for row in payload["recovery_actions"]], ["commit"])
        self.assertEqual(payload["children"][0]["task_id"], next_task_id)
        self.assertEqual((self.root / "dirty-landed.txt").read_text(encoding="utf-8").strip(), next_task_id)

        stash_list = subprocess.run(
            ["git", "-C", str(self.root), "stash", "list"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(stash_list.strip(), "")

        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][blocked_task_id]["status"], "done")
        self.assertEqual(state["task_claims"][next_task_id]["status"], "done")

        recovery_log = subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "log",
                "--format=%s",
                "--grep",
                f"Recover {blocked_task_id} from dirty primary landing state",
                "-n",
                "1",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn(f"Recover {blocked_task_id} from dirty primary landing state", recovery_log)

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
        rendered_snapshot = html_snapshot(self.runtime_paths().html_file)
        self.assertEqual(rendered_snapshot["open_messages"], [])
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

    def test_supervise_run_selects_best_convergence_candidate_via_model_backed_children(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        install_model_backed_supervisor_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def record_result(
    *,
    project_root: Path,
    task_id: str,
    actor: str,
    status: str,
    what_changed: list[str],
    validation: list[str],
) -> None:
    command = [
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
        status,
    ]
    for item in what_changed:
        command.extend(["--what-changed", item])
    for item in validation:
        command.extend(["--validation", item])
    subprocess.run(command, check=True, env=os.environ.copy())


def parse_task_title(prompt_text: str) -> str:
    match = re.search(r"^\s*Title:\s*(.*)$", prompt_text, flags=re.M)
    if match:
        return match.group(1).strip()
    return ""


def candidate_score(title: str) -> int:
    match = re.search(r"Candidate (\d+)", title)
    if not match:
        return 0
    order = int(match.group(1))
    return [61, 72, 95, 66][order - 1]


def wait_for_candidates(run_dir: Path, *, target_count: int, timeout_seconds: float) -> list[dict[str, int]]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        candidates: list[dict[str, int]] = []
        for sibling in sorted(run_dir.parent.iterdir()):
            if not sibling.is_dir() or sibling == run_dir:
                continue
            payload_path = sibling / "candidate.json"
            if not payload_path.exists():
                continue
            try:
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.05)
                continue
            if "task_id" not in payload or payload.get("score") is None:
                continue
            candidates.append(payload)
        if len(candidates) >= target_count:
            return candidates
        time.sleep(0.05)
    return []


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
    run_dir = Path(os.environ["BLACKDOG_RUN_DIR"])
    prompt_text = Path(os.environ["BLACKDOG_PROMPT_FILE"]).read_text(encoding="utf-8")
    title = parse_task_title(prompt_text)
    workspace = Path(os.environ["BLACKDOG_WORKSPACE"])

    if "evaluator" in title.lower():
        candidates = wait_for_candidates(run_dir, target_count=4, timeout_seconds=12.0)
        if len(candidates) < 4:
            print(f"timed out waiting for 4 candidates; got {len(candidates)}", file=sys.stderr)
            return 3
        winner = max(candidates, key=lambda item: (int(item["score"]), str(item["task_id"])))
        (run_dir / "winner.json").write_text(
            json.dumps(
                {
                    "winner_task_id": winner["task_id"],
                    "winner_score": int(winner["score"]),
                },
                sort_keys=True,
            )
            + "\\n",
            encoding="utf-8",
        )
        report = workspace / "convergence-winner.txt"
        report.write_text(f'winner={winner["task_id"]} score={winner["score"]}\\n', encoding="utf-8")
        subprocess.run(["git", "add", report.name], check=True)
        subprocess.run(["git", "commit", "-m", f"Record convergence winner for {task_id}"], check=True)
        record_result(
            project_root=project_root,
            task_id=task_id,
            actor=actor,
            status="success",
            what_changed=[f"Selected candidate winner {winner['task_id']}"],
            validation=[
                "alignment-evaluator",
                f"winner={winner['task_id']}",
                f"winner-score={winner['score']}",
            ],
        )
        return 0

    score = candidate_score(title)
    candidate_file = workspace / f"{task_id}-implementation.py"
    candidate_file.write_text(f"SCORE = {score}\\n", encoding="utf-8")
    subprocess.run(["git", "add", candidate_file.name], check=True)
    subprocess.run(["git", "commit", "-m", f"Implement convergence candidate for {task_id}"], check=True)
    (run_dir / "candidate.json").write_text(
        json.dumps({"task_id": task_id, "score": score}, sort_keys=True) + "\\n",
        encoding="utf-8",
    )
    record_result(
        project_root=project_root,
        task_id=task_id,
        actor=actor,
        status="success",
        what_changed=[f"Implemented candidate for {task_id}"],
        validation=[
            "alignment-candidate",
            f"candidate-score={score}",
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint model-backed convergence launcher",
            use_low_effort=True,
        )

        implementation_titles = [
            "MSA Candidate 1",
            "MSA Candidate 2",
            "MSA Candidate 3",
            "MSA Candidate 4",
        ]
        evaluator_title = "MSA Convergence Evaluator"
        for index, title in enumerate(implementation_titles, start=1):
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--bucket",
                "core",
                "--why",
                f"Produce implementation candidate #{index} for a convergence task.",
                "--evidence",
                "Each implementation should emit a score for evaluator comparison.",
                "--safe-first-slice",
                "Produce an implementation candidate and record the alignment score result.",
                "--path",
                f"src/blackdog/msa_candidate_{index}.py",
                "--epic-title",
                "Release Convergence",
                "--lane-title",
                f"Candidate Lane {index}",
                "--wave",
                "0",
            )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            evaluator_title,
            "--bucket",
            "core",
            "--why",
            "Collect candidate implementations and select the highest scored entry.",
            "--evidence",
            "Evaluator should record the winner metadata and mark task completion.",
            "--safe-first-slice",
            "Wait until all candidates are available, select winner, and record convergence result.",
            "--path",
            "src/blackdog/msa_convergence_evaluator.py",
            "--epic-title",
            "Release Convergence",
            "--lane-title",
            "Evaluator Lane",
            "--wave",
            "0",
        )

        task_ids = task_ids_by_title(self.root)
        implementation_ids = [task_ids[title] for title in implementation_titles]
        evaluator_id = task_ids[evaluator_title]
        all_task_ids = set(implementation_ids + [evaluator_id])

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
                "5",
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
            stdout, stderr = process.communicate(timeout=25)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)

        payload = json.loads(stdout)
        self.assertEqual(payload["final_status"], "idle")
        self.assertEqual(set(child["task_id"] for child in payload["children"]), all_task_ids)
        self.assertTrue(any("--effort" in child["launch_command"] for child in payload["children"]))

        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        for task_id in all_task_ids:
            self.assertEqual(state["task_claims"][task_id]["status"], "done")

        events = load_events(self.runtime_paths())
        for task_id in all_task_ids:
            self.assertTrue(any(row["type"] == "task_result" and row["task_id"] == task_id for row in events))
            self.assertTrue(any(row["type"] == "child_finish" and row["task_id"] == task_id for row in events))

        results = [row for row in load_task_results(self.runtime_paths()) if row["task_id"] in all_task_ids]
        candidate_rows = [row for row in results if row["task_id"] in implementation_ids]
        evaluator_rows = [row for row in results if row["task_id"] == evaluator_id]
        self.assertEqual(len(candidate_rows), len(implementation_ids))
        self.assertEqual(len(evaluator_rows), 1)
        evaluator_result = evaluator_rows[0]
        self.assertEqual(evaluator_result["status"], "success")

        candidate_scores: dict[str, int] = {}
        for row in candidate_rows:
            score_entries = [entry for entry in row["validation"] if str(entry).startswith("candidate-score=")]
            self.assertEqual(len(score_entries), 1)
            candidate_scores[row["task_id"]] = int(score_entries[0].split("=", 1)[1])
        winner_id = max(candidate_scores, key=candidate_scores.get)
        winner_score = candidate_scores[winner_id]

        result_winner_entry = [
            entry for entry in evaluator_result["validation"] if str(entry).startswith("winner=")
        ][0]
        result_winner_score = [
            entry for entry in evaluator_result["validation"] if str(entry).startswith("winner-score=")
        ][0]
        self.assertEqual(result_winner_entry.split("=", 1)[1], winner_id)
        self.assertEqual(int(result_winner_score.split("=", 1)[1]), winner_score)

        snapshot = html_snapshot(self.runtime_paths().html_file)
        board_task_ids = {row["id"] for row in snapshot["board_tasks"]}
        self.assertEqual(board_task_ids, all_task_ids)
        snapshot_tasks = {row["id"]: row for row in snapshot["tasks"]}
        for task_id in all_task_ids:
            self.assertEqual(snapshot_tasks[task_id]["operator_status"], "Complete")
            self.assertEqual(snapshot_tasks[task_id]["latest_result_status"], "success")

    def test_supervise_run_performs_a_second_round_convergence_optimization_funnel(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")

        def parse_winner_report(path: Path) -> tuple[str, int]:
            match = re.search(r"winner=(?P<task_id>\S+) score=(?P<score>\d+)", path.read_text(encoding="utf-8"))
            if match is None:
                raise AssertionError(f"Could not parse winner report from {path}")
            return match.group("task_id"), int(match.group("score"))

        install_model_backed_supervisor_launcher(
            self.root,
            """
#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def record_result(
    *,
    project_root: Path,
    task_id: str,
    actor: str,
    status: str,
    what_changed: list[str],
    validation: list[str],
) -> None:
    command = [
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
        status,
    ]
    for item in what_changed:
        command.extend(["--what-changed", item])
    for item in validation:
        command.extend(["--validation", item])
    subprocess.run(command, check=True, env=os.environ.copy())


def parse_task_title(prompt_text: str) -> str:
    match = re.search(r"^\s*Title:\s*(.*)$", prompt_text, flags=re.M)
    if match:
        return match.group(1).strip()
    return ""


def candidate_score(title: str) -> int:
    match = re.search(r"Candidate (\d+)", title)
    if not match:
        return 0
    order = int(match.group(1))
    return [61, 72, 95, 66][order - 1]


def optimization_score(title: str) -> int:
    match = re.search(r"Optimization (\d+)", title)
    if not match:
        return 0
    order = int(match.group(1))
    return [18, 23][order - 1]


def read_round1_winner(project_root: Path) -> tuple[str, int]:
    payload = (project_root / "convergence-round1-winner.txt").read_text(encoding="utf-8")
    match = re.search(r"winner=(\S+) score=(\d+)", payload)
    if not match:
        raise RuntimeError("round1 winner report missing or malformed")
    return match.group(1), int(match.group(2))


def wait_for_round2_optimizations(run_root: Path, timeout_seconds: float) -> list[dict[str, int]]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        outputs: list[dict[str, int]] = []
        for path in sorted(run_root.glob("optimization-*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.05)
                continue
            if "task_id" not in payload or "optimization_score" not in payload:
                time.sleep(0.05)
                continue
            outputs.append(payload)
        if len(outputs) >= 2:
            return outputs
        time.sleep(0.05)
    return []


def wait_for_candidates(run_dir: Path, timeout_seconds: float) -> list[dict[str, int]]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        candidates: list[dict[str, int]] = []
        for sibling in sorted(run_dir.parent.iterdir()):
            if not sibling.is_dir() or sibling == run_dir:
                continue
            payload_path = sibling / "candidate.json"
            if not payload_path.exists():
                continue
            try:
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                time.sleep(0.05)
                continue
            if "task_id" not in payload or payload.get("score") is None:
                continue
            candidates.append(payload)
        if len(candidates) >= 4:
            return candidates
        time.sleep(0.05)
    return []


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
    run_dir = Path(os.environ["BLACKDOG_RUN_DIR"])
    prompt_text = Path(os.environ["BLACKDOG_PROMPT_FILE"]).read_text(encoding="utf-8")
    title = parse_task_title(prompt_text)
    workspace = Path(os.environ["BLACKDOG_WORKSPACE"])

    if "evaluator" in title.lower():
        candidates = wait_for_candidates(run_dir, timeout_seconds=12.0)
        if len(candidates) < 4:
            print(f"timed out waiting for 4 candidates; got {len(candidates)}", file=sys.stderr)
            return 3
        winner = max(candidates, key=lambda item: (int(item["score"]), str(item["task_id"])))
        (run_dir / "winner.json").write_text(
            json.dumps(
                {
                    "winner_task_id": winner["task_id"],
                    "winner_score": int(winner["score"]),
                },
                sort_keys=True,
            )
            + "\\n",
            encoding="utf-8",
        )
        report = workspace / "convergence-round1-winner.txt"
        report.write_text(f"winner={winner['task_id']} score={winner['score']}\\n", encoding="utf-8")
        subprocess.run(["git", "add", report.name], check=True)
        subprocess.run(["git", "commit", "-m", f"Record convergence winner for {task_id}"], check=True)
        record_result(
            project_root=project_root,
            task_id=task_id,
            actor=actor,
            status="success",
            what_changed=[f"Selected candidate winner {winner['task_id']}"],
            validation=[
                "alignment-evaluator",
                f"winner={winner['task_id']}",
                f"winner-score={winner['score']}",
            ],
        )
        return 0

    if "optimization pass" in title.lower():
        winner_task_id, winner_score = read_round1_winner(project_root)
        score = winner_score + optimization_score(title)
        optimization_source = workspace / f"{task_id}-optimization.py"
        optimization_source.write_text(
            f"ROUND1_WINNER = {winner_task_id!r}\\nOPTIMIZATION_SCORE = {score}\\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", optimization_source.name], check=True)
        subprocess.run(["git", "commit", "-m", f"Implement optimization pass for {task_id}"], check=True)
        optimization_file = run_dir.parent / f"optimization-{task_id}.json"
        optimization_file.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "round1_winner_task_id": winner_task_id,
                    "optimization_score": score,
                    "score_source": winner_score,
                },
                sort_keys=True,
            )
            + "\\n",
            encoding="utf-8",
        )
        record_result(
            project_root=project_root,
            task_id=task_id,
            actor=actor,
            status="success",
            what_changed=[f"Optimized from winner {winner_task_id}"],
            validation=[
                "alignment-optimizer",
                f"round1-winner={winner_task_id}",
                f"optimization-score={score}",
                f"score-source={winner_score}",
            ],
        )
        return 0

    if "final convergence chooser" in title.lower():
        runner = run_dir.parent
        outputs = wait_for_round2_optimizations(runner, timeout_seconds=12.0)
        if len(outputs) < 2:
            print(f"timed out waiting for 2 optimization outputs; got {len(outputs)}", file=sys.stderr)
            return 3
        winner = max(outputs, key=lambda item: (int(item.get("optimization_score", 0)), str(item.get("task_id", ""))))
        winner_task_id = winner["task_id"]
        winner_score = int(winner.get("optimization_score", 0))
        source_task_id = winner.get("round1_winner_task_id", "")
        report = workspace / "convergence-round2-winner.txt"
        report.write_text(
            f"winner={winner_task_id} score={winner_score} source-winner={source_task_id}\\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", report.name], check=True)
        subprocess.run(["git", "commit", "-m", f"Record convergence final winner for {task_id}"], check=True)
        record_result(
            project_root=project_root,
            task_id=task_id,
            actor=actor,
            status="success",
            what_changed=[f"Selected optimized winner {winner_task_id}"],
            validation=[
                "alignment-final-chooser",
                f"winner={winner_task_id}",
                f"winner-score={winner_score}",
                f"source-winner={source_task_id}",
            ],
        )
        return 0

    score = candidate_score(title)
    candidate_file = workspace / f"{task_id}-implementation.py"
    candidate_file.write_text(f"SCORE = {score}\\n", encoding="utf-8")
    subprocess.run(["git", "add", candidate_file.name], check=True)
    subprocess.run(["git", "commit", "-m", f"Implement convergence candidate for {task_id}"], check=True)
    (run_dir / "candidate.json").write_text(json.dumps({"task_id": task_id, "score": score}, sort_keys=True) + "\\n", encoding="utf-8")
    record_result(
        project_root=project_root,
        task_id=task_id,
        actor=actor,
        status="success",
        what_changed=[f"Implemented candidate for {task_id}"],
        validation=[
            "alignment-candidate",
            f"candidate-score={score}",
        ],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""",
            commit_message="Checkpoint model-backed convergence funnel launcher",
            use_low_effort=True,
        )

        implementation_titles = [
            "MSA Candidate 1",
            "MSA Candidate 2",
            "MSA Candidate 3",
            "MSA Candidate 4",
        ]
        evaluator_title = "MSA Convergence Evaluator"
        for index, title in enumerate(implementation_titles, start=1):
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--bucket",
                "core",
                "--why",
                f"Produce implementation candidate #{index} for a convergence task.",
                "--evidence",
                "Each implementation should emit a score for evaluator comparison.",
                "--safe-first-slice",
                "Produce an implementation candidate and record the alignment score result.",
                "--path",
                f"src/blackdog/msa_candidate_{index}.py",
                "--epic-title",
                "Release Convergence",
                "--lane-title",
                f"Candidate Lane {index}",
                "--wave",
                "0",
            )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            evaluator_title,
            "--bucket",
            "core",
            "--why",
            "Collect candidate implementations and select the highest scored entry.",
            "--evidence",
            "Evaluator should record the winner metadata and mark task completion.",
            "--safe-first-slice",
            "Wait until all candidates are available, select winner, and record convergence result.",
            "--path",
            "src/blackdog/msa_convergence_evaluator.py",
            "--epic-title",
            "Release Convergence",
            "--lane-title",
            "Evaluator Lane",
            "--wave",
            "0",
        )

        round1_task_ids_by_title = task_ids_by_title(self.root)
        round1_task_ids = set(round1_task_ids_by_title.values())
        round1_evaluator_id = round1_task_ids_by_title[evaluator_title]

        first_round_process = subprocess.run(
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
        )
        first_round_payload = json.loads(first_round_process.stdout)
        self.assertEqual(first_round_payload["final_status"], "idle")
        self.assertEqual(set(child["task_id"] for child in first_round_payload["children"]), set(round1_task_ids))
        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        for task_id in round1_task_ids:
            if task_id == round1_evaluator_id:
                self.assertIn(state["task_claims"][task_id]["status"], {"done", "released"})
            else:
                self.assertEqual(state["task_claims"][task_id]["status"], "done")
        first_round_winner_id = parse_winner_report(self.root / "convergence-round1-winner.txt")[0]
        self.assertIn(first_round_winner_id, round1_task_ids)

        optimization_titles = [
            "MSA Optimization Pass 1",
            "MSA Optimization Pass 2",
        ]
        chooser_title = "MSA Final Convergence Chooser"
        for index, title in enumerate(optimization_titles, start=1):
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--bucket",
                "core",
                "--why",
                f"Optimize the winner from a convergence round, pass #{index}.",
                "--evidence",
                "Each optimization should raise the score for final selection.",
                "--safe-first-slice",
                "Optimize the first-round winner implementation.",
                "--path",
                f"src/blackdog/msa_round2_optimization_{index}.py",
                "--epic-title",
                "Release Convergence",
                "--lane-title",
                f"Optimization Lane {index}",
                "--wave",
                "1",
            )
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            chooser_title,
            "--bucket",
            "core",
            "--why",
            "Compare optimization outputs and mark the best downstream result.",
            "--evidence",
            "Chooser should commit the final winner artifact from optimized outputs.",
            "--safe-first-slice",
            "Wait for both optimizers and select the highest-scoring output.",
            "--path",
            "src/blackdog/msa_final_convergence_chooser.py",
            "--epic-title",
            "Release Convergence",
            "--lane-title",
            "Optimization Chooser Lane",
            "--wave",
            "2",
        )

        all_task_ids = task_ids_by_title(self.root)
        optimization_ids = [all_task_ids[title] for title in optimization_titles]
        final_chooser_id = all_task_ids[chooser_title]
        second_round_task_ids = optimization_ids + [final_chooser_id]

        second_round_process = subprocess.run(
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
        )
        second_round_payload = json.loads(second_round_process.stdout)
        self.assertEqual(second_round_payload["final_status"], "idle")
        self.assertEqual(set(child["task_id"] for child in second_round_payload["children"]), set(second_round_task_ids))
        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        for task_id in second_round_task_ids:
            self.assertEqual(state["task_claims"][task_id]["status"], "done")
        for task_id in round1_task_ids:
            self.assertEqual(state["task_claims"][task_id]["status"], "done")

        events = load_events(self.runtime_paths())
        sweep_rows = [row for row in events if row["type"] == "supervisor_run_sweep"]
        self.assertEqual(len(sweep_rows), 1)
        sweep_payload = sweep_rows[0]["payload"]
        self.assertEqual(set(sweep_payload["removed_task_ids"]), set(round1_task_ids))
        self.assertEqual(sweep_payload["wave_map"], {"1": 0, "2": 1})
        for task_id in round1_task_ids | set(second_round_task_ids):
            self.assertTrue(any(row["type"] == "task_result" and row["task_id"] == task_id for row in events))
            self.assertTrue(any(row["type"] == "child_finish" and row["task_id"] == task_id for row in events))

        plan_snapshot = load_backlog(load_profile(self.root).paths, load_profile(self.root))
        plan_task_ids = sorted(task_id for lane in plan_snapshot.plan["lanes"] for task_id in lane["task_ids"])
        self.assertEqual(set(plan_task_ids), set(second_round_task_ids))
        self.assertEqual(sorted({int(lane["wave"]) for lane in plan_snapshot.plan["lanes"]}), [0, 1])

        results = [row for row in load_task_results(self.runtime_paths()) if row["task_id"] in round1_task_ids | set(second_round_task_ids)]
        self.assertEqual(len(results), len(round1_task_ids) + len(second_round_task_ids))
        result_by_id = {row["task_id"]: row for row in results}
        for task_id in round1_task_ids | set(second_round_task_ids):
            row = result_by_id[task_id]
            self.assertEqual(row["status"], "success")

        optimization_scores: dict[str, int] = {}
        for task_id in optimization_ids:
            score_entries = [entry for entry in result_by_id[task_id]["validation"] if str(entry).startswith("optimization-score=")]
            self.assertEqual(len(score_entries), 1)
            optimization_scores[task_id] = int(score_entries[0].split("=", 1)[1])
        expected_final_winner = max(
            optimization_scores,
            key=lambda key: (optimization_scores[key], key),
        )
        final_chooser_result = result_by_id[final_chooser_id]
        final_winner_entries = [
            entry for entry in final_chooser_result["validation"] if str(entry).startswith("winner=")
        ]
        final_winner_score_entries = [
            entry for entry in final_chooser_result["validation"] if str(entry).startswith("winner-score=")
        ]
        self.assertEqual(len(final_winner_entries), 1)
        self.assertEqual(len(final_winner_score_entries), 1)

        final_winner_report = parse_winner_report(self.root / "convergence-round2-winner.txt")
        final_winner_id = final_winner_report[0]
        final_winner_score = int(final_winner_report[1])
        self.assertEqual(final_winner_id, expected_final_winner)
        self.assertEqual(final_winner_score, optimization_scores[expected_final_winner])
        self.assertEqual(final_winner_entries[0], f"winner={final_winner_id}")
        self.assertEqual(int(final_winner_score_entries[0].split("=", 1)[1]), final_winner_score)

        source_winner_entries = [
            entry for entry in final_chooser_result["validation"] if str(entry).startswith("source-winner=")
        ]
        self.assertEqual(len(source_winner_entries), 1)
        self.assertEqual(source_winner_entries[0], f"source-winner={first_round_winner_id}")

        snapshot = html_snapshot(self.runtime_paths().html_file)
        board_task_ids = {row["id"] for row in snapshot["board_tasks"]}
        self.assertEqual(board_task_ids, set(second_round_task_ids))
        snapshot_tasks = {row["id"]: row for row in snapshot["tasks"]}
        for task_id in round1_task_ids | set(second_round_task_ids):
            self.assertEqual(snapshot_tasks[task_id]["operator_status"], "Complete")
            self.assertEqual(snapshot_tasks[task_id]["latest_result_status"], "success")
