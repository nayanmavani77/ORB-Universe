"""The `orb_reverse` engine — fade the opening-range breakout.

A close above the range high SELLS. The stop is a multiple of the opening range
rather than one of two fixed modes, which is the setting the original matrix
never searched.

    sessions:
      london:
        engine: orb_reverse
        range_start: "03:00"
        range_end:   "03:15"
        stop_time:   "09:30"
        max_trades_per_session: 3     # a SESSION field, not an engine option
        engine_options:
          sl_range_mult: 0.75
          direction: reverse
"""
from ...registry import register
from .settings import (ANCHOR_MIRROR, ANCHOR_RANGE, DIRECTIONS, FORWARD,
                       REVERSE, OrbReverseSettings, mirror_settings_for)
from .strategy import OrbReverseStrategy

#: uniform alias — every engine exposes `Strategy` and `Settings`
Strategy = OrbReverseStrategy
Settings = OrbReverseSettings

NAME = "orb_reverse"

register(NAME, OrbReverseStrategy, OrbReverseSettings,
         description="Fade the opening-range breakout; stop is a multiple of "
                     "the range.")

__all__ = ["NAME", "Strategy", "Settings", "OrbReverseStrategy",
           "OrbReverseSettings", "FORWARD", "REVERSE",
           "DIRECTIONS", "ANCHOR_RANGE", "ANCHOR_MIRROR",
           "mirror_settings_for"]
