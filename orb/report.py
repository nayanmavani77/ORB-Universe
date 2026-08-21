"""Backtest analytics and the self-contained HTML report.

Produces:
  * trade list (CSV + table in the report)
  * equity curve and drawdown curve
  * headline statistics (net profit, PF, expectancy, win rate, ...)
  * day-wise, weekday, month-wise and hour-of-day P&L
  * maximum drawdown (money and %) and its date range
  * longest streaks of consecutive winning and losing trades
  * long/short and exit-reason breakdowns
"""
from __future__ import annotations

import html
import math
import os
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from . import outputs
from .broker import ClosedTrade

# --- palette (light, colour-blind safe, consistent across the whole report) -
# Also imported by tools/matrix_report.py (with `_CSS`), so a sweep summary and
# a single-run report look like the same product.
C_LINE = "#2f6f9f"
C_POS = "#2e8b57"
C_NEG = "#c0392b"
C_GRID = "#e3e6ea"
C_TEXT = "#2b3038"
C_MUTED = "#6b7280"



# ==========================================================================
# Data frames
# ==========================================================================
def trades_dataframe(trades: Sequence[ClosedTrade]) -> pd.DataFrame:
    rows = []
    for i, t in enumerate(trades, 1):
        rows.append({
            "#": i,
            "ticket": t.ticket,
            # which instrument the trade was on. Blank for a single-instrument
            # run, so every existing CSV keeps its shape.
            "instrument": getattr(t, "instrument", "") or "",
            "direction": t.direction,
            "lots": t.lots,
            "session_name": t.session_name,
            "session": t.session_start,
            "trade_in_session": t.trade_no_in_session,
            "entry_time": t.entry_time,
            "entry_price": t.entry_price,
            "sl": t.sl,
            "tp": t.tp,
            "exit_time": t.exit_time,
            "exit_price": t.exit_price,
            "exit_reason": t.exit_reason,
            "range_high": t.range_high,
            "range_low": t.range_low,
            "range_mid": t.range_mid,
            "gross_profit": t.gross_profit,
            "commission": t.commission,
            "net_profit": t.net_profit,
            "r_multiple": t.r_multiple,
            # the risk R is measured against, and whether the stop was moved to
            # the entry before this trade ended
            "initial_risk": t.initial_risk,
            "breakeven": bool(t.breakeven),
            "balance_after": t.balance_after,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["duration_min"] = (
            (pd.to_datetime(df["exit_time"]) - pd.to_datetime(df["entry_time"]))
            .dt.total_seconds() / 60.0)
    return df


# ==========================================================================
# Statistics
# ==========================================================================
def _streaks(values: Sequence[float]) -> Dict[str, float]:
    """Longest run of wins / losses, and the money made or lost in that run."""
    best_w = best_l = 0
    cur_w = cur_l = 0
    money_w = money_l = 0.0
    cur_mw = cur_ml = 0.0
    for v in values:
        if v > 0:
            cur_w += 1
            cur_mw += v
            cur_l, cur_ml = 0, 0.0
            if cur_w > best_w or (cur_w == best_w and cur_mw > money_w):
                best_w, money_w = cur_w, cur_mw
        elif v < 0:
            cur_l += 1
            cur_ml += v
            cur_w, cur_mw = 0, 0.0
            if cur_l > best_l or (cur_l == best_l and cur_ml < money_l):
                best_l, money_l = cur_l, cur_ml
        else:
            cur_w = cur_l = 0
            cur_mw = cur_ml = 0.0
    return {"max_consecutive_wins": best_w, "max_consecutive_wins_profit": money_w,
            "max_consecutive_losses": best_l, "max_consecutive_losses_loss": money_l}


def _max_drawdown(curve: pd.Series) -> Dict[str, object]:
    if curve.empty:
        return {"max_dd_money": 0.0, "max_dd_pct": 0.0,
                "dd_peak_time": None, "dd_trough_time": None,
                "dd_recovery_time": None}
    running_max = curve.cummax()
    dd = curve - running_max
    dd_pct = np.where(running_max > 0, dd / running_max * 100.0, 0.0)
    # index by POSITION, never by label: two trades can close at the same
    # instant (a stop-time flatten, or several sessions sharing a timestamp),
    # and `.loc` on a duplicated label returns a Series, not a number — which
    # made the comparison below raise instead of reporting a drawdown
    i_trough = int(dd.values.argmin())
    trough_time = curve.index[i_trough]
    i_peak = int(curve.iloc[:i_trough + 1].values.argmax()) if i_trough >= 0 else 0
    peak_time = curve.index[i_peak]
    peak_val = float(curve.iloc[i_peak])
    after = curve.iloc[i_trough:]
    rec = after[after.values >= peak_val]
    recovery_time = rec.index[0] if len(rec) else None
    return {
        "max_dd_money": float(-dd.min()),
        "max_dd_pct": float(-dd_pct.min()),
        "dd_peak_time": peak_time,
        "dd_trough_time": trough_time,
        "dd_recovery_time": recovery_time,
    }


def compute_stats(result) -> Dict[str, object]:
    df = trades_dataframe(result.trades)
    s: Dict[str, object] = {}
    init = result.initial_balance
    s["initial_balance"] = init
    s["final_balance"] = result.final_balance
    s["net_profit"] = result.final_balance - init
    s["return_pct"] = (result.final_balance / init - 1.0) * 100.0 if init else 0.0
    s["bars_processed"] = result.bars_processed
    s["period_start"] = result.first_bar
    s["period_end"] = result.last_bar

    if df.empty:
        s.update({k: 0 for k in (
            "total_trades", "wins", "losses", "breakeven", "win_rate",
            "gross_profit", "gross_loss", "profit_factor", "expectancy",
            "avg_win", "avg_loss", "largest_win", "largest_loss",
            "avg_r", "total_r", "long_trades", "short_trades",
            "long_net", "short_net", "commission_total", "avg_duration_min",
            "max_consecutive_wins", "max_consecutive_losses",
            "max_consecutive_wins_profit", "max_consecutive_losses_loss",
            "max_dd_money", "max_dd_pct", "recovery_factor", "sharpe_per_trade",
            "held_past_entry_day", "held_past_entry_day_net",
            "held_past_entry_day_share", "max_hold_hours",
            "be_moved", "be_flat", "be_won", "be_lost", "be_net")})
        s["exit_reasons"] = {}
        s["dd_peak_time"] = s["dd_trough_time"] = s["dd_recovery_time"] = None
        return s

    net = df["net_profit"]
    wins = net[net > 0]
    losses = net[net < 0]
    s["total_trades"] = int(len(df))
    s["wins"] = int(len(wins))
    s["losses"] = int(len(losses))
    s["breakeven"] = int((net == 0).sum())
    s["win_rate"] = float(len(wins) / len(df) * 100.0)
    s["gross_profit"] = float(wins.sum())
    s["gross_loss"] = float(losses.sum())
    s["profit_factor"] = float(wins.sum() / abs(losses.sum())) if losses.sum() else \
        (math.inf if wins.sum() > 0 else 0.0)
    s["expectancy"] = float(net.mean())
    s["avg_win"] = float(wins.mean()) if len(wins) else 0.0
    s["avg_loss"] = float(losses.mean()) if len(losses) else 0.0
    s["largest_win"] = float(net.max())
    s["largest_loss"] = float(net.min())
    s["avg_r"] = float(df["r_multiple"].mean())
    s["total_r"] = float(df["r_multiple"].sum())
    s["commission_total"] = float(df["commission"].sum())
    s["avg_duration_min"] = float(df["duration_min"].mean())
    s["long_trades"] = int((df["direction"] == "BUY").sum())
    s["short_trades"] = int((df["direction"] == "SELL").sum())
    s["long_net"] = float(df.loc[df["direction"] == "BUY", "net_profit"].sum())
    s["short_net"] = float(df.loc[df["direction"] == "SELL", "net_profit"].sum())
    s["exit_reasons"] = df["exit_reason"].value_counts().to_dict()

    # BREAK EVEN. Every figure here is a count of what HAPPENED; none of them
    # is a verdict on whether break-even paid. That question needs the same
    # period run with it off, because the trades it closed flat would each have
    # gone on to win or to lose and one run cannot say which.
    be = df["breakeven"].astype(bool) if "breakeven" in df.columns else \
        pd.Series(False, index=df.index)
    s["be_moved"] = int(be.sum())
    s["be_flat"] = int((be & (net == 0)).sum())
    s["be_won"] = int((be & (net > 0)).sum())
    # A stop AT the entry still loses if the bar gaps through it — the fill is
    # where the market reopened, not where the order sat. Counted separately
    # because a report claiming break-even trades cannot lose would be wrong.
    s["be_lost"] = int((be & (net < 0)).sum())
    s["be_net"] = float(net[be].sum())
    s.update(_streaks(net.tolist()))

    # trades that survived past their own entry day: these carry gap risk the
    # session window was supposed to prevent
    ent = pd.to_datetime(df["entry_time"]); ext = pd.to_datetime(df["exit_time"])
    held = (ent.dt.date != ext.dt.date)
    s["held_past_entry_day"] = int(held.sum())
    s["held_past_entry_day_net"] = float(df.loc[held, "net_profit"].sum())
    s["held_past_entry_day_share"] = (
        float(df.loc[held, "net_profit"].sum() / net.sum() * 100) if net.sum() else 0.0)
    s["max_hold_hours"] = float(((ext - ent).dt.total_seconds() / 3600).max())

    sd = net.std(ddof=1)
    s["sharpe_per_trade"] = float(net.mean() / sd * math.sqrt(len(net))) if sd else 0.0

    # drawdown on the closed-trade balance curve
    curve = pd.Series([init] + df["balance_after"].tolist(),
                      index=[pd.Timestamp(df["entry_time"].iloc[0])] +
                            [pd.Timestamp(t) for t in df["exit_time"]])
    s.update(_max_drawdown(curve))
    s["recovery_factor"] = float(s["net_profit"] / s["max_dd_money"]) \
        if s["max_dd_money"] else 0.0
    return s


# ==========================================================================
# Breakdowns
# ==========================================================================
def breakdowns(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    if df.empty:
        empty = pd.DataFrame()
        return {k: empty for k in ("daily", "weekday", "monthly", "hourly", "monthly_pivot")}

    d = df.copy()
    d["exit_dt"] = pd.to_datetime(d["exit_time"])
    d["entry_dt"] = pd.to_datetime(d["entry_time"])
    # Attribute a trade to the SESSION it belongs to, not to the calendar day
    # it happened to close on. A session that spans midnight (an evening ORB
    # running into the next morning) otherwise gets smeared across two dates,
    # which makes "is this day good to trade?" unanswerable — and produces
    # phantom days: a Friday bar made entirely of Thursday-evening sessions.
    # The session date is also the key the News Days filter matches on, so the
    # breakdown and the filter now speak about the same thing.
    d["session_dt"] = pd.to_datetime(d["session"]) if "session" in d.columns \
        else d["entry_dt"]
    d["session_dt"] = d["session_dt"].fillna(d["entry_dt"])
    d["day"] = d["session_dt"].dt.date
    d["weekday"] = d["session_dt"].dt.day_name()
    d["month"] = d["session_dt"].dt.to_period("M").astype(str)
    d["year"] = d["session_dt"].dt.year
    d["month_no"] = d["session_dt"].dt.month
    d["hour"] = d["entry_dt"].dt.hour          # entry hour = when the breakout fired

    for key, name in (("day", "daily"), ("weekday", "weekday"),
                      ("month", "monthly"), ("hour", "hourly")):
        grp = d.groupby(key)
        t = pd.DataFrame({
            "trades": grp["net_profit"].size(),
            "wins": grp["net_profit"].apply(lambda x: int((x > 0).sum())),
            "losses": grp["net_profit"].apply(lambda x: int((x < 0).sum())),
            "net_profit": grp["net_profit"].sum(),
        })
        t["win_rate"] = np.where(t["trades"] > 0, t["wins"] / t["trades"] * 100.0, 0.0)
        out[name] = t

    # per-session breakdown — the answer to "which session is carrying this?"
    if "session_name" in d.columns and d["session_name"].notna().any():
        g = d.groupby("session_name")
        t = pd.DataFrame({
            "trades": g["net_profit"].size(),
            "wins": g["net_profit"].apply(lambda x: int((x > 0).sum())),
            "losses": g["net_profit"].apply(lambda x: int((x < 0).sum())),
            "net_profit": g["net_profit"].sum(),
        })
        t["win_rate"] = np.where(t["trades"] > 0, t["wins"] / t["trades"] * 100.0, 0.0)
        out["session"] = t.sort_values("net_profit", ascending=False)

    order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    out["weekday"] = out["weekday"].reindex([x for x in order if x in out["weekday"].index])

    piv = d.pivot_table(index="year", columns="month_no", values="net_profit",
                        aggfunc="sum")
    piv = piv.reindex(columns=range(1, 13))
    piv.columns = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    piv["Year"] = piv.sum(axis=1, skipna=True)
    out["monthly_pivot"] = piv
    return out


# ==========================================================================
# Charts
# ==========================================================================
def _qbuckets(s: pd.Series, n: int = 5, fmt="{:.1f}") -> pd.Series:
    """Quantile buckets with readable edge labels.

    Falls back to fewer buckets when the data has too few distinct values —
    `qcut` would otherwise raise, and a report should never die on a thin
    sample.
    """
    for k in range(n, 1, -1):
        try:
            cats = pd.qcut(s, k, duplicates="drop")
            def _edge(v):
                s = fmt.format(max(v, 0) if v > -1 else v)
                return "0" if s in ("-0", "-0.0") else s

            def _name(i):
                # a NaN input maps to a float, not an Interval — a bucket label
                # must never be the reason a report cannot be written
                left = getattr(i, "left", None)
                if left is None:
                    return "n/a"
                return f"{_edge(left)} - {_edge(i.right)}"
            return cats.map(_name)
        except (ValueError, IndexError):
            continue
    return pd.Series(["all"] * len(s), index=s.index)


def _agg(d: pd.DataFrame, key) -> pd.DataFrame:
    g = d.groupby(key, observed=True)
    t = pd.DataFrame({
        "trades": g["net_profit"].size(),
        "wins": g["net_profit"].apply(lambda x: int((x > 0).sum())),
        "net_profit": g["net_profit"].sum(),
        "avg_trade": g["net_profit"].mean(),
        "avg_r": g["r_multiple"].mean() if "r_multiple" in d.columns else g["net_profit"].mean() * 0,
    })
    t["win_rate"] = np.where(t["trades"] > 0, t["wins"] / t["trades"] * 100.0, 0.0)
    return t


def deep_analysis(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """Cuts that say something about WHY the results look the way they do.

    The day/hour/weekday breakdowns answer "when". These answer "under what
    conditions" — the ones a trader can actually act on:

      range_size  Does a wide opening range trade better than a narrow one?
                  If the edge lives entirely in wide-range days, a range-width
                  filter is worth more than any parameter tuning.
      trade_seq   Is the first breakout of a session better than the third?
                  A decaying sequence argues for max_trades_per_session = 1.
      duration    Do quick trades and slow trades behave differently? A book
                  that only makes money on long holds is really a trend book.
      r_hist      The shape of the outcome distribution — how much of the
                  result rests on a handful of large winners.
      direction   Long against short. A one-sided book is a directional bet.
      exit_reason P&L by how the trade ended, not just how often.
    """
    out: Dict[str, pd.DataFrame] = {}
    if df.empty:
        return out
    d = df.copy()

    if {"range_high", "range_low"} <= set(d.columns):
        size = (d["range_high"] - d["range_low"]).abs()
        if size.notna().any() and size.max() > 0:
            out["range_size"] = _agg(d.assign(_b=_qbuckets(size, 5, "{:.1f}")), "_b")
            out["range_size"].index.name = "Opening range size"

    if "trade_in_session" in d.columns and d["trade_in_session"].notna().any():
        seq = d["trade_in_session"].clip(upper=6).astype(int)
        lab = seq.map(lambda i: f"#{i}" if i < 6 else "#6+")
        out["trade_seq"] = _agg(d.assign(_b=lab), "_b")
        out["trade_seq"] = out["trade_seq"].reindex(
            [x for x in ["#1", "#2", "#3", "#4", "#5", "#6+"]
             if x in out["trade_seq"].index])
        out["trade_seq"].index.name = "Trade number within its session"

    if "duration_min" in d.columns and d["duration_min"].notna().any():
        out["duration"] = _agg(d.assign(_b=_qbuckets(d["duration_min"], 5, "{:.0f}")), "_b")
        out["duration"].index.name = "Time in trade (minutes)"

    if "r_multiple" in d.columns and d["r_multiple"].notna().any():
        r = d["r_multiple"].clip(-3, 6)
        edges = [-3, -1.5, -1.0, -0.5, 0, 0.5, 1, 2, 3, 6]
        names = ["< -1.5R", "-1.5 to -1R", "-1 to -0.5R", "-0.5 to 0R",
                 "0 to 0.5R", "0.5 to 1R", "1 to 2R", "2 to 3R", "3R +"]
        out["r_hist"] = _agg(
            d.assign(_b=pd.cut(r, edges, labels=names, include_lowest=True)), "_b")
        out["r_hist"] = out["r_hist"].reindex([n for n in names
                                               if n in out["r_hist"].index])
        out["r_hist"].index.name = "Outcome in R"

    if "direction" in d.columns:
        out["direction"] = _agg(d, "direction")
        out["direction"].index.name = "Direction"

    if "exit_reason" in d.columns:
        out["exit_reason"] = _agg(d, "exit_reason").sort_values(
            "net_profit", ascending=False)
        out["exit_reason"].index.name = "How the trade ended"

    return out


def robustness(df: pd.DataFrame, result) -> Dict[str, object]:
    """How much of this result you should believe.

    The headline numbers say what happened. These say whether it is likely to
    happen again, and they are the ones that change a decision:

      * CONCENTRATION — how much of the profit came from a handful of trades or
        a single month. A curve that is flat for five months and then jumps is
        one regime, not an edge, and no amount of profit factor will tell you
        that.
      * STABILITY — first half against second half. An edge that only exists in
        the recent half may be real and improving, or may be the sample you
        happened to fit to.
      * CONFIDENCE — 387 trades at +0.17R sounds solid until you notice the
        standard deviation is 1.45R. The standard error and t-statistic say
        whether the average is distinguishable from zero at all.
      * ENDURANCE — the longest time spent below the previous peak. Depth is
        what most reports show; duration is what people actually quit during.
      * HEADROOM — the cost per trade at which the edge disappears. Costs are
        deliberately NOT applied anywhere in this report, so this is the one
        honest way to say how much room there is.
    """
    out: Dict[str, object] = {}
    if df is None or df.empty:
        return out
    d = df.sort_values("entry_time").reset_index(drop=True)
    n = len(d)
    net = float(d["net_profit"].sum())
    out["trades"], out["net"] = n, net

    # --- concentration -------------------------------------------------
    if "session" in d.columns:
        months = d.groupby(pd.to_datetime(d["session"]).dt.to_period("M"))["net_profit"].sum()
        if len(months):
            best = months.sort_values(ascending=False)
            out["best_month"] = str(best.index[0])
            out["best_month_net"] = float(best.iloc[0])
            out["net_ex_best_month"] = net - float(best.iloc[0])
            out["months_total"] = int(len(months))
            out["months_positive"] = int((months > 0).sum())
    # WHERE the winners came from, not just how few there are.
    #
    # "x% of the profit came from the best 5% of trades" is close to a
    # tautology for any low-win-rate trend follower: small losses, rare large
    # wins, so of course the tail carries it. Reporting that as a fault flags
    # the normal case as broken, and it says nothing the month concentration
    # below does not already say.
    #
    # The question worth asking is whether those winners are INDEPENDENT
    # events or one regime cut into pieces. Fifty-nine winners spread over two
    # years is a strategy; fifty-nine winners inside one quarter is a single
    # bet that happened to be sliced into fifty-nine tickets.
    k = max(1, int(round(n * 0.05)))
    top_idx = d["net_profit"].nlargest(k).index
    out["top_k"] = k
    out["top_k_net"] = float(d.loc[top_idx, "net_profit"].sum())
    when = pd.to_datetime(d.loc[top_idx, "entry_time"])
    if len(when):
        out["tail_months"] = int(when.dt.to_period("M").nunique())
        by_q = d.loc[top_idx].groupby(when.dt.to_period("Q"))["net_profit"].sum()
        by_q = by_q.sort_values(ascending=False)
        out["tail_best_quarter"] = str(by_q.index[0])
        tail_net = float(d.loc[top_idx, "net_profit"].sum())
        out["tail_quarter_share"] = (float(by_q.iloc[0]) / tail_net * 100.0
                                     if tail_net else 0.0)
        out["tail_quarters"] = int(len(by_q))

    # --- stability ------------------------------------------------------
    half = n // 2
    halves = []
    for lo, hi in ((0, half), (half, n)):
        p = d.iloc[lo:hi]
        if p.empty:
            continue
        wins = float(p.loc[p["net_profit"] > 0, "net_profit"].sum())
        loss = float(-p.loc[p["net_profit"] < 0, "net_profit"].sum())
        halves.append({
            "trades": len(p),
            "net": float(p["net_profit"].sum()),
            "avg_r": float(p["r_multiple"].mean()),
            "win_rate": float((p["net_profit"] > 0).mean() * 100.0),
            "profit_factor": (wins / loss) if loss else float("inf"),
            "from": pd.to_datetime(p["entry_time"]).min(),
            "to": pd.to_datetime(p["entry_time"]).max(),
        })
    out["halves"] = halves

    # --- confidence -----------------------------------------------------
    r = d["r_multiple"].dropna().astype(float)
    if len(r) > 2:
        sd = float(r.std(ddof=1))
        se = sd / math.sqrt(len(r))
        out["avg_r"], out["sd_r"], out["se_r"] = float(r.mean()), sd, se
        out["t_stat"] = (float(r.mean()) / se) if se else 0.0
        out["ci_lo"] = float(r.mean()) - 1.96 * se
        out["ci_hi"] = float(r.mean()) + 1.96 * se

    # --- endurance ------------------------------------------------------
    bal = d.sort_values("exit_time")
    if "balance_after" in bal.columns and len(bal) > 1:
        b = bal.set_index(pd.to_datetime(bal["exit_time"]))["balance_after"].astype(float)
        under = b < b.cummax()
        if under.any():
            grp = (under != under.shift()).cumsum()
            worst, when = 0, None
            for _g, seg in b[under].groupby(grp[under]):
                days = (seg.index[-1] - seg.index[0]).days
                if days >= worst:
                    worst, when = days, (seg.index[0], seg.index[-1])
            out["underwater_days"] = int(worst)
            out["underwater_span"] = when
            out["recovered"] = bool(not under.iloc[-1])

    out["breakeven_cost"] = net / n if n else 0.0
    return out


def equity_series(result) -> Tuple[List, List, List]:
    """(times, equity, drawdown%) on the closed-trade balance curve.

    Same basis as `compute_stats` reports, so the chart and the headline
    drawdown figure can never disagree.
    """
    if not result.trades:
        return [], [], []
    init = result.initial_balance
    times = [result.trades[0].entry_time] + [t.exit_time for t in result.trades]
    eq = [init] + [t.balance_after for t in result.trades]
    peak, dd = eq[0], []
    for v in eq:
        peak = max(peak, v)
        dd.append((v - peak) / peak * 100.0 if peak else 0.0)
    return times, eq, dd


def session_view(result, name: str):
    """A standalone result object containing only one session's trades.

    The balance curve is rebuilt from the starting balance using just this
    session's trades, so its equity line and drawdown answer "how did THIS
    session do on its own" rather than "what did the account look like while
    this session happened to be running". Trading the sessions together does
    not change any individual trade — they are proven independent — so a
    standalone curve is the honest per-session view.
    """
    import copy
    from types import SimpleNamespace
    subset = [t for t in result.trades if (t.session_name or "MAIN") == name]
    running = result.initial_balance
    rebuilt = []
    for t in subset:
        running += t.net_profit
        c = copy.copy(t)
        c.balance_after = running
        rebuilt.append(c)
    return SimpleNamespace(
        trades=rebuilt,
        equity_curve=[],
        initial_balance=result.initial_balance,
        final_balance=running,
        bars_processed=result.bars_processed,
        first_bar=result.first_bar,
        last_bar=result.last_bar,
        config=result.config,
    )


def session_names(result, df) -> List[str]:
    """Session names that actually took a trade, in config order."""
    cfg = result.config
    order = [s.name or "MAIN" for s in cfg.enabled_sessions()] \
        if hasattr(cfg, "enabled_sessions") else ["MAIN"]
    traded = set(df["session_name"]) if "session_name" in df.columns else set()
    named = [n for n in order if n in traded]
    return named + [n for n in sorted(traded) if n not in order]


def instrument_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Net, trades and win rate per instrument.

    A portfolio run blends several instruments into one balance, so the
    headline says nothing about which of them earned it. This is the split.
    Empty for a single-instrument run, which has nothing to split.
    """
    if df.empty or "instrument" not in df.columns:
        return pd.DataFrame()
    names = [n for n in df["instrument"].dropna().unique() if str(n)]
    if len(names) < 2:
        return pd.DataFrame()
    rows = []
    for name in sorted(names):
        d = df[df["instrument"] == name]
        wins = int((d["net_profit"] > 0).sum())
        rows.append({
            "instrument": name,
            "trades": len(d),
            "wins": wins,
            "losses": int((d["net_profit"] < 0).sum()),
            "win_rate": (wins / len(d) * 100.0) if len(d) else 0.0,
            "net_profit": float(d["net_profit"].sum()),
            "avg_r": float(d["r_multiple"].mean()) if len(d) else 0.0,
        })
    return pd.DataFrame(rows).set_index("instrument")


def session_summary(result, df) -> pd.DataFrame:
    """One row per session — the comparison table, and a CSV of its own."""
    rows = []
    for n in session_names(result, df):
        st = compute_stats(session_view(result, n))
        rows.append({
            "session": n,
            "trades": st["total_trades"],
            "wins": st["wins"],
            "losses": st["losses"],
            "win_rate": st["win_rate"],
            "net_profit": st["net_profit"],
            "profit_factor": st["profit_factor"],
            "expectancy": st["expectancy"],
            "avg_r": st["avg_r"],
            "total_r": st["total_r"],
            "max_dd_money": st["max_dd_money"],
            "max_dd_pct": st["max_dd_pct"],
            "recovery_factor": st["recovery_factor"],
            "long_net": st["long_net"],
            "short_net": st["short_net"],
            "avg_duration_min": st["avg_duration_min"],
            "max_consecutive_losses": st["max_consecutive_losses"],
        })
    return pd.DataFrame(rows).set_index("session") if rows else pd.DataFrame()


# The report used to embed matplotlib PNGs here — `_fig_to_b64`,
# `chart_equity`, `_bar_chart` and `chart_monthly_heatmap`. They were replaced
# by `_svg_equity` and `_hbar_table` below, which draw inline SVG: sharp at any
# zoom, styled by the same CSS as the rest of the page, and with no image
# dependency. The matplotlib versions had no callers left, so they are gone.


# ==========================================================================
# HTML
# ==========================================================================
# `_news_summary` lived here. It was superseded by `_news_compact` further
# down, which fits the setup panel's two-column layout; nothing called the
# old one any more.


def _sessions_line(cfg) -> str:
    """The sessions that actually ran, with the settings they ran with.

    Printed from `enabled_sessions()`, never from the shared defaults block —
    a per-session override makes the two differ, and quoting the defaults would
    describe a backtest that never happened.
    """
    sessions = cfg.enabled_sessions() if hasattr(cfg, "enabled_sessions") \
        else [cfg.strategy]
    return "; ".join(
        f"{s.name or 'MAIN'} {s.range_start}-{s.range_end}"
        f"->{s.stop_time} {s.signal_timeframe} RR 1:{s.risk_reward:g}"
        for s in sessions)


def pnl_basis(cfg) -> str:
    """State plainly whether any trading cost was applied."""
    b = cfg.backtest
    parts = []
    if b.spread_points:
        parts.append(f"spread {b.spread_points:g} pts")
    if b.slippage_points:
        parts.append(f"slippage {b.slippage_points:g} pts")
    if b.commission_per_lot_per_side:
        parts.append(f"commission {b.commission_per_lot_per_side:g}/lot/side")
    if not parts:
        return "GROSS — no spread, slippage or commission applied"
    return "NET of " + ", ".join(parts)


def _money(v, cur="") -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and math.isinf(v):
        return "&infin;"
    return f"{v:,.2f}{(' ' + cur) if cur else ''}"


def _cls(v) -> str:
    try:
        return "pos" if float(v) > 0 else ("neg" if float(v) < 0 else "")
    except (TypeError, ValueError):
        return ""


def _table(df: pd.DataFrame, money_cols=(), pct_cols=(), index_name="") -> str:
    if df is None or df.empty:
        return "<p class='muted'>No data.</p>"
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    rows = []
    for idx, row in df.iterrows():
        cells = [f"<td class='idx'>{html.escape(str(idx))}</td>"]
        for c in df.columns:
            v = row[c]
            if isinstance(v, (int, float, np.floating, np.integer)) and pd.notna(v):
                if c in money_cols:
                    cells.append(f"<td class='num {_cls(v)}'>{v:,.2f}</td>")
                elif c in pct_cols:
                    cells.append(f"<td class='num'>{v:,.1f}%</td>")
                else:
                    cells.append(f"<td class='num'>{v:,.0f}</td>"
                                 if float(v).is_integer() else
                                 f"<td class='num'>{v:,.2f}</td>")
            else:
                cells.append(f"<td>{html.escape('' if pd.isna(v) else str(v))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (f"<table><thead><tr><th>{html.escape(index_name)}</th>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def _trade_table(df: pd.DataFrame, limit: int = 1000) -> str:
    if df.empty:
        return "<p class='muted'>No trades.</p>"
    cols = ["#", "direction", "lots", "entry_time", "entry_price", "sl", "tp",
            "exit_time", "exit_price", "exit_reason", "r_multiple",
            "net_profit", "balance_after"]
    d = df[cols].head(limit).copy()
    d["entry_time"] = pd.to_datetime(d["entry_time"]).dt.strftime("%Y-%m-%d %H:%M")
    d["exit_time"] = pd.to_datetime(d["exit_time"]).dt.strftime("%Y-%m-%d %H:%M")
    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    rows = []
    for _, r in d.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if c in ("net_profit", "balance_after"):
                cells.append(f"<td class='num {_cls(v)}'>{v:,.2f}</td>")
            elif c in ("entry_price", "sl", "tp", "exit_price", "r_multiple", "lots"):
                cells.append(f"<td class='num'>{v:,.2f}</td>")
            elif c == "direction":
                cells.append(f"<td><span class='tag {'buy' if v=='BUY' else 'sell'}'>"
                             f"{v}</span></td>")
            else:
                cells.append(f"<td>{html.escape(str(v))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    note = ("" if len(df) <= limit else
            f"<p class='muted'>Showing the first {limit:,} of {len(df):,} trades — "
            f"the full list is in the CSV.</p>")
    return (f"<table class='trades'><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>{note}")


_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#2b3038;--muted:#6b7280;--line:#e3e6ea;
--pos:#2e8b57;--neg:#c0392b;--accent:#2f6f9f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;margin:34px 0 12px;letter-spacing:.02em;text-transform:uppercase;
color:var(--muted);font-weight:600}
.sub{color:var(--muted);margin:0 0 22px;font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
.tile .v{font-size:20px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}
.pos{color:var(--pos)}.neg{color:var(--neg)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;
letter-spacing:.04em;position:sticky;top:0;background:var(--card)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.idx{font-weight:600}
.scroll{max-height:520px;overflow:auto;border:1px solid var(--line);border-radius:10px;
background:var(--card)}
img{width:100%;height:auto;display:block}
.muted{color:var(--muted);font-size:12.5px}
.tag{display:inline-block;padding:1px 7px;border-radius:99px;font-size:11px;font-weight:600}
.tag.buy{background:#e7f3ec;color:var(--pos)}
.tag.sell{background:#fbeae8;color:var(--neg)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:860px){.two{grid-template-columns:1fr}}
code{background:#eef1f4;padding:1px 5px;border-radius:4px;font-size:12px}
"""

# ==========================================================================
# HTML report
# ==========================================================================
# Colour roles come from a validated categorical palette; light and dark are
# both chosen steps, not an automatic inversion. Series colour follows the
# entity (a session, a direction), never its rank, so filtering never repaints
# the survivors.
_VIZ_CSS = """
:root{color-scheme:light}
.viz{
 --surface-1:#fcfcfb; --surface-2:#f4f3f0; --surface-3:#eceae5; --line:#e3e2de;
 --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#78766f;
 --pos:#1b7f4d; --neg:#c0392b; --pos-soft:#1baf7a; --neg-soft:#e34948;
 --s-1:#2a78d6; --s-2:#eb6834; --s-3:#1baf7a; --s-4:#eda100;
 --grid:#e8e7e3;
}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])) .viz{
 color-scheme:dark;
 --surface-1:#1a1a19; --surface-2:#232322; --surface-3:#2c2c2a; --line:#383835;
 --text-primary:#fff; --text-secondary:#c3c2b7; --text-muted:#96958c;
 --pos:#4ec27f; --neg:#e66767; --pos-soft:#199e70; --neg-soft:#e66767;
 --s-1:#3987e5; --s-2:#d95926; --s-3:#199e70; --s-4:#c98500;
 --grid:#2f2f2d;
}}
:root[data-theme=dark] .viz{color-scheme:dark;
 --surface-1:#1a1a19; --surface-2:#232322; --surface-3:#2c2c2a; --line:#383835;
 --text-primary:#fff; --text-secondary:#c3c2b7; --text-muted:#96958c;
 --pos:#4ec27f; --neg:#e66767; --pos-soft:#199e70; --neg-soft:#e66767;
 --s-1:#3987e5; --s-2:#d95926; --s-3:#199e70; --s-4:#c98500;
 --grid:#2f2f2d;}
*{box-sizing:border-box}
body{margin:0;background:var(--surface-1);color:var(--text-primary);
 font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 -webkit-font-smoothing:antialiased}
.wrap{max-width:1220px;margin:0 auto;padding:34px 24px 80px}
header{display:flex;justify-content:space-between;align-items:flex-start;
 gap:20px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:25px;margin:0 0 3px;letter-spacing:-.022em}
.sub{color:var(--text-secondary);margin:0}
.badge{display:inline-block;background:var(--surface-3);color:var(--text-secondary);
 border-radius:999px;padding:3px 11px;font-size:12px;font-weight:600;margin-top:6px}
h2{font-size:11.5px;text-transform:uppercase;letter-spacing:.1em;
 color:var(--text-muted);margin:46px 0 14px;font-weight:700}
h3{font-size:15.5px;margin:0 0 3px;font-weight:600}
.note{color:var(--text-muted);margin:0 0 14px;font-size:13px;max-width:78ch}
.card{background:var(--surface-2);border:1px solid var(--line);
 border-radius:12px;padding:18px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.two{grid-template-columns:1fr}}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px}
.kpi{background:var(--surface-2);border:1px solid var(--line);border-radius:12px;
 padding:14px 16px}
.kpi .k{color:var(--text-muted);font-size:11.5px;text-transform:uppercase;
 letter-spacing:.06em;font-weight:600;margin-bottom:5px}
.kpi .v{font-size:23px;font-weight:650;letter-spacing:-.02em;
 font-variant-numeric:tabular-nums;line-height:1.15}
.kpi .m{color:var(--text-muted);font-size:12px;margin-top:3px}
.pos{color:var(--pos)} .neg{color:var(--neg)} .muted{color:var(--text-muted)}
.rb-list{display:flex;flex-direction:column}
.rrow{display:grid;grid-template-columns:170px 190px 78px 1fr;gap:14px;
align-items:baseline;padding:14px 0;border-bottom:1px solid var(--line)}
.rrow:last-child{border-bottom:0}
.rlab{font-weight:650;font-size:13px}
.rnum{font-variant-numeric:tabular-nums;font-size:19px;font-weight:600;line-height:1.25}
.rsay{font-size:13px;color:var(--text-secondary);line-height:1.5}
.verdict{display:inline-block;font-size:10.5px;font-weight:700;letter-spacing:.06em;
padding:2px 7px;border-radius:5px;text-align:center;
background:var(--surface-3);color:var(--text-secondary)}
.verdict.pos{background:#e7f3ec;color:#1b7f4d}
.verdict.neg{background:#fbeae8;color:#c0392b}
/* The one line to read before the rows. Text carries the meaning; the
   rule beside it is decoration, so it survives greyscale and CVD. */
.lead-finding{margin:0 0 14px;padding:9px 13px;font-size:13.5px;
line-height:1.5;color:var(--text-primary);background:var(--surface-3);
border-left:3px solid #c0392b;border-radius:0 6px 6px 0}
/* Dark is CHOSEN, not an automatic flip: the chips get their own steps against
   the dark surface rather than inheriting light backgrounds that would glare. */
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])) .viz{
  --chip-pos-bg:#12331f;--chip-neg-bg:#3a1b18}}
:root[data-theme=dark] .viz{--chip-pos-bg:#12331f;--chip-neg-bg:#3a1b18}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])) .viz
  .verdict.pos{background:#12331f;color:#4ec27f}
  }
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])) .viz
  .verdict.neg{background:#3a1b18;color:#e66767}
  }
:root[data-theme=dark] .viz .verdict.pos{background:#12331f;color:#4ec27f}
:root[data-theme=dark] .viz .verdict.neg{background:#3a1b18;color:#e66767}
.vtext{}
.chart{width:100%;height:auto;display:block}
.svg-val{font-size:11px;fill:var(--text-secondary);font-variant-numeric:tabular-nums}
.svg-ax{font-size:11px;fill:var(--text-muted)}
@media (max-width:900px){.rrow{grid-template-columns:1fr;gap:4px}}
section{margin-top:26px}
.swatch{width:10px;height:10px;border-radius:3px;display:inline-block;
 margin-right:7px;vertical-align:middle}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin:2px 0 12px;
 color:var(--text-secondary);font-size:12.5px}
svg{display:block;width:100%;overflow:visible}
.axis text{fill:var(--text-muted);font-size:10.5px}
.axis line{stroke:var(--grid)}
.hb{display:flex;flex-direction:column;gap:9px}
.hrow{display:flex;align-items:center;gap:12px}
.hlab{width:126px;flex:none;color:var(--text-secondary);font-size:12.5px;
 text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.htrack{position:relative;flex:1;height:24px}
.hzero{position:absolute;top:-1px;bottom:-1px;width:1px;background:var(--text-muted);
 opacity:.4}
.hfill{position:absolute;top:4px;bottom:4px;border-radius:4px}
.hval{width:92px;flex:none;text-align:right;font-size:12px;font-weight:600;
 font-variant-numeric:tabular-nums;white-space:nowrap}
.hmeta{width:112px;flex:none;color:var(--text-muted);font-size:12px;
 white-space:nowrap;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
 color:var(--text-secondary);font-weight:600;white-space:nowrap}
td{padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.scroll{max-height:520px;overflow:auto;border:1px solid var(--line);
 border-radius:12px;background:var(--surface-2)}
.scroll thead th{position:sticky;top:0;background:var(--surface-3);z-index:2}
.callout{background:var(--surface-2);border:1px solid var(--line);
 border-left:3px solid var(--s-1);border-radius:10px;padding:13px 16px;margin:0 0 12px}
.callout.warn{border-left-color:var(--s-2)}
.callout.good{border-left-color:var(--s-3)}
.callout b{display:block;margin-bottom:2px}
.callout p{margin:0;color:var(--text-secondary);font-size:13.3px}
#tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .09s;
 background:var(--text-primary);color:var(--surface-1);padding:5px 9px;
 border-radius:6px;font-size:12px;font-weight:500;z-index:99;white-space:nowrap}
.toggle{background:var(--surface-2);border:1px solid var(--line);color:var(--text-secondary);
 border-radius:8px;padding:6px 11px;font-size:12.5px;cursor:pointer}
.toggle:hover{background:var(--surface-3)}
"""

_VIZ_JS = """
const tip=document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el=>{
  el.addEventListener('mouseenter',()=>{tip.textContent=el.dataset.tip;tip.style.opacity=1;});
  el.addEventListener('mousemove',e=>{
    const w=tip.offsetWidth||0;
    tip.style.left=Math.min(e.clientX+14,window.innerWidth-w-10)+'px';
    tip.style.top=(e.clientY-32)+'px';});
  el.addEventListener('mouseleave',()=>{tip.style.opacity=0;});
});
const tg=document.getElementById('themeToggle');
if(tg) tg.addEventListener('click',()=>{
  const r=document.documentElement;
  const cur=r.dataset.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light');
  r.dataset.theme = cur==='dark'?'light':'dark';
});
"""


def _m(v: float, cur: str = "") -> str:
    """Compact money for chart labels and KPI values."""
    if v is None:
        return "-"
    if isinstance(v, float) and math.isinf(v):
        return "&infin;"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1_000_000:
        s = f"{sign}{a/1_000_000:,.2f}M"
    elif a >= 10_000:
        s = f"{sign}{a/1_000:,.1f}k"
    else:
        s = f"{sign}{a:,.0f}"
    return f"{s}{(' ' + cur) if cur else ''}"


def kpi(label: str, value: str, meta: str = "", cls: str = "") -> str:
    m = f'<div class="m">{meta}</div>' if meta else ""
    return (f'<div class="kpi"><div class="k">{html.escape(label)}</div>'
            f'<div class="v {cls}">{value}</div>{m}</div>')


def _svg_equity(times, eq, dd, initial: float, cur: str) -> str:
    """Equity line with the underwater curve beneath it, sharing one x-axis.

    Two panels rather than two y-scales on one plot: drawdown is a percentage
    and equity is money, and putting them on a single axis would be the
    dual-axis mistake.
    """
    if len(eq) < 2:
        return '<p class="muted">Not enough trades to draw a curve.</p>'
    W, H1, H2, PAD_L, PAD_R = 1000.0, 210.0, 78.0, 64.0, 6.0
    n = len(eq)
    lo, hi = min(eq), max(eq)
    span = (hi - lo) or 1.0
    iw = W - PAD_L - PAD_R

    def x(i):
        return PAD_L + i / (n - 1) * iw

    def y(v):
        return H1 - (v - lo) / span * (H1 - 8) - 4

    pts = " ".join(f"{x(i):.2f},{y(v):.2f}" for i, v in enumerate(eq))
    area = f"{x(0):.2f},{H1:.2f} " + pts + f" {x(n-1):.2f},{H1:.2f}"
    y0 = y(initial)

    ddmin = min(dd) or -1.0
    def yd(v):
        return (v / ddmin) * (H2 - 10) if ddmin else 0.0
    dpts = " ".join(f"{x(i):.2f},{yd(v):.2f}" for i, v in enumerate(dd))
    darea = f"{x(0):.2f},0 " + dpts + f" {x(n-1):.2f},0"

    # a handful of x labels only — never one per point
    marks = []
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        i = int(frac * (n - 1))
        marks.append(f'<text x="{x(i):.1f}" y="{H1+H2+34:.0f}" '
                     f'text-anchor="{"start" if frac==0 else "end" if frac==1 else "middle"}">'
                     f'{times[i]:%Y-%m-%d}</text>')

    return f"""
<svg viewBox="0 0 {W:.0f} {H1+H2+42:.0f}" role="img"
     aria-label="Equity curve and drawdown">
  <defs><linearGradient id="eqg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="var(--s-1)" stop-opacity=".26"/>
    <stop offset="100%" stop-color="var(--s-1)" stop-opacity="0"/>
  </linearGradient></defs>
  <g class="axis">
    <line x1="{PAD_L}" x2="{W-PAD_R}" y1="{y0:.1f}" y2="{y0:.1f}"
          stroke-dasharray="3 3"/>
    <text x="{W-PAD_R}" y="{y0-6:.1f}" text-anchor="end">start {_m(initial, cur)}</text>
    <text x="{PAD_L-10}" y="12" text-anchor="end">{_m(hi, cur)}</text>
    <text x="{PAD_L-10}" y="{H1-2:.0f}" text-anchor="end">{_m(lo, cur)}</text>
  </g>
  <polygon points="{area}" fill="url(#eqg)"/>
  <polyline points="{pts}" fill="none" stroke="var(--s-1)" stroke-width="2"
            stroke-linejoin="round"/>
  <g transform="translate(0,{H1+16:.0f})">
    <g class="axis"><line x1="{PAD_L}" x2="{W-PAD_R}" y1="0" y2="0"/></g>
    <polygon points="{darea}" fill="var(--neg-soft)" fill-opacity=".22"/>
    <polyline points="{dpts}" fill="none" stroke="var(--neg-soft)" stroke-width="1.5"/>
    <text class="axis" x="{PAD_L-10}" y="4" text-anchor="end"
          fill="var(--text-muted)" font-size="10.5">0%</text>
    <text class="axis" x="{PAD_L-10}" y="{H2-12:.0f}" text-anchor="end"
          fill="var(--text-muted)" font-size="10.5">{ddmin:.1f}%</text>
  </g>
  <g class="axis">{''.join(marks)}</g>
</svg>"""


def _hbar_table(t: pd.DataFrame, cur: str, value="net_profit",
                colour_by_sign=True, series_var="--s-1", show_wr=True) -> str:
    """One row per category: label, bar, value, supporting counts.

    Label / bar / value / counts are four flex columns rather than absolutely
    positioned text, so a long bar can never push its own number underneath the
    counts beside it. The bar carries magnitude; the numbers carry the detail,
    so nothing here depends on reading colour alone.
    """
    if t is None or t.empty:
        return '<p class="muted">No data.</p>'
    vals = t[value].astype(float)
    diverging = bool((vals < 0).any())
    span = float(vals.abs().max()) or 1.0
    rows = []
    for idx, v in vals.items():
        w = abs(v) / span * (50.0 if diverging else 100.0)
        left = (50.0 if v >= 0 else 50.0 - w) if diverging else 0.0
        col = ("var(--pos-soft)" if v >= 0 else "var(--neg-soft)") \
            if colour_by_sign else f"var({series_var})"
        tr = int(t.loc[idx, "trades"])
        wr = float(t.loc[idx, "win_rate"])
        meta = f"{tr:,} trades &middot; {wr:.0f}%" if show_wr else f"{tr:,} trades"
        tip = (f"{idx} - {tr:,} trades, {wr:.0f}% win rate, {_m(v, cur)}" if show_wr
               else f"{idx} - {tr:,} trades, {_m(v, cur)}")
        rows.append(f"""
<div class="hrow" data-tip="{html.escape(tip)}">
  <div class="hlab">{html.escape(str(idx))}</div>
  <div class="htrack">{'<div class="hzero" style="left:50%"></div>' if diverging else ''}
    <div class="hfill" style="left:{left:.2f}%;width:{w:.2f}%;background:{col}"></div>
  </div>
  <div class="hval {'pos' if v >= 0 else 'neg'}">{_m(v, cur)}</div>
  <div class="hmeta">{meta}</div>
</div>""")
    return f'<div class="hb">{"".join(rows)}</div>'


def _svg_monthly(df: pd.DataFrame, cur: str) -> str:
    """Month-by-month P&L as diverging bars on one zero line.

    The monthly numbers were already in the report as a list. A list makes you
    compare thirteen figures in your head; a bar chart on a shared baseline
    makes a single dominant month impossible to miss, which is the whole reason
    the breakdown exists.

    Sign is carried by position (above / below the zero line) as well as by
    colour, and every bar is labelled, so nothing depends on telling red from
    green.
    """
    if df is None or df.empty or "session" not in df.columns:
        return '<p class="muted">No data.</p>'
    m = df.groupby(pd.to_datetime(df["session"]).dt.to_period("M"))["net_profit"].sum()
    if m.empty:
        return '<p class="muted">No data.</p>'
    W, H = 1000, 280
    padl, padr, padt, padb = 8, 8, 26, 52
    # The zero line sits where the DATA puts it, not at the halfway mark. With
    # twelve positive months and one negative, a centred axis would waste half
    # the panel below the line and squash every bar above it.
    hi = float(max(m.max(), 0.0))
    lo = float(min(m.min(), 0.0))
    span = (hi - lo) or 1.0
    inner_w = W - padl - padr
    inner_h = H - padt - padb
    zero = padt + inner_h * (hi / span)
    slot = inner_w / len(m)
    bw = min(56.0, slot * 0.62)
    bars, labels = [], []
    for i, (per, v) in enumerate(m.items()):
        cx = padl + slot * (i + 0.5)
        h = abs(float(v)) / span * inner_h
        y = zero - h if v >= 0 else zero
        col = "var(--pos-soft)" if v >= 0 else "var(--neg-soft)"
        tip = f"{per} - {_m(float(v), cur)}"
        bars.append(
            f'<rect x="{cx - bw / 2:.1f}" y="{y:.1f}" width="{bw:.1f}" '
            f'height="{max(h, 1.2):.1f}" rx="4" fill="{col}" '
            f'data-tip="{html.escape(tip)}"><title>{html.escape(tip)}</title></rect>')
        vy = (y - 7) if v >= 0 else (y + h + 15)
        labels.append(
            f'<text x="{cx:.1f}" y="{vy:.1f}" text-anchor="middle" '
            f'class="svg-val">{html.escape(_m(float(v), ""))}</text>')
        labels.append(
            f'<text x="{cx:.1f}" y="{H - 10}" text-anchor="middle" '
            f'class="svg-ax">{html.escape(str(per)[2:])}</text>')
    return f"""<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none"
     class="chart" role="img" aria-label="Net P&L by month">
  <line x1="{padl}" y1="{zero:.1f}" x2="{W - padr}" y2="{zero:.1f}"
        stroke="var(--line)" stroke-width="1"/>
  {''.join(bars)}
  {''.join(labels)}
</svg>"""


def _svg_rolling_r(df: pd.DataFrame, window: int = 50) -> str:
    """Rolling average R over the trade sequence.

    The equity curve answers "how much"; this answers "was the edge there all
    along". A line that sits below zero for a third of the sample and only
    lifts later is the same story as a flat-then-vertical equity curve, but
    stated per trade, so a couple of outsized wins cannot flatter it.

    Labels live in the gutters, never over the plot, and the y-axis is
    annotated at its extremes and at zero — without a scale a line chart shows
    a shape but no magnitude, which is how "it goes up" gets mistaken for "it
    goes up enough".
    """
    if df is None or df.empty or "r_multiple" not in df.columns:
        return '<p class="muted">No data.</p>'
    r = (df.sort_values("entry_time")["r_multiple"].dropna().astype(float)
         .reset_index(drop=True))
    if len(r) < window + 5:
        return ('<p class="muted">Not enough trades for a '
                f'{window}-trade rolling view.</p>')
    roll = r.rolling(window).mean().dropna().reset_index(drop=True)

    W, H = 1000, 250
    padl, padr, padt, padb = 62, 12, 16, 34
    lo, hi = float(roll.min()), float(roll.max())
    pad = max(0.05, (hi - lo) * 0.15)
    lo, hi = min(lo - pad, 0.0), max(hi + pad, 0.0)
    rng = (hi - lo) or 1.0
    iw, ih = W - padl - padr, H - padt - padb

    def x(i):
        return padl + iw * (i / max(1, len(roll) - 1))

    def y(v):
        return padt + ih * (1 - (v - lo) / rng)

    zero_y, avg = y(0.0), float(r.mean())
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(roll))
    # the area between the line and zero, so time spent losing reads as area
    area = (f"{padl},{zero_y:.1f} " + pts + f" {x(len(roll) - 1):.1f},{zero_y:.1f}")
    def _tick(v):
        dash = "" if v == 0 else ' stroke-dasharray="3 5"'
        return (f'<line x1="{padl}" y1="{y(v):.1f}" x2="{W - padr}" '
                f'y2="{y(v):.1f}" stroke="var(--line)" stroke-width="1"{dash}/>'
                f'<text x="{padl - 8}" y="{y(v) + 4:.1f}" text-anchor="end" '
                f'class="svg-ax">{v:+.2f}R</text>')

    ticks = "".join(_tick(v) for v in (hi, 0.0, lo) if lo <= v <= hi)
    return f"""<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none"
     class="chart" role="img"
     aria-label="Rolling {window}-trade average R over the trade sequence">
  {ticks}
  <polygon points="{area}" fill="var(--s-1)" fill-opacity=".13"/>
  <polyline points="{pts}" fill="none" stroke="var(--s-1)" stroke-width="2"
            stroke-linejoin="round" stroke-linecap="round"/>
  <line x1="{padl}" y1="{y(avg):.1f}" x2="{W - padr}" y2="{y(avg):.1f}"
        stroke="var(--text-muted)" stroke-width="1.5" stroke-dasharray="6 4"/>
  <text x="{padl}" y="{H - 12}" class="svg-ax">trade {window}</text>
  <text x="{(padl + W - padr) / 2:.0f}" y="{H - 12}" text-anchor="middle"
        class="svg-ax">dashed line = whole-sample average {avg:+.2f}R</text>
  <text x="{W - padr}" y="{H - 12}" text-anchor="end"
        class="svg-ax">trade {len(r)}</text>
</svg>"""


def _panel(title: str, note: str, body: str) -> str:
    return (f'<section><h3>{html.escape(title)}</h3>'
            f'<p class="note">{html.escape(note)}</p>'
            f'<div class="card">{body}</div></section>')


def _verdict(ok: bool, warn: bool, text: str) -> str:
    """A finding with a word, not just a colour.

    Status colour is never the only carrier: each row states GOOD / WATCH /
    FRAGILE in text as well, so the panel reads the same in greyscale and to a
    colour-blind reader.
    """
    state = "FRAGILE" if warn else ("GOOD" if ok else "WATCH")
    cls = "neg" if warn else ("pos" if ok else "")
    #: rank alongside the label, so the panel can lead with the worst
    rank = 0 if warn else (2 if ok else 1)
    return (f'<span class="verdict {cls}">{state}</span>', text, rank)


#: What to read first when several checks fail, most damning first.
#:
#: The order is a chain of reasoning, not a ranking of numbers. If the sample
#: cannot show an edge exists, nothing after it matters; if the winners turn
#: out to be one regime, the edge that does show up is one observation; and so
#: on down to cost, which only matters once something survives the rest.
_FINDING_ORDER = [
    "Confidence",
    "Winners &mdash; spread or clustered",
    "Stability",
    "Concentration &mdash; months",
    "Endurance",
    "Cost headroom",
]


def _robustness_panel(rb: Dict, cur: str) -> str:
    """The questions that decide whether to trade this."""
    if not rb:
        return '<p class="muted">No trades.</p>'
    net, n = rb.get("net", 0.0), rb.get("trades", 0)
    rows = []

    # Are the winners independent events, or one regime in slices?
    if "tail_quarter_share" in rb:
        k = rb.get("top_k", 0)
        share = rb["tail_quarter_share"]
        months, quarters = rb["tail_months"], rb["tail_quarters"]
        rows.append((
            "Winners &mdash; spread or clustered",
            f"{share:,.0f}% in {rb['tail_best_quarter']}",
            _verdict(share < 40 and months >= 6, share > 60 or months <= 3,
                     f"The best {k} trades ({k / n * 100:.0f}% of the book) "
                     f"carry the result. They fall in {months} different "
                     f"month(s) across {quarters} quarter(s), and "
                     f"{share:,.0f}% of what they made landed in "
                     f"{rb['tail_best_quarter']} alone. "
                     + ("Spread across many periods, so they read as "
                        "independent opportunities."
                        if share < 40 and months >= 6 else
                        "That is one market regime cut into pieces, not "
                        "many separate chances &mdash; the honest sample "
                        "size is the number of regimes, not the number of "
                        "trades."))))

    # concentration — months
    if "best_month_net" in rb and net:
        bm = rb["best_month_net"]
        share = bm / net * 100.0
        rows.append((
            "Concentration &mdash; months",
            f"{share:,.0f}% from {rb['best_month']}",
            _verdict(share < 40, share > 60,
                     f"{rb['best_month']} alone made {_m(bm, cur)}; the other "
                     f"{rb['months_total'] - 1} months made "
                     f"{_m(rb['net_ex_best_month'], cur)} between them. "
                     f"{rb['months_positive']} of {rb['months_total']} months "
                     f"were positive.")))

    # stability
    h = rb.get("halves") or []
    if len(h) == 2:
        a, b = h
        rows.append((
            "Stability",
            f"{a['avg_r']:+.2f}R &rarr; {b['avg_r']:+.2f}R",
            _verdict(abs(a["avg_r"] - b["avg_r"]) < 0.10,
                     (a["avg_r"] <= 0) != (b["avg_r"] <= 0)
                     or abs(a["avg_r"] - b["avg_r"]) > 0.15,
                     f"First half: {a['trades']} trades, {_m(a['net'], cur)}, "
                     f"PF {a['profit_factor']:.2f}. Second half: {b['trades']} "
                     f"trades, {_m(b['net'], cur)}, PF "
                     f"{b['profit_factor']:.2f}.")))

    # confidence
    if "t_stat" in rb:
        t = rb["t_stat"]
        rows.append((
            "Confidence",
            f"t = {t:.2f}",
            _verdict(t >= 3.0, t < 2.0,
                     f"Average {rb['avg_r']:+.3f}R with a standard deviation of "
                     f"{rb['sd_r']:.2f}R over {n} trades. 95% confidence "
                     f"interval {rb['ci_lo']:+.3f}R to {rb['ci_hi']:+.3f}R. "
                     + ("The interval excludes zero, but only just &mdash; "
                        "treat the size of the edge as unresolved."
                        if 2.0 <= t < 3.0 else
                        "The interval spans zero, so this sample cannot "
                        "distinguish the edge from chance."
                        if t < 2.0 else
                        "Comfortably clear of zero for this sample size."))))

    # endurance
    if "underwater_days" in rb:
        days = rb["underwater_days"]
        rec = rb.get("recovered", True)
        rows.append((
            "Endurance",
            f"{days} days underwater",
            _verdict(days < 60, days > 120 or not rec,
                     f"Longest stretch below the previous peak: {days} days"
                     + (f" ({rb['underwater_span'][0]:%Y-%m-%d} to "
                        f"{rb['underwater_span'][1]:%Y-%m-%d})"
                        if rb.get("underwater_span") else "")
                     + (". The curve ended below its peak."
                        if not rec else ". Fully recovered."))))

    # headroom
    be = rb.get("breakeven_cost", 0.0)
    rows.append((
        "Cost headroom",
        f"{_m(be, cur)} / trade",
        _verdict(be > 0, be <= 0,
                 "Every figure in this report is GROSS. This is the round-trip "
                 "cost per trade at which the edge reaches zero &mdash; compare "
                 "it against your own spread, slippage and commission.")))

    # WORST FIRST. Six rows all reading FRAGILE is the same as none of
    # them reading FRAGILE: the eye flattens them, and the check that
    # should stop you gets the same weight as the one merely noting a
    # wide interval. Within a severity the original order is kept, so
    # the panel still reads as a sequence rather than reshuffling.
    def _priority(row):
        try:
            return _FINDING_ORDER.index(row[0])
        except ValueError:
            return len(_FINDING_ORDER)

    rows.sort(key=lambda r: (r[2][2], _priority(r)))

    worst = [r for r in rows if r[2][2] == 0]
    lead = ""
    if worst:
        lead = (f'<p class="lead-finding"><strong>Read this first:</strong> '
                f'{worst[0][0]} &mdash; {worst[0][1]}. '
                f'{len(worst)} of {len(rows)} checks came back FRAGILE.</p>')

    body = "".join(
        f'<div class="rrow"><div class="rlab">{lab}</div>'
        f'<div class="rnum">{num}</div><div>{say[0]}</div>'
        f'<div class="rsay">{say[1]}</div></div>'
        for lab, num, say in rows)
    return f'{lead}<div class="rb-list">{body}</div>'


def _findings(stats: Dict, deep: Dict, cur: str) -> str:
    """Plain statements about what the numbers actually say.

    Only claims that follow directly from a computed figure — no advice, and
    nothing that needs a second data source to check.
    """
    out = []
    d = deep.get("direction")
    if d is not None and len(d) == 2 and "BUY" in d.index and "SELL" in d.index:
        L, S = float(d.loc["BUY", "net_profit"]), float(d.loc["SELL", "net_profit"])
        if L * S < 0:
            win, lose = ("long", "short") if L > 0 else ("short", "long")
            out.append(("warn", "The book is one-sided.",
                        f"{win.capitalize()} trades net {_m(max(L,S), cur)} while "
                        f"{lose} trades net {_m(min(L,S), cur)}. Every unit of profit "
                        f"comes from one direction, so this is a directional bet on "
                        f"the period rather than a symmetric breakout."))
    r = deep.get("r_hist")
    if r is not None and not r.empty:
        big = r.loc[[i for i in r.index if i in ("2 to 3R", "3R +")], "net_profit"].sum()
        tot = float(r["net_profit"].sum())
        share = big / tot * 100 if tot else 0
        cnt = int(r.loc[[i for i in r.index if i in ("2 to 3R", "3R +")], "trades"].sum())
        if tot > 0 and share > 60:
            n_all = max(int(r["trades"].sum()), 1)
            tail = (f"carry {share:.0f}% of the net profit" if share <= 100 else
                    f"contribute MORE than the whole net profit ({share:.0f}%) — "
                    f"everything else nets out negative")
            out.append(("warn", "The result rests on a few large winners.",
                        f"Trades of 2R or better are {cnt:,} of {n_all:,} "
                        f"({cnt/n_all*100:.0f}%) but {tail}. Miss a handful of them and "
                        f"the edge largely disappears."))
    ts = deep.get("trade_seq")
    if ts is not None and len(ts) >= 3:
        first = float(ts.iloc[0]["avg_trade"])
        rest = float(ts.iloc[1:]["net_profit"].sum() / max(ts.iloc[1:]["trades"].sum(), 1))
        if first > 0 and rest < 0:
            out.append(("good", "Only the first breakout of a session pays.",
                        f"Trade #1 averages {_m(first, cur)}; every later trade in the "
                        f"same session averages {_m(rest, cur)}. Setting "
                        f"max_trades_per_session to 1 is worth testing."))
    rs = deep.get("range_size")
    if rs is not None and len(rs) >= 3:
        lo_, hi_ = float(rs.iloc[0]["avg_trade"]), float(rs.iloc[-1]["avg_trade"])
        if lo_ * hi_ < 0:
            better = "wider" if hi_ > lo_ else "narrower"
            out.append(("good", f"The edge lives in {better} opening ranges.",
                        f"The narrowest fifth averages {_m(lo_, cur)} per trade and the "
                        f"widest fifth {_m(hi_, cur)}. A minimum range-width filter would "
                        f"remove the losing half without touching the strategy rules."))
    hold = stats.get("max_hold_hours") or 0
    if hold and hold > 24:
        out.append(("warn", "At least one trade was held through a closed market.",
                    f"The longest hold was {hold:,.1f} hours, which spans a weekend or a "
                    f"session break. That is gap risk the stop time was meant to prevent — "
                    f"check the journal for a LATE stop-time warning."))
    if not out:
        out.append(("good", "Nothing unusual in the distribution.",
                    "No one-sided direction bias, no dependence on a handful of outsized "
                    "winners, and no trade held through a market closure."))
    return "".join(
        f'<div class="callout {c}"><b>{html.escape(t)}</b><p>{html.escape(p)}</p></div>'
        for c, t, p in out)


def _kv(rows) -> str:
    return ("<table><tbody>" + "".join(
        f"<tr><td>{html.escape(str(k))}</td>"
        f"<td class='num {c}'>{v}</td></tr>" for k, v, c in rows) +
        "</tbody></table>")


def _setup_rows(cfg, stats, cur):
    b = cfg.backtest
    return [
        ("Symbol", html.escape(_instruments_line(cfg)), ""),
        ("Data", html.escape(f"{cfg.databento.dataset} / {cfg.databento.symbols} "
                             f"/ {cfg.databento.schema}"), ""),
        ("Contract selection",
         html.escape(f"{cfg.databento.contract_mode}, roll at "
                     f"{cfg.databento.roll_boundary_hour:02d}:00 server time"), ""),
        ("Starting balance", _money(b.initial_balance, cur), ""),
        ("Spread / slippage (points)", f"{b.spread_points:g} / {b.slippage_points:g}", ""),
        ("Commission per lot per side", _money(b.commission_per_lot_per_side, cur), ""),
        ("Pessimistic intrabar", "yes" if b.pessimistic_intrabar else "no", ""),
        ("Base bars processed", f"{stats['bars_processed']:,}", ""),
    ]


def _detail_rows(stats, cur):
    return [
        ("Gross profit", _money(stats["gross_profit"], cur), "pos"),
        ("Gross loss", _money(stats["gross_loss"], cur), "neg"),
        ("Average win", _money(stats["avg_win"], cur), "pos"),
        ("Average loss", _money(stats["avg_loss"], cur), "neg"),
        ("Largest win", _money(stats["largest_win"], cur), "pos"),
        ("Largest loss", _money(stats["largest_loss"], cur), "neg"),
        ("Longest winning streak",
         f"{stats['max_consecutive_wins']} ({_money(stats['max_consecutive_wins_profit'], cur)})", ""),
        ("Longest losing streak",
         f"{stats['max_consecutive_losses']} ({_money(stats['max_consecutive_losses_loss'], cur)})", ""),
        ("Recovery factor", f"{stats['recovery_factor']:.2f}", ""),
        ("t-statistic (mean/sd x sqrt N)", f"{stats['sharpe_per_trade']:.2f}", ""),
        ("Average trade duration", f"{stats['avg_duration_min']:,.0f} min", ""),
        ("Longest single hold", f"{stats['max_hold_hours']:,.1f} h", ""),
        ("Trades held past entry day",
         f"{stats['held_past_entry_day']:,} "
         f"({_money(stats['held_past_entry_day_net'], cur)}, "
         f"{stats['held_past_entry_day_share']:,.1f}% of net)", ""),
        ("Commission paid", _money(stats["commission_total"], cur), ""),
    ]


def _news_compact(s) -> str:
    """One short phrase instead of eight lines — the full per-category detail
    is in the journal, and a settings table should stay scannable."""
    from .timeutils import NewsDays
    counts, dates = {}, 0
    for _k, _label, cat in s.news.items():
        n = len(NewsDays(cat.dates))
        if n:
            counts[cat.mode] = counts.get(cat.mode, 0) + 1
            dates += n
    n = len(NewsDays(s.news_days))
    if n:
        counts[s.news_trading] = counts.get(s.news_trading, 0) + 1
        dates += n
    if not counts:
        return '<span class="muted">none</span>'
    parts = [f"{v} {k.upper()}" for k, v in counts.items()]
    return f'{", ".join(parts)} <span class="muted">({dates} dates)</span>'


def _engine_of(s) -> str:
    return str(getattr(s, "engine", "") or "orb")


def _stop_loss_cell(s) -> str:
    """How this session's stop was placed — asked of the ENGINE it ran.

    `sl_mode` is a field of every session, but not every engine reads it: the
    reverse engine sizes its stop from `sl_range_mult` and ignores `sl_mode`
    entirely. Printing "mid range" for such a session would state, in the
    report, the opposite of what the run actually did.
    """
    options = dict(getattr(s, "engine_options", None) or {})
    mult = options.get("sl_range_mult")
    if mult is not None:
        note = {0.5: " (= mid range)", 1.0: " (= full range)"}.get(float(mult), "")
        anchor = options.get("sl_anchor")
        tail = f' <span class="muted">{html.escape(str(anchor))}</span>' \
            if anchor and anchor != "range" else ""
        return f"{float(mult):g} × range{note}{tail}"
    return "full range" if s.sl_mode == "full_range" else "mid range"


def _engine_options_cell(s) -> str:
    """Everything the engine was given, minus what already has its own column,
    so nothing a run used is invisible here."""
    options = dict(getattr(s, "engine_options", None) or {})
    for shown in ("sl_range_mult", "sl_anchor", "max_trades_per_session"):
        options.pop(shown, None)
    if not options:
        return '<span class="muted">—</span>'
    return ", ".join(f"{html.escape(str(k))}={html.escape(str(v))}"
                     for k, v in sorted(options.items()))


def _instruments_table(df) -> str:
    """Per-instrument split. Renders nothing unless the run had more than one."""
    summary = instrument_summary(df)
    if summary.empty:
        return ""
    rows = "".join(
        "<tr><td>{n}</td><td class='num'>{t}</td><td class='num'>{w:.1f}%</td>"
        "<td class='num {cls}'>{net:,.2f}</td><td class='num'>{r:+.2f}R</td></tr>".format(
            n=html.escape(str(name)), t=int(r["trades"]), w=r["win_rate"],
            net=r["net_profit"], r=r["avg_r"],
            cls=("pos" if r["net_profit"] > 0 else "neg"))
        for name, r in summary.iterrows())
    return (
        "<h2>By instrument</h2>"
        "<p class='sub'>This run traded more than one instrument on one "
        "account. The headline blends them; this is who earned it.</p>"
        "<div class='scroll'><table><thead><tr><th>Instrument</th>"
        "<th class='num'>Trades</th><th class='num'>Win rate</th>"
        "<th class='num'>Net</th><th class='num'>Avg R</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>")


def _instruments_line(cfg) -> str:
    """What this run traded. One name, or every mapping in a portfolio run."""
    insts = getattr(cfg, "instruments", None) or {}
    if not insts:
        return str(cfg.symbol.name)
    return " · ".join(
        f"{key}: {(i.signal or key)} -> {(i.mt5 or '?')}"
        for key, i in sorted(insts.items()))


def _sessions_table(cfg, result, df) -> str:
    """The settings each session ACTUALLY ran with — read from the sessions,
    never from the shared defaults block, and never from a field the session's
    engine does not use."""
    sessions = cfg.enabled_sessions() if hasattr(cfg, "enabled_sessions") \
        else [cfg.strategy]
    multi_engine = len({_engine_of(s) for s in sessions}) > 1
    cols = ("Session", "Engine", "Range window", "Stop time", "Signal TF",
            "Stop loss", "Risk : reward", "Lots", "Re-entry", "Max / session",
            "Close at stop", "Engine options", "News", "Trades")
    body = []
    for i, s in enumerate(sessions):
        name = s.name or "MAIN"
        n_tr = int((df["session_name"] == name).sum()) \
            if "session_name" in df.columns else len(df)
        engine = _engine_of(s)
        cells = [
            f'<span class="swatch" style="background:var(--s-{(i % 4) + 1})"></span>'
            + html.escape(name),
            f'<code>{html.escape(engine)}</code>',
            f"{s.range_start} – {s.range_end}",
            s.stop_time if s.stop_time not in ("", "0") else "disabled",
            s.signal_timeframe,
            _stop_loss_cell(s),
            f"1 : {s.risk_reward:g}",
            f"{s.lots:g}",
            "yes" if s.require_range_reentry else "no",
            str(s.max_trades_per_session or "unlimited"),
            "yes" if s.close_at_stop_time else "no",
            _engine_options_cell(s),
            _news_compact(s),
            f"{n_tr:,}",
        ]
        body.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    note = ""
    if multi_engine:
        note = ('<p class="note">This run mixed engines — each session traded '
                'the strategy named in its Engine column, on one account.</p>')
    return (note + "<table><thead><tr>" +
            "".join(f"<th>{html.escape(c)}</th>" for c in cols) +
            "</tr></thead><tbody>" + "".join(body) + "</tbody></table>")


def _session_blocks(result, df, cur) -> str:
    """The full analysis, repeated for each session that traded.

    The combined figures above answer "what does the account do when I run all
    of these together". These answer "what is each session actually
    contributing" — which is the question you need to decide whether to keep
    trading one of them. Sessions are independent (proven in tests/test_sessions),
    so a session's trades are identical whether it ran alone or alongside the
    others, and splitting the report this way loses nothing.
    """
    names = session_names(result, df)
    if len(names) < 2:
        return ""

    summ = session_summary(result, df)
    head = ("<tr><th>Session</th><th class='num'>Trades</th><th class='num'>Win rate</th>"
            "<th class='num'>Net P&amp;L</th><th class='num'>Profit factor</th>"
            "<th class='num'>Expectancy</th><th class='num'>Avg R</th>"
            "<th class='num'>Max DD</th><th class='num'>Longest losing run</th>"
            "<th class='num'>Avg hold</th></tr>")
    body = []
    for i, n in enumerate(names):
        r = summ.loc[n]
        pfv = r["profit_factor"]
        body.append(
            f"<tr><td><span class='swatch' style='background:var(--s-{(i % 4) + 1})'></span>"
            f"{html.escape(n)}</td>"
            f"<td class='num'>{int(r['trades']):,}</td>"
            f"<td class='num'>{r['win_rate']:.1f}%</td>"
            f"<td class='num {_cls(r['net_profit'])}'>{_m(r['net_profit'], cur)}</td>"
            f"<td class='num'>{'&infin;' if math.isinf(pfv) else f'{pfv:.2f}'}</td>"
            f"<td class='num {_cls(r['expectancy'])}'>{_m(r['expectancy'], cur)}</td>"
            f"<td class='num {_cls(r['avg_r'])}'>{r['avg_r']:+.2f}R</td>"
            f"<td class='num neg'>{r['max_dd_pct']:.1f}%</td>"
            f"<td class='num'>{int(r['max_consecutive_losses'])}</td>"
            f"<td class='num'>{r['avg_duration_min']:,.0f} min</td></tr>")

    out = [f"""<h2>Session comparison</h2>
<p class="note">Every session standalone: its own trades, its own balance curve
from the same starting balance, its own drawdown. Sessions are independent, so
these are the same trades each session would have taken running alone.</p>
<div class="card"><table><thead>{head}</thead><tbody>{''.join(body)}</tbody></table></div>"""]

    for i, n in enumerate(names):
        sv = session_view(result, n)
        sst = compute_stats(sv)
        sdf = df[df["session_name"] == n] if "session_name" in df.columns else df
        sbd = breakdowns(sdf)
        sdeep = deep_analysis(sdf)
        stimes, seq, sdd = equity_series(sv)
        pfv = sst["profit_factor"]
        cfgs = [s for s in (result.config.enabled_sessions()
                            if hasattr(result.config, "enabled_sessions")
                            else [result.config.strategy])
                if (s.name or "MAIN") == n]
        setting = ""
        if cfgs:
            s = cfgs[0]
            setting = (f"{s.range_start}&ndash;{s.range_end} &middot; stop {s.stop_time}"
                       f" &middot; {s.signal_timeframe} &middot; R:R 1:{s.risk_reward:g}"
                       f" &middot; {'full range' if s.sl_mode == 'full_range' else 'mid range'} SL")

        kp = "".join([
            kpi("Net P&L", _m(sst["net_profit"], cur),
                f"{sst['return_pct']:,.2f}% of the starting balance",
                _cls(sst["net_profit"])),
            kpi("Profit factor", "&infin;" if math.isinf(pfv) else f"{pfv:.2f}",
                "gross win / gross loss", "pos" if pfv > 1 else "neg"),
            kpi("Trades", f"{sst['total_trades']:,}",
                f"{sst['wins']:,} won, {sst['losses']:,} lost"),
            kpi("Win rate", f"{sst['win_rate']:.1f}%",
                f"expectancy {_m(sst['expectancy'], cur)}/trade"),
            kpi("Max drawdown", f"{sst['max_dd_pct']:.1f}%",
                f"{_m(sst['max_dd_money'], cur)} standalone", "neg"),
            kpi("Average R", f"{sst['avg_r']:+.2f}R", f"{sst['total_r']:+,.1f}R total"),
        ])

        out.append(f"""
<h2><span class="swatch" style="background:var(--s-{(i % 4) + 1})"></span>
Session &mdash; {html.escape(n)}</h2>
<p class="note">{setting}</p>
<div class="kpis">{kp}</div>
<section style="margin-top:16px">
  <h3>Balance and drawdown, this session alone</h3>
  <p class="note">Started from the same balance as the combined run, so the
  curves are comparable between sessions.</p>
  <div class="card">{_svg_equity(stimes, seq, sdd, sv.initial_balance, cur)}</div>
</section>
<div class="two">
  {_panel("Outcome distribution", "This session's trades, in R buckets.",
          _hbar_table(sdeep.get('r_hist'), cur, show_wr=False))}
  {_panel("How trades ended", "P&L by exit reason.",
          _hbar_table(sdeep.get('exit_reason'), cur))}
</div>
<div class="two">
  {_panel("Opening range width", "Five equal groups by the size of the range "
          "this session broke out of.",
          _hbar_table(sdeep.get('range_size'), cur))}
  {_panel("Trade number within the session",
          "Whether this session's first breakout beats its later ones.",
          _hbar_table(sdeep.get('trade_seq'), cur))}
</div>
<div class="two">
  {_panel("Direction", "Long against short for this session alone.",
          _hbar_table(sdeep.get('direction'), cur))}
  {_panel("By weekday", "By session date - the day the range was built.",
          _hbar_table(sbd['weekday'], cur))}
</div>
{_panel("By month", "This session's month-by-month contribution.",
        _hbar_table(sbd['monthly'], cur))}""")
    return "".join(out)


def build_html(result, stats: Dict, df: pd.DataFrame,
               bd: Dict[str, pd.DataFrame]) -> str:
    cfg = result.config
    cur = cfg.symbol.currency
    deep = deep_analysis(df)
    rb = robustness(df, result)
    times, eq, dd = equity_series(result)

    net = stats["net_profit"]
    pf = stats["profit_factor"]
    kpis = "".join([
        kpi("Net P&L", _m(net, cur), f"{stats['return_pct']:,.2f}% on "
            f"{_m(stats['initial_balance'], cur)}", _cls(net)),
        kpi("Profit factor", f"{pf:.2f}" if not math.isinf(pf) else "&infin;",
            "gross win / gross loss", "pos" if pf > 1 else "neg"),
        kpi("Trades", f"{stats['total_trades']:,}",
            f"{stats['wins']:,} won, {stats['losses']:,} lost"),
        kpi("Win rate", f"{stats['win_rate']:.1f}%",
            f"expectancy {_m(stats['expectancy'], cur)}/trade"),
        kpi("Max drawdown", f"{stats['max_dd_pct']:.1f}%",
            f"{_m(stats['max_dd_money'], cur)} on closed trades", "neg"),
        kpi("Average R", f"{stats['avg_r']:+.2f}R",
            f"{stats['total_r']:+,.1f}R total"),
    ])

    # BREAK EVEN. Rendered only when the stop actually moved on at least one
    # trade, so every report predating the feature — and every run with it off
    # — looks exactly as it always did.
    be_block = ""
    if int(stats.get("be_moved", 0)) > 0:
        moved = int(stats["be_moved"])
        flat, won = int(stats["be_flat"]), int(stats["be_won"])
        lost = int(stats["be_lost"])
        share = moved / stats["total_trades"] * 100.0 if stats["total_trades"] else 0.0
        be_block = _panel(
            "Break even",
            "What the stop move did — counts, not a verdict. The trades it "
            "closed flat would each have gone on to win or to lose, and one "
            "run cannot say which. Run the same period with break-even off to "
            "score it. A stop at the entry is not a guarantee: price can gap "
            "straight through it and fill worse.",
            _kv([("Trades that reached the trigger",
                  f"{moved:,} of {stats['total_trades']:,} ({share:.1f}%)", ""),
                 ("— closed flat at the entry", f"{flat:,}", ""),
                 ("— went on to win", f"{won:,}", "pos" if won else ""),
                 ("— still lost, to a gap through the stop", f"{lost:,}",
                  "neg" if lost else ""),
                 ("Net from those trades", _m(stats['be_net'], cur),
                  "pos" if stats['be_net'] > 0 else "neg")]))

    sess_block = ""
    if bd.get("session") is not None and not bd["session"].empty \
            and len(bd["session"]) > 1:
        sess_block = _panel(
            "By session",
            "Each enabled session's own contribution. One session carrying the "
            "whole result is worth knowing before trading all of them.",
            _hbar_table(bd["session"], cur))

    # Name the engine(s) this run actually used. The title was hard-coded to
    # the breakout engine, so an orb_reverse report was headed "Range Breakout"
    # while the sessions table below it said otherwise.
    engines = sorted({str(getattr(sc, "engine", "") or "orb")
                      for sc in cfg.enabled_sessions()}) or ["orb"]
    title = " + ".join(engines)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} backtest - {html.escape(str(cfg.symbol.name))}</title>
<style>{_VIZ_CSS}</style></head>
<body class="viz"><div id="tip"></div><div class="wrap">

<header>
  <div>
    <h1>{html.escape(title)} &mdash; backtest</h1>
    <p class="sub">{html.escape(str(cfg.symbol.name))}
      &middot; {html.escape(str(stats['period_start']))} &rarr; {html.escape(str(stats['period_end']))}
      &middot; {stats['bars_processed']:,} base bars</p>
    <span class="badge">{html.escape(pnl_basis(cfg))}</span>
  </div>
  <button class="toggle" id="themeToggle">Light / dark</button>
</header>

<h2>Headline &mdash; all sessions combined</h2>
<div class="kpis">{kpis}</div>

<h2>What the numbers say</h2>
{_findings(stats, deep, cur)}

<h2>Equity</h2>
<section>
  <h3>Balance and drawdown</h3>
  <p class="note">Closed-trade balance above, underwater curve below, sharing one
  time axis - the same basis as the headline drawdown, so the two cannot disagree.</p>
  <div class="card">{_svg_equity(times, eq, dd, result.initial_balance, cur)}</div>
</section>

<h2>How much to believe it</h2>
<p class="note" style="margin:-6px 0 14px">The headline says what happened.
These say whether it is likely to happen again &mdash; concentration, stability,
statistical confidence, how long it stayed underwater, and how much cost it can
carry before the edge is gone.</p>
<div class="card">{_robustness_panel(rb, cur)}</div>

<section>
  <h3>Month by month</h3>
  <p class="note">Attributed to the session date. One dominant bar is a regime,
  not an edge &mdash; and it is far easier to see here than in a column of
  thirteen numbers.</p>
  <div class="card">{_svg_monthly(df, cur)}</div>
</section>

<section>
  <h3>Rolling average R (50 trades)</h3>
  <p class="note">Per trade, so a couple of outsized wins cannot flatter it.
  Where this sits relative to the 0R line is where the edge actually was.</p>
  <div class="card">{_svg_rolling_r(df)}</div>
</section>

<h2>Sessions actually run</h2>
<div class="card">{_instruments_table(df)}{_sessions_table(cfg, result, df)}
<p class="note" style="margin:12px 0 0">The settings each session ran with,
including any per-session override. All times are server time.</p></div>

<h2>Where the result comes from</h2>
<div class="two">
  {_panel("Outcome distribution",
          "Every trade placed in an R bucket. A healthy book earns across the "
          "middle; one that depends on the far right tail is fragile.",
          _hbar_table(deep.get('r_hist'), cur, show_wr=False))}
  {_panel("How trades ended",
          "P&L by exit reason, not just how often each exit fired.",
          _hbar_table(deep.get('exit_reason'), cur))}
</div>
{be_block}
<div class="two">
  {_panel("Opening range width",
          "Trades split into five equal groups by the size of the range they broke "
          "out of. If the edge sits in one group, a width filter is worth more than "
          "any parameter tuning.",
          _hbar_table(deep.get('range_size'), cur))}
  {_panel("Trade number within its session",
          "Whether the first breakout of a session beats the later ones. A decaying "
          "sequence argues for capping trades per session.",
          _hbar_table(deep.get('trade_seq'), cur))}
</div>
<div class="two">
  {_panel("Time in trade",
          "Trades split into five equal groups by how long they were held.",
          _hbar_table(deep.get('duration'), cur))}
  {_panel("Direction",
          "Long against short. A one-sided book is a directional bet, not a "
          "symmetric breakout.",
          _hbar_table(deep.get('direction'), cur))}
</div>
{sess_block}

<h2>When</h2>
<div class="two">
  {_panel("By weekday",
          "Attributed to the session date - the day the range was built, not the "
          "day the trade happened to close.",
          _hbar_table(bd['weekday'], cur))}
  {_panel("By entry hour (server time)",
          "The hour the breakout fired.",
          _hbar_table(bd['hourly'], cur))}
</div>

{_session_blocks(result, df, cur)}

<h2>Detail</h2>
<div class="two">
  <div class="card">{_kv(_detail_rows(stats, cur))}</div>
  <div class="card">{_kv(_setup_rows(cfg, stats, cur))}</div>
</div>

<h2>Day by day</h2>
<div class="scroll">{_table(bd['daily'], ('net_profit',), ('win_rate',), 'Date')}</div>

<h2>Every trade</h2>
<div class="scroll">{_trade_table(df)}</div>

</div><script>{_VIZ_JS}</script></body></html>"""
def write_report(result, out_dir: Optional[str] = None,
                 name: Optional[str] = None) -> Dict[str, str]:
    cfg = result.config
    name = name or cfg.backtest.report_name
    # Precedence: the argument, then `backtest.out_dir` from the config, then
    # the standard layout `backtest/<engine>/<run-name>/`. The config default is
    # null, so a caller that sets nothing now gets the standard layout instead
    # of the flat folder every run used to share.
    out_dir = out_dir or cfg.backtest.out_dir or outputs.resolve(cfg, name)
    os.makedirs(out_dir, exist_ok=True)

    df = trades_dataframe(result.trades)
    stats = compute_stats(result)
    bd = breakdowns(df)

    paths = {}
    csv_path = os.path.join(out_dir, f"{name}_trades.csv")
    df.to_csv(csv_path, index=False)
    paths["trades_csv"] = csv_path

    for key, fname in (("daily", "daily_pnl"), ("monthly", "monthly_pnl"),
                       ("hourly", "hourly_pnl"), ("weekday", "weekday_pnl")):
        p = os.path.join(out_dir, f"{name}_{fname}.csv")
        bd[key].to_csv(p)
        paths[f"{key}_csv"] = p

    eq = pd.DataFrame(result.equity_curve, columns=["time", "equity"])
    if not eq.empty:
        # downsample to hourly closes so the file stays small on long runs
        eq["time"] = pd.to_datetime(eq["time"])
        eq = (eq.set_index("time").resample("1h").last().dropna()
              .reset_index())
    eq_path = os.path.join(out_dir, f"{name}_equity.csv")
    eq.to_csv(eq_path, index=False)
    paths["equity_csv"] = eq_path

    ss = session_summary(result, df)
    if len(ss) > 1:
        p = os.path.join(out_dir, f"{name}_session_summary.csv")
        ss.to_csv(p)
        paths["session_summary_csv"] = p
        for sname in ss.index:
            sp = os.path.join(out_dir, f"{name}_trades_{sname}.csv")
            df[df["session_name"] == sname].to_csv(sp, index=False)
            paths[f"trades_{sname}_csv"] = sp

    stats_path = os.path.join(out_dir, f"{name}_stats.csv")
    pd.Series({k: v for k, v in stats.items()
               if not isinstance(v, dict)}).to_csv(stats_path, header=False)
    paths["stats_csv"] = stats_path

    html_path = os.path.join(out_dir, f"{name}.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(build_html(result, stats, df, bd))
    paths["html"] = html_path

    return paths


# `print_summary` lived here — a console table of the same stats the HTML
# report shows. It had no callers: `tools/backtest.py` prints its own summary
# (it knows the run name and the output folder, which this did not), and every
# other tool reads the CSVs. Removed rather than left to drift out of step
# with `compute_stats`.
