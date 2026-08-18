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

import base64
import html
import io
import math
import os
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
import matplotlib.dates as mdates        # noqa: E402

from .broker import ClosedTrade          # noqa: E402

# --- palette (light, colour-blind safe, consistent across every figure) ----
C_LINE = "#2f6f9f"
C_POS = "#2e8b57"
C_NEG = "#c0392b"
C_GRID = "#e3e6ea"
C_TEXT = "#2b3038"
C_MUTED = "#6b7280"

plt.rcParams.update({
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": C_GRID,
    "axes.labelcolor": C_TEXT,
    "text.color": C_TEXT,
    "xtick.color": C_MUTED,
    "ytick.color": C_MUTED,
    "grid.color": C_GRID,
    "font.size": 9,
    "axes.spines.top": False,
    "axes.spines.right": False,
})


# ==========================================================================
# Data frames
# ==========================================================================
def trades_dataframe(trades: Sequence[ClosedTrade]) -> pd.DataFrame:
    rows = []
    for i, t in enumerate(trades, 1):
        rows.append({
            "#": i,
            "ticket": t.ticket,
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
            "held_past_entry_day_share", "max_hold_hours")})
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


def _fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def chart_equity(df: pd.DataFrame, initial: float, stats: Dict) -> str:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 6), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})
    if df.empty:
        ax1.text(0.5, 0.5, "no trades", ha="center", va="center")
        return _fig_to_b64(fig)

    x = [pd.Timestamp(df["entry_time"].iloc[0])] + \
        [pd.Timestamp(t) for t in df["exit_time"]]
    y = [initial] + df["balance_after"].tolist()
    ser = pd.Series(y, index=x)

    ax1.plot(ser.index, ser.values, color=C_LINE, lw=1.6)
    ax1.fill_between(ser.index, initial, ser.values,
                     where=(ser.values >= initial), color=C_POS, alpha=0.12)
    ax1.fill_between(ser.index, initial, ser.values,
                     where=(ser.values < initial), color=C_NEG, alpha=0.12)
    ax1.axhline(initial, color=C_MUTED, lw=0.8, ls="--")
    ax1.set_ylabel("Balance")
    ax1.set_title("Equity curve", loc="left", fontsize=11, weight="bold")
    ax1.grid(True, lw=0.6)

    if stats.get("dd_peak_time") is not None and stats.get("dd_trough_time") is not None:
        ax1.axvspan(stats["dd_peak_time"], stats["dd_trough_time"],
                    color=C_NEG, alpha=0.07)

    dd = ser - ser.cummax()
    ax2.fill_between(dd.index, dd.values, 0, color=C_NEG, alpha=0.35)
    ax2.plot(dd.index, dd.values, color=C_NEG, lw=1.0)
    ax2.set_ylabel("Drawdown")
    ax2.grid(True, lw=0.6)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.autofmt_xdate()
    return _fig_to_b64(fig)


