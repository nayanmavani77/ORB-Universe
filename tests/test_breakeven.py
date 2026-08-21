"""Break even: move the stop to the ENTRY once the trade is far enough ahead.

    trigger  = `breakeven_trigger_r` x the risk the trade was OPENED with
    action   = stop loss -> entry price, once per trade

Two modelling decisions are pinned here because they change results rather
than just code:

  * The stop moves DURING the bar that reaches the trigger, and that same bar
    is then walked for stops. A bar that runs to the trigger and comes back
    through the entry therefore closes flat. Four numbers per bar cannot say
    whether the high or the low came first, so the worse order is assumed --
    the same choice the simulator already makes when one bar touches both the
    stop and the target.
  * R is measured against the risk recorded AT ENTRY, never against the stop
    at exit. Break-even puts the stop on the entry, so recomputing risk there
    would report every winner that passed through break-even as 0R and would
    corrupt `total_r`, which is what every sweep ranks on.

The bars are synthetic and shaped so each outcome is checkable by hand.

    python -m pytest tests/test_breakeven.py -q
"""
from __future__ import annotations

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

#: the range every test builds. `sl_mode=full_range` puts a long's stop at
#: 4490, so a long entered at 4512 risks 22.0 and its 1R trigger is 4534.
LOW, HIGH = 4490.0, 4510.0


def bar(minute, o, h, l, c):
    return Bar(DAY + timedelta(minutes=minute), o, h, l, c, 1.0)


def cfg(breakeven: bool, trigger: float = 1.0, **over):
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
    s.risk_reward = 4.0                # target far away, so break-even decides
    s.lots = 1.0
    s.sl_mode = "full_range"
    s.log_level = "none"
    s.magic = 1
    s.breakeven = breakeven
    s.breakeven_trigger_r = trigger
    s.require_range_reentry = False
    for _k, _l, cat in s.news.items():
        cat.mode = "off"
    s.news_days, s.news_trading = "", "off"
    for k, v in over.items():
        setattr(s, k, v)
    app.sessions = {"t": s}
    return app


def range_bars():
    return [bar(600, 4500, HIGH, LOW, 4500),
            bar(601, 4500, HIGH, LOW, 4500)]


def long_entry():
    """Breakout close above 4510; the fill is the OPEN of the next bar."""
    return [bar(602, 4512, 4513, 4511, 4512)]


def run(app, tail):
    return run_backtest(app, range_bars() + long_entry() + tail, QUIET)


# ==========================================================================
# 1. the requirement
# ==========================================================================
def test_the_stop_moves_to_the_entry_once_the_trigger_is_reached():
    """Entry 4512, risk 22.0, so 1R is 4534. Touch it, then come back to the
    entry: the trade must close at 4512 for exactly nothing."""
    res = run(cfg(breakeven=True), [
        bar(603, 4512, 4535, 4513, 4530),      # high 4535 -> past the trigger
        bar(604, 4530, 4530, 4500, 4505),      # back through the entry
    ])
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_price == 4512.0
    assert t.net_profit == 0.0
    assert t.exit_reason == "BREAK EVEN stop hit"
    assert t.breakeven is True
    # the bar AFTER the one that armed it, because 603's low stops at 4513
    assert t.exit_time == DAY + timedelta(minutes=604)


def test_without_the_option_the_same_bars_run_to_the_original_stop():
    """The control. Identical bars, feature off -- the trade keeps the stop it
    was given and loses the full 1R."""
    res = run(cfg(breakeven=False), [
        bar(603, 4512, 4535, 4512, 4530),
        bar(604, 4530, 4530, 4500, 4505),
        bar(605, 4505, 4505, 4489, 4489),      # reaches the 4490 stop
    ])
    t = res.trades[0]
    assert t.exit_price == LOW
    assert t.exit_reason == "STOP LOSS hit"
    assert t.breakeven is False
    assert t.r_multiple == pytest.approx(-1.0)


def test_off_by_default():
    s = StrategyConfig()
    assert s.breakeven is False
    assert s.breakeven_trigger_r == 1.0


# ==========================================================================
# 2. the same-bar rule -- the pessimistic reading, pinned
# ==========================================================================
def test_one_bar_reaching_the_trigger_and_returning_closes_flat():
    """Story A. A SINGLE bar whose high passes the trigger and whose low comes
    back through the entry closes the trade at break-even -- the worse of the
    two orderings the four numbers allow."""
    res = run(cfg(breakeven=True), [
        bar(603, 4512, 4540, 4500, 4505),      # trigger AND entry, one bar
    ])
    t = res.trades[0]
    assert t.exit_reason == "BREAK EVEN stop hit"
    assert t.exit_price == 4512.0
    assert t.exit_time == DAY + timedelta(minutes=603)


