"""Compatibility shim for :mod:`blackdog.proper.threads`."""

from __future__ import annotations

import sys

from .proper import threads as _module

sys.modules[__name__] = _module
