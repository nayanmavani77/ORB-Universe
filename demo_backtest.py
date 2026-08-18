#!/usr/bin/env python3
"""Smoke test / demo — runs the whole pipeline on synthetic random-walk bars.

No Databento key and no data files needed.  Use it to check the install, see
the journal output and look at the report layout before you point the engine
at real DBN files.

    python demo_backtest.py --days 180
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta

from orb.backtest import run_backtest
from orb.bars import Bar
from orb.config import AppConfig
from orb.report import print_summary, write_report

#: the default configuration — the orb engine's own
#: master config. There is no parent config file any more.
ENGINE_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "orb", "engines", "orb", "config.yaml")


def synth_bars(days: int, seed: int = 7, start=datetime(2024, 1, 1),
               price: float = 4700.0) -> list:
    rnd = random.Random(seed)
    bars, day = [], start
    made = 0
    while made < days:
        if day.weekday() >= 5:                     # skip weekends
            day += timedelta(days=1)
            continue
        t = day.replace(hour=8)
        end = day.replace(hour=18)
        # gentle intraday drift + noise, tick size 0.25
        drift = rnd.gauss(0, 0.15)
        while t < end:
            step = rnd.gauss(drift * 0.02, 0.9)
            price = max(100.0, price + step)
            rng = abs(rnd.gauss(0, 0.8)) + 0.25
            o = price
            c = price + rnd.gauss(0, 0.4)
            h = max(o, c) + rng
            l = min(o, c) - rng
            q = lambda x: round(x * 4) / 4.0       # noqa: E731
            bars.append(Bar(t, q(o), q(h), q(l), q(c), rnd.randint(50, 900)))
            price = c
            t += timedelta(minutes=1)
        day += timedelta(days=1)
        made += 1
    return bars


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--config", default=ENGINE_CONFIG)
    p.add_argument("--seed", type=int, default=7)
    a = p.parse_args()

    cfg = AppConfig.load(a.config)
    cfg.backtest.report_name = "demo_report"
    bars = synth_bars(a.days, seed=a.seed)
    result = run_backtest(cfg, bars)
    print_summary(result)
    for k, v in write_report(result).items():
        print(f"  {k:<14} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
