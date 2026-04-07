"""Compatibility shim for :mod:`blackdog.proper.scaffold`."""

from __future__ import annotations

import sys

from .proper import scaffold as _module

sys.modules[__name__] = _module