def _bar_chart(index, values, title, xlabel="", rotate=0, width=11, height=3.2) -> str:
    fig, ax = plt.subplots(figsize=(width, height))
    colors = [C_POS if v >= 0 else C_NEG for v in values]
    ax.bar([str(i) for i in index], values, color=colors, alpha=0.85)
    ax.axhline(0, color=C_MUTED, lw=0.8)
    ax.set_title(title, loc="left", fontsize=11, weight="bold")
    ax.set_ylabel("Net P&L")
    if xlabel:
        ax.set_xlabel(xlabel)
    ax.grid(True, axis="y", lw=0.6)
    if rotate:
        plt.setp(ax.get_xticklabels(), rotation=rotate, ha="right")
    if len(index) > 40:
        step = max(1, len(index) // 30)
        for i, lbl in enumerate(ax.get_xticklabels()):
            lbl.set_visible(i % step == 0)
    return _fig_to_b64(fig)


def chart_monthly_heatmap(piv: pd.DataFrame) -> str:
    if piv.empty:
        return ""
    data = piv.drop(columns=["Year"], errors="ignore")
    fig, ax = plt.subplots(figsize=(11, max(1.6, 0.45 * len(data) + 1.2)))
    arr = data.to_numpy(dtype="float64")
    vmax = np.nanmax(np.abs(arr)) if np.isfinite(arr).any() else 1.0
    im = ax.imshow(arr, cmap="RdYlGn", vmin=-vmax, vmax=vmax, aspect="auto")
    ax.set_xticks(range(len(data.columns)))
    ax.set_xticklabels(data.columns)
    ax.set_yticks(range(len(data.index)))
    ax.set_yticklabels(data.index)
    for i in range(arr.shape[0]):
        for j in range(arr.shape[1]):
            v = arr[i, j]
            if np.isfinite(v):
                ax.text(j, i, f"{v:,.0f}", ha="center", va="center", fontsize=8,
                        color="#1b1b1b")
    ax.set_title("Month-wise net P&L", loc="left", fontsize=11, weight="bold")
    fig.colorbar(im, ax=ax, shrink=0.8, pad=0.01)
    return _fig_to_b64(fig)


# ==========================================================================
# HTML
# ==========================================================================
def _news_summary(s) -> str:
    """One line per configured news category, for the report's setup panel."""
    from .timeutils import NewsDays
    parts = []
    for _key, label, cat in s.news.items():
        n = len(NewsDays(cat.dates))
        if n:
            parts.append(f"{label}: {cat.mode.upper()} ({n})")
    n = len(NewsDays(s.news_days))
    if n:
        parts.append(f"General: {s.news_trading.upper()} ({n})")
    return "<br>".join(parts) if parts else "none configured"


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


def _panel(title: str, note: str, body: str) -> str:
    return (f'<section><h3>{html.escape(title)}</h3>'
            f'<p class="note">{html.escape(note)}</p>'
            f'<div class="card">{body}</div></section>')


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
        ("Symbol", html.escape(str(cfg.symbol.name)), ""),
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

    sess_block = ""
    if bd.get("session") is not None and not bd["session"].empty \
            and len(bd["session"]) > 1:
        sess_block = _panel(
            "By session",
            "Each enabled session's own contribution. One session carrying the "
            "whole result is worth knowing before trading all of them.",
            _hbar_table(bd["session"], cur))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest - {html.escape(str(cfg.symbol.name))}</title>
<style>{_VIZ_CSS}</style></head>
<body class="viz"><div id="tip"></div><div class="wrap">

<header>
  <div>
    <h1>Range Breakout - backtest</h1>
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

<h2>Sessions actually run</h2>
<div class="card">{_sessions_table(cfg, result, df)}
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
{_panel("By month",
        "Attributed to the session date. A result that lives in one or two months "
        "is a regime, not an edge.",
        _hbar_table(bd['monthly'], cur))}

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
    out_dir = out_dir or cfg.backtest.out_dir
    name = name or cfg.backtest.report_name
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


def print_summary(result) -> None:
    s = compute_stats(result)
    cur = result.config.symbol.currency
    line = "-" * 62
    print("\n" + line)
    print("  RANGE BREAKOUT EA — BACKTEST SUMMARY")
    print(line)
    rows = [
        ("P&L basis", pnl_basis(result.config)),
        ("Period", f"{s['period_start']}  ->  {s['period_end']}"),
        ("Initial balance", f"{s['initial_balance']:,.2f} {cur}"),
        ("Final balance", f"{s['final_balance']:,.2f} {cur}"),
        ("Net profit", f"{s['net_profit']:,.2f} {cur}  ({s['return_pct']:,.2f}%)"),
        ("Total trades", f"{s['total_trades']:,}"),
        ("Wins / losses", f"{s['wins']:,} / {s['losses']:,}  "
                          f"({s['win_rate']:.1f}% win rate)"),
        ("Profit factor", f"{s['profit_factor']:.2f}"),
        ("Expectancy", f"{s['expectancy']:,.2f} {cur} per trade"),
        ("Average win / loss", f"{s['avg_win']:,.2f} / {s['avg_loss']:,.2f} {cur}"),
        ("Largest win / loss", f"{s['largest_win']:,.2f} / {s['largest_loss']:,.2f} {cur}"),
        ("Sessions run", _sessions_line(result.config)),
        ("Max drawdown", f"{s['max_dd_money']:,.2f} {cur}  ({s['max_dd_pct']:.2f}%)"),
        ("Held past entry day", f"{s['held_past_entry_day']:,} trades  "
                                f"({s['held_past_entry_day_net']:,.2f} {cur}, "
                                f"{s['held_past_entry_day_share']:.1f}% of net)"),
        ("Longest single hold", f"{s['max_hold_hours']:.1f} h"),
        ("Recovery factor", f"{s['recovery_factor']:.2f}"),
        ("Max consecutive wins", f"{s['max_consecutive_wins']}  "
                                 f"({s['max_consecutive_wins_profit']:,.2f} {cur})"),
        ("Max consecutive losses", f"{s['max_consecutive_losses']}  "
                                   f"({s['max_consecutive_losses_loss']:,.2f} {cur})"),
        ("Long / short net", f"{s['long_net']:,.2f} / {s['short_net']:,.2f} {cur}"),
        ("Average R multiple", f"{s['avg_r']:.2f}"),
    ]
    for k, v in rows:
        print(f"  {k:<24} {v}")
    print(line + "\n")
