"""Backtest engine.

Drives `MultiEngine` over historical base bars in exactly the order the EA
would see them live. Each session builds whichever strategy its `engine` field
resolves to, so one backtest can mix engines on one account:

    for each base bar:
        1. feed the resampler  -> a signal-timeframe bar may complete
        2. strategy.on_time()  -> session sync, range build, stop-time close
        3. strategy.on_bar_closed() if a timeframe bar just closed
        4. broker.process_bar() -> intrabar SL / TP on any open position

Entries fill at the price available on the tick the EA would have acted on,
i.e. the open of the base bar that completed the signal bar.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence

from .bars import Bar
from .broker import ClosedTrade, SimBroker
from .config import AppConfig, journal_settings
from .engine import Engine, MultiEngine  # noqa: F401
from .logger import RbeaLogger
from .timeutils import ServerClock


@dataclass
class BacktestResult:
    trades: List[ClosedTrade]
    equity_curve: List
    initial_balance: float
    final_balance: float
    bars_processed: int
    first_bar: Optional[datetime]
    last_bar: Optional[datetime]
    config: AppConfig


def make_clock(cfg: AppConfig) -> ServerClock:
    return ServerClock(utc_offset_hours=cfg.server_utc_offset_hours,
                       timezone_name=cfg.server_timezone)


def run_backtest(cfg: AppConfig, bars: Sequence[Bar],
                 logger: Optional[RbeaLogger] = None) -> BacktestResult:
    if not bars:
        raise ValueError("No bars to backtest.")

    _level, _file, _show_time = journal_settings(cfg)
    log = logger or RbeaLogger(level=_level, file_path=_file,
                               show_time=_show_time)

    bt = cfg.backtest
    broker = SimBroker(
        spec=cfg.symbol,
        initial_balance=bt.initial_balance,
        spread_points=bt.spread_points,
        slippage_points=bt.slippage_points,
        commission_per_lot_per_side=bt.commission_per_lot_per_side,
        pessimistic_intrabar=bt.pessimistic_intrabar,
        logger=log,
    )

    # one engine per enabled session; a single-session config yields exactly
    # one, driving the identical tick sequence it always did
    cfg.validate_sessions()
    engine = MultiEngine(cfg.enabled_sessions(), broker, logger=log)

    # journal line for every exit, same wording as OnTradeTransaction()
    def _on_exit(trade: ClosedTrade) -> None:
        # routed by session, so a trade is journalled by the strategy that
        # opened it — with several engines running, "the first one" is wrong
        engine.strategy_for(trade.session_name).report_exit(
            trade.ticket, trade.exit_reason, trade.exit_price,
            trade.net_profit, cfg.symbol.currency)
    broker.on_exit = _on_exit

    log.info(f"Backtest range: {bars[0].time:%Y.%m.%d %H:%M} .. "
             f"{bars[-1].time:%Y.%m.%d %H:%M} | {len(bars)} base bars "
             f"| balance {bt.initial_balance:,.2f} {cfg.symbol.currency}")

    # the simulated clock is the only thing that differs from live
    for bar in bars:
        engine.on_bar(bar, now=bar.time)

    engine.flush()
    if broker.position is not None:
        broker.set_market(bars[-1].close, bars[-1].time)
        broker.close_all("end of backtest")

    log.info(f"Backtest finished | {len(broker.trades)} trade(s) | "
             f"final balance {broker.balance:,.2f} {cfg.symbol.currency}")

    return BacktestResult(
        trades=broker.trades,
        equity_curve=broker.equity_curve,
        initial_balance=bt.initial_balance,
        final_balance=broker.balance,
        bars_processed=len(bars),
        first_bar=bars[0].time,
        last_bar=bars[-1].time,
        config=cfg,
    )
