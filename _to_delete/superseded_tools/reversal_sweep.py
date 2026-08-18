#!/usr/bin/env python3
"""Sweep the reversal engine, including the stop-loss range multiplier.

The lists come from section 5 of `reversal_config.yaml`, so the normal way to
use this is to edit that file and run with no arguments:

    python tools/reversal_sweep.py --dry-run      # how many runs is that?
    python tools/reversal_sweep.py                # go

Every list is also a flag, and a flag OVERRIDES the file:

    python tools/reversal_sweep.py --sl-mult 0.5,1,1.5 --tf M5 --dry-run

The dimension the original matrix never searched
------------------------------------------------
`tools/run_matrix.py` sweeps timeframe x session x ORB length x news x R:R and
leaves the stop at whatever `config.yaml` says — in practice `mid_range`, i.e.
0.5 x range, for every run ever done. This tool adds the multiplier as a real
axis, which for a fade is the parameter most likely to matter: a reversed trade
is betting price returns into the range, and how much room it gets before the
return happens is the whole question.

Keep both directions
--------------------
`directions: [reverse, forward]` adds the ordinary breakout direction at every
identical setting. Without that control arm a profitable reversal cannot be
distinguished from a stop distance that simply suits the market. The comparison
lands in `_summary/reverse_vs_forward.csv`.

Reading the output
------------------
Lots are fixed, so dollar risk per trade scales with the multiplier: a 2.0x run
risks 4x the money of a 0.5x run per trade. Net P&L is therefore NOT comparable
across multipliers — `avg_r` and `total_r` are.

Nothing under `orb/` is modified.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                            # noqa: E402

from orb.backtest import make_clock, run_backtest              # noqa: E402
from orb.data.dbn import load_dbn_bars                         # noqa: E402
from orb.logger import RbeaLogger                              # noqa: E402
from orb.outputs import sweep_dir                              # noqa: E402
from orb.report import compute_stats, trades_dataframe         # noqa: E402

from orb_reversal.settings import DEFAULT_PATH, ReversalConfig  # noqa: E402

_BARS = None
_ITEMS = None
_SAVE_TRADES = False


# --------------------------------------------------------------------------
def _init_worker(cfg_path, over, start, end, data, save_trades):
    """Set up a worker process.

    Windows (and macOS) start worker processes with `spawn`, which does NOT
    inherit the parent's memory — a module-level global set in `main()` arrives
    as None in the worker. Only Linux's `fork` shares it. So every worker
    rebuilds what it needs here instead of relying on inheritance: the grid is
    deterministic from the config, and the bars are re-read from the same file.

    Cost: each worker holds its own copy of the bars, roughly 120 MB per worker
    for a 7-month M1 history. With many cores, watch RAM — `-j` controls it.
    """
    global _BARS, _ITEMS, _SAVE_TRADES
    _SAVE_TRADES = save_trades
    rc = ReversalConfig.load(cfg_path)
    rc.period["start"], rc.period["end"] = start, end
    if data:
        rc.period["data"] = data
    _ITEMS = rc.sweep_items(over)
    app = _ITEMS[0].cfg
    d = app.databento
    _BARS = load_dbn_bars(app.backtest.dbn_paths, make_clock(app),
                          contract_mode=d.contract_mode,
                          contract_symbol=d.contract_symbol,
                          include_spreads=d.include_spreads,
                          roll_min_volume=d.roll_min_volume,
                          roll_boundary_hour=d.roll_boundary_hour,
                          start=start, end=end, logger=RbeaLogger(level=0))


def _floats(s):
    return None if s is None else [float(x) for x in
                                   str(s).replace(" ", "").split(",") if x]


def _ints(s):
    return None if s is None else [int(x) for x in
                                   str(s).replace(" ", "").split(",") if x]


def _strs(s):
    return None if s is None else [x.strip() for x in str(s).split(",")
                                   if x.strip()]


# --------------------------------------------------------------------------
def _run_one(idx: int):
    item = _ITEMS[idx]
    # the session's config names its engine; no class swapping needed
    res = run_backtest(item.cfg, _BARS, RbeaLogger(level=0))
    st = compute_stats(res)
    row = item.row() | {
        "trades": st["total_trades"], "wins": st["wins"], "losses": st["losses"],
        "win_rate": st["win_rate"], "net_profit": st["net_profit"],
        "profit_factor": (None if st["profit_factor"] == float("inf")
                          else st["profit_factor"]),
        "expectancy": st["expectancy"], "max_dd_money": st["max_dd_money"],
        "max_dd_pct": st["max_dd_pct"], "avg_r": st["avg_r"],
        "total_r": st["total_r"], "recovery_factor": st["recovery_factor"],
        "max_consec_losses": st["max_consecutive_losses"],
        "long_net": st["long_net"], "short_net": st["short_net"],
        "avg_duration_min": st["avg_duration_min"],
    }
    tdf = trades_dataframe(res.trades) if _SAVE_TRADES else None
    return idx, row, tdf


# --------------------------------------------------------------------------
def main() -> int:
    global _BARS, _ITEMS, _SAVE_TRADES
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reversal-config", "-r", default=DEFAULT_PATH)
    p.add_argument("--dry-run", action="store_true",
                   help="print the run count and the axes, then stop")
    p.add_argument("--jobs", "-j", type=int, default=max(1, (os.cpu_count() or 2)))

    g = p.add_argument_group("overrides — omit to use the config file")
    g.add_argument("--session", dest="sessions", default=None)
    g.add_argument("--tf", dest="timeframes", default=None)
    g.add_argument("--orb", dest="orb_minutes", default=None)
    g.add_argument("--rr", dest="risk_reward", default=None)
    g.add_argument("--sl-mult", dest="sl_range_mult", default=None)
    g.add_argument("--caps", dest="max_trades", default=None)
    g.add_argument("--news", default=None, help="include,skip")
    g.add_argument("--directions", default=None, help="reverse,forward")
    g.add_argument("--reverse-only", action="store_true")
    g.add_argument("--forward-only", action="store_true")
    g.add_argument("--start", default=None)
    g.add_argument("--end", default=None)
    g.add_argument("--data", "-d", nargs="+", default=None)
    g.add_argument("--out", default=None)
    g.add_argument("--trades", action="store_true",
                   help="write a trades CSV per configuration")
    p.add_argument("--resume", action="store_true",
                   help="skip configurations already present in the output "
                        "folder's all_results.csv and keep their rows — for "
                        "picking a long sweep back up after an interruption")
    p.add_argument("--save-every", type=int, default=100,
                   help="write a partial all_results.csv every N runs, so an "
                        "interrupted sweep is never a total loss "
                        "(default: %(default)s)")
    a = p.parse_args()

    rc = ReversalConfig.load(a.reversal_config)
    if a.start:
        rc.period["start"] = a.start
    if a.end:
        rc.period["end"] = a.end
    if a.data:
        rc.period["data"] = a.data
    if a.out:
        rc.sweep["out_dir"] = a.out

    directions = _strs(a.directions)
    if a.reverse_only:
        directions = ["reverse"]
    if a.forward_only:
        directions = ["forward"]

    over = {"sessions": _strs(a.sessions), "timeframes": _strs(a.timeframes),
            "orb_minutes": _ints(a.orb_minutes),
            "risk_reward": _floats(a.risk_reward),
            "sl_range_mult": _floats(a.sl_range_mult),
            "max_trades": _ints(a.max_trades), "news": _strs(a.news),
            "directions": directions}
    over = {k: v for k, v in over.items() if v is not None}

    n = rc.sweep_size(over)
    w = dict(rc.sweep)
    w.update(over)
    start, end = rc.dates()
    out = a.out or rc.sweep.get("out_dir") or sweep_dir(
        ["reversal"], "sweep")
    print("=" * 74)
    print("  REVERSAL SWEEP")
    print("=" * 74)
    print(f"  config file    {rc.path}   (inherits {rc.base_config})")
    for key, label in (("sessions", "sessions"), ("timeframes", "timeframes"),
                       ("orb_minutes", "ORB minutes"), ("news", "news"),
                       ("risk_reward", "risk:reward"),
                       ("sl_range_mult", "SL x range"),
                       ("max_trades", "trades/session"),
                       ("directions", "directions")):
        if key in w:
            print(f"  {label:<14} {w[key]}")
    print(f"  period         {start} .. {end}")
    print(f"  output         {out}/")
    print(f"\n  {n:,} configurations   ~{n * 4 / max(1, a.jobs) / 60:.0f} min "
          f"on {a.jobs} core(s)")
    print("=" * 74 + "\n")
    if a.dry_run:
        print("Dry run — nothing executed. Cut a list in "
              f"{rc.path} section 5 to make this smaller.")
        return 0

    items = rc.sweep_items(over)
    _SAVE_TRADES = bool(a.trades or rc.sweep.get("save_trades"))

    # --- resume ----------------------------------------------------------
    # `items` stays the FULL grid: the workers rebuild the same list from the
    # config, so the index sent to a worker has to mean the same thing there.
    # Resuming filters the list of indices to run, never the grid itself.
    todo = list(range(len(items)))
    done_rows, prior = [], os.path.join(out, "_summary", "all_results.csv")
    if a.resume and os.path.exists(prior):
        old = pd.read_csv(prior)
        finished = set(old["run_name"])
        done_rows = old.to_dict("records")
        todo = [i for i in todo if items[i].run_name not in finished]
        print(f"Resuming: {len(finished):,} already done, "
              f"{len(todo):,} left to run\n", flush=True)
        if not todo:
            print("Nothing left to run. Delete the output folder to start over.")
            return 0
    elif a.resume:
        print(f"No previous results at {prior} — starting from the top.\n",
              flush=True)

    # Load once in the parent to fail fast on a bad path and to report the
    # range, then drop it: the workers load their own copies (see
    # `_init_worker` — spawn does not inherit memory).
    base_app = items[0].cfg
    d = base_app.databento
    print("Loading bars ...", flush=True)
    probe = load_dbn_bars(base_app.backtest.dbn_paths, make_clock(base_app),
                          contract_mode=d.contract_mode,
                          contract_symbol=d.contract_symbol,
                          include_spreads=d.include_spreads,
                          roll_min_volume=d.roll_min_volume,
                          roll_boundary_hour=d.roll_boundary_hour,
                          start=start, end=end, logger=RbeaLogger(level=0))
    n_bars, first, last = len(probe), probe[0].time, probe[-1].time
    del probe
    print(f"  {n_bars:,} bars  {first:%Y-%m-%d %H:%M} .. {last:%Y-%m-%d %H:%M}"
          f"  (New York time)", flush=True)
    print(f"  each of the {a.jobs} worker(s) loads its own copy: "
          f"~{n_bars * 550 / 1e6:.0f} MB each, ~{n_bars * 550 * a.jobs / 1e9:.1f} GB "
          f"total — lower -j if RAM is tight\n", flush=True)
    _ITEMS = items

    os.makedirs(out, exist_ok=True)
    trades_dir = os.path.join(out, "trades")
    if _SAVE_TRADES:
        os.makedirs(trades_dir, exist_ok=True)

    summary = os.path.join(out, "_summary")
    os.makedirs(summary, exist_ok=True)

    rows, done, t0 = list(done_rows), 0, time.time()
    init_args = (rc.path, over, start, end, rc.period.get("data"), _SAVE_TRADES)
    # "spawn" on every platform, so the behaviour that is tested is the
    # behaviour that runs. Windows and macOS have no choice; forcing it on
    # Linux too means one code path instead of two.
    ctx = mp.get_context("spawn")
    with ctx.Pool(a.jobs, initializer=_init_worker, initargs=init_args) as pool:
        for idx, row, tdf in pool.imap_unordered(_run_one, todo, chunksize=1):
            if row["run_name"] != items[idx].run_name:            # grids disagree
                raise SystemExit(
                    "Worker and parent built different grids — "
                    f"index {idx} is {items[idx].run_name!r} here but "
                    f"{row['run_name']!r} in the worker. Do not trust these "
                    "results; re-run without --resume.")
            rows.append(row)
            if tdf is not None and not tdf.empty:
                tdf.to_csv(os.path.join(trades_dir, f"{items[idx].run_name}.csv"),
                           index=False)
            done += 1
            if a.save_every and done % a.save_every == 0:
                pd.DataFrame(rows).to_csv(
                    os.path.join(summary, "all_results.csv"), index=False)
            if done % 10 == 0 or done == len(todo):
                el = time.time() - t0
                eta = el / done * (len(todo) - done)
                print(f"[{done:>5}/{len(todo)}] {row['run_name']:<54} "
                      f"net {row['net_profit']:>11,.0f}  "
                      f"elapsed {el/60:5.1f}m  eta {eta/60:5.1f}m", flush=True)

    df = pd.DataFrame(rows).sort_values("net_profit", ascending=False)
    df.to_csv(os.path.join(summary, "all_results.csv"), index=False)

    def agg(by):
        g = df.groupby(by)
        return pd.DataFrame({
            "configs": g.size(), "trades": g["trades"].sum(),
            "net_profit": g["net_profit"].sum(),
            "avg_net_per_config": g["net_profit"].mean(),
            "total_r": g["total_r"].sum(),
            "avg_r": g["avg_r"].mean(),
            "median_profit_factor": g["profit_factor"].median(),
            "avg_win_rate": g["win_rate"].mean(),
            "worst_dd_pct": g["max_dd_pct"].max(),
            "profitable": g["net_profit"].apply(lambda x: int((x > 0).sum())),
        }).sort_values("total_r", ascending=False)

    for by, fname in (("sl_range_mult", "by_sl_multiplier"),
                      ("risk_reward", "by_risk_reward"),
                      ("max_trades_per_session", "by_trade_cap"),
                      ("signal_timeframe", "by_timeframe"),
                      ("orb_minutes", "by_orb_duration"),
                      ("news_mode", "by_news_mode"),
                      ("session", "by_session"),
                      ("direction", "by_direction"),
                      (["sl_range_mult", "risk_reward"], "by_sl_mult_x_rr"),
                      (["sl_range_mult", "direction"], "by_sl_mult_x_direction"),
                      (["sl_range_mult", "max_trades_per_session"], "by_sl_mult_x_cap")):
        try:
            agg(by).to_csv(os.path.join(summary, f"{fname}.csv"))
        except (KeyError, ValueError):
            pass

    if df.direction.nunique() > 1:
        key = ["signal_timeframe", "session", "orb_minutes", "news_mode",
               "risk_reward", "sl_range_mult", "max_trades_per_session"]
        cols = ["trades", "net_profit", "profit_factor", "win_rate",
                "max_dd_pct", "avg_r"]
        is_rev = df.direction == "reverse"
        pair = (df[is_rev].set_index(key)[cols]
                .join(df[~is_rev].set_index(key)[cols],
                      lsuffix="_rev", rsuffix="_fwd"))
        pair["edge_over_forward"] = pair.net_profit_rev - pair.net_profit_fwd
        pair.sort_values("edge_over_forward", ascending=False).to_csv(
            os.path.join(summary, "reverse_vs_forward.csv"))

    json.dump({"reversal_config": rc.path, "base_config": rc.base_config,
               "period": [start, end], "bars": n_bars,
               "configurations": len(rows), "axes": {k: w.get(k) for k in
                                                     ("sessions", "timeframes",
                                                      "orb_minutes", "news",
                                                      "risk_reward",
                                                      "sl_range_mult",
                                                      "max_trades",
                                                      "directions")},
               "anchor": rc.settings().sl_anchor,
               "engine": "orb.backtest.run_backtest (unmodified) with "
                         "orb_reversal.ReversalStrategy substituted"},
              open(os.path.join(summary, "run_info.json"), "w"), indent=2)

    print("\n" + "=" * 78)
    print("  BY STOP MULTIPLIER   (0.5 = the original mid_range, "
          "1.0 = full_range)")
    print("  ranked by total_r — risk-normalised, unlike net P&L")
    print("=" * 78)
    print(agg("sl_range_mult").round(2).to_string())
    print("\n  TOP 10 CONFIGURATIONS BY NET P&L")
    print(df.head(10)[["run_name", "trades", "win_rate", "net_profit",
                       "profit_factor", "max_dd_pct", "avg_r"]]
          .to_string(index=False))
    print(f"\nWrote {summary}/all_results.csv and the breakdown tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
