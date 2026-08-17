"""The single OnTick sequence — the one source of truth for backtest AND live.

Both `orb.backtest` and `orb.live_trader` drive *this* class and nothing else.
Neither of them is allowed to call `Resampler.push`, `Strategy.ingest_bar`,
`Strategy.on_time` or `Strategy.on_bar_closed` itself; if the order of those
calls ever needs to change, it changes here, once.

The only legitimate difference between the two modes is where `now` comes
from — a simulated clock in the backtest, the wall clock when live — so it is
passed in as an argument rather than being decided inside.

    backtest:  engine.on_bar(bar, now=bar.time)
    live:      engine.on_bar(bar, now=clock.now())   # bar arrived
               engine.on_idle(now=clock.now())       # quiet poll, no bar
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .bars import Bar, BarStore, Resampler
from .broker import Broker
from .config import StrategyConfig
from .logger import RbeaLogger
from .strategy import RangeBreakoutStrategy
from .timeutils import timeframe_seconds


class Engine:
    """Owns the strategy, the bar history and the resampler.

    Everything the EA does on a tick happens in `_tick()`. `on_bar()` and
    `on_idle()` are the only two entry points.
    """

    def __init__(self, cfg: StrategyConfig, broker: Broker,
                 logger: Optional[RbeaLogger] = None,
                 store: Optional[BarStore] = None):
        self.cfg = cfg
        self.broker = broker
        self.log = logger or RbeaLogger()
        self.store = store if store is not None else BarStore()
        self.resampler = Resampler(timeframe_seconds(cfg.signal_timeframe))
        self.strategy = RangeBreakoutStrategy(cfg, broker, store=self.store,
                                              logger=self.log)
        # optional hook called once per tick, after the EA's own housekeeping
        # (live mode uses it to journal server-side exits)
        self.after_tick = None

    # ------------------------------------------------------------------
    def on_bar(self, bar: Bar, now: datetime) -> Optional[Bar]:
        """A completed base bar arrived. Returns the timeframe bar it closed,
        if any."""
        self.broker.sync_market(bar, now)          # no-op for a live broker
        closed_tf = self.feed(bar, now)
        self.broker.settle_bar(bar)                # no-op for a live broker
        return closed_tf

    def feed(self, bar: Bar, now: datetime) -> Optional[Bar]:
        """Everything `on_bar` does EXCEPT the broker hooks.

        Split out so `MultiEngine` can run the hooks once per bar and still feed
        several sessions through the identical tick sequence. Nobody else should
        call this — `on_bar` is the entry point for a single session.
        """
        closed_tf = self.resampler.push(bar)
        self._tick(now, closed_tf)
        return closed_tf

    def on_idle(self, now: datetime) -> None:
        """No bar this poll — the EA still needs its per-tick housekeeping
        (session rollover, range build, stop time)."""
        self._tick(now, None)

    def warmup_bar(self, bar: Bar) -> Optional[Bar]:
        """Seed history from a PAST bar without taking any decision.

        Used on live start-up so the current session's range can be rebuilt
        from downloaded history after a restart. It runs the resampler and
        files completed bars into the store, but never calls `on_time` or
        `on_bar_closed` — replaying history through the decision path would
        fire breakouts that already happened and place orders into today's
        market at yesterday's signals.
        """
        closed = self.resampler.push(bar)
        if closed is not None:
            self.strategy.ingest_bar(closed)
        return closed

    # ------------------------------------------------------------------
    def _tick(self, now: datetime, closed_tf: Optional[Bar]) -> None:
        """The EA's OnTick(), in order. This is the single source of truth."""
        # a bar is already in the MT5 history buffer on the tick that opens the
        # next one — which is the very tick ComputeRange() runs on
        if closed_tf is not None:
            self.strategy.ingest_bar(closed_tf)

        # steps 1-3: session sync, range build, stop-time flatten
        self.strategy.on_time(now)

        if self.after_tick is not None:
            self.after_tick(now)

        # steps 4-7: arming, breakout detection, order placement
        if closed_tf is not None:
            self.strategy.on_bar_closed(closed_tf, now)

    # ------------------------------------------------------------------
    def flush(self) -> Optional[Bar]:
        """End of data: push the final partial timeframe bar into history."""
        tail = self.resampler.flush()
        if tail is not None:
            self.store.add(tail)
        return tail

    # convenience passthroughs so callers do not reach into the strategy
    @property
    def range_high(self) -> float:
        return self.strategy.range_high

    @property
    def range_low(self) -> float:
        return self.strategy.range_low

    @property
    def range_mid(self) -> float:
        return self.strategy.range_mid


class MultiEngine:
    """Runs one independent `Engine` per enabled session over the same bars.

    Each session owns its own strategy instance, its own bar store and its own
    resampler — so Asia can run M1 while New York runs M15, with different
    ranges, R:R, SL mode, re-entry rule and news filter, and neither can see or
    touch the other's state.

    What they DO share is the broker, because there is one account and one
    market. That is safe only because enabled sessions are proven not to
    overlap (`AppConfig.validate_sessions`), so at most one session is ever
    inside its trading window. The broker hooks therefore run exactly once per
    bar, around the whole fan-out, never once per session:

        sync_market(bar)          <- price is set once
          session A: resampler -> tick
          session B: resampler -> tick
        settle_bar(bar)           <- SL/TP resolved once

    Running `settle_bar` per session would walk the same bar several times and
    could close a position twice, so the ordering here is the load-bearing part.

    A single enabled session drives exactly the same sequence as `Engine`
    itself, which is what keeps the one-session and many-session paths honest.
    """

    def __init__(self, sessions, broker: Broker,
                 logger: Optional[RbeaLogger] = None):
        self.broker = broker
        self.log = logger or RbeaLogger()
        self.engines = []
        for cfg in sessions:
            label = cfg.name or "MAIN"
            self.log.info(f"--- session {label} ---")
            self.engines.append(Engine(cfg, broker, logger=self.log))
        names = ", ".join(e.cfg.name or "MAIN" for e in self.engines)
        self.log.info(f"{len(self.engines)} session(s) enabled: {names}")

    # ------------------------------------------------------------------
    def on_bar(self, bar: Bar, now: datetime) -> None:
        self.broker.sync_market(bar, now)
        for e in self.engines:
            e.feed(bar, now)
        self.broker.settle_bar(bar)

    def on_idle(self, now: datetime) -> None:
        for e in self.engines:
            e.on_idle(now)

    def warmup_bar(self, bar: Bar) -> None:
        """Seed every session's history. Each session has its own resampler and
        its own timeframe, so one shared warm-up pass would be wrong — the bar
        has to go through all of them."""
        for e in self.engines:
            e.warmup_bar(bar)

    def flush(self) -> None:
        for e in self.engines:
            e.flush()

    # ------------------------------------------------------------------
    @property
    def after_tick(self):
        return self.engines[0].after_tick if self.engines else None

    @after_tick.setter
    def after_tick(self, fn) -> None:
        """Housekeeping that must happen once per tick, not once per session."""
        for i, e in enumerate(self.engines):
            e.after_tick = fn if i == 0 else None

    @property
    def strategy(self):
        """The first session's strategy — for single-session callers."""
        return self.engines[0].strategy
