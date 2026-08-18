"""ORB — opening-range trading engines, a Python port of RangeBreakoutEA.mq5 v1.70.

The package holds one CORE and any number of ENGINES:

  * the core (`config`, `engine`, `broker`, `bars`, `report`, `data/`, …) knows
    nothing about any particular strategy;
  * each engine lives in its own folder under `orb/engines/` — today `orb`
    (trade the breakout) and `orb_reverse` (fade it) — and registers itself by
    name in `orb.registry`.

A session names the engine it runs, so several engines run side by side, in one
process, in both backtest and live. `orb/engines/orb/strategy.py` is the 1:1
port of the MQL5 expert advisor; `orb/strategy.py` remains as an import shim.

Data comes from Databento (DBN files for backtesting, the Live client for
trading), execution goes through the MetaTrader5 Python API in live mode and
through a simulated broker in backtest mode.
"""

__version__ = "1.70.0"