def test_a_bar_that_stops_short_of_the_trigger_does_not_move_the_stop():
    """4533.99 is inside 1R (4534). Nothing moves, so the original stop is
    still what the trade dies on."""
    res = run(cfg(breakeven=True), [
        bar(603, 4512, 4533.99, 4512, 4530),
        bar(604, 4530, 4530, 4489, 4489),
    ])
    t = res.trades[0]
    assert t.breakeven is False
    assert t.exit_reason == "STOP LOSS hit"
    assert t.exit_price == LOW


# ==========================================================================
# 3. the trigger is configurable, and measured in R
# ==========================================================================
def test_a_higher_trigger_leaves_the_stop_alone_for_longer():
    """1.5R on a 22.0 risk is 4545. A bar reaching only 4540 clears the 1.0R
    trigger but not this one."""
    tail = [bar(603, 4512, 4540, 4512, 4530),
            bar(604, 4530, 4530, 4489, 4489)]
    assert run(cfg(True, trigger=1.0), tail).trades[0].breakeven is True
    assert run(cfg(True, trigger=1.5), tail).trades[0].breakeven is False


def test_the_trigger_scales_with_the_trades_own_risk():
    """mid_range halves the stop distance (4500 instead of 4490), so 1R is
    4524 rather than 4534 and a smaller move now arms break-even."""
    tail = [bar(603, 4512, 4530, 4512, 4528),      # past 4524, short of 4534
            bar(604, 4528, 4528, 4495, 4495)]
    assert run(cfg(True, sl_mode="mid_range"), tail).trades[0].breakeven is True
    assert run(cfg(True, sl_mode="full_range"), tail).trades[0].breakeven is False


# ==========================================================================
# 4. the R-multiple -- the bug this feature would otherwise introduce
# ==========================================================================
def test_a_winner_through_break_even_reports_its_full_r():
    """The whole point of recording the risk at entry. Target is 4.0R away;
    the trade passes the trigger first, so its stop is sitting ON the entry
    when it exits. It must still report +4R, not 0R."""
    res = run(cfg(breakeven=True), [
        bar(603, 4512, 4535, 4513, 4530),      # arms break-even
        bar(604, 4530, 4600, 4530, 4599),      # 4512 + 4 x 22 = 4600
    ])
    t = res.trades[0]
    assert t.breakeven is True
    assert t.exit_reason == "TAKE PROFIT hit"
    assert t.initial_risk == pytest.approx(22.0)
    assert t.r_multiple == pytest.approx(4.0, abs=1e-6)
    assert t.net_profit > 0


def test_r_is_identical_with_and_without_the_feature_when_it_never_fires():
    """A trade that never reaches the trigger must be numerically untouched --
    this is what keeps the golden master byte-identical."""
    tail = [bar(603, 4512, 4520, 4512, 4515),
            bar(604, 4515, 4515, 4489, 4489)]
    on, off = run(cfg(True), tail).trades[0], run(cfg(False), tail).trades[0]
    assert on.r_multiple == off.r_multiple == pytest.approx(-1.0)
    assert on.net_profit == off.net_profit
    assert on.exit_price == off.exit_price


# ==========================================================================
# 5. shorts
# ==========================================================================
def test_a_short_moves_its_stop_down_to_the_entry():
    """Mirror image. Break below 4490, fill at 4488, stop at 4510 (full range)
    so risk is 22.0 and the 1R trigger is 4466."""
    bars = range_bars() + [
        bar(602, 4488, 4489, 4487, 4488),      # breakout close below 4490
        bar(603, 4488, 4488, 4465, 4470),      # low 4465 -> past the trigger
        bar(604, 4470, 4495, 4470, 4494),      # back up through the entry
    ]
    t = run_backtest(cfg(breakeven=True), bars, QUIET).trades[0]
    assert t.direction == "SELL"
    assert t.breakeven is True
    assert t.exit_reason == "BREAK EVEN stop hit"
    assert t.exit_price == pytest.approx(t.entry_price)
    assert t.net_profit == pytest.approx(0.0)


# ==========================================================================
# 6. it fires at most once
# ==========================================================================
def test_the_stop_is_not_moved_twice():
    """Once break-even has fired the stop stays at the entry; a later, bigger
    excursion must not drag it up behind price. That would be a trailing stop,
    which is a different feature and was not asked for."""
    res = run(cfg(breakeven=True), [
        bar(603, 4512, 4535, 4513, 4530),      # arms at 4512
        bar(604, 4530, 4570, 4530, 4560),      # much further ahead
        bar(605, 4560, 4560, 4500, 4505),      # back through the entry
    ])
    t = res.trades[0]
    assert t.exit_price == 4512.0                # entry, not anything trailed
    assert t.net_profit == pytest.approx(0.0)


