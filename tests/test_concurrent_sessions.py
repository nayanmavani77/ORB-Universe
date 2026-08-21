"""Two sessions holding the SAME instrument at the same time.

The position slot used to be one per INSTRUMENT. Two sessions sharing a clock
fought over it -- whichever fired first took it and the other was silently
refused -- so overlapping sessions were rejected outright by `validate_sessions`.

The slot is now one per SESSION, keyed by `(instrument, magic)`. ORB and
reverse-ORB can both hold gold through the same London hours, each managing
only its own trade. The magic uniqueness rule is what makes that key safe, and
it is still enforced.

What has to be true, and is pinned here:

  * each session opens its own position; neither blocks the other
  * each stop and target is walked independently against the same bars
  * a stop time closes only the session that reached it
  * equity reflects BOTH positions -- risk stacks, and the report must show it
  * one session on one instrument is byte-for-byte what it always was

    python -m pytest tests/test_concurrent_sessions.py -q
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orb.backtest import run_backtest                            # noqa: E402
from orb.bars import Bar                                         # noqa: E402
from orb.broker import SimBroker                                 # noqa: E402
from orb.config import AppConfig, StrategyConfig, SymbolSpec     # noqa: E402
from orb.logger import RbeaLogger                                # noqa: E402

DAY = datetime(2026, 3, 2)
QUIET = RbeaLogger(level=0)
SPEC = SymbolSpec(name="T", digits=2, point=0.01, tick_size=0.01,
                  volume_min=0.01, volume_max=100.0, volume_step=0.01,
                  value_per_price_unit=1.0)


def bar(minute, o, h, l, c, instrument=""):
    b = Bar(DAY + timedelta(minutes=minute), o, h, l, c, 1.0)
    b.instrument = instrument
    return b


# ==========================================================================
# 1. the broker's slot
# ==========================================================================
def broker():
    return SimBroker(spec=SPEC)


def test_two_magics_hold_the_same_instrument_at_once():
    b = broker()
    b.set_market(100.0, DAY)
    ok1, p1, _ = b.open_market(True, 1.0, 90.0, "a", magic=1)
    ok2, p2, _ = b.open_market(False, 1.0, 110.0, "b", magic=2)
    assert ok1 and ok2
    assert p1.ticket != p2.ticket
    assert len(b.positions) == 2


def test_one_magic_still_holds_only_one():
    """The rule the strategy relies on: a session that is already in a trade
    must not open a second one."""
    b = broker()
    b.set_market(100.0, DAY)
    assert b.open_market(True, 1.0, 90.0, "a", magic=1)[0] is True
    ok, pos, err = b.open_market(True, 1.0, 90.0, "a", magic=1)
    assert ok is False and pos is None
    assert err == "position already open"


def test_a_session_counts_only_its_own_positions():
    """`positions_count()` is how the strategy asks 'am I in a trade?'. If it
    answered for everyone, one session would permanently block the other."""
    b = broker()
    b.set_market(100.0, DAY)
    b.open_market(True, 1.0, 90.0, "a", magic=1)
    assert b.positions_count(instrument="", magic=1) == 1
    assert b.positions_count(instrument="", magic=2) == 0
    assert b.positions_count() == 1              # unscoped: the whole account


def test_a_view_scopes_both_instrument_and_session():
    b = broker()
    b.add_instrument("gc", SPEC)
    b.set_market(100.0, DAY, "gc")
    b.open_market(True, 1.0, 90.0, "a", magic=1, instrument="gc")
    gc1, gc2 = b.view("gc", 1), b.view("gc", 2)
    assert gc1.positions_count() == 1
    assert gc2.positions_count() == 0
    assert gc1.position_for().magic == 1
    assert gc2.position_for() is None


def test_each_position_is_walked_against_the_same_bar():
    """One bar, two positions, opposite directions. The long's stop and the
    short's target are both inside it, so both must resolve -- independently."""
    b = broker()
    b.set_market(100.0, DAY)
    _, long_pos, _ = b.open_market(True, 1.0, 95.0, "a", magic=1)
    _, short_pos, _ = b.open_market(False, 1.0, 110.0, "b", magic=2)
    short_pos.tp = 94.0
    b.process_bar(bar(1, 100.0, 101.0, 93.0, 96.0))
    assert len(b.trades) == 2
    reasons = {t.exit_reason for t in b.trades}
    assert reasons == {"STOP LOSS hit", "TAKE PROFIT hit"}
    assert not b.positions


def test_close_all_scoped_to_one_session_leaves_the_other_running():
    """A stop time must not flatten a trade that belongs to another session
    with hours of its own left to run."""
    b = broker()
    b.set_market(100.0, DAY)
    b.open_market(True, 1.0, 90.0, "a", magic=1)
    b.open_market(True, 1.0, 90.0, "b", magic=2)
    b.close_all("stop time", instrument="", magic=1)
    assert len(b.trades) == 1
    assert b.trades[0].ticket == 1
    assert [p.magic for p in b.positions.values()] == [2]


def test_close_all_unscoped_still_flattens_everything():
    b = broker()
    b.set_market(100.0, DAY)
    b.open_market(True, 1.0, 90.0, "a", magic=1)
    b.open_market(True, 1.0, 90.0, "b", magic=2)
    b.close_all("shutdown")
    assert len(b.trades) == 2
    assert not b.positions


