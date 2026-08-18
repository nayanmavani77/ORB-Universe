"""Several engines, one system.

The requirement: the system supports multiple strategy engines, every engine has
the same structure, and engines run together in backtest and live without
interfering.

The load-bearing test is `test_mixed_engines_equal_separate_runs`: Asia running
the `orb` engine and London running the `orb_reverse` engine, in ONE backtest,
must produce exactly the trades each produces on its own. That is the whole
claim, and before the registry existed it was impossible — a second engine could
only be reached by rebinding a module global, which applied to every session at
once.

    python -m pytest tests/test_multi_engine.py -q
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orb.registry as registry                                   # noqa: E402
from orb.backtest import make_clock, run_backtest                 # noqa: E402
from orb.config import AppConfig, StrategyConfig                  # noqa: E402
from orb.data.dbn import load_dbn_bars                            # noqa: E402
from orb.engines import BUILTIN                                   # noqa: E402
from orb.engines.base import EngineSettings                       # noqa: E402
from orb.logger import RbeaLogger                                 # noqa: E402
from orb.markets import range_window                              # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "gc_1m_merged.parquet")
CONFIG = os.path.join(ROOT, "orb", "engines", "orb", "config.yaml")
START, END = "2026-03-01", "2026-06-01"

pytestmark = pytest.mark.skipif(not os.path.exists(DATA),
                                reason="merged bar data not available")


# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def base():
    cfg = AppConfig.load(CONFIG)
    cfg.backtest.dbn_paths = [DATA]
    cfg.server_timezone = "America/New_York"
    cfg.server_utc_offset_hours = 0
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


def _session(base, name, market, engine, options=None, magic=None):
    """One configured session, windowed on a real market open."""
    s = copy.deepcopy(base.strategy)
    start, end, stop = range_window(market, 15)
    s.name, s.enabled = name, True
    s.engine = engine
    opts = dict(options or {})
    # the cap is a SESSION field, never an engine option — engines reject it
    s.max_trades_per_session = int(opts.pop("max_trades_per_session", 0))
    s.engine_options = opts
    s.range_start, s.range_end, s.stop_time = start, end, stop
    s.signal_timeframe = "M5"
    s.risk_reward = 2.0
    s.log_level = "none"
    s.magic = magic if magic is not None else base.strategy.magic
    for _k, _l, cat in s.news.items():
        cat.mode = "off"
    s.news_days, s.news_trading = "", "off"
    return s


def _run(base, bars, sessions):
    cfg = copy.deepcopy(base)
    cfg.sessions = {s.name: s for s in sessions}
    cfg.validate_sessions()
    return run_backtest(cfg, bars, RbeaLogger(level=0))


def _key(t):
    return (t.session_name, t.entry_time, t.direction,
            round(t.entry_price, 6), round(t.sl, 6), round(t.tp, 6),
            round(t.net_profit, 6))


REVERSAL_OPTS = {"sl_range_mult": 0.75, "direction": "reverse",
                 "max_trades_per_session": 2}


# ==========================================================================
# 1. the requirement
# ==========================================================================
def test_mixed_engines_equal_separate_runs(base, bars):
    """Asia on breakout + London on reversal, together, equals each alone.

    Both engines must actually trade, and the combined run must be the exact
    union — no interference, no ordering effect, nothing lost.
    """
    asia = _session(base, "asia", "ASIA", "orb", magic=101)
    london = _session(base, "london", "LONDON", "orb_reverse", REVERSAL_OPTS,
                      magic=102)

    alone_a = _run(base, bars, [copy.deepcopy(asia)])
    alone_l = _run(base, bars, [copy.deepcopy(london)])
    together = _run(base, bars, [copy.deepcopy(asia), copy.deepcopy(london)])

    assert len(alone_a.trades) > 20, "the breakout session took no trades"
    assert len(alone_l.trades) > 20, "the reversal session took no trades"

    want = sorted([_key(t) for t in alone_a.trades]
                  + [_key(t) for t in alone_l.trades])
    got = sorted(_key(t) for t in together.trades)
    assert got == want

    by_session = {}
    for t in together.trades:
        by_session.setdefault(t.session_name, 0)
        by_session[t.session_name] += 1
    assert by_session == {"asia": len(alone_a.trades),
                          "london": len(alone_l.trades)}


def test_the_two_sessions_really_ran_different_strategies(base, bars):
    """Guard against the combined run quietly using one engine for both — which
    is exactly what the old global monkey-patch did."""
    london_rev = _session(base, "london", "LONDON", "orb_reverse", REVERSAL_OPTS,
                          magic=102)
    london_fwd = _session(base, "london", "LONDON", "orb_reverse",
                          dict(REVERSAL_OPTS, direction="forward"), magic=102)
    rev = _run(base, bars, [london_rev])
    fwd = _run(base, bars, [london_fwd])
    shared = ({t.entry_time: t.direction for t in rev.trades}.keys()
              & {t.entry_time: t.direction for t in fwd.trades}.keys())
    assert len(shared) > 10
    r = {t.entry_time: t.direction for t in rev.trades}
    f = {t.entry_time: t.direction for t in fwd.trades}
    assert all(r[k] != f[k] for k in shared)


def test_three_sessions_three_engine_settings(base, bars):
    """Asia breakout, London reversal, New York reversal with a different
    multiplier — all in one run."""
    sessions = [
        _session(base, "asia", "ASIA", "orb", magic=201),
        _session(base, "london", "LONDON", "orb_reverse",
                 {"sl_range_mult": 0.75, "max_trades_per_session": 2}, magic=202),
        _session(base, "new_york", "NEW_YORK", "orb_reverse",
                 {"sl_range_mult": 0.25, "max_trades_per_session": 1}, magic=203),
    ]
    res = _run(base, bars, [copy.deepcopy(s) for s in sessions])
    names = {t.session_name for t in res.trades}
    assert names == {"asia", "london", "new_york"}
    # the New York cap of 1 must be honoured independently of the others
    ny = [t for t in res.trades if t.session_name == "new_york"]
    assert max(t.trade_no_in_session for t in ny) == 1
    ld = [t for t in res.trades if t.session_name == "london"]
    assert max(t.trade_no_in_session for t in ld) == 2


def test_exits_are_journalled_by_the_session_that_opened_them(base, bars):
    """`MultiEngine.strategy_for` routes by session name. Routing to "the first
    session's strategy" would have a breakout strategy reporting a reversal
    exit once more than one engine is running."""
    asia = _session(base, "asia", "ASIA", "orb", magic=301)
    london = _session(base, "london", "LONDON", "orb_reverse", REVERSAL_OPTS,
                      magic=302)
    cfg = copy.deepcopy(base)
    cfg.sessions = {"asia": asia, "london": london}
    cfg.validate_sessions()

    from orb.engine import MultiEngine
    seen = {}

    class _Spy(MultiEngine):
        def strategy_for(self, session_name):
            s = super().strategy_for(session_name)
            seen[session_name] = type(s).__name__
            return s

    import orb.backtest as bt
    original = bt.MultiEngine
    bt.MultiEngine = _Spy
    try:
        run_backtest(cfg, bars, RbeaLogger(level=0))
    finally:
        bt.MultiEngine = original

    assert seen.get("asia") == "OrbStrategy"
    assert seen.get("london") == "OrbReverseStrategy"


# ==========================================================================
# 2. every engine has the same shape
# ==========================================================================
@pytest.mark.parametrize("module", BUILTIN, ids=lambda m: m.NAME)
def test_engine_follows_the_folder_contract(module):
    """Every engine folder holds the SAME five files — code plus its own single
    config. Documentation lives in docs/<engine>/, mirroring backtest/<engine>/,
    so the two engine folders contain nothing but code and config."""
    folder = os.path.dirname(os.path.abspath(module.__file__))
    for required in ("__init__.py", "strategy.py", "settings.py", "grid.py",
                     "config.yaml"):
        assert os.path.exists(os.path.join(folder, required)), \
            f"{module.NAME} is missing {required}"
    extra = {f for f in os.listdir(folder)
             if not f.startswith("__") and f != "__pycache__"}
    assert extra == {"strategy.py", "settings.py", "grid.py", "config.yaml"}, \
        f"{module.NAME} has unexpected files: {sorted(extra)}"
    assert os.path.exists(os.path.join(ROOT, "docs", module.NAME, "README.md")), \
        f"{module.NAME} has no docs/{module.NAME}/README.md"


@pytest.mark.parametrize("module", BUILTIN, ids=lambda m: m.NAME)
def test_every_engine_has_exactly_one_config(module):
    """One config file per engine, in its own folder, naming its own engine."""
    from orb.runconfig import RunConfig, config_path
    folder = os.path.dirname(os.path.abspath(module.__file__))
    configs = [f for f in os.listdir(folder) if f.endswith((".yaml", ".yml"))]
    assert configs == ["config.yaml"], \
        f"{module.NAME} should have exactly one config.yaml, found {configs}"
    rc = RunConfig.load(module.NAME)
    assert rc.engine == module.NAME
    assert rc.path == config_path(module.NAME)


@pytest.mark.parametrize("module", BUILTIN, ids=lambda m: m.NAME)
def test_engine_is_registered_and_well_formed(module):
    spec = registry.spec(module.NAME)
    assert spec.description.strip(), f"{module.NAME} has no description"
    assert issubclass(spec.settings_cls, EngineSettings)
    # the constructor signature Engine relies on
    import inspect
    params = inspect.signature(spec.strategy_cls.__init__).parameters
    for expected in ("cfg", "broker", "store", "logger"):
        assert expected in params, \
            f"{module.NAME}.{spec.strategy_cls.__name__} cannot be built by Engine"


@pytest.mark.parametrize("module", BUILTIN, ids=lambda m: m.NAME)
def test_engine_settings_round_trip(module):
    settings_cls = registry.spec(module.NAME).settings_cls
    settings = settings_cls.from_options({})
    assert settings_cls.from_options(settings.to_options()) == settings
    assert isinstance(settings.describe(), str) and settings.describe()


def test_every_engine_grid_declares_axes():
    import importlib
    for module in BUILTIN:
        grid = importlib.import_module(f"{module.__name__}.grid")
        assert getattr(grid, "AXES", None), f"{module.NAME}.grid has no AXES"
        assert callable(getattr(grid, "build", None))


def test_registering_a_name_twice_is_refused():
    class _Fake:
        def __init__(self, cfg, broker, store=None, logger=None): pass
    with pytest.raises(ValueError) as exc:
        registry.register("orb", _Fake)
    assert "already registered" in str(exc.value)
    # re-registering the SAME class is fine — imports can happen twice
    from orb.engines.orb import OrbStrategy
    registry.register("orb", OrbStrategy)


# ==========================================================================
# 3. engine_options survive the config machinery
# ==========================================================================
def test_engine_options_survive_asdict_and_to_dict(base):
    cfg = copy.deepcopy(base)
    s = cfg.use_single_session("MAIN")
    s.engine = "orb_reverse"
    s.engine_options = {"sl_range_mult": 1.25}
    data = cfg.to_dict()
    assert data["strategy"]["engine"] == "orb_reverse"
    assert data["strategy"]["engine_options"] == {"sl_range_mult": 1.25}


def test_engine_options_are_inherited_by_sessions():
    """`from_dict` builds each session from `asdict(cfg.strategy)`. A plain
    dict field survives that; the `rev_*` attributes it replaced did not."""
    cfg = AppConfig.from_dict({
        "server_timezone": "America/New_York",
        "defaults": {"engine": "orb_reverse",
                     "engine_options": {"sl_range_mult": 1.5},
                     "range_start": "03:00", "range_end": "03:15",
                     "stop_time": "09:30"},
        "sessions": {
            "inherits": {"range_start": "03:00", "range_end": "03:15",
                         "stop_time": "09:25", "magic": 11},
            "overrides": {"range_start": "19:00", "range_end": "19:15",
                          "stop_time": "02:55", "magic": 12,
                          "engine": "orb", "engine_options": {}},
        },
        "symbol": {}, "databento": {}, "backtest": {}, "mt5": {},
    })
    assert cfg.sessions["inherits"].engine == "orb_reverse"
    assert cfg.sessions["inherits"].engine_options == {"sl_range_mult": 1.5}
    assert cfg.sessions["overrides"].engine == "orb"
    assert cfg.sessions["overrides"].engine_options == {}


def test_engine_defaults_to_breakout_so_old_configs_still_load(base):
    """Every config written before engines existed must keep working."""
    cfg = AppConfig.load(CONFIG)
    for session in cfg.sessions.values():
        assert session.engine == "orb"
        assert session.engine_options == {}


def test_a_bad_engine_option_fails_at_construction(base, bars):
    cfg = copy.deepcopy(base)
    s = cfg.use_single_session("MAIN")
    s.engine = "orb_reverse"
    s.engine_options = {"sl_range_mult": -1}
    s.range_start, s.range_end, s.stop_time = "03:00", "03:15", "09:30"
    s.log_level = "none"
    with pytest.raises(ValueError) as exc:
        run_backtest(cfg, bars[:500], RbeaLogger(level=0))
    assert "sl_range_mult" in str(exc.value)


def test_engine_name_is_normalised():
    s = StrategyConfig(engine="  Orb_Reverse  ", range_start="03:00",
                       range_end="03:15", stop_time="09:30")
    s.validate()
    assert s.engine == "orb_reverse"


# ==========================================================================
# 4. live trading takes the same path
# ==========================================================================
def test_live_builds_the_right_engine_per_session(base):
    """`LiveTrader` drives the same `MultiEngine`, so engine selection must work
    there too. Before the registry it could not: nothing applied the monkey-patch
    in the live path, so the reversal had no live path at all.
    """
    from orb.live_trader import LiveTrader

    class _Broker:
        spec = type("S", (), {"name": "GC", "digits": 2, "point": 0.01,
                              "tick_size": 0.1, "stops_level_points": 0})()
        digits, stops_level_price, translate_levels = 2, 0.0, False
        def sync_market(self, *a): pass
        def settle_bar(self, *a): pass
        def reference_price(self, is_buy): return 0.0
        def price_for(self, is_buy): return 0.0
        def positions_count(self): return 0
        def normalize_price(self, p): return p
        def normalize_lot(self, l): return l

    class _Feed:
        def start(self): pass
        def stop(self): pass
        def poll(self): return []
        def history(self, *a, **k): return []

    cfg = copy.deepcopy(base)
    cfg.sessions = {
        "asia": _session(base, "asia", "ASIA", "orb", magic=401),
        "london": _session(base, "london", "LONDON", "orb_reverse",
                           REVERSAL_OPTS, magic=402),
    }
    cfg.validate_sessions()

    trader = LiveTrader(cfg, broker=_Broker(), feed=_Feed(),
                        logger=RbeaLogger(level=0))
    built = {e.cfg.name: type(e.strategy).__name__ for e in trader.engine.engines}
    assert built == {"asia": "OrbStrategy",
                     "london": "OrbReverseStrategy"}


def test_every_engine_grid_returns_the_same_item_type():
    """Both engines' grids return `base.GridItem`, so a sweep tool treats every
    engine alike: `run_name` and `cfg` always mean the same thing, and `row()`
    always produces the results record, with that engine's own axes flattened
    in."""
    import importlib
    from orb.engines.base import GridItem
    cfg = AppConfig.load(CONFIG)
    common = dict(sessions=["LONDON"], timeframes=["M5"], orb_minutes=[15],
                  risk_reward=[2.0], news_modes=["SKIP_NEWS"])
    shape = None
    for module in BUILTIN:
        grid = importlib.import_module(f"{module.__name__}.grid")
        items = grid.build(cfg, **common)
        assert items and all(isinstance(i, GridItem) for i in items)
        item = items[0]
        assert item.engine == module.NAME
        row = item.row()
        fixed = {"run_name", "engine", "session", "signal_timeframe",
                 "orb_minutes", "news_mode", "risk_reward", "range_start",
                 "range_end", "stop_time"}
        assert fixed <= set(row), f"{module.NAME} row is missing {fixed - set(row)}"
        assert set(item.axes) <= set(row)
        shape = fixed if shape is None else shape
        assert fixed == shape


# ==========================================================================
# 5. one master config per engine, and no parent config
# ==========================================================================
def test_there_is_no_parent_config():
    """Each engine's config.yaml is complete. A config.yaml in the project root
    would be a parent again, and the whole point is that there is not one."""
    assert not os.path.exists(os.path.join(ROOT, "config.yaml")), \
        "a parent config.yaml has reappeared in the project root"


@pytest.mark.parametrize("module", BUILTIN, ids=lambda m: m.NAME)
def test_engine_config_is_self_contained(module):
    """Every block a run needs is IN the engine's own file — no lookups."""
    from orb.runconfig import RunConfig
    rc = RunConfig.load(module.NAME)
    app = rc.app
    assert app.symbol.name, "no instrument"
    assert app.backtest.dbn_paths, "no data path"
    assert app.backtest.initial_balance > 0, "no account size"
    assert app.server_timezone or app.server_utc_offset_hours is not None
    assert app.sessions, "no sessions"
    assert any(s.enabled for s in app.sessions.values()), "no enabled session"
    assert rc.dates()[0] < rc.dates()[1], "no backtest period"
    # and every session defaults to this file's engine
    for session in app.sessions.values():
        assert session.engine in {module.NAME} | set(registry.names())


