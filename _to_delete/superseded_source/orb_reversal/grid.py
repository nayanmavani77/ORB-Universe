"""Compatibility shim — the reversal sweep grid moved.

It now lives at `orb/engines/reversal/grid.py`, and the session-open table it
used to carry a private copy of is shared in `orb/markets.py`.

`build_grid` is the old name for what is now `build`.
"""
from orb.engines.reversal.grid import (AXES, DIRECTIONS, GridItem,  # noqa: F401
                                       NEWS_MODES, ORB_MINUTES, RISK_REWARD,
                                       SL_RANGE_MULTS, TIMEFRAMES, TRADE_CAPS,
                                       build)
from orb.markets import (SESSION_ORDER, SESSION_STOP_OVERRIDE,  # noqa: F401
                         SESSIONS, add_minutes)

build_grid = build
RR_VALUES = RISK_REWARD
SL_MULTS = SL_RANGE_MULTS

__all__ = ["build", "build_grid", "GridItem", "AXES", "TIMEFRAMES",
           "ORB_MINUTES", "RISK_REWARD", "RR_VALUES", "SL_RANGE_MULTS",
           "SL_MULTS", "TRADE_CAPS", "DIRECTIONS", "NEWS_MODES", "SESSIONS",
           "SESSION_ORDER", "SESSION_STOP_OVERRIDE", "add_minutes"]
