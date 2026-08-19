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


def show_cost(d) -> int:
    """What this request would cost, before committing to it.

    Databento bills historical data by volume, so a request is worth pricing
    before it is sent — especially with `stype_in: parent`, which returns EVERY
    contract month plus every calendar spread, not just the front month.
    """
    try:
        import databento as db
    except ImportError:
        print("pip install databento", file=sys.stderr)
        return 2

    client = db.Historical(d.api_key)
    args = dict(dataset=d.dataset, symbols=d.symbols, schema=d.schema,
                stype_in=d.stype_in, start=d.start, end=d.end)
    print(f"  {d.dataset}  {d.symbols}  {d.schema}  "
          f"{d.stype_in}  {d.start} .. {d.end}")
    try:
        size = client.metadata.get_billable_size(**args)
        print(f"  billable size   {size:,} bytes ({size / 1e6:,.1f} MB)")
    except Exception as exc:
        print(f"  billable size   unavailable ({exc!r})")
    try:
        count = client.metadata.get_record_count(**args)
        print(f"  records         {count:,}")
    except Exception as exc:
        print(f"  records         unavailable ({exc!r})")
    try:
        cost = client.metadata.get_cost(**args)
        print(f"  COST            ${cost:,.2f}")
    except Exception as exc:
        print(f"  cost            unavailable ({exc!r})")
    print("\n  Nothing was downloaded and nothing was billed. Drop --cost to "
          "run it for real.")
    return 0


def main(argv=None) -> int:
    p = build_parser("download_data.py", "Databento history downloader",
                     include=("Data source (Databento)",))
    p.add_argument("--set", dest="overrides", action="append", default=[],
                   metavar="PATH=VALUE",
                   help="override any config field directly (repeatable), "
                        "e.g. --set databento.schema=ohlcv-1s")
    p.add_argument("--cost", action="store_true",
                   help="ask Databento what this request would cost and how "
                        "many records it would return, then stop. Nothing is "
                        "downloaded and nothing is billed. Worth doing before "
                        "any request measured in months — a year of "
                        "parent-symbology 1-minute data is a different order "
                        "of size from a week of it.")
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

    if a.cost:
        return show_cost(d)

    print(f"Downloading {d.dataset} {d.symbols} {d.schema} "
          f"{d.start} .. {d.end} ...")
    print(f"Saved: {download_history(d)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
