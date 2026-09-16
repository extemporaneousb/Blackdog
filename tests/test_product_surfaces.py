from __future__ import annotations

import argparse
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import blackdog.codex_sessions as codex_sessions
import blackdog.stats as stats
from blackdog.workflow_contract import NEXT_ACTION_AUTHORITY_GUIDANCE
from blackdog_cli.main import _build_parser


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


class ProductSurfaceTests(unittest.TestCase):
    def test_command_inventory_is_the_frozen_task_only_surface(self) -> None:
        parser = _build_parser()
        commands = _subcommands(parser)
        self.assertEqual(
            set(commands),
            {
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
