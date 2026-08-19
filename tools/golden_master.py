#!/usr/bin/env python3
"""Behaviour lock: prove a refactor changed no trade.

    python tools/golden_master.py record      # before touching anything
    python tools/golden_master.py check       # after every change

`record` runs a fixed set of backtests and stores each one's trade list.
`check` runs the identical set again and compares, trade for trade, to the
cent. Any difference at all is reported and the exit code is non-zero.

This is the safety net for the multi-engine restructure. The requirement is
that the reorganisation changes no trading behaviour, and the only way to
support that claim is to have the trades from before and diff them.

What is covered
---------------
Both engines, single-session and multi-session, several timeframes, both news
modes, both reversal anchors, and every stop multiplier that maps onto an
original stop mode. The cases deliberately include the ones most likely to
break under a refactor: multi-session fan-out, a session spanning midnight, and
the New York window that stops before the contract roll.

The comparison is on the trade fields that describe BEHAVIOUR — entry, exit,
direction, levels, P&L — not on formatting, ordering of columns, or anything
cosmetic, so a report change does not raise a false alarm.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                            # noqa: E402

from orb.backtest import make_clock, run_backtest              # noqa: E402
from orb.config import AppConfig                               # noqa: E402
from orb.data.dbn import load_dbn_bars                         # noqa: E402
from orb.logger import RbeaLogger                              # noqa: E402
from orb.report import trades_dataframe                        # noqa: E402

#: the default configuration — the orb engine's own
#: master config. There is no parent config file any more.
ENGINE_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "orb", "engines", "orb", "config.yaml")

#: Anchored to the repo like ENGINE_CONFIG above, not to the working directory.
#: The point of this tool is that two runs on different days compare equal, so
#: it must read the same data and write the same folder wherever it is invoked
#: from — a relative path here made `check` fail from any other directory.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DIR = os.path.join(REPO, "golden_master")
DATA = os.path.join(REPO, "data", "gc_1m_merged.parquet")
START, END = "2026-01-01", "2026-08-13"

# the fields that describe what the strategy DID — everything else is cosmetic
FIELDS = ["session_name", "session", "trade_in_session", "direction", "lots",
          "entry_time", "entry_price", "sl", "tp", "exit_time", "exit_price",
          "exit_reason", "range_high", "range_low", "range_mid",
          "gross_profit", "net_profit", "r_multiple"]

# name -> (open time New York, session that opens next)
WINDOWS = {"ASIA": ("19:00", "03:00"), "LONDON": ("03:00", "09:30"),
           "NEW_YORK": ("09:30", "16:55")}


def _hhmm_plus(hhmm: str, minutes: int) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    t = (h * 60 + m + minutes) % 1440
    return f"{t // 60:02d}:{t % 60:02d}"


# --------------------------------------------------------------------------
def _breakout(base: AppConfig, session: str, tf: str, orb: int, rr: float,
              sl_mode: str, news: str):
    import copy
    cfg = copy.deepcopy(base)
    open_t, stop_t = WINDOWS[session]
    s = cfg.use_single_session(f"{session}")
    s.signal_timeframe = tf
    s.range_start, s.range_end, s.stop_time = open_t, _hhmm_plus(open_t, orb), stop_t
    s.risk_reward, s.sl_mode = float(rr), sl_mode
    s.max_trades_per_session = 0
    s.log_level = "none"
    for _k, _l, cat in s.news.items():
        cat.mode = news
    s.news_days, s.news_trading = "", news
    return cfg


def _multi(base: AppConfig, tf: str = "M5", instrument: str = "gc"):
    """All three WINDOWS at once, on one instrument — the fan-out path, the one
    most at risk.

    Scoped to a single instrument on purpose. The bars here come from
    `load_dbn_bars`, which loads one file and cannot tag what it loads, so a
    run spanning several instruments would have no way to route them. It is
    also what the case has always meant: Asia, London and New York competing
    for one position slot on one account.

    Once a config declares a (session x instrument) matrix, "every session" is
    every CELL — three windows times three symbols. Enabling all nine would
    change the case into something else and quietly need two data files it was
    never given.
    """
    import copy
    cfg = copy.deepcopy(base)
    for name, sess in cfg.sessions.items():
        sess.enabled = (sess.instrument or instrument) == instrument
        sess.signal_timeframe = tf
        sess.log_level = "none"
    cfg.validate_sessions()
    return cfg


CASES = []


def build_cases(base: AppConfig):
    """(case name, how to run it). Kept explicit rather than generated, so the
    set cannot silently change between record and check."""
    out = []

    # --- breakout, single session -----------------------------------------
    for session in ("ASIA", "LONDON", "NEW_YORK"):
        for tf in ("M1", "M5", "M15"):
            out.append((f"breakout_{session}_{tf}_ORB15_RR2_mid",
                        ("breakout", dict(session=session, tf=tf, orb=15, rr=2.0,
                                          sl_mode="mid_range", news="off"))))
    out.append(("breakout_LONDON_M5_ORB30_RR4_full",
                ("breakout", dict(session="LONDON", tf="M5", orb=30, rr=4.0,
                                  sl_mode="full_range", news="off"))))
    out.append(("breakout_LONDON_M5_ORB15_RR2_mid_newson",
                ("breakout", dict(session="LONDON", tf="M5", orb=15, rr=2.0,
                                  sl_mode="mid_range", news="on"))))
    out.append(("breakout_ASIA_M5_ORB60_RR1_full",
                ("breakout", dict(session="ASIA", tf="M5", orb=60, rr=1.0,
                                  sl_mode="full_range", news="on"))))

    # --- breakout, all three sessions in one run --------------------------
    out.append(("breakout_MULTI_M5", ("multi", dict(tf="M5"))))
    out.append(("breakout_MULTI_M15", ("multi", dict(tf="M15"))))

    # --- reversal ----------------------------------------------------------
    for mult in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0):
        out.append((f"reversal_LONDON_M5_SL{mult:g}_RR2_RR".replace(".", "p"),
                    ("reversal", dict(session="LONDON", tf="M5", orb=15, rr=2.0,
                                      mult=mult, cap=2, anchor="range",
                                      direction="reverse", news="off"))))
    out.append(("reversal_LONDON_M5_SL0p5_RR2_RR_mirror",
                ("reversal", dict(session="LONDON", tf="M5", orb=15, rr=2.0,
                                  mult=0.5, cap=2, anchor="mirror",
                                  direction="reverse", news="off"))))
    out.append(("reversal_LONDON_M15_SL0p75_RR2_RRR",
                ("reversal", dict(session="LONDON", tf="M15", orb=15, rr=2.0,
                                  mult=0.75, cap=3, anchor="range",
                                  direction="reverse", news="off"))))
    out.append(("reversal_forward_LONDON_M5_SL0p5_RR2",
                ("reversal", dict(session="LONDON", tf="M5", orb=15, rr=2.0,
                                  mult=0.5, cap=0, anchor="range",
                                  direction="forward", news="off"))))
    out.append(("reversal_NEW_YORK_M5_SL1_RR2_R",
                ("reversal", dict(session="NEW_YORK", tf="M5", orb=15, rr=2.0,
                                  mult=1.0, cap=1, anchor="range",
                                  direction="reverse", news="on"))))
    return out


def run_case(base: AppConfig, bars, kind: str, kw: dict) -> pd.DataFrame:
    if kind == "breakout":
        cfg = _breakout(base, kw["session"], kw["tf"], kw["orb"], kw["rr"],
                        kw["sl_mode"], kw["news"])
        res = run_backtest(cfg, bars, RbeaLogger(level=0))
    elif kind == "multi":
        res = run_backtest(_multi(base, kw["tf"]), bars, RbeaLogger(level=0))
    elif kind == "reversal":
        # The case is declared by INTENT — multiplier, direction, anchor, cap —
        # and translated to whatever the current API is. What is being compared
        # is the trades, not the spelling of the call.
        from orb.engines.orb_reverse import OrbReverseSettings
        cfg = _breakout(base, kw["session"], kw["tf"], kw["orb"], kw["rr"],
                        "mid_range", kw["news"])
        settings = OrbReverseSettings(sl_range_mult=kw["mult"],
                                      direction=kw["direction"],
                                      sl_anchor=kw["anchor"])
        settings.apply_to(cfg)
        for session in cfg.enabled_sessions():
            session.max_trades_per_session = int(kw["cap"])
        res = run_backtest(cfg, bars, RbeaLogger(level=0))
    else:
        raise ValueError(kind)
    df = trades_dataframe(res.trades)
    if df.empty:
        return pd.DataFrame(columns=FIELDS)
    for c in FIELDS:
        if c not in df.columns:
            df[c] = None
    df = df[FIELDS].copy()
    for c in ("entry_price", "sl", "tp", "exit_price", "range_high",
              "range_low", "range_mid", "gross_profit", "net_profit",
              "r_multiple", "lots"):
        df[c] = pd.to_numeric(df[c], errors="coerce").round(6)
    return df


def digest(df: pd.DataFrame) -> str:
    return hashlib.sha256(
        df.to_csv(index=False).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["record", "check", "list"])
    p.add_argument("--config", "-c", default=ENGINE_CONFIG)
    p.add_argument("--data", "-d", nargs="+", default=[DATA])
    p.add_argument("--dir", default=DEFAULT_DIR)
    p.add_argument("--start", default=START)
    p.add_argument("--end", default=END)
    a = p.parse_args()

    base = AppConfig.load(a.config)
    base.backtest.dbn_paths = a.data
    base.server_timezone = "America/New_York"
    base.server_utc_offset_hours = 0
    base.strategy.log_level = "none"

    cases = build_cases(base)
    if a.mode == "list":
        for name, (kind, _) in cases:
            print(f"  {kind:<9} {name}")
        print(f"\n{len(cases)} cases")
        return 0

    d = base.databento
    print(f"Loading bars ...", flush=True)
    bars = load_dbn_bars(a.data, make_clock(base),
                         contract_mode=d.contract_mode,
                         contract_symbol=d.contract_symbol,
                         include_spreads=d.include_spreads,
                         roll_min_volume=d.roll_min_volume,
                         roll_boundary_hour=d.roll_boundary_hour,
                         start=a.start, end=a.end, logger=RbeaLogger(level=0))
    print(f"  {len(bars):,} bars  {bars[0].time:%Y-%m-%d %H:%M} .. "
          f"{bars[-1].time:%Y-%m-%d %H:%M}\n", flush=True)

    os.makedirs(a.dir, exist_ok=True)
    man_path = os.path.join(a.dir, "manifest.json")

    if a.mode == "record":
        man = {"start": a.start, "end": a.end, "bars": len(bars), "cases": {}}
        for i, (name, (kind, kw)) in enumerate(cases, 1):
            df = run_case(base, bars, kind, kw)
            df.to_csv(os.path.join(a.dir, f"{name}.csv"), index=False)
            man["cases"][name] = {"kind": kind, "trades": len(df),
                                  "digest": digest(df),
                                  "net": round(float(df.net_profit.sum()), 2)
                                  if len(df) else 0.0}
            print(f"[{i:>2}/{len(cases)}] {name:<46} {len(df):>4} trades  "
                  f"{man['cases'][name]['digest']}")
        json.dump(man, open(man_path, "w"), indent=1)
        print(f"\nRecorded {len(cases)} cases to {a.dir}/")
        print("Re-run with `check` after any change.")
        return 0

    # --- check -------------------------------------------------------------
    if not os.path.exists(man_path):
        print(f"No baseline at {man_path}. Run `record` first.", file=sys.stderr)
        return 2
    man = json.load(open(man_path))
    bad, missing = [], []
    for i, (name, (kind, kw)) in enumerate(cases, 1):
        ref_path = os.path.join(a.dir, f"{name}.csv")
        if name not in man["cases"] or not os.path.exists(ref_path):
            missing.append(name)
            print(f"[{i:>2}/{len(cases)}] {name:<46} NO BASELINE")
            continue
        got = run_case(base, bars, kind, kw)
        want_digest = man["cases"][name]["digest"]
        got_digest = digest(got)
        if got_digest == want_digest:
            print(f"[{i:>2}/{len(cases)}] {name:<46} same  {got_digest}")
            continue
        bad.append(name)
        want = pd.read_csv(ref_path)
        print(f"[{i:>2}/{len(cases)}] {name:<46} CHANGED")
        print(f"        trades {len(want)} -> {len(got)}")
        wn = round(float(want.net_profit.sum()), 2) if len(want) else 0.0
        gn = round(float(got.net_profit.sum()), 2) if len(got) else 0.0
        print(f"        net    {wn:,.2f} -> {gn:,.2f}")
        if len(want) == len(got) and len(want):
            for c in FIELDS:
                w, g = want[c].astype(str), got[c].astype(str)
                diff = (w != g)
                if diff.any():
                    j = int(diff.values.argmax())
                    print(f"        first difference in '{c}' at row {j}: "
                          f"{w.iloc[j]!r} -> {g.iloc[j]!r}")
                    break

    print("\n" + "=" * 62)
    if bad or missing:
        if bad:
            print(f"  {len(bad)} case(s) CHANGED — behaviour is not identical")
            for n in bad:
                print(f"     {n}")
        if missing:
            print(f"  {len(missing)} case(s) have no baseline")
            for n in missing:
                print(f"     {n}")
        print("=" * 62)
        return 1
    print(f"  all {len(cases)} cases identical — behaviour unchanged")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
