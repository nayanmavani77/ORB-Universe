#!/usr/bin/env python3
"""Download Databento historical OHLCV data as a .dbn.zst file for backtesting.

    python download_data.py -c config.yaml
    python download_data.py --dataset GLBX.MDP3 --db-symbols GC.FUT \
        --stype-in parent --schema ohlcv-1m \
        --download-start 2024-01-01 --download-end 2025-01-01
"""
from __future__ import annotations

import sys

from orb.cli import apply_options, build_parser
from orb.config import AppConfig
from orb.data.dbn import download_history


def main(argv=None) -> int:
    p = build_parser("download_data.py", "Databento history downloader",
                     include=("Data source (Databento)",))
    p.add_argument("--set", dest="overrides", action="append", default=[],
                   metavar="PATH=VALUE")
    a = p.parse_args(argv)

    try:
        cfg = apply_options(AppConfig.load(a.config), a)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    d = cfg.databento
    for field in ("start", "end"):
        if not getattr(d, field):
            print(f"databento.{field} is required "
                  f"(config, or --download-{field})", file=sys.stderr)
            return 2

    print(f"Downloading {d.dataset} {d.symbols} {d.schema} "
          f"{d.start} .. {d.end} ...")
    print(f"Saved: {download_history(d)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
