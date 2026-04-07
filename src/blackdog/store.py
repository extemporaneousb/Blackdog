"""Compatibility shim for :mod:`blackdog.core.store`."""

from __future__ import annotations

import sys

from .core import store as _module

sys.modules[__name__] = _module
