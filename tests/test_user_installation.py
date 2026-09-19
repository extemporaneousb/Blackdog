from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from blackdog import runtime_distribution as runtime
from blackdog.repo_lifecycle import RepoLifecycleError, update_repo
from blackdog.user_installation import UserInstallationError, install_user
from blackdog_core.profile import load_profile
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT
from tests.process_support import run_cli


class UserInstallationTests(CoreAuditTestCase):
    def release(self, label: str) -> Path:
        entries = runtime._package_entries(REPO_ROOT)
        entries["blackdog/__init__.py"] = entries["blackdog/__init__.py"].replace(b'"0.1.0"', f'"0.1.0.{label}"'.encode())
        # Independent process evidence of the runtime that actually executed a
        # normal command; version is deliberately a user-level diagnostic.
        entries["blackdog_cli/main.py"] = entries["blackdog_cli/main.py"].replace(
            b"print(json.dumps(payload, indent=2, sort_keys=True))",
            f'payload["fixture_runtime"] = "{label}"\n    print(json.dumps(payload, indent=2, sort_keys=True))'.encode(),
        )
        entries["blackdog/guidance.py"] = entries["blackdog/guidance.py"].replace(
            b"Common judgment and delivery", f"Common judgment and delivery {label}".encode(),
        )
        target = self.root / f"{label} release.pyz"
        with patch.object(runtime, "_package_entries", return_value=entries):
            runtime.write_release(target, source_root=REPO_ROOT)
        return target

    def call(self, executable: Path, *args: str, cwd: Path | None = None, ok: bool = True):
        env = dict(os.environ)
        env["PATH"] = str(self.root / "user bin") + os.pathsep + env.get("PATH", os.defpath)
        result = run_cli([str(executable), *args], cwd=cwd or self.root, env=env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result

    def install(self, archive: Path):
        return self.call(archive, "self", "install", "--bin-dir", str(self.root / "user bin"),
                         "--data-dir", str(self.root / "user data"), "--json")["installation"]

    def test_two_release_user_upgrade_preserves_repository_until_combined_update(self) -> None:
        a, b = self.release("a"), self.release("b")
        initial = self.call(a, "repo", "install", "--project-name", "Demo", "--json")["repo"]
        old = Path(initial["blackdog_path"])
        installed = self.install(a)
        user = Path(installed["entrypoint"])
        self.assertEqual(installed["visibility"]["status"], "visible")
        self.install(b)
        a.unlink(); b.unlink()
        nested = self.root / "nested"; nested.mkdir()
        observed = self.call(user, "version", "--json", cwd=nested)["version"]
        self.assertEqual(observed["invoking"]["version"], "0.1.0.b")
        self.assertEqual(observed["repository"]["runtime"]["version"], "0.1.0.a")
        self.assertEqual(self.call(user, "summary", "--json", cwd=nested)["fixture_runtime"], "a")
        with tempfile.TemporaryDirectory() as elsewhere:
            self.assertEqual(self.call(user, "summary", "--project-root", str(self.root), "--json", cwd=Path(elsewhere))["fixture_runtime"], "a")
        profile = (self.root / "blackdog.toml").read_bytes()
        custom = self.root / "project-guidance.md"; custom.write_text("Repository-owned conventions\n")
        paths = load_profile(self.root).paths
        before = {p: p.read_bytes() if p.exists() else None for p in (paths.runtime_file, paths.events_file)}
        update = self.call(user, "repo", "update", "--json")["repo"]
        self.assertEqual(update["action"], "update")
        self.assertIn("Common judgment and delivery b", (self.root / ".codex/skills/demo/references/engineering.md").read_text())
        self.assertEqual(self.call(user, "summary", "--json")["fixture_runtime"], "b")
        self.assertEqual(self.call(old, "summary", "--json")["fixture_runtime"], "a")
        self.assertEqual((self.root / "blackdog.toml").read_bytes(), profile)
        self.assertEqual(custom.read_text(), "Repository-owned conventions\n")
        for path, data in before.items():
            self.assertEqual(path.read_bytes() if path.exists() else None, data)
        self.assertTrue(old.is_file())
        # An explicit source override can differ from the invoking archive;
        # instruction generation must follow the installed source, too.
        self.call(old, "repo", "update", "--source-root", str(REPO_ROOT), "--json")
        current = self.call(user, "version", "--json")["version"]
        self.assertEqual(current["invoking"]["version"], "0.1.0.b")
        self.assertEqual(current["repository"]["runtime"]["version"], "0.1.0")
        self.assertTrue((self.root / ".codex/skills/demo/references/engineering.md").read_text().startswith("# Common judgment and delivery\n"))

    def test_custom_control_root_and_linked_worktree_use_shared_selected_archive(self) -> None:
        self.write_profile()
        path = self.root / "blackdog.toml"
        path.write_text(path.read_text().replace("@git-common/blackdog", "@git-common/custom-runtime"))
        a, b = self.release("a"), self.release("b")
        self.call(a, "repo", "install", "--json")
        user = Path(self.install(b)["entrypoint"])
        self.git_output("add", "blackdog.toml", "AGENTS.md", ".codex")
        self.git_output("commit", "-m", "Install fixture")
        linked = self.root / "linked checkout"
        self.git_output("worktree", "add", "-b", "linked", str(linked))
        try:
            report = self.call(user, "version", "--json", cwd=linked)["version"]
            self.assertEqual(report["repository"]["control_dir"], str((self.root / ".git/custom-runtime").resolve()))
            self.assertEqual(self.call(user, "summary", "--json", cwd=linked)["fixture_runtime"], "a")
            self.call(user, "repo", "update", "--json", cwd=linked)
            self.assertIn("Common judgment and delivery b", (linked / ".codex/skills/demo/references/engineering.md").read_text())
            self.assertIn("Common judgment and delivery a", (self.root / ".codex/skills/demo/references/engineering.md").read_text())
        finally:
            self.git_output("worktree", "remove", "--force", str(linked))

    def test_corrupt_repository_runtime_fails_closed_without_user_fallback(self) -> None:
        a, b = self.release("a"), self.release("b")
        repo = self.call(a, "repo", "install", "--json")["repo"]
        user = Path(self.install(b)["entrypoint"])
        selected = Path(repo["blackdog_path"])
        selected.write_bytes(selected.read_bytes() + b"corrupt")
        failure = self.call(user, "summary", "--json", ok=False)
        self.assertIn("digest", failure.stderr)
        report = self.call(user, "version", "--json")["version"]
        self.assertIn("digest", report["repository"]["error"])

    def test_version_remains_read_only_when_profile_or_task_store_is_unusable(self) -> None:
        archive = self.release("a")
        self.call(archive, "repo", "install", "--json")
        user = Path(self.install(archive)["entrypoint"])
        store = load_profile(self.root).paths.runtime_file
        store.write_text('{"schema_version": 999}')
        self.assertIsNone(self.call(user, "version", "--json")["version"]["repository"]["error"])
        self.assertEqual(store.read_text(), '{"schema_version": 999}')
        (self.root / "blackdog.toml").write_text('invalid = [')
        version = self.call(user, "version", "--json")["version"]
        self.assertEqual(version["invoking"]["version"], "0.1.0.a")
        self.assertIsNotNone(version["repository"]["error"])
        self.assertEqual(store.read_text(), '{"schema_version": 999}')

    def test_development_checkout_dispatch_uses_source_and_preserves_arguments(self) -> None:
        archive = self.release("a")
        self.call(archive, "repo", "install", "--json")
        user = Path(self.install(archive)["entrypoint"])
        (self.root / "pyproject.toml").write_text('[project]\nname = "blackdog"\n')
        module = self.root / "src/blackdog_cli/main.py"
        module.parent.mkdir(parents=True); module.write_text("# Source fixture\n")
        script = self.root / "scripts/blackdog"; script.parent.mkdir()
        script.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({"source_argv":sys.argv[1:]}))\n')
        script.chmod(0o755)
        args = ["prompt", "preview", "--request", "quoted coordinator; keep spaces", "--json"]
        self.assertEqual(self.call(user, *args)["source_argv"], args)
        version = self.call(user, "version", "--json")["version"]["repository"]
        self.assertIsNone(version["runtime"])
        self.assertEqual(version["recovery_runtime"]["version"], "0.1.0.a")
        # A copied user launcher is not a valid repository executable: reject
        # the loop before exec instead of repeatedly reentering the dispatcher.
        script.write_bytes(user.read_bytes())
        result = run_cli([str(user), *args], cwd=self.root, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("recursive dispatch", result.stderr)

    def test_unmanaged_command_is_preserved_and_missing_path_is_reported(self) -> None:
        bin_dir = self.root / "not on PATH"; bin_dir.mkdir()
        command = bin_dir / "blackdog"; command.write_text("owned by another installer")
        with self.assertRaisesRegex(UserInstallationError, "unmanaged"):
            install_user(bin_dir=bin_dir, data_dir=self.root / "user data")
        self.assertEqual(command.read_text(), "owned by another installer")
        command.unlink()
        result = install_user(bin_dir=bin_dir, data_dir=self.root / "user data")
        self.assertNotEqual(result["visibility"]["status"], "visible")
        self.assertIn("export PATH=", result["visibility"]["path_setup"])
        first = Path(result["runtime"]["path"])
        with patch("blackdog.user_installation._publish_bytes", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                install_user(bin_dir=bin_dir, data_dir=self.root / "user data")
        self.assertTrue(first.exists())
        self.assertEqual(self.call(command, "version", "--project-root", str(bin_dir), "--json")["version"]["invoking"]["archive_sha256"], result["runtime"]["archive_sha256"])

    def test_refresh_failure_is_explicit_and_retry_repairs_selected_release(self) -> None:
        a = self.release("a")
        self.call(a, "repo", "install", "--project-name", "Demo", "--json")
        real_run = subprocess.run
        def interrupt_refresh(argv, **kwargs):
            if argv[1:3] == ["repo", "refresh"]:
                return subprocess.CompletedProcess(argv, 1, "", "simulated write failure")
            return real_run(argv, **kwargs)
        with patch("blackdog.repo_lifecycle.subprocess.run", side_effect=interrupt_refresh):
            with self.assertRaisesRegex(RepoLifecycleError, "Runtime selection was updated.*did not complete"):
                update_repo(self.root, source_root=str(REPO_ROOT))
        selected = runtime.installed_runtime(load_profile(self.root))
        self.assertIsNotNone(selected)
        result = update_repo(self.root, source_root=str(REPO_ROOT))
        self.assertEqual(result.action, "update")
        self.assertNotIn("delivery a", (self.root / ".codex/skills/demo/references/engineering.md").read_text())
        self.assertEqual(runtime.installed_runtime(load_profile(self.root)), selected)
