from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile

from blackdog.contract import managed_skill_relative_path
from blackdog.guidance import (
    MAX_GUIDANCE_DOCUMENT_BYTES,
    guidance_relative_paths,
    rendered_guidance,
    resolve_guidance,
)
from blackdog.repo_lifecycle import (
    RepoLifecycleError,
    _write_repo_agents,
    _write_repo_skill,
    render_repo_skill,
)
from tests.core_audit_support import CoreAuditTestCase


class GuidanceDeliveryTests(CoreAuditTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write_profile("Guidance Fixture")
        self.profile = self.load_test_profile()

    def install_guidance(self) -> None:
        _write_repo_agents(self.profile)
        _write_repo_skill(self.profile, overwrite=False)

    def test_install_makes_automatic_catalog_and_all_references_available(self) -> None:
        self.install_guidance()
        skill_dir = managed_skill_relative_path(self.profile).parent
        catalog = skill_dir / "references" / "catalog.md"
        agents = (self.root / "AGENTS.md").read_text()
        self.assertIn(catalog.as_posix(), agents)
        self.assertIn("automatically", agents)
        self.assertIn("quoted roles are not assignments", agents)
        self.assertIn("analysis does not authorize implementation", agents)
        self.assertIn("guidance_args", agents)
        text = (self.root / catalog).read_text()
        for name, path in guidance_relative_paths(self.profile).items():
            self.assertIn(f"]({name}.md)", text)
            document, = resolve_guidance(self.profile, (name,))
            self.assertEqual(document.path, path.as_posix())
            self.assertTrue(document.text.startswith("# "))
        self.assertLessEqual(len(render_repo_skill(self.profile).splitlines()), 26)

    def test_install_preserves_existing_skill_but_delivers_missing_references(self) -> None:
        skill = self.root / managed_skill_relative_path(self.profile)
        skill.parent.mkdir(parents=True)
        skill.write_text("existing caller instructions\n")
        self.install_guidance()
        self.assertEqual(skill.read_text(), "existing caller instructions\n")
        self.assertEqual(len(resolve_guidance(self.profile, ("engineering", "evidence"))), 2)
        self.assertTrue((skill.parent / "references" / "catalog.md").is_file())

    def test_refresh_restores_managed_guidance_and_preserves_repository_refinement(self) -> None:
        self.install_guidance()
        local = self.root / "docs" / "team-guidance.md"
        local.parent.mkdir()
        local.write_text("Repository owned requirements.\n")
        agents = self.root / "AGENTS.md"
        agents.write_text("# Local instructions\nUse docs/team-guidance.md.\n\n" + agents.read_text())
        guide = self.root / guidance_relative_paths(self.profile)["cleanup"]
        guide.write_text("unreviewed edit\n")
        stale = guide.parent / "retired.md"
        stale.write_text("obsolete generated reference\n")
        _write_repo_agents(self.profile)
        result = _write_repo_skill(self.profile, overwrite=True)
        self.assertIn(guide.resolve(), result.changed)
        self.assertIn(stale.resolve(), result.removed)
        self.assertFalse(stale.exists())
        self.assertEqual(local.read_text(), "Repository owned requirements.\n")
        self.assertIn("Use docs/team-guidance.md.", agents.read_text())
        for relative, expected in rendered_guidance(self.profile).items():
            self.assertEqual((self.root / relative).read_text(), expected)
        again = _write_repo_skill(self.profile, overwrite=True)
        self.assertEqual(again.changed, ())
        self.assertEqual(again.removed, ())

    def test_resolution_preserves_exact_bytes_and_metadata_omits_text(self) -> None:
        content = "# Local\r\nA measured café convention.\r\n".encode("utf-8")
        local = self.root / "local.md"
        local.write_bytes(content)
        document, = resolve_guidance(self.profile, ("local.md",))
        self.assertEqual(document.text.encode("utf-8"), content)
        self.assertEqual(document.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual(document.to_dict(), {"path": "local.md", "sha256": document.sha256})
        local.write_text("new convention\n")
        self.assertEqual(document.text.encode("utf-8"), content)

    def test_delivery_refuses_an_escaping_reference_before_writing_skill_files(self) -> None:
        skill = self.root / managed_skill_relative_path(self.profile)
        skill.parent.mkdir(parents=True)
        with tempfile.TemporaryDirectory() as outside:
            (skill.parent / "references").symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaisesRegex(RepoLifecycleError, "escapes"):
                _write_repo_skill(self.profile, overwrite=True)
            self.assertFalse(skill.exists())
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_missing_or_invalid_selection_does_not_mutate_any_file(self) -> None:
        before = self.git_output("status", "--porcelain=v1", "--untracked-files=all")
        for selector in ("engineering", "missing.md", "../outside.md", str(self.root / "absolute.md"), "", " engineering", "."):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                resolve_guidance(self.profile, (selector,))
        self.assertEqual(self.git_output("status", "--porcelain=v1", "--untracked-files=all"), before)

    def test_symlink_escape_and_duplicate_aliases_are_refused(self) -> None:
        self.install_guidance()
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "private.md"
            target.write_text("outside convention\n")
            (self.root / "escape.md").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "contained"):
                resolve_guidance(self.profile, ("escape.md",))
        relative = guidance_relative_paths(self.profile)["engineering"]
        (self.root / "alias.md").symlink_to(self.root / relative)
        for selectors in (("engineering", "engineering"), ("engineering", relative.as_posix()), ("engineering", "alias.md")):
            with self.subTest(selectors=selectors), self.assertRaisesRegex(ValueError, "duplicate"):
                resolve_guidance(self.profile, selectors)

    def test_empty_invalid_utf8_directory_and_oversized_guidance_are_refused(self) -> None:
        fixtures = {"empty.md": b" \n", "binary.md": b"\xff", "large.md": b"a" * (MAX_GUIDANCE_DOCUMENT_BYTES + 1)}
        for name, content in fixtures.items():
            (self.root / name).write_bytes(content)
            with self.subTest(name=name), self.assertRaises(ValueError):
                resolve_guidance(self.profile, (name,))
        (self.root / "folder").mkdir()
        with self.assertRaises(ValueError):
            resolve_guidance(self.profile, ("folder",))

    def test_selection_metadata_rejects_control_characters_and_oversized_paths(self) -> None:
        (self.root / "guide\nname.md").write_text("A valid body with an invalid metadata path.\n")
        for selector in ("guide\nname.md", "x" * 1025):
            with self.subTest(selector=selector), self.assertRaisesRegex(ValueError, "bounded text"):
                resolve_guidance(self.profile, (selector,))
        (self.root / "guide.md").write_text("Normal repository guidance.\n")
        document, = resolve_guidance(self.profile, ("./guide.md",))
        self.assertEqual(document.path, "guide.md")

    def test_selection_has_document_count_and_total_size_bounds(self) -> None:
        selectors = []
        for index in range(5):
            selector = f"guide-{index}.md"
            (self.root / selector).write_bytes(b"a" * MAX_GUIDANCE_DOCUMENT_BYTES)
            selectors.append(selector)
        with self.assertRaisesRegex(ValueError, "selected guidance exceeds"):
            resolve_guidance(self.profile, tuple(selectors))
        with self.assertRaisesRegex(ValueError, "at most"):
            resolve_guidance(self.profile, tuple("missing.md" for _ in range(17)))