def test_merged_run_needs_no_parent_config():
    """The requirement that survived removing the parent: several engines, one
    account. Sessions come from each engine's own file."""
    from orb.runconfig import RunConfig, merge
    configs = RunConfig.load_many(["orb", "orb_reverse"])
    configs[0].app.sessions["asia"].enabled = True
    configs[0].app.sessions["new_york"].enabled = False
    app = merge(configs)
    engines = {s.name: s.engine for s in app.enabled_sessions()}
    assert engines == {"asia": "orb", "london": "orb_reverse"}
    # one account, one instrument — taken from configs that agree
    assert app.symbol.name == configs[0].app.symbol.name
    assert app.backtest.initial_balance == configs[0].app.backtest.initial_balance


def test_merged_run_trades_both_engines(base, bars):
    """Not just configured — actually traded, by two different strategies."""
    from orb.runconfig import RunConfig, merge
    configs = RunConfig.load_many(["orb", "orb_reverse"])
    configs[0].app.sessions["asia"].enabled = True
    configs[0].app.sessions["new_york"].enabled = False
    app = merge(configs)
    app.backtest.dbn_paths = [DATA]
    for session in app.enabled_sessions():
        session.log_level = "none"
    app.strategy.log_level = "none"
    res = run_backtest(app, bars, RbeaLogger(level=0))
    traded = {t.session_name for t in res.trades}
    assert traded == {"asia", "london"}, f"only {traded} traded"


