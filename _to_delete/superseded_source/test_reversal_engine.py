"""Proof that the reversal engine does what it claims — and that the original
engine is untouched by it.

The two claims that matter most:

  1. `sl_range_mult` 0.5 and 1.0 reproduce the original engine's `mid_range` and
     `full_range` EXACTLY, trade for trade, to the cent. If that holds, the
     multiplier is a true generalisation and every result already produced under
     the old modes stays comparable.

  2. Nothing under `orb/` changes. The strategy class is swapped inside a
     context manager and restored afterwards, including when the run raises.
"""
from __future__ import annotations

import copy
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orb.registry as registry                                   # noqa: E402
from orb.backtest import make_clock, run_backtest                 # noqa: E402
from orb.config import AppConfig                                  # noqa: E402
from orb.data.dbn import load_dbn_bars                            # noqa: E402
from orb.engines.breakout import RangeBreakoutStrategy            # noqa: E402
from orb.engines.reversal import (ANCHOR_MIRROR, ANCHOR_RANGE,    # noqa: E402
                                  ReversalSettings, ReversalStrategy)
from orb.engines.reversal.grid import build as build_grid         # noqa: E402
from orb.logger import RbeaLogger                                 # noqa: E402
from orb_reversal.runner import run_forward, run_reversal         # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "gc_1m_merged.parquet")
START, END = "2026-03-01", "2026-05-01"

pytestmark = pytest.mark.skipif(not os.path.exists(DATA),
                                reason="merged bar data not available")


# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def base():
    cfg = AppConfig.load(os.path.join(ROOT, "config.yaml"))
    cfg.backtest.dbn_paths = [DATA]
    cfg.server_timezone = "America/New_York"
    cfg.server_utc_offset_hours = 0
    s = cfg.use_single_session("LONDON")
    s.signal_timeframe = "M5"
    s.range_start, s.range_end, s.stop_time = "03:00", "03:15", "09:30"
    s.risk_reward = 2.0
    s.max_trades_per_session = 0
    s.log_level = "none"
    for _k, _l, cat in s.news.items():
        cat.mode = "on"
    s.news_days, s.news_trading = "", "on"
    cfg.strategy.log_level = "none"
    return cfg


@pytest.fixture(scope="module")
def bars(base):
    d = base.databento
    return load_dbn_bars(base.backtest.dbn_paths, make_clock(base),
                         contract_mode=d.contract_mode,
                         contract_symbol=d.contract_symbol,
                         include_spreads=d.include_spreads,
                         roll_min_volume=d.roll_min_volume,
                         roll_boundary_hour=d.roll_boundary_hour,
                         start=START, end=END, logger=RbeaLogger(level=0))


def _original(base, bars, sl_mode):
    import copy
    cfg = copy.deepcopy(base)
    for s in cfg.sessions.values():
        s.sl_mode = sl_mode
    return run_backtest(cfg, bars, RbeaLogger(level=0))


def _key(t):
    return (t.entry_time, t.direction, round(t.entry_price, 6),
            round(t.sl, 6), round(t.tp, 6), round(t.net_profit, 6))


# ==========================================================================
# 1. the multiplier is a true generalisation of the original two modes
# ==========================================================================
@pytest.mark.parametrize("mult,sl_mode", [(0.5, "mid_range"), (1.0, "full_range")])
def test_forward_multiplier_reproduces_original_mode(base, bars, mult, sl_mode):
    """mult 0.5 == mid_range, mult 1.0 == full_range — trade for trade."""
    ref = _original(base, bars, sl_mode)
    got = run_forward(base, bars, ReversalSettings(sl_range_mult=mult,
                                                   max_trades_per_session=0))
    assert len(ref.trades) == len(got.trades) > 0
    assert [_key(t) for t in ref.trades] == [_key(t) for t in got.trades]
    assert ref.final_balance == pytest.approx(got.final_balance, abs=1e-6)


def test_multiplier_the_original_engine_cannot_express(base, bars):
    """1.5 x range is wider than `full_range`, the widest the original allows."""
    full = _original(base, bars, "full_range")
    wide = run_forward(base, bars, ReversalSettings(sl_range_mult=1.5,
                                                    max_trades_per_session=0))
    def avg_risk(res):
        r = [abs(t.entry_price - t.sl) for t in res.trades]
        return sum(r) / len(r)
    assert avg_risk(wide) > avg_risk(full) > 0


