"""Pullback entries: enter on the RETURN to the level, not on the breakout.

    long  — price breaks above the range high, then comes back and TOUCHES it
    short — price breaks below the range low, then comes back and TOUCHES it

A touch is enough. The bar need not close at the level, and the entry fires
DURING the bar that is still forming — which is why detection runs on the base
bar (`on_price`) rather than on the closed signal bar (`on_bar_closed`).

The bars here are synthetic and shaped so the intended behaviour is
unambiguous, and so the entry price is checkable by hand.

    python -m pytest tests/test_pullback.py -q
"""
from __future__ import annotations

import copy
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orb.backtest import run_backtest                            # noqa: E402
from orb.bars import Bar                                         # noqa: E402
from orb.config import AppConfig, StrategyConfig, SymbolSpec     # noqa: E402
from orb.logger import RbeaLogger                                # noqa: E402

DAY = datetime(2026, 3, 2)
QUIET = RbeaLogger(level=0)

#: the range every test below builds: 4490 .. 4510
LOW, HIGH = 4490.0, 4510.0


def bar(minute, o, h, l, c):
    return Bar(DAY + timedelta(minutes=minute), o, h, l, c, 1.0)


def cfg(pullback: bool, **over):
    app = AppConfig()
    app.symbol = SymbolSpec(name="T", digits=2, point=0.01, tick_size=0.01,
                            volume_min=0.01, volume_max=100.0,
                            volume_step=0.01, value_per_price_unit=1.0)
    app.backtest.initial_balance = 100_000.0
    app.backtest.spread_points = 0.0
    app.backtest.slippage_points = 0.0
    app.backtest.commission_per_lot_per_side = 0.0

    s = StrategyConfig()
    s.name, s.enabled = "t", True
    s.engine, s.engine_options = "orb", {}
    s.signal_timeframe = "M1"
    s.range_start, s.range_end, s.stop_time = "10:00", "10:02", "10:58"
    s.risk_reward = 2.0
    s.lots = 1.0
    s.sl_mode = "full_range"          # SL at the far side: long -> 4490
    s.log_level = "none"
    s.magic = 1
    s.pullback_entry = pullback
    s.require_range_reentry = False
    for _k, _l, cat in s.news.items():
        cat.mode = "off"
    s.news_days, s.news_trading = "", "off"
    for k, v in over.items():
        setattr(s, k, v)
    app.sessions = {"t": s}
    return app


def range_bars():
    """Two bars that build the 4490..4510 range."""
    return [bar(600, 4500, HIGH, LOW, 4500),
            bar(601, 4500, HIGH, LOW, 4500)]


def flat(start, n, price):
    return [bar(start + i, price, price, price, price) for i in range(n)]


# ==========================================================================
# 1. the requirement — long
# ==========================================================================
def test_long_enters_on_the_pullback_touch_not_on_the_breakout():
    """The breakout bar must NOT trade. The trade happens later, when price
    comes back and touches the range high."""
    bars = range_bars() + [
        bar(602, 4512, 4515, 4511, 4514),      # breakout close above 4510
        bar(603, 4514, 4516, 4513, 4515),      # still above — no touch
        bar(604, 4515, 4515, HIGH, 4512),      # LOW touches 4510 exactly
    ] + flat(605, 40, 4530)                     # then away to target

    res = run_backtest(cfg(pullback=True), bars, QUIET)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.direction == "BUY"
    # entered AT the level, on the bar that touched it — not at 4512/4514
    assert t.entry_price == HIGH
    assert t.entry_time == DAY + timedelta(minutes=604)


def test_without_the_option_the_same_bars_enter_on_the_breakout():
    """The control. Same bars, feature off — the original behaviour, entering
    straight after the breakout bar closes."""
    bars = range_bars() + [
        bar(602, 4512, 4515, 4511, 4514),
        bar(603, 4514, 4516, 4513, 4515),
        bar(604, 4515, 4515, HIGH, 4512),
    ] + flat(605, 40, 4530)

    res = run_backtest(cfg(pullback=False), bars, QUIET)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_time == DAY + timedelta(minutes=603)
    assert t.entry_price != HIGH


# ==========================================================================
# 2. the requirement — short
# ==========================================================================
def test_short_enters_on_the_pullback_touch_of_the_range_low():
    bars = range_bars() + [
        bar(602, 4488, 4489, 4485, 4486),      # breakout close below 4490
        bar(603, 4486, 4487, 4484, 4485),
        bar(604, 4485, LOW, 4485, 4488),       # HIGH touches 4490 exactly
    ] + flat(605, 40, 4470)

    res = run_backtest(cfg(pullback=True), bars, QUIET)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.direction == "SELL"
    assert t.entry_price == LOW
    assert t.entry_time == DAY + timedelta(minutes=604)


# ==========================================================================
# 3. a TOUCH is enough — no close required
# ==========================================================================
def test_a_wick_that_touches_is_enough_even_closing_far_above():
    """The bar dips to the level and closes well above it. That is a touch, and
    a resting limit order would have been filled — so this must trade."""
    bars = range_bars() + [
        bar(602, 4512, 4515, 4511, 4514),
        bar(603, 4514, 4520, HIGH, 4519),      # wick to 4510, closes 4519
    ] + flat(604, 40, 4530)

    res = run_backtest(cfg(pullback=True), bars, QUIET)
    assert len(res.trades) == 1
    assert res.trades[0].entry_price == HIGH
    assert res.trades[0].entry_time == DAY + timedelta(minutes=603)


