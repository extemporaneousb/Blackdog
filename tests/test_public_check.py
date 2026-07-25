from __future__ import annotations

import hashlib
import subprocess

from scripts.public_check import (
    BUILTIN_HASHED_MARKERS,
    HashedMarker,
    scan_bytes,
    scan_repository,
)
from tests.core_audit_support import CoreAuditTestCase, REPO_ROOT


class PublicContentCheckTests(CoreAuditTestCase):
    def test_current_repository_passes(self) -> None:
        violations, scanned = scan_repository(REPO_ROOT)

        self.assertGreater(scanned, 0)
        self.assertEqual(violations, ())

    def test_builtin_private_marker_is_rejected_without_storing_it_in_fixture_text(self) -> None:
        private_marker = bytes((117, 116, 116, 101, 114))

        violations = scan_bytes(
            b"prefix-" + private_marker + b"-suffix",
            path="fixture.txt",
        )

        self.assertEqual(
            {(row.line, row.rule) for row in violations},
            {(1, "private-marker-01")},
        )

    def test_custom_marker_and_local_term_are_rejected(self) -> None:
        custom_term = b"sensitive-client"
        marker = HashedMarker(
            rule="custom-private-marker",
            length=len(custom_term),
            sha256=hashlib.sha256(custom_term).hexdigest(),
        )

        marker_violations = scan_bytes(
            b"prefix " + custom_term,
            path="fixture.txt",
            markers=(marker,),
        )
        local_violations = scan_bytes(
            b"prefix " + custom_term,
            path="fixture.txt",
            markers=(),
            local_terms=(custom_term,),
        )

        self.assertEqual([row.rule for row in marker_violations], ["custom-private-marker"])
        self.assertEqual([row.rule for row in local_violations], ["local-denylist-01"])

    def test_personal_home_path_and_non_example_email_are_rejected(self) -> None:
        personal_path = "/" + "Users" + "/" + "alice" + "/project"
        email = "person" + "@" + "company.com"

        violations = scan_bytes(
            f"{personal_path}\n{email}\nblackdog@example.com\n".encode(),
            path="fixture.txt",
            markers=(),
        )

        self.assertEqual(
            {(row.line, row.rule) for row in violations},
            {
                (1, "personal-home-path"),
                (2, "non-example-email"),
            },
        )

    def test_generated_history_export_is_rejected(self) -> None:
        history = self.root / ".blackdog" / "history.jsonl"
        history.parent.mkdir()
        history.write_text("{}\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "-f", ".blackdog/history.jsonl"],
            check=True,
            capture_output=True,
            text=True,
        )

        violations, _ = scan_repository(self.root, markers=())

        self.assertEqual(
            {(row.path, row.rule) for row in violations},
            {(".blackdog/history.jsonl", "generated-private-export")},
        )

    def test_builtin_marker_configuration_uses_only_digests(self) -> None:
        self.assertEqual({marker.length for marker in BUILTIN_HASHED_MARKERS}, {3, 5})
        self.assertTrue(
            all(
                len(marker.sha256) == 64
                and marker.sha256 == marker.sha256.lower()
                and all(character in "0123456789abcdef" for character in marker.sha256)
                for marker in BUILTIN_HASHED_MARKERS
            )
        )
