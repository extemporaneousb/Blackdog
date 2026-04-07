"""Compatibility shim for :mod:`blackdog.core.backlog`."""

from __future__ import annotations

import sys

from .core import backlog as _module

sys.modules[__name__] = _module
