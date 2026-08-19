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


def load_instrument_bars(cfg: AppConfig, clock, *, start=None, end=None,
                         logger=None, instruments=None):
    """Every instrument's bars, tagged and merged into one time-ordered stream.

    Each instrument is loaded through the SAME single-contract, spread-free
    path a single-instrument run uses — `load_dbn_bars` with that
    instrument's own files and contract mode — and only then tagged and
    merged. The merge is stable on (time, instrument) so a run is
    reproducible whatever order the files were read in.

    Returns the flat list `run_backtest` expects. With no `instruments:`
    block it loads exactly what it always did.
    """
    from .data.dbn import load_dbn_bars
    d = cfg.databento
    if not cfg.instruments:
        return load_dbn_bars(
            cfg.backtest.dbn_paths, clock, contract_mode=d.contract_mode,
            contract_symbol=d.contract_symbol,
            include_spreads=d.include_spreads,
            roll_min_volume=d.roll_min_volume,
            roll_boundary_hour=d.roll_boundary_hour,
            start=start, end=end, logger=logger)

    # Which instruments to load.
    #
    # `instruments=` wins when given. A SWEEP needs it: its bars are loaded
    # once and shared by every configuration in the grid, so they must cover
    # every instrument the grid trades — not just the ones the first item's
    # session happens to name. Getting this wrong made an ES-only bar list
    # serve the NQ configurations too, and they returned ES's trades.
    #
    # Otherwise: only what an ENABLED session actually trades. A config may
    # declare instruments it is not trading today — a 3x3 (session x
    # instrument) matrix with most cells switched off is the normal case — and
    # those must not demand a data file that has not been downloaded yet.
    # Loading them anyway would also cost minutes and gigabytes for bars
    # nothing reads.
    if instruments is not None:
        wanted = {str(x).strip() for x in instruments if str(x).strip()}
        unknown = sorted(wanted - set(cfg.instruments))
        if unknown:
            raise ValueError(
                f"Cannot load bars for undeclared instrument(s) {unknown}. "
                f"Declared: {sorted(cfg.instruments)}.")
    else:
        wanted = {(s.instrument or "") for s in cfg.enabled_sessions()} - {""}
    if not wanted:
        wanted = set(cfg.instruments)

    out = []
    for name, inst in cfg.instruments.items():
        if name not in wanted:
            continue
        paths = inst.data or cfg.backtest.dbn_paths
        if not paths:
            raise ValueError(
                f"Instrument '{name}' has no data. Give it `data: [...]` in "
                f"the instruments block, or set backtest.dbn_paths for the "
                f"whole run.")
        bars = load_dbn_bars(
            paths, clock,
            contract_mode=(inst.contract_mode or d.contract_mode),
            contract_symbol=(inst.contract_symbol or d.contract_symbol),
            include_spreads=d.include_spreads,
            roll_min_volume=d.roll_min_volume,
            roll_boundary_hour=d.roll_boundary_hour,
            start=start, end=end, logger=logger)
        for b in bars:
            b.instrument = name
        if logger:
            logger.info(f"{name:<10} {len(bars):,} bars from "
                        f"{len(paths) if isinstance(paths, (list, tuple)) else 1}"
                        f" file(s)")
        out.extend(bars)
    out.sort(key=lambda b: (b.time, b.instrument))
    return out


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
    # Register each instrument's contract details BEFORE any session opens, so
    # P&L, tick rounding and lot steps are that instrument's own. Without this
    # an ES trade would be valued with gold's 100-per-point.
    for name, inst in (cfg.instruments or {}).items():
        broker.add_instrument(name, inst.spec(cfg.symbol))

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

    # Did ANY bar reach a session? Zero trades is a legitimate result — a
    # quiet market, a filter that never fired. Zero bars ROUTED is not: it
    # means the bars and the sessions describe different instruments, and
    # reporting that as a flat month would hide a wiring mistake behind a
    # plausible-looking answer.
    if not getattr(engine, "_routed", 1):
        want = sorted({(s.instrument or "") for s in cfg.enabled_sessions()})
        got = sorted(getattr(engine, "_skipped_tags", set()))
        raise ValueError(
            f"Not one bar reached a session. The sessions trade {want}, the "
            f"bars are tagged {got}. Nothing was traded because nothing "
            f"matched — load the bars for the instrument(s) this run actually "
            f"trades.")

    engine.flush()
    if broker.positions:
        # Flatten every instrument at ITS OWN last price. Using one bar's close
        # for all of them would value an ES position at gold's last print.
        last = {}
        for bar in bars:
            last[getattr(bar, "instrument", "") or ""] = bar
        for key in list(broker.positions):
            tail = last.get(key) or bars[-1]
            broker.set_market(tail.close, tail.time, key)
            broker.close_all("end of backtest", instrument=key)

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