def test_merged_run_rejects_disagreeing_configs():
    """One account, one instrument, one feed. If two engine configs disagree
    about those, that is a misconfiguration — say so, do not pick a winner."""
    from orb.runconfig import RunConfig, merge
    configs = RunConfig.load_many(["orb", "orb_reverse"])
    configs[1].app.symbol.name = "SI"          # a different instrument
    configs[1].app.backtest.initial_balance = 5000.0
    with pytest.raises(SystemExit) as exc:
        merge(configs)
    message = str(exc.value)
    assert "symbol.name" in message
    assert "backtest.initial_balance" in message
    assert "GC" in message and "SI" in message


def test_merged_run_rejects_a_magic_collision():
    """Two sessions sharing a magic number would be indistinguishable to the
    broker once both engines hold positions."""
    from orb.runconfig import RunConfig, merge
    configs = RunConfig.load_many(["orb", "orb_reverse"])
    configs[0].app.sessions["asia"].enabled = True
    configs[0].app.sessions["new_york"].enabled = False
    configs[1].app.sessions["london"].magic = \
        configs[0].app.sessions["asia"].magic
    with pytest.raises(SystemExit) as exc:
        merge(configs)
    assert "magic" in str(exc.value)


def test_a_session_can_name_a_different_engine_in_one_file():
    """The other way to mix: one config file, one session overriding `engine:`.
    Both routes must work, since a config file is the master for its engine."""
    from orb.runconfig import RunConfig
    rc = RunConfig.load("orb")
    app = copy.deepcopy(rc.app)
    app.sessions["asia"].enabled = True
    app.sessions["asia"].engine = "orb_reverse"
    app.sessions["asia"].engine_options = {"sl_range_mult": 0.5}
    app.validate_sessions()
    engines = {s.name: s.engine for s in app.enabled_sessions()}
    assert engines["asia"] == "orb_reverse"
    assert engines["new_york"] == "orb"


