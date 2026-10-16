from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import shutil
from pathlib import Path
import tempfile
from unittest.mock import patch

from blackdog.contract import managed_skill_relative_path
from blackdog.guidance import add_guidance, inspect_guidance, rendered_guidance, resolve_guidance
from blackdog.prompting import preview_prompt
from blackdog.repo_lifecycle import _write_repo_skill
from blackdog_cli.main import main
from blackdog_core.profile import ConfigError, guidance_configs, load_profile
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class GuidanceCatalogTests(CoreAuditTestCase):
    def setUp(self):
        super().setUp()
        self.write_profile("Guidance Fixture")
        self.profile = self.load_test_profile()
        _write_repo_skill(self.profile, overwrite=True)
        self.local = self.root / "team guidance.md"
        self.content = "# Team convention\r\nPreserve café evidence.\r\n\r\n"
        self.local.write_bytes(self.content.encode())

    def cli(self, *args, ok=True):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            status = main(["guidance", *args, "--project-root", str(self.root)])
        self.assertEqual(status, 0 if ok else 1, error.getvalue())
        return output.getvalue(), error.getvalue()

    def add(self, **kwargs):
        options = dict(name="release", path=self.local.name, summary="Review selected release evidence.",
                       cues=("Check this release",))
        options.update(kwargs)
        return add_guidance(self.profile, **options)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def test_list_and_show_are_read_only_including_invalid_or_missing_sources(self):
        guide = self.root / ".codex/skills/guidance-fixture/references/cleanup.md"
        guide.unlink()
        shutil.rmtree(self.profile.paths.control_dir)
        before = self.snapshot()
        output, _ = self.cli("list", "--json")
        value = json.loads(output)["guidance"]
        self.assertEqual(value["source"], "installed_repository_files")
        row = next(row for row in value["guides"] if row["name"] == "cleanup")
        self.assertEqual(row["status"], "unavailable")
        self.cli("show", "cleanup", ok=False)
        self.cli("show", ok=False)
        self.cli("show", "cleanup", "--all", ok=False)
        self.assertEqual(before, self.snapshot())

    def test_show_uses_exact_installed_bytes_and_reports_drift_without_substitution(self):
        guide = self.root / ".codex/skills/guidance-fixture/references/cleanup.md"
        guide.write_bytes(self.content.encode())
        output, context = self.cli("show", "cleanup")
        self.assertEqual(output, self.content)
        self.assertIn("different_from_runtime", context)
        preview = preview_prompt(self.profile, request="Tidy", include_prompt=True, guidance=("cleanup",))
        self.assertIn(json.dumps(self.content, ensure_ascii=False), preview.composed_prompt)
        shown = inspect_guidance(self.profile, ("cleanup",), include_text=True)["guides"][0]
        self.assertEqual(shown["sha256"], preview.guidance[0]["sha256"])
        self.assertEqual(shown["sha256"], hashlib.sha256(output.encode()).hexdigest())
        by_path = inspect_guidance(self.profile, (guide.relative_to(self.root).as_posix(),), include_text=True)["guides"][0]
        self.assertEqual(by_path, shown)

    def test_registered_name_shares_resolution_and_survives_refresh_without_prompt_copies(self):
        original_profile = self.profile.paths.profile_file.read_bytes()
        receipt = self.add()
        self.profile = load_profile(self.root, read_only=True)
        self.assertEqual(receipt["path"], self.local.name)
        self.assertTrue(self.profile.paths.profile_file.read_bytes().startswith(original_profile))
        self.assertNotIn("Preserve café", self.profile.paths.profile_file.read_text())
        shown = inspect_guidance(self.profile, ("release",), include_text=True)["guides"][0]
        resolved, = resolve_guidance(self.profile, ("release",))
        self.assertEqual(shown["text"], self.content)
        self.assertEqual(shown["sha256"], resolved.sha256)
        catalog = self.root / managed_skill_relative_path(self.profile).parent / "references/catalog.md"
        self.assertIn("team%20guidance.md", catalog.read_text())
        self.assertIn("Check this release", catalog.read_text())
        self.assertNotIn("Preserve café", catalog.read_text())
        _write_repo_skill(self.profile, overwrite=True)
        self.assertEqual(self.local.read_bytes(), self.content.encode())
        self.assertEqual(inspect_guidance(self.profile)["catalog"]["status"], "current")
        self.assertFalse(self.profile.paths.runtime_file.exists())
        self.assertFalse(self.profile.paths.events_file.exists())

    def test_add_never_replaces_an_existing_name_or_aliases_a_registered_file(self):
        self.add()
        for kwargs in ({}, {"name": "engineering"}, {"name": "alias"}):
            with self.subTest(kwargs=kwargs):
                before = self.profile.paths.profile_file.read_bytes()
                with self.assertRaises(ValueError):
                    self.add(**kwargs)
                self.assertEqual(self.profile.paths.profile_file.read_bytes(), before)
                self.assertEqual(self.local.read_bytes(), self.content.encode())
        profile = load_profile(self.root)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            resolve_guidance(profile, ("release", self.local.name))

    def test_add_refuses_missing_escaping_and_managed_sources_before_registration(self):
        for name in ("blackdog", "old-managed"):
            directory = self.root / ".codex/skills" / name
            directory.mkdir()
            (directory / "ref.md").write_text("must survive refresh")
            if name == "old-managed":
                (directory / ".blackdog-managed.json").write_text("{}")
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "guide.md"
            target.write_text("outside")
            (self.root / "escape.md").symlink_to(target)
            for path in ("absent.md", "../outside.md", str(target), "escape.md",
                         ".codex/skills/guidance-fixture/references/cleanup.md",
                         ".codex/skills/blackdog/ref.md", ".codex/skills/old-managed/ref.md"):
                with self.subTest(path=path):
                    before = self.profile.paths.profile_file.read_bytes()
                    with self.assertRaises((ConfigError, ValueError)):
                        self.add(path=path)
                    self.assertEqual(self.profile.paths.profile_file.read_bytes(), before)
        self.assertFalse(self.profile.paths.runtime_file.exists())

    def test_invalid_metadata_is_rejected_and_table_format_round_trips(self):
        for raw in ({}, [{"name": "release"}], [{"name": "a/b", "path": "x.md", "summary": "x", "cues": ["x"]}],
                    [{"name": "release", "path": "x.md", "summary": "x", "cues": []}]):
            with self.subTest(raw=raw), self.assertRaises(ConfigError):
                guidance_configs(raw)
        summary = 'Check "quoted" evidence \\ bounds.'
        self.add(summary=summary, cues=('Explain "this"',))
        self.assertEqual(load_profile(self.root).guidance[0].summary, summary)

    def test_catalog_drift_is_explicit_and_show_all_has_the_same_small_index(self):
        catalog = self.root / managed_skill_relative_path(self.profile).parent / "references/catalog.md"
        catalog.write_text("stale index")
        result = inspect_guidance(self.profile)
        self.assertEqual(result["catalog"]["status"], "different_from_runtime")
        output, _ = self.cli("show", "--all", "--json")
        shown = json.loads(output)["guidance"]
        self.assertEqual([r["name"] for r in result["guides"]], [r["name"] for r in shown["guides"]])
        self.assertTrue(all(r["text"] for r in shown["guides"]))

    def test_partial_index_write_failure_keeps_registration_and_can_refresh(self):
        from blackdog import guidance
        original = guidance.atomic_write_text
        def write(path, text):
            if path.name == "catalog.md":
                raise OSError("fixture write failure")
            original(path, text)
        with patch.object(guidance, "atomic_write_text", side_effect=write):
            with self.assertRaisesRegex(ValueError, "registered but agent catalog refresh failed"):
                self.add()
        profile = load_profile(self.root)
        self.assertEqual(profile.guidance[0].name, "release")
        _write_repo_skill(profile, overwrite=True)
        self.assertEqual(inspect_guidance(profile)["catalog"]["status"], "current")
        self.assertEqual(self.local.read_bytes(), self.content.encode())

    def test_checked_in_guidance_is_generated_from_the_live_definitions(self):
        profile = load_profile(REPO_ROOT, read_only=True)
        for relative, expected in rendered_guidance(profile).items():
            with self.subTest(path=relative):
                self.assertEqual((REPO_ROOT / relative).read_bytes(), expected.encode())

    def test_add_cli_and_inline_toml_layout_do_not_corrupt_existing_configuration(self):
        output, _ = self.cli("add", "release", "--path", self.local.name,
                             "--summary", "Review release evidence.", "--cue", "Check this release", "--json")
        self.assertEqual(json.loads(output)["guidance"]["name"], "release")
        self.cli("show", "release")
        self.write_profile("Guidance Fixture")
        profile = self.profile.paths.profile_file
        profile.write_text("guidance = []\n" + profile.read_text())
        before = profile.read_bytes()
        with self.assertRaisesRegex(ValueError, "inline array"):
            self.add()
        self.assertEqual(profile.read_bytes(), before)
