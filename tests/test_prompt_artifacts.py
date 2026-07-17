from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from blackdog.prompt_artifacts import (
    PROMPT_ARTIFACT_MAX_BYTES,
    PROMPT_ARTIFACT_ROOT,
    PromptArtifactError,
    persist_prompt_receipts,
    verify_prompt_artifact,
)
from blackdog_core.state import (
    PROMPT_MODE_RAW,
    PROMPT_MODE_SKILL,
    PROMPT_MODE_TUNED,
    create_prompt_receipt,
)


class PromptArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.control_dir = Path(self.tmp.name) / "shared-control"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_content_address_is_private_deduplicated_and_mode_preserving(self) -> None:
        receipts = tuple(
            create_prompt_receipt(
                "  Preserve this normalized private prompt.\n",
                source=f"{mode}.md",
                mode=mode,
            )
            for mode in (PROMPT_MODE_RAW, PROMPT_MODE_SKILL, PROMPT_MODE_TUNED)
        )
        persisted = persist_prompt_receipts(self.control_dir, receipts)

        self.assertEqual(
            [receipt.mode for receipt in persisted],
            [PROMPT_MODE_RAW, PROMPT_MODE_SKILL, PROMPT_MODE_TUNED],
        )
        self.assertEqual(len({receipt.replay_artifact_path for receipt in persisted}), 1)
        relative = persisted[0].replay_artifact_path
        self.assertIsNotNone(relative)
        assert relative is not None
        artifact = self.control_dir / relative
        self.assertEqual(
            artifact.read_text(encoding="utf-8"),
            "Preserve this normalized private prompt.",
        )
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
        self.assertEqual(artifact.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(artifact.parent.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            verify_prompt_artifact(
                self.control_dir,
                prompt_hash=persisted[0].prompt_hash,
                replay_artifact_path=relative,
            ),
            artifact.resolve(),
        )

    def test_existing_content_is_verified_and_never_overwritten(self) -> None:
        receipt = create_prompt_receipt("Immutable replay content.")
        persisted = persist_prompt_receipts(self.control_dir, (receipt,))[0]
        assert persisted.replay_artifact_path is not None
        artifact = self.control_dir / persisted.replay_artifact_path
        artifact.write_text("tampered", encoding="utf-8")

        with self.assertRaisesRegex(PromptArtifactError, "does not match") as raised:
            persist_prompt_receipts(self.control_dir, (receipt,))
        self.assertEqual(raised.exception.code, "prompt_artifact_hash_mismatch")
        self.assertEqual(artifact.read_text(encoding="utf-8"), "tampered")

    def test_oversized_batch_is_rejected_before_any_artifact_write(self) -> None:
        small = create_prompt_receipt("small request")
        oversized = create_prompt_receipt("x" * (PROMPT_ARTIFACT_MAX_BYTES + 1))

        with self.assertRaises(PromptArtifactError) as raised:
            persist_prompt_receipts(self.control_dir, (small, oversized))
        self.assertEqual(raised.exception.code, "prompt_artifact_oversized")
        self.assertFalse((self.control_dir / PROMPT_ARTIFACT_ROOT).exists())

    def test_replay_path_must_be_the_hash_derived_control_relative_path(self) -> None:
        receipt = persist_prompt_receipts(
            self.control_dir,
            (create_prompt_receipt("Bounded replay path."),),
        )[0]
        with self.assertRaises(PromptArtifactError) as raised:
            verify_prompt_artifact(
                self.control_dir,
                prompt_hash=receipt.prompt_hash,
                replay_artifact_path="../../outside.txt",
            )
        self.assertEqual(raised.exception.code, "prompt_artifact_path_invalid")

        forged = replace(receipt, replay_artifact_path="prompts/sha256/not-the-hash.txt")
        with self.assertRaises(PromptArtifactError) as forged_error:
            verify_prompt_artifact(
                self.control_dir,
                prompt_hash=forged.prompt_hash,
                replay_artifact_path=str(forged.replay_artifact_path),
            )
        self.assertEqual(forged_error.exception.code, "prompt_artifact_path_invalid")

    def test_symlinked_artifact_directory_cannot_escape_control_storage(self) -> None:
        self.control_dir.mkdir(parents=True)
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (self.control_dir / "prompts").symlink_to(outside, target_is_directory=True)

        with self.assertRaises(PromptArtifactError):
            persist_prompt_receipts(
                self.control_dir,
                (create_prompt_receipt("Never write this outside control storage."),),
            )
        self.assertFalse((outside / "sha256").exists())
        self.assertEqual(tuple(outside.iterdir()), ())


if __name__ == "__main__":
    unittest.main()