def test_equity_reflects_both_open_positions():
    """Risk stacks. Two 1-lot longs 5.0 in front is 10.0 of open profit, not
    5.0 -- if this under-reported, the drawdown in every report would be a
    fraction of the real one."""
    b = broker()
    b.set_market(100.0, DAY)
    b.open_market(True, 1.0, 90.0, "a", magic=1)
    b.open_market(True, 1.0, 90.0, "b", magic=2)
    b.set_market(105.0, DAY)
    assert b.floating_pnl() == pytest.approx(10.0)


def test_positions_on_different_instruments_stay_apart():
    """The original guarantee, unchanged: an ES bar must never be walked
    against a gold position."""
    b = broker()
    b.add_instrument("gc", SPEC)
    b.add_instrument("es", SPEC)
    b.set_market(100.0, DAY, "gc")
    b.set_market(100.0, DAY, "es")
    b.open_market(True, 1.0, 95.0, "a", magic=1, instrument="gc")
    b.open_market(True, 1.0, 95.0, "b", magic=2, instrument="es")
    b.process_bar(bar(1, 100.0, 101.0, 94.0, 96.0, instrument="es"))
    assert len(b.trades) == 1
    assert b.trades[0].instrument == "es"
    assert b.position_for("gc") is not None


# ==========================================================================
# 2. end to end -- two engines, one instrument, one clock
# ==========================================================================
def app_with(*sessions):
    app = AppConfig()
    app.symbol = SPEC
    app.backtest.initial_balance = 100_000.0
    app.backtest.spread_points = 0.0
    app.backtest.slippage_points = 0.0
    app.backtest.commission_per_lot_per_side = 0.0
    app.sessions = {s.name: s for s in sessions}
    return app


def session(name, magic, engine="orb", **over):
    s = StrategyConfig()
    s.name, s.enabled, s.magic = name, True, magic
    s.engine, s.engine_options = engine, {}
    s.signal_timeframe = "M1"
    s.range_start, s.range_end, s.stop_time = "10:00", "10:02", "10:58"
    s.risk_reward, s.lots = 2.0, 1.0
    s.sl_mode = "full_range"
    s.log_level = "none"
    s.require_range_reentry = False
    for _k, _l, cat in s.news.items():
        cat.mode = "off"
    s.news_days, s.news_trading = "", "off"
    for k, v in over.items():
        setattr(s, k, v)
    return s


def breakout_bars():
    """Build the 4490..4510 range, then close above it and run up."""
    return ([bar(600, 4500, 4510, 4490, 4500),
             bar(601, 4500, 4510, 4490, 4500),
             bar(602, 4512, 4515, 4511, 4514),
             # the fill is this bar's OPEN, so the entry is 4512
             bar(603, 4512, 4513, 4511, 4512)]
            + [bar(604 + i, 4520, 4520, 4520, 4520) for i in range(40)])


def test_an_overlapping_pair_no_longer_raises():
    """The config that used to be rejected outright."""
    app = app_with(session("a", 1), session("b", 2))
    app.validate_sessions()                       # must not raise


def test_two_sessions_on_one_instrument_both_trade():
    """Same engine, same hours, different R:R. Before, the second session got
    nothing at all -- the first held the only slot."""
    app = app_with(session("a", 1, risk_reward=2.0),
                   session("b", 2, risk_reward=3.0))
    res = run_backtest(app, breakout_bars(), QUIET)
    by = {t.session_name: t for t in res.trades}
    assert set(by) == {"a", "b"}
    assert by["a"].entry_price == by["b"].entry_price      # same signal
    assert by["a"].tp != by["b"].tp                        # own targets


def test_a_single_session_run_is_untouched():
    """The control. One session on one instrument must behave exactly as it
    always did -- this is what keeps the golden master byte-identical."""
    one = run_backtest(app_with(session("a", 1)), breakout_bars(), QUIET)
    assert len(one.trades) == 1
    t = one.trades[0]
    assert t.session_name == "a"
    assert t.entry_price == 4512.0


def test_opposite_engines_hold_the_instrument_together():
    """The requirement: ORB long and reverse-ORB short on the same gold, same
    hours. Reverse fades the break, so the two are on opposite sides."""
    app = app_with(session("orb", 1, engine="orb"),
                   session("rev", 2, engine="orb_reverse",
                           engine_options={"sl_range_mult": 0.5,
                                           "direction": "reverse"}))
    res = run_backtest(app, breakout_bars(), QUIET)
    sides = {t.session_name: t.direction for t in res.trades}
    assert sides == {"orb": "BUY", "rev": "SELL"}


def test_one_sessions_stop_time_does_not_close_the_other():
    """`a` stops at 10:30, `b` runs to 10:58. When `a` flattens, `b` must still
    be open -- on the same instrument."""
    app = app_with(session("a", 1, stop_time="10:30"),
                   session("b", 2, stop_time="10:58"))
    res = run_backtest(app, breakout_bars(), QUIET)
    by = {t.session_name: t for t in res.trades}
    assert by["a"].exit_reason == "stop time"
    assert by["a"].exit_time < by["b"].exit_time
