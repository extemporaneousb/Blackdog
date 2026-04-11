from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from blackdog_core.backlog import BacklogSnapshot, BacklogTask, TaskNarrative
from blackdog_core.profile import BlackdogPaths, RepoProfile
from blackdog_core import runtime_model


class RuntimeModelTests(unittest.TestCase):
    def _profile(self, root: Path) -> RepoProfile:
        paths = BlackdogPaths(
            project_root=root,
            profile_file=root / "blackdog.toml",
            control_dir=root / ".git" / "blackdog",
            backlog_dir=root / ".git" / "blackdog",
            backlog_file=root / ".git" / "blackdog" / "backlog.md",
            state_file=root / ".git" / "blackdog" / "backlog-state.json",
            events_file=root / ".git" / "blackdog" / "events.jsonl",
            results_dir=root / ".git" / "blackdog" / "task-results",
            threads_dir=root / ".git" / "blackdog" / "threads",
            inbox_file=root / ".git" / "blackdog" / "inbox.jsonl",
            html_file=root / ".git" / "blackdog" / "board.html",
            skill_dir=root / ".codex" / "skills" / "blackdog",
            worktrees_dir=root / ".worktrees",
            supervisor_runs_dir=root / ".git" / "blackdog" / "supervisor-runs",
        )
        return RepoProfile(
            project_name="Blackdog",
            profile_version=1,
            id_prefix="BLACK",
            id_digest_length=10,
            require_claim_for_completion=True,
            auto_render_html=False,
            buckets=("core", "cli", "docs"),
            domains=("state", "events", "inbox", "results"),
            validation_commands=("make test",),
            doc_routing_defaults=("docs/TARGET_MODEL.md",),
            supervisor_launch_command=("codex", "exec"),
            supervisor_model=None,
            supervisor_reasoning_effort=None,
            supervisor_dynamic_reasoning=False,
            supervisor_max_parallel=1,
            supervisor_workspace_mode="git-worktree",
            pm_heuristics={},
            paths=paths,
        )

    def _task(
        self,
        *,
        task_id: str,
        title: str,
        requires_approval: bool,
        wave: int,
        predecessor_ids: tuple[str, ...] = (),
    ) -> BacklogTask:
        payload = {
            "id": task_id,
            "title": title,
            "bucket": "core",
            "priority": "P2",
            "risk": "low",
            "effort": "M",
            "paths": ["src/blackdog_core/runtime_model.py"],
            "checks": ["PYTHONPATH=src python3 -m unittest tests.test_runtime_model"],
            "docs": ["docs/TARGET_MODEL.md"],
            "requires_approval": requires_approval,
            "approval_reason": "review needed" if requires_approval else "",
            "safe_first_slice": "Add the runtime-model projection layer.",
            "task_shaping": {"estimated_active_minutes": 30},
            "objective": "OBJ-1",
            "domains": ["state"],
        }
        return BacklogTask(
            payload=payload,
            narrative=TaskNarrative(why="target model", evidence="runtime model slice", affected_paths=("src/blackdog_core/runtime_model.py",)),
            epic_title=None,
            lane_id=None,
            lane_title=None,
            wave=wave,
            lane_order=wave,
            lane_position=0,
            predecessor_ids=predecessor_ids,
        )

    def _snapshot(self) -> BacklogSnapshot:
        tasks = {
            "BLACK-1": self._task(task_id="BLACK-1", title="Implement runtime model", requires_approval=False, wave=0),
            "BLACK-2": self._task(
                task_id="BLACK-2",
                title="Review model boundaries",
                requires_approval=True,
                wave=1,
                predecessor_ids=("BLACK-1",),
            ),
        }
        plan = {
            "epics": [],
            "lanes": [],
        }
        return BacklogSnapshot(
            raw_text="",
            headers={"Project": "Target Runtime Model"},
            sections={},
            tasks=tasks,
            plan=plan,
        )

    def test_project_runtime_model_projects_current_artifacts_into_target_types(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            profile = self._profile(root)
            snapshot = self._snapshot()
            state = {
                "approval_tasks": {
                    "BLACK-2": {
                        "status": "pending",
                        "title": "Review model boundaries",
                        "bucket": "core",
                        "paths": ["src/blackdog_core/runtime_model.py"],
                        "approval_reason": "review needed",
                    }
                },
                "task_claims": {
                    "BLACK-1": {
                        "status": "done",
                        "title": "Implement runtime model",
                        "bucket": "core",
                        "paths": ["src/blackdog_core/runtime_model.py"],
                        "priority": "P2",
                        "risk": "low",
                        "claimed_by": "codex",
                        "claimed_at": "2026-04-11T10:00:00-07:00",
                        "completed_by": "codex",
                        "completed_at": "2026-04-11T10:30:00-07:00",
                    }
                },
            }
            events = [
                {
                    "event_id": "evt-1",
                    "type": "claim",
                    "at": "2026-04-11T10:00:00-07:00",
                    "actor": "codex",
                    "task_id": "BLACK-1",
                    "payload": {},
                },
                {
                    "event_id": "evt-2",
                    "type": "task_result",
                    "at": "2026-04-11T10:30:00-07:00",
                    "actor": "codex",
                    "task_id": "BLACK-1",
                    "payload": {
                        "run_id": "run-1",
                        "result_file": str(root / ".git" / "blackdog" / "task-results" / "BLACK-1" / "20260411-103000-run-1.json"),
                    },
                },
            ]
            inbox = [
                {
                    "action": "message",
                    "message_id": "msg-1",
                    "at": "2026-04-11T10:05:00-07:00",
                    "sender": "supervisor",
                    "recipient": "codex",
                    "kind": "pause",
                    "task_id": "BLACK-1",
                    "reply_to": None,
                    "tags": ["control"],
                    "body": "pause the run",
                },
                {
                    "action": "resolve",
                    "message_id": "msg-1",
                    "at": "2026-04-11T10:06:00-07:00",
                    "actor": "supervisor",
                    "note": "cleared",
                },
                {
                    "action": "message",
                    "message_id": "msg-2",
                    "at": "2026-04-11T10:07:00-07:00",
                    "sender": "supervisor",
                    "recipient": "codex",
                    "kind": "request-input",
                    "task_id": "BLACK-2",
                    "reply_to": None,
                    "tags": ["control"],
                    "body": "need a boundary call",
                },
            ]
            results = [
                {
                    "schema_version": 1,
                    "task_id": "BLACK-1",
                    "recorded_at": "2026-04-11T10:30:00-07:00",
                    "actor": "codex",
                    "run_id": "run-1",
                    "status": "success",
                    "what_changed": ["created runtime_model.py"],
                    "validation": ["tests.test_runtime_model"],
                    "residual": [],
                    "needs_user_input": False,
                    "followup_candidates": [],
                    "metadata": {"branch": "feature/runtime-model"},
                    "task_shaping_telemetry": {"estimated_active_minutes": 30},
                }
            ]
            workspace = runtime_model.project_workspace(
                project_root=root,
                checkout_root=root / "worktree",
                branch="feature/runtime-model",
                commit="abc123",
                dirty=False,
                role="task",
                workspace_mode="git-worktree",
                target_branch="main",
                task_id="BLACK-1",
            )
            prompt_receipt = runtime_model.project_prompt_receipt(
                task_id="BLACK-1",
                run_id="run-1",
                actor="codex",
                workspace=workspace,
                at="2026-04-11T10:01:00-07:00",
                prompt_text="Implement the runtime model.",
                prompt_file=root / ".git" / "blackdog" / "supervisor-runs" / "run-1" / "BLACK-1" / "prompt.txt",
                prompt_hash="prompt-hash",
                prompt_template_version="v1",
                prompt_template_hash="template-hash",
                launch_command=("codex", "exec"),
                launch_command_strategy="prompt",
                launch_settings={"model": "gpt-5"},
                packet={"prompt_text": "Implement the runtime model.", "launch_settings": {"model": "gpt-5"}},
            )

            model = runtime_model.project_runtime_model(
                profile,
                snapshot,
                state,
                workspace=workspace,
                events=events,
                inbox=inbox,
                results=results,
                prompt_receipts=(prompt_receipt,),
                execution_mode="supervisor-led",
                execution_id="run-1",
            )

            self.assertEqual(model.repository.project_name, "Blackdog")
            self.assertEqual(model.workspace.branch, "feature/runtime-model")
            self.assertEqual(model.workset.workset_id, "blackdog")
            self.assertEqual(model.workset.title, "Target Runtime Model")
            self.assertEqual(model.workset.task_ids, ("BLACK-1", "BLACK-2"))
            self.assertEqual(model.task_states[0].status, "done")
            self.assertEqual(model.task_states[0].latest_result.status, "success")
            self.assertEqual(model.task_states[1].status, "approval")
            self.assertEqual(model.task_attempts[0].attempt_id, "BLACK-1:run-1")
            self.assertEqual(model.task_attempts[0].prompt_receipt.prompt_text, "Implement the runtime model.")
            self.assertEqual(model.workset_execution.execution_id, "run-1")
            self.assertEqual(model.workset_execution.status, "waiting")
            self.assertEqual(
                {wait.kind for wait in model.wait_conditions},
                {"task:approval", "control:request-input"},
            )
            self.assertEqual(
                {(message.message_id, message.status) for message in model.control_messages},
                {("msg-1", "resolved"), ("msg-2", "open")},
            )

    def test_project_prompt_receipt_keeps_full_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            profile = self._profile(root)
            workspace = runtime_model.project_workspace(
                project_root=root,
                checkout_root=root / "worktree",
                branch="feature/runtime-model",
                commit="abc123",
                dirty=False,
                role="task",
                workspace_mode="git-worktree",
                target_branch="main",
            )
            receipt = runtime_model.project_prompt_receipt(
                task_id="BLACK-1",
                run_id="run-1",
                actor="codex",
                workspace=workspace,
                at="2026-04-11T10:01:00-07:00",
                prompt_text="Implement the runtime model.",
                prompt_file=root / "prompt.txt",
                prompt_hash="prompt-hash",
                prompt_template_version="v1",
                prompt_template_hash="template-hash",
                launch_command=("codex", "exec"),
                launch_command_strategy="prompt",
                launch_settings={"model": "gpt-5"},
                packet={"prompt_text": "Implement the runtime model.", "launch_settings": {"model": "gpt-5"}},
            )

            self.assertEqual(receipt.receipt_id, "BLACK-1:run-1")
            self.assertEqual(receipt.attempt_id, "BLACK-1:run-1")
            self.assertEqual(receipt.prompt_text, "Implement the runtime model.")
            self.assertEqual(receipt.prompt_hash, "prompt-hash")
            self.assertEqual(receipt.packet["launch_settings"]["model"], "gpt-5")
            self.assertEqual(receipt.branch, "feature/runtime-model")
            self.assertEqual(receipt.workspace_root, workspace.checkout_root)


if __name__ == "__main__":
    unittest.main()
