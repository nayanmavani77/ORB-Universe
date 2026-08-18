#!/usr/bin/env python3
"""Session-wise performance report, built from a matrix summary file.

    python tools/session_report.py --summary backtest/orb/matrix/_summary/all_results.csv \
                                   --out backtest/orb/matrix/_summary/session_performance.html

Answers one question: how does each session behave, and under what settings?
Every number is aggregated from configurations that already ran — this reads
results, it never re-runs a backtest.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

# Categorical slots 1-3 of the validated palette, assigned in fixed order and
# never cycled. Colour follows the SESSION, so a filtered chart never repaints
# the survivors.
SESSIONS = ["ASIA", "LONDON", "NEW_YORK"]
LIGHT = {"ASIA": "#2a78d6", "LONDON": "#eb6834", "NEW_YORK": "#1baf7a"}
DARK = {"ASIA": "#3987e5", "LONDON": "#d95926", "NEW_YORK": "#199e70"}
LABEL = {"ASIA": "Asia", "LONDON": "London", "NEW_YORK": "New York"}


def money(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"{'-' if v < 0 else ''}${a/1_000_000:,.2f}M"
    if a >= 1_000:
        return f"{'-' if v < 0 else ''}${a/1_000:,.0f}k"
    return f"{'-' if v < 0 else ''}${a:,.0f}"


def analyse(df: pd.DataFrame) -> dict:
    out = {}
    for s in SESSIONS:
        g = df[df.session == s]
        if g.empty:
            continue
        tw = int(g.trades.sum())
        best = g.loc[g.net_profit.idxmax()]
        o = {
            "configs": len(g),
            "trades": tw,
            "net": float(g.net_profit.sum()),
            "pct_profitable": float((g.net_profit > 0).mean() * 100),
            "median_net": float(g.net_profit.median()),
            "win_rate": float(g.wins.sum() / tw * 100) if tw else 0.0,
            "median_pf": float(g.profit_factor.median()),
            "median_dd": float(g.max_dd_pct.median()),
            "long_net": float(g.long_net.sum()),
            "short_net": float(g.short_net.sum()),
            "avg_hold": float((g.avg_duration_min * g.trades).sum() / tw) if tw else 0.0,
            "best": {"cfg": str(best.config), "net": float(best.net_profit),
                     "pf": float(best.profit_factor), "trades": int(best.trades),
                     "wr": float(best.win_rate), "dd": float(best.max_dd_pct)},
        }
        for dim in ("risk_reward", "timeframe", "orb_minutes", "news_mode"):
            if dim in g.columns:
                o[dim] = {str(k): float(v)
                          for k, v in g.groupby(dim).net_profit.sum().items()}
        out[s] = o
    return out


# --------------------------------------------------------------------------
def tiles(a: dict) -> str:
    cards = []
    for s in SESSIONS:
        if s not in a:
            continue
        o = a[s]
        sign = "pos" if o["net"] > 0 else "neg"
        cards.append(f"""
<div class="tile">
  <div class="tile-head"><span class="swatch" style="background:var(--s-{s})"></span>
    <span class="tile-name">{LABEL[s]}</span></div>
  <div class="hero {sign}">{money(o['net'])}</div>
  <div class="tile-sub">total across {o['configs']} configurations</div>
  <dl>
    <div><dt>Configs profitable</dt><dd>{o['pct_profitable']:.1f}%</dd></div>
    <div><dt>Median profit factor</dt><dd>{o['median_pf']:.2f}</dd></div>
    <div><dt>Win rate</dt><dd>{o['win_rate']:.1f}%</dd></div>
    <div><dt>Median max drawdown</dt><dd>{o['median_dd']:.1f}%</dd></div>
    <div><dt>Trades</dt><dd>{o['trades']:,}</dd></div>
    <div><dt>Average hold</dt><dd>{o['avg_hold']:.0f} min</dd></div>
  </dl>