# ==========================================================================
# 6. session settings take priority over defaults — all of them
# ==========================================================================
def _cfg(defaults, sessions):
    return AppConfig.from_dict({
        "server_timezone": "America/New_York",
        "defaults": defaults, "sessions": sessions,
        "symbol": {}, "databento": {}, "backtest": {}, "mt5": {}})


WINDOW_A = {"range_start": "03:00", "range_end": "03:15",
            "stop_time": "09:25", "magic": 11}
WINDOW_B = {"range_start": "19:00", "range_end": "19:30",
            "stop_time": "02:55", "magic": 12}


def test_plain_session_fields_override_defaults():
    cfg = _cfg({"signal_timeframe": "M5", "risk_reward": 4.0,
                "sl_mode": "mid_range", "lots": 1.0,
                "max_trades_per_session": 0},
               {"a": dict(WINDOW_A, signal_timeframe="M1", risk_reward=1.5,
                          sl_mode="full_range", lots=3.0,
                          max_trades_per_session=2),
                "b": dict(WINDOW_B)})
    a, b = cfg.sessions["a"], cfg.sessions["b"]
    assert (a.signal_timeframe, a.risk_reward, a.sl_mode, a.lots,
            a.max_trades_per_session) == ("M1", 1.5, "full_range", 3.0, 2)
    assert (b.signal_timeframe, b.risk_reward, b.sl_mode, b.lots,
            b.max_trades_per_session) == ("M5", 4.0, "mid_range", 1.0, 0)


