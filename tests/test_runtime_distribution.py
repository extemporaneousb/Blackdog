from __future__ import annotations

import hashlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
import zipfile

from blackdog import runtime_distribution as runtime
from blackdog_core.profile import load_profile
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class RuntimeDistributionTests(CoreAuditTestCase):
    def run_release(self, executable: Path, *args: str, cwd: Path | None = None) -> dict:
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        result = subprocess.run(
            [str(executable), *args], cwd=cwd or self.root, env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def build_release(self, name: str = "release with spaces.pyz") -> Path:
        path = Path(self.tmp.name) / name
        runtime.write_release(path, source_root=REPO_ROOT)
        return path

    def test_release_is_reproducible_and_contains_versioned_source_manifest(self) -> None:
        first = runtime.release_bytes(source_root=REPO_ROOT)
        second = runtime.release_bytes(source_root=REPO_ROOT)
        self.assertEqual(first, second)
        with zipfile.ZipFile(io.BytesIO(first)) as archive:
            manifest = json.loads(archive.read("blackdog-release.json"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["requires_python"], ">=3.11")
            self.assertIn(b"MIT License", archive.read("LICENSE"))
            self.assertEqual(len(manifest["source_sha256"]), 64)
            self.assertNotIn("tests", {Path(name).parts[0] for name in archive.namelist()})

    def test_archive_install_begin_and_cleanup_survive_original_archive_deletion(self) -> None:
        artifact = self.build_release()
        installed = self.run_release(artifact, "repo", "install", "--project-root", str(self.root), "--json")["repo"]
        executable = Path(installed["blackdog_path"])
        self.assertFalse((self.root / ".VE").exists())
        self.assertEqual(hashlib.sha256(executable.read_bytes()).hexdigest(), executable.stem)
        artifact.unlink()
        self.git_output("add", "blackdog.toml", "AGENTS.md", ".codex")
        self.git_output("commit", "-m", "Install standalone runtime")
        begun = self.run_release(
            executable, "task", "begin", "--project-root", str(self.root),
            "--execution-prompt", "Public runtime acceptance", "--request", "Public runtime acceptance", "--json",
        )["task"]
        task_root = Path(begun["worktree_path"])
        self.assertFalse((task_root / ".VE").exists())
        self.assertEqual(begun["setup_receipt"]["workspace_blackdog_path"], str(executable))
        closed = self.run_release(
            executable, "task", "close", "--project-root", str(self.root),
            "--task", begun["task_id"], "--status", "abandoned", "--summary", "Acceptance complete",
            "--validation", "runtime=passed", "--cleanup", "--json", cwd=task_root,
        )["closure"]
        while closed["next_action"]["kind"] == "command":
            argv = closed["next_action"]["argv"]
            self.assertEqual(Path(argv[0]), executable)
            closed = self.run_release(Path(argv[0]), *argv[1:])["closure"]
        self.assertEqual(closed["next_action"]["kind"], "complete")
        self.assertFalse(task_root.exists())
        self.run_release(executable, "summary", "--project-root", str(self.root), "--json")

    def test_update_retains_old_digest_pinned_executable(self) -> None:
        first = self.build_release()
        installed = self.run_release(first, "repo", "install", "--project-root", str(self.root), "--json")["repo"]
        original = Path(installed["blackdog_path"])
        entries = runtime._package_entries(REPO_ROOT)
        entries["blackdog/__init__.py"] = entries["blackdog/__init__.py"].replace(b'"0.1.0"', b'"0.1.0.test"')
        with patch.object(runtime, "_package_entries", return_value=entries):
            newer = self.build_release("updated release.pyz")
        updated = self.run_release(newer, "repo", "update", "--project-root", str(self.root), "--json")["repo"]
        selected = Path(updated["blackdog_path"])
        self.assertNotEqual(selected, original)
        self.assertTrue(original.is_file())
        first.unlink()
        newer.unlink()
        self.run_release(original, "summary", "--project-root", str(self.root), "--json")
        self.run_release(selected, "summary", "--project-root", str(self.root), "--json")

    def test_archive_digest_validation_is_shared_with_profile_free_recovery(self) -> None:
        self.write_profile()
        profile = load_profile(self.root)
        archive = runtime.install_runtime(profile)
        archive.write_bytes(archive.read_bytes() + b"corrupt")
        for kwargs in ({}, {"profile": profile}):
            with self.subTest(kwargs=bool(kwargs)), self.assertRaises(runtime.RuntimeDistributionError):
                runtime.runtime_executable(self.root, **kwargs)

    def test_project_python_handler_is_optional_and_preserved_when_requested(self) -> None:
        self.write_profile()
        profile_path = self.root / "blackdog.toml"
        profile_path.write_text(profile_path.read_text(encoding="utf-8") + '''
[[handlers]]
id = "python"
kind = "python-overlay-venv"
root_path = ".VE"
worktree_path = ".VE"
script_policy = "root-bin-fallback"
''', encoding="utf-8")
        (self.root / ".gitignore").write_text(".VE/\n", encoding="utf-8")
        artifact = self.build_release()
        installed = self.run_release(artifact, "repo", "install", "--project-root", str(self.root), "--json")["repo"]
        executable = Path(installed["blackdog_path"])
        marker = self.root / ".VE" / "retained-project-environment"
        marker.write_text("project-owned", encoding="utf-8")
        self.run_release(artifact, "repo", "update", "--project-root", str(self.root), "--json")
        self.assertEqual(marker.read_text(encoding="utf-8"), "project-owned")
        self.assertEqual(profile_path.read_text(encoding="utf-8").count('kind = "python-overlay-venv"'), 1)
        artifact.unlink()
        self.git_output("add", ".gitignore", "blackdog.toml", "AGENTS.md", ".codex")
        self.git_output("commit", "-m", "Configure optional project Python environment")
        begun = self.run_release(
            executable, "task", "begin", "--project-root", str(self.root),
            "--execution-prompt", "Public project handler fixture", "--request", "Public project handler fixture", "--json",
        )["task"]
        self.assertTrue((Path(begun["worktree_path"]) / ".VE" / "bin" / "python").is_file())
        self.assertNotIn(".VE", begun["setup_receipt"]["workspace_blackdog_path"])

    def test_self_development_executes_task_source_with_standalone_recovery(self) -> None:
        (self.root / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        for relative, data in runtime._package_entries(REPO_ROOT).items():
            path = self.root / relative if relative == "LICENSE" else self.root / "src" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.root / "pyproject.toml").write_text('[project]\nname = "blackdog"\n', encoding="utf-8")
        bootstrap = self.root / "scripts" / "blackdog"
        bootstrap.parent.mkdir()
        bootstrap.write_bytes((REPO_ROOT / "scripts" / "blackdog").read_bytes())
        bootstrap.chmod(0o755)
        self.git_output("add", ".gitignore", "src", "scripts", "pyproject.toml", "LICENSE")
        self.git_output("commit", "-m", "Create public source fixture")
        artifact = self.build_release()
        self.run_release(artifact, "repo", "install", "--project-root", str(self.root), "--json")
        artifact.unlink()
        self.git_output("add", "blackdog.toml", "AGENTS.md", ".codex")
        self.git_output("commit", "-m", "Install source fixture")
        begun = self.run_release(
            bootstrap, "task", "begin", "--project-root", str(self.root),
            "--execution-prompt", "Public source fixture", "--request", "Public source fixture", "--json",
        )["task"]
        task_root = Path(begun["worktree_path"])
        task_launcher = Path(begun["setup_receipt"]["workspace_blackdog_path"])
        self.assertEqual(task_launcher, task_root / "scripts" / "blackdog")
        self.assertFalse((task_root / ".VE").exists())
        cli_source = task_root / "src" / "blackdog_cli" / "main.py"
        cli_source.write_text(cli_source.read_text(encoding="utf-8").replace(
            "from __future__ import annotations", 'from __future__ import annotations\nprint("TASK_SOURCE_MARKER")', 1,
        ), encoding="utf-8")
        probe = subprocess.run([str(task_launcher), "--help"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("TASK_SOURCE_MARKER", probe.stdout)
        stable = Path(runtime.runtime_executable(self.root, profile=load_profile(self.root)))
        self.assertFalse(stable.is_relative_to(task_root))
        self.run_release(stable, "summary", "--project-root", str(self.root), "--json")

    def test_internal_archive_directory_symlink_cannot_escape_control_root(self) -> None:
        self.write_profile()
        profile = load_profile(self.root)
        archive = runtime.install_runtime(profile)
        outside = self.root / "outside"
        archive.parent.rename(outside)
        archive.parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(runtime.RuntimeDistributionError):
            runtime.installed_runtime(profile)

    def test_first_install_rejects_symlink_publication_parents_without_external_mutation(self) -> None:
        self.write_profile()
        original = load_profile(self.root)
        for relative in ("runtime", "runtime/sha256", "bin"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory(dir=self.root) as directory:
                base = Path(directory)
                control = base / "control"
                outside = base / "outside"
                outside.mkdir()
                marker = outside / "marker"
                marker.write_bytes(b"unchanged")
                symlink = control / relative
                symlink.parent.mkdir(parents=True, exist_ok=True)
                symlink.symlink_to(outside, target_is_directory=True)
                profile = replace(original, paths=replace(original.paths, control_dir=control))
                with self.assertRaises(runtime.RuntimeDistributionError):
                    runtime.install_runtime(profile)
                self.assertEqual([path.name for path in outside.iterdir()], ["marker"])
                self.assertEqual(marker.read_bytes(), b"unchanged")
                self.assertFalse((control / "bin" / "blackdog").exists())

    def test_first_install_rejects_archive_file_symlink_before_publication(self) -> None:
        self.write_profile()
        profile = load_profile(self.root)
        digest = hashlib.sha256(runtime.release_bytes()).hexdigest()
        target = profile.paths.control_dir / "runtime" / "sha256" / f"{digest}.pyz"
        target.parent.mkdir(parents=True)
        outside = self.root / "outside-marker"
        outside.write_bytes(b"unchanged")
        target.symlink_to(outside)
        with self.assertRaises(runtime.RuntimeDistributionError):
            runtime.install_runtime(profile)
        self.assertEqual(outside.read_bytes(), b"unchanged")
        self.assertTrue(target.is_symlink())
        self.assertFalse((profile.paths.control_dir / "bin" / "blackdog").exists())

    def test_corrupt_launcher_cannot_authorize_external_runtime_publication(self) -> None:
        self.write_profile()
        profile = load_profile(self.root)
        outside = self.root / "external-runtime"
        outside.mkdir()
        marker = outside / "unrelated"
        marker.write_bytes(b"unchanged")
        (profile.paths.control_dir / "runtime").symlink_to(outside, target_is_directory=True)
        launcher = profile.paths.control_dir / "bin" / "blackdog"
        launcher.parent.mkdir()
        launcher.write_bytes(b"corrupt launcher")
        with self.assertRaises(runtime.RuntimeDistributionError):
            runtime.install_runtime(profile, update=True)
        self.assertEqual([path.name for path in outside.iterdir()], ["unrelated"])
        self.assertEqual(marker.read_bytes(), b"unchanged")
        self.assertEqual(launcher.read_bytes(), b"corrupt launcher")

    def test_distribution_manifest_traversal_is_rejected_before_file_access(self) -> None:
        outside = self.root.parent / "outside.py"
        for candidate in (self.root / "blackdog" / ".." / ".." / "outside.py", outside):
            with self.subTest(candidate=candidate), self.assertRaises(runtime.RuntimeDistributionError):
                runtime._read_package_file(candidate, self.root)

    def test_release_source_fifo_is_rejected_without_opening_it(self) -> None:
        fifo = self.root / "module.py"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(runtime.RuntimeDistributionError, "regular files"):
            runtime._read_package_file(fifo, self.root)

    def test_source_manifest_excludes_untracked_modules_and_rejects_symlinks(self) -> None:
        src = self.root / "src"
        package_files = {
            "blackdog/__init__.py": '__version__ = "1.2.3"\n',
            "blackdog/runtime_distribution.py": "# fixture\n",
            "blackdog_cli/__init__.py": "",
            "blackdog_cli/main.py": "# fixture\n",
            "blackdog_core/__init__.py": "",
        }
        for name, content in package_files.items():
            path = src / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (self.root / "LICENSE").write_text("MIT License fixture\n", encoding="utf-8")
        self.git_output("add", "src", "LICENSE")
        private = src / "blackdog" / "private_helper.py"
        private.write_text("PRIVATE_FIXTURE_CONTENT\n", encoding="utf-8")
        data = runtime.release_bytes(source_root=self.root)
        self.assertNotIn(b"PRIVATE_FIXTURE_CONTENT", data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(json.loads(archive.read("blackdog-release.json"))["version"], "1.2.3")
        included = src / "blackdog" / "runtime_distribution.py"
        included.unlink()
        included.symlink_to(private)
        with self.assertRaises(runtime.RuntimeDistributionError):
            runtime.release_bytes(source_root=self.root)
        included.unlink()
        included.write_text("# fixture\n", encoding="utf-8")
        moved = self.root / "external-source"
        src.rename(moved)
        src.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(runtime.RuntimeDistributionError):
            runtime.release_bytes(source_root=self.root)

    def test_entrypoint_rejects_unsupported_python_before_product_import(self) -> None:
        class UnsupportedPython:
            version_info = (3, 10)
        with patch.dict("sys.modules", {"sys": UnsupportedPython()}):
            with self.assertRaisesRegex(SystemExit, "Python 3.11"):
                exec(runtime._MAIN, {})

    def test_summary_does_not_import_task_or_provider_implementation(self) -> None:
        artifact = self.build_release()
        self.write_profile()
        script = (
            "import sys; sys.path.insert(0, sys.argv.pop(1)); "
            "from blackdog_cli.main import main; "
            "assert main(['summary', '--project-root', sys.argv[1], '--json']) == 0; "
            "assert 'blackdog.wtam' not in sys.modules; "
            "assert 'blackdog.codex_sessions' not in sys.modules"
        )
        completed = subprocess.run(
            ["python3", "-I", "-S", "-c", script, str(artifact), str(self.root)],
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_unbind_preserves_unconfigured_project_environment_launcher(self) -> None:
        from blackdog.repo_membership import unbind_repo
        artifact = self.build_release()
        self.run_release(artifact, "repo", "install", "--project-root", str(self.root), "--json")
        unrelated = self.root / ".VE" / "bin" / "blackdog"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("project-owned launcher\n", encoding="utf-8")
        profile = load_profile(self.root)
        stable = runtime.installed_runtime(profile)
        unbind_repo(self.root, confirm=True, keep_control_dir=True)
        self.assertTrue(unrelated.is_file())
        self.assertTrue(stable.is_file())

    def test_expected_worktree_errors_remain_bounded_cli_failures(self) -> None:
        artifact = self.build_release()
        installed = self.run_release(artifact, "repo", "install", "--project-root", str(self.root), "--json")["repo"]
        executable = installed["blackdog_path"]
        artifact.unlink()
        self.git_output("add", "blackdog.toml", "AGENTS.md", ".codex")
        self.git_output("commit", "-m", "Install worktree error fixture")
        self.git_output("checkout", "--detach", "HEAD")
        detached = subprocess.run(
            [executable, "worktree", "preflight", "--project-root", str(self.root), "--json"],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertEqual(detached.returncode, 1)
        self.assertIn("detached HEAD", detached.stderr)
        self.assertNotIn("Traceback", detached.stderr)
        self.git_output("checkout", "main")
        missing = subprocess.run(
            [executable, "task", "begin", "--project-root", str(self.root),
             "--execution-prompt", "Public missing-ref fixture", "--request", "Public missing-ref fixture",
             "--from", "nonexistent-fixture-ref", "--json"],
            cwd=self.root, capture_output=True, text=True,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertIn("could not resolve --from ref", missing.stderr)
        self.assertNotIn("Traceback", missing.stderr)