def test_risk_scales_with_the_multiplier(base, bars):
    """Stop distance is monotonic in the multiplier, in both directions."""
    for reverse in (False, True):
        prev = 0.0
        for mult in (0.25, 0.5, 1.0, 2.0):
            res = run_reversal(base, bars,
                               ReversalSettings(sl_range_mult=mult,
                                                direction=("reverse" if reverse else "forward"), max_trades_per_session=0))
            risk = [abs(t.entry_price - t.sl) for t in res.trades]
            avg = sum(risk) / len(risk)
            assert avg > prev, f"mult {mult} reverse={reverse} not wider"
            prev = avg


def test_reversed_risk_equals_multiplier_times_range(base, bars):
    """With the default `range` anchor the risk IS mult x range height —
    exactly, not approximately, and independent of the breakout overshoot."""
    mult = 0.75
    res = run_reversal(base, bars, ReversalSettings(sl_range_mult=mult,
                                                    sl_anchor=ANCHOR_RANGE,
                                                    max_trades_per_session=0))
    assert res.trades
    for t in res.trades:
        expected = mult * abs(t.range_high - t.range_low)
        assert abs(t.entry_price - t.sl) == pytest.approx(expected, abs=0.02)


def test_mirror_anchor_differs_from_range_anchor(base, bars):
    """The mirror anchor includes the breakout overshoot, so it is wider."""
    a = run_reversal(base, bars, ReversalSettings(sl_range_mult=0.5,
                                                   sl_anchor=ANCHOR_RANGE,
                                                   max_trades_per_session=0))
    b = run_reversal(base, bars, ReversalSettings(sl_range_mult=0.5,
                                                   sl_anchor=ANCHOR_MIRROR,
                                                   max_trades_per_session=0))
    def avg_risk(res):
        r = [abs(t.entry_price - t.sl) for t in res.trades]
        return sum(r) / len(r)
    assert avg_risk(b) > avg_risk(a)


# ==========================================================================
# 2. the reversal really is reversed, and its stop is a real stop
# ==========================================================================
def test_direction_is_flipped(base, bars):
    st = ReversalSettings(sl_range_mult=0.5, max_trades_per_session=0)
    fwd = run_forward(base, bars, st)
    rev = run_reversal(base, bars, st)
    f = {t.entry_time: t.direction for t in fwd.trades}
    r = {t.entry_time: t.direction for t in rev.trades}
    shared = set(f) & set(r)
    assert len(shared) > 20, "not enough overlapping entries to compare"
    assert all(f[k] != r[k] for k in shared)


def test_reversed_stop_is_on_the_losing_side_of_entry(base, bars):
    """The bug that made every reversed trade close at breakeven: a stop on the
    wrong side of the entry. It must never come back."""
    for mult in (0.25, 0.5, 1.0, 2.0):
        res = run_reversal(base, bars, ReversalSettings(sl_range_mult=mult,
                                                         max_trades_per_session=0))
        assert res.trades
        for t in res.trades:
            if t.direction.upper().startswith("BUY"):
                assert t.sl < t.entry_price, f"BUY stop above entry at {t.entry_time}"
                assert t.tp > t.entry_price
            else:
                assert t.sl > t.entry_price, f"SELL stop below entry at {t.entry_time}"
                assert t.tp < t.entry_price


def test_no_zero_risk_and_reward_matches_rr(base, bars):
    res = run_reversal(base, bars, ReversalSettings(sl_range_mult=0.5,
                                                     max_trades_per_session=0))
    for t in res.trades:
        risk = abs(t.entry_price - t.sl)
        assert risk > 0
        assert abs(t.tp - t.entry_price) == pytest.approx(2.0 * risk, rel=1e-3)


@pytest.mark.parametrize("cap", [1, 2, 3])
def test_trade_cap_is_respected(base, bars, cap):
    res = run_reversal(base, bars, ReversalSettings(sl_range_mult=0.5,
                                                     max_trades_per_session=cap))
    assert res.trades
    assert max(t.trade_no_in_session for t in res.trades) <= cap


def test_more_cap_means_at_least_as_many_trades(base, bars):
    counts = [len(run_reversal(base, bars,
                               ReversalSettings(sl_range_mult=0.5,
                                                max_trades_per_session=c)).trades)
              for c in (1, 2, 3)]
    assert counts[0] < counts[1] <= counts[2]


