"""Compatibility shim for :mod:`blackdog.core.config`."""

from __future__ import annotations

import sys

from .core import config as _module

sys.modules[__name__] = _module
