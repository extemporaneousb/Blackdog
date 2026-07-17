from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import unittest

import blackdog.wtam as wtam
from blackdog_cli.main import main as cli_main
from tests.test_landing_transaction_faults import LandingRepo, _run_git


def _git_output(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class CleanupOwnershipTests(unittest.TestCase):
    def _terminal_pristine_repo(self, suffix: str) -> LandingRepo:
        repo = LandingRepo(cleanup=False, suffix=suffix)
        (repo.worktree / f"{suffix}.txt").unlink()
        result = wtam.close_task(
            repo.profile,
            workset_id=repo.workset_id,
            task_id=repo.task_id,
            actor=repo.actor,
            status="blocked",
            summary="Retain a pristine workspace for cleanup ownership proof.",
            cleanup=False,
        )
        self.assertEqual(result.operation_status, "succeeded")
        return repo

    def _snapshot(self, repo: LandingRepo, *paths: Path) -> dict[str, object]:
        return {
            "worktrees": _git_output(repo.root, "worktree", "list", "--porcelain"),
            "refs": _git_output(
                repo.root,
                "for-each-ref",
                "--format=%(refname) %(objectname)",
                "refs/heads",
            ),
            "runtime": repo.profile.paths.runtime_file.read_bytes(),
            "events": repo.profile.paths.events_file.read_bytes(),
            "paths": tuple((str(path), path.exists()) for path in paths),
        }

    def _run_cleanup_cli(
        self,
        repo: LandingRepo,
        *extra: str,
    ) -> tuple[int, dict[str, object], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli_main(
                [
                    "task",
                    "cleanup",
                    "--project-root",
                    str(repo.root),
                    "--workset",
                    repo.workset_id,
                    "--task",
                    repo.task_id,
                    *extra,
                    "--json",
                ]
            )
        rendered = stdout.getvalue()
        self.assertTrue(rendered, stderr.getvalue())
        return exit_code, json.loads(rendered)["cleanup"], stderr.getvalue()

    def _assert_typed_refusal(self, exit_code: int, payload: dict[str, object]) -> None:
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["operation_status"], "blocked")
        self.assertFalse(payload["mutation_started"])
        self.assertFalse(payload["mutation_completed"])
        self.assertEqual(payload["mutation_phase"], "none")
        self.assertTrue(payload["cleanup_refused"])
        self.assertEqual(
            payload["branch_cleanup_proof"],
            "workspace_ownership_unproven",
        )
        next_action = payload["next_action"]
        self.assertEqual(next_action["action_id"], "inspect_cleanup_ownership")
        self.assertEqual(next_action["safety_class"], "read_only")
        self.assertEqual(next_action["mutation_class"], "none")
        self.assertEqual(
            payload["recommended_commands"],
            [
                {
                    "action_id": next_action["action_id"],
                    "command": next_action["command"],
                    "argv": next_action["argv"],
                    "reason": next_action["reason_detail"],
                    "disposition": next_action["disposition"],
                }
            ],
        )

    def test_unrelated_registered_path_is_zero_mutation_typed_refusal(self) -> None:
        repo = self._terminal_pristine_repo("unrelated-path")
        unrelated = repo.base / "unrelated-worktree"
        try:
            _run_git(repo.root, "worktree", "add", "-b", "unrelated", str(unrelated), "main")
            before = self._snapshot(repo, repo.worktree, unrelated)
            exit_code, payload, stderr = self._run_cleanup_cli(
                repo,
                f"--path={unrelated}",
            )
            self.assertEqual(stderr, "")
            self._assert_typed_refusal(exit_code, payload)
            self.assertTrue(payload["worktree_existed"])
            self.assertEqual(self._snapshot(repo, repo.worktree, unrelated), before)
        finally:
            subprocess.run(
                ["git", "-C", str(repo.root), "worktree", "remove", "--force", str(unrelated)],
                check=False,
                capture_output=True,
                text=True,
            )
            repo.close()

    def test_wrong_branch_is_zero_mutation_typed_refusal(self) -> None:
        repo = self._terminal_pristine_repo("wrong-branch")
        try:
            _run_git(repo.root, "branch", "unrelated", "main")
            before = self._snapshot(repo, repo.worktree)
            exit_code, payload, stderr = self._run_cleanup_cli(
                repo,
                "--branch=unrelated",
            )
            self.assertEqual(stderr, "")
            self._assert_typed_refusal(exit_code, payload)
            self.assertEqual(self._snapshot(repo, repo.worktree), before)
        finally:
            repo.close()

    def test_detached_recorded_workspace_is_zero_mutation_typed_refusal(self) -> None:
        repo = self._terminal_pristine_repo("detached")
        try:
            _run_git(repo.worktree, "checkout", "--detach")
            before = self._snapshot(repo, repo.worktree)
            exit_code, payload, stderr = self._run_cleanup_cli(repo)
            self.assertEqual(stderr, "")
            self._assert_typed_refusal(exit_code, payload)
            self.assertIn("detached", payload["error"])
            self.assertEqual(self._snapshot(repo, repo.worktree), before)
        finally:
            repo.close()

    def test_primary_path_is_zero_mutation_typed_refusal(self) -> None:
        repo = self._terminal_pristine_repo("primary-path")
        try:
            before = self._snapshot(repo, repo.root, repo.worktree)
            exit_code, payload, stderr = self._run_cleanup_cli(
                repo,
                f"--path={repo.root}",
            )
            self.assertEqual(stderr, "")
            self._assert_typed_refusal(exit_code, payload)
            self.assertEqual(self._snapshot(repo, repo.root, repo.worktree), before)
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
