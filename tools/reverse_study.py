#!/usr/bin/env python3
"""Does taking the OPPOSITE side of a losing configuration make money?

    python tools/reverse_study.py --worst 10 --rr 1,1.5,2,2.5 \
        --data data/gc_1m_merged.parquet --out backtest/orb_reverse/study

Variants tested for each configuration:

    ORIGINAL   the configuration exactly as the matrix ran it
    R          reverse trade #1, then stop trading that session
    RR         reverse trades #1 and #2, then stop
    RRR        reverse trades #1, #2 and #3, then stop

Why this RE-RUNS the backtest instead of reading the trade files
---------------------------------------------------------------
It is tempting to flip the sign of each trade's P&L in the CSV and call that
the reversed result. That answer would be wrong, and wrong in a direction that
flatters the reversal.

A losing trade in the file exited at its STOP LOSS: price moved 1R against it.
The reversed trade would have been 1R in profit at that moment — but its own
take profit sits 2R (or 4R) away, and whether price kept going far enough to
reach it, or turned around and hit the reversed stop first, is simply not in
the trade record. Flipping the sign silently assumes every reversed trade runs
all the way to target, which converts every -1R loss into a +2R win.

The only honest way to answer is to walk the bars again with the direction
flipped, letting the stop and the target compete for the reversed trade exactly
as they did for the original. That is what this tool does: same engine, same
data, same rules — only the direction of entry and the trades-per-session cap
change.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                          # noqa: E402

from orb.backtest import make_clock, run_backtest            # noqa: E402
from orb.config import AppConfig                             # noqa: E402
from orb.data.dbn import load_dbn_bars                       # noqa: E402
from orb.logger import RbeaLogger                            # noqa: E402
from orb.report import compute_stats                         # noqa: E402
from orb.engines.orb_reverse import mirror_settings_for      # noqa: E402

#: the default configuration — the orb engine's own
#: master config. There is no parent config file any more.
ENGINE_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "orb", "engines", "orb", "config.yaml")


# --------------------------------------------------------------------------
def load_matrix_builder():
    """Reuse run_matrix's own config builder so the configurations under test
    are byte-identical to the ones in the sweep."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_matrix.py")
    spec = importlib.util.spec_from_file_location("run_matrix", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def metrics(result, label: str, config: str) -> dict:
    s = compute_stats(result)
    pf = s["profit_factor"]
    return {
        "config": config,
        "variant": label,
        "trades": s["total_trades"],
        "win_rate": round(s["win_rate"], 2),
        "net_profit": round(s["net_profit"], 2),
        "profit_factor": (None if pf in (float("inf"),) else round(pf, 3)),
        "max_dd_money": round(s["max_dd_money"], 2),
        "max_dd_pct": round(s["max_dd_pct"], 2),
        "expectancy": round(s["expectancy"], 2),
        "avg_r": round(s["avg_r"], 3),
    }


def run_variant(cfg: AppConfig, bars, reverse: bool, max_trades: int):
    run_cfg = copy.deepcopy(cfg)
    for s in run_cfg.sessions.values():
        s.max_trades_per_session = max_trades
        s.log_level = "none"
    run_cfg.strategy.log_level = "none"
    if reverse:
        mirror_settings_for(run_cfg, max_trades).apply_to(run_cfg)
    return run_backtest(run_cfg, bars, RbeaLogger(level=0))


# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(
        description="Reverse the first N trades of a session and compare")
    p.add_argument("--config", "-c", default=ENGINE_CONFIG)
    p.add_argument("--data", "-d", nargs="+", default=None)
    p.add_argument("--summary", default=os.path.join("backtest", "orb", "matrix", "_summary", "all_results.csv"),
                   help="matrix summary used to pick the worst configurations")
    p.add_argument("--worst", type=int, default=10,
                   help="how many of the worst configurations to test")
    p.add_argument("--rr", default="1,1.5,2,2.5",
                   help="restrict the pool to these risk:reward values")
    p.add_argument("--only", default=None,
                   help="test exactly this configuration instead of the worst N")
    p.add_argument("--match", default=None,
                   help="test EVERY configuration whose name contains this, e.g. "
                        "LONDON. Ignores --worst and --rr.")
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="2026-08-13")
    p.add_argument("--out", default=os.path.join("backtest", "orb_reverse", "study"))
    a = p.parse_args()

    base = AppConfig.load(a.config)
    if a.data:
        base.backtest.dbn_paths = a.data

    # --- which configurations -------------------------------------------
    if a.only:
        names = [a.only]
    elif a.match:
        summ = pd.read_csv(a.summary)
        names = sorted(summ[summ.config.str.contains(a.match, case=False)]["config"])
        if not names:
            print(f"No configuration name contains '{a.match}'", file=sys.stderr)
            return 2
        print(f"Matched {len(names)} configuration(s) containing '{a.match}'")
    else:
        summ = pd.read_csv(a.summary)
        rrs = [float(x) for x in a.rr.replace(" ", "").split(",") if x]
        pool = summ[summ.risk_reward.isin(rrs)]
        if pool.empty:
            print(f"No configurations with R:R in {rrs}", file=sys.stderr)
            return 2
        names = pool.nsmallest(a.worst, "net_profit")["config"].tolist()
        print(f"Pool: {len(pool)} configurations at R:R {rrs}")
    print(f"Testing {len(names)} configuration(s)"
          + (":" if len(names) <= 15 else f" x 4 variants = {len(names)*4} runs"))
    if len(names) <= 15:
        for n in names:
            print(f"    {n}")

    # --- bars, loaded once ------------------------------------------------
    d = base.databento
    print("\nLoading bars ...")
    bars = load_dbn_bars(base.backtest.dbn_paths, make_clock(base),
                         contract_mode=d.contract_mode,
                         contract_symbol=d.contract_symbol,
                         include_spreads=d.include_spreads,
                         roll_min_volume=d.roll_min_volume,
                         roll_boundary_hour=d.roll_boundary_hour,
                         start=a.start, end=a.end, logger=RbeaLogger(level=0))
    print(f"  {len(bars):,} bars  {bars[0].time:%Y-%m-%d %H:%M} .. "
          f"{bars[-1].time:%Y-%m-%d %H:%M}")

    # --- rebuild the matrix configurations --------------------------------
    rm = load_matrix_builder()
    # build the WHOLE grid: run_matrix only appends the _RR suffix to a name
    # when it is sweeping more than one value, so asking for a single R:R would
    # produce short names that never match what the summary calls them
    built = {item["name"]: item["cfg"]
             for item in rm.build_configs(base, "", rm.RR_VALUES)}
    missing = [n for n in names if n not in built]
    if missing:
        print(f"Could not rebuild: {missing}", file=sys.stderr)
        return 2

    VARIANTS = [("ORIGINAL", False, 0), ("R", True, 1),
                ("RR", True, 2), ("RRR", True, 3)]

    rows = []
    for i, name in enumerate(names, 1):
        cfg = built[name]
        quiet = len(names) > 15
        if not quiet:
            print(f"\n[{i}/{len(names)}] {name}")
        for label, reverse, mx in VARIANTS:
            res = run_variant(cfg, bars, reverse, mx)
            m = metrics(res, label, name)
            rows.append(m)
            pf = "inf" if m["profit_factor"] is None else f"{m['profit_factor']:.2f}"
            if not quiet:
                print(f"      {label:<9} trades {m['trades']:>5}  net "
                      f"{m['net_profit']:>11,.0f}  win {m['win_rate']:>5.1f}%  "
                      f"PF {pf:>5}  maxDD {m['max_dd_pct']:>5.1f}%")
        if quiet:
            best = max((r for r in rows if r["config"] == name),
                       key=lambda r: r["net_profit"])
            print(f"[{i:>3}/{len(names)}] {name:<38} best {best['variant']:<8} "
                  f"net {best['net_profit']:>10,.0f}  maxDD {best['max_dd_pct']:>5.1f}%")

    os.makedirs(a.out, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(a.out, "reversal_comparison.csv"), index=False)

    # a wide view: one row per configuration, one column block per variant
    wide = df.pivot(index="config", columns="variant",
                    values=["net_profit", "trades", "win_rate",
                            "profit_factor", "max_dd_pct"])
    wide.to_csv(os.path.join(a.out, "reversal_comparison_wide.csv"))

    order = [v[0] for v in VARIANTS]
    tot = (df.groupby("variant")
             .agg(configs=("config", "size"), trades=("trades", "sum"),
                  net_profit=("net_profit", "sum"),
                  profitable=("net_profit", lambda x: int((x > 0).sum())),
                  median_dd=("max_dd_pct", "median"))
             .reindex(order))
    tot.to_csv(os.path.join(a.out, "reversal_totals.csv"))

    print("\n" + "=" * 74)
    print("  TOTALS ACROSS ALL TESTED CONFIGURATIONS")
    print("=" * 74)
    print(tot.round(2).to_string())

    json.dump({"configs": names, "variants": order,
               "period": [a.start, a.end], "rr_pool": a.rr},
              open(os.path.join(a.out, "run_info.json"), "w"), indent=1)
    print(f"\nWrote {a.out}/reversal_comparison.csv, "
          f"reversal_comparison_wide.csv, reversal_totals.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
