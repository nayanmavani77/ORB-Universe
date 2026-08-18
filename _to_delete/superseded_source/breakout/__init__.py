"""Breakout engine — trade the opening-range breakout.

The original strategy, a 1:1 port of RangeBreakoutEA.mq5 v1.70. A close beyond
the range high buys; a close beyond the range low sells.

`strategy.py` is the file that was `orb/strategy.py`, moved without a single
change to its logic — only the depth of its relative imports differs.
"""
from ...registry import register
from .settings import BreakoutSettings
from .strategy import RangeBreakoutStrategy

NAME = "breakout"

register(NAME, RangeBreakoutStrategy, BreakoutSettings,
         description="Trade the opening-range breakout (the original strategy).")

__all__ = ["NAME", "RangeBreakoutStrategy", "BreakoutSettings"]
