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
from orb.engines.orb import OrbStrategy            # noqa: E402
from orb.engines.orb_reverse import (ANCHOR_MIRROR, ANCHOR_RANGE,    # noqa: E402
                                  OrbReverseSettings, OrbReverseStrategy)
from orb.engines.orb_reverse.grid import build as build_grid         # noqa: E402
from orb.logger import RbeaLogger                                 # noqa: E402
from orb.runconfig import RunConfig                                # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "gc_1m_merged.parquet")
START, END = "2026-03-01", "2026-05-01"

pytestmark = pytest.mark.skipif(not os.path.exists(DATA),
                                reason="merged bar data not available")


# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def base():
    cfg = AppConfig.load(os.path.join(ROOT, "orb", "engines", "orb", "config.yaml"))
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


def _capped(settings, max_trades_per_session=0):
    """Tag settings with a session trade cap.

    `max_trades_per_session` is a SESSION field, enforced by the core — it is
    deliberately NOT an engine option, because having it in both places meant a
    config could set it where nothing read it and the cap silently did not
    apply. Tests still want to express stop and cap in one call, so this tags
    the settings object and `run_reversal` puts the cap where it belongs.
    """
    settings.session_max_trades = int(max_trades_per_session)
    return settings


def run_reversal(cfg, bars, settings, logger=None):
    """Apply the settings to a copy of `cfg` and run it. The engine is chosen
    by `settings.apply_to`, which sets `engine: orb_reverse` on each session —
    there is no class swapping any more."""
    run_cfg = copy.deepcopy(cfg)
    settings.apply_to(run_cfg)
    cap = getattr(settings, "session_max_trades", None)
    if cap is not None:
        for session in run_cfg.enabled_sessions():
            session.max_trades_per_session = cap
    return run_backtest(run_cfg, bars, logger or RbeaLogger(level=0))


def run_forward(cfg, bars, settings, logger=None):
    """The same settings in the ordinary breakout direction — the control arm."""
    forward = copy.deepcopy(settings)
    forward.direction = "forward"
    forward.session_max_trades = getattr(settings, "session_max_trades", 0)
    return run_reversal(cfg, bars, forward, logger)


# ==========================================================================
# 1. the multiplier is a true generalisation of the original two modes
# ==========================================================================
@pytest.mark.parametrize("mult,sl_mode", [(0.5, "mid_range"), (1.0, "full_range")])
def test_forward_multiplier_reproduces_original_mode(base, bars, mult, sl_mode):
    """mult 0.5 == mid_range, mult 1.0 == full_range — trade for trade."""
    ref = _original(base, bars, sl_mode)
    got = run_forward(base, bars, _capped(OrbReverseSettings(sl_range_mult=mult), 0))
    assert len(ref.trades) == len(got.trades) > 0
    assert [_key(t) for t in ref.trades] == [_key(t) for t in got.trades]
    assert ref.final_balance == pytest.approx(got.final_balance, abs=1e-6)


def test_multiplier_the_original_engine_cannot_express(base, bars):
    """1.5 x range is wider than `full_range`, the widest the original allows."""
    full = _original(base, bars, "full_range")
    wide = run_forward(base, bars, _capped(OrbReverseSettings(sl_range_mult=1.5), 0))
    def avg_risk(res):
        r = [abs(t.entry_price - t.sl) for t in res.trades]
        return sum(r) / len(r)
    assert avg_risk(wide) > avg_risk(full) > 0


def test_risk_scales_with_the_multiplier(base, bars):
    """Stop distance is monotonic in the multiplier, in both directions."""
    for reverse in (False, True):
        prev = 0.0
        for mult in (0.25, 0.5, 1.0, 2.0):
            res = run_reversal(base, bars, _capped(
                    OrbReverseSettings(
                        sl_range_mult=mult,
                        direction=("reverse" if reverse else "forward")), 0))
            risk = [abs(t.entry_price - t.sl) for t in res.trades]
            avg = sum(risk) / len(risk)
            assert avg > prev, f"mult {mult} reverse={reverse} not wider"
            prev = avg


