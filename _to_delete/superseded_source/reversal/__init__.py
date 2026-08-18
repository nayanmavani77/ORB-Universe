"""Reversal engine — fade the opening-range breakout.

A close above the range high SELLS. The stop is a multiple of the opening range
rather than one of two fixed modes, which is the setting the original matrix
never searched.

    sessions:
      london:
        engine: reversal
        range_start: "03:00"
        range_end:   "03:15"
        stop_time:   "09:30"
        engine_options:
          sl_range_mult: 0.75
          direction: reverse
          max_trades_per_session: 3
"""
from ...registry import register
from .settings import (ANCHOR_MIRROR, ANCHOR_RANGE, DIRECTIONS, FORWARD,
                       REVERSE, ReversalSettings)
from .strategy import ReversalStrategy

NAME = "reversal"

register(NAME, ReversalStrategy, ReversalSettings,
         description="Fade the opening-range breakout; stop is a multiple of "
                     "the range.")

__all__ = ["NAME", "ReversalStrategy", "ReversalSettings", "FORWARD", "REVERSE",
           "DIRECTIONS", "ANCHOR_RANGE", "ANCHOR_MIRROR"]
