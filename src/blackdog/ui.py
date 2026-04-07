"""Compatibility shim for :mod:`blackdog.proper.ui`."""

from __future__ import annotations

import sys

from .proper import ui as _module

sys.modules[__name__] = _module
