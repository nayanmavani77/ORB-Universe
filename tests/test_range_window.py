"""The range must contain the window's LAST bar.

THE FAULT. `on_time` built the range as soon as `now >= session_end`. In a
backtest that is correct: `_tick` runs only when a bar arrives, and the bar
that ends the window is ingested immediately before `on_time`. Live, `on_idle`
ticks every second, so `on_time` was reached at the STROKE of the window end —
before the feed had delivered the bar that closes it.

A timeframe bar only closes when the next one starts, so at that instant the
window's final bar is still forming and is not in the store yet.

  * a 30-minute window on M5 lost its 19:25 bar — six bars became five, and
    every live range was silently narrower than the backtest's;
  * London's 15-minute window on M15 is exactly ONE bar, so it lost the only
    one: "No bars inside range window ... session skipped", every single day.

Seen in a real journal on 2026-08-18, and reproduced here by driving the live
loop through a full window with bars flowing normally.

THE FIX. `ingest_bar` records `last_ingested_time`, and the range waits until
that reaches `session_end - one bar`. It is deliberately NOT `last_bar_time`,
which is set in `on_bar_closed` — that runs AFTER `on_time` and returns early
outside the trading window, so gating on it deadlocks: no range, so
`on_bar_closed` bails, so nothing is recorded, so no range.

    python -m pytest tests/test_range_window.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                                              # noqa: E402

from orb.bars import Bar                                   # noqa: E402
from orb.broker import SimBroker                           # noqa: E402
from orb.live_trader import LiveTrader                     # noqa: E402
from orb.logger import RbeaLogger                          # noqa: E402
from orb.runconfig import RunConfig                        # noqa: E402

BASE = datetime(2026, 8, 18)


def m1(minute, price):
    t = BASE + timedelta(minutes=minute)
    return Bar(t, price, price, price, price, 1)


class Clock:
    """A wall clock that follows the feed, as a live one does: idle polls creep
    forward a few seconds, a delivered bar sets it just past that bar's close."""

    def __init__(self, start):
        self.now = start

    def __call__(self, bar=None):
        if bar is not None:
            self.now = max(self.now, bar.time + timedelta(seconds=40))
        else:
            self.now += timedelta(seconds=5)
        return self.now


class Feed:
    """One bar every few polls, so `on_idle` runs in between — as live."""

    def __init__(self, bars, every=4):
        self.bars = list(bars)
        self.n = 0
        self.every = every

    def start(self):
        pass

    def stop(self):
        pass

    def poll(self, timeout=1.0):
        self.n += 1
        if self.bars and self.n % self.every == 0:
            return self.bars.pop(0)
        return None


def live_through_window(engine, session, first_minute, n_bars, tf=None):
    """Run the live loop from before a range window to well after it."""
    cfg = RunConfig.load(engine).app_config({})
    for s in cfg.sessions.values():
        s.enabled = (s.name == session)
        if s.enabled and tf:
            s.signal_timeframe = tf
    bars = [m1(first_minute + i, 4455 + (i % 5)) for i in range(n_bars)]
    trader = LiveTrader(cfg, broker=SimBroker(cfg.symbol, 100000.0),
                        feed=Feed(bars),
                        logger=RbeaLogger(level=0))
    trader.run(poll_seconds=0, max_polls=n_bars * 6,
               now_fn=Clock(BASE + timedelta(minutes=first_minute)))
    return trader.engine.engines[0].strategy


# --------------------------------------------------------------------------
def test_london_m15_window_is_not_skipped():
    """The regression. One M15 bar covers the whole 03:00-03:15 window, so
    losing it loses the session."""
    strat = live_through_window("orb_reverse", "london", 170, 50)
    assert strat.range_computed, "the range was never built"
    assert strat.range_valid, "the session was skipped"
    assert strat.range_high > strat.range_low


def test_the_windows_last_bar_is_included():
    """A 30-minute window on M5 is six bars, 19:00 through 19:25. The 19:25 one
    is the bar that was being dropped."""
    strat = live_through_window("orb", "asia", 19 * 60 - 5, 60)
    bars = strat.store.window(strat.session_start, strat.session_end)
    opens = [b.time.strftime("%H:%M") for b in bars]
    assert opens == ["19:00", "19:05", "19:10", "19:15", "19:20", "19:25"], opens


