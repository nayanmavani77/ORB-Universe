"""Options for the breakout engine.

There are none.

Everything the breakout strategy needs — `range_start`, `range_end`,
`stop_time`, `signal_timeframe`, `sl_mode`, `risk_reward`, `lots`,
`require_range_reentry`, `max_trades_per_session`, `close_at_stop_time`, the
news filter — is a field of `StrategyConfig` itself, because this engine was the
only one when those fields were designed. Nothing is moved: doing so would
change a config schema that already works and is documented.

The class exists anyway, empty, for two reasons:

  1. every engine has the same five files, so there is no "except breakout,
     which is different" to remember;
  2. it turns a typo into an error. Writing

         engine: breakout
         engine_options:
           sl_range_mult: 0.75      <- belongs to the reversal engine

     now fails at load with a message naming the mistake, instead of running a
     backtest that silently ignores the line.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..base import EngineSettings


@dataclass
class BreakoutSettings(EngineSettings):
    """No options — see the module docstring."""

    def describe(self) -> str:
        return "no engine options (all settings are standard session fields)"
