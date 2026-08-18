#!/usr/bin/env python3
"""Run the full 54-configuration ORB test matrix.

    3 timeframes  x  3 sessions  x  3 ORB durations  x  2 news modes  =  54

Every run uses the UNCHANGED engine (`orb.backtest.run_backtest`) and the
unchanged strategy. Only configuration differs between runs, so the results are
directly comparable.

    python tools/run_matrix.py --data data/gc_1m_merged.parquet \
        --start 2026-01-01 --end 2026-08-13 --out matrix_out

Session definitions — all in America/New_York, DST-aware
--------------------------------------------------------
    Asia       19:00 ET   -> trades until London opens   (03:00 next day)
    London     03:00 ET   -> trades until New York opens (09:30 same day)
    New York   09:30 ET   -> trades until Asia opens     (19:00 same day)

Each session builds its opening range from its open, then trades from the end
of that range until the next session opens. That is expressed with the engine's
existing `stop_time` — set to the next session's opening time — so no strategy
code is involved.

News modes
----------
    include_news : news_trading = "on"   (every open market day is traded)
    skip_news    : news_trading = "off"  (a listed date is removed entirely)

The skip is at DAY level: the engine's news check tests both the session's own
date and the date the signal fires on, so a session that spans midnight is
blocked if EITHER date is listed. A date listed as a news day therefore removes
the Asia, London and New York sessions touching it.

Output
------
One folder per configuration, plus a `_summary` folder with the cross-run
comparisons.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                          # noqa: E402

from orb.backtest import make_clock, run_backtest            # noqa: E402
from orb.config import AppConfig                             # noqa: E402
from orb.data.dbn import load_dbn_bars                       # noqa: E402
from orb.logger import RbeaLogger, parse_log_level           # noqa: E402
from orb.report import compute_stats, write_report           # noqa: E402

#: the default configuration — the orb engine's own
#: master config. There is no parent config file any more.
ENGINE_CONFIG = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "orb", "engines", "orb", "config.yaml")

# --------------------------------------------------------------------------
TIMEFRAMES = ["M1", "M5", "M15"]
ORB_MINUTES = [15, 30, 60]
# risk:reward multiples, 1:1 .. 1:5
RR_VALUES = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]
NEWS_MODES = [("INCLUDE_NEWS", "on"), ("SKIP_NEWS", "off")]

# name -> (open time in New York, the session that opens next)
SESSIONS = {
    "ASIA":     ("19:00", "LONDON"),
    "LONDON":   ("03:00", "NEW_YORK"),
    "NEW_YORK": ("09:30", "ASIA"),
}
SESSION_ORDER = ["ASIA", "LONDON", "NEW_YORK"]

# A session normally trades until the next one opens.  New York is the
# exception: the next session (Asia) opens at 19:00, but the contract rolls at
# 18:00 — the CME open — so trading to 19:00 would leave a position sitting
# across the instrument change and take the whole calendar spread as a fake
# move.  16:55 stops New York before the 17:00 COMEX halt, which is where the
# position should be flat anyway.
SESSION_STOP_OVERRIDE = {
    "NEW_YORK": "16:55",
}


def _light_outputs(result, folder: str, name: str) -> None:
    """Trades + stats + breakdown CSVs, without the HTML report or journal."""
    from orb.report import breakdowns, trades_dataframe
    df = trades_dataframe(result.trades)
    df.to_csv(os.path.join(folder, f"{name}_trades.csv"), index=False)
    bd = breakdowns(df)
    for k, fn in (("daily", "daily_pnl"), ("monthly", "monthly_pnl"),
                  ("hourly", "hourly_pnl"), ("weekday", "weekday_pnl")):
        bd[k].to_csv(os.path.join(folder, f"{name}_{fn}.csv"))
    st = compute_stats(result)
    pd.Series({k: v for k, v in st.items() if not isinstance(v, dict)}).to_csv(
        os.path.join(folder, f"{name}_stats.csv"), header=False)


def add_minutes(hhmm: str, minutes: int) -> str:
    return (datetime.strptime(hhmm, "%H:%M")
            + timedelta(minutes=minutes)).strftime("%H:%M")


def build_configs(base: AppConfig, news_dates: str, rr_values):
    """The configuration grid, in the order the specification lists it."""
    out = []
    for tf in TIMEFRAMES:
        for news_label, news_mode in NEWS_MODES:
            for session in SESSION_ORDER:
                open_time, next_session = SESSIONS[session]
                for orb in ORB_MINUTES:
                  for rr in rr_values:
                    rr_tag = f"RR{rr:g}".replace(".", "p")
                    name = f"{tf}_{session}_ORB{orb}_{news_label}_{rr_tag}" \
                        if len(rr_values) > 1 else \
                        f"{tf}_{session}_ORB{orb}_{news_label}"
                    cfg = copy.deepcopy(base)
                    # this tool generates its own window, so it must own the
                    # single session — otherwise a `sessions:` block in the
                    # config would silently replace the permutation
                    s = cfg.use_single_session(name)
                    s.risk_reward = float(rr)
                    s.signal_timeframe = tf
                    s.range_start = open_time
                    s.range_end = add_minutes(open_time, orb)
                    # trade until the next session opens, unless this session
                    # has to stop earlier to stay clear of the contract roll
                    s.stop_time = SESSION_STOP_OVERRIDE.get(
                        session, SESSIONS[next_session][0])
                    # Set EVERY news bucket explicitly. The per-category
                    # dates live in the config; this run decides only how
                    # they are applied. Leaving a category at its config
                    # mode would filter the INCLUDE half too — the whole
                    # point of INCLUDE is that no date is excluded.
                    for _k, _l, cat in s.news.items():
                        cat.mode = news_mode
                    s.news_days = ""          # categories carry the dates
                    s.news_trading = news_mode
                    s.log_level = "normal"
                    cfg.server_timezone = "America/New_York"
                    cfg.server_utc_offset_hours = 0
                    out.append({
                        "name": name, "timeframe": tf, "session": session,
                        "orb_minutes": orb, "news_mode": news_label,
                        "risk_reward": float(rr),
                        "range_start": s.range_start, "range_end": s.range_end,
                        "stop_time": s.stop_time, "cfg": cfg,
                    })
    return out


# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="Run the 54-configuration ORB matrix")
    p.add_argument("--config", "-c", default=ENGINE_CONFIG)
    p.add_argument("--data", "-d", nargs="+", default=None,
                   help="bar data (overrides the config)")
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="2026-08-13")
    p.add_argument("--out", default="matrix_out")
    p.add_argument("--news-days", default=None,
                   help="News Days list, or a path to a file of dates")
    p.add_argument("--only", default=None,
                   help="run only configurations whose name contains this")
    p.add_argument("--rr", default=None,
                   help="risk:reward values, comma separated (default 2.0 only; "
                        "use '1:5' or '1,1.5,...,5' to sweep)")
    p.add_argument("--light", action="store_true",
                   help="skip the per-run HTML report and journal — for large "
                        "sweeps where only the CSVs and the summary matter")
    a = p.parse_args()

    base = AppConfig.load(a.config)
    if a.data:
        base.backtest.dbn_paths = a.data

    # --- news dates -------------------------------------------------------
    news_dates = a.news_days or ""
    if news_dates and os.path.exists(news_dates):
        news_dates = open(news_dates, encoding="utf-8").read()
    if not news_dates.strip():
        # fall back to whatever the config already holds (categories + general)
        from orb.timeutils import NewsDays
        parts = [c.dates for _k, _l, c in base.strategy.news.items() if c.dates]
        if base.strategy.news_days:
            parts.append(base.strategy.news_days)
        news_dates = ",".join(parts)
    from orb.timeutils import NewsDays
    n_news = len(NewsDays(news_dates))

    os.makedirs(a.out, exist_ok=True)

    # --- data, loaded ONCE and reused by all 54 runs -----------------------
    print("Loading bars ...")
    clock = make_clock(AppConfig.from_dict(
        {"server_timezone": "America/New_York",
         "strategy": {}, "symbol": {}, "databento": {}, "backtest": {}, "mt5": {}}))
    d = base.databento
    bars = load_dbn_bars(base.backtest.dbn_paths, clock,
                         contract_mode=d.contract_mode,
                         contract_symbol=d.contract_symbol,
                         include_spreads=d.include_spreads,
                         roll_min_volume=d.roll_min_volume,
                         roll_boundary_hour=d.roll_boundary_hour,
                         start=a.start, end=a.end, logger=RbeaLogger(level=0))
    print(f"  {len(bars):,} bars  {bars[0].time:%Y-%m-%d %H:%M} .. "
          f"{bars[-1].time:%Y-%m-%d %H:%M}  (New York time)\n")

    # --- risk:reward values ----------------------------------------------
    if not a.rr:
        rr_values = [base.strategy.risk_reward]
    elif ":" in a.rr:                      # "1:5" -> 1.0 .. 5.0 in 0.5 steps
        lo, hi = (float(x) for x in a.rr.split(":"))
        rr_values, v = [], lo
        while v <= hi + 1e-9:
            rr_values.append(round(v, 3))
            v += 0.5
    else:
        rr_values = [float(x) for x in a.rr.split(",")]

    configs = build_configs(base, news_dates, rr_values)
    if a.only:
        configs = [c for c in configs if a.only.upper() in c["name"].upper()]

    if n_news == 0:
        print("!! No News Days are configured, so every SKIP_NEWS run will be")
        print("   identical to its INCLUDE_NEWS twin. The folders and the")
        print("   comparison are still produced; add dates and re-run to make")
        print("   the news comparison meaningful.\n")

    # --- run --------------------------------------------------------------
    rows = []
    for i, item in enumerate(configs, 1):
        folder = os.path.join(a.out, item["name"])
        os.makedirs(folder, exist_ok=True)
        cfg = item["cfg"]
        cfg.backtest.out_dir = folder
        cfg.backtest.report_name = item["name"]
        if a.light:
            cfg.strategy.log_file = None
            log = RbeaLogger(level=0, stream=open(os.devnull, "w"))
        else:
            cfg.strategy.log_file = os.path.join(folder, "journal.log")
            if os.path.exists(cfg.strategy.log_file):
                os.remove(cfg.strategy.log_file)
            log = RbeaLogger(level=parse_log_level("normal"),
                             file_path=cfg.strategy.log_file,
                             show_time=True, stream=open(os.devnull, "w"))
        result = run_backtest(cfg, bars, logger=log)
        log.close()
        stats = compute_stats(result)
        if a.light:
            _light_outputs(result, folder, item["name"])
        else:
            write_report(result)

        # the exact configuration this run used, saved beside its results
        with open(os.path.join(folder, "config_used.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({k: v for k, v in item.items() if k != "cfg"} |
                      {"config": cfg.to_dict()}, fh, indent=2, default=str)

        row = {
            "config": item["name"], "timeframe": item["timeframe"],
            "session": item["session"], "orb_minutes": item["orb_minutes"],
            "news_mode": item["news_mode"],
            "risk_reward": item["risk_reward"],
            "range_start": item["range_start"], "range_end": item["range_end"],
            "stop_time": item["stop_time"],
            "trades": stats["total_trades"], "wins": stats["wins"],
            "losses": stats["losses"], "win_rate": stats["win_rate"],
            "net_profit": stats["net_profit"], "profit_factor": stats["profit_factor"],
            "expectancy": stats["expectancy"], "max_dd_money": stats["max_dd_money"],
            "max_dd_pct": stats["max_dd_pct"],
            "max_consec_wins": stats["max_consecutive_wins"],
            "max_consec_losses": stats["max_consecutive_losses"],
            "avg_r": stats["avg_r"], "total_r": stats["total_r"],
            "recovery_factor": stats["recovery_factor"],
            "long_net": stats["long_net"], "short_net": stats["short_net"],
            "avg_duration_min": stats["avg_duration_min"],
            "folder": folder,
        }
        rows.append(row)
        if i % max(1, len(configs) // 40) == 0 or len(configs) <= 60:
            print(f"[{i:>3}/{len(configs)}] {item['name']:<44} "
                  f"trades {row['trades']:>4}  net {row['net_profit']:>11,.0f}  "
                  f"PF {row['profit_factor']:.2f}")

    # --- summary ----------------------------------------------------------
    summary_dir = os.path.join(a.out, "_summary")
    os.makedirs(summary_dir, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(os.path.join(summary_dir, "all_results.csv"), index=False)

    def agg(by):
        g = df.groupby(by)
        t = pd.DataFrame({
            "configs": g.size(),
            "trades": g["trades"].sum(),
            "net_profit": g["net_profit"].sum(),
            "avg_net_per_config": g["net_profit"].mean(),
            "median_profit_factor": g["profit_factor"].median(),
            "avg_win_rate": g["win_rate"].mean(),
            "worst_dd_pct": g["max_dd_pct"].max(),
            "configs_profitable": g["net_profit"].apply(lambda x: int((x > 0).sum())),
        })
        return t.sort_values("net_profit", ascending=False)

    for by, fname in (("timeframe", "by_timeframe"), ("session", "by_session"),
                      ("orb_minutes", "by_orb_duration"),
                      ("news_mode", "by_news_mode"),
                      ("risk_reward", "by_risk_reward"),
                      (["timeframe", "session"], "by_timeframe_session"),
                      (["session", "risk_reward"], "by_session_rr"),
                      (["timeframe", "risk_reward"], "by_timeframe_rr")):
        agg(by).to_csv(os.path.join(summary_dir, f"{fname}.csv"))

    # best R:R for each of the 54 base configurations
    if len(rr_values) > 1:
        d = df.copy()
        d["base"] = (d.timeframe + "_" + d.session + "_ORB"
                     + d.orb_minutes.astype(str) + "_" + d.news_mode)
        best = d.loc[d.groupby("base")["net_profit"].idxmax()]
        best[["base", "risk_reward", "trades", "net_profit", "profit_factor",
              "win_rate", "max_dd_pct", "avg_r"]].sort_values(
            "net_profit", ascending=False).to_csv(
            os.path.join(summary_dir, "best_rr_per_config.csv"), index=False)

    # news effect: pair each INCLUDE with its SKIP twin
    key = ["timeframe", "session", "orb_minutes", "risk_reward"]
    inc = df[df.news_mode == "INCLUDE_NEWS"].set_index(key)
    skp = df[df.news_mode == "SKIP_NEWS"].set_index(key)
    cols = ["trades", "net_profit", "profit_factor", "win_rate", "max_dd_pct",
            "max_consec_losses", "avg_r"]
    eff = inc[cols].join(skp[cols], lsuffix="_include", rsuffix="_skip")
    for c in cols:
        eff[f"delta_{c}"] = eff[f"{c}_skip"] - eff[f"{c}_include"]
    eff.to_csv(os.path.join(summary_dir, "news_effect.csv"))

    print(f"\nWrote {len(rows)} configuration folder(s) under {a.out}/")
    print(f"Summary tables in {summary_dir}/")
    with open(os.path.join(summary_dir, "run_info.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"period_start": a.start, "period_end": a.end,
                   "bars": len(bars), "configurations": len(rows),
                   "news_days_configured": n_news,
                   "risk_reward_values": rr_values,
                   "news_dates": news_dates,
                   "sessions": {k: {"open_ny": v[0], "next": v[1]}
                                for k, v in SESSIONS.items()},
                   "engine": "orb.backtest.run_backtest (unchanged)"},
                  fh, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
