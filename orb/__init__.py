"""Range Breakout (ORB) EA — Python port of RangeBreakoutEA.mq5 v1.70.

The strategy logic in `orb.strategy` is a 1:1 port of the MQL5 expert advisor.
Data comes from Databento (DBN files for backtesting, Live client for trading),
execution goes through the MetaTrader5 Python API in live mode and through a
simulated broker in backtest mode.
"""

__version__ = "1.70.0"
