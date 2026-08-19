"""Many instruments, one system.

The requirement: the system trades more than gold. A user declares
`gc -> XAUUSDm`, `es -> US500`, `nq -> USTEC` in one `instruments:` block, points
each session at one of them, and everything downstream — backtest, sweep, report,
live — follows without another line of configuration.

What that costs, and what these tests pin:

  * **routing** — bars carry an `instrument` tag and only reach the sessions that
    trade that instrument. A gold bar must never move an ES range.
  * **valuation** — each instrument is priced with ITS OWN contract spec. The
    same 22-point move is $2,200 on gold and $1,100 on ES, and getting this
    wrong is silent: the trades all look right, only the money is fiction.
  * **positions** — one position PER instrument, not one per account. Gold being
    long may not block an ES entry.
  * **windows** — two sessions on DIFFERENT instruments may share a clock window
    (New York is New York for both). Two on the SAME instrument still may not:
    they would fight over one position slot.
  * **selection** — `--instruments gc,es` narrows a run, and a typo is an error
    rather than a backtest that quietly does nothing.
  * **sweeps** — the parameter grid runs once per instrument, and every result
    row says which instrument earned it.

Nothing here needs market data: the bars are synthetic and built so the intended
breakout is unambiguous, which also makes the arithmetic checkable by hand.

    python -m pytest tests/test_instruments.py -q
"""
from __future__ import annotations

import copy
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orb.backtest import make_clock, run_backtest                # noqa: E402
from orb.bars import Bar                                         # noqa: E402
from orb.broker import InstrumentView, SimBroker                 # noqa: E402
from orb.config import (AppConfig, InstrumentConfig,             # noqa: E402
                        StrategyConfig, SymbolSpec)
from orb.engine import MultiEngine                               # noqa: E402
from orb.logger import RbeaLogger                                # noqa: E402
from orb.outputs import instruments_of                           # noqa: E402
from orb.report import instrument_summary, trades_dataframe      # noqa: E402
from orb.runconfig import RunConfig                              # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "orb", "engines", "orb", "config.yaml")

DAY = datetime(2026, 3, 2)            # a Monday
QUIET = RbeaLogger(level=0)


# ==========================================================================
# scaffolding
# ==========================================================================
def bar(minute: int, price: float, instrument: str = "",
        high: float = None, low: float = None) -> Bar:
    """One M1 bar at `minute` past midnight, flat unless a range is given."""
    t = DAY + timedelta(minutes=minute)
    b = Bar(t, price, high if high is not None else price,
            low if low is not None else price, price, 1.0)
    b.instrument = instrument
    return b


def breakout_day(instrument: str, base: float, *, first_minute: int = 600,
                 span: float = 10.0, push: float = 22.0):
    """A day whose shape forces exactly one long breakout.

    Two bars build a `base ± span` range, one bar breaks the high, then the
    price walks up by `push` and stops. The move is the same NUMBER of points
    for every instrument, so the only thing that can make two instruments earn
    different money is their contract spec — which is the point.
    """
    out = [bar(first_minute, base, instrument,
               high=base + span, low=base - span),
           bar(first_minute + 1, base, instrument,
               high=base + span, low=base - span)]
    # the breakout bar: closes above the range high
    out.append(bar(first_minute + 2, base + span + 1.0, instrument,
                   high=base + span + 1.0, low=base))
    # the walk that carries it to target, then flat bars to the session stop
    for i in range(3, 60):
        step = min(push, (i - 2) * 2.0)
        price = base + span + step
        out.append(bar(first_minute + i, price, instrument,
                       high=price, low=price - 0.5))
    return out


def merge(*streams):
    """Interleave per-instrument streams exactly as `load_instrument_bars` does."""
    bars = [b for s in streams for b in s]
    bars.sort(key=lambda b: (b.time, b.instrument))
    return bars


def instrument(name, mt5, value_per_point, **kw):
    return InstrumentConfig(name=name, signal=f"{name.upper()}.FUT", mt5=mt5,
                            value_per_point=value_per_point, **kw)


def session(name, instrument_name, *, first_minute=600, magic=0):
    """A session windowed on the synthetic day above."""
    s = StrategyConfig()
    s.name, s.enabled = name, True
    s.engine, s.engine_options = "orb", {}
    s.instrument = instrument_name
    s.signal_timeframe = "M1"
    s.range_start = f"{first_minute // 60:02d}:{first_minute % 60:02d}"
    s.range_end = f"{(first_minute + 2) // 60:02d}:{(first_minute + 2) % 60:02d}"
    s.stop_time = f"{(first_minute + 58) // 60:02d}:{(first_minute + 58) % 60:02d}"
    s.risk_reward = 2.0
    s.fixed_lots = 1.0
    s.log_level = "none"
    s.magic = magic or (1000 + abs(hash(name)) % 100)
    for _key, _label, cat in s.news.items():
        cat.mode = "off"
    s.news_days, s.news_trading = "", "off"
    return s


def portfolio(*pairs, spread=0.0):
    """A two-instrument config: `pairs` are (instrument, session) tuples."""
    cfg = AppConfig()
    cfg.symbol = SymbolSpec(name="BASE", digits=2, point=0.01, tick_size=0.01,
                            volume_min=0.01, volume_max=100.0,
                            volume_step=0.01, value_per_price_unit=1.0)
    cfg.backtest.initial_balance = 100_000.0
    cfg.backtest.spread_points = spread
    cfg.backtest.slippage_points = 0.0
    cfg.backtest.commission_per_lot_per_side = 0.0
    cfg.strategy.log_level = "none"
    cfg.instruments = {i.name: i for i, _ in pairs}
    cfg.sessions = {s.name: s for _, s in pairs}
    return cfg


