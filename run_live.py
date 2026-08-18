#!/usr/bin/env python3
"""Run an engine live: Databento data -> MetaTrader 5 execution.

    python run_live.py                                  # the orb engine
    python run_live.py --engine orb_reverse             # the reverse engine
    python run_live.py --engine orb,orb_reverse         # both, one account

`--engine` picks WHICH ENGINE CONFIG is loaded, exactly as it does for
`tools/backtest.py`. Naming several merges their sessions onto one MT5
account: each session runs the engine of the file it is written in, and the
blocks a run has only one of — instrument, data, account, clock — must match
across those files. `--config` overrides the file for a single engine.

Every other flag mirrors the backtester and overrides the file for this run
only — see docs/CLI.md or --help.

Credentials are NOT in any config file. DATABENTO_API_KEY, MT5_LOGIN,
MT5_PASSWORD and MT5_SERVER live in `.env` at the project root, which is
git-ignored; copy `.env.example` to `.env` and fill it in. The run stops
before touching the market if any of them is missing.

Requires Windows with a running MT5 terminal, and
`pip install MetaTrader5 databento`.
"""
from __future__ import annotations

import json
import sys

from orb.cli import ENGINE_CONFIG, apply_options, build_parser
from orb.config import AppConfig, ENV_FILE, missing_secrets
from orb.live_trader import LiveTrader
from orb.runconfig import RunConfig, available, merge

#: argparse destination for --engine. `orb/cli.py` derives every SPEC flag's
#: dest from its config path, so --engine (strategy.engine) lands here, not on
#: `a.engine`. Named once so a path rename cannot break this file silently.
ENGINE_DEST = "opt_strategy__engine"

EPILOG = """
examples:
  python run_live.py --dry-run
  python run_live.py --engine orb_reverse --dry-run
  python run_live.py --engine orb,orb_reverse --show-config
  python run_live.py --range 13:30-14:30 --utc-offset 2
  python run_live.py --mt5-symbol XAUUSD

Credentials come from .env, never from a flag or the config file:
  DATABENTO_API_KEY, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_TERMINAL_PATH
"""


def _load(a) -> AppConfig:
    """Build the AppConfig this run will use.

    `--engine` names one or more engines, and each engine's own config.yaml is
    the complete configuration for it — there is no parent config. Naming
    several merges them onto one account; `merge` raises if the blocks a run
    has only one of disagree between the files.

    `--config` names a file explicitly and is only meaningful for one engine.
    """
    raw = getattr(a, ENGINE_DEST, None)
    names = [e.strip().lower() for e in str(raw or "").split(",") if e.strip()]
    # `--config` carries a default, so "was it given?" means "is it something
    # other than the default". Only an explicit file overrides the engine's own.
    chosen = a.config if a.config and a.config != ENGINE_CONFIG else None
    if not names:
        # No --engine: fall back to whatever --config points at, whose own
        # default (orb/engines/orb/config.yaml) is the orb engine.
        return AppConfig.load(a.config)
    unknown = [n for n in names if n not in available()]
    if unknown:
        raise ValueError(f"Unknown engine(s): {', '.join(unknown)}. "
                         f"Available: {', '.join(available())}")
    if len(names) > 1:
        if chosen:
            raise ValueError("--config names one file, so it cannot be "
                             "combined with several engines.")
        return merge(RunConfig.load_many(names))
    return RunConfig.load(names[0], chosen).app_config()


def main(argv=None) -> int:
    p = build_parser("run_live.py", "ORB engines — live trading", EPILOG)
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
        cfg = _load(a)
        # `--engine` has already chosen the config file(s) above. Blank it so
        # apply_options does not then stamp the raw string onto every session's
        # `engine` field — which for a merged run would be "orb,orb_reverse",
        # a name no engine is registered under.
        setattr(a, ENGINE_DEST, None)
        cfg = apply_options(cfg, a)
        cfg.validate_sessions()
    except (ValueError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    # Printed BEFORE the credential gate below, so `--show-config` still works
    # on a machine that has no `.env` — inspecting what would run should not
    # require the keys to run it.
    if a.show_config:
        print(json.dumps(cfg.to_dict(), indent=2, default=str))

    for session in cfg.enabled_sessions():
        print(f"  session {(session.name or 'MAIN'):<12} [{session.engine}]  "
              f"{session.range_start}-{session.range_end} -> "
              f"{session.stop_time}  magic {session.magic}", file=sys.stderr)

    # The last thing checked before the market is touched. Missing credentials
    # stop the run here rather than half-way through the first session.
    missing = missing_secrets(cfg, live=True)
    if missing:
        print(f"Missing credential(s): {', '.join(missing)}\n"
              f"They belong in {ENV_FILE} at the project root — copy "
              f".env.example to {ENV_FILE} and fill it in.\n"
              f"They are deliberately not in the engine config, which is "
              f"tracked in git.", file=sys.stderr)
        return 2

    # A clean message rather than a traceback out of the broker constructor.
    # The usual cause is running this on a machine without the MetaTrader5
    # package, which only exists on Windows — worth saying plainly, because
    # everything up to this point works fine anywhere.
    try:
        trader = LiveTrader(cfg)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not a.no_warmup:
        trader.warmup(days=a.warmup_days)
    trader.run(poll_seconds=a.poll)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
