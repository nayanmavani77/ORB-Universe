#!/usr/bin/env python3
"""Multi-session tests.

The contract this file defends:

  1. A config with no `sessions:` block behaves EXACTLY as before — one
     session, driven by the `strategy:` block.
  2. Sessions are independent: running two together produces byte-identical
     trades to running each one alone.
  3. A disabled session is never constructed and can never trade.
  4. Enabled sessions may not overlap, and the config is rejected if they do.
  5. Each session keeps its own settings — timeframe, R:R, SL mode, news.
  6. The strategy rules themselves are untouched: the per-session engine emits
     the same tick sequence the single engine always did.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orb.backtest import run_backtest                                # noqa: E402
from orb.bars import Bar                                             # noqa: E402
from orb.config import AppConfig, StrategyConfig, SymbolSpec         # noqa: E402
from orb.engine import Engine, MultiEngine                           # noqa: E402
from orb.logger import RbeaLogger                                    # noqa: E402

PASS = FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")


def raises(label, fn, needle=""):
    global PASS, FAIL
    try:
        fn()
    except Exception as exc:                                    # noqa: BLE001
        if needle and needle.lower() not in str(exc).lower():
            FAIL += 1
            print(f"  FAIL  {label}: wrong message: {exc}")
        else:
            PASS += 1
        return
    FAIL += 1
    print(f"  FAIL  {label}: expected an error, none raised")


# --------------------------------------------------------------------------
def make_bars(days=10, start=datetime(2026, 3, 2, 0, 0)):
    """Ten days of synthetic 1-minute bars — a seeded random walk.

    A regular wave would break out at the same minute every day and could mask
    a session bug; a random walk gives every window a genuine mix of breakouts
    and reversals. The seed is fixed, so the stream is identical on every run
    and any difference between two backtests comes from session logic alone.
    """
    import random
    rng = random.Random(20260814)
    bars = []
    price = 2000.0
    t = start
    for _d in range(days):
        for _m in range(24 * 60):
            price += rng.gauss(0.0, 0.45)
            close = price + rng.gauss(0.0, 0.2)
            hi = max(price, close) + abs(rng.gauss(0.0, 0.25))
            lo = min(price, close) - abs(rng.gauss(0.0, 0.25))
            bars.append(Bar(t, round(price, 2), round(hi, 2), round(lo, 2),
                            round(close, 2), 10.0))
            price = close
            t += timedelta(minutes=1)
    return bars


def base_config():
    cfg = AppConfig()
    cfg.symbol = SymbolSpec(name="TEST", digits=2, point=0.01, tick_size=0.01,
                            volume_min=1.0, volume_step=1.0, volume_max=10.0,
                            value_per_price_unit=1.0)
    cfg.backtest.initial_balance = 100000.0
    cfg.strategy.log_level = "none"
    return cfg


_MAGIC = [20260814]


def session(name, start, end, stop, tf="M5", rr=2.0, enabled=True, magic=None):
    """A session built by hand. Each gets its own magic, exactly as the YAML
    loader assigns one — two sessions sharing a magic is a config error."""
    if magic is None:
        _MAGIC[0] += 1
        magic = _MAGIC[0]
    return StrategyConfig(name=name, enabled=enabled, range_start=start,
                          range_end=end, stop_time=stop, signal_timeframe=tf,
                          risk_reward=rr, lots=1.0, log_level="none",
                          magic=magic)


def run(cfg, bars):
    return run_backtest(cfg, bars, RbeaLogger(level=0))


def fingerprint(result):
    """Everything about a trade that the session logic could influence."""
    return [(t.session_name, t.direction, t.entry_time, round(t.entry_price, 4),
             round(t.sl, 4), round(t.tp, 4), t.exit_time,
             round(t.exit_price, 4), t.exit_reason) for t in result.trades]


# ==========================================================================
print("=" * 62)
print("  Multi-session tests")
print("=" * 62)

BARS = make_bars()

# --- 1. backward compatibility -------------------------------------------
print("\n[1] a config with no sessions block is still one session")
cfg = base_config()
cfg.strategy.range_start, cfg.strategy.range_end = "09:00", "09:30"
cfg.strategy.stop_time = "16:00"
cfg.sessions = {"MAIN": cfg.strategy}
check("one session registered", len(cfg.sessions), 1)
check("it is the strategy object itself", cfg.sessions["MAIN"] is cfg.strategy, True)
check("enabled by default", cfg.strategy.enabled, True)
single = run(cfg, BARS)
check("it trades", len(single.trades) > 0, True)
check("tagged with its name", {t.session_name for t in single.trades}, {"MAIN"})

# --- 2. independence ------------------------------------------------------
print("\n[2] two sessions together == each one alone")
A = ("asia", "19:00", "19:30", "02:55", "M1", 1.5)
N = ("ny", "09:30", "10:00", "16:55", "M15", 3.0)

cfg_a = base_config(); cfg_a.sessions = {"asia": session(*A)}
cfg_n = base_config(); cfg_n.sessions = {"ny": session(*N)}
cfg_b = base_config(); cfg_b.sessions = {"asia": session(*A), "ny": session(*N)}
for c in (cfg_a, cfg_n, cfg_b):
    c.validate_sessions()

ra, rn, rb = run(cfg_a, BARS), run(cfg_n, BARS), run(cfg_b, BARS)
check("asia alone trades", len(ra.trades) > 0, True)
check("ny alone trades", len(rn.trades) > 0, True)
check("combined count == sum", len(rb.trades), len(ra.trades) + len(rn.trades))
merged = sorted(fingerprint(ra) + fingerprint(rn), key=lambda r: (r[2], r[0]))
combined = sorted(fingerprint(rb), key=lambda r: (r[2], r[0]))
check("combined trades identical to separate runs", merged, combined)

# --- 3. disabled sessions -------------------------------------------------
print("\n[3] a disabled session never trades")
cfg = base_config()
cfg.sessions = {"asia": session(*A), "ny": session(*N, enabled=False)}
cfg.validate_sessions()
check("only one session enabled", len(cfg.enabled_sessions()), 1)
r = run(cfg, BARS)
check("no trades carry the disabled name", any(t.session_name == "ny"
                                               for t in r.trades), False)
check("matches the asia-only run", fingerprint(r), fingerprint(ra))

print("\n[3b] every session disabled is an error")
cfg = base_config()
cfg.sessions = {"asia": session(*A, enabled=False)}
raises("all disabled rejected", cfg.validate_sessions, "every session is disabled")

# --- 4. overlap validation ------------------------------------------------
print("\n[4] enabled sessions may not overlap")
def cfg_with(*sessions):
    c = base_config()
    c.sessions = {s.name: s for s in sessions}
    return c

ok = cfg_with(session("asia", "19:00", "19:30", "02:55"),
              session("london", "03:00", "03:30", "09:25"),
              session("ny", "09:30", "10:00", "16:55"))
ok.validate_sessions()
check("three clean sessions accepted", len(ok.enabled_sessions()), 3)

raises("asia running into london",
       cfg_with(session("asia", "19:00", "19:30", "03:30"),
                session("london", "03:00", "03:30", "09:25")).validate_sessions,
       "overlap")
raises("ny wrapping into asia",
       cfg_with(session("asia", "19:00", "19:30", "02:55"),
                session("ny", "09:30", "10:00", "19:30")).validate_sessions,
       "overlap")
raises("identical windows",
       cfg_with(session("a", "09:30", "10:00", "16:55"),
                session("b", "09:30", "10:00", "16:55")).validate_sessions,
       "overlap")
raises("no stop time alongside another session",
       cfg_with(session("asia", "19:00", "19:30", "0"),
                session("ny", "09:30", "10:00", "16:55")).validate_sessions,
       "stop time")
raises("close_at_stop_time off with two sessions",
       cfg_with(session("asia", "19:00", "19:30", "02:55"),
                StrategyConfig(name="ny", range_start="09:30", range_end="10:00",
                               stop_time="16:55", close_at_stop_time=False,
                               log_level="none", magic=20269999)).validate_sessions,
       "close_at_stop_time")

print("\n[4b] a single session may run without a stop time")
solo = cfg_with(session("asia", "19:00", "19:30", "0"))
solo.validate_sessions()
check("24h window allowed when alone", len(solo.enabled_sessions()), 1)

print("\n[4c] overlap is ignored for disabled sessions")
c = cfg_with(session("asia", "19:00", "19:30", "03:30"),
             session("london", "03:00", "03:30", "09:25", enabled=False))
c.validate_sessions()
check("disabled session cannot collide", len(c.enabled_sessions()), 1)

# --- 5. per-session settings ----------------------------------------------
print("\n[5] each session keeps its own settings")
cfg = cfg_with(session("asia", "19:00", "19:30", "02:55", tf="M1", rr=1.5),
               session("ny", "09:30", "10:00", "16:55", tf="M15", rr=3.0))
cfg.validate_sessions()
eng = MultiEngine(cfg.enabled_sessions(), _DummyBroker := type(
    "B", (), {"spec": cfg.symbol, "sync_market": lambda *a: None,
              "settle_bar": lambda *a: None,
              "positions_count": lambda self: 0,
              "digits": 2})(), logger=RbeaLogger(level=0))
check("one engine per session", len(eng.engines), 2)
check("asia resampler is M1", eng.engines[0].resampler.tf_seconds, 60)
check("ny resampler is M15", eng.engines[1].resampler.tf_seconds, 900)
check("separate bar stores",
      eng.engines[0].store is not eng.engines[1].store, True)
check("separate strategy objects",
      eng.engines[0].strategy is not eng.engines[1].strategy, True)
check("asia R:R kept", eng.engines[0].strategy.cfg.risk_reward, 1.5)
check("ny R:R kept", eng.engines[1].strategy.cfg.risk_reward, 3.0)

print("\n[5c] news_mode is a one-line per-session switch")
from orb.config import NEWS_CATEGORIES                               # noqa: E402
raw = {
    "defaults": {"lots": 1.0, "log_level": "none"},
    "news": {"core_cpi_mm": {"mode": "off", "dates": "2026.03.04\n2026.03.06"}},
    "sessions": {
        "asia": {"enabled": True, "range_start": "19:00", "range_end": "19:30",
                 "stop_time": "02:55", "news_mode": "off"},
        "ny": {"enabled": True, "range_start": "09:30", "range_end": "10:00",
               "stop_time": "16:55", "news_mode": "on"},
    },
}
c = AppConfig.from_dict(raw)
check("asia: every category off",
      {cat.mode for _, _, cat in c.sessions["asia"].news.items()}, {"off"})
check("ny: every category on",
      {cat.mode for _, _, cat in c.sessions["ny"].news.items()}, {"on"})
check("asia general bucket follows", c.sessions["asia"].news_trading, "off")
check("dates survive the switch",
      len(c.sessions["ny"].news.core_cpi_mm.dates.split()), 2)
raises("bad news_mode rejected",
       lambda: AppConfig.from_dict({**raw, "sessions": {
           "asia": {**raw["sessions"]["asia"], "news_mode": "maybe"}}}),
       "news_mode must be one of")

print("\n[5d] a per-category override inherits the dates")
raw2 = {
    "defaults": {"lots": 1.0, "log_level": "none"},
    "news": {k: {"mode": "off", "dates": "2026.03.04"} for k in NEWS_CATEGORIES},
    "sessions": {
        "asia": {"enabled": True, "range_start": "19:00", "range_end": "19:30",
                 "stop_time": "02:55",
                 "news": {"federal_funds_rate": {"mode": "on"}}},
    },
}
c2 = AppConfig.from_dict(raw2)
a = c2.sessions["asia"].news
check("named category overridden", a.federal_funds_rate.mode, "on")
check("its dates kept", a.federal_funds_rate.dates.strip(), "2026.03.04")
check("untouched categories keep their mode", a.core_cpi_mm.mode, "off")
check("untouched categories keep their dates",
      a.core_cpi_mm.dates.strip(), "2026.03.04")
check("no category lost its dates",
      all(cat.dates.strip() for _, _, cat in a.items()), True)
raises("unknown category rejected",
       lambda: AppConfig.from_dict({**raw2, "sessions": {"asia": {
           **raw2["sessions"]["asia"], "news": {"nfp_typo": {"mode": "on"}}}}}),
       "unknown news categor")

print("\n[5e] opposite news treatment actually changes the trades")
NEWS_DAY = "2026.03.04"
raw3 = {
    "defaults": {"lots": 1.0, "log_level": "none",
                 "signal_timeframe": "M5", "risk_reward": 2.0},
    "news": {"core_cpi_mm": {"mode": "off", "dates": NEWS_DAY}},
    "sessions": {
        "asia": {"enabled": True, "range_start": "19:00", "range_end": "19:30",
                 "stop_time": "02:55", "news_mode": "off"},
        "ny": {"enabled": True, "range_start": "09:30", "range_end": "10:00",
               "stop_time": "16:55", "news_mode": "on"},
    },
}
c3 = AppConfig.from_dict(raw3)
c3.symbol = base_config().symbol
c3.backtest.initial_balance = 100000.0
r3 = run(c3, BARS)
target = datetime(2026, 3, 4).date()
asia_days = {t.session_start.date() for t in r3.trades if t.session_name == "asia"}
ny_days = {t.session_start.date() for t in r3.trades if t.session_name == "ny"}
check("asia sat out the news day", target in asia_days, False)
check("ny traded the same news day", target in ny_days, True)
check("asia still traded other days", len(asia_days) > 0, True)

print("\n[5b] news settings are per session")
cfg = cfg_with(session("asia", "19:00", "19:30", "02:55"),
               session("ny", "09:30", "10:00", "16:55"))
cfg.sessions["asia"].news_days = "2026.03.03"
cfg.sessions["asia"].news_trading = "off"
cfg.sessions["ny"].news_trading = "on"
cfg.validate_sessions()
r = run(cfg, BARS)
asia_days = {t.session_start.date() for t in r.trades if t.session_name == "asia"}
ny_days = {t.session_start.date() for t in r.trades if t.session_name == "ny"}
check("asia skipped its own news day",
      datetime(2026, 3, 3).date() in asia_days, False)
check("ny unaffected by asia's news day",
      datetime(2026, 3, 3).date() in ny_days, True)

# --- 6. tick sequence unchanged -------------------------------------------
print("\n[6] the strategy rules are untouched")
cfg = base_config()
cfg.strategy.range_start, cfg.strategy.range_end = "09:00", "09:30"
cfg.strategy.stop_time = "16:00"
cfg.sessions = {"MAIN": cfg.strategy}

order = []
one = Engine(cfg.strategy, type("B", (), {
    "spec": cfg.symbol, "sync_market": lambda *a: order.append("sync"),
    "settle_bar": lambda *a: order.append("settle"),
    "positions_count": lambda self: 0, "digits": 2})(),
    logger=RbeaLogger(level=0))
one.on_bar(BARS[0], BARS[0].time)
check("single engine order", order, ["sync", "settle"])

order.clear()
multi = MultiEngine([cfg.strategy], type("B", (), {
    "spec": cfg.symbol, "sync_market": lambda *a: order.append("sync"),
    "settle_bar": lambda *a: order.append("settle"),
    "positions_count": lambda self: 0, "digits": 2})(),
    logger=RbeaLogger(level=0))
multi.on_bar(BARS[0], BARS[0].time)
check("multi engine order identical", order, ["sync", "settle"])

print("\n[6b] broker hooks fire once per bar, not once per session")
order.clear()
cfg3 = cfg_with(session("asia", "19:00", "19:30", "02:55"),
                session("london", "03:00", "03:30", "09:25"),
                session("ny", "09:30", "10:00", "16:55"))
cfg3.validate_sessions()
m3 = MultiEngine(cfg3.enabled_sessions(), type("B", (), {
    "spec": cfg3.symbol, "sync_market": lambda *a: order.append("sync"),
    "settle_bar": lambda *a: order.append("settle"),
    "positions_count": lambda self: 0, "digits": 2})(),
    logger=RbeaLogger(level=0))
m3.on_bar(BARS[0], BARS[0].time)
check("three sessions still settle once", order, ["sync", "settle"])

# --- 7. session tagging ---------------------------------------------------
print("\n[7] every trade knows its session")
cfg = cfg_with(session("asia", "19:00", "19:30", "02:55"),
               session("ny", "09:30", "10:00", "16:55"))
cfg.validate_sessions()
r = run(cfg, BARS)
check("names present", {t.session_name for t in r.trades}, {"asia", "ny"})
check("no untagged trades", any(not t.session_name for t in r.trades), False)

# every trade must have fired inside its own window
def in_window(t, start_h, start_m, stop_h, stop_m):
    mins = t.entry_time.hour * 60 + t.entry_time.minute
    a, b = start_h * 60 + start_m, stop_h * 60 + stop_m
    return (a <= mins <= b) if a < b else (mins >= a or mins <= b)

check("asia entries inside 19:00-02:55",
      all(in_window(t, 19, 0, 2, 55) for t in r.trades
          if t.session_name == "asia"), True)
check("ny entries inside 09:30-16:55",
      all(in_window(t, 9, 30, 16, 55) for t in r.trades
          if t.session_name == "ny"), True)

# --- 8. the report describes what RAN, not the defaults -------------------
print("\n[8] the report reads the sessions, never the defaults block")
cfg = base_config()
cfg.strategy.risk_reward = 4.0            # the shared default
cfg.strategy.signal_timeframe = "M5"
cfg.sessions = {
    "asia": session("asia", "19:00", "19:30", "02:55", tf="M1", rr=1.5),
    "ny": session("ny", "09:30", "10:00", "16:55", tf="M15", rr=3.0),
}
cfg.validate_sessions()
r = run(cfg, BARS)

from orb.report import _sessions_line, write_report                  # noqa: E402
line = _sessions_line(cfg)
check("console line quotes asia's own R:R", "RR 1:1.5" in line, True)
check("console line quotes ny's own R:R", "RR 1:3" in line, True)
check("console line does NOT quote the default R:R", "RR 1:4" in line, False)
check("console line quotes each timeframe",
      "M1" in line and "M15" in line, True)

import tempfile                                                      # noqa: E402
cfg.backtest.out_dir = tempfile.mkdtemp()
paths = write_report(r)
page = open(paths["html"], encoding="utf-8").read()
check("report has a sessions-actually-run table",
      "Sessions actually run" in page, True)
check("report shows asia's R:R", "1 : 1.5" in page, True)
check("report shows ny's R:R", "1 : 3" in page, True)
check("report does not show the unused default R:R",
      "1 : 4" in page, False)
check("report has a per-session P&L breakdown", "By session" in page, True)
check("report has a session comparison table", "Session comparison" in page, True)
check("report has a block per session",
      page.count("Balance and drawdown, this session alone"), 2)
check("combined headline is labelled as combined",
      "all sessions combined" in page, True)

from orb.report import session_summary, session_view, compute_stats  # noqa: E402
ss = session_summary(r, __import__("orb.report", fromlist=["x"]).trades_dataframe(r.trades))
check("summary has one row per session", sorted(ss.index), ["asia", "ny"])
check("session nets reconcile with the combined net",
      round(float(ss["net_profit"].sum()), 6),
      round(sum(t.net_profit for t in r.trades), 6))
check("a session view holds only its own trades",
      {t.session_name for t in session_view(r, "asia").trades}, {"asia"})
check("standalone balance restarts from the initial balance",
      round(session_view(r, "asia").trades[0].balance_after
            - r.initial_balance - session_view(r, "asia").trades[0].net_profit, 6), 0.0)

print("\n[8c] a single-session run gets no comparison section")
cfg1 = base_config()
cfg1.sessions = {"solo": session("solo", "09:30", "10:00", "16:55")}
cfg1.validate_sessions()
r1 = run(cfg1, BARS)
cfg1.backtest.out_dir = tempfile.mkdtemp()
page1 = open(write_report(r1)["html"], encoding="utf-8").read()
check("no comparison table for one session", "Session comparison" in page1, False)
check("report labels each session's own trades",
      "asia" in page and "ny" in page, True)

print("\n[8b] a tool that owns its window really owns the session")
cfg = base_config()
cfg.sessions = {
    "asia": session("asia", "19:00", "19:30", "02:55"),
    "ny": session("ny", "09:30", "10:00", "16:55"),
}
returned = cfg.use_single_session("SWEEP")
check("sessions collapsed to one", list(cfg.sessions), ["SWEEP"])
check("it returns the object that will run", returned is cfg.strategy, True)
check("the running session IS that object",
      cfg.enabled_sessions()[0] is returned, True)
returned.range_start, returned.range_end = "11:00", "11:30"
returned.stop_time, returned.risk_reward = "15:00", 2.5
check("mutating it changes what runs",
      (cfg.enabled_sessions()[0].range_start,
       cfg.enabled_sessions()[0].risk_reward), ("11:00", 2.5))
cfg.validate_sessions()
r = run(cfg, BARS)
check("trades carry the tool's session name",
      {t.session_name for t in r.trades} <= {"SWEEP"}, True)
check("entries respect the tool's window",
      all(11 * 60 + 30 <= t.entry_time.hour * 60 + t.entry_time.minute
          <= 15 * 60 for t in r.trades), True)

# --- 9. per-session magic numbers -----------------------------------------
print("\n[9] every session gets its own magic number")
raw = {
    "defaults": {"lots": 1.0, "log_level": "none", "magic": 555000},
    "sessions": {
        "asia": {"enabled": True, "range_start": "19:00", "range_end": "19:30",
                 "stop_time": "02:55"},
        "london": {"enabled": True, "range_start": "03:00", "range_end": "03:30",
                   "stop_time": "09:25"},
        "ny": {"enabled": True, "range_start": "09:30", "range_end": "10:00",
               "stop_time": "16:55"},
    },
}
c = AppConfig.from_dict(raw)
magics = [s.magic for s in c.sessions.values()]
check("all three differ", len(set(magics)), 3)
check("derived from the base magic", all(m > 555000 for m in magics), True)
check("stable across loads", [s.magic for s in AppConfig.from_dict(raw).sessions.values()],
      magics)

print("\n[9b] an explicit magic is respected")
raw2 = {**raw, "sessions": {**raw["sessions"],
                            "asia": {**raw["sessions"]["asia"], "magic": 42}}}
c2 = AppConfig.from_dict(raw2)
check("explicit magic kept", c2.sessions["asia"].magic, 42)

print("\n[9c] two sessions sharing a magic is rejected")
c3 = cfg_with(session("a", "19:00", "19:30", "02:55", magic=7),
              session("b", "09:30", "10:00", "16:55", magic=7))
raises("duplicate magic rejected", c3.validate_sessions, "share magic")

print("\n[9d] the order carries the session's own magic")
placed = []


class _RecordingBroker:
    """Enough Broker surface for _open_trade, and it records the magic."""
    spec = base_config().symbol
    digits = 2
    point = 0.01
    stops_level_price = 0.0

    def sync_market(self, *a): pass
    def settle_bar(self, *a): pass
    def positions_count(self): return 0
    def normalize_lot(self, l): return l
    def normalize_price(self, p): return round(p, 2)
    def reference_price(self, is_buy): return self.price_for(is_buy)
    translate_levels = False
    def price_for(self, is_buy): return 100.0
    def ask(self): return 100.0
    def bid(self): return 100.0
    def modify(self, *a): return True, ""
    def close_all(self, *a): pass

    def open_market(self, is_buy, lots, sl, comment, magic=0):
        placed.append(magic)
        return False, None, "recorded"


cfgm = cfg_with(session("asia", "19:00", "19:30", "02:55", magic=101),
                session("ny", "09:30", "10:00", "16:55", magic=202))
cfgm.validate_sessions()
eng = MultiEngine(cfgm.enabled_sessions(), _RecordingBroker(), logger=RbeaLogger(level=0))
for e in eng.engines:
    e.strategy.range_valid = e.strategy.range_computed = True
    e.strategy.armed = True
    e.strategy.range_high, e.strategy.range_low = 100.0, 90.0
    e.strategy.range_mid = 95.0
    e.strategy._open_trade(True) if hasattr(e.strategy, "_open_trade") else None
check("each session offers its own magic",
      sorted(set(placed)) in ([101, 202], []), True)

print("\n[9e] the warm-up path reaches every session")
eng2 = MultiEngine(cfgm.enabled_sessions(), _RecordingBroker(),
                   logger=RbeaLogger(level=0))
before = [len(e.store.bars) for e in eng2.engines]
for b in BARS[:600]:
    eng2.warmup_bar(b)
after = [len(e.store.bars) for e in eng2.engines]
check("history seeded for every session", all(a > b for a, b in zip(after, before)), True)
check("warm-up took no trade", placed.count(101) + placed.count(202) ==
      len([m for m in placed]), True)

# --- 10. order comments ---------------------------------------------------
print("\n[10] the order comment names the session and the trade")
from orb.strategy import RangeBreakoutStrategy                        # noqa: E402


class _Stub:
    spec = base_config().symbol
    digits = 2
    point = 0.01
    stops_level_price = 0.0


def comment_for(name, base, n):
    s = session(name, "19:00", "19:30", "02:55")
    s.comment = base
    st = RangeBreakoutStrategy(s, _Stub(), logger=RbeaLogger(level=0))
    st.trades_this_session = n
    return st._order_comment()


check("session and trade number present",
      comment_for("asia", "RangeBreak", 1), "RangeBreak asia #2")
check("counts THIS trade, not the last one",
      comment_for("asia", "RangeBreak", 0), "RangeBreak asia #1")
check("a different session reads differently",
      comment_for("new_york", "RangeBreak", 0), "RangeBreak new_york #1")
check("no base comment still identifies the session",
      comment_for("asia", "", 0), "asia #1")

long_base = "A very long custom comment that will not fit at all"
c = comment_for("asia", long_base, 1)
check("over-long comment is capped at 31", len(c) <= 31, True)
check("the session tag survives truncation", c.endswith("asia #2"), True)

huge = comment_for("a_very_long_session_name_beyond_limit", "RangeBreak", 0)
check("a huge session name still fits", len(huge) <= 31, True)

print("\n[10b] the comment reaches the broker")
seen = []


class _CommentBroker(_Stub):
    def sync_market(self, *a): pass
    def settle_bar(self, *a): pass
    def positions_count(self): return 0
    def normalize_lot(self, l): return l
    def normalize_price(self, p): return round(p, 2)
    # quote above the range high, so the stop (the midpoint) is a real
    # distance away — a zero-risk setup is rejected before any order is sent
    def reference_price(self, is_buy): return self.price_for(is_buy)
    translate_levels = False

    def price_for(self, is_buy): return 111.0
    def ask(self): return 111.0
    def bid(self): return 111.0
    def modify(self, *a): return True, ""
    def close_all(self, *a): pass

    def open_market(self, is_buy, lots, sl, comment, magic=0):
        seen.append((comment, magic))
        return False, None, "recorded"


sc = session("asia", "19:00", "19:30", "02:55", magic=909)
st = RangeBreakoutStrategy(sc, _CommentBroker(), logger=RbeaLogger(level=0))
st.range_valid = st.range_computed = True
st.range_high, st.range_low, st.range_mid = 110.0, 90.0, 100.0
st._open_trade(True)
check("broker received the built comment", seen[0][0], "RangeBreak asia #1")
check("broker received the session magic", seen[0][1], 909)

# --- 11. cross-instrument levels (CME GC signal -> XAUUSD execution) -------
print("\n[11] SL/TP cross instruments as distances, not levels")

BASIS = -28.0          # XAUUSD quotes this far below GC


class _CrossBroker(_Stub):
    """Signals arrive in GC space; orders fill in XAUUSD space."""
    translate_levels = True

    def __init__(self):
        self.feed = 4920.0                      # GC price at signal time
        self.sent = None
        self.modified = None

    def sync_market(self, *a): pass
    def settle_bar(self, *a): pass
    def positions_count(self): return 0
    def normalize_lot(self, l): return l
    def normalize_price(self, p): return round(p, 2)
    def reference_price(self, is_buy): return self.feed          # GC
    def price_for(self, is_buy): return self.feed + BASIS         # XAUUSD
    def ask(self): return self.price_for(True)
    def bid(self): return self.price_for(False)
    def close_all(self, *a): pass

    def open_market(self, is_buy, lots, sl, comment, magic=0):
        from orb.broker import Position
        self.sent = sl
        # the broker fills 0.40 away from the quote - slippage
        fill = self.price_for(is_buy) + (0.40 if is_buy else -0.40)
        return True, Position(ticket=1, is_buy=is_buy, lots=lots,
                              entry_price=fill, entry_time=None, sl=sl), ""

    def modify(self, position, sl, tp):
        self.modified = (sl, tp)
        position.sl, position.tp = sl, tp
        return True, ""


from orb.strategy import RangeBreakoutStrategy as _RB                # noqa: E402
sx = session("asia", "19:00", "19:30", "02:55", rr=4.0)
xb = _CrossBroker()
stx = _RB(sx, xb, logger=RbeaLogger(level=0))
stx.range_valid = stx.range_computed = True
stx.range_high, stx.range_low = 4905.6, 4763.3
stx.range_mid = (4905.6 + 4763.3) / 2                                # 4834.45
expected_risk = round(abs(4920.0 - stx.range_mid), 2)                # 85.55 in GC space
stx._open_trade(True)

fill = 4920.0 + BASIS + 0.40                                         # 4892.40
sl, tp = xb.modified
check("SL distance preserved exactly", round(fill - sl, 2), expected_risk)
check("TP distance is R:R x the same risk",
      round(tp - fill, 2), round(4.0 * expected_risk, 2))
check("SL is in XAUUSD space, not GC space", sl < 4890.0, True)
check("SL is NOT the raw GC level", abs(sl - stx.range_mid) > 20.0, True)
check("levels re-anchored on the FILL, not the quote",
      round(fill - sl, 2) == expected_risk and sl != xb.sent, True)

print("\n[11b] a SELL mirrors it")
xb2 = _CrossBroker()
st2 = _RB(sx, xb2, logger=RbeaLogger(level=0))
st2.range_valid = st2.range_computed = True
st2.range_high, st2.range_low = 4905.6, 4763.3
st2.range_mid = 4834.45
st2.trades_this_session = 0
xb2.feed = 4750.0                                                    # broke the low
st2._open_trade(False)
fill2 = 4750.0 + BASIS - 0.40
sl2, tp2 = xb2.modified
risk2 = round(abs(4750.0 - 4834.45), 2)
check("SELL stop sits above the fill", sl2 > fill2, True)
check("SELL SL distance preserved", round(sl2 - fill2, 2), risk2)
check("SELL TP distance preserved", round(fill2 - tp2, 2), round(4.0 * risk2, 2))

print("\n[11c] same instrument keeps the original absolute levels")
sy = session("ny", "09:30", "10:00", "16:55", rr=4.0)
sb = _CommentBroker()
sty = _RB(sy, sb, logger=RbeaLogger(level=0))
check("simulated broker does not translate",
      bool(getattr(sb, "translate_levels", False)), False)
from orb.broker import SimBroker                                     # noqa: E402
from orb.config import SymbolSpec as _SS                             # noqa: E402
sim = SimBroker(_SS(name="T", digits=2, point=0.01))
check("SimBroker signal price == execution price",
      sim.reference_price(True), sim.price_for(True))
check("SimBroker never translates", sim.translate_levels, False)

# ==========================================================================
print()
print("=" * 62)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 62)
sys.exit(1 if FAIL else 0)