GC = instrument("gc", "XAUUSDm", 100.0)
ES = instrument("es", "US500", 50.0)
NQ = instrument("nq", "USTEC", 20.0)


# ==========================================================================
# 1. the requirement — two instruments, one run
# ==========================================================================
def test_two_instruments_trade_in_the_same_run():
    """Both instruments take their trade, in one backtest, on one balance."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")),
                    (copy.deepcopy(ES), session("es_ny", "es")))
    bars = merge(breakout_day("gc", 2400.0), breakout_day("es", 5000.0))

    res = run_backtest(cfg, bars, QUIET)

    by_instrument = {}
    for t in res.trades:
        by_instrument.setdefault(t.instrument, []).append(t)
    assert sorted(by_instrument) == ["es", "gc"], \
        f"expected one trade per instrument, got {by_instrument!r}"
    assert len(by_instrument["gc"]) == 1
    assert len(by_instrument["es"]) == 1


def test_each_instrument_is_valued_with_its_own_contract_spec():
    """The identical move must earn different money — that is what
    `value_per_point` means, and getting it wrong is invisible in the trade log."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")),
                    (copy.deepcopy(ES), session("es_ny", "es")))
    bars = merge(breakout_day("gc", 2400.0), breakout_day("es", 5000.0))

    res = run_backtest(cfg, bars, QUIET)
    money = {t.instrument: t.net_profit for t in res.trades}
    points = {t.instrument: abs(t.exit_price - t.entry_price)
              for t in res.trades}

    # same number of points on both...
    assert round(points["gc"], 6) == round(points["es"], 6)
    # ...and gold is worth exactly twice ES per point
    assert round(money["gc"], 6) == round(money["es"] * 2.0, 6)


def test_a_bar_only_reaches_the_sessions_that_trade_it():
    """Routing, directly: a gold bar must not touch the ES engine."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")),
                    (copy.deepcopy(ES), session("es_ny", "es")))
    broker = SimBroker(spec=cfg.symbol, initial_balance=100_000.0, logger=QUIET)
    for name, inst in cfg.instruments.items():
        broker.add_instrument(name, inst.spec(cfg.symbol))
    engine = MultiEngine(cfg.enabled_sessions(), broker, logger=QUIET)

    for tag in ("gc", "es"):
        picked = engine.engines_for(bar(600, 1.0, tag))
        assert [e.cfg.instrument for e in picked] == [tag]


def test_one_position_per_instrument_not_one_per_account():
    """Gold holding a position may not stop ES from entering."""
    broker = SimBroker(spec=SymbolSpec(value_per_price_unit=1.0),
                       initial_balance=100_000.0, logger=QUIET)
    broker.add_instrument("gc", GC.spec(SymbolSpec()))
    broker.add_instrument("es", ES.spec(SymbolSpec()))
    broker.set_market(2400.0, DAY, "gc")
    broker.set_market(5000.0, DAY, "es")

    broker.open_market(True, 1.0, 2390.0, "gc long", magic=1, instrument="gc")
    assert broker.positions_count("gc") == 1
    assert broker.positions_count("es") == 0, \
        "the gold position was counted against ES"

    broker.open_market(True, 1.0, 4990.0, "es long", magic=2, instrument="es")
    assert broker.positions_count("gc") == 1
    assert broker.positions_count("es") == 1


def test_floating_pnl_sums_every_instrument():
    """Equity is the whole portfolio, so a drawdown on one instrument shows up
    even while the other is in profit."""
    broker = SimBroker(spec=SymbolSpec(value_per_price_unit=1.0),
                       initial_balance=100_000.0, logger=QUIET)
    broker.add_instrument("gc", GC.spec(SymbolSpec()))
    broker.add_instrument("es", ES.spec(SymbolSpec()))
    broker.set_market(2400.0, DAY, "gc")
    broker.set_market(5000.0, DAY, "es")
    broker.open_market(True, 1.0, 2390.0, "gc", magic=1, instrument="gc")
    broker.open_market(True, 1.0, 4990.0, "es", magic=2, instrument="es")

    broker.set_market(2410.0, DAY, "gc")       # +10 pts x 100 = +1000
    broker.set_market(4995.0, DAY, "es")       # -5  pts x  50 = -250
    assert round(broker.floating_pnl(), 6) == 750.0


# ==========================================================================
# 2. the untagged bar — every run that predates instruments
# ==========================================================================
def test_an_untagged_bar_is_adopted_by_the_only_instrument():
    """A config with ONE declared instrument must still run on bar files that
    carry no tag — which is every backtest file written before this feature."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")))
    bars = breakout_day("", 2400.0)              # deliberately untagged
    assert all(b.instrument == "" for b in bars)

    res = run_backtest(cfg, bars, QUIET)
    assert len(res.trades) == 1
    assert res.trades[0].instrument == "gc"


def test_no_instruments_block_behaves_exactly_as_before():
    """The no-instruments path is untouched: untagged bars, untagged trades."""
    cfg = AppConfig()
    cfg.symbol = SymbolSpec(name="GC", digits=2, point=0.01, tick_size=0.01,
                            volume_min=0.01, volume_step=0.01,
                            value_per_price_unit=100.0)
    cfg.backtest.initial_balance = 100_000.0
    cfg.backtest.spread_points = 0.0
    cfg.backtest.slippage_points = 0.0
    cfg.backtest.commission_per_lot_per_side = 0.0
    s = session("solo", "")
    cfg.sessions = {s.name: s}

    res = run_backtest(cfg, breakout_day("", 2400.0), QUIET)
    assert len(res.trades) == 1
    assert (res.trades[0].instrument or "") == ""


