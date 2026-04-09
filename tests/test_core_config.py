from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import blackdog_core.profile as config_module
from tests.core_audit_support import CoreAuditTestCase


class CoreConfigAuditTests(CoreAuditTestCase):
    def write_profile(self, text: str) -> None:
        (self.root / config_module.PROFILE_FILE_NAME).write_text(text, encoding="utf-8")

    def test_core_config_helper_names_cover_empty_prefix_and_named_backlogs(self) -> None:
        self.assertEqual(config_module.default_id_prefix("!!!"), "BDOG")
        self.assertEqual(
            config_module.default_html_file_name("Demo Project", "Sprint Review"),
            "demo-project-sprint-review-backlog.html",
        )
        self.assertEqual(config_module.default_host_skill_name("Demo Project"), "blackdog-demo-project")
        self.assertEqual(
            config_module.default_host_skill_dir("Demo Project"),
            ".codex/skills/blackdog-demo-project",
        )

    def test_core_config_named_backlog_paths_build_named_artifact_roots_and_reject_blank_names(self) -> None:
        self.write_profile(config_module.render_default_profile("Demo"))
        profile = config_module.load_profile(self.root)

        named = config_module.named_backlog_paths(profile, " Sprint Review ")

        self.assertEqual(
            named.backlog_dir,
            (profile.paths.control_dir / config_module.DEFAULT_NAMED_BACKLOGS_DIR / "sprint-review").resolve(),
        )
        self.assertEqual(named.backlog_file, named.backlog_dir / "backlog.md")
        self.assertEqual(named.results_dir, named.backlog_dir / "task-results")
        self.assertEqual(named.html_file, named.backlog_dir / "demo-sprint-review-backlog.html")
        self.assertEqual(named.skill_dir, profile.paths.skill_dir)
        self.assertEqual(named.worktrees_dir, profile.paths.worktrees_dir)

        with self.assertRaises(config_module.ConfigError):
            config_module.named_backlog_paths(profile, "  ")

    def test_core_config_find_project_root_and_write_default_profile_raise_for_missing_inputs(self) -> None:
        missing_root = self.root / "nested" / "inner"
        missing_root.mkdir(parents=True)
        with self.assertRaises(config_module.ConfigError):
            config_module.find_project_root(missing_root)

        profile_path = config_module.write_default_profile(self.root, "Demo")
        self.assertEqual(profile_path, (self.root / config_module.PROFILE_FILE_NAME).resolve())
        with self.assertRaises(config_module.ConfigError):
            config_module.write_default_profile(self.root, "Demo")

    def test_core_config_git_helpers_cover_success_and_failure_branches(self) -> None:
        success = subprocess.CompletedProcess(["git"], 0, "relative/.git\n", "")
        absolute = subprocess.CompletedProcess(["git"], 0, "/tmp/shared-git\n", "")
        failure = subprocess.CompletedProcess(["git"], 1, "", "fatal: no git dir")

        with patch("blackdog_core.profile.subprocess.run", return_value=success):
            self.assertEqual(config_module._run_git(self.root, "status"), "relative/.git")
            self.assertEqual(
                config_module._resolve_path_value(self.root, config_module.GIT_COMMON_TOKEN),
                (self.root / "relative/.git").resolve(),
            )
            self.assertEqual(
                config_module._resolve_path_value(self.root, f"{config_module.GIT_COMMON_TOKEN}/blackdog"),
                (self.root / "relative/.git/blackdog").resolve(),
            )

        with patch("blackdog_core.profile.subprocess.run", return_value=absolute):
            self.assertEqual(config_module._git_common_dir(self.root), Path("/tmp/shared-git").resolve())

        with patch("blackdog_core.profile.subprocess.run", return_value=failure):
            with self.assertRaises(config_module.ConfigError):
                config_module._run_git(self.root, "status")

    def test_core_config_paths_from_raw_cover_explicit_supervisor_runs_and_missing_runtime_keys(self) -> None:
        raw_paths = {
            "control_dir": "@git-common/blackdog",
            "skill_dir": ".codex/skills/demo",
            "worktrees_dir": "../.worktrees",
            "supervisor_runs_dir": "tmp/supervisor-runs",
        }
        with patch("blackdog_core.profile._run_git", return_value=".git"):
            paths = config_module._paths_from_raw(self.root, raw_paths, project_name="Demo")
        self.assertEqual(paths.control_dir, (self.root / ".git/blackdog").resolve())
        self.assertEqual(paths.backlog_file, paths.control_dir / "backlog.md")
        self.assertEqual(paths.supervisor_runs_dir, (self.root / "tmp/supervisor-runs").resolve())

        fallback_paths = {
            "skill_dir": ".codex/skills/demo",
            "backlog_dir": ".git/blackdog",
            "backlog_file": ".git/blackdog/backlog.md",
            "state_file": ".git/blackdog/backlog-state.json",
            "events_file": ".git/blackdog/events.jsonl",
            "results_dir": ".git/blackdog/task-results",
            "threads_dir": ".git/blackdog/threads",
            "inbox_file": ".git/blackdog/inbox.jsonl",
            "html_file": ".git/blackdog/demo-backlog.html",
        }
        fallback = config_module._paths_from_raw(self.root, fallback_paths, project_name="Demo")
        self.assertEqual(fallback.control_dir, fallback.backlog_dir)
        self.assertEqual(fallback.supervisor_runs_dir, fallback.backlog_dir / "supervisor-runs")

        raw_paths_no_control = {
            "skill_dir": ".codex/skills/demo",
            "backlog_dir": ".git/blackdog",
            "backlog_file": ".git/blackdog/backlog.md",
            "events_file": ".git/blackdog/events.jsonl",
            "results_dir": ".git/blackdog/task-results",
            "inbox_file": ".git/blackdog/inbox.jsonl",
            "html_file": ".git/blackdog/demo-backlog.html",
        }
        with self.assertRaises(config_module.ConfigError):
            config_module._paths_from_raw(self.root, raw_paths_no_control, project_name="Demo")

    def test_core_config_load_profile_rejects_missing_skill_dir_and_runtime_paths(self) -> None:
        self.write_profile(
            "[project]\nname = \"Demo\"\n\n[paths]\ncontrol_dir = \"@git-common/blackdog\"\n"
        )
        with self.assertRaises(config_module.ConfigError):
            config_module.load_profile(self.root)

        self.write_profile(
            "[project]\nname = \"Demo\"\n\n[paths]\nskill_dir = \".codex/skills/demo\"\n"
        )
        with self.assertRaises(config_module.ConfigError):
            config_module.load_profile(self.root)

    def test_core_config_load_profile_parses_string_launch_command_and_explicit_runs_dir(self) -> None:
        self.write_profile(
            "[project]\n"
            "name = \"Demo\"\n\n"
            "[paths]\n"
            "control_dir = \"@git-common/blackdog\"\n"
            "skill_dir = \".codex/skills/demo\"\n"
            "supervisor_runs_dir = \"tmp/runs\"\n\n"
            "[supervisor]\n"
            "launch_command = \"codex exec --dangerously-bypass-approvals-and-sandbox\"\n"
            "reasoning_effort = \"high\"\n"
            "dynamic_reasoning = true\n"
            "max_parallel = 3\n"
        )

        with patch("blackdog_core.profile._run_git", return_value=".git"):
            profile = config_module.load_profile(self.root)

        self.assertEqual(
            profile.supervisor_launch_command,
            ("codex", "exec", "--dangerously-bypass-approvals-and-sandbox"),
        )
        self.assertEqual(profile.supervisor_reasoning_effort, "high")
        self.assertTrue(profile.supervisor_dynamic_reasoning)
        self.assertEqual(profile.supervisor_max_parallel, 3)
        self.assertEqual(profile.paths.supervisor_runs_dir, (self.root / "tmp/runs").resolve())

    def test_core_config_load_profile_rejects_invalid_supervisor_settings(self) -> None:
        cases = (
            ('launch_command = "   "\n', "supervisor.launch_command"),
            ('reasoning_effort = "extreme"\n', "supervisor.reasoning_effort"),
            ('workspace_mode = "linked"\n', "supervisor.workspace_mode"),
            ("max_parallel = -1\n", "supervisor.max_parallel"),
        )
        for supervisor_lines, expected in cases:
            with self.subTest(expected=expected):
                self.write_profile(
                    "[project]\n"
                    "name = \"Demo\"\n\n"
                    "[paths]\n"
                    "control_dir = \"@git-common/blackdog\"\n"
                    "skill_dir = \".codex/skills/demo\"\n\n"
                    "[supervisor]\n"
                    + supervisor_lines
                )
                with patch("blackdog_core.profile._run_git", return_value=".git"):
                    with self.assertRaises(config_module.ConfigError) as exc:
                        config_module.load_profile(self.root)
                self.assertIn(expected, str(exc.exception))