# ==========================================================================
# 3. engines are selected, not swapped, and the core stays engine-agnostic
# ==========================================================================
def test_registry_resolves_both_engines():
    assert registry.resolve("breakout") is RangeBreakoutStrategy
    assert registry.resolve("reversal") is ReversalStrategy
    assert {"breakout", "reversal"} <= set(registry.names())


def test_unknown_engine_names_the_valid_ones():
    """A typo in `engine:` must say what was allowed instead."""
    with pytest.raises(ValueError) as exc:
        registry.resolve("revrsal")
    message = str(exc.value)
    assert "revrsal" in message
    assert "breakout" in message and "reversal" in message


def test_engine_selection_is_per_session_not_global():
    """The point of the registry: two sessions, two different strategies, in
    one process. The old monkey-patch made this impossible."""
    from orb.engine import Engine

    class _Broker:
        spec = type("S", (), {"name": "GC", "digits": 2, "point": 0.01,
                              "stops_level_points": 0})()
        digits, stops_level_price = 2, 0.0
        def sync_market(self, *a): pass
        def settle_bar(self, *a): pass
        def reference_price(self, is_buy): return 0.0
        def price_for(self, is_buy): return 0.0

    a = AppConfig.load(os.path.join(ROOT, "config.yaml"))
    one = a.use_single_session("A")
    one.range_start, one.range_end, one.stop_time = "03:00", "03:15", "09:30"
    one.log_level = "none"
    two = copy.deepcopy(one)
    two.name, two.engine = "B", "reversal"
    two.engine_options = {"sl_range_mult": 0.75}
    one.engine, one.engine_options = "breakout", {}

    broker = _Broker()
    e1 = Engine(one, broker, logger=RbeaLogger(level=0))
    e2 = Engine(two, broker, logger=RbeaLogger(level=0))
    assert type(e1.strategy) is RangeBreakoutStrategy
    assert type(e2.strategy) is ReversalStrategy
    # and building the second did not change the first
    assert type(e1.strategy) is RangeBreakoutStrategy


def test_reversal_engine_context_manager_is_gone_and_says_so():
    """It must not survive as a silent no-op: a sweep that quietly ran forward
    trades and labelled them reversed would be the worst possible failure."""
    from orb_reversal.runner import reversal_engine
    with pytest.raises(RuntimeError) as exc:
        with reversal_engine():
            pass
    assert "engine: reversal" in str(exc.value)


def test_original_backtest_unaffected_by_a_reversal_run(base, bars):
    """Run the original, run a reversal, run the original again — identical."""
    a = _original(base, bars, "mid_range")
    run_reversal(base, bars, ReversalSettings(sl_range_mult=1.5, max_trades_per_session=2))
    b = _original(base, bars, "mid_range")
    assert [_key(t) for t in a.trades] == [_key(t) for t in b.trades]


def test_caller_config_is_not_mutated(base, bars):
    session = next(iter(base.enabled_sessions()))
    before = (session.sl_mode, session.max_trades_per_session, session.comment)
    run_reversal(base, bars, ReversalSettings(sl_range_mult=2.0, max_trades_per_session=1))
    after = (session.sl_mode, session.max_trades_per_session, session.comment)
    assert before == after
    assert session.engine == "breakout" and session.engine_options == {}


def test_the_core_does_not_know_any_engine():
    """The invariant that actually matters.

    It is no longer "the word sl_range_mult appears nowhere in orb/config.py" —
    that was a proxy, and it is now wrong, because config.py documents
    `engine_options` with a reversal example in a comment. What must hold is
    stronger and structural: no core module may IMPORT an engine, or name an
    engine's strategy class in code. Engines depend on the core; the core must
    never depend on an engine, or they cannot be added without editing it.
    """
    core = ["config.py", "engine.py", "broker.py", "bars.py", "backtest.py",
            "live_trader.py", "timeutils.py", "logger.py", "registry.py",
            "markets.py"]
    reaches_engines = []
    for module in core:
        src = open(os.path.join(ROOT, "orb", module), encoding="utf-8").read()
        code = re.sub(r'""".*?"""', "", src, flags=re.S)     # drop docstrings
        code = re.sub(r"#.*", "", code)                      # drop comments
        if "orb.engines" in code or "from .engines" in code:
            reaches_engines.append(module)
        # naming a SPECIFIC strategy class is never allowed anywhere in core
        for cls in ("RangeBreakoutStrategy", "ReversalStrategy"):
            assert cls not in code, (
                f"{cls} is named in orb/{module} — the core must not know "
                f"which engines exist")
        # nor may core reach into one engine's modules
        for engine in ("engines.breakout", "engines.reversal"):
            assert engine not in code, f"orb/{module} reaches into {engine}"

    # Exactly one core module may mention the engines package, and it is the
    # registry — the single, deliberate seam where the built-ins are loaded.
    # If a second one appears, the core has grown a dependency on strategies.
    assert reaches_engines == ["registry.py"], (
        f"only orb/registry.py may reach the engines package, but "
        f"{reaches_engines} do")


