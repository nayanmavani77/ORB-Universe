"""Compatibility shim — the breakout strategy moved.

The class now lives at `orb/engines/breakout/strategy.py`, alongside the other
engines, each in the same folder shape. Its logic did not change: the file is
byte-identical apart from the depth of its relative imports.

Import it from its new home in new code:

    from orb.engines.orb import OrbStrategy

This shim stays so existing imports keep working, and because it is what several
tests read as a literal file when checking that no engine-specific vocabulary
has leaked into the core.
"""
from .engines.orb.strategy import OrbStrategy  # noqa: F401

__all__ = ["OrbStrategy"]
