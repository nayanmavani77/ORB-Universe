#!/usr/bin/env python3
"""Download Databento history, and inspect what is in the files you have.

    python download_data.py --dataset GLBX.MDP3 --db-symbols GC.FUT \
        --stype-in parent --schema ohlcv-1m \
        --download-start 2024-01-01 --download-end 2025-01-01

    python download_data.py --list-contracts        # what is inside the data

`--list-contracts` lives here rather than with a backtest runner: it answers
"which contracts are in these files, and how much volume does each have",
which is a question about the DATA, not about a strategy. It reads files you
already have and downloads nothing, so it needs no API key.

DOWNLOADING does need one. DATABENTO_API_KEY lives in `.env` at the project
root (copy `.env.example` to `.env`), never in a config file — the configs are
tracked in git.

`--config` defaults to the orb engine's own config; the DATA block is shared
across engines, so it does not matter which one you point at.
"""
from __future__ import annotations

import sys

from orb.cli import apply_options, build_parser
from orb.config import ENV_FILE, AppConfig, missing_secrets
from orb.data.dbn import download_history, list_contracts


def main(argv=None) -> int:
    p = build_parser("download_data.py", "Databento history downloader",
                     include=("Data source (Databento)",))
    p.add_argument("--set", dest="overrides", action="append", default=[],
                   metavar="PATH=VALUE",
                   help="override any config field directly (repeatable), "
                        "e.g. --set databento.schema=ohlcv-1s")
    p.add_argument("--list-contracts", action="store_true",
                   help="list the contracts found in the configured data files "
                        "with their bar counts and volume, then stop. Nothing "
                        "is downloaded.")
    a = p.parse_args(argv)

    try:
        cfg = apply_options(AppConfig.load(a.config), a)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if a.list_contracts:
        # `--data` is already a shared flag (backtest.dbn_paths) and
        # apply_options has folded it in above
        paths = cfg.backtest.dbn_paths
        if not paths:
            print("No data files configured. Set backtest.dbn_paths in the "
                  "engine config, or pass --data.", file=sys.stderr)
            return 2
        try:
            print(list_contracts(paths).to_string())
        except (ValueError, FileNotFoundError) as exc:
            print(f"Data error: {exc}", file=sys.stderr)
            return 2
        return 0

    d = cfg.databento
    for field in ("start", "end"):
        if not getattr(d, field):
            print(f"databento.{field} is required "
                  f"(config, or --download-{field})", file=sys.stderr)
            return 2

    # Checked before the request rather than inside the Databento client, so
    # the message names `.env` instead of a bare "no API key".
    missing = missing_secrets(cfg, live=False)
    if missing:
        print(f"Missing credential(s): {', '.join(missing)}\n"
              f"They belong in {ENV_FILE} at the project root — copy "
              f".env.example to {ENV_FILE} and fill it in.\n"
              f"(`--list-contracts` reads local files and needs no key.)",
              file=sys.stderr)
        return 2

    print(f"Downloading {d.dataset} {d.symbols} {d.schema} "
          f"{d.start} .. {d.end} ...")
    print(f"Saved: {download_history(d)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
