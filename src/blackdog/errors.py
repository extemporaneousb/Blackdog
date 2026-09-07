"""Lightweight product error boundary for CLI dispatch."""


class BlackdogError(RuntimeError):
    """An expected Blackdog product failure that the CLI can report directly."""