# ==========================================================================
# 3. windows — shared clock, separate books
# ==========================================================================
def test_two_instruments_may_share_a_clock_window():
    """New York is New York for gold and for ES. Overlap across instruments is
    legal because each holds its own position."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")),
                    (copy.deepcopy(ES), session("es_ny", "es")))
    cfg.validate_sessions()             # must not raise


def test_two_sessions_on_the_same_instrument_may_not_overlap():
    """The original rule survives: one position slot per instrument, so two
    overlapping windows on it would silently fight."""
    a = session("gc_a", "gc")
    b = session("gc_b", "gc")
    cfg = portfolio((copy.deepcopy(GC), a))
    cfg.sessions[b.name] = b
    with pytest.raises(ValueError, match="overlap"):
        cfg.validate_sessions()


def test_a_session_naming_an_undeclared_instrument_is_rejected():
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")))
    cfg.sessions["gc_ny"].instrument = "platinum"
    with pytest.raises(ValueError):
        cfg.validate_sessions()


def test_a_lone_instrument_need_not_be_repeated_on_every_session():
    """With exactly one instrument declared there is nothing to choose, so
    configs written before this feature keep loading."""
    s = session("ny", "")
    cfg = portfolio((copy.deepcopy(GC), s))
    cfg.validate_sessions()
    assert cfg.sessions["ny"].instrument == "gc"


# ==========================================================================
# 4. selection — `--instruments gc,es`
# ==========================================================================
def _three():
    return portfolio((copy.deepcopy(GC), session("gc_ny", "gc")),
                     (copy.deepcopy(ES), session("es_ny", "es")),
                     (copy.deepcopy(NQ), session("nq_ny", "nq")))


def test_select_instruments_keeps_only_what_was_asked_for():
    cfg = _three()
    cfg.select_instruments("gc,es")
    assert sorted(cfg.instruments) == ["es", "gc"]
    assert sorted(s.name for s in cfg.enabled_sessions()) == ["es_ny", "gc_ny"]
    assert cfg.sessions["nq_ny"].enabled is False


def test_select_instruments_accepts_a_list_as_well_as_a_string():
    cfg = _three()
    cfg.select_instruments(["nq"])
    assert list(cfg.instruments) == ["nq"]


def test_selecting_nothing_changes_nothing():
    cfg = _three()
    cfg.select_instruments(None)
    cfg.select_instruments("")
    assert sorted(cfg.instruments) == ["es", "gc", "nq"]


def test_a_typo_is_an_error_not_a_silent_empty_run():
    cfg = _three()
    with pytest.raises(ValueError, match="Unknown instrument"):
        cfg.select_instruments("gc,eos")


def test_selecting_an_instrument_nothing_trades_is_an_error():
    """Declared but with every session on it disabled — the run would do
    nothing at all, so it says so."""
    cfg = _three()
    cfg.sessions["nq_ny"].enabled = False
    with pytest.raises(ValueError, match="No sessions left"):
        cfg.select_instruments("nq")


def test_the_output_folder_names_what_was_traded():
    """Two runs of the same engine and settings on different instruments must
    not land in the same folder and overwrite each other."""
    cfg = _three()
    assert instruments_of(cfg) == "es-gc-nq"
    cfg.select_instruments("gc")
    assert instruments_of(cfg) == "gc"


# ==========================================================================
# 5. contract specs
# ==========================================================================
def test_an_instrument_overlays_only_what_it_states():
    """Four lines is all an instrument needs; everything unset stays at the
    shared default, which live trading then replaces with the terminal's own."""
    base = SymbolSpec(name="GC", digits=2, point=0.01, tick_size=0.25,
                      volume_min=0.01, volume_max=50.0, volume_step=0.01,
                      value_per_price_unit=100.0, currency="USD")
    spec = instrument("es", "US500", 50.0).spec(base)

    assert spec.name == "US500"                   # routes to this MT5 symbol
    assert spec.value_per_price_unit == 50.0      # its own money per point
    assert spec.tick_size == 0.25                 # untouched default
    assert spec.volume_max == 50.0                # untouched default
    assert base.name == "GC", "the shared spec was mutated"


def test_an_instrument_may_override_the_contract_details_too():
    spec = instrument("nq", "USTEC", 20.0, digits=1, tick_size=0.1,
                      volume_min=0.1, volume_step=0.1,
                      currency="EUR").spec(SymbolSpec())
    assert (spec.digits, spec.tick_size, spec.volume_min,
            spec.volume_step, spec.currency) == (1, 0.1, 0.1, 0.1, "EUR")


def test_the_broker_view_scopes_every_call_to_one_instrument():
    """Strategy code is written as if it owned the broker. `InstrumentView` is
    what makes that true without the strategy knowing instruments exist."""
    broker = SimBroker(spec=SymbolSpec(value_per_price_unit=1.0),
                       initial_balance=100_000.0, logger=QUIET)
    broker.add_instrument("gc", GC.spec(SymbolSpec()))
    broker.add_instrument("es", ES.spec(SymbolSpec()))
    broker.set_market(2400.0, DAY, "gc")
    broker.set_market(5000.0, DAY, "es")

    gc_view = InstrumentView(broker, "gc")
    es_view = InstrumentView(broker, "es")
    assert gc_view.ask() == 2400.0
    assert es_view.ask() == 5000.0

    gc_view.open_market(True, 1.0, 2390.0, "gc", magic=1)
    assert gc_view.positions_count() == 1
    assert es_view.positions_count() == 0
    # anything not instrument-scoped falls through to the shared account
    assert gc_view.balance == es_view.balance


