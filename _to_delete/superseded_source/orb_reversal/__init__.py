"""Compatibility shim — the reversal engine moved into `orb/engines/reversal/`.

Every engine now lives under `orb/engines/<name>/` with the same five files, and
a session picks one by name:

    sessions:
      london:
        engine: reversal
        engine_options:
          sl_range_mult: 0.75
          direction: reverse

Import from the new home in new code:

    from orb.engines.reversal import ReversalSettings, ReversalStrategy

What changed, and why
---------------------
This package used to reach the engine by rebinding `orb.engine.RangeBreakoutStrategy`
— a process-wide monkey-patch. That made it impossible to run a breakout engine
and a reversal engine at the same time, and it gave the reversal no live path at
all, because `LiveTrader` never applied the patch. Selection now happens per
session through `orb/registry.py`, so both work.

`reversal_engine()` is therefore gone rather than kept as a no-op: a context
manager that silently stopped doing anything would let a reversal sweep quietly
run forward trades. Calling it raises with an explanation.
"""
from orb.engines.reversal import (ANCHOR_MIRROR, ANCHOR_RANGE,  # noqa: F401
                                  DIRECTIONS, FORWARD, REVERSE,
                                  ReversalSettings, ReversalStrategy)
from orb.engines.reversal import grid  # noqa: F401

from .runner import reversal_engine, run_forward, run_reversal  # noqa: F401
from .settings import DEFAULT_PATH, ReversalConfig  # noqa: F401

__all__ = ["ReversalSettings", "ReversalStrategy", "run_reversal",
           "run_forward", "reversal_engine", "ReversalConfig", "DEFAULT_PATH",
           "FORWARD", "REVERSE", "DIRECTIONS", "ANCHOR_RANGE", "ANCHOR_MIRROR",
           "grid"]
