"""Core Blackdog runtime contracts.

``blackdog.core.backlog`` owns the durable backlog runtime. Prompt/tune
helpers now live under ``blackdog.proper`` and remain reachable here only
through compatibility wrappers while callers migrate.
"""

__all__ = ["backlog", "config", "store"]
