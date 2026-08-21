#!/usr/bin/env python3
"""Run ONE backtest of any engine, and write its report.

    python tools/backtest.py --engine orb
    python tools/backtest.py --engine orb_reverse
    python tools/backtest.py --engine orb,orb_reverse     <- both, one account

Everything comes from that engine's own config file —
`orb/engines/<engine>/config.yaml` — which is the COMPLETE configuration for
it. There is no parent config. Edit that one file and run with no other
arguments.

Naming several engines merges their sessions onto one account: each session
runs the engine of the file it is written in. The blocks a run has only one of
— instrument, data, account, clock — must match across those files, and a
mismatch is reported rather than silently resolved.

Every setting is also a flag, and a flag OVERRIDES the file, for trying one
value without editing anything:

    python tools/backtest.py --engine orb_reverse --sl-mult 1.5
    python tools/backtest.py --engine orb_reverse --set direction=forward
    python tools/backtest.py --engine orb --session LONDON --rr 3

Results go to `backtest/<engine>/<run-name>/`, or
`backtest/mixed/<engine>_<engine>/` when several engines are merged.

One tool for every engine. Adding an engine does not mean writing another
script; it means dropping a `config.yaml` next to the strategy.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orb.backtest import (load_instrument_bars, make_clock,       # noqa: E402
                          run_backtest)
from orb.logger import RbeaLogger, parse_log_level                # noqa: E402
from orb.outputs import resolve as resolve_out                    # noqa: E402
from orb.report import compute_stats, write_report                # noqa: E402
from orb.runconfig import RunConfig, available, merge, merged_dates  # noqa: E402


def _coerce(text: str):
    """`0.75` -> float, `3` -> int, `true` -> bool, anything else -> str."""
    low = str(text).strip().lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def _execute(app, name, out, start, end, level, header) -> int:
    """Load the bars, run, report. Shared by the single- and merged-engine
    paths so both behave identically."""
    os.makedirs(out, exist_ok=True)
    app.backtest.out_dir = out
    app.backtest.report_name = name
    log_path = os.path.join(out, f"{name}_journal.log")
    if os.path.exists(log_path):
        os.remove(log_path)
    for session in app.enabled_sessions():
        session.log_file = log_path
    app.strategy.log_file = log_path

    print("Loading bars ...")
    bars = load_instrument_bars(app, make_clock(app), start=start, end=end,
                                logger=RbeaLogger(level=0))
    print(f"  {len(bars):,} bars  {bars[0].time:%Y-%m-%d %H:%M} .. "
          f"{bars[-1].time:%Y-%m-%d %H:%M}  (New York time)\n")

    log = RbeaLogger(level=parse_log_level(level), file_path=log_path,
                     show_time=True)
    result = run_backtest(app, bars, logger=log)
    log.close()

    stats = compute_stats(result)
    write_report(result)

    print("=" * 68)
    print(f"  {name}")
    print("=" * 68)
    for key in ("total_trades", "wins", "losses", "win_rate", "net_profit",
                "profit_factor", "expectancy", "max_dd_money", "max_dd_pct",
                "avg_r", "total_r", "recovery_factor"):
        value = stats[key]
        print(f"  {key:<18} {value:>14,.2f}" if isinstance(value, (int, float))
              else f"  {key:<18} {value:>14}")
    by_session = {}
    for t in result.trades:
        by_session.setdefault(t.session_name, [0, 0.0])
        by_session[t.session_name][0] += 1
        by_session[t.session_name][1] += t.net_profit
    if len(by_session) > 1:
        print("\n  per session")
        for sess, (n, net) in sorted(by_session.items()):
            engine = next((s.engine for s in app.enabled_sessions()
                           if (s.name or "MAIN") == sess), "?")
            print(f"    {sess:<14} [{engine:<12}] {n:>4} trades  "
                  f"net {net:>12,.2f}")
    print(f"\n  Report, trades CSV and journal in {out}/")
    print(f"  {header}")
    return 0


#: Flags `_run_merged` understands. Everything else in the "run overrides" and
#: "engine option overrides" groups is per-ENGINE, and a merged run has more
#: than one engine — so there is no honest answer to "which engine does
#: `--rr 3` apply to?". Rather than pick one silently, a merged run rejects
#: them and says so. (`--out` is handled separately; it names the output
#: folder, which a merged run does have exactly one of.)
#: `--instruments` IS honoured in a merged run: it selects instruments, which
#: every engine in the run shares, unlike the per-engine knobs below.
MERGED_OK = ("start", "end", "data", "log_level", "out", "instruments")

#: Human-facing flag names, for the error message above. Keyed by the argparse
#: destination so the two lists can never drift apart.
FLAG_NAMES = {
    "session": "--session", "signal_timeframe": "--tf", "orb_minutes": "--orb",
    "risk_reward": "--rr", "lots": "--lots", "news": "--news",
    "instruments": "--instruments",
    "max_trades_per_session": "--max-trades",
    "pullback_entry": "--pullback",
    "breakeven": "--breakeven", "breakeven_trigger_r": "--breakeven-trigger",
    "options": "--set", "sl_mult": "--sl-mult",
    "forward": "--forward", "reverse": "--reverse",
}


def _reject_per_engine_flags(a, engines) -> list:
    """Which per-engine flags did the user pass to a merged run?

    Returns the flag names, so the caller can refuse with a message naming
    them. A merged run that quietly ignored `--rr 3` would report results the
    user did not ask for, which is worse than not running at all.
    """
    used = []
    for dest, flag in FLAG_NAMES.items():
        if dest in MERGED_OK:
            continue
        value = getattr(a, dest, None)
        if value:                      # [] and False and None all mean "not set"
            used.append(flag)
    return sorted(used)


def _run_merged(a, engines) -> int:
    """Several engines, one account, one pass over the bars.

    Each session keeps the engine of the config file it is written in, so the
    per-engine override flags have no single meaning here — see MERGED_OK.
    """
    blocked = _reject_per_engine_flags(a, engines)
    if blocked:
        verb = "applies" if len(blocked) == 1 else "apply"
        print(f"{', '.join(blocked)} {verb} to ONE engine, but this run merges "
              f"{len(engines)} ({', '.join(engines)}).\n"
              f"Set the value in that engine's config.yaml instead, or run the "
              f"engine on its own:\n"
              f"    python tools/backtest.py --engine {engines[0]} ...",
              file=sys.stderr)
        return 2
    configs = RunConfig.load_many(engines)
    app = merge(configs)                     # raises on a shared-block mismatch
    if a.news_check:
        return news_check(app)
    if a.instruments:
        app.select_instruments(a.instruments)
    start, end = merged_dates(configs)
    if a.start:
        start = a.start
    if a.end:
        end = a.end
    if a.data:
        app.backtest.dbn_paths = a.data
    level = a.log_level or "normal"
    for session in app.enabled_sessions():
        session.log_level = level

    name = "_".join(engines)
    out = a.out or os.path.join("backtest", "mixed", name)

    print("=" * 68)
    print(f"  BACKTEST — {', '.join(engines)}  (merged, one account)")
    print("=" * 68)
    for rc in configs:
        print(f"  {rc.engine:<14} {os.path.relpath(rc.path)}")
    for session in app.enabled_sessions():
        print(f"  session        {(session.name or 'MAIN'):<12} "
              f"[{session.engine:<12}] {session.range_start}-"
              f"{session.range_end} -> {session.stop_time}  magic "
              f"{session.magic}")
    print(f"  period         {start} .. {end}")
    print(f"  output         {out}/")
    print("=" * 68 + "\n")
    if a.show:
        return 0
    return _execute(app, name, out, start, end, level,
                    "Each engine is configured in its own config.yaml.")


def news_check(app) -> int:
    """Is the news filter still able to block anything?

    A news calendar is a finite list of dates typed into the config. When the
    clock passes the last of them the filter stays switched on, keeps
    announcing its categories, and blocks nothing — there is simply no date
    left to match. Nothing fails, so nothing tells you. This prints the one
    fact that answers it: how many dates are still ahead.
    """
    import datetime as _dt
    from orb.timeutils import NewsDays

    today = _dt.date.today()
    print("=" * 78)
    print(f"  NEWS FILTER — today is {today:%Y-%m-%d}")
    print("=" * 78)

    worst = None
    for session in app.enabled_sessions():
        rows = []
        for _key, label, cat in session.news.items():
            rows.append((label, NewsDays(cat.dates), cat.mode))
        rows.append(("News Days (general)", NewsDays(session.news_days),
                     session.news_trading))

        print(f"\n  {session.name or 'MAIN'}"
              + (f"   [{session.instrument}]" if session.instrument else ""))
        any_dates = False
        for label, dates, mode in rows:
            if not len(dates):
                continue
            any_dates = True
            last = dates.last_date
            ahead = sum(1 for frm, to in dates.ranges if to >= today)
            state = ("EXPIRED" if last < today else
                     f"{(last - today).days}d left")
            flag = "  <-- blocks nothing from here on" if ahead == 0 else ""
            print(f"    {label:<28} {mode.upper():<5} {len(dates):>3} entries  "
                  f"last {last:%Y-%m-%d}  {ahead:>3} ahead  {state}{flag}")
            if mode in ("off", "only"):
                worst = last if worst is None else max(worst, last)
        if not any_dates:
            print("    no dates configured — every day is tradeable")

    print("\n" + "=" * 78)
    if worst is None:
        print("  No OFF or ONLY category has any dates. The news filter cannot "
              "block\n  anything, whatever the modes say.")
    elif worst < today:
        print(f"  THE CALENDAR HAS EXPIRED. The latest blocking date anywhere "
              f"is\n  {worst:%Y-%m-%d}, {(today - worst).days} day(s) ago. "
              f"The filter is ON and is letting every\n  news day through. Add "
              f"the coming months' releases to the `news:` block.")
        return 1
    else:
        print(f"  Latest blocking date: {worst:%Y-%m-%d} "
              f"({(worst - today).days} day(s) from now).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--engine", "-e", default="orb",
                   help="which engine to run, or several comma-separated to "
                        "run them together on one account (default: %(default)s)")
    p.add_argument("--config", "-c", default=None,
                   help="an alternative config file for this engine")
    p.add_argument("--show", action="store_true",
                   help="print the settings that would run, then stop")

    g = p.add_argument_group("run overrides — omit to use the config file")
    g.add_argument("--news-check", action="store_true",
                   help="print every news category, how many dates it holds, "
                        "when it runs out and how many are still ahead — then "
                        "stop. A calendar that has expired filters NOTHING "
                        "while still looking configured, so this is the fast "
                        "way to see whether the filter can still bite.")
    g.add_argument("--instruments", "-i", default=None, metavar="A,B",
                   help="which instruments to trade this run, comma "
                        "separated, e.g. -i gc,es. Omit to trade every one "
                        "declared in the engine's `instruments:` block.")
    g.add_argument("--session", default=None)
    g.add_argument("--tf", dest="signal_timeframe", default=None)
    g.add_argument("--orb", dest="orb_minutes", type=int, default=None)
    g.add_argument("--rr", dest="risk_reward", type=float, default=None)
    g.add_argument("--lots", type=float, default=None)
    g.add_argument("--max-trades", dest="max_trades_per_session", type=int,
                   default=None, metavar="N",
                   help="cap trades per session for this run; 0 = unlimited. "
                        "A session field, so it does NOT go through --set.")
    g.add_argument("--breakeven", dest="breakeven", action="store_true",
                   default=None,
                   help="move the stop loss to the entry price once the trade "
                        "is far enough in front (see --breakeven-trigger). "
                        "A session field, so it does NOT go through --set.")
    g.add_argument("--no-breakeven", dest="breakeven", action="store_false",
                   help="leave the stop where it was placed for this run, "
                        "whatever the config says.")
    g.add_argument("--breakeven-trigger", dest="breakeven_trigger_r",
                   type=float, default=None, metavar="R",
                   help="how far in front the trade must be before the stop "
                        "moves to entry, as a multiple of its own risk. "
                        "1.0 = 1:1. Only used with --breakeven.")
    g.add_argument("--pullback", dest="pullback_entry", action="store_true",
                   default=None,
                   help="enter on a PULLBACK to the level that broke, instead "
                        "of on the breakout close: long on a touch of the "
                        "range high, short on a touch of the range low. A "
                        "touch is enough and it fires during the forming bar. "
                        "A session field, so it does NOT go through --set.")
    g.add_argument("--no-pullback", dest="pullback_entry",
                   action="store_false",
                   help="force the ordinary breakout entry for this run, "
                        "whatever the config says.")
    g.add_argument("--news", default=None, choices=["skip", "include"])
    g.add_argument("--start", default=None)
    g.add_argument("--end", default=None)
    g.add_argument("--data", "-d", nargs="+", default=None)
    g.add_argument("--out", default=None)
    g.add_argument("--log-level", default=None)

    o = p.add_argument_group("engine option overrides")
    o.add_argument("--set", dest="options", action="append", default=[],
                   metavar="NAME=VALUE",
                   help="override one of this engine's options, repeatable. "
                        "e.g. --set sl_range_mult=1.5 --set direction=forward")
    # the reversal options people reach for most often, as plain flags
    o.add_argument("--sl-mult", type=float, default=None,
                   help="shorthand for --set sl_range_mult=...")
    o.add_argument("--forward", action="store_true",
                   help="shorthand for --set direction=forward")
    o.add_argument("--reverse", action="store_true",
                   help="shorthand for --set direction=reverse")
    a = p.parse_args()

    known = available()
    engines = [e.strip().lower() for e in str(a.engine).split(",") if e.strip()]
    missing = [e for e in engines if e not in known]
    if missing:
        print(f"Unknown engine(s): {', '.join(missing)}. "
              f"Available: {', '.join(known)}", file=sys.stderr)
        return 2
    if len(engines) > 1 and a.config:
        print("--config names one file, so it cannot be combined with several "
              "engines.", file=sys.stderr)
        return 2

    # --- several engines: merge their sessions onto one account -----------
    if len(engines) > 1:
        return _run_merged(a, engines)

    rc = RunConfig.load(engines[0], a.config)

    run_over = {k: getattr(a, k) for k in
                ("session", "signal_timeframe", "orb_minutes", "risk_reward",
                 "lots", "news", "log_level", "max_trades_per_session",
                 "pullback_entry", "breakeven", "breakeven_trigger_r")}
    if a.start:
        rc.period["start"] = a.start
    if a.end:
        rc.period["end"] = a.end
    if a.data:
        rc.period["data"] = a.data
    engine_opts = {}
    for pair in a.options:
        if "=" not in pair:
            print(f"--set needs NAME=VALUE (got {pair!r})", file=sys.stderr)
            return 2
        key, _, value = pair.partition("=")
        engine_opts[key.strip()] = _coerce(value)
    if a.sl_mult is not None:
        engine_opts["sl_range_mult"] = a.sl_mult
    if a.forward:
        engine_opts["direction"] = "forward"
    if a.reverse:
        engine_opts["direction"] = "reverse"
    if engine_opts:
        run_over["engine_options"] = engine_opts

    # An unknown engine option raises out of EngineSettings.from_options with a
    # message that already names the valid keys. Print that, rather than a
    # stack trace — a typo in `--set` is a user error, not a crash.
    try:
        app = rc.app_config(run_over)
        if a.news_check:
            return news_check(app)
        if a.instruments:
            app.select_instruments(a.instruments)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    from orb.outputs import instruments_of
    name = rc.run_name(run_over)
    tag = instruments_of(app)
    if tag and not name.upper().startswith(tag.upper()):
        name = f"{tag.upper()}_{name}"
    start, end = rc.dates()
    out = resolve_out(app, name, out_dir=a.out or rc.out_dir())

    print("=" * 68)
    print(f"  BACKTEST — {engines[0]}")
    print("=" * 68)
    print(rc.describe(run_over))
    print(f"  output         {out}/")
    print("=" * 68 + "\n")
    if a.show:
        return 0

    level = str(run_over.get("log_level") or "normal")
    return _execute(
        app, name, out, start, end, level,
        f"Change settings in {os.path.relpath(rc.path)}, "
        f"or pass a flag to override once.")


if __name__ == "__main__":
    raise SystemExit(main())
