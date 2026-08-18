#!/usr/bin/env python3
"""Prove two data sources produce identical backtests.

Use after merging files, converting formats, re-downloading history, or
anything else that touches the data — before you trust the new source.

    python tools/verify_sources.py \
        --a "data/gc_ohlcv1m_parent_*.dbn.zst" \
        --b "data/gc_1m_merged.parquet"

It runs a matrix of deliberately different configurations through both sources
and compares **every field of every trade**, not just the totals. Two files can
easily agree on net profit while disagreeing on which trades produced it, so
totals alone are not evidence.

Exit code is 0 only if every case matches.

What this does NOT prove
------------------------
It only exercises the bars the strategy actually reads. A difference in a
back-month contract, or in a bar that is not the high or low of its range
window, cannot change any trade and so cannot be detected here — verified by
experiment: corrupting a mid-window bar by 5.00 left all seven cases identical,
while corrupting the bar that *set* the range high flipped six of them to
MISMATCH immediately.

That is a property of the strategy, not a weakness in the merge. For row-level
completeness across every contract, use the checks built into
`tools/merge_data.py`, which compare every (timestamp, symbol) key and every
OHLCV value. The two tools together cover it: merge_data proves the data is
identical, verify_sources proves the results are.
"""
from __future__ import annotations

import argparse
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                          # noqa: E402

from orb.backtest import make_clock, run_backtest            # noqa: E402
from orb.config import AppConfig                             # noqa: E402
from orb.data.dbn import load_dbn_bars                       # noqa: E402
from orb.logger import RbeaLogger                            # noqa: E402

#: the default configuration — the orb engine's own
#: master config. There is no parent config file any more.
ENGINE_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "orb", "engines", "orb", "config.yaml")

# (name, strategy overrides, databento overrides, data clamps)
CASES = [
    ("default config",              {}, {}, {}),
    ("evening session 20:00",
     dict(range_start="20:00", range_end="20:30", stop_time="03:30"), {}, {}),
    ("M15 bars, RR3, full-range SL",
     dict(signal_timeframe="M15", risk_reward=3.0, sl_mode="full_range"), {}, {}),
    ("M1 bars, no re-entry, max 1",
     dict(signal_timeframe="M1", require_range_reentry=False,
          max_trades_per_session=1), {}, {}),
    ("news days off, no close at stop",
     dict(news_days="2025.12.25,2024.07.04", news_trading="off",
          close_at_stop_time=False), {}, {}),
    ("fixed contract", {}, dict(contract_mode="symbol"), {}),
    ("date-clamped window", {}, {}, dict(start="2024-03-01", end="2025-09-30")),
]


def main() -> int:
    p = argparse.ArgumentParser(description="Compare two data sources")
    p.add_argument("--a", required=True, help="source A (file, glob or directory)")
    p.add_argument("--b", required=True, help="source B")
    p.add_argument("--config", "-c", default=ENGINE_CONFIG)
    p.add_argument("--contract", default=None,
                   help="contract for the fixed-contract case, e.g. GCZ5")
    a = p.parse_args()

    base = AppConfig.load(a.config)
    log = RbeaLogger(level=0)

    print(f"A: {a.a}\nB: {a.b}\n")
    print(f"{'case':<32} {'trades':>8} {'net A':>12} {'net B':>12}   result")
    print("-" * 82)

    all_ok, ran = True, 0
    for name, strat, dbo, clamp in CASES:
        results = {}
        for tag, src in (("A", a.a), ("B", a.b)):
            cfg = copy.deepcopy(base)
            # each CASE defines its own window; own the session explicitly
            cfg.use_single_session(name)
            for k, v in strat.items():
                setattr(cfg.strategy, k, v)
            for k, v in dbo.items():
                setattr(cfg.databento, k, v)
            if dbo.get("contract_mode") == "symbol":
                cfg.databento.contract_symbol = a.contract or cfg.databento.contract_symbol
                if not cfg.databento.contract_symbol:
                    results = None
                    break
            cfg.strategy.log_level = "none"
            cfg.validate_sessions()
            bars = load_dbn_bars(src, make_clock(cfg),
                                 contract_mode=cfg.databento.contract_mode,
                                 contract_symbol=cfg.databento.contract_symbol,
                                 include_spreads=cfg.databento.include_spreads,
                                 roll_min_volume=cfg.databento.roll_min_volume,
                                 roll_boundary_hour=cfg.databento.roll_boundary_hour,
                                 logger=log, **clamp)
            r = run_backtest(cfg, bars, RbeaLogger(level=0))
            results[tag] = (pd.DataFrame([t.__dict__ for t in r.trades]),
                            r.final_balance)
        if results is None:
            print(f"{name:<32} {'-':>8} {'-':>12} {'-':>12}   skipped "
                  f"(pass --contract)")
            continue

        (da, ba), (dbf, bb) = results["A"], results["B"]
        same = da.equals(dbf) and abs(ba - bb) < 1e-9
        # a case with no trades proves nothing — say so rather than count it
        if len(da) == 0 and len(dbf) == 0:
            verdict = "both empty (no evidence)"
        else:
            ran += 1
            verdict = "identical" if same else "MISMATCH"
            all_ok = all_ok and same
        print(f"{name:<32} {len(da):>8,} {ba-100000:>12,.0f} {bb-100000:>12,.0f}"
              f"   {verdict}")

    print("-" * 82)
    if ran == 0:
        print("No case produced any trades — this run proves nothing. Check the "
              "config and the date range.")
        return 1
    print(f"{ran} configuration(s) with trades compared field by field: "
          + ("ALL IDENTICAL" if all_ok else "MISMATCH FOUND — do not swap sources"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
