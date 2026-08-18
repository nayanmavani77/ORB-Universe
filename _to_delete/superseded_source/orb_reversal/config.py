"""Compatibility shim — `ReversalSettings` moved.

It now lives at `orb/engines/reversal/settings.py`, which is where every
engine's settings live. The old split was confusing: this module (`config.py`)
held the settings, while `settings.py` held the config-file loader — the two
names were swapped relative to their contents.
"""
from orb.engines.reversal.settings import (ANCHOR_MIRROR, ANCHOR_RANGE,  # noqa: F401
                                           ANCHORS, DIRECTIONS, FORWARD,
                                           REVERSE, ReversalSettings)

__all__ = ["ReversalSettings", "ANCHOR_RANGE", "ANCHOR_MIRROR", "ANCHORS",
           "FORWARD", "REVERSE", "DIRECTIONS"]
