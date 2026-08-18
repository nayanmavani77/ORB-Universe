#!/usr/bin/env python3
"""Build the cross-configuration comparison report from a matrix run.

    python tools/matrix_report.py --dir backtest/orb/matrix

Reads `_summary/all_results.csv` and produces `_summary/comparison.html`
answering the four questions the test matrix was designed for: best timeframe,
best session, best ORB duration, and the effect of the news filter.
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt      # noqa: E402

from orb.report import _CSS, C_POS, C_NEG, C_MUTED, C_GRID, C_TEXT  # noqa: E402

plt.rcParams.update({
    "figure.facecolor": "white", "axes.facecolor": "white",
    "axes.edgecolor": C_GRID, "axes.labelcolor": C_TEXT, "text.color": C_TEXT,
    "xtick.color": C_MUTED, "ytick.color": C_MUTED, "grid.color": C_GRID,
    "font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
})


def b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def grouped_bar(df, group_col, title, xlabel=""):
    """Net profit of every configuration, coloured by group."""
    fig, ax = plt.subplots(figsize=(11, 3.4))
    order = sorted(df[group_col].unique(), key=str)
    data = [df[df[group_col] == g]["net_profit"].values for g in order]
    pos = range(len(order))
    for i, (g, vals) in enumerate(zip(order, data)):
        ax.scatter([i] * len(vals), vals, s=26, alpha=0.65,
                   color=[C_POS if v >= 0 else C_NEG for v in vals], zorder=3)
        ax.hlines(vals.mean(), i - 0.28, i + 0.28, color="#2f6f9f", lw=2.2,
                  zorder=4)
    ax.axhline(0, color=C_MUTED, lw=0.9)
    ax.set_xticks(list(pos))
    ax.set_xticklabels([str(g) for g in order])
    ax.set_ylabel("Net P&L per configuration")
    if xlabel:
        ax.set_xlabel(xlabel)
    ax.set_title(title + "   (dot = one configuration, bar = mean)",
                 loc="left", fontsize=11, weight="bold")
    ax.grid(True, axis="y", lw=0.6)
    return b64(fig)


def table(df, money=(), pct=(), index_name=""):
    if df is None or df.empty:
        return "<p class='muted'>No data.</p>"
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    rows = []
    for idx, row in df.iterrows():
        cells = [f"<td class='idx'>{html.escape(str(idx))}</td>"]
        for c in df.columns:
            v = row[c]
            if isinstance(v, (int, float)) and pd.notna(v):
                cls = ""
                if c in money:
                    cls = "pos" if v > 0 else ("neg" if v < 0 else "")
                    cells.append(f"<td class='num {cls}'>{v:,.0f}</td>")
                elif c in pct:
                    cells.append(f"<td class='num'>{v:,.1f}%</td>")
                else:
                    cells.append(f"<td class='num'>{v:,.2f}</td>")
            else:
                cells.append(f"<td>{html.escape(str(v))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (f"<table><thead><tr><th>{html.escape(index_name)}</th>{head}</tr>"
            f"</thead><tbody>{''.join(rows)}</tbody></table>")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="backtest/orb/matrix")
    a = p.parse_args()
    sd = os.path.join(a.dir, "_summary")
    df = pd.read_csv(os.path.join(sd, "all_results.csv"))
    info = json.load(open(os.path.join(sd, "run_info.json"), encoding="utf-8"))

    inc = df[df.news_mode == "INCLUDE_NEWS"].copy()
    news_configured = info.get("news_days_configured", 0)

    MONEY = ("net_profit", "avg_net_per_config", "total_net")
    PCT = ("avg_win_rate", "worst_dd_pct", "win_rate", "max_dd_pct")

    def agg(by, label):
        g = inc.groupby(by)
        t = pd.DataFrame({
            "configs": g.size(),
            "profitable": g["net_profit"].apply(lambda x: int((x > 0).sum())),
            "total_net": g["net_profit"].sum(),
            "avg_net_per_config": g["net_profit"].mean(),
            "median_PF": g["profit_factor"].median(),
            "avg_win_rate": g["win_rate"].mean(),
            "worst_dd_pct": g["max_dd_pct"].max(),
            "trades": g["trades"].sum(),
        }).sort_values("total_net", ascending=False)
        t.index.name = label
        return t

    tf_t, se_t, orb_t = (agg("timeframe", "Timeframe"),
                         agg("session", "Session"),
                         agg("orb_minutes", "ORB minutes"))
    has_rr = "risk_reward" in df.columns and df.risk_reward.nunique() > 1
    img_tf = grouped_bar(inc, "timeframe", "By timeframe")
    img_se = grouped_bar(inc, "session", "By session")
    img_orb = grouped_bar(inc, "orb_minutes", "By ORB duration", "minutes")

    # timeframe x session grid
    piv = inc.pivot_table(index="timeframe", columns="session",
                          values="net_profit", aggfunc="sum")
    piv = piv.reindex(index=["M1", "M5", "M15"],
                      columns=["ASIA", "LONDON", "NEW_YORK"])

    # --- risk:reward ---------------------------------------------------
    rr_block = ""
    if has_rr:
        rr_t = agg("risk_reward", "R:R").sort_index()
        img_rr = grouped_bar(inc, "risk_reward", "By risk:reward", "R multiple")

        # the R:R curve for the strongest base configurations
        fig, ax = plt.subplots(figsize=(11, 3.8))
        inc2 = inc.copy()
        inc2["base"] = (inc2.timeframe + "_" + inc2.session + "_ORB"
                        + inc2.orb_minutes.astype(str))
        tot = inc2.groupby("base")["net_profit"].sum().nlargest(6).index
        for b in tot:
            g = inc2[inc2.base == b].sort_values("risk_reward")
            ax.plot(g.risk_reward, g.net_profit, marker="o", ms=4, lw=1.6, label=b)
        ax.axhline(0, color=C_MUTED, lw=0.9)
        ax.set_xlabel("R:R"); ax.set_ylabel("Net P&L")
        ax.set_title("Net P&L vs R:R — six strongest configurations",
                     loc="left", fontsize=11, weight="bold")
        ax.grid(True, lw=0.6); ax.legend(fontsize=8, frameon=False, ncol=2)
        img_rr_curve = b64(fig)

        best_rr_path = os.path.join(sd, "best_rr_per_config.csv")
        best_tbl = ""
        if os.path.exists(best_rr_path):
            brr = pd.read_csv(best_rr_path)
            best_tbl = (f"<h2>Best R:R for each configuration</h2>"
                        f"<div class='scroll'>"
                        f"{brr.to_html(index=False, border=0)}</div>")
            counts = brr.risk_reward.value_counts().sort_index()
            best_tbl += ("<div class='card'><b>How often each R:R wins:</b><br>"
                         + "  ".join(f"1:{k:g} &rarr; {v}" for k, v in counts.items())
                         + "</div>")

        rr_block = (f"<h2>4. Best risk : reward</h2>"
                    f"<div class='card'><img src='data:image/png;base64,{img_rr}'></div>"
                    f"<div class='card'>{table(rr_t, MONEY, PCT, 'R:R')}</div>"
                    f"<div class='card'><img src='data:image/png;base64,"
                    f"{img_rr_curve}'></div>{best_tbl}")

    # news effect
    eff = pd.read_csv(os.path.join(sd, "news_effect.csv"))
    if news_configured == 0:
        news_block = (
            "<div class='card warn'><b>No News Days were configured for this "
            "run.</b><br>Every <code>SKIP_NEWS</code> configuration is therefore "
            "identical to its <code>INCLUDE_NEWS</code> twin, and the news "
            "comparison below is empty by construction — not a finding. Add the "
            "dates and re-run to populate it:"
            "<pre>python tools/run_matrix.py --news-days news_days.txt \\\n"
            "    --data data/gc_1m_merged.parquet \\\n"
            "    --start 2026-01-01 --end 2026-08-13 --out backtest/orb/matrix</pre></div>")
    else:
        cols = ["timeframe", "session", "orb_minutes", "trades_include",
                "trades_skip", "net_profit_include", "net_profit_skip",
                "delta_net_profit", "profit_factor_include",
                "profit_factor_skip", "delta_profit_factor",
                "delta_win_rate", "delta_max_dd_pct", "delta_max_consec_losses"]
        have = [c for c in cols if c in eff.columns]
        improved = int((eff["delta_net_profit"] > 0).sum())
        news_block = (
            f"<p>Skipping news days improved net profit in <b>{improved} of "
            f"{len(eff)}</b> matched pairs "
            f"({news_configured} news date(s) applied).</p>"
            f"<div class='scroll'>{eff[have].to_html(index=False, border=0)}</div>")

    best = inc.sort_values("net_profit", ascending=False).iloc[0]
    worst = inc.sort_values("net_profit").iloc[0]

    cols = ["config", "trades", "net_profit", "profit_factor", "win_rate",
            "max_dd_pct", "max_consec_losses", "avg_r"]
    full = df.sort_values("net_profit", ascending=False)[cols]

    def tile(k, v, cls=""):
        return (f"<div class='tile'><div class='k'>{k}</div>"
                f"<div class='v {cls}'>{v}</div></div>")

    tiles = "".join([
        tile("Configurations", f"{len(df)}"),
        tile("Period", f"{info['period_start']} → {info['period_end']}"),
        tile("Best configuration", best["config"].replace("_INCLUDE_NEWS", "")),
        tile("Best net P&L", f"{best['net_profit']:,.0f}", "pos"),
        tile("Best profit factor", f"{best['profit_factor']:.2f}"),
        tile("Best R:R", f"1:{best['risk_reward']:g}"
             if "risk_reward" in best else "-"),
        tile("Worst configuration", worst["config"].replace("_INCLUDE_NEWS", "")),
        tile("Worst net P&L", f"{worst['net_profit']:,.0f}", "neg"),
        tile("Profitable (of 27)", f"{int((inc.net_profit > 0).sum())}"),
    ])

    sess = info["sessions"]
    sess_rows = "".join(
        f"<tr><td>{k.replace('_',' ').title()}</td><td class='num'>"
        f"{v['open_ny']} New York</td><td class='num'>until "
        f"{sess[v['next']]['open_ny']} ({v['next'].replace('_',' ').title()} "
        f"opens)</td></tr>" for k, v in sess.items())

    html_out = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ORB test matrix — 54 configurations</title>
<style>{_CSS}
.warn{{border-left:4px solid #d98324;background:#fff8f0}}
pre{{background:#eef1f4;padding:10px;border-radius:6px;overflow:auto;font-size:12px}}
.grid3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}}
@media(max-width:900px){{.grid3{{grid-template-columns:1fr}}}}
</style></head><body><div class="wrap">
<h1>ORB test matrix — 54 configurations</h1>
<p class="sub">3 timeframes × 3 sessions × 3 ORB durations × 2 news modes
&middot; {info['period_start']} → {info['period_end']}
&middot; {info['bars']:,} bars &middot; gross P&amp;L, no costs
&middot; engine: <code>{info['engine']}</code></p>

<div class="grid">{tiles}</div>

<h2>Session definitions</h2>
<div class="card"><table><thead><tr><th>Session</th><th>Opens</th>
<th>Trades until</th></tr></thead><tbody>{sess_rows}</tbody></table>
<p class="muted">All times America/New_York, DST-aware. Each session builds its
opening range from its open, then trades from the end of that range until the
next session opens.</p></div>

<h2>1. Best timeframe</h2>
<div class="card"><img src="data:image/png;base64,{img_tf}"></div>
<div class="card">{table(tf_t, MONEY, PCT, 'Timeframe')}</div>

<h2>2. Best session</h2>
<div class="card"><img src="data:image/png;base64,{img_se}"></div>
<div class="card">{table(se_t, MONEY, PCT, 'Session')}</div>

<h2>3. Best ORB duration</h2>
<div class="card"><img src="data:image/png;base64,{img_orb}"></div>
<div class="card">{table(orb_t, MONEY, PCT, 'ORB minutes')}</div>

<h2>Timeframe × session grid <span class="muted"
style="text-transform:none;font-weight:400">— net P&amp;L, include-news</span></h2>
<div class="card">{table(piv.round(0), tuple(piv.columns), (), 'Timeframe')}</div>

{rr_block}

<h2>5. News filter effect</h2>
{news_block}

<h2>All {len(df)} configurations</h2>
<div class="scroll">{table(full.set_index('config'),
                           ('net_profit',), ('win_rate', 'max_dd_pct'),
                           'Configuration')}</div>

<h2>Reading these results</h2>
<div class="card"><ul>
<li>Every run used the <b>same unchanged strategy and engine</b>. Only the
configuration differs, so the comparison is like-for-like.</li>
<li>Sessions do <b>not</b> get equal trading windows — that follows from the
"trade until the next session opens" rule. Asia gets ~7.5 h, London ~6.5 h,
New York ~9.5 h. Part of any session difference is window length, not edge.</li>
<li>The period is <b>{info['period_start']} → {info['period_end']}</b>, about
seven months, during a strong gold uptrend. Treat one period as one sample.</li>
<li>P&amp;L is <b>gross</b>. Costs scale with trade count, so the
high-frequency M1 configurations would suffer most once costs are applied.</li>
<li>54 configurations is a wide search. The best result in a grid this size is
partly selection; prefer settings whose neighbours also perform well.</li>
</ul></div>

<p class="muted" style="margin-top:26px">Every configuration has its own folder
under <code>{html.escape(a.dir)}/</code> containing its full report, trade list,
day/month/hour breakdowns, equity curve, journal and the exact config used.</p>
</div></body></html>"""

    out = os.path.join(sd, "comparison.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html_out)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
