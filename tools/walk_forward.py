#!/usr/bin/env python3
"""Walk-forward test of the reversal idea: re-fit to recent data, trade forward.

The question this answers
-------------------------
The London reversal study picked the 10 configurations that lost the most on
2026 data and then reversed them ON THAT SAME 2026 DATA. That is profitable by
arithmetic, not by edge -- it would come out profitable on random noise too.

This tool removes that circularity without assuming any edge lasts forever:

    for every month M in the history:
        look ONLY at the F months before M          <- the fit
        rank the configurations by how badly they lost in that window
        take the K worst, choose the reversal variant that worked best there
        trade that choice through month M and record the result   <- the test

Every recorded trade happens on data the selection never saw, but the selection
is always RECENT -- which is how the account is actually traded. If the sum of
those forward months is positive, "re-fit to recent losers and reverse them" is
a real procedure. If it is not, the in-sample profit was the selection talking.

Two phases
----------
    python tools/walk_forward.py collect --data data/gc_1m_merged.parquet \
        --start 2023-01-01 --end 2026-08-13 --out walk_forward_out

        Runs every LONDON configuration in all four variants across the whole
        history ONCE and stores the individual trades. This is the expensive
        part (a few CPU-hours); it is done once and reused.

    python tools/walk_forward.py analyze --out walk_forward_out

        Replays the walk-forward selection over those stored trades for a grid
        of fit lengths and selection sizes. Seconds, not hours.

Splitting it this way is exact, not an approximation: lots are fixed, so a
trade's P&L does not depend on the account balance before it, and each session
is independent of the ones before it. Slicing the trade record by session date
gives precisely the numbers a run restricted to that window would have given.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                          # noqa: E402

from orb.backtest import make_clock, run_backtest            # noqa: E402
from orb.config import AppConfig                             # noqa: E402
from orb.data.dbn import load_dbn_bars                       # noqa: E402
from orb.logger import RbeaLogger                            # noqa: E402
from orb.engines.orb_reverse import mirror_settings_for      # noqa: E402

#: the default configuration — the orb engine's own
#: master config. There is no parent config file any more.
ENGINE_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "orb", "engines", "orb", "config.yaml")

# variant -> (reverse entries?, max trades per session; 0 = unlimited)
VARIANTS = {
    "ORIGINAL": (False, 0),
    "R":        (True, 1),
    "RR":       (True, 2),
    "RRR":      (True, 3),
}
REVERSALS = ["R", "RR", "RRR"]

_BARS = None
_BUILT = None


def _init_worker(cfg_path, data, start, end):
    """Set up a worker process.

    Windows and macOS start workers with `spawn`, which does not inherit the
    parent's memory: a global set in the parent arrives as None in the worker.
    Each worker therefore rebuilds the configuration grid (deterministic) and
    re-reads the bars (~120 MB per worker per 7 months of M1) rather than
    relying on `fork`.
    """
    global _BARS, _BUILT
    base = AppConfig.load(cfg_path)
    if data:
        base.backtest.dbn_paths = data
    d = base.databento
    _BARS = load_dbn_bars(base.backtest.dbn_paths, make_clock(base),
                          contract_mode=d.contract_mode,
                          contract_symbol=d.contract_symbol,
                          include_spreads=d.include_spreads,
                          roll_min_volume=d.roll_min_volume,
                          roll_boundary_hour=d.roll_boundary_hour,
                          start=start, end=end, logger=RbeaLogger(level=0))
    rm = load_matrix_builder()
    _BUILT = {i["name"]: i["cfg"] for i in rm.build_configs(base, "", rm.RR_VALUES)}


def load_matrix_builder():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_matrix.py")
    spec = importlib.util.spec_from_file_location("run_matrix", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
def _run_one(task):
    """One configuration in one variant, across the whole loaded history."""
    name, variant = task
    reverse, max_trades = VARIANTS[variant]
    cfg = copy.deepcopy(_BUILT[name])
    for s in cfg.sessions.values():
        s.max_trades_per_session = max_trades
        s.log_level = "none"
    cfg.strategy.log_level = "none"
    t0 = time.time()
    if reverse:
        # engine selected per session through the registry — the old
        # class-swap is gone, see mirror_settings_for()
        mirror_settings_for(cfg, max_trades).apply_to(cfg)
    res = run_backtest(cfg, _BARS, RbeaLogger(level=0))
    rows = [{
        "config": name,
        "variant": variant,
        "session_start": t.session_start,
        "entry_time": t.entry_time,
        "exit_time": t.exit_time,
        "direction": t.direction,
        "net_profit": t.net_profit,
        "r_multiple": t.r_multiple,
        "exit_reason": t.exit_reason,
        "trade_in_session": t.trade_no_in_session,
    } for t in res.trades]
    return name, variant, rows, time.time() - t0


def collect(a) -> int:
    global _BARS, _BUILT
    base = AppConfig.load(a.config)
    if a.data:
        base.backtest.dbn_paths = a.data
    d = base.databento
    print("Loading bars ...", flush=True)
    _BARS = load_dbn_bars(base.backtest.dbn_paths, make_clock(base),
                          contract_mode=d.contract_mode,
                          contract_symbol=d.contract_symbol,
                          include_spreads=d.include_spreads,
                          roll_min_volume=d.roll_min_volume,
                          roll_boundary_hour=d.roll_boundary_hour,
                          start=a.start, end=a.end, logger=RbeaLogger(level=0))
    print(f"  {len(_BARS):,} bars  {_BARS[0].time:%Y-%m-%d %H:%M} .. "
          f"{_BARS[-1].time:%Y-%m-%d %H:%M}  (New York time)", flush=True)

    rm = load_matrix_builder()
    _BUILT = {i["name"]: i["cfg"] for i in rm.build_configs(base, "", rm.RR_VALUES)}
    names = sorted(n for n in _BUILT if f"_{a.session.upper()}_" in n)
    if a.limit:
        names = names[:a.limit]
    tasks = [(n, v) for n in names for v in VARIANTS]
    print(f"{len(names)} {a.session.upper()} configurations x {len(VARIANTS)} "
          f"variants = {len(tasks)} runs", flush=True)

    os.makedirs(a.out, exist_ok=True)
    all_rows, done, t_start = [], 0, time.time()
    ctx = mp.get_context("spawn")
    with ctx.Pool(a.jobs, initializer=_init_worker,
                  initargs=(a.config, a.data, a.start, a.end)) as pool:
        for name, variant, rows, secs in pool.imap_unordered(_run_one, tasks,
                                                             chunksize=1):
            all_rows.extend(rows)
            done += 1
            if done % 10 == 0 or done == len(tasks):
                el = time.time() - t_start
                eta = el / done * (len(tasks) - done)
                print(f"[{done:>4}/{len(tasks)}] {name} {variant:<8} "
                      f"{len(rows):>5} trades  {secs:5.1f}s   "
                      f"elapsed {el/60:5.1f}m  eta {eta/60:5.1f}m", flush=True)

    df = pd.DataFrame(all_rows)
    df["session_start"] = pd.to_datetime(df["session_start"])
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    path = os.path.join(a.out, "trades_all.parquet")
    df.to_parquet(path, index=False)
    json.dump({"session": a.session.upper(), "configs": names,
               "variants": list(VARIANTS), "period": [a.start, a.end],
               "bars": len(_BARS), "runs": len(tasks)},
              open(os.path.join(a.out, "collect_info.json"), "w"), indent=1)
    print(f"\n{len(df):,} trades from {len(tasks)} runs -> {path}")
    return 0


# --------------------------------------------------------------------------
def _windows(months, fit: int, step: int):
    """(fit months, trade months) pairs, rolling forward one step at a time."""
    out = []
    i = fit
    while i + step <= len(months):
        out.append((months[i - fit:i], months[i:i + step]))
        i += step
    return out


def walk(df: pd.DataFrame, fit: int, k: int, step: int, min_trades: int,
         pick_variant: str | None):
    """One walk-forward pass. Returns the per-month rows."""
    months = sorted(df["month"].unique())
    rows = []
    for fit_months, trade_months in _windows(months, fit, step):
        f = df[df["month"].isin(fit_months)]
        t = df[df["month"].isin(trade_months)]
        if f.empty or t.empty:
            continue
        orig = f[f.variant == "ORIGINAL"]
        agg = orig.groupby("config").agg(net=("net_profit", "sum"),
                                         n=("net_profit", "size"))
        agg = agg[agg.n >= min_trades]
        if len(agg) < k:
            continue
        worst = agg.nsmallest(k, "net").index.tolist()
        best = agg.nlargest(k, "net").index.tolist()

        label = trade_months[0] if step == 1 else \
            f"{trade_months[0]}..{trade_months[-1]}"

        def pnl(cfgs, variant_of):
            n_pnl, n_tr, picks = 0.0, 0, []
            for c in cfgs:
                v = variant_of(f, c)
                if v is None:
                    continue
                sel = t[(t.config == c) & (t.variant == v)]
                n_pnl += float(sel.net_profit.sum())
                n_tr += len(sel)
                picks.append(f"{c}|{v}")
            return n_pnl, n_tr, picks

        def best_reversal(fit_df, c):
            sub = fit_df[(fit_df.config == c) & (fit_df.variant.isin(REVERSALS))]
            if sub.empty:
                return None
            return sub.groupby("variant").net_profit.sum().idxmax()

        strategies = {
            # the procedure under test: worst K in the fit window, reversed
            "REVERSE_WORST": (worst, best_reversal if pick_variant is None
                              else (lambda _f, _c, v=pick_variant: v)),
            # the direct counterfactual: keep trading those same losers as-is
            "KEEP_WORST": (worst, lambda _f, _c: "ORIGINAL"),
            # the ordinary momentum benchmark: back the recent winners
            "FOLLOW_BEST": (best, lambda _f, _c: "ORIGINAL"),
        }
        for sname, (cfgs, vof) in strategies.items():
            p, n, picks = pnl(cfgs, vof)
            rows.append({"trade_month": label, "strategy": sname,
                         "net_profit": round(p, 2), "trades": n,
                         "configs": len(picks), "picks": ";".join(picks)})
        # every configuration, every month, no selection at all
        for v in VARIANTS:
            sel = t[t.variant == v]
            rows.append({"trade_month": label, "strategy": f"ALL_{v}",
                         "net_profit": round(float(sel.net_profit.sum()), 2),
                         "trades": len(sel),
                         "configs": sel.config.nunique(), "picks": ""})
    return pd.DataFrame(rows)


def summarize(w: pd.DataFrame) -> pd.DataFrame:
    g = w.groupby("strategy")
    out = pd.DataFrame({
        "months": g.size(),
        "net_profit": g.net_profit.sum(),
        "trades": g.trades.sum(),
        "months_up": g.net_profit.apply(lambda x: int((x > 0).sum())),
        "best_month": g.net_profit.max(),
        "worst_month": g.net_profit.min(),
        "avg_month": g.net_profit.mean(),
    })
    out["hit_rate_%"] = (out.months_up / out.months * 100).round(1)
    # drawdown of the month-by-month equity curve
    dd = {}
    for s, sub in w.groupby("strategy"):
        eq = sub.sort_values("trade_month").net_profit.cumsum()
        dd[s] = float((eq - eq.cummax()).min())
    out["max_dd"] = pd.Series(dd)
    return out.sort_values("net_profit", ascending=False).round(2)


def analyze(a) -> int:
    path = os.path.join(a.out, "trades_all.parquet")
    df = pd.read_parquet(path)
    df["month"] = pd.to_datetime(df["session_start"]).dt.to_period("M").astype(str)
    info = json.load(open(os.path.join(a.out, "collect_info.json")))
    print(f"{len(df):,} trades  {df.config.nunique()} configurations  "
          f"{df.month.min()} .. {df.month.max()}\n")

    grid_rows, detail = [], {}
    fits = [int(x) for x in a.fit.split(",")]
    ks = [int(x) for x in a.k.split(",")]
    for fit in fits:
        for k in ks:
            w = walk(df, fit, k, a.step, a.min_trades, a.variant)
            if w.empty:
                continue
            s = summarize(w)
            detail[(fit, k)] = (w, s)
            for name in ("REVERSE_WORST", "KEEP_WORST", "FOLLOW_BEST"):
                if name in s.index:
                    r = s.loc[name]
                    grid_rows.append({"fit_months": fit, "worst_k": k,
                                      "strategy": name,
                                      "net_profit": r.net_profit,
                                      "trades": int(r.trades),
                                      "months": int(r.months),
                                      "hit_rate_%": r["hit_rate_%"],
                                      "max_dd": r.max_dd,
                                      "avg_month": r.avg_month})
    grid = pd.DataFrame(grid_rows)
    grid.to_csv(os.path.join(a.out, "walk_forward_grid.csv"), index=False)

    # the fullest detail for the middle of the grid
    key = (fits[len(fits) // 2], ks[len(ks) // 2])
    if key in detail:
        w, s = detail[key]
        w.to_csv(os.path.join(a.out, f"monthly_fit{key[0]}_k{key[1]}.csv"),
                 index=False)
        s.to_csv(os.path.join(a.out, f"summary_fit{key[0]}_k{key[1]}.csv"))

    piv = grid.pivot_table(index=["fit_months", "worst_k"], columns="strategy",
                           values="net_profit")
    piv.to_csv(os.path.join(a.out, "walk_forward_matrix.csv"))

    print("=" * 78)
    print(f"  WALK-FORWARD, {info['session']} — net P&L by fit length and "
          f"selection size")
    print("=" * 78)
    print(piv.round(0).to_string())
    print("\nDetail for fit=%d months, k=%d configurations:" % key)
    if key in detail:
        print(detail[key][1].to_string())
    print(f"\nWrote {a.out}/walk_forward_grid.csv, walk_forward_matrix.csv, "
          f"monthly_*.csv, summary_*.csv")
    return 0


# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect", help="run every configuration once (slow)")
    c.add_argument("--config", "-c", default=ENGINE_CONFIG)
    c.add_argument("--data", "-d", nargs="+", default=None)
    c.add_argument("--session", default="LONDON")
    c.add_argument("--start", default="2023-01-01")
    c.add_argument("--end", default="2026-08-13")
    c.add_argument("--out", default="walk_forward_out")
    c.add_argument("--jobs", "-j", type=int, default=max(1, (os.cpu_count() or 2)))
    c.add_argument("--limit", type=int, default=0, help="first N configs only")

    n = sub.add_parser("analyze", help="replay the walk-forward (fast)")
    n.add_argument("--out", default="walk_forward_out")
    n.add_argument("--fit", default="1,2,3,6",
                   help="fit window lengths in months")
    n.add_argument("--k", default="1,3,5,10",
                   help="how many of the worst configurations to take")
    n.add_argument("--step", type=int, default=1,
                   help="months traded before re-fitting")
    n.add_argument("--min-trades", type=int, default=5,
                   help="ignore a configuration with fewer trades in the fit")
    n.add_argument("--variant", default=None,
                   help="force one reversal variant (R/RR/RRR) instead of "
                        "picking the best in the fit window")

    a = p.parse_args()
    return collect(a) if a.cmd == "collect" else analyze(a)


if __name__ == "__main__":
    raise SystemExit(main())
