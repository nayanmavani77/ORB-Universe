"""Compatibility shim — the ORB breakout strategy moved.

The class now lives at `orb/engines/orb/strategy.py`, alongside the other
engines, each in the same folder shape. Its logic did not change: the file is
byte-identical apart from the depth of its relative imports.

Import it from its new home in new code:

    from orb.engines.orb import OrbStrategy

This shim stays so existing imports keep working — `tests/test_parity.py` and
`tests/test_sessions.py` still reach the class through this path.
"""
from .engines.orb.strategy import OrbStrategy  # noqa: F401

__all__ = ["OrbStrategy"]
