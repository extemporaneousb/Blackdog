#!/usr/bin/env python3
"""Exercise isolated Blackdog lifecycles and emit comparable timing evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any


class AcceptanceError(RuntimeError):
    """A required public lifecycle assertion failed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def distribution(values: list[float | None]) -> dict[str, Any]:
    samples = [value for value in values if value is not None]
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or value < 0
        for value in samples
    ):
        raise ValueError("durations must be finite nonnegative numbers or null")
    ordered = sorted(samples)
    return {
        "eligible_count": len(values),
        "sample_count": len(samples),
        "missing_count": len(values) - len(samples),
        "p50_ms": statistics.median(samples) if samples else None,
        "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1] if ordered else None,
        "p95_method": "nearest_rank",
        "small_sample": len(samples) < 20,
    }


class Fixture:
    def __init__(self, base: Path, *, artifact: bytes | None, source: Path | None) -> None:
        self.base = base
        self.root = base / "fixture repo with spaces"
        self.root.mkdir()
        self.environment = {
            key: os.environ[key]
            for key in ("PATH", "TMPDIR", "LANG")
            if key in os.environ
        }
        self.environment["BLACKDOG_HOME"] = str(base / "isolated blackdog state")
        tools = base / "fixture tools"
        tools.mkdir()
        (tools / "python3").symlink_to(Path(sys.executable).resolve())
        self.environment["PATH"] = str(tools) + os.pathsep + self.environment.get("PATH", os.defpath)
        self.timings: dict[str, float] = {}
        self.portable = artifact is not None
        self.source = source
        if artifact is not None:
            self.copied_artifact = base / "portable artifact with spaces.pyz"
            self.copied_artifact.write_bytes(artifact)
            self.launch = [sys.executable, "-I", "-S", str(self.copied_artifact)]
        else:
            require(source is not None, "a source or artifact is required")
            self.copied_artifact = None
            self.launch = [
                sys.executable, "-S", "-c",
                "import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "from blackdog_cli.main import main; raise SystemExit(main())",
                str(source / "src"),
            ]

    def command(
        self, argv: list[str], *, cwd: Path | None = None, phase: str | None = None,
    ) -> str:
        start = time.monotonic_ns()
        result = subprocess.run(
            argv, cwd=cwd or self.root, env=self.environment,
            capture_output=True, text=True, timeout=120,
        )
        if phase is not None:
            require(phase not in self.timings, "phase names must identify one measured invocation")
            self.timings[phase] = (time.monotonic_ns() - start) / 1_000_000
        require(
            result.returncode == 0,
            f"{phase or 'fixture command'} returned {result.returncode}: {result.stderr[-1000:]}",
        )
        return result.stdout.strip()

    def git(self, *args: str) -> str:
        return self.command(["git", "-C", str(self.root), *args])

    def cli(
        self, *args: str, phase: str | None = None, cwd: Path | None = None,
        json_flag: bool = True,
    ) -> dict[str, Any]:
        argv = [*self.launch, *args, *(["--json"] if json_flag else [])]
        value = json.loads(self.command(argv, cwd=cwd, phase=phase))
        require(isinstance(value, dict), "CLI JSON must be an object")
        return value

    def complete(self, row: dict[str, Any]) -> None:
        action = row.get("next_action")
        require(
            isinstance(action, dict) and action.get("kind") == "complete",
            "unexpected lifecycle action requires review",
        )

    def begin(self, name: str, *, root: Path | None = None) -> dict[str, Any]:
        target = root or self.root
        row = self.cli(
            "task", "begin", "--project-root", str(target), "--actor", "acceptance",
            "--execution-prompt", "Verify the portable runtime with a public synthetic fixture.",
            "--request", "Verify the portable runtime with a public synthetic fixture.",
            "--title", name, phase=f"begin_{name}", cwd=target,
        )["task"]
        self.complete(row)
        require(row["workspace_role"] == "task", "begin must return a task workspace")
        workspace = Path(row["worktree_path"])
        require(workspace.is_relative_to(self.base), "task workspace escaped the isolated fixture")
        require(workspace.is_dir(), "task workspace is missing")
        if self.portable:
            require(not (workspace / ".VE").exists(), "portable begin created a repository environment")
        return row

    def inspect(self, task_id: str, operation: str, *, phase: str | None = None) -> dict[str, Any]:
        payload = self.cli(
            "task", operation, "--project-root", str(self.root),
            "--task", task_id, phase=phase,
        )
        row = payload[{"show": "task_show", "recover": "recovery"}[operation]]
        require(isinstance(row, dict) and row.get("task_id") == task_id, "read surface lost task identity")
        return row

    def install(self) -> None:
        self.git("init", "-b", "main")
        git_config = {
            "user.name": "Blackdog Acceptance",
            "user.email": "acceptance@example.com",
            "commit.gpgsign": "false",
            "core.hooksPath": str(self.base / "empty hooks"),
        }
        for key, value in git_config.items():
            self.git("config", key, value)
        (self.root / ".gitignore").write_text(".VE/\n", encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "Create acceptance fixture")
        install_args = [
            "repo", "install", "--project-root", str(self.root),
            "--project-name", "Portable Acceptance",
        ]
        if self.source is not None:
            install_args.extend(["--source-root", str(self.source)])
        self.cli(*install_args, phase="install")
        self.git("add", "-A")
        self.git("commit", "-m", "Install acceptance fixture")
        contract = self.cli("worktree", "preflight", "--project-root", str(self.root))
        installed = Path(contract["workspace_blackdog_path"])
        require(
            installed.is_file() and os.access(installed, os.X_OK),
            "installed launcher is not executable",
        )
        if self.portable:
            require(not (self.root / ".VE").exists(), "portable install created a repository environment")
            assert self.copied_artifact is not None
            self.copied_artifact.unlink()
        self.launch = [str(installed)]

    def exercise_close_cleanup(self) -> None:
        closed = self.begin("close")
        closed_path = Path(closed["worktree_path"])
        for operation in ("show", "recover"):
            self.complete(self.inspect(closed["task_id"], operation, phase=operation))
        closure = self.cli(
            "task", "close", "--project-root", str(closed_path), "--actor", "acceptance",
            "--status", "abandoned", "--summary", "Complete the public close fixture",
            "--validation", "fixture=passed", phase="close", cwd=closed_path,
        )["closure"]
        self.complete(closure)
        action = self.inspect(closed["task_id"], "show")["next_action"]
        require(
            action.get("kind") == "command" and action.get("action_id") == "cleanup_terminal_task",
            "retained task must emit its cleanup command",
        )
        cleanup_argv = action.get("argv")
        require(
            isinstance(cleanup_argv, list) and bool(cleanup_argv)
            and all(isinstance(arg, str) for arg in cleanup_argv),
            "cleanup action has invalid argv",
        )
        cleanup_executable = Path(cleanup_argv[0])
        require(
            cleanup_executable.is_file() and not cleanup_executable.is_relative_to(closed_path),
            "cleanup command cannot survive its own worktree removal",
        )
        self.command(cleanup_argv, phase="cleanup")
        require(not closed_path.exists(), "cleanup retained the disposed fixture")
        self.command(cleanup_argv, phase="cleanup_replay")
        self.complete(self.inspect(closed["task_id"], "show"))

    def exercise_landing(self) -> None:
        landed = self.begin("land")
        landed_path = Path(landed["worktree_path"])
        require(landed["target_branch"] == "main", "primary begin recorded the wrong target")
        (landed_path / "accepted.txt").write_text("portable acceptance\n", encoding="utf-8")
        self.command(
            [sys.executable, "-I", "-S", "-c",
             "from pathlib import Path; "
             "assert Path('accepted.txt').read_text() == 'portable acceptance\\n'"],
            cwd=landed_path, phase="validation",
        )
        landing = self.cli(
            "task", "land", "--project-root", str(landed_path), "--actor", "acceptance",
            "--summary", "Land the public acceptance fixture", "--validation", "fixture=passed",
            phase="land", cwd=landed_path,
        )["landing"]
        self.complete(landing)
        require(
            landing["task_status"] == "done" and landing["attempt_status"] == "success",
            "landing terminal state is incomplete",
        )
        require(
            self.git("rev-parse", "main") == landing["landed_commit"],
            "canonical commit did not reach the recorded target",
        )
        require(
            self.git("show", "main:accepted.txt") == "portable acceptance",
            "landed contents differ from validated fixture",
        )
        require(not landed_path.exists(), "default landing did not clean its worktree")
        self.complete(self.inspect(landed["task_id"], "recover", phase="recover_after_land"))

    def verify_terminal_state(self) -> None:
        summary = self.cli("summary", "--project-root", str(self.root), phase="summary")
        require(
            summary["counts"]["tasks"] == 2 and summary["counts"]["in_progress"] == 0,
            "summary lost terminal task state",
        )
        self.cli("snapshot", "--project-root", str(self.root), json_flag=False)
        self.cli("attempts", "table", "--project-root", str(self.root))
        self.cli("worktree", "table", "--project-root", str(self.root))
        require(not self.git("status", "--porcelain"), "fixture target checkout is dirty")
        registered = self.git("worktree", "list", "--porcelain").splitlines()
        require(
            sum(line.startswith("worktree ") for line in registered) == 1,
            "fixture leaked a worktree",
        )

    def run(self) -> dict[str, Any]:
        started = time.monotonic_ns()
        self.install()
        self.exercise_close_cleanup()
        self.exercise_landing()
        self.verify_terminal_state()
        self.timings["scenario_wall"] = (time.monotonic_ns() - started) / 1_000_000
        return {"status": "passed", "durations_ms": self.timings, "terminal_tasks": 2, "landed_tasks": 1}


