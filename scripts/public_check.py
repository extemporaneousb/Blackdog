#!/usr/bin/env python3
"""Reject private or machine-local content from the public Git tree."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Sequence


@dataclass(frozen=True)
class HashedMarker:
    rule: str
    length: int
    sha256: str


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    rule: str


# Digests keep known private identifiers out of the public repository while
# still enforcing them in local and CI checks. Matching is ASCII
# case-insensitive and examines every fixed-length byte window.
BUILTIN_HASHED_MARKERS = (
    HashedMarker(
        rule="private-marker-01",
        length=5,
        sha256="ae19f6375b5500141978a11ee8072047fee622144258438326a1548e7324c5ac",
    ),
    HashedMarker(
        rule="private-marker-02",
        length=3,
        sha256="3d6b6df41550dc0ac702ff7674b3c8818b8284df036fd05c4091f55a8f355d62",
    ),
)

GENERATED_PRIVATE_PATHS = frozenset({".blackdog/history.jsonl"})
HOME_PATH_RE = re.compile(
    r"(?P<posix>/(?:Users|home)/[A-Za-z0-9._-]+(?=/|$|[^A-Za-z0-9._-]))"
    r"|(?P<windows>[A-Za-z]:\\Users\\[A-Za-z0-9._-]+(?=\\|$|[^A-Za-z0-9._-]))"
)
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"([A-Za-z0-9._%+-]+)@([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)"
    r"(?![A-Za-z0-9.-])"
)
PUBLIC_EXAMPLE_DOMAINS = frozenset({"example.com", "example.net", "example.org"})


def _run_git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return completed.stdout


def resolve_root(value: str | None) -> Path:
    if value:
        root = Path(value).expanduser().resolve()
    else:
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "not inside a Git repository")
        root = Path(completed.stdout.strip()).resolve()
    if not (root / ".git").exists():
        _run_git(root, "rev-parse", "--git-dir")
    return root


def candidate_paths(root: Path) -> tuple[str, ...]:
    output = _run_git(
        root,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    return tuple(
        sorted(
            {
                item.decode("utf-8", errors="surrogateescape")
                for item in output.split(b"\0")
                if item
            }
        )
    )


def load_local_terms(paths: Sequence[Path]) -> tuple[bytes, ...]:
    terms: list[bytes] = []
    for path in paths:
        if not path.is_file():
            raise RuntimeError(f"denylist file does not exist: {path}")
        for raw_line in path.read_bytes().splitlines():
            term = raw_line.strip()
            if not term or term.startswith(b"#"):
                continue
            terms.append(term.lower())
    return tuple(dict.fromkeys(terms))


def _line_number(data: bytes, offset: int) -> int:
    return data.count(b"\n", 0, offset) + 1


def _scan_hashed_markers(
    data: bytes,
    path: str,
    markers: Sequence[HashedMarker],
) -> list[Violation]:
    lowered = data.lower()
    violations: list[Violation] = []
    for marker in markers:
        if len(lowered) < marker.length:
            continue
        target = marker.sha256
        for offset in range(len(lowered) - marker.length + 1):
            window = lowered[offset : offset + marker.length]
            if hashlib.sha256(window).hexdigest() == target:
                violations.append(
                    Violation(
                        path=path,
                        line=_line_number(data, offset),
                        rule=marker.rule,
                    )
                )
                break
    return violations


def _scan_generic_private_content(data: bytes, path: str) -> list[Violation]:
    text = data.decode("utf-8", errors="replace")
    violations: list[Violation] = []
    for match in HOME_PATH_RE.finditer(text):
        violations.append(
            Violation(
                path=path,
                line=text.count("\n", 0, match.start()) + 1,
                rule="personal-home-path",
            )
        )
    for match in EMAIL_RE.finditer(text):
        if match.group(2).lower() in PUBLIC_EXAMPLE_DOMAINS:
            continue
        violations.append(
            Violation(
                path=path,
                line=text.count("\n", 0, match.start()) + 1,
                rule="non-example-email",
            )
        )
    return violations


def scan_bytes(
    data: bytes,
    *,
    path: str,
    markers: Sequence[HashedMarker] = BUILTIN_HASHED_MARKERS,
    local_terms: Sequence[bytes] = (),
) -> tuple[Violation, ...]:
    violations = _scan_hashed_markers(data, path, markers)
    lowered = data.lower()
    for index, term in enumerate(local_terms, start=1):
        offset = lowered.find(term)
        if offset >= 0:
            violations.append(
                Violation(
                    path=path,
                    line=_line_number(data, offset),
                    rule=f"local-denylist-{index:02d}",
                )
            )
    violations.extend(_scan_generic_private_content(data, path))
    return tuple(violations)


def scan_repository(
    root: Path,
    *,
    markers: Sequence[HashedMarker] = BUILTIN_HASHED_MARKERS,
    local_terms: Sequence[bytes] = (),
) -> tuple[tuple[Violation, ...], int]:
    violations: list[Violation] = []
    scanned = 0
    for relative in candidate_paths(root):
        path = root / relative
        if not path.exists():
            continue
        if relative in GENERATED_PRIVATE_PATHS:
            violations.append(Violation(path=relative, line=1, rule="generated-private-export"))
            continue
        if path.is_symlink():
            data = path.readlink().as_posix().encode("utf-8", errors="surrogateescape")
        elif path.is_file():
            data = path.read_bytes()
        else:
            continue
        scanned += 1
        path_data = relative.encode("utf-8", errors="surrogateescape")
        violations.extend(scan_bytes(path_data, path=relative, markers=markers, local_terms=local_terms))
        violations.extend(scan_bytes(data, path=relative, markers=markers, local_terms=local_terms))
    unique = sorted(set(violations), key=lambda row: (row.path, row.line, row.rule))
    return tuple(unique), scanned


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", help="Git worktree to inspect; defaults to the current worktree")
    parser.add_argument(
        "--denylist",
        action="append",
        default=[],
        metavar="PATH",
        help="additional machine-local plaintext denylist; may be repeated",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        root = resolve_root(args.root)
        denylist_paths = [Path(value).expanduser().resolve() for value in args.denylist]
        default_denylist = root / ".public-denylist.local"
        if default_denylist.is_file() and default_denylist not in denylist_paths:
            denylist_paths.append(default_denylist)
        local_terms = load_local_terms(denylist_paths)
        violations, scanned = scan_repository(root, local_terms=local_terms)
    except (OSError, RuntimeError) as exc:
        print(f"Public content check could not run: {exc}", file=sys.stderr)
        return 2

    if violations:
        print("Public content check failed:", file=sys.stderr)
        for violation in violations:
            print(
                f"  {violation.path}:{violation.line}: {violation.rule}",
                file=sys.stderr,
            )
        print(
            "Replace private content with neutral fixtures or keep it outside Git.",
            file=sys.stderr,
        )
        return 1

    print(f"Public content check passed ({scanned} candidate files scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
