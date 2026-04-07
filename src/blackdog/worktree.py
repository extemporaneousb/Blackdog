"""Compatibility shim for :mod:`blackdog.proper.worktree`."""

from __future__ import annotations

import sys

from .proper import worktree as _module

sys.modules[__name__] = _module
