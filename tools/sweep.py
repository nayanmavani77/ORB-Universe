#!/usr/bin/env python3
"""Grid-search the range window over your own data.

Loads the DBN files ONCE, then runs the engine for every combination, so a
48-point sweep costs one load instead of 48.

    python tools/sweep.py --tz America/New_York \
        --starts 00:00-23:00/60 --lengths 30 --split 2025-07-01

Every run is also split into an in-sample and an out-of-sample half, because a
grid search that only reports its best in-sample number is telling you about
the past, not the future.
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                              # noqa: E402

from orb.backtest import make_clock, run_backtest                # noqa: E402
from orb.cli import apply_options, build_parser                  # noqa: E402
from orb.config import AppConfig                                 # noqa: E402
from orb.data.dbn import load_dbn_bars                           # noqa: E402
from orb.logger import RbeaLogger                                # noqa: E402


# --------------------------------------------------------------------------
def expand_times(spec: str):
    """'09:00,13:30' or '00:00-23:00/60' (start-end/step-minutes)."""
    out = []
    for token in spec.split(","):
        token = token.strip()
        if "-" in token and "/" in token:
            rng, step = token.split("/")
            a, b = rng.split("-")
            step = int(step)
            t = datetime.strptime(a, "%H:%M")
            end = datetime.strptime(b, "%H:%M")
            while t <= end:
                out.append(t.strftime("%H:%M"))
                t += timedelta(minutes=step)
        else:
            out.append(token)
    return out


def add_minutes(hhmm: str, minutes: int) -> str:
    t = datetime.strptime(hhmm, "%H:%M") + timedelta(minutes=minutes)
    return t.strftime("%H:%M")


# --------------------------------------------------------------------------
def _metrics(trades, label: str):
    """Headline numbers for a slice of the trade list."""
    if not trades:
        return {f"{label}_trades": 0, f"{label}_net": 0.0, f"{label}_pf": 0.0,
                f"{label}_win": 0.0, f"{label}_dd": 0.0, f"{label}_exp": 0.0}
    net = pd.Series([t.net_profit for t in trades])
    wins, losses = net[net > 0], net[net < 0]
    equity = net.cumsum()
    dd = float((equity.cummax() - equity).max())
    return {
        f"{label}_trades": len(net),
        f"{label}_net": float(net.sum()),
        f"{label}_pf": float(wins.sum() / abs(losses.sum())) if losses.sum() else 0.0,
        f"{label}_win": float(len(wins) / len(net) * 100),
        f"{label}_dd": dd,
        f"{label}_exp": float(net.mean()),
    }


def main() -> int:
    p = build_parser("sweep.py", "Range-window grid search")
    p.add_argument("--starts", default="00:00-23:00/60",
                   help="range start times, e.g. '09:30,13:30' or '07:00-11:00/15'")
    p.add_argument("--lengths", default="30",
                   help="range lengths in minutes, comma separated, e.g. 15,30,60")
    p.add_argument("--stop-after", type=int, default=None, metavar="MINUTES",
                   help="derive stop_time as range_end + MINUTES. Without this, "
                        "a fixed stop_time gives an early range a much longer "
                        "trading window than a late one, and the comparison is "
                        "meaningless.")
    p.add_argument("--split", default=None, metavar="DATE",
                   help="in-sample / out-of-sample boundary, e.g. 2025-07-01")
    p.add_argument("--data-start", default=None)
    p.add_argument("--data-end", default=None)
    p.add_argument("--min-trades", type=int, default=50,
                   help="hide rows with fewer trades than this")
    p.add_argument("--sweep-out", dest="sweep_out", default="sweep_results.csv",
                   help="CSV to write the full grid to")
    p.add_argument("--set", dest="overrides", action="append", default=[])
    a = p.parse_args()

    cfg = apply_options(AppConfig.load(a.config), a)
    log = RbeaLogger(level=0)

    d = cfg.databento
    print("Loading data once ...")
    bars = load_dbn_bars(cfg.backtest.dbn_paths, make_clock(cfg),
                         contract_mode=d.contract_mode,
                         contract_symbol=d.contract_symbol,
                         include_spreads=d.include_spreads,
                         roll_min_volume=d.roll_min_volume,
                         roll_boundary_hour=d.roll_boundary_hour,
                         start=a.data_start, end=a.data_end, logger=log)
    zone = cfg.server_timezone or f"UTC{cfg.server_utc_offset_hours:+g}"
    print(f"  {len(bars):,} bars  {bars[0].time:%Y-%m-%d %H:%M} .. "
          f"{bars[-1].time:%Y-%m-%d %H:%M}  (clock: {zone})")

    starts = expand_times(a.starts)
    lengths = [int(x) for x in str(a.lengths).split(",")]
    split = pd.Timestamp(a.split) if a.split else None

    rows = []
    total = len(starts) * len(lengths)
    for i, start in enumerate(starts, 1):
        for length in lengths:
            run_cfg = copy.deepcopy(cfg)
            # the sweep owns the window, so it owns the session too
            run_cfg.use_single_session(f"{start}+{length}m")
            run_cfg.strategy.range_start = start
            run_cfg.strategy.range_end = add_minutes(start, length)
            if a.stop_after is not None:
                run_cfg.strategy.stop_time = add_minutes(start, length + a.stop_after)
            run_cfg.strategy.log_level = "none"
            try:
                run_cfg.validate_sessions()
                res = run_backtest(run_cfg, bars, RbeaLogger(level=0))
            except Exception as exc:
                print(f"  [{i}/{total}] {start} +{length}m -> skipped ({exc})")
                continue
            row = {"range_start": start, "range_end": run_cfg.strategy.range_end,
                   "stop_time": run_cfg.strategy.stop_time, "length_min": length}
            row.update(_metrics(res.trades, "all"))
            if split is not None:
                is_t = [t for t in res.trades
                        if pd.Timestamp(t.entry_time) < split]
                oos = [t for t in res.trades
                       if pd.Timestamp(t.entry_time) >= split]
                row.update(_metrics(is_t, "is"))
                row.update(_metrics(oos, "oos"))
            rows.append(row)
            print(f"  [{i*len(lengths)}/{total}] {start}-{row['range_end']}  "
                  f"trades {row['all_trades']:5d}  net {row['all_net']:>12,.0f}  "
                  f"PF {row['all_pf']:.2f}")

    df = pd.DataFrame(rows)
    if df.empty:
        print("No results.")
        return 1
    df.to_csv(a.sweep_out, index=False)

    show = df[df["all_trades"] >= a.min_trades].copy()
    show = show.sort_values("all_net", ascending=False)
    cols = ["range_start", "range_end", "all_trades", "all_net", "all_pf",
            "all_win", "all_dd"]
    if split is not None:
        cols += ["is_net", "is_pf", "oos_net", "oos_pf", "oos_trades"]
    print("\n" + "=" * 100)
    print("  RANKED BY TOTAL NET PROFIT")
    print("=" * 100)
    print(show[cols].head(20).to_string(index=False,
                                        float_format=lambda x: f"{x:,.2f}"))
    if split is not None:
        print("\n" + "=" * 100)
        print(f"  RANKED BY OUT-OF-SAMPLE PROFIT FACTOR  (out-of-sample = from {a.split})")
        print("=" * 100)
        print(show.sort_values("oos_pf", ascending=False)[cols]
              .head(20).to_string(index=False, float_format=lambda x: f"{x:,.2f}"))
    print(f"\nFull grid written to {a.sweep_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
