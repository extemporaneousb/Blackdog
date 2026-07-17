from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Iterable

from blackdog_core.state import PromptReceiptRecord


PROMPT_ARTIFACT_MAX_BYTES = 1_048_576
PROMPT_ARTIFACT_ROOT = Path("prompts") / "sha256"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PromptArtifactError(RuntimeError):
    """A bounded prompt-artifact persistence or verification failure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


def prompt_artifact_relative_path(prompt_hash: str) -> str:
    resolved_hash = str(prompt_hash or "").strip()
    if _SHA256_RE.fullmatch(resolved_hash) is None:
        raise PromptArtifactError(
            "prompt_artifact_hash_invalid",
            "Prompt replay artifacts require a lowercase SHA-256 prompt hash.",
        )
    return (PROMPT_ARTIFACT_ROOT / f"{resolved_hash}.txt").as_posix()


def _validated_receipt_bytes(receipt: PromptReceiptRecord) -> bytes:
    if receipt.text is None:
        raise PromptArtifactError(
            "prompt_artifact_text_missing",
            "A prompt replay artifact can only be created from normalized prompt text.",
        )
    normalized = str(receipt.text).strip()
    if not normalized:
        raise PromptArtifactError(
            "prompt_artifact_text_missing",
            "A prompt replay artifact cannot contain an empty prompt.",
        )
    if normalized != receipt.text:
        raise PromptArtifactError(
            "prompt_artifact_text_not_normalized",
            "Prompt replay artifact text must match the normalized prompt receipt.",
        )
    data = normalized.encode("utf-8")
    if len(data) > PROMPT_ARTIFACT_MAX_BYTES:
        raise PromptArtifactError(
            "prompt_artifact_oversized",
            f"Normalized prompt text exceeds the {PROMPT_ARTIFACT_MAX_BYTES}-byte replay artifact limit.",
        )
    observed_hash = hashlib.sha256(data).hexdigest()
    if observed_hash != receipt.prompt_hash:
        raise PromptArtifactError(
            "prompt_artifact_hash_mismatch",
            "Prompt receipt text does not match its recorded hash.",
        )
    return data


def _artifact_path(
    control_dir: Path,
    *,
    prompt_hash: str,
    replay_artifact_path: str | None = None,
) -> tuple[Path, str]:
    expected_relative = prompt_artifact_relative_path(prompt_hash)
    relative = str(replay_artifact_path or expected_relative).strip()
    candidate_relative = Path(relative)
    if candidate_relative.is_absolute() or candidate_relative.as_posix() != expected_relative:
        raise PromptArtifactError(
            "prompt_artifact_path_invalid",
            "Prompt replay artifact path does not match its content-addressed location.",
        )
    resolved_control = Path(control_dir).expanduser().resolve(strict=False)
    candidate = resolved_control / candidate_relative
    try:
        resolved_parent = candidate.parent.resolve(strict=False)
        resolved_parent.relative_to(resolved_control)
    except (OSError, ValueError) as exc:
        raise PromptArtifactError(
            "prompt_artifact_path_invalid",
            "Prompt replay artifact path escapes the configured control directory.",
        ) from exc
    return candidate, expected_relative


def _read_regular_private_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise PromptArtifactError(
            "prompt_artifact_missing",
            f"Prompt replay artifact is missing: {path}",
        ) from exc
    except OSError as exc:
        raise PromptArtifactError(
            "prompt_artifact_unreadable",
            f"Prompt replay artifact cannot be opened: {path}: {exc}",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PromptArtifactError(
                "prompt_artifact_not_regular",
                f"Prompt replay artifact is not a regular file: {path}",
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise PromptArtifactError(
                "prompt_artifact_permissions_invalid",
                f"Prompt replay artifact permissions must be 0600: {path}",
            )
        if metadata.st_size > PROMPT_ARTIFACT_MAX_BYTES:
            raise PromptArtifactError(
                "prompt_artifact_oversized",
                f"Prompt replay artifact exceeds the {PROMPT_ARTIFACT_MAX_BYTES}-byte limit: {path}",
            )
        chunks: list[bytes] = []
        remaining = PROMPT_ARTIFACT_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > PROMPT_ARTIFACT_MAX_BYTES:
            raise PromptArtifactError(
                "prompt_artifact_oversized",
                f"Prompt replay artifact exceeds the {PROMPT_ARTIFACT_MAX_BYTES}-byte limit: {path}",
            )
        return data
    except OSError as exc:
        raise PromptArtifactError(
            "prompt_artifact_unreadable",
            f"Prompt replay artifact cannot be read: {path}: {exc}",
        ) from exc
    finally:
        os.close(descriptor)


def verify_prompt_artifact(
    control_dir: Path,
    *,
    prompt_hash: str,
    replay_artifact_path: str,
) -> Path:
    path, _relative = _artifact_path(
        control_dir,
        prompt_hash=prompt_hash,
        replay_artifact_path=replay_artifact_path,
    )
    data = _read_regular_private_file(path)
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise PromptArtifactError(
            "prompt_artifact_encoding_invalid",
            f"Prompt replay artifact is not valid UTF-8: {path}",
        ) from exc
    if not text or text != text.strip():
        raise PromptArtifactError(
            "prompt_artifact_text_not_normalized",
            f"Prompt replay artifact does not contain normalized prompt text: {path}",
        )
    if hashlib.sha256(data).hexdigest() != prompt_hash:
        raise PromptArtifactError(
            "prompt_artifact_hash_mismatch",
            f"Prompt replay artifact content does not match its recorded hash: {path}",
        )
    return path.resolve()


def _ensure_private_directory(path: Path, *, control_dir: Path) -> None:
    descriptor: int | None = None
    try:
        resolved_control = control_dir.resolve(strict=False)
        relative = path.relative_to(resolved_control)
        resolved_control.mkdir(parents=True, exist_ok=True)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved_control, directory_flags)
        for part in relative.parts:
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            try:
                metadata = os.fstat(next_descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise OSError(f"artifact directory component is not a directory: {part}")
                os.fchmod(next_descriptor, 0o700)
            except OSError:
                os.close(next_descriptor)
                raise
            os.close(descriptor)
            descriptor = next_descriptor
    except (OSError, ValueError) as exc:
        raise PromptArtifactError(
            "prompt_artifact_directory_unavailable",
            f"Prompt replay artifact directory is unavailable: {path}: {exc}",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _persist_validated_receipt(
    control_dir: Path,
    receipt: PromptReceiptRecord,
    data: bytes,
) -> PromptReceiptRecord:
    target, relative = _artifact_path(control_dir, prompt_hash=receipt.prompt_hash)
    artifact_dir = target.parent
    _ensure_private_directory(artifact_dir, control_dir=Path(control_dir))
    if target.exists() or target.is_symlink():
        verify_prompt_artifact(
            control_dir,
            prompt_hash=receipt.prompt_hash,
            replay_artifact_path=relative,
        )
        return replace(receipt, replay_artifact_path=relative)

    descriptor: int | None = None
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".prompt-", dir=artifact_dir)
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_path, target, follow_symlinks=False)
        except FileExistsError:
            pass
        directory_descriptor = os.open(artifact_dir, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise PromptArtifactError(
            "prompt_artifact_write_failed",
            f"Prompt replay artifact could not be persisted: {target}: {exc}",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    verify_prompt_artifact(
        control_dir,
        prompt_hash=receipt.prompt_hash,
        replay_artifact_path=relative,
    )
    return replace(receipt, replay_artifact_path=relative)


def persist_prompt_receipts(
    control_dir: Path,
    receipts: Iterable[PromptReceiptRecord],
) -> tuple[PromptReceiptRecord, ...]:
    resolved_receipts = tuple(receipts)
    validated = tuple(_validated_receipt_bytes(receipt) for receipt in resolved_receipts)
    return tuple(
        _persist_validated_receipt(Path(control_dir), receipt, data)
        for receipt, data in zip(resolved_receipts, validated, strict=True)
    )


__all__ = [
    "PROMPT_ARTIFACT_MAX_BYTES",
    "PROMPT_ARTIFACT_ROOT",
    "PromptArtifactError",
    "persist_prompt_receipts",
    "prompt_artifact_relative_path",
    "verify_prompt_artifact",
]