# ==========================================================================
# 7. config plumbing
# ==========================================================================
def test_the_sweep_axis_builds_both_states():
    from orb.engines.orb.grid import build, _breakeven
    app = cfg(breakeven=False)
    items = build(app, sessions=["ASIA"], timeframes=["M5"], orb_minutes=[30],
                  risk_reward=[2.0], news_modes=["INCLUDE_NEWS"],
                  sl_modes=["mid_range"], trade_caps=[0],
                  breakevens=[None, 1.0, 1.5])
    assert len(items) == 3
    assert [i.axes["breakeven"] for i in items] == ["off", "1R", "1.5R"]
    assert [i.cfg.strategy.breakeven for i in items] == [False, True, True]
    assert [i.cfg.strategy.breakeven_trigger_r for i in items][1:] == [1.0, 1.5]
    assert [i.run_name.rsplit("_", 1)[-1] for i in items] == \
        ["NOBE", "BE1", "BE1p5"]


def test_the_axis_reads_off_from_yaml_and_from_the_command_line():
    """`--set` hands values through as text and YAML turns `off` into False,
    so both spellings have to mean the same thing."""
    from orb.engines.orb.grid import _breakeven
    assert [_breakeven(v) for v in (None, False, "off", "OFF", "none")] == \
        [None] * 5
    assert [_breakeven(v) for v in (1, "1.5", 2.0)] == [1.0, 1.5, 2.0]
    with pytest.raises(ValueError):
        _breakeven("banana")
    with pytest.raises(ValueError):
        _breakeven(0.0) if False else _breakeven(-1)


def test_the_option_survives_a_round_trip_through_the_config():
    from orb.config import AppConfig
    app = cfg(breakeven=True, breakeven_trigger_r=1.5)
    back = AppConfig.from_dict(app.to_dict())
    s = back.sessions["t"]
    assert s.breakeven is True
    assert s.breakeven_trigger_r == 1.5


# ==========================================================================
# 8. the report
# ==========================================================================
def test_the_report_counts_what_break_even_did():
    from orb.report import compute_stats
    res = run(cfg(breakeven=True), [
        bar(603, 4512, 4535, 4513, 4530),
        bar(604, 4530, 4530, 4500, 4505),
    ])
    s = compute_stats(res)
    assert s["be_moved"] == 1
    assert s["be_flat"] == 1
    assert s["be_won"] == 0
    assert s["be_lost"] == 0


def test_a_run_with_the_feature_off_reports_nothing_for_it():
    from orb.report import compute_stats
    res = run(cfg(breakeven=False), [
        bar(603, 4512, 4535, 4512, 4530),
        bar(604, 4530, 4530, 4489, 4489),
    ])
    assert compute_stats(res)["be_moved"] == 0


# ==========================================================================
# 9. the live path
# ==========================================================================
class LiveBroker:
    """A broker that behaves like MT5 rather than like the simulator.

    It does NOT walk bars for stops -- the server does that live -- so the only
    thing break-even can be observed doing here is the `modify` call it sends.
    That call is the whole live implementation, which is why it is tested at
    this level rather than through P&L.
    """
    translate_levels = False
    prices_from_bars = False

    def __init__(self, stops_level_price=0.0, accept=True, price=4530.0):
        from orb.config import SymbolSpec
        self.spec = SymbolSpec(name="T", digits=2, point=0.01, tick_size=0.01,
                               volume_min=0.01, volume_max=100.0,
                               volume_step=0.01, value_per_price_unit=1.0)
        self.stops_level_price = stops_level_price
        self.accept = accept
        self.price = price
        self.modifies = []
        self.position = None
        self._ticket = 0

    digits = 2
    def ask(self): return self.price
    def bid(self): return self.price
    def price_for(self, is_buy): return self.price
    def reference_price(self, is_buy): return self.price
    def positions_count(self): return 1 if self.position else 0
    def position_for(self, instrument=""): return self.position
    def normalize_price(self, p): return round(p, 2)
    def normalize_lot(self, l): return l
    def sync_market(self, *a): pass
    def settle_bar(self, *a): pass
    def trades_opened_since(self, *a, **k): return 0
    def close_all(self, *a, **k): self.position = None

    def open_market(self, is_buy, lots, sl, comment, magic=0, price=None):
        from orb.broker import Position
        self._ticket += 1
        self.position = Position(ticket=self._ticket, is_buy=is_buy, lots=lots,
                                 entry_price=self.price, entry_time=DAY,
                                 sl=sl, magic=magic, comment=comment)
        return True, self.position, ""

    def modify(self, position, sl, tp):
        if not self.accept:
            return False, "invalid stops"
        self.modifies.append((position.ticket, sl, tp))
        position.sl, position.tp = sl, tp
        return True, ""


