"""Proof that backtest and live share one source of truth.

Two independent checks:

  A. STATIC — neither runner is allowed to sequence the EA's tick itself.
     Only `orb/engine.py` may call ingest_bar / on_time / on_bar_closed /
     Resampler.push. If someone re-implements the loop in a runner later,
     this fails.

  B. BEHAVIOURAL — the same bars are pushed through the LIVE code path
     (LiveTrader.run, with a replay feed and a simulated broker) and through
     the BACKTEST code path, and every resulting trade must match exactly.

Run:  python -m tests.test_single_source
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

from orb.backtest import run_backtest                      # noqa: E402
from orb.bars import Bar                                   # noqa: E402
from orb.broker import SimBroker                           # noqa: E402
from orb.config import AppConfig                           # noqa: E402
from orb.live_trader import LiveTrader                     # noqa: E402
from orb.logger import RbeaLogger                          # noqa: E402

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")


# ==========================================================================
print("\n--- A. static: only the engine sequences the tick -------------------")

SEQUENCING_CALLS = ["ingest_bar", "on_time", "on_bar_closed", "resampler.push"]

# the warm-up path must exist and must be the engine's
src_live = open("orb/live_trader.py", encoding="utf-8").read()
check("live_trader warms up through the engine",
      "self.engine.warmup_bar(" in src_live, True)
check("live_trader does not reach into a resampler",
      "resampler" in src_live, False)

for runner in ("orb/backtest.py", "orb/live_trader.py"):
    src = open(runner, encoding="utf-8").read()
    # strip comments and docstrings so prose mentioning the names does not count
    src = re.sub(r'""".*?"""', "", src, flags=re.S)
    src = re.sub(r"#.*", "", src)
    for call in SEQUENCING_CALLS:
        pattern = re.escape(call) + r"\s*\("
        hits = len(re.findall(pattern, src))
        # No exceptions any more: warm-up used to reach into the resampler
        # from live_trader, which broke the moment MultiEngine arrived (it has
        # no .resampler). It now goes through Engine.warmup_bar, so neither
        # runner touches the sequencing calls at all.
        allowed = 0
        check(f"{runner} calls {call}() {allowed}x", hits, allowed)

engine_src = open("orb/engine.py", encoding="utf-8").read()
for call in SEQUENCING_CALLS:
    check(f"orb/engine.py owns {call}()",
          len(re.findall(re.escape(call) + r"\s*\(", engine_src)) >= 1, True)


# ==========================================================================
print("\n--- B. behavioural: live path == backtest path ----------------------")


def base_cfg(**over):
    raw = {
        "server_utc_offset_hours": 0,
        "strategy": {
            "range_start": "09:00", "range_end": "10:00", "stop_time": "17:00",
            "signal_timeframe": "M5", "sl_mode": "mid_range", "risk_reward": 2.0,
            "lots": 1.0, "require_range_reentry": True,
            "max_trades_per_session": 0, "close_at_stop_time": True,
            "log_level": "none",
        },
        "symbol": {
            "name": "TEST", "digits": 2, "point": 0.01, "tick_size": 0.01,
            "stops_level_points": 0, "volume_min": 1.0, "volume_max": 100.0,
            "volume_step": 1.0, "value_per_price_unit": 1.0, "currency": "USD",
        },
        "backtest": {"initial_balance": 10000.0, "dbn_paths": []},
    }
    raw["strategy"].update(over)
    return AppConfig.from_dict(raw)


def synth(days=12, seed=3):
    """Noisy bars that produce a mix of TP, SL, stop-time and no-trade days."""
    import random
    rnd = random.Random(seed)
    bars, price = [], 100.0
    for k in range(days):
        day = datetime(2025, 3, 3) + timedelta(days=k)
        if day.weekday() >= 5:
            continue
        t = day.replace(hour=8)
        end = day.replace(hour=18)
        while t < end:
            price = max(10.0, price + rnd.gauss(0, 0.06))
            o = price
            c = price + rnd.gauss(0, 0.03)
            h = max(o, c) + abs(rnd.gauss(0, 0.04))
            l = min(o, c) - abs(rnd.gauss(0, 0.04))
            q = lambda x: round(x, 2)                      # noqa: E731
            bars.append(Bar(t, q(o), q(h), q(l), q(c), 1.0))
            price = c
            t += timedelta(minutes=1)
    return bars


class ReplayFeed:
    """Stands in for DatabentoLiveFeed: hands the same bars to the live loop."""

    def __init__(self, bars):
        self._bars = list(bars)
        self._i = 0
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def poll(self, timeout=1.0):
        if self._i >= len(self._bars):
            return None
        bar = self._bars[self._i]
        self._i += 1
        return bar

    def stop(self):
        self.stopped = True


bars = synth()
print(f"  ({len(bars)} synthetic base bars)")

# --- backtest path ---
cfg_bt = base_cfg()
res = run_backtest(cfg_bt, bars, RbeaLogger(level=0))

# --- live path: same bars, same engine, simulated broker + replay feed ---
cfg_lv = base_cfg()
sim = SimBroker(cfg_lv.symbol, initial_balance=10000.0)
feed = ReplayFeed(bars)
trader = LiveTrader(cfg_lv, logger=RbeaLogger(level=0), broker=sim, feed=feed)
sim.on_exit = lambda t: trader.strategy.report_exit(
    t.ticket, t.exit_reason, t.exit_price, t.net_profit, "USD")

# the live loop's clock: bar time when a bar arrives, last known time when idle
_state = {"t": bars[0].time}


def now_fn(bar=None):
    if bar is not None:
        _state["t"] = bar.time
    return _state["t"]


trader.run(poll_seconds=0, max_polls=len(bars) + 1, now_fn=now_fn)
trader.engine.flush()
if sim.position is not None:
    sim.set_market(bars[-1].close, bars[-1].time)
    sim.close_all("end of backtest")

check("feed was started", feed.started, True)
check("feed was stopped", feed.stopped, True)
check("some trades were generated", len(res.trades) > 3, True)
check("trade count identical", len(sim.trades), len(res.trades))

fields = ("direction", "lots", "entry_time", "entry_price", "sl", "tp",
          "exit_time", "exit_price", "exit_reason", "net_profit",
          "range_high", "range_low", "range_mid", "trade_no_in_session")
mismatch = None
for i, (a, b) in enumerate(zip(res.trades, sim.trades)):
    for f in fields:
        if getattr(a, f) != getattr(b, f):
            mismatch = f"trade {i} field '{f}': backtest {getattr(a, f)!r} "\
                       f"vs live {getattr(b, f)!r}"
            break
    if mismatch:
        break
check("every trade field identical", mismatch, None)
check("final balance identical", round(sim.balance, 6),
      round(res.final_balance, 6))


# ==========================================================================
print("\n" + "=" * 62)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 62)
sys.exit(1 if FAIL else 0)