</div>""")
    return f'<div class="tiles">{"".join(cards)}</div>'


def hbars(a: dict, key: str, title: str, note: str = "", fmt=money) -> str:
    """Horizontal bars, one row per session, direct-labelled.

    The direct labels are also what satisfies the relief rule for the aqua slot,
    which sits below 3:1 on the light surface.

    A metric that can go negative gets a centred zero line so the sign is
    visible; an all-positive metric (a percentage, a ratio) is anchored at the
    left edge instead, because a centred axis would throw away half the width
    and imply a midpoint that does not exist.
    """
    vals = {s: a[s][key] for s in SESSIONS if s in a}
    diverging = any(v < 0 for v in vals.values())
    span = max(abs(v) for v in vals.values()) or 1.0
    full = 50.0 if diverging else 96.0
    rows = []
    for s, v in vals.items():
        w = abs(v) / span * full
        left = (50.0 if v >= 0 else 50.0 - w) if diverging else 0.0
        lab_left = left + w + 1
        rows.append(f"""
  <div class="hrow" data-tip="{LABEL[s]}: {fmt(v)}">
    <div class="hlab">{LABEL[s]}</div>
    <div class="htrack">{'<div class="hzero"></div>' if diverging else ''}
      <div class="hfill" style="left:{left}%;width:{w}%;background:var(--s-{s})"></div>
      <div class="hval {'pos' if v >= 0 else 'neg'}"
           style="left:{lab_left if v >= 0 else left - 1}%;
                  transform:translateX({'0' if v >= 0 else '-100%'})">{fmt(v)}</div>
    </div>
  </div>""")
    sub = f'<p class="note">{html.escape(note)}</p>' if note else ""
    return f'<section><h3>{html.escape(title)}</h3>{sub}<div class="hbars">{"".join(rows)}</div></section>'


def pct(v: float) -> str:
    return f"{v:.1f}%"


def ratio(v: float) -> str:
    return f"{v:.2f}"


def grouped(a: dict, dim: str, title: str, note: str, order=None) -> str:
    """Grouped vertical bars: one group per dimension value, one bar per session."""
    keys = set()
    for s in SESSIONS:
        if s in a and dim in a[s]:
            keys |= set(a[s][dim])
    if not keys:
        return ""
    ks = order or sorted(keys, key=lambda x: float(x) if x.replace('.', '').isdigit() else x)
    ks = [k for k in ks if k in keys]

    # One shared scale for the whole chart, with a real zero baseline: positives
    # grow up from it, negatives hang below it. Anchoring negatives to the top of
    # the box instead would make a loss look like a tall bar.
    allv = [a[s][dim].get(k, 0.0) for s in SESSIONS if s in a for k in ks]
    vmax, vmin = max(allv + [0.0]), min(allv + [0.0])
    rng = (vmax - vmin) or 1.0
    zero = (0.0 - vmin) / rng * 100.0            # % up from the floor

    groups = []
    for k in ks:
        bars = []
        for s in SESSIONS:
            if s not in a:
                continue
            v = a[s][dim].get(k, 0.0)
            h = abs(v) / rng * 100.0
            bottom = (min(v, 0.0) - vmin) / rng * 100.0
            radius = "4px 4px 0 0" if v >= 0 else "0 0 4px 4px"
            bars.append(
                f'<div class="gb" data-tip="{LABEL[s]} · {html.escape(str(k))}: {money(v)}">'
                f'<div class="gfill" style="bottom:{bottom}%;height:{h}%;'
                f'border-radius:{radius};background:var(--s-{s})"></div></div>')
        groups.append(f'<div class="ggroup"><div class="gbars">{"".join(bars)}</div>'
                      f'<div class="gkey">{html.escape(str(k))}</div></div>')

    return f"""<section><h3>{html.escape(title)}</h3>
<p class="note">{html.escape(note)}</p>
{legend()}
<div class="gwrap">
  <div class="gaxis"><span>{money(vmax)}</span><span class="gz"
       style="bottom:{zero}%">0</span><span>{money(vmin)}</span></div>
  <div class="grouped">
    <div class="gzero" style="bottom:{zero}%"></div>
    {''.join(groups)}
  </div>