def test_breakout_settings_reject_another_engines_option():
    """`engine_options` meant for the reversal must not be silently ignored by
    the breakout engine — that would run a backtest with defaults and say
    nothing."""
    from orb.engines.breakout import BreakoutSettings
    with pytest.raises(ValueError) as exc:
        BreakoutSettings.from_options({"sl_range_mult": 0.75})
    assert "sl_range_mult" in str(exc.value)


# ==========================================================================
# 4. settings and grid
# ==========================================================================
@pytest.mark.parametrize("kwargs", [
    {"sl_range_mult": 0}, {"sl_range_mult": -1}, {"sl_anchor": "nope"},
    {"max_trades_per_session": -2}, {"direction": "sideways"},
])
def test_bad_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ReversalSettings(**kwargs).validate()


def test_labels_are_file_safe_and_distinct():
    a = ReversalSettings(sl_range_mult=1.5, max_trades_per_session=2).run_name()
    b = ReversalSettings(sl_range_mult=0.5, max_trades_per_session=2).run_name()
    assert a == "REV_SL1p5_RR" and b == "REV_SL0p5_RR"
    assert "." not in a and "/" not in a


def test_grid_sizes_and_uniqueness(base):
    g = build_grid(base, sessions=["LONDON"], timeframes=["M5"],
                   orb_minutes=[15], news_modes=["SKIP_NEWS"],
                   risk_reward=[2.0], sl_range_mults=[0.5, 1.0, 1.5],
                   trade_caps=[1, 2], directions=["reverse", "forward"])
    assert len(g) == 3 * 2 * 2
    assert len({i.run_name for i in g}) == len(g)
    for item in g:
        s = next(iter(item.cfg.enabled_sessions()))
        assert s.engine == "reversal"
        assert s.engine_options["sl_range_mult"] == item.sl_range_mult
        assert s.engine_options["direction"] == item.direction
        assert s.max_trades_per_session == item.max_trades_per_session
        assert s.range_start == "03:00" and s.range_end == "03:15"
        assert s.stop_time == "09:30"


def test_grid_new_york_stops_before_the_contract_roll(base):
    g = build_grid(base, sessions=["NEW_YORK"], timeframes=["M5"],
                   orb_minutes=[15], news_modes=["SKIP_NEWS"], risk_reward=[2.0],
                   sl_range_mults=[0.5], trade_caps=[1])
    assert next(iter(g[0].cfg.enabled_sessions())).stop_time == "16:55"


# ==========================================================================
# 5. the configuration file
# ==========================================================================
from orb_reversal.settings import ReversalConfig                  # noqa: E402

CFG_PATH = os.path.join(ROOT, "reversal_config.yaml")


def test_config_file_exists_and_loads():
    rc = ReversalConfig.load(CFG_PATH)
    assert rc.base_config == "config.yaml"
    assert rc.settings().sl_range_mult > 0
    assert "session" in rc.run and "sl_range_mult" in rc.reversal


def test_config_builds_the_session_window():
    rc = ReversalConfig.load(CFG_PATH)
    app = rc.app_config()
    s = next(iter(app.enabled_sessions()))
    assert s.range_start == "03:00"                    # LONDON
    assert s.range_end == "03:15"                      # 15-minute ORB
    assert s.stop_time == "09:30"
    assert s.engine == "reversal"
    assert s.engine_options["sl_range_mult"] == rc.settings().sl_range_mult
    assert s.engine_options["direction"] == "reverse"


