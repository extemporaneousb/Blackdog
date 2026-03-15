from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from blackdog.backlog import load_backlog
from blackdog.cli import main as blackdog_main
from blackdog.config import load_profile, render_default_profile
from blackdog.skill_cli import main as blackdog_skill_main
from blackdog.store import append_jsonl, record_task_result, send_message
from blackdog.supervisor import _build_child_prompt, _resolved_launch_command


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


def read_sse_event(stream) -> dict[str, object]:
    event_name = ""
    event_id = ""
    data_lines: list[str] = []
    while True:
        raw = stream.readline()
        if not raw:
            raise AssertionError("SSE stream closed before an event was received")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data_lines:
                return {
                    "event": event_name,
                    "id": event_id,
                    "data": json.loads("\n".join(data_lines)),
                }
            continue
        if line.startswith("id: "):
            event_id = line[4:]
        elif line.startswith("event: "):
            event_name = line[7:]
        elif line.startswith("data: "):
            data_lines.append(line[6:])


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
            "Blackdog should expose a WTAM-style start entrypoint.",
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

    def test_ui_snapshot_reports_graph_and_loop_contract(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        paths = self.runtime_paths()
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
        loop_dir = paths.supervisor_runs_dir / "20260313-120000-loop-abcd1234"
        loop_dir.mkdir(parents=True)
        (loop_dir / "status.json").write_text(
            json.dumps(
                {
                    "loop_id": "abcd1234",
                    "actor": "supervisor",
                    "workspace_mode": "git-worktree",
                    "cycles": [{"index": 1, "status": "paused"}],
                    "completed_at": "2026-03-13T12:00:05-07:00",
                    "final_status": "paused",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        snapshot = json.loads(
            subprocess.run(
                [sys.executable, "-m", "blackdog.cli", "ui", "snapshot", "--project-root", str(self.root)],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )

        task_ids = {task["title"]: task["id"] for task in snapshot["graph"]["tasks"]}
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(Path(snapshot["project_root"]).resolve(), self.root.resolve())
        self.assertEqual(Path(snapshot["control_dir"]).resolve(), self.runtime_paths().control_dir.resolve())
        self.assertEqual(snapshot["graph"]["edges"], [{"from": task_ids["UI slice one"], "to": task_ids["UI slice two"]}])
        self.assertEqual(snapshot["supervisor"]["loops"][0]["loop_id"], "abcd1234")
        self.assertEqual(snapshot["links"]["backlog"], "/artifacts/backlog.md")

    def test_ui_snapshot_exposes_active_tasks_filters_messages_and_interrupts_empty_runs(self) -> None:
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
                "The live UI should separate activity, inbox controls, and result history.",
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
                [sys.executable, "-m", "blackdog.cli", "ui", "snapshot", "--project-root", str(self.root)],
                check=True,
                capture_output=True,
                text=True,
                env=cli_env(),
                cwd=self.root,
            ).stdout
        )

        graph_tasks = {task["id"]: task for task in snapshot["graph"]["tasks"]}
        active_runs = {run["run_id"]: run for run in snapshot["supervisor"]["active_runs"]}
        recent_runs = {run["run_id"]: run for run in snapshot["supervisor"]["recent_runs"]}

        self.assertEqual([row["recipient"] for row in snapshot["control_messages"]], ["supervisor"])
        self.assertEqual([row["recipient"] for row in snapshot["dispatch_messages"]], ["supervisor/child-01"])
        self.assertEqual({row["run_id"] for row in snapshot["supervisor"]["recent_runs"]}, {"liverun1", "stalerun1"})
        self.assertNotIn("result-noise", recent_runs)
        self.assertEqual(set(active_runs), {"liverun1"})
        self.assertEqual(recent_runs["stalerun1"]["status"], "interrupted")
        self.assertEqual(snapshot["active_tasks"][0]["task_id"], first_task)
        self.assertEqual(snapshot["active_tasks"][0]["prompt_href"], f"/artifacts/supervisor-runs/20260314-120000-liverun1/{first_task}/prompt.txt")
        self.assertEqual(graph_tasks[first_task]["latest_result_status"], "success")
        self.assertIsNotNone(graph_tasks[first_task]["active_compute_label"])
        self.assertGreaterEqual(int(graph_tasks[first_task]["total_compute_seconds"]), 0)
        self.assertEqual(graph_tasks[second_task]["predecessor_ids"], [first_task])
        run_cli("render", "--project-root", str(self.root), "--actor", "tester")
        self.assertNotIn('style="', paths.html_file.read_text(encoding="utf-8"))

    def test_supervise_status_reports_loop_controls_ready_tasks_and_recent_results(self) -> None:
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
                "Chat needs loop state, controls, ready tasks, and recent child results.",
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
        pause_message = json.loads(
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
                    "pause",
                    "--body",
                    "pause loop before the next cycle",
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
        loop_dir = self.runtime_paths().supervisor_runs_dir / "20260313-120000-loop-abcd1234"
        loop_dir.mkdir(parents=True)
        status_file = loop_dir / "status.json"
        status_file.write_text(
            json.dumps(
                {
                    "loop_id": "abcd1234",
                    "actor": "supervisor",
                    "workspace_mode": "git-worktree",
                    "poll_interval_seconds": 5.0,
                    "max_cycles": None,
                    "stop_when_idle": False,
                    "loop_dir": str(loop_dir),
                    "status_file": str(status_file),
                    "cycles": [
                        {
                            "index": 1,
                            "at": "2026-03-13T12:00:00-07:00",
                            "status": "paused",
                            "ready_task_ids": [task_ids["Status task one"], task_ids["Status task two"]],
                            "open_message_ids": [pause_message["message_id"]],
                        }
                    ],
                    "completed_at": "2026-03-13T12:00:05-07:00",
                    "final_status": "paused",
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
        self.assertEqual(payload["latest_loop"]["loop_id"], "abcd1234")
        self.assertEqual(payload["latest_loop"]["status_file"], str(status_file))
        self.assertEqual(payload["latest_loop"]["status"], "paused")
        self.assertEqual(payload["control_action"], {"action": "pause", "message_id": pause_message["message_id"]})
        self.assertEqual([row["message_id"] for row in payload["open_control_messages"]], [pause_message["message_id"]])
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
        self.assertIn("Latest loop: paused | abcd1234 | cycles 1 | workspace git-worktree", text_output)
        self.assertIn(f"Next cycle control: pause via {pause_message['message_id']}", text_output)
        self.assertIn("Recent child-run results:", text_output)

    def test_ui_serve_stream_updates_after_backlog_change(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "ui",
                "serve",
                "--project-root",
                str(self.root),
                "--port",
                "0",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=cli_env(),
            cwd=self.root,
        )
        try:
            state_file = wait_for_file(self.runtime_paths().supervisor_runs_dir / "ui-server.json")
            server_state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(Path(server_state["project_root"]).resolve(), self.root.resolve())
            self.assertEqual(Path(server_state["control_dir"]).resolve(), self.runtime_paths().control_dir.resolve())
            stream = urllib.request.urlopen(server_state["stream_url"], timeout=10)
            try:
                initial_event = read_sse_event(stream)
                self.assertEqual(initial_event["event"], "snapshot")
                self.assertEqual(initial_event["data"]["total"], 0)

                run_cli(
                    "add",
                    "--project-root",
                    str(self.root),
                    "--title",
                    "Live UI task",
                    "--bucket",
                    "html",
                    "--why",
                    "Need one task to trigger a live update.",
                    "--evidence",
                    "A server-sent event should arrive after the add command mutates backlog state.",
                    "--safe-first-slice",
                    "Append one task and let the UI server publish a new snapshot.",
                    "--path",
                    "src/blackdog/ui.py",
                    "--epic-title",
                    "UI",
                    "--lane-title",
                    "Live lane",
                    "--wave",
                    "0",
                )

                updated_event = read_sse_event(stream)
                self.assertEqual(updated_event["event"], "snapshot")
                self.assertEqual(updated_event["data"]["total"], 1)
                self.assertEqual(updated_event["data"]["graph"]["tasks"][0]["title"], "Live UI task")
            finally:
                stream.close()
        finally:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            self.assertFalse((self.runtime_paths().supervisor_runs_dir / "ui-server.json").exists())

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
        ve_bin = self.root / ".VE" / "bin"
        ve_bin.mkdir(parents=True)
        blackdog_script = ve_bin / "blackdog"
        blackdog_script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        blackdog_script.chmod(0o755)

        profile = load_profile(self.root)
        snapshot = load_backlog(profile.paths, profile)
        task = next(iter(snapshot.tasks.values()))
        prompt = _build_child_prompt(
            profile,
            task,
            child_agent="supervisor/child-01",
            workspace_mode="current",
            workspace=self.root,
        )

        self.assertIn(str(blackdog_script.resolve()), prompt)
        self.assertNotIn("PYTHONPATH=src python3 -m blackdog.cli", prompt)

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
        self.assertFalse(workspace.exists())
        self.assertIn("Treat committed repo state as the baseline for this task", prompt_text)
        self.assertIn("Do not run `blackdog claim` for this task again.", prompt_text)
        self.assertIn("Prefer Blackdog CLI output over direct reads of raw state files", prompt_text)
        self.assertIn("Commit your code changes on that task branch", prompt_text)
        self.assertIn("Do not run `blackdog complete` for this task from a branch-backed child run", prompt_text)
        paths = self.runtime_paths()
        state = json.loads(paths.state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][task_id]["status"], "done")
        self.assertEqual((self.root / "feature.txt").read_text(encoding="utf-8"), task_id + "\n")
        self.assertIsNotNone(payload["children"][0]["task_branch"])
        self.assertIsNone(payload["children"][0]["land_error"])
        self.assertIsNotNone(payload["children"][0]["land_result"])
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

    def test_supervise_run_lands_through_dirty_primary_worktree(self) -> None:
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
            commit_message="Checkpoint dirty primary launcher for supervisor autostash test",
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
        self.assertIsNotNone(payload["children"][0]["land_result"])
        self.assertIsNotNone(payload["children"][0]["land_result"]["auto_stash"])
        self.assertTrue(payload["children"][0]["land_result"]["auto_stash"]["stash_ref"])
        self.assertFalse((self.root / "dirty.txt").exists())
        self.assertEqual((self.root / "dirty-landed.txt").read_text(encoding="utf-8"), task_id + "\n")
        stash_list = subprocess.run(
            ["git", "-C", str(self.root), "stash", "list"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("blackdog-supervisor-land-", stash_list)
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
        self.assertTrue(any("Please clean up or land your work; Blackdog will retry before stashing." in row["body"] for row in open_messages))

    def test_supervise_loop_runs_multiple_cycles(self) -> None:
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
    file_name = f"loop-{task_id}.txt"
    Path(file_name).write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", file_name], check=True)
    subprocess.run(["git", "commit", "-m", f"Commit {task_id} from loop child"], check=True)
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
            f"loop child completed {task_id}",
            "--validation",
            "fake-loop-child",
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
        for title in ("Loop task one", "Loop task two"):
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                title,
                "--bucket",
                "core",
                "--why",
                "Need a persistent supervisor loop.",
                "--evidence",
                "The loop should keep claiming work across cycles.",
                "--safe-first-slice",
                "Run one child per cycle until both tasks are done.",
                "--path",
                "README.md",
                "--epic-title",
                "Supervisor",
                "--lane-title",
                "Loop lane",
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
            ["git", "-C", str(self.root), "commit", "-m", "Checkpoint loop launcher for supervisor landing test"],
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
                    "loop",
                    "--project-root",
                    str(self.root),
                    "--count",
                    "1",
                    "--poll-interval-seconds",
                    "0",
                    "--max-cycles",
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

        self.assertEqual([cycle["status"] for cycle in payload["cycles"]], ["ran", "ran"])
        for cycle in payload["cycles"]:
            self.assertIsNotNone(cycle["children"][0]["land_result"])
            self.assertIsNone(cycle["children"][0]["land_error"])
        status_file = Path(payload["status_file"])
        self.assertTrue(status_file.exists())
        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        done_count = sum(1 for entry in state["task_claims"].values() if entry.get("status") == "done")
        self.assertEqual(done_count, 2)

    def test_supervise_loop_pauses_on_inbox_message(self) -> None:
        run_cli("init", "--project-root", str(self.root), "--project-name", "Demo")
        run_cli(
            "add",
            "--project-root",
            str(self.root),
            "--title",
            "Loop task",
            "--bucket",
            "core",
            "--why",
            "Need to test inbox steering.",
            "--evidence",
            "The loop should honor pause instructions.",
            "--safe-first-slice",
            "Pause the loop before it launches work.",
            "--path",
            "README.md",
            "--epic-title",
            "Supervisor",
            "--lane-title",
            "Loop lane",
            "--wave",
            "0",
        )
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
            "pause",
            "--body",
            "pause loop",
        )

        payload = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "blackdog.cli",
                    "supervise",
                    "loop",
                    "--project-root",
                    str(self.root),
                    "--actor",
                    "supervisor",
                    "--poll-interval-seconds",
                    "0",
                    "--max-cycles",
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

        self.assertEqual(payload["cycles"][0]["status"], "paused")
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

    def test_supervise_loop_stops_at_cycle_boundary_on_inbox_message(self) -> None:
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
""".strip()
            + "\n",
            encoding="utf-8",
        )
        launcher_script.chmod(0o755)
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
                "Need to test boundary stop semantics.",
                "--evidence",
                "The supervisor loop should stop before starting another cycle.",
                "--safe-first-slice",
                "Run one child, then stop before the next claim.",
                "--path",
                "README.md",
                "--epic-title",
                "Supervisor",
                "--lane-title",
                "Boundary lane",
                "--wave",
                "0",
            )
        profile_text = (self.root / "blackdog.toml").read_text(encoding="utf-8").replace(
            'launch_command = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]',
            f'launch_command = ["{launcher_script}", "exec", "--dangerously-bypass-approvals-and-sandbox"]',
        )
        (self.root / "blackdog.toml").write_text(profile_text, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "blackdog.toml", "codex"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Checkpoint boundary launcher for supervisor stop test"],
            check=True,
            capture_output=True,
            text=True,
        )

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "loop",
                "--project-root",
                str(self.root),
                "--actor",
                "supervisor",
                "--count",
                "1",
                "--poll-interval-seconds",
                "0.2",
                "--max-cycles",
                "3",
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
            status_file = wait_for_glob(self.runtime_paths().supervisor_runs_dir, "*-loop-*/status.json")
            wait_for_json(
                status_file,
                lambda payload: len(payload.get("cycles", [])) >= 1 and payload["cycles"][0]["status"] == "ran",
                timeout=10.0,
            )
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
                "stop after this cycle",
            )
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)
        payload = json.loads(stdout)
        self.assertEqual([cycle["status"] for cycle in payload["cycles"]], ["ran", "stopped"])
        self.assertIn("stopped_by_message_id", payload)
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

    def test_supervise_loop_picks_up_task_added_between_cycles(self) -> None:
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
    file_name = f"picked-up-{task_id}.txt"
    Path(file_name).write_text(task_id + "\\n", encoding="utf-8")
    subprocess.run(["git", "add", file_name], check=True)
    subprocess.run(["git", "commit", "-m", f"Commit {task_id} after backlog pickup"], check=True)
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
            f"picked up task {task_id} after loop restart",
            "--validation",
            "fake-pickup-child",
        ],
        check=True,
        env=os.environ.copy(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
""".strip()
            + "\n",
            encoding="utf-8",
        )
        launcher_script.chmod(0o755)
        profile_text = (self.root / "blackdog.toml").read_text(encoding="utf-8").replace(
            'launch_command = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]',
            f'launch_command = ["{launcher_script}", "exec", "--dangerously-bypass-approvals-and-sandbox"]',
        )
        (self.root / "blackdog.toml").write_text(profile_text, encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "blackdog.toml", "codex"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-m", "Checkpoint pickup launcher for supervisor loop test"],
            check=True,
            capture_output=True,
            text=True,
        )

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "blackdog.cli",
                "supervise",
                "loop",
                "--project-root",
                str(self.root),
                "--actor",
                "supervisor",
                "--count",
                "1",
                "--poll-interval-seconds",
                "0.5",
                "--max-cycles",
                "2",
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
            status_file = wait_for_glob(self.runtime_paths().supervisor_runs_dir, "*-loop-*/status.json")
            wait_for_json(
                status_file,
                lambda payload: len(payload.get("cycles", [])) >= 1 and payload["cycles"][0]["status"] == "idle",
                timeout=10.0,
            )
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                "Task added during loop",
                "--bucket",
                "core",
                "--why",
                "Need to prove the loop rereads backlog state between cycles.",
                "--evidence",
                "A task added after an idle cycle should be claimed on the next pass.",
                "--safe-first-slice",
                "Add one task while the loop is sleeping between cycles.",
                "--path",
                "README.md",
                "--epic-title",
                "Supervisor",
                "--lane-title",
                "Boundary lane",
                "--wave",
                "0",
            )
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)
        payload = json.loads(stdout)
        self.assertEqual([cycle["status"] for cycle in payload["cycles"]], ["idle", "ran"])
        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        done_count = sum(1 for entry in state["task_claims"].values() if entry.get("status") == "done")
        self.assertEqual(done_count, 1)

    def test_supervise_loop_picks_up_task_added_while_current_task_runs(self) -> None:
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
            "Need to prove the loop rereads backlog state after a running task finishes.",
            "--evidence",
            "A downstream task added during the active child run should be claimed on the next cycle.",
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
        (self.root / f"gate-{current_task_id}.txt").write_text("hold\n", encoding="utf-8")
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
    started_file = project_root / f"started-{task_id}.txt"
    gate_file = project_root / f"gate-{task_id}.txt"
    release_file = project_root / f"release-{task_id}.txt"
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
                "loop",
                "--project-root",
                str(self.root),
                "--actor",
                "supervisor",
                "--count",
                "1",
                "--poll-interval-seconds",
                "0",
                "--max-cycles",
                "2",
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
            wait_for_file(self.root / f"started-{current_task_id}.txt", timeout=10)
            run_cli(
                "add",
                "--project-root",
                str(self.root),
                "--title",
                "Task added during active run",
                "--bucket",
                "core",
                "--why",
                "Need to prove the loop does not stop after the current child finishes.",
                "--evidence",
                "A downstream task added during execution should be launched on the next cycle.",
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
            (self.root / f"release-{current_task_id}.txt").write_text("release\n", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)

        payload = json.loads(stdout)
        self.assertEqual([cycle["status"] for cycle in payload["cycles"]], ["ran", "ran"])
        self.assertEqual(payload["cycles"][0]["task_ids"], [current_task_id])
        self.assertEqual(payload["cycles"][1]["task_ids"], [downstream_task_id])
        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][current_task_id]["status"], "done")
        self.assertEqual(state["task_claims"][downstream_task_id]["status"], "done")

    def test_supervise_loop_skips_removed_downstream_task_and_runs_next_available(self) -> None:
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
                "Need to prove the loop rereads backlog state after downstream removal.",
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
        (self.root / f"gate-{current_task_id}.txt").write_text("hold\n", encoding="utf-8")
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
    started_file = project_root / f"started-{task_id}.txt"
    gate_file = project_root / f"gate-{task_id}.txt"
    release_file = project_root / f"release-{task_id}.txt"
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
                "loop",
                "--project-root",
                str(self.root),
                "--actor",
                "supervisor",
                "--count",
                "1",
                "--poll-interval-seconds",
                "0",
                "--max-cycles",
                "2",
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
            wait_for_file(self.root / f"started-{current_task_id}.txt", timeout=10)
            remove_task_from_backlog(self.root, removed_task_id)
            self.assertNotIn("Removed downstream task", task_ids_by_title(self.root))
            (self.root / f"release-{current_task_id}.txt").write_text("release\n", encoding="utf-8")
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)

        payload = json.loads(stdout)
        self.assertEqual([cycle["status"] for cycle in payload["cycles"]], ["ran", "ran"])
        self.assertEqual(payload["cycles"][0]["task_ids"], [current_task_id])
        self.assertEqual(payload["cycles"][1]["task_ids"], [remaining_task_id])
        executed_task_ids = [task_id for cycle in payload["cycles"] for task_id in cycle.get("task_ids", [])]
        self.assertNotIn(removed_task_id, executed_task_ids)
        state = json.loads(self.runtime_paths().state_file.read_text(encoding="utf-8"))
        self.assertEqual(state["task_claims"][current_task_id]["status"], "done")
        self.assertEqual(state["task_claims"][remaining_task_id]["status"], "done")
        self.assertNotIn(removed_task_id, state["task_claims"])