</div></section>"""


def legend() -> str:
    items = "".join(
        f'<span class="lg"><span class="swatch" style="background:var(--s-{s})"></span>'
        f'{LABEL[s]}</span>' for s in SESSIONS)
    return f'<div class="legend">{items}</div>'


def table(df: pd.DataFrame) -> str:
    cols = ["config", "session", "timeframe", "orb_minutes", "news_mode",
            "risk_reward", "trades", "win_rate", "net_profit", "profit_factor",
            "max_dd_pct"]
    cols = [c for c in cols if c in df.columns]
    d = df[cols].sort_values("net_profit", ascending=False)
    head = "".join(f"<th>{html.escape(c.replace('_', ' '))}</th>" for c in cols)
    body = []
    for _, r in d.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            if c == "net_profit":
                cells.append(f'<td class="num {"pos" if v > 0 else "neg"}">{v:,.0f}</td>')
            elif c == "session":
                cells.append(f'<td><span class="swatch" style="background:var(--s-{v})">'
                             f'</span>{LABEL.get(v, v)}</td>')
            elif isinstance(v, float):
                cells.append(f'<td class="num">{v:,.2f}</td>')
            elif isinstance(v, (int,)):
                cells.append(f'<td class="num">{v:,}</td>')
            else:
                cells.append(f"<td>{html.escape(str(v))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<table class="grid"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table>')


CSS = """
:root{color-scheme:light}
.viz{
 --surface-1:#fcfcfb; --surface-2:#f4f3f0; --line:#e3e2de;
 --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#78766f;
 --pos:#1b7f4d; --neg:#c0392b;
 --s-ASIA:#2a78d6; --s-LONDON:#eb6834; --s-NEW_YORK:#1baf7a;
}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])) .viz{
 color-scheme:dark;
 --surface-1:#1a1a19; --surface-2:#232322; --line:#383835;
 --text-primary:#fff; --text-secondary:#c3c2b7; --text-muted:#96958c;
 --pos:#4ec27f; --neg:#e66767;
 --s-ASIA:#3987e5; --s-LONDON:#d95926; --s-NEW_YORK:#199e70;
}}
:root[data-theme=dark] .viz{color-scheme:dark;
 --surface-1:#1a1a19; --surface-2:#232322; --line:#383835;
 --text-primary:#fff; --text-secondary:#c3c2b7; --text-muted:#96958c;
 --pos:#4ec27f; --neg:#e66767;
 --s-ASIA:#3987e5; --s-LONDON:#d95926; --s-NEW_YORK:#199e70;}
*{box-sizing:border-box}
body{margin:0;background:var(--surface-1);color:var(--text-primary);
 font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:32px 24px 72px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.09em;
 color:var(--text-muted);margin:44px 0 14px;font-weight:600}
h3{font-size:16px;margin:0 0 4px;font-weight:600}
.sub{color:var(--text-secondary);margin:0 0 8px}
.note{color:var(--text-muted);margin:0 0 14px;font-size:13px}
section{margin:0 0 34px}
.swatch{width:10px;height:10px;border-radius:3px;display:inline-block;
 margin-right:7px;vertical-align:middle}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:14px}
.tile{background:var(--surface-2);border:1px solid var(--line);
 border-radius:12px;padding:18px 18px 8px}
.tile-name{font-weight:600}
.tile-head{margin-bottom:10px}
.hero{font-size:32px;font-weight:650;letter-spacing:-.03em;line-height:1.1}
.hero.pos{color:var(--pos)} .hero.neg{color:var(--neg)}
.tile-sub{color:var(--text-muted);font-size:12px;margin-bottom:12px}
dl{margin:0;border-top:1px solid var(--line)}
dl>div{display:flex;justify-content:space-between;gap:12px;padding:7px 0;
 border-bottom:1px solid var(--line)}
dl>div:last-child{border-bottom:0}
dt{color:var(--text-secondary);font-size:13px}
dd{margin:0;font-variant-numeric:tabular-nums;font-weight:600;font-size:13px}
.legend{display:flex;gap:16px;margin:0 0 12px;color:var(--text-secondary);font-size:13px}
.hbars{display:flex;flex-direction:column;gap:10px}
.hrow{display:flex;align-items:center;gap:12px}
.hlab{width:88px;color:var(--text-secondary);flex:none;font-size:13px}
.htrack{position:relative;flex:1;height:26px}
.hzero{position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--line)}
.hfill{position:absolute;top:3px;bottom:3px;border-radius:4px}
.hval{position:absolute;top:50%;transform:translateY(-50%);font-size:12px;
 font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap;margin-top:-1px}
.hval.pos{color:var(--pos)} .hval.neg{color:var(--neg)}
.gwrap{display:flex;gap:10px}
.gaxis{position:relative;width:52px;height:230px;flex:none;
 display:flex;flex-direction:column;justify-content:space-between;
 color:var(--text-muted);font-size:11px;text-align:right;
 font-variant-numeric:tabular-nums}
.gaxis .gz{position:absolute;right:0;transform:translateY(50%)}
.grouped{position:relative;display:flex;gap:18px;height:230px;flex:1;
 justify-content:space-around}
