from __future__ import annotations

import argparse
import re
import tomllib

from blackdog.contract import managed_skill_relative_path
from blackdog.repo_lifecycle import (
    AGENTS_MANAGED_BEGIN,
    AGENTS_MANAGED_END,
    render_repo_agents_contract,
    render_repo_skill,
)
from blackdog.workflow_contract import (
    AGENT_WORKFLOW,
    NEXT_ACTION_AUTHORITY_GUIDANCE,
    PROMPT_INPUT_DISPOSAL_GUIDANCE,
    SHIPPED_VISIBLE_COMMAND_INVOCATIONS,
    TARGET_BRANCH_GUIDANCE,
    visible_command_signature,
)
from blackdog_core.profile import load_profile
from blackdog_cli.main import _build_parser
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class CoreContractTests(CoreAuditTestCase):
    def _visible_command_signature(
        self,
        parser: argparse.ArgumentParser,
    ) -> tuple[tuple[str, tuple[object, ...]], ...]:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return tuple(
                    (
                        choice.dest,
                        self._visible_command_signature(action.choices[choice.dest]),
                    )
                    for choice in action._choices_actions
                    if choice.help != argparse.SUPPRESS
                )
        return ()

    def _markdown_command_list(self, text: str, *, heading: str) -> tuple[str, ...]:
        section = text.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
        return tuple(re.findall(r"^- `(blackdog [^`]+)`$", section, flags=re.MULTILINE))

    def _cli_documented_visible_commands(self, cli_doc: str) -> tuple[str, ...]:
        return tuple(
            command
            for command in SHIPPED_VISIBLE_COMMAND_INVOCATIONS
            if re.search(
                rf"(?m)^(?:### `)?{re.escape(command)}(?:`|(?:\s|$))",
                cli_doc,
            )
        )

    def test_blackdog_repo_opts_into_silent_bounded_codex_hooks(self) -> None:
        config = tomllib.loads((REPO_ROOT / ".codex" / "config.toml").read_text(encoding="utf-8"))
        expected_handler = {
            "type": "command",
            "command": (
                '"$(git rev-parse --show-toplevel)/.VE/bin/blackdog" codex hook stamp '
                '--project-root "$(git rev-parse --show-toplevel)"'
            ),
            "timeout": 5,
        }

        self.assertEqual(
            config["hooks"],
            {
                "UserPromptSubmit": [{"hooks": [expected_handler]}],
                "Stop": [{"hooks": [expected_handler]}],
            },
        )
        self.assertNotIn("--json", expected_handler["command"])

    def test_pyproject_and_makefile_keep_the_shipped_cli_surface(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["scripts"], {"blackdog": "blackdog_cli.main:main"})
        self.assertEqual(
            pyproject["project"]["description"],
            "Repo-scoped task and attempt runtime for AI-assisted local development",
        )
        self.assertNotIn("blackdog", pyproject.get("tool", {}))
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("python3 -m unittest discover", makefile)
        make_targets = {
            line.split(":", 1)[0]
            for line in makefile.splitlines()
            if line and not line.startswith((".", "\t")) and ":" in line
        }
        self.assertEqual(make_targets, {"acceptance", "public-check", "test", "test-core"})
        self.assertIn("test: public-check", makefile)

    def test_core_import_boundaries_exclude_blackdog_product_code(self) -> None:
        self.assertEqual(self.core_import_boundary_violations(), [])

    def test_docs_freeze_the_lean_machine_owned_contract(self) -> None:
        index_doc = (REPO_ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
        architecture = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        cli_doc = (REPO_ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
        file_formats = (REPO_ROOT / "docs" / "FILE_FORMATS.md").read_text(encoding="utf-8")

        self.assertIn("[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)", index_doc)
        self.assertIn("[docs/CLI.md](docs/CLI.md)", index_doc)
        self.assertIn("[docs/FILE_FORMATS.md](docs/FILE_FORMATS.md)", index_doc)
        self.assertIn("Validate repo installation and layering through the normal test suite", index_doc)
        self.assertNotIn("PRODUCT_SPEC", index_doc)
        self.assertNotIn("TARGET_MODEL", index_doc)
        self.assertNotIn("OPERATOR_NOTES", index_doc)
        self.assertIn("package boundaries, storage ownership, repo lifecycle", architecture)
        self.assertIn("planning.json", architecture)
        self.assertIn("runtime.json", architecture)
        self.assertIn("Agents mutate planning and runtime state", architecture)
        self.assertIn("prompt receipts", architecture)
        self.assertIn("workset claims", architecture)
        self.assertIn("repo lifecycle workflows", architecture)
        self.assertIn("repo analyze", architecture)
        self.assertIn("repo install", architecture)
        self.assertIn("repo update", architecture)
        self.assertIn("repo refresh", architecture)
        self.assertIn("handler blocks", architecture)
        self.assertIn("python-overlay-venv", architecture)
        self.assertIn("blackdog-runtime", architecture)
        self.assertIn("Older runtime files may still load one removed managed-claim token", architecture)
        self.assertIn("repo analyze", cli_doc)
        self.assertIn("repo install", cli_doc)
        self.assertIn("repo update", cli_doc)
        self.assertIn("repo refresh", cli_doc)
        self.assertIn("prompt preview", cli_doc)
        self.assertIn("prompt tune", cli_doc)
        self.assertIn("attempts summary", cli_doc)
        self.assertIn("attempts table", cli_doc)
        self.assertIn("codex hook stamp", cli_doc)
        self.assertIn("workset put", cli_doc)
        self.assertIn("BLACKDOG_ENABLE_WORKSET_COMMANDS=1", cli_doc)
        self.assertIn("task begin", cli_doc)
        self.assertIn("task show", cli_doc)
        self.assertIn("task recover", cli_doc)
        self.assertIn("task land", cli_doc)
        self.assertIn("task close", cli_doc)
        self.assertIn("task cleanup", cli_doc)
        self.assertIn("--workset", cli_doc)
        self.assertIn("next --project-root /path/to/repo --workset kernel", cli_doc)
        self.assertIn("worktree preflight", cli_doc)
        self.assertIn("worktree table", cli_doc)
        self.assertIn("worktree preview", cli_doc)
        self.assertIn("worktree start", cli_doc)
        self.assertIn("worktree show", cli_doc)
        self.assertIn("worktree land", cli_doc)
        self.assertIn("worktree close", cli_doc)
        self.assertIn("worktree cleanup", cli_doc)
        self.assertIn("[[handlers]]", cli_doc)
        self.assertIn("handler plan", cli_doc)
        self.assertIn("root-bin fallback", cli_doc)
        self.assertIn("--prompt", cli_doc)
        self.assertIn("--show-prompt", cli_doc)
        self.assertIn("summary", cli_doc)
        self.assertIn("snapshot", cli_doc)
        self.assertIn("prompt_source", cli_doc)
        self.assertIn("Blackdog-Execution-Model", cli_doc)
        self.assertIn("Blackdog-User-Prompt-Hash", cli_doc)
        self.assertIn("execution_prompt_source", cli_doc)
        self.assertIn("user_prompt_source", cli_doc)
        self.assertIn("reasoning_effort", cli_doc)
        self.assertIn("commit", cli_doc)
        self.assertNotIn("analysis-only workflow", cli_doc)
        self.assertIn("bind/table/scaffold/install/update/refresh/archive/unarchive/unbind", cli_doc)
        self.assertIn("canonical landed commit", cli_doc)
        self.assertIn("planning.json", file_formats)
        self.assertIn("runtime.json", file_formats)
        self.assertIn("attempts", file_formats)
        self.assertIn("workset_claim", file_formats)
        self.assertIn("task_claims", file_formats)
        self.assertIn("execution_model", file_formats)
        self.assertIn("prompt_receipt", file_formats)
        self.assertIn("user_prompt_receipt", file_formats)
        self.assertIn("Blackdog-Execution-Model", file_formats)
        self.assertIn("Blackdog-User-Prompt-Hash", file_formats)
        self.assertIn("execution_prompt_*", file_formats)
        self.assertIn("user_prompt_*", file_formats)
        self.assertIn("prompt modes", file_formats)
        self.assertIn("worktree.start", file_formats)
        self.assertIn("blackdog.toml", file_formats)
        self.assertIn("python-overlay-venv", file_formats)
        self.assertIn("blackdog-runtime", file_formats)
        self.assertIn("handler_actions", file_formats)
        self.assertIn("abandoned", file_formats)
        self.assertIn("worktree.close", file_formats)
        self.assertIn("`backlog.md` is not part of the current contract", file_formats)
        self.assertIn("New runtime writes use only `direct_wtam`", file_formats)
        self.assertNotIn("blackdog supervisor", architecture)
        self.assertNotIn("workset_manager", architecture)
        self.assertNotIn("blackdog supervisor", cli_doc)
        self.assertNotIn("blackdog supervisor", file_formats)
        self.assertNotIn("workset_manager", file_formats)
        self.assertNotIn("docs/SUPERVISED_EXECUTION_TARGET.md", index_doc)

    def test_guardrail_reporting_contracts_are_documented(self) -> None:
        agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        architecture = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        cli_doc = (REPO_ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
        file_formats = (REPO_ROOT / "docs" / "FILE_FORMATS.md").read_text(encoding="utf-8")

        for text in (architecture, cli_doc, file_formats):
            lower_text = text.lower()
            with self.subTest(contract="task-class guards"):
                self.assertIn("task-class guard extension points", lower_text)
            with self.subTest(contract="implementation-without-Blackdog"):
                self.assertIn("implementation-without-blackdog detection", lower_text)
            with self.subTest(contract="learning reports"):
                self.assertIn("learning/report outputs", lower_text)
            with self.subTest(contract="supervised closeout"):
                self.assertIn("supervised integration closeout", lower_text)

        self.assertIn("environment/launcher repair expectations", architecture.lower())
        self.assertIn("environment/launcher repair expectations", cli_doc.lower())
        self.assertIn("handler_actions", file_formats)
        self.assertIn("implementation_like_unlinked_turns", cli_doc)
        self.assertIn("implementation_like_unlinked_turns", file_formats)
        self.assertIn("blackdog codex coverage|history|hook", agents)

    def test_cli_help_matches_shared_visible_command_manifest(self) -> None:
        parser = _build_parser()
        self.assertEqual(self._visible_command_signature(parser), visible_command_signature())

    def test_docs_publish_shared_visible_command_inventory(self) -> None:
        index_doc = (REPO_ROOT / "docs" / "INDEX.md").read_text(encoding="utf-8")
        architecture = (REPO_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        cli_doc = (REPO_ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")

        self.assertEqual(
            self._markdown_command_list(index_doc, heading="Current Product Surface"),
            SHIPPED_VISIBLE_COMMAND_INVOCATIONS,
        )
        self.assertEqual(
            self._markdown_command_list(architecture, heading="Current Shipped Surface"),
            SHIPPED_VISIBLE_COMMAND_INVOCATIONS,
        )
        self.assertEqual(
            self._cli_documented_visible_commands(cli_doc),
            SHIPPED_VISIBLE_COMMAND_INVOCATIONS,
        )

    def test_checked_in_agent_contracts_match_current_renderers(self) -> None:
        profile = load_profile(REPO_ROOT)
        skill_path = REPO_ROOT / managed_skill_relative_path(profile)
        agents_text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        managed_start = agents_text.index(AGENTS_MANAGED_BEGIN)
        managed_end = agents_text.index(AGENTS_MANAGED_END) + len(AGENTS_MANAGED_END)
        managed_contract = agents_text[managed_start:managed_end] + "\n"

        self.assertEqual(skill_path.read_text(encoding="utf-8"), render_repo_skill(profile))
        self.assertEqual(managed_contract, render_repo_agents_contract(profile))
        skill_text = skill_path.read_text(encoding="utf-8")
        for rendered_contract in (managed_contract, skill_text):
            self.assertIn("--actor codex", rendered_contract)
            self.assertIn("--json", rendered_contract)
            self.assertIn("exact triggering user request verbatim", rendered_contract)
            self.assertEqual(rendered_contract.count(PROMPT_INPUT_DISPOSAL_GUIDANCE), 1)
            self.assertEqual(rendered_contract.count(NEXT_ACTION_AUTHORITY_GUIDANCE), 1)
            self.assertEqual(rendered_contract.count(TARGET_BRANCH_GUIDANCE), 1)
            self.assertIn("retry_task_close_finalization", rendered_contract)
            self.assertNotIn("--actor AGENT", rendered_contract)
            self.assertNotIn("--execution-prompt-file EXECUTION_PROMPT", rendered_contract)
            self.assertNotIn("--request-file USER_REQUEST", rendered_contract)
            self.assertNotIn("delete the temporary inputs after Blackdog confirms", rendered_contract)
            self.assertNotIn("partial or blocked normal task lifecycle result", rendered_contract)
            self.assertNotIn("primary `main` branch", rendered_contract)

        self.assertTrue(AGENT_WORKFLOW.begin_command.endswith(" --json"))
        self.assertTrue(AGENT_WORKFLOW.land_command.endswith(" --json"))
        for doc_name in ("ARCHITECTURE.md", "CLI.md", "FILE_FORMATS.md"):
            doc = (REPO_ROOT / "docs" / doc_name).read_text(encoding="utf-8")
            self.assertIn("execution_prompt_replay_artifact_path", doc, doc_name)
            self.assertIn("user_prompt_replay_artifact_path", doc, doc_name)
            self.assertIn("regardless of `operation_status`", doc, doc_name)
            self.assertIn("target_branch", doc, doc_name)
            self.assertIn("never assume it is `main`", doc, doc_name)

        manual_rules = " ".join(agents_text[:managed_start].split())
        self.assertIn("`task begin` is the one normal implementation entrypoint", manual_rules)
        self.assertIn("it performs its own readiness checks", manual_rules)
        self.assertIn("`worktree preflight` is optional read-only diagnosis", manual_rules)
        self.assertIn("not a separate prerequisite for `task begin`", manual_rules)
        self.assertNotIn("Before any repo edit you intend to keep, run", manual_rules)
        self.assertNotIn("primary worktree: yes`, stop", manual_rules)

    def test_repo_prunes_legacy_product_modules_and_docs(self) -> None:
        removed_paths = [
            "src/blackdog/architecture.py",
            "src/blackdog/board.py",
            "src/blackdog/conversations.py",
            "src/blackdog/execution_context.py",
            "src/blackdog/installs.py",
            "src/blackdog/scaffold.py",
            "src/blackdog/supervisor.py",
            "src/blackdog/supervisor_policy.py",
            "src/blackdog/tuning.py",
            "src/blackdog/ui.css",
            "src/blackdog/workset_manager.py",
            "src/blackdog/worktree.py",
            "docs/ACCEPTANCE.md",
            "docs/BOUNDARIES.md",
            "docs/CHARTER.md",
            "docs/EMACS.md",
            "docs/EXTRACTION_AUDIT.md",
            "docs/INTEGRATION.md",
            "docs/MIGRATION.md",
            "docs/MODULE_INVENTORY.md",
            "docs/OPERATOR_NOTES.md",
            "docs/OWNERSHIP_INVENTORY.md",
            "docs/PRODUCT_SPEC.md",
            "docs/RELEASE_NOTES.md",
            "docs/REPO_LIFECYCLE_MVP.md",
            "docs/SINGLE_AGENT_AUDIT.md",
            "docs/SUPERVISED_EXECUTION_TARGET.md",
            "docs/TARGET_MODEL_EXECUTION_PLAN.md",
            "docs/TARGET_MODEL.md",
            "docs/architecture-diagrams.html",
        ]
        for relative_path in removed_paths:
            with self.subTest(path=relative_path):
                self.assertFalse((REPO_ROOT / relative_path).exists(), f"{relative_path} should be removed")
        self.assertFalse(
            any(path.is_file() for path in (REPO_ROOT / "extensions").rglob("*")),
            "legacy editor surfaces under extensions/ should be removed",
        )

    def test_doc_routing_still_points_at_required_repo_contract_docs(self) -> None:
        profile = tomllib.loads((REPO_ROOT / "blackdog.toml").read_text(encoding="utf-8"))
        routed = profile["taxonomy"]["doc_routing_defaults"]
        self.assertEqual(
            routed,
            [
                "AGENTS.md",
                "docs/INDEX.md",
                "docs/ARCHITECTURE.md",
                "docs/CLI.md",
                "docs/FILE_FORMATS.md",
            ],
        )
        handlers = profile["handlers"]
        self.assertEqual(handlers[0]["kind"], "python-overlay-venv")
        self.assertEqual(handlers[1]["kind"], "blackdog-runtime")
