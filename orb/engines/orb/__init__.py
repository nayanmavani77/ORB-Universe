"""The `orb` engine — trade the opening-range breakout.

The original strategy, a 1:1 port of RangeBreakoutEA.mq5 v1.70. A close beyond
the range high buys; a close beyond the range low sells.

`strategy.py` is the file that was `orb/strategy.py`, moved without a single
change to its logic — only the depth of its relative imports differs.
"""
from ...registry import register
from .settings import OrbSettings
from .strategy import OrbStrategy

#: uniform alias — every engine exposes `Strategy` and `Settings`
Strategy = OrbStrategy
Settings = OrbSettings

NAME = "orb"

register(NAME, OrbStrategy, OrbSettings,
         description="Trade the opening-range breakout — the original strategy.")

__all__ = ["NAME", "Strategy", "Settings", "OrbStrategy", "OrbSettings"]