def live_strategy(broker, **over):
    from orb.engines.orb.strategy import OrbStrategy
    app = cfg(breakeven=True, **over)
    return OrbStrategy(app.sessions["t"], broker, logger=QUIET)


def armed_long(broker, entry=4512.0, risk=22.0):
    """A live position already open, as break-even expects to find it."""
    st = live_strategy(broker)
    broker.open_market(True, 1.0, entry - risk, "c", magic=1)
    p = broker.position
    p.entry_price = p.signal_entry = entry
    p.initial_risk = risk
    st._be_pos = p
    return st, p


def test_live_the_stop_move_is_sent_to_the_broker():
    b = LiveBroker()
    st, p = armed_long(b)
    st._maybe_breakeven(bar(603, 4512, 4535, 4513, 4530))    # high past 1R
    assert b.modifies == [(1, 4512.0, 0.0)]
    assert p.sl == 4512.0
    assert p.breakeven_at == 4512.0


def test_live_nothing_is_sent_before_the_trigger():
    b = LiveBroker()
    st, p = armed_long(b)
    st._maybe_breakeven(bar(603, 4512, 4533, 4513, 4530))    # short of 4534
    assert b.modifies == []
    assert p.breakeven_at == 0.0


def test_live_the_brokers_minimum_stop_distance_is_respected():
    """Price can retreat between the bar that reached the trigger and the
    moment the order goes out. A stop closer to the market than the broker
    allows must not be sent -- and must be retried, not written off."""
    b = LiveBroker(stops_level_price=5.0, price=4514.0)      # only 2.0 of room
    st, p = armed_long(b)
    st._maybe_breakeven(bar(603, 4512, 4535, 4513, 4530))
    assert b.modifies == []
    assert p.breakeven_at == 0.0            # NOT marked done

    b.price = 4530.0                        # room again on a later bar
    st._maybe_breakeven(bar(604, 4530, 4536, 4529, 4535))
    assert b.modifies == [(1, 4512.0, 0.0)]


def test_live_a_rejected_modify_leaves_the_original_stop_alone():
    b = LiveBroker(accept=False)
    st, p = armed_long(b)
    st._maybe_breakeven(bar(603, 4512, 4535, 4513, 4530))
    assert p.sl == 4490.0                   # still the original
    assert p.breakeven_at == 0.0            # so it will try again


def test_live_it_is_sent_once_not_on_every_bar():
    b = LiveBroker()
    st, p = armed_long(b)
    for m in (603, 604, 605):
        st._maybe_breakeven(bar(m, 4530, 4560, 4529, 4555))
    assert len(b.modifies) == 1


def test_live_a_cross_instrument_trade_measures_against_the_feed():
    """`translate_levels`: the bar is quoted on the feed's instrument and the
    fill on the broker's, tens of dollars apart. The trigger has to be measured
    where the bar lives, or it fires at a price that means nothing."""
    b = LiveBroker(price=4456.0)             # broker quotes 56 below the feed
    b.translate_levels = True
    st = live_strategy(b)
    b.open_market(True, 1.0, 4434.0, "c", magic=1)
    p = b.position
    p.entry_price, p.signal_entry, p.initial_risk = 4456.0, 4512.0, 22.0
    st._be_pos = p

    st._maybe_breakeven(bar(603, 4512, 4533, 4513, 4530))    # feed: short of 1R
    assert b.modifies == []
    st._maybe_breakeven(bar(604, 4530, 4535, 4529, 4534))    # feed: reaches it
    # moved to the BROKER's entry, not the feed's
    assert b.modifies == [(1, 4456.0, 0.0)]


def test_a_position_from_a_previous_session_is_not_managed():
    """A session rollover drops the held position. Yesterday's trade belongs to
    the range that opened it, and that range is gone."""
    b = LiveBroker()
    st, p = armed_long(b)
    st._be_pos = None                        # what the session reset does
    st._maybe_breakeven(bar(603, 4512, 4535, 4513, 4530))
    assert b.modifies == []


def test_a_closed_position_is_released():
    """Once the trade is gone the strategy must stop holding it, or a restart
    of the same session would try to move a stop on a ticket that no longer
    exists."""
    b = LiveBroker()
    st, p = armed_long(b)
    b.position = None                        # the server closed it
    st._maybe_breakeven(bar(603, 4512, 4535, 4513, 4530))
    assert st._be_pos is None
    assert b.modifies == []
