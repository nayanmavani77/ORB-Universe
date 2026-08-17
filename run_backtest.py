#!/usr/bin/env python3
"""Backtest the Range Breakout EA on downloaded Databento DBN data.

Every configuration field has a command-line flag — see docs/CLI.md, or run
this with --help. Command-line values always win over the config file, and the
config file is never modified.
"""
from __future__ import annotations

import json
import sys

from orb.backtest import make_clock, run_backtest
from orb.cli import apply_options, build_parser
from orb.config import AppConfig
from orb.data.dbn import list_contracts, load_dbn_bars
from orb.logger import RbeaLogger, parse_log_level
from orb.report import print_summary, write_report

EPILOG = """
examples:
  # everything from the config file
  python run_backtest.py -c config.yaml

  # US session on 15-minute bars, 3R target, one trade a day, realistic costs
  python run_backtest.py --range 13:30-14:30 --stop-time 20:00 \\
                         --tf M15 --rr 3 --max-trades 1 \\
                         --spread 2 --slippage 1 --commission 2.50 \\
                         --name us_session

  # a precise window, down to the minute (UTC)
  python run_backtest.py --start "2025-01-06 08:00" --end "2025-06-30 22:00"

  # a different instrument
  python run_backtest.py --symbol ES --value-per-point 50 --tick-size 0.25 \\
                         --db-symbols ES.FUT --data "data/es_*.dbn.zst"

  # one fixed contract instead of the rolling front month
  python run_backtest.py --contract GCZ5 --start 2025-08-01 --end 2025-11-30

  # sweep
  for rr in 1.5 2 2.5 3; do
      python run_backtest.py --rr $rr --name rr_$rr --quiet
  done
"""


def parse_args(argv=None):
    p = build_parser("run_backtest.py",
                     "Range Breakout EA backtester", EPILOG)
    g = p.add_argument_group("Backtest period")
    g.add_argument("--start", default=None, metavar="WHEN",
                   help="first bar to use: YYYY-MM-DD or 'YYYY-MM-DD HH:MM' (UTC)")
    g.add_argument("--end", default=None, metavar="WHEN",
                   help="last bar to use: YYYY-MM-DD (inclusive of that day) or "
                        "'YYYY-MM-DD HH:MM' (exclusive) (UTC)")
    g.add_argument("--range", dest="range_window", default=None,
                   metavar="HH:MM-HH:MM",
                   help="set the range window in one go, e.g. 13:30-14:30")
    g = p.add_argument_group("Sessions")
    g.add_argument("--sessions", default=None, metavar="NAMES",
                   help="run exactly these sessions, comma separated, e.g. "
                        "'asia,new_york'. Every other session is switched off. "
                        "Omit to use the enabled flags in the config. Target one "
                        "session's settings with "
                        "--set sessions.asia.risk_reward=3")
    g = p.add_argument_group("Misc")
    g.add_argument("--set", dest="overrides", action="append", default=[],
                   metavar="PATH=VALUE",
                   help="override any config field directly (repeatable)")
    g.add_argument("--show-config", action="store_true",
                   help="print the settings actually used, then run")
    g.add_argument("--list-contracts", action="store_true",
                   help="list the instruments in the data and exit")
    g.add_argument("--quiet", action="store_true", help="suppress the journal")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(argv)
    try:
        cfg = apply_options(AppConfig.load(a.config), a)
        # validate what will actually RUN — every enabled session — not just
        # the shared defaults block
        cfg.validate_sessions()
    except (ValueError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if not cfg.backtest.dbn_paths:
        print("No DBN data configured. Set backtest.dbn_paths in the config "
              "or pass --data.", file=sys.stderr)
        return 2

    if a.list_contracts:
        print(list_contracts(cfg.backtest.dbn_paths).to_string())
        return 0

    if a.show_config:
        print(json.dumps(cfg.to_dict(), indent=2, default=str))

    log = RbeaLogger(level=parse_log_level(cfg.strategy.log_level),
                     file_path=cfg.strategy.log_file,
                     show_time=cfg.strategy.log_show_time)
    d = cfg.databento
    try:
        bars = load_dbn_bars(cfg.backtest.dbn_paths, make_clock(cfg),
                             contract_mode=d.contract_mode,
                             contract_symbol=d.contract_symbol,
                             include_spreads=d.include_spreads,
                             roll_min_volume=d.roll_min_volume,
                             roll_boundary_hour=d.roll_boundary_hour,
                             start=a.start, end=a.end, logger=log)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Data error: {exc}", file=sys.stderr)
        return 2

    result = run_backtest(cfg, bars, logger=log)
    print_summary(result)
    print("Report files:")
    for k, v in write_report(result).items():
        print(f"  {k:<14} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
