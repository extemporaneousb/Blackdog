from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import blackdog.codex_sessions as codex_sessions
import blackdog.stats as stats
import blackdog.workflow_contract as workflow_contract
from blackdog.workflow_contract import NEXT_ACTION_AUTHORITY_GUIDANCE
from blackdog_cli.main import _build_parser, main
from tests.test_wtam_lifecycle import ProductRepo


_TASK_INTENT_CLAIMS = (
    "A task's durable intent is its title plus the request and execution prompt receipts on its attempts.",
    "Prompt bodies live in content-addressed artifacts.",
    "Planning detail from older formats remains only in immutable migration archives.",
)


def _task_intent_doc_errors(text: str) -> list[str]:
    normalized = " ".join(text.split())
    errors = [claim for claim in _TASK_INTENT_CLAIMS if claim not in normalized]
    if re.search(r"[-*]\s+(?:optional\s+)?`objective`", text):
        errors.append("obsolete task objective field")
    return errors


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(
        item for item in parser._actions if isinstance(item, argparse._SubParsersAction)
    )
    return dict(action.choices)


def _public_command_parsers(
    parser: argparse.ArgumentParser, *parents: str
) -> dict[str, argparse.ArgumentParser]:
    """Discover public leaves from argparse, independently of the declared catalog."""
    groups = [
        action for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    if not groups:
        return {" ".join(("blackdog", *parents)): parser}
    leaves = {}
    for group in groups:
        hidden = {
            action.dest for action in group._choices_actions
            if action.help == argparse.SUPPRESS
        }
        for name, child in group.choices.items():
            if name not in hidden:
                leaves.update(_public_command_parsers(child, *parents, name))
    return leaves


class ProductSurfaceTests(unittest.TestCase):
    def test_declared_inventory_matches_every_public_parser_leaf(self) -> None:
        public = set(_public_command_parsers(_build_parser()))
        declared = workflow_contract.command_invocations()
        self.assertEqual(set(declared), public)
        self.assertEqual(len(declared), len(public))
        self.assertEqual(
            set(workflow_contract.SHIPPED_VISIBLE_COMMAND_INVOCATIONS), public
        )

    def test_inventory_comparison_detects_the_previous_omissions(self) -> None:
        public = set(_public_command_parsers(_build_parser()))
        omitted = {"blackdog repo migrate", "blackdog task outcome", "blackdog task validate"}
        stale_tree = tuple(
            replace(
                command,
                children=tuple(
                    child for child in command.children
                    if f"blackdog {command.name} {child.name}" not in omitted
                ),
            )
            for command in workflow_contract.SHIPPED_VISIBLE_COMMAND_TREE
        )
        with patch.object(workflow_contract, "SHIPPED_VISIBLE_COMMAND_TREE", stale_tree):
            stale = set(workflow_contract.command_invocations())
            self.assertEqual(public - stale, omitted)
            self.assertEqual(stale - public, set())
            with self.assertRaises(AssertionError):
                self.assertEqual(stale, public)

    def test_prompt_preview_cli_inventories_match_public_leaves_without_hidden_flags(self) -> None:
        repo = ProductRepo("command-inventory")
        self.addCleanup(repo.close)
        public = _public_command_parsers(_build_parser())
        argv = [
            "prompt", "preview", "--project-root", str(repo.root),
            "--request", "Inspect the selected code.",
        ]
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = main([*argv, "--json"])
        self.assertEqual(result, 0, error.getvalue())
        preview = json.loads(output.getvalue())["prompt_preview"]
        commands = preview["repo_lifecycle_commands"] + preview["wtam_commands"]
        self.assertEqual(set(commands), set(public))
        self.assertEqual(len(commands), len(public))
        self.assertEqual(
            set(preview["wtam_commands"]),
            {command for command in public if command.split()[1] in {"task", "worktree"}},
        )

        text_output, text_error = io.StringIO(), io.StringIO()
        with redirect_stdout(text_output), redirect_stderr(text_error):
            result = main(argv)
        self.assertEqual(result, 0, text_error.getvalue())
        rendered = re.findall(r"^  - (blackdog .+)$", text_output.getvalue(), re.MULTILINE)
        self.assertEqual(set(rendered), set(public))
        self.assertEqual(len(rendered), len(public))
        hidden_flags = {
            option for parser in public.values() for action in parser._actions
            if action.help == argparse.SUPPRESS for option in action.option_strings
        }
        self.assertTrue(hidden_flags, "The parser has hidden replay inputs to protect")
        for flag in hidden_flags:
            with self.subTest(flag=flag):
                self.assertNotIn(flag, "\n".join(commands))
                self.assertNotIn(flag, text_output.getvalue())
        self.assertFalse(repo.profile.paths.runtime_file.exists())

    def test_command_inventory_is_the_frozen_task_only_surface(self) -> None:
        parser = _build_parser()
        commands = _subcommands(parser)
        self.assertEqual(
            set(commands),
            {
                "self",
                "version",
                "init",
                "summary",
                "snapshot",
                "stats",
                "local-repo",
                "prompt",
                "attempts",
                "codex",
                "repo",
                "task",
                "worktree",
            },
        )
        self.assertEqual(
            set(_subcommands(commands["task"])),
            {
                "begin",
                "show",
                "recover",
                "land",
                "reconcile-landing",
                "close",
                "cancel",
                "reopen",
                "cleanup",
                "outcome",
                "validate",
            },
        )
        self.assertEqual(set(_subcommands(commands["worktree"])), {"preflight", "table"})

    def test_root_read_commands_have_only_runtime_v4_arguments(self) -> None:
        commands = _subcommands(_build_parser())
        summary_dests = {action.dest for action in commands["summary"]._actions}
        self.assertNotIn("include_canceled", summary_dests)
        for command in _subcommands(commands["attempts"]).values():
            with self.subTest(command=command.prog):
                self.assertNotIn("task", {action.dest for action in command._actions})

    def test_repo_skill_uses_the_generated_next_action_guidance(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        skill_text = (project_root / ".codex/skills/blackdog/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"- {NEXT_ACTION_AUTHORITY_GUIDANCE}", skill_text)

    def test_docs_match_the_v4_task_intent_schema(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        for relative_path in ("docs/ARCHITECTURE.md", "docs/FILE_FORMATS.md"):
            with self.subTest(relative_path=relative_path):
                text = (project_root / relative_path).read_text(encoding="utf-8")
                self.assertEqual(_task_intent_doc_errors(text), [])

    def test_task_intent_doc_check_allows_reflow_but_rejects_missing_or_obsolete_claims(self) -> None:
        text = "\n\n".join(_TASK_INTENT_CLAIMS)
        self.assertEqual(_task_intent_doc_errors("\n".join(text.split())), [])
        for claim in _TASK_INTENT_CLAIMS:
            with self.subTest(missing=claim):
                self.assertIn(claim, _task_intent_doc_errors(text.replace(claim, "")))
        for field in ("- optional `objective`", "* optional\n  `objective`", "- `objective`"):
            with self.subTest(obsolete=field):
                self.assertIn("obsolete task objective field", _task_intent_doc_errors(text + "\n" + field))

    def test_hidden_begin_replay_inputs_preserve_prompt_provenance(self) -> None:
        parsed = _build_parser().parse_args(
            [
                "task",
                "begin",
                "--execution-prompt",
                "execution",
                "--request",
                "request",
                "--execution-prompt-source",
                "/tmp/execution.txt",
                "--request-source",
                "/tmp/request.txt",
            ]
        )
        self.assertEqual(parsed.prompt, "execution")
        self.assertEqual(parsed.user_prompt, "request")
        self.assertEqual(parsed.execution_prompt_source, "/tmp/execution.txt")
        self.assertEqual(parsed.request_source, "/tmp/request.txt")

    def test_provider_session_implementation_lives_in_product_package(self) -> None:
        module_path = Path(codex_sessions.__file__).resolve()
        self.assertEqual(module_path.parent.name, "blackdog")
        self.assertTrue(callable(codex_sessions.collect_codex_sessions))
        with patch.object(codex_sessions, "collect_codex_turns", return_value=()) as collect:
            self.assertEqual(stats.collect_codex_turns(since=None), ())
            collect.assert_called_once_with(since=None)


if __name__ == "__main__":
    unittest.main()