def test_price_that_never_returns_never_trades():
    """No touch, no trade — the session simply passes."""
    bars = range_bars() + [
        bar(602, 4512, 4515, 4511, 4514),
    ] + flat(603, 40, 4530)                    # never comes back to 4510

    res = run_backtest(cfg(pullback=True), bars, QUIET)
    assert res.trades == []


def test_the_entry_fires_during_the_forming_signal_bar():
    """The real point of running on base bars. On M5 the touch lands inside a
    bar that will not close for another four minutes, and the trade must not
    wait for it.

    Bars 600-604 are the 10:00 M5 bar and build the range. Bars 605-609 are the
    10:05 M5 bar and break out — known to the EA when bar 610 arrives. Bar 611
    then touches the level, four minutes before the 10:10 M5 bar closes.
    """
    bars = [bar(600 + i, 4500, HIGH, LOW, 4500) for i in range(5)]     # range
    bars += [bar(605 + i, 4514, 4516, 4512, 4514) for i in range(5)]   # breakout
    bars += [bar(610, 4514, 4516, 4512, 4514),                         # no touch
             bar(611, 4514, 4515, HIGH, 4514)]                         # TOUCH
    bars += flat(612, 40, 4530)

    app = cfg(pullback=True)
    app.sessions["t"].signal_timeframe = "M5"
    app.sessions["t"].range_start, app.sessions["t"].range_end = "10:00", "10:05"
    res = run_backtest(app, bars, QUIET)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.entry_price == HIGH
    # in at 10:11 — the M5 bar covering it does not close until 10:15
    assert t.entry_time == DAY + timedelta(minutes=611)


# ==========================================================================
# 4. the level, the stop and the target
# ==========================================================================
def test_the_stop_is_measured_from_the_level_not_from_the_breakout():
    """full_range on a long puts the stop at the range low. Entering at 4510
    with the stop at 4490 is 20 points of risk, and RR 2 puts the target 40
    above the entry."""
    bars = range_bars() + [
        bar(602, 4512, 4515, 4511, 4514),
        bar(603, 4514, 4516, HIGH, 4515),
    ] + flat(604, 60, 4520)

    res = run_backtest(cfg(pullback=True), bars, QUIET)
    t = res.trades[0]
    assert t.entry_price == HIGH
    assert t.sl == LOW
    assert round(t.tp, 6) == round(HIGH + 2.0 * (HIGH - LOW), 6) == 4550.0


# ==========================================================================
# 5. the guards still apply
# ==========================================================================
def test_a_pending_level_dies_at_the_session_stop_time():
    """The touch comes after the stop time, so it must not trade."""
    bars = range_bars() + [bar(602, 4512, 4515, 4511, 4514)]
    bars += flat(603, 55, 4530)                       # away all session
    bars += [bar(658, 4515, 4516, HIGH, 4515)]        # touch at 10:58 = stop
    bars += flat(659, 5, 4530)

    res = run_backtest(cfg(pullback=True), bars, QUIET)
    assert res.trades == []


def test_the_session_cap_is_honoured():
    """One trade allowed; a second pullback in the same session must not fire."""
    bars = range_bars() + [
        bar(602, 4512, 4515, 4511, 4514),
        bar(603, 4514, 4516, HIGH, 4515),      # touch -> trade 1
        bar(604, 4515, 4515, 4489, 4489),      # stopped out at the range low
        bar(605, 4495, 4499, 4494, 4498),      # back inside
        bar(606, 4512, 4515, 4511, 4514),      # a second breakout
        bar(607, 4514, 4516, HIGH, 4515),      # and a second touch
    ] + flat(608, 30, 4530)

    app = cfg(pullback=True, max_trades_per_session=1)
    res = run_backtest(app, bars, QUIET)
    assert len(res.trades) == 1


def test_no_second_entry_while_a_trade_is_running():
    bars = range_bars() + [
        bar(602, 4512, 4515, 4511, 4514),
        bar(603, 4514, 4516, HIGH, 4515),      # touch -> in
        bar(604, 4515, 4516, HIGH, 4515),      # touches again, already in
    ] + flat(605, 40, 4520)

    res = run_backtest(cfg(pullback=True), bars, QUIET)
    assert len(res.trades) == 1


# ==========================================================================
# 6. configuration
# ==========================================================================
def test_the_option_is_off_by_default():
    assert StrategyConfig().pullback_entry is False


def test_it_can_be_set_per_session_and_per_instrument_cell():
    """It is an ordinary session field, so the (session x instrument) matrix
    can switch it on for one symbol and leave it off for another."""
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    raw = yaml.safe_load(open(os.path.join(root, "orb", "engines", "orb",
                                           "config.yaml"), encoding="utf-8"))
    raw["sessions"] = {"ny": {
        "enabled": True, "range_start": "09:30", "range_end": "10:00",
        "stop_time": "16:55", "pullback_entry": True,
        "instruments": {"gc": {}, "es": {"pullback_entry": False}}}}
    app = AppConfig.from_dict(raw)
    assert app.sessions["ny_gc"].pullback_entry is True
    assert app.sessions["ny_es"].pullback_entry is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
