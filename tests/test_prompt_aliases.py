from __future__ import annotations

from contextlib import redirect_stderr
import io
from pathlib import Path

import blackdog.wtam as wtam
from blackdog.contract import managed_skill_relative_path
from blackdog.workflow_contract import (
    EXECUTION_PROMPT_INPUT,
    PROMPT_INPUT_CONTRACTS,
    REQUEST_INPUT,
    REQUEST_LINEAGE_INPUT,
)
from blackdog_core.state import create_prompt_receipt
from blackdog_cli.main import _build_parser, _load_text_input
from tests.core_audit_support import CoreAuditTestCase


class PromptAliasTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile("Prompt Alias Demo")
        self.parser = _build_parser()
        self.execution_path = self.root / "execution.txt"
        self.request_path = self.root / "request.txt"
        self.execution_path.write_text("Implement the bounded change.\n", encoding="utf-8")
        self.request_path.write_text("Please make the change.\n", encoding="utf-8")

    def parse(self, *args: str):
        return self.parser.parse_args(args)

    def assert_parse_error(self, *args: str) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parse(*args)

    def receipt(self, args, *, role: str, mode: str = "raw"):
        if role == "request_lineage":
            raw_text = args.user_prompt
            file_path = args.user_prompt_file
            inline_source = REQUEST_LINEAGE_INPUT.canonical_inline_source
        else:
            raw_text = args.prompt
            file_path = args.prompt_file
            inline_source = (
                EXECUTION_PROMPT_INPUT.canonical_inline_source
                if role == "execution"
                else REQUEST_INPUT.canonical_inline_source
            )
        text, source = _load_text_input(
            label=role,
            raw_text=raw_text,
            file_path=file_path,
            inline_source=inline_source,
        )
        return create_prompt_receipt(
            text,
            recorded_at="2026-07-16T12:00:00+00:00",
            source=source,
            mode=mode,
        )

    def test_prompt_preview_and_tune_aliases_produce_identical_receipts(self) -> None:
        for command in ("preview", "tune"):
            old_inline = self.parse("prompt", command, "--prompt", "same request")
            new_inline = self.parse("prompt", command, "--request", "same request")
            self.assertEqual(self.receipt(old_inline, role="request"), self.receipt(new_inline, role="request"))

            old_file = self.parse("prompt", command, "--prompt-file", str(self.request_path))
            new_file = self.parse("prompt", command, "--request-file", str(self.request_path))
            self.assertEqual(self.receipt(old_file, role="request"), self.receipt(new_file, role="request"))

    def test_task_begin_aliases_preserve_execution_request_and_skill_provenance(self) -> None:
        base = ("task", "begin", "--actor", "codex", "--prompt-mode", "skill")
        old = self.parse(
            *base,
            "--prompt-file",
            str(self.execution_path),
            "--user-prompt-file",
            str(self.request_path),
        )
        new = self.parse(
            *base,
            "--execution-prompt-file",
            str(self.execution_path),
            "--request-file",
            str(self.request_path),
        )
        self.assertEqual(
            self.receipt(old, role="execution", mode=old.prompt_mode),
            self.receipt(new, role="execution", mode=new.prompt_mode),
        )
        self.assertEqual(
            self.receipt(old, role="request_lineage"),
            self.receipt(new, role="request_lineage"),
        )

        profile = self.load_test_profile()
        skill_path = self.root / managed_skill_relative_path(profile)
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text("managed skill bytes\n", encoding="utf-8")
        old_provenance = wtam._managed_skill_provenance(profile, workspace_root=self.root)
        new_provenance = wtam._managed_skill_provenance(profile, workspace_root=self.root)
        self.assertEqual(old_provenance, new_provenance)

    def test_worktree_preview_and_start_aliases_produce_identical_execution_receipts(self) -> None:
        for command in ("preview", "start"):
            base = ("worktree", command, "--workset", "ws", "--task", "T-1", "--actor", "codex")
            old_inline = self.parse(*base, "--prompt", "same execution")
            new_inline = self.parse(*base, "--execution-prompt", "same execution")
            self.assertEqual(
                self.receipt(old_inline, role="execution"),
                self.receipt(new_inline, role="execution"),
            )
            old_file = self.parse(*base, "--prompt-file", str(self.execution_path))
            new_file = self.parse(*base, "--execution-prompt-file", str(self.execution_path))
            self.assertEqual(
                self.receipt(old_file, role="execution"),
                self.receipt(new_file, role="execution"),
            )

    def test_alias_collisions_are_rejected_for_every_prompt_surface(self) -> None:
        for command in ("preview", "tune"):
            self.assert_parse_error("prompt", command, "--request", "one", "--prompt", "two")
            self.assert_parse_error(
                "prompt",
                command,
                "--request-file",
                str(self.request_path),
                "--prompt-file",
                str(self.request_path),
            )
            self.assert_parse_error(
                "prompt",
                command,
                "--request",
                "one",
                "--prompt-file",
                str(self.request_path),
            )

        task_base = ("task", "begin", "--actor", "codex")
        self.assert_parse_error(*task_base, "--execution-prompt", "one", "--prompt", "two")
        self.assert_parse_error(
            *task_base,
            "--execution-prompt-file",
            str(self.execution_path),
            "--prompt-file",
            str(self.execution_path),
        )
        self.assert_parse_error(
            *task_base,
            "--execution-prompt",
            "one",
            "--prompt-file",
            str(self.execution_path),
        )
        self.assert_parse_error(
            *task_base,
            "--execution-prompt",
            "one",
            "--request",
            "raw one",
            "--user-prompt",
            "raw two",
        )
        self.assert_parse_error(
            *task_base,
            "--execution-prompt",
            "one",
            "--request",
            "raw one",
            "--user-prompt-file",
            str(self.request_path),
        )

        for command in ("preview", "start"):
            base = ("worktree", command, "--workset", "ws", "--task", "T-1", "--actor", "codex")
            self.assert_parse_error(*base, "--execution-prompt", "one", "--prompt", "two")
            self.assert_parse_error(
                *base,
                "--execution-prompt",
                "one",
                "--prompt-file",
                str(self.execution_path),
            )

    def test_manifest_declares_canonical_and_supported_compatibility_flags(self) -> None:
        self.assertEqual(
            tuple(contract.role for contract in PROMPT_INPUT_CONTRACTS),
            ("request", "execution", "request_lineage"),
        )
        for contract in PROMPT_INPUT_CONTRACTS:
            self.assertTrue(contract.inline_flag.startswith("--"))
            self.assertTrue(contract.file_flag.endswith("-file"))
            self.assertEqual(contract.compatibility_status, "supported_alias")
            self.assertTrue(contract.canonical_inline_source.startswith("inline:--"))