def test_one_engine_option_overrides_without_dropping_the_others():
    """The bug this locks: a session stating ONE option used to replace the
    whole dict, so `max_trades_per_session: 2` silently fell back to the
    engine default and the run took a different number of trades than the
    config appears to ask for."""
    cfg = _cfg({"engine": "orb_reverse",
                "engine_options": {"sl_range_mult": 0.5, "direction": "reverse",
                                   "sl_anchor": "range"}},
               {"a": dict(WINDOW_A, engine_options={"sl_range_mult": 1.5}),
                "b": dict(WINDOW_B)})
    assert cfg.sessions["a"].engine_options == {
        "sl_range_mult": 1.5, "direction": "reverse", "sl_anchor": "range"}
    assert cfg.sessions["b"].engine_options == {
        "sl_range_mult": 0.5, "direction": "reverse", "sl_anchor": "range"}


def test_a_session_switching_engine_does_not_inherit_the_other_vocabulary():
    """Options belong to one engine. Handing orb_reverse's options to an orb
    session would either be rejected as unknown or, worse, collide on a name
    and mean something else."""
    cfg = _cfg({"engine": "orb_reverse",
                "engine_options": {"sl_range_mult": 0.5}},
               {"a": dict(WINDOW_A),
                "b": dict(WINDOW_B, engine="orb")})
    assert cfg.sessions["a"].engine_options == {"sl_range_mult": 0.5}
    assert cfg.sessions["b"].engine == "orb"
    assert cfg.sessions["b"].engine_options == {}


