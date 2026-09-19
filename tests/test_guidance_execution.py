from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import hashlib
import io
import json
from unittest import TestCase
from unittest.mock import patch

import blackdog.wtam as wtam
from blackdog.contract import managed_skill_relative_path
from blackdog.prompting import preview_prompt
from blackdog_cli.main import main
from blackdog_core.profile import ConfigError
from blackdog_core.state import ValidationRecord, load_runtime_state, task_record
from blackdog_core.tasks import TaskError
from tests.test_wtam_lifecycle import ProductRepo, _ReadyHandlers, _git


class GuidanceExecutionTests(TestCase):
    def setUp(self):
        self.repo = ProductRepo("guidance-execution")
        self.addCleanup(self.repo.close)
        self.guide = self.repo.root / "guide.md"
        self.content = "# Local guidance\r\nPreserve obligations.\r\n\r\n"
        self.guide.write_bytes(self.content.encode())
        skill = self.repo.root / managed_skill_relative_path(self.repo.profile)
        skill.parent.mkdir(parents=True)
        skill.write_text("Use the repository workflow.\n")
        _git(self.repo.root, "add", "guide.md", ".codex")
        _git(self.repo.root, "commit", "-qm", "Add guidance")
        self.stack = self.enterContext(ExitStack())
        for name in ("plan_worktree_handlers", "execute_worktree_handlers", "validate_existing_worktree_handlers"):
            self.stack.enter_context(patch.object(wtam, name, return_value=_ReadyHandlers()))

    def begin(self, **kwargs):
        options = dict(actor=self.repo.actor, prompt="Bounded implementation.",
                       user_prompt="Tidy this up.", user_prompt_source="request.txt",
                       prompt_mode="skill", task_id=self.repo.task_id,
                       branch=self.repo.branch, path=str(self.repo.worktree), cwd=self.repo.root,
                       guidance=("guide.md",), host="fixture-host", host_version="1.0")
        options.update(kwargs)
        return wtam.begin_task_worktree(self.repo.profile, **options)

    def test_guidance_is_frozen_and_exact_recovery_ignores_changed_or_deleted_sources(self):
        first = self.begin()
        task = task_record(load_runtime_state(self.repo.profile.paths), self.repo.task_id)
        attempt = task.attempts[-1]
        receipt = attempt.prompt_receipt
        artifact = self.repo.profile.paths.control_dir / receipt.replay_artifact_path
        frozen = artifact.read_bytes().decode()
        self.assertIn(json.dumps(self.content, ensure_ascii=False), frozen)
        self.assertEqual(attempt.setup_receipt["guidance"]["documents"], [{
            "path": "guide.md", "sha256": hashlib.sha256(self.content.encode()).hexdigest(),
        }])
        self.assertEqual(attempt.setup_receipt["execution_context"]["host_version"], "1.0")
        request = self.repo.profile.paths.control_dir / attempt.user_prompt_receipt.replay_artifact_path
        self.assertEqual(request.read_text(), "Tidy this up.")
        self.guide.write_text("Different future convention\n")
        for removed in (False, True):
            if removed:
                self.guide.unlink()
            replay = self.begin(prompt=frozen, prompt_source=str(artifact),
                                guidance=(), host=None, host_version=None)
            self.assertEqual(replay["prompt_hash"], first["prompt_hash"])
            self.assertEqual(replay["setup_receipt"]["guidance"], attempt.setup_receipt["guidance"])
            self.assertEqual(replay["setup_receipt"]["execution_context"], attempt.setup_receipt["execution_context"])
            self.assertEqual(artifact.read_bytes().decode(), frozen)
        with self.assertRaisesRegex(TaskError, "different execution context"):
            self.begin(prompt=frozen, prompt_source=str(artifact), guidance=(), host_version="2.0")
        with self.assertRaisesRegex(TaskError, "already contains frozen guidance"):
            self.begin(prompt=frozen, prompt_source=str(artifact))

    def test_invalid_guidance_or_context_cannot_create_a_task(self):
        for options in ({"guidance": ("missing.md",)}, {"host_version": "1.0", "host": None},
                        {"host": "bad\ncontext"}, {"guidance": ("../outside.md",)}):
            with self.subTest(options=options), self.assertRaises((ConfigError, TaskError, ValueError)):
                self.begin(**options)
            self.assertFalse(self.repo.profile.paths.runtime_file.exists())
            self.assertFalse(self.repo.worktree.exists())

    def test_successor_keeps_guidance_but_does_not_invent_current_host(self):
        first = self.begin()
        artifact = self.repo.profile.paths.control_dir / first["execution_prompt_replay_artifact_path"]
        frozen = artifact.read_bytes().decode()
        self.guide.unlink()
        for host, host_version in ((None, None), ("new-host", "2.0")):
            closed = wtam.close_task(self.repo.profile, task_id=self.repo.task_id, actor=self.repo.actor,
                            status="blocked", summary="Retain workspace",
                            validations=(ValidationRecord("fixture", "skipped"),), cleanup=False)
            self.assertEqual(closed.operation_status, "succeeded", closed.to_dict())
            wtam.cancel_task(self.repo.profile, task_id=self.repo.task_id, actor=self.repo.actor,
                             summary="Cancel before reopening")
            wtam.reopen_task(self.repo.profile, task_id=self.repo.task_id, actor=self.repo.actor,
                             summary="Resume authorized work")
            resumed = self.begin(prompt=frozen, prompt_source=str(artifact), guidance=(),
                                 host=host, host_version=host_version)
            self.assertEqual(resumed["prompt_hash"], first["prompt_hash"])
            self.assertEqual(resumed["setup_receipt"]["guidance"], first["setup_receipt"]["guidance"])
            if host is None:
                self.assertNotIn("execution_context", resumed["setup_receipt"])
            else:
                self.assertEqual(resumed["setup_receipt"]["execution_context"]["host"], host)
                self.assertEqual(resumed["setup_receipt"]["execution_context"]["host_version"], host_version)

    def test_preview_and_raw_admission_preserve_only_selected_content(self):
        unrelated = self.repo.root / "unrelated.md"
        unrelated.write_text("MUST NOT LOAD THIS BODY")
        _git(self.repo.root, "add", "unrelated.md")
        _git(self.repo.root, "commit", "-qm", "Add unrelated document")
        preview = preview_prompt(self.repo.profile, request="Tidy this up.", include_prompt=True,
                                 guidance=("guide.md",))
        self.assertIn(json.dumps(self.content, ensure_ascii=False), preview.composed_prompt)
        self.assertNotIn("MUST NOT LOAD", preview.composed_prompt)
        begun = self.begin(prompt_mode="raw", include_prompt=True)
        artifact = self.repo.profile.paths.control_dir / begun["execution_prompt_replay_artifact_path"]
        frozen = artifact.read_bytes().decode()
        self.assertIn(json.dumps(self.content, ensure_ascii=False), frozen)
        self.assertNotIn("You are working in the repo", frozen)

    def test_preview_cli_accepts_request_without_removed_source_argument(self):
        for selection in ([], ["--guidance", "guide.md"]):
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                result = main(["prompt", "preview", "--project-root", str(self.repo.root),
                               "--request", "Compare options; do not implement.",
                               "--show-prompt", "--json", *selection])
            self.assertEqual(result, 0, error.getvalue())
            preview = json.loads(output.getvalue())["prompt_preview"]
            self.assertEqual(len(preview["guidance"]), int(bool(selection)))
            self.assertFalse(self.repo.profile.paths.runtime_file.exists())