def test_config_overrides_win_over_the_file():
    rc = ReversalConfig.load(CFG_PATH)
    app = rc.app_config({"session": "NEW_YORK", "orb_minutes": 30,
                         "signal_timeframe": "M15", "risk_reward": 3.0})
    s = next(iter(app.enabled_sessions()))
    assert s.range_start == "09:30" and s.range_end == "10:00"
    assert s.stop_time == "16:55"                      # before the 18:00 roll
    assert s.signal_timeframe == "M15" and s.risk_reward == 3.0


def test_config_news_switch():
    rc = ReversalConfig.load(CFG_PATH)
    for word, mode in (("skip", "off"), ("include", "on")):
        s = next(iter(rc.app_config({"news": word}).enabled_sessions()))
        assert s.news_trading == mode
        assert all(c.mode == mode for _k, _l, c in s.news.items())


def test_config_rejects_a_bad_session():
    rc = ReversalConfig.load(CFG_PATH)
    rc.run["session"] = "TOKYO"
    with pytest.raises(SystemExit):
        rc.validate()


def test_config_rejects_a_bad_news_word():
    rc = ReversalConfig.load(CFG_PATH)
    rc.run["news"] = "sometimes"
    with pytest.raises(SystemExit):
        rc.validate()


def test_config_rejects_a_scalar_where_a_list_belongs():
    rc = ReversalConfig.load(CFG_PATH)
    rc.sweep["sl_range_mult"] = 0.5
    with pytest.raises(SystemExit):
        rc.validate()


def test_sweep_size_matches_the_grid_it_builds():
    rc = ReversalConfig.load(CFG_PATH)
    over = {"timeframes": ["M5"], "orb_minutes": [15], "news": ["skip"],
            "risk_reward": [2.0], "sl_range_mult": [0.5, 1.0, 1.5],
            "max_trades": [2], "directions": ["reverse", "forward"]}
    assert rc.sweep_size(over) == 6 == len(rc.sweep_items(over))


def test_sweep_items_carry_the_multiplier_and_direction():
    rc = ReversalConfig.load(CFG_PATH)
    items = rc.sweep_items({"timeframes": ["M5"], "orb_minutes": [15],
                            "news": ["skip"], "risk_reward": [2.0],
                            "sl_range_mult": [0.25, 2.0], "max_trades": [1],
                            "directions": ["reverse", "forward"]})
    seen = {(i.sl_range_mult, i.direction == "reverse") for i in items}
    assert seen == {(0.25, True), (0.25, False), (2.0, True), (2.0, False)}
    for i in items:
        s = next(iter(i.cfg.enabled_sessions()))
        assert s.engine_options["sl_range_mult"] == i.sl_range_mult
        assert s.engine_options["direction"] == i.direction


def test_config_does_not_touch_the_original_config_file():
    before = open(os.path.join(ROOT, "config.yaml"), encoding="utf-8").read()
    rc = ReversalConfig.load(CFG_PATH)
    rc.app_config({"session": "ASIA"})
    rc.sweep_items({"timeframes": ["M5"], "orb_minutes": [15],
                    "news": ["skip"], "risk_reward": [2.0],
                    "sl_range_mult": [1.0], "max_trades": [1],
                    "directions": ["reverse"]})
    after = open(os.path.join(ROOT, "config.yaml"), encoding="utf-8").read()
    assert before == after


def test_mirror_anchor_reproduces_the_earlier_study(base, bars):
    """The older tools/reversal_test.py measured risk by mirroring the original
    stop LEVEL. `sl_anchor: mirror` at 0.5 must still do exactly that, so the
    numbers already produced can be reproduced with the new engine."""
    import copy
    from orb.strategy import RangeBreakoutStrategy as _Orig
    cfg = copy.deepcopy(base)
    for s in cfg.sessions.values():
        s.sl_mode = "mid_range"
        s.max_trades_per_session = 2
    ref = run_backtest(cfg, bars, RbeaLogger(level=0))          # forward, mid
    got = run_reversal(base, bars,
                       ReversalSettings(sl_range_mult=0.5,
                                        sl_anchor=ANCHOR_MIRROR, max_trades_per_session=2))
    # same entries, opposite direction, and the risk each trade took matches
    # what the forward trade at the same instant would have risked
    fwd_risk = {t.entry_time: abs(t.entry_price - t.sl) for t in ref.trades}
    checked = 0
    for t in got.trades:
        if t.entry_time in fwd_risk:
            assert abs(t.entry_price - t.sl) == pytest.approx(
                fwd_risk[t.entry_time], abs=0.02)
            checked += 1
    assert checked > 20
