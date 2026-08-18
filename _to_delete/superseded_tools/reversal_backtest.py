#!/usr/bin/env python3
"""Run ONE reversal backtest and write the usual report.

Everything comes from `reversal_config.yaml`, so the normal way to use this is
to edit that file and run with no arguments at all:

    python tools/reversal_backtest.py

Every setting is also a flag, and a flag OVERRIDES the file — for trying one
value without editing anything:

    python tools/reversal_backtest.py --sl-mult 1.5
    python tools/reversal_backtest.py --sl-mult 1.5 --forward   # control arm

The stop loss is `--sl-mult` x the opening range:

    0.25  quarter range      tight
    0.5   half the range     identical to the original engine's `mid_range`
    1.0   the whole range    identical to the original engine's `full_range`
    1.5   wider than the original engine can express
    2.0   wider still

`--forward` runs the same settings in the ORDINARY breakout direction. Run it
alongside every reversal: if the forward version at the same multiplier is also
profitable, what you found is a stop distance that suits this market, not a
fade edge.

The session names its engine in config (`engine: reversal`) and the runner
resolves it through `orb/registry.py` — there is no class swapping any more, so
this engine also works live and alongside other engines.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orb.backtest import make_clock                            # noqa: E402
from orb.data.dbn import load_dbn_bars                         # noqa: E402
from orb.logger import RbeaLogger, parse_log_level             # noqa: E402
from orb.outputs import resolve as resolve_out                 # noqa: E402
from orb.report import compute_stats, write_report             # noqa: E402

from orb_reversal.runner import run_reversal                   # noqa: E402
from orb_reversal.settings import DEFAULT_PATH, ReversalConfig  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reversal-config", "-r", default=DEFAULT_PATH,
                   help="the reversal settings file (default: %(default)s)")
    p.add_argument("--show", action="store_true",
                   help="print the settings that would run, then stop")

    g = p.add_argument_group("overrides — omit to use the config file")
    g.add_argument("--session", default=None)
    g.add_argument("--tf", dest="signal_timeframe", default=None)
    g.add_argument("--orb", dest="orb_minutes", type=int, default=None)
    g.add_argument("--rr", dest="risk_reward", type=float, default=None)
    g.add_argument("--lots", type=float, default=None)
    g.add_argument("--sl-mult", type=float, default=None,
                   help="stop as a multiple of the opening range")
    g.add_argument("--anchor", default=None, choices=["range", "mirror"])
    g.add_argument("--max-trades", type=int, default=None,
                   help="1=R, 2=RR, 3=RRR, 0=unlimited")
    g.add_argument("--forward", action="store_true",
                   help="ordinary breakout direction instead of the fade")
    g.add_argument("--reverse", action="store_true",
                   help="force the fade (overrides the file)")
    g.add_argument("--skip-news", action="store_true")
    g.add_argument("--include-news", action="store_true")
    g.add_argument("--start", default=None)
    g.add_argument("--end", default=None)
    g.add_argument("--data", "-d", nargs="+", default=None)
    g.add_argument("--out", default=None)
    g.add_argument("--log-level", default=None)
    a = p.parse_args()

    rc = ReversalConfig.load(a.reversal_config)

    run_over = {k: getattr(a, k) for k in
                ("session", "signal_timeframe", "orb_minutes", "risk_reward",
                 "lots", "log_level")}
    if a.skip_news:
        run_over["news"] = "skip"
    if a.include_news:
        run_over["news"] = "include"
    if a.start:
        rc.period["start"] = a.start
    if a.end:
        rc.period["end"] = a.end
    if a.data:
        rc.period["data"] = a.data
    if a.out:
        rc.output["dir"] = a.out
    if a.sl_mult is not None:
        rc.reversal["sl_range_mult"] = a.sl_mult
    if a.anchor:
        rc.reversal["sl_anchor"] = a.anchor
    if a.max_trades is not None:
        rc.reversal["max_trades"] = a.max_trades
    if a.forward:
        rc.reversal["direction"] = "forward"
    if a.reverse:
        rc.reversal["direction"] = "reverse"

    app = rc.app_config(run_over)
    st = rc.settings()
    name = rc.name()
    start, end = rc.dates()
    # outputs/<engine>/<run-name>/ unless --out (or output.dir) says otherwise
    out = resolve_out(app, name, out_dir=a.out or rc.output.get("dir"))

    print("=" * 66)
    print("  REVERSAL BACKTEST")
    print("=" * 66)
    print(rc.describe())
    print(f"  output         {out}/")
    print("=" * 66 + "\n")
    if a.show:
        return 0

    app.backtest.out_dir = out
    app.backtest.report_name = name
    log_path = os.path.join(out, f"{name}_journal.log")
    if os.path.exists(log_path):
        os.remove(log_path)
    level = str(run_over.get("log_level") or rc.run.get("log_level", "normal"))
    for s in app.enabled_sessions():
        s.log_file = log_path
    app.strategy.log_file = log_path

    d = app.databento
    print("Loading bars ...")
    bars = load_dbn_bars(app.backtest.dbn_paths, make_clock(app),
                         contract_mode=d.contract_mode,
                         contract_symbol=d.contract_symbol,
                         include_spreads=d.include_spreads,
                         roll_min_volume=d.roll_min_volume,
                         roll_boundary_hour=d.roll_boundary_hour,
                         start=start, end=end, logger=RbeaLogger(level=0))
    print(f"  {len(bars):,} bars  {bars[0].time:%Y-%m-%d %H:%M} .. "
          f"{bars[-1].time:%Y-%m-%d %H:%M}  (New York time)\n")

    log = RbeaLogger(level=parse_log_level(level), file_path=log_path,
                     show_time=True)
    result = run_reversal(app, bars, st, logger=log)
    log.close()

    stats = compute_stats(result)
    write_report(result)

    print("=" * 66)
    print(f"  {name}")
    print("=" * 66)
    for k in ("total_trades", "wins", "losses", "win_rate", "net_profit",
              "profit_factor", "expectancy", "max_dd_money", "max_dd_pct",
              "avg_r", "total_r", "recovery_factor"):
        v = stats[k]
        print(f"  {k:<18} {v:>14,.2f}" if isinstance(v, (int, float))
              else f"  {k:<18} {v:>14}")
    print(f"\n  Report, trades CSV and journal in {out}/")
    print(f"  Change settings in {rc.path}, or pass a flag to override once.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