# ==========================================================================
# 6. reporting
# ==========================================================================
def test_the_trades_csv_says_which_instrument_earned_what():
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")),
                    (copy.deepcopy(ES), session("es_ny", "es")))
    res = run_backtest(cfg, merge(breakout_day("gc", 2400.0),
                                  breakout_day("es", 5000.0)), QUIET)

    df = trades_dataframe(res.trades)
    assert "instrument" in df.columns
    assert sorted(df["instrument"].unique()) == ["es", "gc"]

    split = instrument_summary(df)
    assert sorted(split.index) == ["es", "gc"]
    # the split must add up to the headline, or it is not a split
    assert round(split["net_profit"].sum(), 6) == round(df["net_profit"].sum(), 6)


def test_a_single_instrument_report_has_nothing_to_split():
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")))
    res = run_backtest(cfg, breakout_day("gc", 2400.0), QUIET)
    assert instrument_summary(trades_dataframe(res.trades)).empty


# ==========================================================================
# 7. sweeps
# ==========================================================================
def test_a_sweep_runs_the_whole_grid_once_per_instrument():
    rc = RunConfig.load("orb")
    rc.sweep.update({"sessions": ["ASIA"], "timeframes": ["M5"],
                     "orb_minutes": [15], "risk_reward": [2.0],
                     "news": ["skip"], "sl_modes": ["mid_range"]})
    rc.app.instruments = {"gc": copy.deepcopy(GC), "es": copy.deepcopy(ES)}

    one = rc.sweep_size({"instruments": ["gc"]})
    two = rc.sweep_size({"instruments": ["gc", "es"]})
    assert two == one * 2

    items = rc.sweep_items({"instruments": ["gc", "es"]})
    assert len(items) == two
    assert sorted(i.axes["instrument"] for i in items) == ["es", "gc"]
    # names must differ, or the two runs overwrite each other's CSV
    assert len({i.run_name for i in items}) == two
    for item in items:
        name = item.axes["instrument"]
        assert item.run_name.startswith(f"{name}_")
        assert list(item.cfg.instruments) == [name]
        assert item.cfg.strategy.instrument == name


def test_a_single_instrument_sweep_keeps_the_run_names_it_always_had():
    """The prefix only earns its place when there is something to tell apart,
    so existing sweep folders and their filenames do not move."""
    rc = RunConfig.load("orb")
    rc.sweep.update({"sessions": ["ASIA"], "timeframes": ["M5"],
                     "orb_minutes": [15], "risk_reward": [2.0],
                     "news": ["skip"], "sl_modes": ["mid_range"]})
    rc.app.instruments = {"gc": copy.deepcopy(GC)}
    item = rc.sweep_items({"instruments": ["gc"]})[0]
    assert item.run_name == "M5_ASIA_ORB15_SKIP_NEWS_RR2"
    assert item.cfg.strategy.instrument == "gc"


def test_a_sweep_on_an_undeclared_instrument_says_so():
    rc = RunConfig.load("orb")
    with pytest.raises(ValueError, match="Unknown instrument"):
        rc.sweep_items({"instruments": ["platinum"]})


def test_a_sweep_defaults_to_what_the_enabled_sessions_trade():
    """Not to everything DECLARED. A config may declare markets whose data has
    not been downloaded yet — a 3x3 session matrix with most cells off is the
    normal case — and sweeping those would triple the run and then fail on a
    missing file."""
    rc = RunConfig.load("orb")
    rc.app.instruments = {"gc": copy.deepcopy(GC), "es": copy.deepcopy(ES)}
    rc.sweep.pop("instruments", None)
    live = sorted({(s.instrument or "") for s in rc.app.enabled_sessions()} - {""})
    assert rc.sweep_instruments() == live
    assert "es" not in rc.sweep_instruments(), \
        "an instrument nothing trades was swept anyway"
    # naming it explicitly still sweeps it
    assert sorted(rc.sweep_instruments({"instruments": ["gc", "es"]})) == ["es", "gc"]


def test_a_sweep_falls_back_to_declared_when_no_session_names_one():
    """The single-instrument case: nothing has to say `instrument:` at all."""
    rc = RunConfig.load("orb")
    rc.app.instruments = {"gc": copy.deepcopy(GC)}
    rc.sweep.pop("instruments", None)
    for s in rc.app.sessions.values():
        s.instrument = ""
    assert rc.sweep_instruments() == ["gc"]


# ==========================================================================
# 8. the live path
# ==========================================================================
class Clock:
    """A wall clock that follows the feed, as a live one does."""

    def __init__(self, start):
        self.now = start

    def __call__(self, delivered=None):
        if delivered is not None:
            self.now = max(self.now, delivered.time + timedelta(seconds=40))
        else:
            self.now += timedelta(seconds=5)
        return self.now


class Feed:
    """One instrument's queue. `poll(timeout=0)` must not block — that is the
    contract `LiveTrader._next_bar` relies on to stay fair."""

    def __init__(self, bars):
        self.bars = list(bars)
        self.polls = 0
        self.timeouts = []

    def start(self):
        pass

    def stop(self):
        pass

    def poll(self, timeout=1.0):
        self.polls += 1
        self.timeouts.append(timeout)
        return self.bars.pop(0) if self.bars else None


