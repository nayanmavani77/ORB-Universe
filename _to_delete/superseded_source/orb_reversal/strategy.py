"""Compatibility shim — `ReversalStrategy` moved to `orb/engines/reversal/`."""
from orb.engines.reversal.strategy import ReversalStrategy  # noqa: F401

__all__ = ["ReversalStrategy"]
