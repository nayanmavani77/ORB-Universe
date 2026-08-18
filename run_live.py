#!/usr/bin/env python3
"""Run the Range Breakout EA live: Databento data -> MetaTrader 5 execution.

Same flags as the backtester — see docs/CLI.md or --help.
Requires Windows with a running MT5 terminal, and
`pip install MetaTrader5 databento`.
"""
from __future__ import annotations

import json
import sys

from orb.cli import apply_options, build_parser
from orb.config import AppConfig, ENV_FILE, missing_secrets
from orb.live_trader import LiveTrader

EPILOG = """
examples:
  python run_live.py -c config.yaml --dry-run
  python run_live.py -c config.yaml --range 13:30-14:30 --utc-offset 2
  python run_live.py -c config.yaml --mt5-symbol XAUUSD --mt5-login 123456
"""


def main(argv=None) -> int:
    p = build_parser("run_live.py",
                     "Range Breakout EA — live trading", EPILOG)
    g = p.add_argument_group("Live run")
    g.add_argument("--range", dest="range_window", default=None,
                   metavar="HH:MM-HH:MM", help="set the range window in one go")
    g.add_argument("--no-warmup", action="store_true",
                   help="skip the historical warm-up download")
    g.add_argument("--warmup-days", type=int, default=3,
                   help="days of history to preload (default 3)")
    g.add_argument("--poll", type=float, default=1.0,
                   help="feed poll interval in seconds (default 1.0)")
    g.add_argument("--set", dest="overrides", action="append", default=[],
                   metavar="PATH=VALUE",
                   help="override any config field directly (repeatable)")
    g.add_argument("--show-config", action="store_true")
    g.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)

    try:
        cfg = apply_options(AppConfig.load(a.config), a)
        cfg.validate_sessions()
        missing = missing_secrets(cfg, live=True)
        if missing:
            print(f"Missing credential(s): {', '.join(missing)}\n"
                  f"They belong in {ENV_FILE} at the project root — copy "
                  f".env.example to {ENV_FILE} and fill it in.\n"
                  f"They are deliberately not in the engine config, which is "
                  f"tracked in git.", file=sys.stderr)
            return 2
    except (ValueError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if a.show_config:
        print(json.dumps(cfg.to_dict(), indent=2, default=str))

    trader = LiveTrader(cfg)
    if not a.no_warmup:
        trader.warmup(days=a.warmup_days)
    trader.run(poll_seconds=a.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