def _live(cfg, feeds, clock, poll_seconds=0):
    from orb.live_trader import LiveTrader
    trader = LiveTrader(cfg, broker=SimBroker(cfg.symbol, 100_000.0,
                                              logger=QUIET),
                        feed=object(), logger=QUIET)
    trader.feeds = feeds                       # one per instrument
    trader.feed = next(iter(feeds.values()))
    trader._feed_turn = 0
    total = sum(len(f.bars) for f in feeds.values())
    trader.run(poll_seconds=poll_seconds, max_polls=total * 4, now_fn=clock)
    return trader


def test_live_trades_two_instruments_from_two_feeds():
    """The live loop, end to end: two feeds, two instruments, two positions,
    each valued with its own contract spec."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")),
                    (copy.deepcopy(ES), session("es_ny", "es")))
    feeds = {"gc": Feed(breakout_day("gc", 2400.0)),
             "es": Feed(breakout_day("es", 5000.0))}
    trader = _live(cfg, feeds, Clock(DAY + timedelta(minutes=595)))

    traded = {t.instrument for t in trader.broker.trades}
    assert traded == {"gc", "es"}, f"only {traded} traded"

    money = {t.instrument: t.net_profit for t in trader.broker.trades}
    assert round(money["gc"], 6) == round(money["es"] * 2.0, 6)


def test_every_feed_is_polled_even_when_another_is_busy():
    """The fairness rule. A feed with a backlog must not starve a quiet one —
    the naive `for f in feeds: poll(blocking); break` did exactly that."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")),
                    (copy.deepcopy(ES), session("es_ny", "es")))
    feeds = {"gc": Feed(breakout_day("gc", 2400.0)),
             "es": Feed(breakout_day("es", 5000.0))}
    _live(cfg, feeds, Clock(DAY + timedelta(minutes=595)))

    assert feeds["gc"].polls > 0 and feeds["es"].polls > 0
    # each was swept without blocking, so neither waits on the other
    assert 0 in feeds["gc"].timeouts and 0 in feeds["es"].timeouts
    assert not feeds["gc"].bars and not feeds["es"].bars, \
        "a feed still had a backlog when the loop ended"


def test_a_single_feed_is_polled_exactly_as_it_always_was():
    """One blocking poll per pass — the single-instrument path is untouched."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")))
    feed = Feed(breakout_day("gc", 2400.0))
    trader = _live(cfg, {"gc": feed}, Clock(DAY + timedelta(minutes=595)),
                   poll_seconds=0.5)
    # every poll blocked for the full timeout: no extra zero-timeout probe was
    # added, so one loop pass is still exactly one poll
    assert set(feed.timeouts) == {0.5}
    assert len(trader.broker.trades) == 1


def test_the_live_broker_learns_every_instruments_symbol():
    """`add_instrument` is what maps gc -> XAUUSDm; without it an ES order goes
    to gold's symbol."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")),
                    (copy.deepcopy(ES), session("es_ny", "es")))
    feeds = {"gc": Feed([]), "es": Feed([])}
    trader = _live(cfg, feeds, Clock(DAY))
    assert trader.broker.spec_for("gc").value_per_price_unit == 100.0
    assert trader.broker.spec_for("es").value_per_price_unit == 50.0
    assert [(e.cfg.name, e.strategy.broker.instrument)
            for e in trader.engine.engines] == [("gc_ny", "gc"),
                                                ("es_ny", "es")]


