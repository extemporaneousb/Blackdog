from __future__ import annotations

import argparse
from pathlib import Path
import unittest

import blackdog.codex_sessions as codex_sessions
import blackdog.stats as stats
from blackdog.workflow_contract import NEXT_ACTION_AUTHORITY_GUIDANCE
from blackdog_cli.main import _build_parser


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(
        item for item in parser._actions if isinstance(item, argparse._SubParsersAction)
    )
    return dict(action.choices)


def _subcommand_help(parser: argparse.ArgumentParser) -> dict[str, str]:
    action = next(
        item for item in parser._actions if isinstance(item, argparse._SubParsersAction)
    )
    return {item.dest: item.help for item in action._choices_actions}


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

    def test_readme_inventory_matches_the_task_only_surface(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        readme = (project_root / "README.md").read_text(encoding="utf-8")
        for expected in (
            "`blackdog_core`: durable task runtime contract",
            "`blackdog prompt preview`",
            "`blackdog codex coverage|history|hook stamp`",
            "`blackdog worktree preflight|table`",
        ):
            self.assertIn(expected, readme)

    def test_task_help_uses_direct_task_language(self) -> None:
        parser = _build_parser()
        self.assertEqual(
            _subcommand_help(parser)["task"],
            "Manage executable task lifecycle",
        )
        self.assertEqual(
            _subcommand_help(_subcommands(parser)["task"]),
            {
                "begin": "Create a task and start its WTAM attempt",
                "show": "Inspect the current or latest task for this worktree",
                "recover": "Inspect recovery state or classify an interrupted attempt",
                "cancel": "Cancel an inactive task",
                "reopen": "Reopen a canceled task",
                "land": "Land the current task and close it",
                "reconcile-landing": (
                    "Prove and optionally correct a landed commit missing from terminal "
                    "runtime state"
                ),
                "close": "Close the current task without landing code",
                "cleanup": "Remove a retained or leftover task workspace and delete its branch",
            },
        )

    def test_repo_skill_uses_the_generated_next_action_guidance(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        skill_text = (project_root / ".codex/skills/blackdog/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"- {NEXT_ACTION_AUTHORITY_GUIDANCE}", skill_text)

    def test_docs_match_the_v4_task_intent_schema(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        expected = (
            "A task's durable intent is its title plus the request and execution prompt\n"
            "receipts on its attempts. Prompt bodies live in content-addressed artifacts.\n"
            "Planning detail from older formats remains only in immutable migration\n"
            "archives."
        )
        for relative_path in ("docs/ARCHITECTURE.md", "docs/FILE_FORMATS.md"):
            with self.subTest(relative_path=relative_path):
                text = (project_root / relative_path).read_text(encoding="utf-8")
                self.assertIn(expected, text)
                self.assertNotIn("- optional `objective`", text)

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
        self.assertIs(stats.collect_codex_turns, codex_sessions.collect_codex_turns)


if __name__ == "__main__":
    unittest.main()