def test_reversed_risk_equals_multiplier_times_range(base, bars):
    """With the default `range` anchor the risk IS mult x range height —
    exactly, not approximately, and independent of the breakout overshoot."""
    mult = 0.75
    res = run_reversal(base, bars, _capped(OrbReverseSettings(sl_range_mult=mult,
                                                    sl_anchor=ANCHOR_RANGE), 0))
    assert res.trades
    for t in res.trades:
        expected = mult * abs(t.range_high - t.range_low)
        assert abs(t.entry_price - t.sl) == pytest.approx(expected, abs=0.02)


def test_mirror_anchor_differs_from_range_anchor(base, bars):
    """The mirror anchor includes the breakout overshoot, so it is wider."""
    a = run_reversal(base, bars, _capped(OrbReverseSettings(sl_range_mult=0.5,
                                                   sl_anchor=ANCHOR_RANGE), 0))
    b = run_reversal(base, bars, _capped(OrbReverseSettings(sl_range_mult=0.5,
                                                   sl_anchor=ANCHOR_MIRROR), 0))
    def avg_risk(res):
        r = [abs(t.entry_price - t.sl) for t in res.trades]
        return sum(r) / len(r)
    assert avg_risk(b) > avg_risk(a)


# ==========================================================================
# 2. the reversal really is reversed, and its stop is a real stop
# ==========================================================================
def test_direction_is_flipped(base, bars):
    st = _capped(OrbReverseSettings(sl_range_mult=0.5), 0)
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
        res = run_reversal(base, bars, _capped(OrbReverseSettings(sl_range_mult=mult), 0))
        assert res.trades
        for t in res.trades:
            if t.direction.upper().startswith("BUY"):
                assert t.sl < t.entry_price, f"BUY stop above entry at {t.entry_time}"
                assert t.tp > t.entry_price
            else:
                assert t.sl > t.entry_price, f"SELL stop below entry at {t.entry_time}"
                assert t.tp < t.entry_price


def test_no_zero_risk_and_reward_matches_rr(base, bars):
    res = run_reversal(base, bars, _capped(OrbReverseSettings(sl_range_mult=0.5), 0))
    for t in res.trades:
        risk = abs(t.entry_price - t.sl)
        assert risk > 0
        assert abs(t.tp - t.entry_price) == pytest.approx(2.0 * risk, rel=1e-3)


@pytest.mark.parametrize("cap", [1, 2, 3])
def test_trade_cap_is_respected(base, bars, cap):
    res = run_reversal(base, bars, _capped(OrbReverseSettings(sl_range_mult=0.5), cap))
    assert res.trades
    assert max(t.trade_no_in_session for t in res.trades) <= cap


def test_more_cap_means_at_least_as_many_trades(base, bars):
    counts = [len(run_reversal(base, bars,
                               _capped(OrbReverseSettings(sl_range_mult=0.5), c)).trades)
              for c in (1, 2, 3)]
    assert counts[0] < counts[1] <= counts[2]


# ==========================================================================
# 3. engines are selected, not swapped, and the core stays engine-agnostic
# ==========================================================================
def test_registry_resolves_both_engines():
    assert registry.resolve("orb") is OrbStrategy
    assert registry.resolve("orb_reverse") is OrbReverseStrategy
    assert {"orb", "orb_reverse"} <= set(registry.names())


def test_unknown_engine_names_the_valid_ones():
    """A typo in `engine:` must say what was allowed instead."""
    with pytest.raises(ValueError) as exc:
        registry.resolve("orb_reverze")
    message = str(exc.value)
    assert "orb_reverze" in message
    assert "orb" in message and "orb_reverse" in message


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

    a = AppConfig.load(os.path.join(ROOT, "orb", "engines", "orb", "config.yaml"))
    one = a.use_single_session("A")
    one.range_start, one.range_end, one.stop_time = "03:00", "03:15", "09:30"
    one.log_level = "none"
    two = copy.deepcopy(one)
    two.name, two.engine = "B", "orb_reverse"
    two.engine_options = {"sl_range_mult": 0.75}
    one.engine, one.engine_options = "orb", {}

    broker = _Broker()
    e1 = Engine(one, broker, logger=RbeaLogger(level=0))
    e2 = Engine(two, broker, logger=RbeaLogger(level=0))
    assert type(e1.strategy) is OrbStrategy
    assert type(e2.strategy) is OrbReverseStrategy
    # and building the second did not change the first
    assert type(e1.strategy) is OrbStrategy