def test_engine_options_must_be_a_mapping():
    with pytest.raises(ValueError) as exc:
        _cfg({"engine": "orb_reverse"},
             {"a": dict(WINDOW_A, engine_options=[1, 2])})
    assert "engine_options" in str(exc.value)


def test_a_session_log_level_is_not_silenced_by_the_defaults():
    """One journal, many sessions. Taking the level from the defaults threw
    away a session that asked for `verbose` — the detail it was turned on for
    was never written."""
    from orb.config import journal_settings
    cfg = _cfg({"log_level": "none"},
               {"a": dict(WINDOW_A, log_level="verbose"), "b": dict(WINDOW_B)})
    level, _path, _show = journal_settings(cfg)
    assert level == 2, "a session asking for verbose was silenced"


def test_journal_file_comes_from_whichever_session_names_one():
    from orb.config import journal_settings
    cfg = _cfg({"log_level": "normal", "log_file": None},
               {"a": dict(WINDOW_A, log_file="run.log"), "b": dict(WINDOW_B)})
    assert journal_settings(cfg)[1] == "run.log"


# ==========================================================================
# 7. the report states what actually ran
# ==========================================================================
def test_report_shows_the_engine_and_its_options(base, bars):
    """The report used to print `sl_mode` for every session — a field the
    reverse engine ignores — and never showed the multiplier it really used.
    It said "mid range" for a run whose stop was 0.75 x the range."""
    from orb.report import _sessions_table, trades_dataframe

    sessions = [
        _session(base, "asia", "ASIA", "orb", magic=701),
        _session(base, "london", "LONDON", "orb_reverse",
                 {"sl_range_mult": 0.75, "direction": "reverse",
                  "max_trades_per_session": 2}, magic=702),
    ]
    res = _run(base, bars, [copy.deepcopy(s) for s in sessions])
    cfg = copy.deepcopy(base)
    cfg.sessions = {s.name: s for s in sessions}
    table = _sessions_table(cfg, res, trades_dataframe(res.trades))

    assert "orb_reverse" in table and ">orb<" in table.replace("</code>", "<")
    assert "0.75 × range" in table, "the multiplier that ran is not in the report"
    assert "mid range" in table, "the orb session's real stop mode is missing"
    assert "Engine" in table and "Engine options" in table
    assert "mixed engines" in table, "a mixed run is not flagged as one"