def test_the_range_matches_the_bars_it_should_have_used():
    """High and low must come from the full window, not a truncated one."""
    strat = live_through_window("orb", "asia", 19 * 60 - 5, 60)
    bars = strat.store.window(strat.session_start, strat.session_end)
    assert strat.range_high == max(b.high for b in bars)
    assert strat.range_low == min(b.low for b in bars)


def test_ingest_bar_is_what_records_the_time():
    """Not `on_bar_closed`. Gating on that field deadlocks — it runs after
    `on_time` and returns early outside the trading window."""
    import inspect

    from orb.engines.orb.strategy import OrbStrategy
    src = inspect.getsource(OrbStrategy.ingest_bar)
    assert "last_ingested_time" in src, (
        "ingest_bar must record the time, or the range gate deadlocks")


def test_a_genuinely_empty_window_is_still_skipped():
    """Deferring must not become waiting forever. A weekend or holiday window
    has no bars at all, and once data has flowed past it the session is
    correctly reported as skipped."""
    cfg = RunConfig.load("orb_reverse").app_config({})
    for s in cfg.sessions.values():
        s.enabled = (s.name == "london")
    # bars only AFTER the window: 03:20 onward, nothing inside 03:00-03:15
    bars = [m1(200 + i, 4455 + (i % 5)) for i in range(40)]
    trader = LiveTrader(cfg, broker=SimBroker(cfg.symbol, 100000.0),
                        feed=Feed(bars), logger=RbeaLogger(level=0))
    trader.run(poll_seconds=0, max_polls=240,
               now_fn=Clock(BASE + timedelta(minutes=200)))
    strat = trader.engine.engines[0].strategy
    assert strat.range_computed, "an empty window must still resolve"
    assert not strat.range_valid, "an empty window cannot produce a range"


# --------------------------------------------------------------------------
# starting INSIDE a window: the range would have a hole in it
# --------------------------------------------------------------------------
def start_inside_the_window():
    """EA starts 03:07. History reaches 02:57, the live feed begins 03:07 —
    so 02:58..03:06 is covered by neither, and that is half the window."""
    cfg = RunConfig.load("orb_reverse").app_config({})
    for s in cfg.sessions.values():
        s.enabled = (s.name == "london")
    warm = [m1(160 + i, 4450 + (i % 7)) for i in range(18)]    # 02:40 .. 02:57
    live = [m1(187 + i, 4470 + (i % 3)) for i in range(30)]    # 03:07 .. 03:36
    trader = LiveTrader(cfg, broker=SimBroker(cfg.symbol, 100000.0),
                        feed=Feed(live), logger=RbeaLogger(level=0))
    for b in warm:
        trader.engine.warmup_bar(b)
    trader._warmup_bars = list(warm)
    trader.run(poll_seconds=0, max_polls=200,
               now_fn=Clock(BASE + timedelta(minutes=187)))
    return trader.engine.engines[0].strategy


def test_a_window_with_a_hole_is_not_traded():
    """`_is_late_session` does NOT catch this — the EA started before the
    window ended, so by that test it is not late. Without this check the
    session traded a range built from a fraction of its bars."""
    strat = start_inside_the_window()
    assert not strat.range_valid, "a range with a hole was accepted"
    assert strat._session_blocked, "the session was not skipped"


def test_a_window_after_the_gap_is_unaffected():
    """The ordinary case: the EA starts before the window, so the gap sits
    entirely in the past and must not block anything."""
    strat = live_through_window("orb_reverse", "london", 170, 50)
    assert strat.range_valid
    assert not strat._session_blocked


def test_no_coverage_gap_means_no_check():
    """A backtest has no gap at all, so the test is inert there."""
    from orb.engines.orb.strategy import OrbStrategy
    strat = OrbStrategy.__new__(OrbStrategy)
    strat.coverage_gap = None
    assert strat._range_window_has_a_hole() is False


def test_backtest_path_is_unchanged():
    """A backtest ingests the closing bar before `on_time`, so the gate is
    already satisfied every time and nothing defers. tools/golden_master.py
    proves the same over 24 real backtests."""
    from orb.backtest import run_backtest
    cfg = RunConfig.load("orb").app_config({})
    for s in cfg.sessions.values():
        s.enabled = (s.name == "asia")
    bars = [m1(19 * 60 - 5 + i, 4455 + (i % 5)) for i in range(120)]
    result = run_backtest(cfg, bars, logger=RbeaLogger(level=0))
    assert result.bars_processed == len(bars)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
