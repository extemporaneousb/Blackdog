"""Compatibility shim for :mod:`blackdog.proper.supervisor`."""

from __future__ import annotations

import sys

from .proper import supervisor as _module

sys.modules[__name__] = _module