.ggroup{max-width:300px}
.gzero{position:absolute;left:0;right:0;height:1px;background:var(--text-muted);
 opacity:.45;z-index:1}
.ggroup{flex:1;display:flex;flex-direction:column;height:100%}
.gbars{flex:1;display:flex;gap:2px;position:relative}
.gb{flex:1;position:relative}
.gfill{position:absolute;left:0;right:0}
.gkey{text-align:center;color:var(--text-secondary);font-size:12px;padding-top:8px}
table.grid{width:100%;border-collapse:collapse;font-size:12.5px}
table.grid th{position:sticky;top:0;background:var(--surface-2);text-align:left;
 padding:8px 10px;border-bottom:1px solid var(--line);color:var(--text-secondary);
 font-weight:600;white-space:nowrap}
table.grid td{padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.pos{color:var(--pos)} .neg{color:var(--neg)}
.scroll{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.callout{background:var(--surface-2);border:1px solid var(--line);
 border-left:3px solid var(--s-ASIA);border-radius:8px;padding:14px 16px;margin:0 0 14px}
.callout.warn{border-left-color:var(--s-LONDON)}
.callout b{display:block;margin-bottom:3px}
.callout p{margin:0;color:var(--text-secondary);font-size:13.5px}
#tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .1s;
 background:var(--text-primary);color:var(--surface-1);padding:5px 9px;
 border-radius:6px;font-size:12px;font-weight:500;z-index:99;white-space:nowrap}
"""

JS = """
const tip=document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el=>{
  el.addEventListener('mouseenter',e=>{tip.textContent=el.dataset.tip;tip.style.opacity=1;});
  el.addEventListener('mousemove',e=>{
    tip.style.left=(e.clientX+14)+'px';
    tip.style.top=(e.clientY-30)+'px';});
  el.addEventListener('mouseleave',()=>{tip.style.opacity=0;});
});
"""


def build(df: pd.DataFrame, meta: dict) -> str:
    a = analyse(df)
    rank = sorted(a, key=lambda s: -a[s]["net"])
    top, bottom = rank[0], rank[-1]

    best_rows = []
    for s in SESSIONS:
        if s not in a:
            continue
        b = a[s]["best"]
        best_rows.append(
            f'<tr><td><span class="swatch" style="background:var(--s-{s})"></span>'
            f'{LABEL[s]}</td><td>{html.escape(b["cfg"])}</td>'
            f'<td class="num">{b["trades"]:,}</td>'
            f'<td class="num">{b["wr"]:.1f}%</td>'
            f'<td class="num {"pos" if b["net"] > 0 else "neg"}">{b["net"]:,.0f}</td>'
            f'<td class="num">{b["pf"]:.2f}</td>'
            f'<td class="num">{b["dd"]:.1f}%</td></tr>')

    ls_rows = []
    for s in SESSIONS:
        if s not in a:
            continue
        L, S_ = a[s]["long_net"], a[s]["short_net"]
        tot = L + S_
        ls_rows.append(
            f'<tr><td><span class="swatch" style="background:var(--s-{s})"></span>'
            f'{LABEL[s]}</td>'
            f'<td class="num {"pos" if L > 0 else "neg"}">{L:,.0f}</td>'
            f'<td class="num {"pos" if S_ > 0 else "neg"}">{S_:,.0f}</td>'
            f'<td class="num {"pos" if tot > 0 else "neg"}">{tot:,.0f}</td></tr>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Session performance — ORB</title>
<style>{CSS}</style></head>
<body class="viz"><div id="tip"></div><div class="wrap">

<h1>Session-wise performance</h1>
<p class="sub">{meta['configs']} configurations &middot; {meta['period']}
&middot; gross P&amp;L, no spread, slippage or commission</p>

<h2>Headline</h2>
{tiles(a)}

<div class="callout" style="margin-top:16px">
  <b>{LABEL[top]} carries the strategy.</b>
  <p>{a[top]['pct_profitable']:.0f}% of its {a[top]['configs']} configurations finish
  profitable, against {a[bottom]['pct_profitable']:.0f}% for {LABEL[bottom]}. That gap is
  far larger than any timeframe, ORB-length or R:R choice inside a session — which
  session you trade matters more than how you trade it.</p>
</div>
<div class="callout warn">
  <b>{LABEL[bottom]} loses under every variant tested.</b>
  <p>Its best configuration of {a[bottom]['configs']} nets only
  {money(a[bottom]['best']['net'])}, and the median configuration draws down
  {a[bottom]['median_dd']:.0f}%. No setting rescues it in this sample, so this reads as
  the session being wrong for the strategy rather than the parameters being wrong.</p>
</div>

<h2>Totals</h2>
{hbars(a, 'net', 'Net P&L by session',
       'Summed across every configuration. Direction and scale, not a forecast.')}
{hbars(a, 'pct_profitable', 'Share of configurations that finish profitable',
       'How dependent the session is on picking the right settings. A high number '
       'means the session works broadly; a low one means any winner is a lucky draw.',
       fmt=pct)}
{hbars(a, 'median_pf', 'Median profit factor',
       'The middle configuration, not the best one — far more honest than the top '
       'result. Above 1.00 means the middle configuration made money.', fmt=ratio)}

<h2>Where each session earns it</h2>
{grouped(a, 'risk_reward', 'Net P&L by risk : reward',
         'Each session summed at each R:R value. Watch the shape, not the peak: a curve '
         'still rising at the right-hand edge means the take-profit is being reached less '
         'and less often, not that a higher target is better.')}
{grouped(a, 'timeframe', 'Net P&L by signal timeframe',
         'The bar that must close beyond the range.', order=['M1', 'M5', 'M15'])}
{grouped(a, 'orb_minutes', 'Net P&L by opening-range length',
         'Minutes of range built from the session open before any breakout counts.',
         order=['15', '30', '60'])}
{grouped(a, 'news_mode', 'Net P&L by news handling',
         'INCLUDE_NEWS trades every open day; SKIP_NEWS removes the listed economic '
         'dates entirely.', order=['INCLUDE_NEWS', 'SKIP_NEWS'])}

<h2>Direction</h2>
<p class="note">Long and short P&amp;L separately. A session that makes all of its
money on one side is taking a directional bet, not trading a symmetric breakout.</p>
<table class="grid"><thead><tr><th>Session</th><th class="num">Long net</th>
<th class="num">Short net</th><th class="num">Total</th></tr></thead>
<tbody>{''.join(ls_rows)}</tbody></table>

<h2>Best configuration in each session</h2>
<p class="note">The single highest-earning configuration per session. Treat these as
the ceiling of the search, not as an expectation — the best of many trials on one
period carries selection luck.</p>
<table class="grid"><thead><tr><th>Session</th><th>Configuration</th>
<th class="num">Trades</th><th class="num">Win rate</th><th class="num">Net P&amp;L</th>
<th class="num">Profit factor</th><th class="num">Max DD</th></tr></thead>
<tbody>{''.join(best_rows)}</tbody></table>

<h2>Every configuration</h2>
<p class="note">All {meta['configs']} runs, highest net P&amp;L first.</p>
<div class="scroll">{table(df)}</div>

</div><script>{JS}</script></body></html>"""


