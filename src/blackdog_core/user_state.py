"""User-local Blackdog state paths.

These paths are deliberately outside checked-in repo state. BLACKDOG_HOME is
the explicit override; otherwise Blackdog keeps its user-local state under the
Codex home because the current product is Codex-facing.
"""

from __future__ import annotations

from pathlib import Path
import os


def blackdog_home() -> Path:
    configured = os.environ.get("BLACKDOG_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return (Path(codex_home).expanduser().resolve() / "blackdog").resolve()
    return (Path.home() / ".codex" / "blackdog").resolve()


def ensure_blackdog_home() -> Path:
    home = blackdog_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


def user_state_file(*parts: str) -> Path:
    return ensure_blackdog_home().joinpath(*parts)


__all__ = [
    "blackdog_home",
    "ensure_blackdog_home",
    "user_state_file",
]