def test_report_marks_a_multiplier_that_equals_an_orb_stop_mode(base, bars):
    from orb.report import _sessions_table, trades_dataframe
    sessions = [_session(base, "london", "LONDON", "orb_reverse",
                         {"sl_range_mult": 1.0}, magic=703)]
    res = _run(base, bars, [copy.deepcopy(s) for s in sessions])
    cfg = copy.deepcopy(base)
    cfg.sessions = {s.name: s for s in sessions}
    table = _sessions_table(cfg, res, trades_dataframe(res.trades))
    assert "1 × range (= full range)" in table


def test_a_core_field_under_engine_options_is_refused():
    """The bug this locks: `max_trades_per_session` written under
    engine_options is read by nobody — the core enforces it from the SESSION.
    A config asking for 3 trades silently ran unlimited."""
    from orb.engines.orb_reverse import OrbReverseSettings
    with pytest.raises(ValueError) as exc:
        OrbReverseSettings.from_options({"sl_range_mult": 0.75,
                                         "max_trades_per_session": 3})
    message = str(exc.value)
    assert "session settings, not engine options" in message
    assert "max_trades_per_session" in message


def test_the_shipped_config_actually_caps_the_session(base, bars):
    """End to end: the orb_reverse config asks for 3 trades per session, and
    the run must take no more."""
    from orb.runconfig import RunConfig
    rc = RunConfig.load("orb_reverse")
    app = rc.app_config()
    app.backtest.dbn_paths = [DATA]
    for session in app.enabled_sessions():
        session.log_level = "none"
    app.strategy.log_level = "none"
    asked = {s.name: s.max_trades_per_session for s in app.enabled_sessions()}
    assert asked.get("london") == 3, "the config no longer asks for a cap of 3"
    res = run_backtest(app, bars, RbeaLogger(level=0))
    taken = max(t.trade_no_in_session for t in res.trades)
    assert taken <= 3, f"the cap was not applied — {taken} trades in a session"


# ==========================================================================
# 8. credentials live in .env, never in a config file
# ==========================================================================
def test_no_engine_config_contains_a_credential_field():
    """Engine configs are tracked in git. A key or password written into one is
    committed and pushed, so the fields must not be there to fill in."""
    import re as _re
    import yaml
    for module in BUILTIN:
        path = os.path.join(os.path.dirname(os.path.abspath(module.__file__)),
                            "config.yaml")
        raw = yaml.safe_load(open(path, encoding="utf-8").read()) or {}
        assert not (raw.get("databento") or {}).get("api_key"), \
            f"{module.NAME}/config.yaml carries a Databento key"
        mt5 = raw.get("mt5") or {}
        for field in ("login", "password", "server", "terminal_path"):
            assert field not in mt5, \
                f"{module.NAME}/config.yaml has mt5.{field} — it belongs in .env"


def test_env_example_exists_and_is_a_template_only():
    """The committed template must list every variable and hold no real value."""
    path = os.path.join(ROOT, ".env.example")
    assert os.path.exists(path), ".env.example is missing"
    text = open(path, encoding="utf-8").read()
    for var in ("DATABENTO_API_KEY", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER",
                "MT5_TERMINAL_PATH"):
        assert var in text, f".env.example does not mention {var}"
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        _key, _, value = line.partition("=")
        assert value.strip() == "", \
            f".env.example must stay empty, but has a value: {line}"


def test_gitignore_hides_env_but_keeps_the_template():
    text = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
    lines = [l.strip() for l in text.splitlines()]
    assert ".env" in lines, ".gitignore does not ignore .env"
    assert "!.env.example" in lines, ".gitignore also hides the template"