def test_the_warm_up_replay_only_sees_its_own_instrument():
    """The warm-up holds every instrument's history merged. Replaying it whole
    would price a gold session on ES bars and count trades never its own."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")),
                    (copy.deepcopy(ES), session("es_ny", "es")))
    feeds = {"gc": Feed([]), "es": Feed([])}
    trader = _live(cfg, feeds, Clock(DAY))
    trader._warmup_bars = merge(breakout_day("gc", 2400.0),
                                breakout_day("es", 5000.0))

    start = DAY + timedelta(minutes=600)
    end = DAY + timedelta(minutes=602)
    for name in ("gc", "es"):
        cfg_one = cfg.sessions[f"{name}_ny"]
        assert trader._replay_session(start, end, cfg_one) == 1, \
            f"{name} did not replay exactly its own single trade"


# ==========================================================================
# 9. signal scale vs execution scale
# ==========================================================================
class Recorder(RbeaLogger):
    """Captures journal lines so a warning can be asserted on."""

    def __init__(self):
        super().__init__(level=0)
        self.lines = []

    def info(self, msg, *a, **kw):
        self.lines.append(("INFO", str(msg)))

    def warn(self, msg, *a, **kw):
        self.lines.append(("WARN", str(msg)))

    def text(self):
        return "\n".join(m for _lvl, m in self.lines)


def _scale_check(signal_price, broker_price):
    """Run the start-up scale check with a feed and a broker that disagree by
    a chosen amount, and return the journal."""
    from orb.live_trader import LiveTrader
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")))
    log = Recorder()
    broker = SimBroker(cfg.symbol, 100_000.0, logger=QUIET)
    trader = LiveTrader(cfg, broker=broker, feed=object(), logger=log)
    trader.feeds = {"gc": Feed([])}
    trader.broker.add_instrument("gc", GC.spec(cfg.symbol))
    trader.broker.set_market(broker_price, DAY, "gc")
    trader._warmup_bars = [bar(600, signal_price, "gc")]
    log.lines.clear()             # drop the construction chatter
    trader.check_price_scale()
    return log


def test_a_scale_mismatch_is_called_out_loudly():
    """A CFD quoted 10x off the future is the failure this exists for: every
    stop and target would be wrong by that factor, silently."""
    log = _scale_check(2400.0, 240.0)
    assert "PRICE SCALE MISMATCH" in log.text()
    assert any(lvl == "WARN" for lvl, _m in log.lines)


def test_an_ordinary_basis_passes_quietly():
    """GC futures and XAUUSD spot really do sit ~$56 apart on ~2400. That is
    normal and must not cry wolf."""
    log = _scale_check(2400.0, 2456.0)
    assert "MISMATCH" not in log.text()
    assert "Wide basis" not in log.text()
    assert "Scale check OK" in log.text()


def test_a_wide_but_not_absurd_gap_is_flagged_gently():
    log = _scale_check(2400.0, 2760.0)          # 15% apart
    assert "Wide basis" in log.text()
    assert "MISMATCH" not in log.text()


def test_the_check_is_silent_when_there_is_nothing_to_compare():
    """A closed market quotes nothing. That must not produce a scare."""
    log = _scale_check(2400.0, 0.0)
    assert log.lines == []


# ==========================================================================
# 10. lot rules are the broker's; price rounding is the signal's
# ==========================================================================
def test_an_instrument_may_carry_the_brokers_lot_rules():
    """`volume_min` and friends belong to the ORDER, so the broker's rules win
    — and the backtest must honour them or it sizes trades MT5 would reject."""
    inst = instrument("es", "US500m", 1.0, volume_min=0.14, volume_step=0.01,
                      volume_max=1000.0)
    broker = SimBroker(spec=SymbolSpec(volume_min=0.01, volume_step=0.01,
                                       volume_max=100.0),
                       initial_balance=100_000.0, logger=QUIET)
    broker.add_instrument("es", inst.spec(SymbolSpec(volume_min=0.01,
                                                     volume_step=0.01,
                                                     volume_max=100.0)))
    # below the broker's minimum: it must be lifted TO the minimum, not sent
    assert broker.normalize_lot(0.10, "es") == 0.14
    # and the untagged account default is untouched by it
    assert broker.normalize_lot(0.10) == 0.10


def test_the_shipped_config_does_not_pin_price_rounding():
    """The regression. `digits` describes how PRICES round, and the backtest
    prices CME FUTURES, not the broker's CFD. XAUUSDm quotes 3 decimals where
    GC futures quote 2 — pinning the 3 moved every stop by a tenth of a cent
    and silently changed 3 of the 24 golden-master cases.

    Live reads digits/point/tick_size from the terminal at start-up regardless,
    so setting them per instrument buys nothing and costs reproducibility.
    """
    cfg = AppConfig.load(CONFIG)
    for name, inst in cfg.instruments.items():
        for attr in ("digits", "point", "tick_size"):
            assert getattr(inst, attr) is None, (
                f"instrument '{name}' pins {attr}. That rounds prices, and the "
                f"backtest prices futures rather than the broker's CFD — this "
                f"is what moved the golden master.")


def test_price_rounding_really_does_follow_digits():
    """Proof the field above is not merely cosmetic: it changes stop prices."""
    two = InstrumentConfig(name="a", mt5="A", value_per_point=1.0, digits=2)
    three = InstrumentConfig(name="b", mt5="B", value_per_point=1.0, digits=3)
    broker = SimBroker(spec=SymbolSpec(digits=2), initial_balance=100_000.0,
                       logger=QUIET)
    broker.add_instrument("a", two.spec(SymbolSpec(digits=2)))
    broker.add_instrument("b", three.spec(SymbolSpec(digits=2)))
    assert broker.normalize_price(4402.1749, "a") == 4402.17
    assert broker.normalize_price(4402.1749, "b") == 4402.175


# ==========================================================================
# 11. the shipped config still loads
# ==========================================================================
def test_the_shipped_config_declares_its_instrument_and_loads():
    cfg = AppConfig.load(CONFIG)
    assert cfg.instruments, "the shipped config lost its instruments block"
    for name, inst in cfg.instruments.items():
        assert inst.mt5, f"instrument '{name}' has no MT5 symbol to trade on"
        assert inst.value_per_point > 0, \
            f"instrument '{name}' would value every trade at zero"
    cfg.validate_sessions()



# ==========================================================================
# 12. the (session x instrument) matrix
# ==========================================================================
WINDOW = {"range_start": "09:30", "range_end": "10:00", "stop_time": "16:55"}


def _matrix(sessions, instruments=("gc", "es", "nq")):
    """A config built from the shipped one with these sessions substituted."""
    import yaml
    raw = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    raw["instruments"] = {k: v for k, v in raw["instruments"].items()
                          if k in instruments}
    raw["sessions"] = sessions
    return AppConfig.from_dict(raw)


def test_one_session_runs_several_symbols_each_with_its_own_settings():
    """The requirement. One window, three symbols, three different settings,
    one of them switched off — and the other two unaffected."""
    cfg = _matrix({"new_york": dict(WINDOW, enabled=True, risk_reward=4.0,
                                    instruments={
                                        "gc": {"enabled": True, "lots": 1.0},
                                        "es": {"enabled": True, "lots": 39.48,
                                               "risk_reward": 2.0},
                                        "nq": {"enabled": False, "lots": 10.91}})})
    got = {s.name: (s.instrument, s.lots, s.risk_reward, s.enabled)
           for s in cfg.sessions.values()}
    assert got == {
        "new_york_gc": ("gc", 1.0, 4.0, True),      # inherits the row's RR
        "new_york_es": ("es", 39.48, 2.0, True),    # overrides it
        "new_york_nq": ("nq", 10.91, 4.0, False),   # off, neighbours unharmed
    }


def test_one_symbol_runs_across_several_sessions_with_its_own_settings():
    """The mirror requirement: gold in three windows, different each time."""
    cfg = _matrix({
        "asia": {"range_start": "19:00", "range_end": "19:30",
                 "stop_time": "02:55", "enabled": True,
                 "instruments": {"gc": {"max_trades_per_session": 3}}},
        "london": {"range_start": "03:00", "range_end": "03:30",
                   "stop_time": "09:25", "enabled": True,
                   "instruments": {"gc": {"signal_timeframe": "M15",
                                          "risk_reward": 1.5}}},
        "new_york": dict(WINDOW, enabled=True,
                         instruments={"gc": {"risk_reward": 4.0}}),
    })
    got = {s.name: (s.signal_timeframe, s.risk_reward, s.max_trades_per_session)
           for s in cfg.sessions.values()}
    assert got["asia_gc"][2] == 3
    assert got["london_gc"][:2] == ("M15", 1.5)
    assert got["new_york_gc"][1] == 4.0
    assert all(s.instrument == "gc" for s in cfg.sessions.values())


def test_a_cell_inherits_defaults_then_the_row_then_itself():
    """Three levels, in that order — the row may override the defaults and the
    cell may override the row."""
    cfg = _matrix({"new_york": dict(WINDOW, enabled=True, risk_reward=9.0,
                                    signal_timeframe="M15",
                                    instruments={
                                        "gc": {},                       # row wins
                                        "es": {"risk_reward": 1.0}})})  # cell wins
    gc, es = cfg.sessions["new_york_gc"], cfg.sessions["new_york_es"]
    assert (gc.risk_reward, gc.signal_timeframe) == (9.0, "M15")
    assert (es.risk_reward, es.signal_timeframe) == (1.0, "M15")


def test_engine_options_merge_option_by_option_through_all_three_levels():
    """A cell stating ONE option must not silently discard the row's others —
    the same rule the row already follows against the defaults."""
    cfg = _matrix({"london": {"range_start": "03:00", "range_end": "03:15",
                              "stop_time": "09:25", "enabled": True,
                              "engine": "orb_reverse",
                              "engine_options": {"sl_range_mult": 0.75,
                                                 "direction": "reverse",
                                                 "sl_anchor": "range"},
                              "instruments": {
                                  "gc": {},
                                  "es": {"engine_options": {"sl_range_mult": 1.5}}}}})
    assert cfg.sessions["london_gc"].engine_options == {
        "sl_range_mult": 0.75, "direction": "reverse", "sl_anchor": "range"}
    assert cfg.sessions["london_es"].engine_options == {
        "sl_range_mult": 1.5, "direction": "reverse", "sl_anchor": "range"}


def test_switching_the_row_off_silences_every_symbol_under_it():
    cfg = _matrix({"new_york": dict(WINDOW, enabled=False,
                                    instruments={"gc": {"enabled": True},
                                                 "es": {"enabled": True}}),
                   "asia": {"range_start": "19:00", "range_end": "19:30",
                            "stop_time": "02:55", "enabled": True,
                            "instruments": {"gc": {}}}})
    assert not cfg.sessions["new_york_gc"].enabled
    assert not cfg.sessions["new_york_es"].enabled
    assert cfg.sessions["asia_gc"].enabled


def test_each_cell_gets_its_own_magic():
    cfg = _matrix({"new_york": dict(WINDOW, enabled=True,
                                    instruments={"gc": {}, "es": {}, "nq": {}})})
    magics = [s.magic for s in cfg.sessions.values()]
    assert len(set(magics)) == 3, f"cells share a magic: {magics}"


def test_a_cell_may_pin_its_own_magic():
    """Anything already trading live pins its magic so inserting a cell above
    it cannot renumber it out from under open positions."""
    cfg = _matrix({"new_york": dict(WINDOW, enabled=True,
                                    instruments={"gc": {"magic": 4242}, "es": {}})})
    assert cfg.sessions["new_york_gc"].magic == 4242


def test_two_cells_sharing_a_magic_are_rejected():
    with pytest.raises(ValueError, match="magic"):
        _matrix({"new_york": dict(WINDOW, enabled=True,
                                  instruments={"gc": {"magic": 7}, "es": {"magic": 7}})})


def test_cells_on_different_instruments_may_share_a_window():
    _matrix({"new_york": dict(WINDOW, enabled=True,
                              instruments={"gc": {}, "es": {}, "nq": {}})})


def test_the_same_instrument_may_not_overlap_across_windows():
    with pytest.raises(ValueError, match="overlap"):
        _matrix({"new_york": dict(WINDOW, enabled=True,
                                  instruments={"gc": {}}),
                 "late_ny": {"range_start": "09:45", "range_end": "10:15",
                             "stop_time": "16:55", "enabled": True,
                             "instruments": {"gc": {}}}})


def test_a_cell_naming_an_undeclared_instrument_is_rejected():
    with pytest.raises(ValueError, match="not defined|Unknown instrument"):
        _matrix({"new_york": dict(WINDOW, enabled=True,
                                  instruments={"platinum": {}})})


def test_a_typo_in_a_cell_is_rejected_with_the_cell_named():
    with pytest.raises(ValueError, match="risk_rewrd"):
        _matrix({"new_york": dict(WINDOW, enabled=True,
                                  instruments={"gc": {"risk_rewrd": 3}})})


def test_a_cell_may_be_given_its_own_name():
    cfg = _matrix({"new_york": dict(WINDOW, enabled=True,
                                    instruments={"gc": {"name": "gold_ny"}})})
    assert "gold_ny" in cfg.sessions
    assert cfg.sessions["gold_ny"].instrument == "gc"


def test_the_flat_form_still_works_unchanged():
    """Every config written before the matrix keeps loading, with the same
    session name it always had."""
    cfg = _matrix({"new_york": dict(WINDOW, enabled=True, instrument="gc",
                                    lots=1.0)}, instruments=("gc",))
    assert list(cfg.sessions) == ["new_york"]
    assert cfg.sessions["new_york"].instrument == "gc"


def test_the_two_forms_produce_identical_sessions():
    """The matrix is sugar, not new semantics: one cell must equal the flat
    session it stands for, field for field."""
    from dataclasses import asdict
    flat = _matrix({"ny": dict(WINDOW, enabled=True, instrument="gc",
                               lots=2.0, risk_reward=3.0, magic=99)},
                   instruments=("gc",))
    matrix = _matrix({"ny": dict(WINDOW, enabled=True, risk_reward=3.0,
                                 instruments={"gc": {"lots": 2.0, "magic": 99,
                                                     "name": "ny"}})},
                     instruments=("gc",))
    a, b = asdict(flat.sessions["ny"]), asdict(matrix.sessions["ny"])
    assert a == b, {k: (a[k], b[k]) for k in a if a[k] != b[k]}


def test_a_run_with_several_instruments_refuses_untagged_bars():
    """Silence is the worst outcome. Untagged bars used to be handed to every
    engine, and the broker then had no price under `es` — so the run quietly
    took zero trades instead of saying the bars were loaded wrong."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")),
                    (copy.deepcopy(ES), session("es_ny", "es")))
    with pytest.raises(ValueError, match="carries no instrument"):
        run_backtest(cfg, breakout_day("", 2400.0), QUIET)


