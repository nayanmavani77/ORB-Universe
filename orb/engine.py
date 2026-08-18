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

from datetime import datetime, timedelta
from typing import Optional

from .bars import Bar, BarStore, Resampler
from .broker import Broker
from .config import StrategyConfig
from .logger import RbeaLogger
from .registry import resolve as resolve_engine
from .timeutils import timeframe_seconds


class Engine:
    """Owns the strategy, the bar history and the resampler.

    Everything the EA does on a tick happens in `_tick()`. `on_bar()` and
    `on_idle()` are the only two entry points.
    """

    def __init__(self, cfg: StrategyConfig, broker: Broker,
                 logger: Optional[RbeaLogger] = None,
                 store: Optional[BarStore] = None,
                 strategy_cls: Optional[type] = None):
        self.cfg = cfg
        self.broker = broker
        # Every line this session writes is tagged with its name. Bound once,
        # here, rather than at 78 call sites — so a line added later cannot
        # forget to say which session it belongs to. With several engines
        # interleaved in one journal, an untagged "Stop Time reached" is
        # unreadable: you cannot tell whose it is.
        base = logger or RbeaLogger()
        bind = getattr(base, "for_session", None)
        self.log = bind(cfg.name or "MAIN") if callable(bind) else base
        self.store = store if store is not None else BarStore()
        self.resampler = Resampler(timeframe_seconds(cfg.signal_timeframe))
        #: LIVE ONLY. Open time of the last bar closed early rather than by the
        #: next bar's arrival — see `close_due_bar` / `eager_close`. Stays None
        #: in a backtest, which never sets either.
        self._last_clock_closed: Optional[datetime] = None

        #: LIVE ONLY, set by `LiveTrader`. When true, a timeframe bar is closed
        #: the INSTANT the base bar that completes it arrives, instead of
        #: waiting for the clock or for the next bar. See `feed`.
        self.eager_close = False
        #: length of one incoming base bar, in seconds. Needed to know whether
        #: an arriving bar reaches the end of its bucket. Learned from the feed
        #: if not supplied.
        self.base_seconds: Optional[int] = None
        self._prev_bar_time: Optional[datetime] = None
        # Which strategy this session runs is a per-session lookup, not a name
        # fixed in this file. That is what lets two sessions run two different
        # engines in one process; before, a second engine could only be reached
        # by rebinding a module global, which applied to every session at once.
        cls = strategy_cls or resolve_engine(cfg.engine)
        self.strategy = cls(cfg, broker, store=self.store, logger=self.log)
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
        self._learn_base_seconds(bar)
        closed_tf = self.resampler.push(bar)
        if (closed_tf is not None and self._last_clock_closed is not None
                and closed_tf.time <= self._last_clock_closed):
            # already emitted early; a late base bar re-opened the bucket.
            # Dropping the repeat keeps one bar = one decision.
            closed_tf = None
        if closed_tf is None and self.eager_close:
            closed_tf = self._close_if_bar_completed(bar)
        self._tick(now, closed_tf)
        return closed_tf

    def _learn_base_seconds(self, bar: Bar) -> None:
        """Work out how long one incoming bar is, from the feed itself.

        Only ever shrinks, so one gap in the data cannot make the engine think
        bars are longer than they are.
        """
        prev, self._prev_bar_time = self._prev_bar_time, bar.time
        if prev is None:
            return
        delta = int((bar.time - prev).total_seconds())
        if delta > 0 and (self.base_seconds is None or delta < self.base_seconds):
            self.base_seconds = delta

    def _close_if_bar_completed(self, bar: Bar) -> Optional[Bar]:
        """Close the timeframe bar the moment the base bar that finishes it
        arrives. LIVE ONLY — `eager_close` is set by `LiveTrader`.

        The bar that arrives IS the news. A 1-minute bar labelled 09:34 covers
        09:34:00-09:34:59 and Databento emits it at 09:34:60 — so the instant it
        lands, the M1 signal bar for 09:34 is finished and there is nothing left
        to wait for. Same for M15: once the 03:14 base bar arrives, the
        03:00-03:15 signal bar is complete.

        Waiting any longer is pure latency. `close_due_bar` remains as the
        fallback for a bucket whose final base bar never arrives at all (a
        minute with no trades), where nothing signals completion but the clock.
        """
        cur = self.resampler.current
        if cur is None or not self.base_seconds:
            return None
        bucket_end = cur.time + timedelta(seconds=self.resampler.tf_seconds)
        if bar.time + timedelta(seconds=self.base_seconds) < bucket_end:
            return None                       # more of this bucket still to come
        closed = self.resampler.flush()
        if closed is not None:
            self._last_clock_closed = closed.time
        return closed

    #: LIVE ONLY. Seconds after a bar's interval ends before the engine will
    #: close it on the clock rather than wait for the next bar. Databento emits
    #: a 1-minute bar the moment it completes, so a few seconds covers
    #: delivery; long enough that the bucket's final base bar has certainly
    #: arrived, short enough that almost the whole wasted minute is recovered.
    IDLE_CLOSE_GRACE_SECONDS = 10

    def close_due_bar(self, now: datetime) -> Optional[Bar]:
        """Close the forming bar once the clock has passed its end. LIVE ONLY.

        `Resampler.push` closes a bucket when a bar belonging to the NEXT
        bucket arrives — the exact analogue of MT5's `iTime` changing, and
        correct in a backtest, where the next bar is always available
        immediately.

        Live it costs a full bar. Databento emits the 09:34 bar at 09:35:00, so
        the EA HAS the completed bar then — but the resampler holds it until
        the 09:35 bar turns up at 09:36:00. The backtest fills at the open of
        the bar after the signal (09:35:00); live filled a minute later, every
        single trade. Seen on 2026-08-18: breakout on the 09:34 bar, order sent
        at 09:36.

        Nothing about the decision changes — the same completed bar runs
        through the same `_tick`. Only the waiting is removed, and only where
        the wait was an artefact of the feed rather than of the strategy.
        `run_backtest` never calls `on_idle`, so this cannot reach a backtest.
        """
        cur = self.resampler.current
        if cur is None:
            return None
        due = (cur.time + timedelta(seconds=self.resampler.tf_seconds)
               + timedelta(seconds=self.IDLE_CLOSE_GRACE_SECONDS))
        if now < due:
            return None
        closed = self.resampler.flush()
        if closed is not None:
            self._last_clock_closed = closed.time
        return closed

    def on_idle(self, now: datetime) -> None:
        """No bar this poll — the EA still needs its per-tick housekeeping
        (session rollover, range build, stop time), and may also have a bar
        whose interval has elapsed but whose successor has not arrived."""
        self._tick(now, self.close_due_bar(now))

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
            self.log.info(f"--- session {label} [{cfg.engine}] "
                          f"{cfg.range_start}-{cfg.range_end} -> "
                          f"{cfg.stop_time} | magic {cfg.magic} ---")
            self.engines.append(Engine(cfg, broker, logger=self.log))
        names = ", ".join(f"{e.cfg.name or 'MAIN'} ({e.cfg.engine})"
                          for e in self.engines)
        self.log.info(f"{len(self.engines)} session(s) enabled: {names}")
        engines_used = sorted({e.cfg.engine for e in self.engines})
        if len(engines_used) > 1:
            self.log.info(f"{len(engines_used)} engines running side by side: "
                          f"{', '.join(engines_used)}")

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

    def strategy_for(self, session_name: str):
        """The strategy belonging to a named session.

        A closing trade must be journalled by the strategy that opened it. With
        one engine, "the first session's strategy" happened to be right often
        enough not to matter; with several engines running different strategies
        it is simply wrong — an `orb_reverse` exit would be reported by the
        `orb` strategy. `ClosedTrade.session_name` carries the answer, so use it.

        Falls back to the first session when the name is unknown, which keeps
        the single-session path behaving exactly as it always did.
        """
        want = str(session_name or "")
        for e in self.engines:
            if (e.cfg.name or "MAIN") == want:
                return e.strategy
        return self.engines[0].strategy if self.engines else None
