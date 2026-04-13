from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time

from blackdog_cli.main import main as blackdog_main


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    name: str
    total_ms: int
    handler_actions: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "total_ms": self.total_ms,
            "handler_actions": self.handler_actions,
        }


def _run_cli(*args: str) -> dict[str, object]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = blackdog_main(list(args))
    if exit_code != 0:
        raise RuntimeError(stderr.getvalue().strip() or f"blackdog {' '.join(args)} failed")
    text = stdout.getvalue().strip()
    return json.loads(text) if text else {}


def _timed_cli(name: str, *args: str) -> BenchmarkResult:
    started = time.perf_counter()
    payload = _run_cli(*args)
    total_ms = int((time.perf_counter() - started) * 1000)
    repo_payload = payload.get("repo")
    worktree_payload = payload.get("worktree")
    handlers: list[dict[str, object]] = []
    if isinstance(repo_payload, dict) and isinstance(repo_payload.get("handlers"), dict):
        handlers = list(repo_payload["handlers"].get("actions") or [])
    if isinstance(worktree_payload, dict) and isinstance(worktree_payload.get("handlers"), dict):
        handlers = list(worktree_payload["handlers"].get("actions") or [])
    return BenchmarkResult(name=name, total_ms=total_ms, handler_actions=handlers)


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "blackdog@example.com"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Blackdog Bench"], check=True, capture_output=True, text=True)
    (root / ".gitignore").write_text(".VE/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "Initial bench repo"], check=True, capture_output=True, text=True)


def _commit_runtime_contract(root: Path) -> None:
    subprocess.run(
        ["git", "-C", str(root), "add", "blackdog.toml", ".codex/skills/blackdog/SKILL.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--short"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if status:
        subprocess.run(["git", "-C", str(root), "commit", "-m", "Add Blackdog repo runtime"], check=True, capture_output=True, text=True)


def _put_workset(root: Path, workset_id: str, task_id: str) -> None:
    payload = {
        "id": workset_id,
        "title": workset_id,
        "tasks": [{"id": task_id, "title": task_id, "intent": "benchmark worktree start"}],
    }
    _run_cli(
        "workset",
        "put",
        "--project-root",
        str(root),
        "--json",
        json.dumps(payload),
    )


def run_benchmarks() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir) / "bench-repo"
        root.mkdir(parents=True, exist_ok=True)
        _init_repo(root)

        cold_install = _timed_cli(
            "cold_repo_install",
            "repo",
            "install",
            "--project-root",
            str(root),
            "--project-name",
            "Bench Repo",
            "--source-root",
            str(REPO_ROOT),
            "--json",
        )
        _commit_runtime_contract(root)
        warm_update = _timed_cli(
            "warm_repo_update",
            "repo",
            "update",
            "--project-root",
            str(root),
            "--source-root",
            str(REPO_ROOT),
            "--json",
        )

        _put_workset(root, "bench-cold", "BENCH-1")
        cold_start = _timed_cli(
            "cold_worktree_start",
            "worktree",
            "start",
            "--project-root",
            str(root),
            "--workset",
            "bench-cold",
            "--task",
            "BENCH-1",
            "--actor",
            "codex",
            "--prompt",
            "Benchmark cold worktree start.",
            "--json",
        )

        _put_workset(root, "bench-warm", "BENCH-2")
        warm_start = _timed_cli(
            "warm_worktree_start",
            "worktree",
            "start",
            "--project-root",
            str(root),
            "--workset",
            "bench-warm",
            "--task",
            "BENCH-2",
            "--actor",
            "codex",
            "--prompt",
            "Benchmark warm worktree start.",
            "--json",
        )

        return {
            "benchmarks": [
                cold_install.to_dict(),
                warm_update.to_dict(),
                cold_start.to_dict(),
                warm_start.to_dict(),
            ]
        }


def main() -> int:
    print(json.dumps(run_benchmarks(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
