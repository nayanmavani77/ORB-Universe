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

`Engine.close_due_bar` closes the forming bar once the clock has passed its
end plus a short grace for delivery. Nothing about the decision changes — the
same completed bar goes through the same `_tick`. `run_backtest` never calls
`on_idle`, so a backtest cannot reach it; tools/golden_master.py proves that
over 24 runs.

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

BASE = datetime(2026, 8, 18)
NEVER = 10 ** 9          # a grace so long the clock-close can never fire


def m1(minute, price, spread=0.0):
    t = BASE + timedelta(minutes=minute)
    return Bar(t, price, price + spread, price - spread, price, 1)


class Clock:
    def __init__(self, start):
        self.now = start

    def __call__(self, bar=None):
        self.now += timedelta(seconds=5)
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


def live_run(grace, bars=None):
    original = Engine.IDLE_CLOSE_GRACE_SECONDS
    Engine.IDLE_CLOSE_GRACE_SECONDS = grace
    try:
        cfg = RunConfig.load("orb_reverse").app_config({})
        for s in cfg.sessions.values():
            s.enabled = (s.name == "london")
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
        trader = LiveTrader(cfg, broker=SimBroker(cfg.symbol, 100000.0),
                            feed=Feed(bars, clock), logger=log)
        trader.run(poll_seconds=0, max_polls=400, now_fn=clock)
        return log.events, trader
    finally:
        Engine.IDLE_CLOSE_GRACE_SECONDS = original


def first(events, needle):
    for when, msg in events:
        if needle in msg:
            return when, msg
    return None, None


# --------------------------------------------------------------------------
def test_a_breakout_is_acted_on_as_soon_as_its_bar_is_finished():
    """The 03:02 bar completes and is delivered at 03:03:00. The EA must act
    then, not when the 03:03 bar arrives a minute later."""
    events, _ = live_run(grace=10)
    when, msg = first(events, "BREAKOUT")
    assert when is not None, "no breakout at all"
    assert when < BASE + timedelta(minutes=183, seconds=30), (
        f"acted at {when:%H:%M:%S} — still waiting for the next bar")


def test_the_old_behaviour_really_was_a_bar_late():
    """Guards the guard: with the clock-close disabled the lag comes back, so
    the test above is measuring something real."""
    late, _ = live_run(grace=NEVER)
    early, _ = live_run(grace=10)
    t_late, _ = first(late, "BREAKOUT")
    t_early, _ = first(early, "BREAKOUT")
    assert t_late is not None and t_early is not None
    gained = (t_late - t_early).total_seconds()
    assert gained >= 45, f"only {gained:.0f}s recovered — expected most of a bar"


def test_the_range_is_built_without_waiting_for_the_next_bar():
    """Same saving applies to the range: the window's last bar closes on the
    clock, so a session opens on time instead of a bar later."""
    events, _ = live_run(grace=10)
    when, _ = first(events, "Range built")
    assert when is not None, "the range was never built"
    assert when < BASE + timedelta(minutes=181, seconds=30), (
        f"range built at {when:%H:%M:%S} — a bar late")


def test_a_bar_is_never_processed_twice():
    """A late base bar can re-open a bucket the clock already closed. One bar
    must still mean one decision."""
    events, _ = live_run(grace=10)
    breakouts = [m for _, m in events if "BREAKOUT" in m]
    bars_named = [m.split("bar ")[1].split(" closed")[0] for m in breakouts]
    assert len(bars_named) == len(set(bars_named)), (
        f"a bar was judged twice: {bars_named}")


def test_the_engine_tracks_what_the_clock_closed():
    """The de-duplication key. It must stay None until a clock-close happens,
    which is what keeps a backtest out of this path entirely."""
    _, trader = live_run(grace=NEVER)
    for engine in trader.engine.engines:
        assert engine._last_clock_closed is None


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