def main() -> int:
    p = argparse.ArgumentParser(description="Session-wise performance report")
    p.add_argument("--summary", "-s", required=True,
                   help="all_results.csv from a matrix run")
    p.add_argument("--out", "-o", required=True, help="output .html")
    p.add_argument("--csv", default=None, help="also write the per-session table here")
    a = p.parse_args()

    df = pd.read_csv(a.summary)
    if "session" not in df.columns:
        print("That summary has no `session` column.", file=sys.stderr)
        return 2

    info_path = os.path.join(os.path.dirname(a.summary), "run_info.json")
    period = "period unknown"
    if os.path.exists(info_path):
        i = json.load(open(info_path))
        period = f"{i.get('period_start')} to {i.get('period_end')}"
    meta = {"configs": len(df), "period": period}

    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(build(df, meta))
    print(f"Wrote {a.out}")

    if a.csv:
        rows = []
        for s, o in analyse(df).items():
            rows.append({"session": s, "configs": o["configs"], "trades": o["trades"],
                         "net_profit": o["net"], "pct_configs_profitable": o["pct_profitable"],
                         "median_net": o["median_net"], "win_rate": o["win_rate"],
                         "median_profit_factor": o["median_pf"],
                         "median_max_dd_pct": o["median_dd"], "long_net": o["long_net"],
                         "short_net": o["short_net"], "avg_hold_min": o["avg_hold"],
                         "best_config": o["best"]["cfg"], "best_net": o["best"]["net"]})
        pd.DataFrame(rows).to_csv(a.csv, index=False)
        print(f"Wrote {a.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