def test_a_tagged_bar_is_never_relabelled():
    """THE BUG THIS EXISTS FOR. An earlier version re-tagged any bar to the
    run's single instrument, so a sweep that loaded only ES bars happily traded
    them as NQ: ES and NQ came back byte-identical — same 311 trades, same
    45.34% win rate, same $343.10 — and nothing flagged it.

    A bar that says it is ES is ES. If it matches no session, the run raises
    rather than reporting zero trades, because a flat month and a wiring
    mistake must not look the same."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")))
    with pytest.raises(ValueError, match="Not one bar reached a session"):
        run_backtest(cfg, breakout_day("es", 2400.0), QUIET)


def test_bars_this_run_does_not_trade_are_skipped_not_traded():
    """A merged multi-instrument stream legitimately carries bars a given run
    does not trade. Those are ignored; the ones it does trade still work."""
    cfg = portfolio((copy.deepcopy(GC), session("gc_ny", "gc")))
    cfg.instruments["es"] = copy.deepcopy(ES)
    mixed = merge(breakout_day("gc", 2400.0), breakout_day("es", 5000.0))
    res = run_backtest(cfg, mixed, QUIET)
    assert len(res.trades) == 1
    assert res.trades[0].instrument == "gc"


def test_a_sweep_loads_every_instrument_in_its_grid():
    """The other half of the same bug: the worker inferred which instruments to
    load from the FIRST grid item's session, so a two-instrument sweep loaded
    only the first one's bars."""
    from orb.backtest import load_instrument_bars
    cfg = _matrix({"new_york": dict(WINDOW, enabled=True,
                                    instruments={"gc": {"enabled": True},
                                                 "es": {"enabled": False}})})
    cfg.instruments["es"].data = cfg.instruments["gc"].data   # stand-in file
    only_enabled = load_instrument_bars(cfg, make_clock(cfg), start="2026-06-01",
                                        end="2026-06-03", logger=QUIET)
    assert {b.instrument for b in only_enabled} == {"gc"}

    both = load_instrument_bars(cfg, make_clock(cfg), start="2026-06-01",
                                end="2026-06-03", logger=QUIET,
                                instruments=["gc", "es"])
    assert {b.instrument for b in both} == {"gc", "es"}


def test_loading_bars_for_an_undeclared_instrument_is_an_error():
    from orb.backtest import load_instrument_bars
    cfg = _matrix({"new_york": dict(WINDOW, enabled=True,
                                    instruments={"gc": {}})})
    with pytest.raises(ValueError, match="undeclared instrument"):
        load_instrument_bars(cfg, make_clock(cfg), logger=QUIET,
                             instruments=["platinum"])


def test_only_instruments_an_enabled_session_trades_are_loaded():
    """Declaring an instrument must not demand a data file for it. A 3x3 matrix
    with most cells off is the normal case."""
    from orb.backtest import load_instrument_bars
    cfg = _matrix({"new_york": dict(WINDOW, enabled=True,
                                    instruments={"gc": {"enabled": True},
                                                 "es": {"enabled": False},
                                                 "nq": {"enabled": False}})})
    cfg.instruments["es"].data = ["data/does_not_exist.parquet"]
    cfg.instruments["nq"].data = ["data/also_missing.parquet"]
    bars = load_instrument_bars(cfg, make_clock(cfg),
                                start="2026-06-01", end="2026-06-05", logger=QUIET)
    assert bars, "no bars loaded"
    assert {b.instrument for b in bars} == {"gc"}

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