def source_revision(source: Path) -> str:
    changed = subprocess.run(
        ["git", "-C", str(source), "status", "--porcelain", "--",
         "src", "scripts", "blackdog.toml", "pyproject.toml"],
        check=True, capture_output=True, text=True,
    )
    require(not changed.stdout.strip(), "baseline source implementation must be committed")
    return subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def publish_report(target: Path, report: dict[str, Any]) -> None:
    content = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    runtime = parser.add_mutually_exclusive_group(required=True)
    runtime.add_argument("--artifact", type=Path, help="portable zipapp to test with isolated Python")
    runtime.add_argument(
        "--baseline-source", type=Path,
        help="older source checkout for equivalent environment-coupled baseline",
    )
    parser.add_argument("--samples", type=int, default=7)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.samples < 1 or args.samples > 100:
        parser.error("--samples must be between 1 and 100")
    artifact = args.artifact.resolve() if args.artifact else None
    source = args.baseline_source.resolve() if args.baseline_source else None
    if artifact is not None and not artifact.is_file():
        parser.error("--artifact must name a file")
    if source is not None and not (source / "src" / "blackdog_cli" / "main.py").is_file():
        parser.error("--baseline-source must name a Blackdog source checkout")
    artifact_bytes = artifact.read_bytes() if artifact else None
    baseline_revision = source_revision(source) if source else None
    rows = []
    for _ in range(args.samples):
        with tempfile.TemporaryDirectory(prefix="blackdog runtime acceptance ") as directory:
            rows.append(Fixture(Path(directory).resolve(), artifact=artifact_bytes, source=source).run())
        if source is not None:
            require(source_revision(source) == baseline_revision, "baseline source changed during measurement")
    phases = sorted({phase for row in rows for phase in row["durations_ms"]})
    report = {
        "schema_version": 1,
        "scenario": "fresh_install_close_cleanup_replay_and_land_v1",
        "runtime_mode": "portable_zipapp" if artifact else "source_baseline",
        "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest() if artifact_bytes else None,
        "source_revision": baseline_revision,
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "sample_count": len(rows),
        "validation": "public fixture content assertion before landing",
        "durations": {
            phase: distribution([row["durations_ms"].get(phase) for row in rows])
            for phase in phases
        },
        "samples": rows,
        "limitations": [
            "Synthetic local workload; no agent execution or human waiting measured.",
            "Scenario wall time includes fixture setup and read checks; phases are not a decomposition.",
            "Small samples provide descriptive percentiles, not a stable tail-latency estimate.",
            "Source baseline uses the supplied checkout and its explicit environment handlers.",
        ],
    }
    publish_report(args.output, report)
    print(json.dumps({"status": "passed", "sample_count": len(rows), "runtime_mode": report["runtime_mode"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
