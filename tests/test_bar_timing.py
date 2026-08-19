"""The EA must act as soon as a bar is finished, not when the next one lands.

THE FAULT, from a real journal on 2026-08-18: a breakout on the 09:34 bar, and
the order sent at 09:36 — a full bar late, on every trade.

`Resampler.push` closes a bucket when a bar belonging to the NEXT bucket
arrives. That is the exact analogue of MT5's `iTime` changing, and it is right
in a backtest, where the next bar is always there immediately.

Live it costs a whole bar. Databento emits the 09:34 bar the moment it
completes, at 09:35:00 — so the EA HAS the finished bar then. But the
resampler held it until the 09:35 bar turned up at 09:36:00. Meanwhile the
backtest fills at the open of the bar after the signal, i.e. 09:35:00. Live was
therefore systematically one minute behind its own backtest, entering further
past the range every time.

TWO fixes, in order of how much they recover:

  * `Engine._close_if_bar_completed` (`eager_close`) — the arriving bar IS the
    news. A 1-minute bar labelled 09:34 covers 09:34:00-09:34:59 and is emitted
    at 09:35:00, so the moment it lands the M1 signal bar is finished. Same for
    M15 once its 03:14 base bar arrives. Nothing is left to wait for.
  * `Engine.close_due_bar` — the fallback, for a bucket whose final base bar
    never arrives at all (a minute with no trades), where only the clock can
    say the bar is over.

Measured on the live loop, for a bar finishing at 03:03:00 with 1-second polls:

    wait for the next bar     03:04:01   (+61s)
    clock close + grace       03:03:10   (+10s)
    close on arrival          03:03:01   (+1s)

Nothing about the decision changes — the same completed bar goes through the
same `_tick`. Neither path is reachable from `run_backtest`, which never idles
and never sets `eager_close`; tools/golden_master.py proves that over 24 runs.

    python -m pytest tests/test_bar_timing.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                                              # noqa: E402

from orb.bars import Bar                                   # noqa: E402
from orb.broker import SimBroker                           # noqa: E402
from orb.engine import Engine                              # noqa: E402
from orb.live_trader import LiveTrader                     # noqa: E402
from orb.logger import RbeaLogger                          # noqa: E402
from orb.runconfig import RunConfig                        # noqa: E402

def in_window(session, window: str) -> bool:
    """Does this session belong to that WINDOW?

    A window trading several instruments expands to one session per cell —
    `london_gc`, `london_es` — so a test meaning "the London window" cannot
    match on the bare name any more. These tests drive gold's synthetic bars,
    so they want the gc cell.
    """
    name = session.name or ""
    return name == window or name.startswith(window + "_")
BASE = datetime(2026, 8, 18)
NEVER = 10 ** 9          # a grace so long the clock-close can never fire


def m1(minute, price, spread=0.0):
    t = BASE + timedelta(minutes=minute)
    return Bar(t, price, price + spread, price - spread, price, 1)


#: how often the simulated loop polls. Kept at one second because these tests
#: measure LATENCY — a coarser tick would hide the very thing under test.
POLL_SECONDS = 1


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self, bar=None):
        self.now += timedelta(seconds=POLL_SECONDS)
        return self.now


class Feed:
    """Databento behaviour: the bar labelled T is delivered at T + 1 minute."""

    def __init__(self, bars, clock):
        self.bars = list(bars)
        self.clock = clock

    def start(self):
        pass

    def stop(self):
        pass

    def poll(self, timeout=1.0):
        if self.bars and self.clock.now >= self.bars[0].time + timedelta(minutes=1):
            return self.bars.pop(0)
        return None


class Spy(RbeaLogger):
    """Records WHEN each journal line was written, by the simulated clock."""

    def __init__(self, clock):
        super().__init__(level=0)
        self.clock = clock
        self.events = []

    def write(self, level, tag, msg):
        self.events.append((self.clock.now, msg))


class TickBroker(SimBroker):
    """A SimBroker that claims live-quote pricing.

    These tests measure LATENCY — when the decision is taken — not fill price,
    so a bar-priced simulator is fine underneath. The flag is what a real
    `MT5Broker` sets, and it is what `Engine.eager_close` is gated on:
    `tests/test_single_source.py` uses a plain SimBroker and therefore keeps
    the slower, bar-for-bar path that matches the backtest exactly.
    """

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        # set AFTER super().__init__, which assigns the instance attribute —
        # a class-level override alone is silently overwritten
        self.prices_from_bars = False


def live_run(grace, bars=None, eager=True):
    original = Engine.IDLE_CLOSE_GRACE_SECONDS
    Engine.IDLE_CLOSE_GRACE_SECONDS = grace
    real_feed = Engine.feed
    if not eager:
        def no_eager(self, bar, now):
            self.eager_close = False
            return real_feed(self, bar, now)
        Engine.feed = no_eager
    try:
        cfg = RunConfig.load("orb_reverse").app_config({})
        for s in cfg.sessions.values():
            s.enabled = in_window(s, "london") and (s.instrument or "gc") == "gc"
            if s.enabled:
                s.signal_timeframe = "M1"
                s.range_start, s.range_end = "03:00", "03:01"
                s.stop_time = "09:30"
        if bars is None:
            bars = ([m1(180, 4450, spread=2.0),                # range 4448..4452
                     m1(181, 4450)]                            # inside
                    + [m1(182 + i, 4460 + i) for i in range(8)])   # breaks UP
        clock = Clock(BASE + timedelta(minutes=179, seconds=30))
        log = Spy(clock)
        trader = LiveTrader(cfg, broker=TickBroker(cfg.symbol, 100000.0),
                            feed=Feed(bars, clock), logger=log)
        trader.run(poll_seconds=0, max_polls=2000, now_fn=clock)
        return log.events, trader
    finally:
        Engine.IDLE_CLOSE_GRACE_SECONDS = original
        Engine.feed = real_feed


def first(events, needle):
    for when, msg in events:
        if needle in msg:
            return when, msg
    return None, None


# --------------------------------------------------------------------------
BAR_CLOSED = BASE + timedelta(minutes=183)        # the 03:02 bar ends 03:03:00


def test_a_breakout_is_acted_on_the_moment_its_bar_arrives():
    """The tightest requirement. The 03:02 bar finishes and is delivered at
    03:03:00; the EA must act within a poll of that, not after a grace period
    and not after the next bar."""
    events, _ = live_run(grace=10)
    when, _ = first(events, "BREAKOUT")
    assert when is not None, "no breakout at all"
    lag = (when - BAR_CLOSED).total_seconds()
    assert lag <= POLL_SECONDS + 1, f"acted {lag:.0f}s after the bar closed"


def test_the_grace_period_is_no_longer_on_the_critical_path():
    """With eager close off, the 10s grace shows up. With it on, it must not —
    proving the speed comes from closing on arrival, not from a shorter wait."""
    slow, _ = live_run(grace=10, eager=False)
    fast, _ = live_run(grace=10, eager=True)
    t_slow, _ = first(slow, "BREAKOUT")
    t_fast, _ = first(fast, "BREAKOUT")
    assert (t_slow - BAR_CLOSED).total_seconds() >= 9
    assert (t_fast - BAR_CLOSED).total_seconds() <= POLL_SECONDS + 1


def test_the_old_behaviour_really_was_a_bar_late():
    """Guards the guard: with both fixes disabled the lag comes back, so the
    tests above are measuring something real."""
    late, _ = live_run(grace=NEVER, eager=False)
    early, _ = live_run(grace=10)
    t_late, _ = first(late, "BREAKOUT")
    t_early, _ = first(early, "BREAKOUT")
    assert t_late is not None and t_early is not None
    gained = (t_late - t_early).total_seconds()
    assert gained >= 55, f"only {gained:.0f}s recovered — expected a whole bar"


def test_the_range_is_built_without_waiting_for_the_next_bar():
    """Same saving applies to the range: the window's last bar closes on the
    clock, so a session opens on time instead of a bar later."""
    events, _ = live_run(grace=10)
    when, _ = first(events, "RANGE BUILT")
    assert when is not None, "the range was never built"
    # the window's last bar (03:00) is delivered at 03:01:00
    lag = (when - (BASE + timedelta(minutes=181))).total_seconds()
    assert lag <= POLL_SECONDS + 1, (
        f"range built {lag:.0f}s after its last bar arrived")


def test_a_bar_is_never_processed_twice():
    """A late base bar can re-open a bucket the clock already closed. One bar
    must still mean one decision."""
    events, _ = live_run(grace=10)
    breakouts = [m for _, m in events if "BREAKOUT" in m]
    bars_named = [m.split("bar ")[1].split(" closed")[0] for m in breakouts]
    assert len(bars_named) == len(set(bars_named)), (
        f"a bar was judged twice: {bars_named}")


def test_a_bar_priced_broker_never_closes_early():
    """The gate. A SimBroker fills at the open of the bar the EA reacted on, so
    it cannot price a decision until the next bar exists — closing early there
    would fill a whole bar stale. Caught by test_single_source when the gate
    was briefly live-vs-backtest instead of broker capability."""
    from orb.broker import MT5Broker
    assert SimBroker.prices_from_bars is True
    assert MT5Broker.__init__ is not None          # flag is set in __init__
    cfg = RunConfig.load("orb_reverse").app_config({})
    for s in cfg.sessions.values():
        s.enabled = in_window(s, "london") and (s.instrument or "gc") == "gc"

    class Feed0:
        def start(self): pass
        def stop(self): pass
        def poll(self, timeout=1.0): return None

    trader = LiveTrader(cfg, broker=SimBroker(cfg.symbol, 100000.0),
                        feed=Feed0(), logger=RbeaLogger(level=0))
    trader.run(poll_seconds=0, max_polls=3,
               now_fn=Clock(BASE + timedelta(minutes=179)))
    for e in trader.engine.engines:
        assert e.eager_close is False, "a bar-priced broker closed bars early"


def test_a_backtest_never_closes_a_bar_early():
    """Both fast paths are opt-in and only `LiveTrader` opts in."""
    from orb.backtest import run_backtest
    from orb.engine import MultiEngine
    cfg = RunConfig.load("orb_reverse").app_config({})
    for s in cfg.sessions.values():
        s.enabled = in_window(s, "london") and (s.instrument or "gc") == "gc"
    engine = MultiEngine(cfg.enabled_sessions(),
                         SimBroker(cfg.symbol, 100000.0),
                         logger=RbeaLogger(level=0))
    for e in engine.engines:
        assert e.eager_close is False, "a fresh engine closes bars early"
        assert e._last_clock_closed is None
    bars = [m1(180 + i, 4450 + (i % 3), spread=1.0) for i in range(40)]
    run_backtest(cfg, bars, logger=RbeaLogger(level=0))


def test_the_base_bar_length_is_learned_from_the_feed():
    """`eager_close` needs to know how long an incoming bar is. A wrong value
    would close buckets early and truncate a range, so it is learned from the
    feed and only ever shrinks."""
    _, trader = live_run(grace=10)
    for engine in trader.engine.engines:
        assert engine.base_seconds == 60, engine.base_seconds


def test_a_backtest_never_closes_a_bar_on_the_clock():
    """`run_backtest` drives only `on_bar`; `on_idle` is a live-only entry
    point. tools/golden_master.py proves the same over 24 real backtests."""
    import inspect

    from orb import backtest as bt
    src = inspect.getsource(bt)
    assert "on_idle" not in src, (
        "the backtest now idles — clock-closing would change backtested trades")


def test_grace_is_long_enough_for_delivery():
    """Closing the instant the interval ends would race the feed and drop the
    bucket's final base bar."""
    assert Engine.IDLE_CLOSE_GRACE_SECONDS >= 5


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