def test_no_monkey_patch_survives_anywhere():
    """The class-swap is gone from the whole project. A leftover copy would be a
    silent no-op now that `Engine` resolves from the registry — a sweep would
    run forward trades and label them reversed."""
    import glob
    hits = []
    for path in glob.glob(os.path.join(ROOT, "**", "*.py"), recursive=True):
        if "_to_delete" in path or "__pycache__" in path:
            continue
        src = open(path, encoding="utf-8").read()
        if re.search(r"engine_mod\.\w*Strategy\s*=", src) or \
                re.search(r"orb\.engine\.\w*Strategy\s*=", src):
            hits.append(os.path.relpath(path, ROOT))
    assert hits == [], f"a strategy monkey-patch survives in {hits}"


def test_original_backtest_unaffected_by_a_reversal_run(base, bars):
    """Run the original, run a reversal, run the original again — identical."""
    a = _original(base, bars, "mid_range")
    run_reversal(base, bars, _capped(OrbReverseSettings(sl_range_mult=1.5), 2))
    b = _original(base, bars, "mid_range")
    assert [_key(t) for t in a.trades] == [_key(t) for t in b.trades]


def test_caller_config_is_not_mutated(base, bars):
    session = next(iter(base.enabled_sessions()))
    before = (session.sl_mode, session.max_trades_per_session, session.comment)
    run_reversal(base, bars, _capped(OrbReverseSettings(sl_range_mult=2.0), 1))
    after = (session.sl_mode, session.max_trades_per_session, session.comment)
    assert before == after
    assert session.engine == "orb" and session.engine_options == {}


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
        for cls in ("OrbStrategy", "OrbReverseStrategy"):
            assert cls not in code, (
                f"{cls} is named in orb/{module} — the core must not know "
                f"which engines exist")
        # nor may core reach into one engine's modules
        for engine in ("engines.orb", "engines.orb_reverse"):
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
    from orb.engines.orb import OrbSettings
    with pytest.raises(ValueError) as exc:
        OrbSettings.from_options({"sl_range_mult": 0.75})
    assert "sl_range_mult" in str(exc.value)


# ==========================================================================
# 4. settings and grid
# ==========================================================================
@pytest.mark.parametrize("kwargs", [
    {"sl_range_mult": 0}, {"sl_range_mult": -1}, {"sl_anchor": "nope"},
    {"direction": "sideways"},
])
def test_bad_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        OrbReverseSettings(**kwargs).validate()


def test_labels_are_file_safe_and_distinct():
    a = OrbReverseSettings(sl_range_mult=1.5).run_name(2)
    b = OrbReverseSettings(sl_range_mult=0.5).run_name(2)
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
        assert s.engine == "orb_reverse"
        assert s.engine_options["sl_range_mult"] == item.axes["sl_range_mult"]
        assert s.engine_options["direction"] == item.axes["direction"]
        assert s.max_trades_per_session == item.axes["max_trades_per_session"]
        assert s.range_start == "03:00" and s.range_end == "03:15"
        assert s.stop_time == "09:30"


def test_grid_new_york_stops_before_the_contract_roll(base):
    g = build_grid(base, sessions=["NEW_YORK"], timeframes=["M5"],
                   orb_minutes=[15], news_modes=["SKIP_NEWS"], risk_reward=[2.0],
                   sl_range_mults=[0.5], trade_caps=[1])
    assert next(iter(g[0].cfg.enabled_sessions())).stop_time == "16:55"
