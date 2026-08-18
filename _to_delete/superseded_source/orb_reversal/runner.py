"""Running the reversal engine.

There is no class swapping here any more. A session names its engine in config
and `Engine` resolves it through `orb/registry.py`, so running the reversal is
just `run_backtest` with `engine: reversal` on the session — which is also why
it now works in live trading and alongside other engines.
"""
from __future__ import annotations

import copy
from typing import Optional, Sequence

from orb.backtest import BacktestResult, run_backtest
from orb.config import AppConfig
from orb.engines.reversal import FORWARD, REVERSE, ReversalSettings
from orb.logger import RbeaLogger


def run_reversal(cfg: AppConfig, bars: Sequence, settings: ReversalSettings,
                 logger: Optional[RbeaLogger] = None,
                 copy_config: bool = True) -> BacktestResult:
    """One reversal backtest.

    `cfg` is deep-copied by default so the caller's configuration is not mutated
    by the settings being applied — a sweep reuses one base config for thousands
    of runs and must not accumulate state.
    """
    settings.validate()
    run_cfg = copy.deepcopy(cfg) if copy_config else cfg
    settings.apply_to(run_cfg)
    return run_backtest(run_cfg, bars, logger)


def run_forward(cfg: AppConfig, bars: Sequence, settings: ReversalSettings,
                logger: Optional[RbeaLogger] = None) -> BacktestResult:
    """The same configuration in the ORDINARY breakout direction.

    The control arm: identical stop multiplier, identical trade cap, only the
    direction differs. Without it a profitable reversal cannot be told apart
    from a stop multiplier that simply suits this market.
    """
    forward = copy.deepcopy(settings)
    forward.direction = FORWARD
    return run_reversal(cfg, bars, forward, logger)


def reversal_engine(*_args, **_kwargs):
    """Removed. Kept only to fail loudly instead of silently doing nothing.

    This used to rebind `orb.engine.RangeBreakoutStrategy` for the duration of a
    run. `Engine` no longer reads that name, so a no-op version of this would
    let a reversal sweep quietly execute forward trades and report them as
    reversed — the worst possible failure for a research tool.
    """
    raise RuntimeError(
        "reversal_engine() has been removed. Engines are now selected per "
        "session: set `engine: reversal` on the session (or call "
        "ReversalSettings.apply_to_session(cfg)) and run the ordinary "
        "run_backtest(). See orb/registry.py.")


__all__ = ["run_reversal", "run_forward", "reversal_engine", "REVERSE",
           "FORWARD"]
