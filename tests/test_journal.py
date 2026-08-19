"""The journal has to be readable by a person at 3am.

Two complaints from a real operator, both fixed here and pinned so they cannot
come back:

  * "it says new session instead of the session name" — with several engines
    interleaved in one journal, a line reading "New session" or "Stop Time
    reached" with no owner is unreadable. Every strategy line now carries the
    session it belongs to, bound once in `Engine.__init__` rather than at 78
    call sites, so a line added later cannot forget.
  * "SL, TP adjustment is not logged" — the take profit is a SECOND call to
    the broker, made after the fill because it can only be worked out from the
    real fill price. It was reported at DEBUG, i.e. invisible at normal level,
    so the journal showed a TP in the fill summary with nothing to say whether
    the broker had accepted it. On a live account that is the whole trade.

Also pinned: timestamps carry seconds (the EA now acts within a second of a bar
closing, and minute resolution hid exactly that), and the date is a banner
rather than a repeat on every line.

    python -m pytest tests/test_journal.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                                              # noqa: E402

from orb.backtest import run_backtest                      # noqa: E402
from orb.bars import Bar                                   # noqa: E402
from orb.logger import SOURCE_WIDTH, RbeaLogger            # noqa: E402
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


def m1(minute, price, spread=0.0):
    t = BASE + timedelta(minutes=minute)
    return Bar(t, price, price + spread, price - spread, price, 1)


class Capture(RbeaLogger):
    """Collects the finished lines instead of printing them."""

    def __init__(self, **kw):
        super().__init__(level=1, show_time=True, **kw)
        self.lines = []

    def _emit(self, line):
        self.lines.append(line)


def journal(bars=None, session="london", engine="orb_reverse"):
    cfg = RunConfig.load(engine).app_config({})
    for s in cfg.sessions.values():
        s.enabled = in_window(s, session) and (s.instrument or "gc") == "gc"
        if s.enabled:
            s.signal_timeframe = "M1"
            s.range_start, s.range_end, s.stop_time = "03:00", "03:01", "09:30"
    if bars is None:
        # a full round trip: range, breakout, fill, take profit, exit
        bars = ([m1(180, 4450, spread=2.0), m1(181, 4450), m1(182, 4460)]
                + [m1(183 + i, 4458 - i * 3, spread=2.0) for i in range(8)])
    log = Capture()
    run_backtest(cfg, bars, logger=log)
    return log.lines


def find(lines, needle):
    return [ln for ln in lines if needle in ln]


# --------------------------------------------------------------------------
# 1. every line says which session it is
# --------------------------------------------------------------------------
def test_the_session_name_is_on_the_trading_lines():
    lines = journal()
    for needle in ("SESSION OPEN", "RANGE BUILT", "BREAKOUT", "FILLED", "EXIT"):
        hits = find(lines, needle)
        assert hits, f"no {needle} line at all"
        for ln in hits:
            assert "london" in ln, f"{needle} line has no session name: {ln}"


def test_the_session_column_is_aligned():
    """Ragged columns are what make an interleaved journal unreadable — and
    start-up lines, written before the engine has a clock, are the ones that
    used to sit a column to the left."""
    lines = [ln for ln in journal()
             if "|" in ln and not ln.startswith("----") and "=" not in ln[:3]]
    assert lines
    positions = {ln.index("|") for ln in lines}
    assert len(positions) == 1, f"the pipe moves around: {sorted(positions)}"


def test_two_sessions_are_told_apart():
    """The whole point. One logger, two names, no bleed."""
    log = Capture()
    a, b = log.for_session("asia"), log.for_session("new_york")
    a.clock_time = b.clock_time = BASE
    a.info("range window opened")
    b.info("range window opened")
    assert any("asia" in ln for ln in log.lines)
    assert any("new_york" in ln for ln in log.lines)
    assert len(log.lines) == 3, "date banner + one line each"


def test_a_session_logger_shares_the_duplicate_guard():
    """Suppression and the date banner must be global, or an interleaved
    journal repeats itself."""
    log = Capture()
    a = log.for_session("asia")
    a.clock_time = BASE
    a.info("same")
    a.info("same")
    assert len(find(log.lines, "same")) == 1


def test_a_session_logger_shares_the_once_keys():
    log = Capture()
    a, b = log.for_session("asia"), log.for_session("new_york")
    a.clock_time = b.clock_time = BASE
    assert a.first_time_this_session("k") is True
    assert b.first_time_this_session("k") is False


def test_a_long_session_name_cannot_break_the_layout():
    log = Capture()
    a = log.for_session("a_very_long_session_name")
    a.clock_time = BASE
    a.info("hello")
    line = find(log.lines, "hello")[0]
    assert line.index("|") == len("00:00:00  ") + len("INFO  ") + SOURCE_WIDTH + 1


# --------------------------------------------------------------------------
# 2. the take profit is reported
# --------------------------------------------------------------------------
def test_the_take_profit_being_set_is_reported():
    """It is a separate broker call after the fill. Its success used to be a
    DEBUG line, so a normal journal never showed whether it landed."""
    lines = journal()
    assert find(lines, "TP SET"), "the TP being accepted is not journalled"


def test_the_fill_reports_stop_risk_and_target():
    lines = find(journal(), "  SL ")
    assert lines, "no stop-loss line"
    ln = lines[0]
    for token in ("SL ", "risk ", "TP ", "reward ", "R:R "):
        assert token in ln, f"{token!r} missing from {ln}"


def test_the_exit_states_the_money_and_the_verdict():
    lines = find(journal(), "EXIT")
    assert lines, "no exit line"
    assert "USD" in lines[0]
    assert ("profit" in lines[0] or "loss" in lines[0] or "flat" in lines[0])


# --------------------------------------------------------------------------
# 3. time
# --------------------------------------------------------------------------
def test_timestamps_carry_seconds():
    """The EA acts within a second of a bar closing; minutes hide that."""
    stamped = [ln for ln in journal() if ln[:2].isdigit() and ln[2] == ":"]
    assert stamped, "nothing was stamped"
    head = stamped[0][:8]
    assert head.count(":") == 2, f"no seconds in {head!r}"


def test_the_date_is_a_banner_not_a_repeat():
    lines = journal()
    banners = [ln for ln in lines if ln.startswith("---- ")]
    assert len(banners) == 1, f"expected one date banner, got {len(banners)}"
    assert "2026-08-18" in banners[0]


def test_a_new_day_gets_its_own_banner():
    log = Capture()
    a = log.for_session("asia")
    for day in (18, 19):
        a.clock_time = datetime(2026, 8, day, 3, 0, 0)
        a.info(f"day {day}")
    assert len([ln for ln in log.lines if ln.startswith("---- ")]) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