def test_dotenv_parsing(tmp_path, monkeypatch):
    """Quotes, an `export` prefix, comments and blank lines — the shapes a
    hand-edited .env actually takes."""
    from orb.config import load_dotenv
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n\n"
        "DATABENTO_API_KEY=db-plain\n"
        'MT5_PASSWORD="p@ss word#hash"\n'
        "export MT5_SERVER=Broker-Live1\n"
        "MT5_TERMINAL_PATH=C:/Program Files/MetaTrader 5/terminal64.exe\n"
        "NOT_A_PAIR\n", encoding="utf-8")
    for var in ("DATABENTO_API_KEY", "MT5_PASSWORD", "MT5_SERVER",
                "MT5_TERMINAL_PATH"):
        monkeypatch.delenv(var, raising=False)
    applied = load_dotenv(str(env))
    assert applied["DATABENTO_API_KEY"] == "db-plain"
    assert applied["MT5_PASSWORD"] == "p@ss word#hash"   # quotes off, # kept
    assert applied["MT5_SERVER"] == "Broker-Live1"       # `export ` stripped
    assert applied["MT5_TERMINAL_PATH"].endswith("terminal64.exe")
    assert "NOT_A_PAIR" not in applied


def test_a_real_env_var_beats_the_file(tmp_path, monkeypatch):
    """So one command can point at a second account without editing .env."""
    from orb.config import load_dotenv
    env = tmp_path / ".env"
    env.write_text("MT5_LOGIN=11111111\n", encoding="utf-8")
    monkeypatch.setenv("MT5_LOGIN", "99999999")
    load_dotenv(str(env))
    assert os.environ["MT5_LOGIN"] == "99999999"


def test_missing_secrets_names_what_is_absent(monkeypatch):
    from orb.config import missing_secrets
    from orb.runconfig import RunConfig
    for var in ("DATABENTO_API_KEY", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
        monkeypatch.delenv(var, raising=False)
    cfg = RunConfig.load("orb").app
    cfg.databento.api_key = None
    cfg.mt5.login = cfg.mt5.password = cfg.mt5.server = None
    assert missing_secrets(cfg, live=True) == [
        "DATABENTO_API_KEY", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"]
    assert missing_secrets(cfg, live=False) == ["DATABENTO_API_KEY"]


def test_live_exits_are_routed_to_the_session_that_opened_them():
    """A closing trade must be journalled by the strategy that opened it.

    `orb/backtest.py` has always routed exits by session name. The live path
    used `LiveTrader.strategy`, which is the FIRST session's — so an
    `orb_reverse` exit was reported by the `orb` strategy. Harmless while
    `report_exit` is pure journalling with a shared logger, but wrong, and a
    trap the moment it gains any state or per-session output. Live routes on
    the magic, which names a session uniquely (`runconfig.merge` refuses a run
    whose magics collide).
    """
    from orb.broker import SimBroker
    from orb.engines.orb.strategy import OrbStrategy
    from orb.engines.orb_reverse.strategy import OrbReverseStrategy
    from orb.live_trader import LiveTrader
    from orb.logger import RbeaLogger
    from orb.runconfig import RunConfig, merge

    class Feed:
        def start(self): pass
        def stop(self): pass
        def poll(self, timeout=1.0): return None

    cfg = merge(RunConfig.load_many(["orb", "orb_reverse"]))
    for s in cfg.sessions.values():
        s.enabled = True
    trader = LiveTrader(cfg, broker=SimBroker(cfg.symbol, 100000.0),
                        feed=Feed(), logger=RbeaLogger(level=0))

    by_magic = {s.magic: s for s in cfg.enabled_sessions()}
    assert len(by_magic) > 1, "this test needs a mixed run"
    expected = {"orb": OrbStrategy, "orb_reverse": OrbReverseStrategy}
    for magic, session in by_magic.items():
        got = trader._strategy_for_magic(magic)
        assert isinstance(got, expected[session.engine]), (
            f"magic {magic} ({session.name}, {session.engine}) routed to "
            f"{type(got).__name__}")

    # an unknown magic must not raise — it falls back, as it always did
    assert trader._strategy_for_magic(-1) is trader.strategy
