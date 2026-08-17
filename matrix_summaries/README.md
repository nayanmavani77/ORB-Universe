# ORB permutation results — summary archive

Regenerated 2026-08-16 from the corrected engine.

Both result sets below were produced AFTER the contract-roll fix (the front
month now rolls at 18:00 New York, the CME session boundary, instead of at
midnight). Any earlier summary you had was generated before that fix and
contained roughly $78k of P&L that came from the calendar-spread gap rather
than from price. Those older numbers should not be used.

Period for both: **2026-01-01 to 2026-08-13**
Bars: 218,252 one-minute bars, gold front month.
P&L is **GROSS** — no spread, slippage or commission.

---

## 1. `54_configs_RR2/` — the base matrix

3 timeframes x 3 sessions x 3 ORB durations x 2 news modes = **54 runs**, all at R:R 1:2.

Top 5 by net P&L:

| config                     |   trades |   net_profit |   profit_factor |
|:---------------------------|---------:|-------------:|----------------:|
| M1_ASIA_ORB30_SKIP_NEWS    |      458 |      115,085 |            1.63 |
| M1_ASIA_ORB30_INCLUDE_NEWS |      677 |      114,935 |            1.41 |
| M5_ASIA_ORB60_INCLUDE_NEWS |      375 |      111,485 |            1.49 |
| M5_ASIA_ORB30_INCLUDE_NEWS |      482 |       91,165 |            1.38 |
| M1_ASIA_ORB60_INCLUDE_NEWS |      478 |       89,120 |            1.33 |

Files:

| file | what it holds |
|---|---|
| `comparison.html` | the readable report — open this first |
| `all_results.csv` | every run, one row each |
| `by_session.csv` | Asia vs London vs New York |
| `by_timeframe.csv` | M1 vs M5 vs M15 |
| `by_orb_duration.csv` | 15 vs 30 vs 60 minute ranges |
| `by_news_mode.csv` | include vs skip news days |
| `by_timeframe_session.csv` | timeframe x session grid |
| `news_effect.csv` | per-configuration news impact |
| `run_info.json` | the exact settings the run used |

---

## 2. `486_configs_RR_sweep/` — the R:R sweep

The same 54 configurations run at nine risk:reward values (1:1 through 1:5 in
0.5 steps) = **486 runs**.

Top 5 by net P&L:

| config                           |   trades |   net_profit |   profit_factor |
|:---------------------------------|---------:|-------------:|----------------:|
| M5_ASIA_ORB60_INCLUDE_NEWS_RR4   |      361 |      149,780 |            1.63 |
| M5_ASIA_ORB30_INCLUDE_NEWS_RR4p5 |      461 |      144,033 |            1.56 |
| M1_ASIA_ORB30_INCLUDE_NEWS_RR4p5 |      624 |      134,512 |            1.44 |
| M5_ASIA_ORB60_INCLUDE_NEWS_RR4p5 |      361 |      133,365 |            1.56 |
| M5_ASIA_ORB60_INCLUDE_NEWS_RR3p5 |      363 |      133,124 |            1.56 |

Total net P&L by session, across all R:R values:

| sess     |   net_profit |
|:---------|-------------:|
| ASIA     |   11,179,260 |
| LONDON   |   -6,575,693 |
| NEW_YORK |    2,136,665 |

Total net P&L by risk:reward:

|   risk_reward |   net_profit |
|--------------:|-------------:|
|           1   |      504,510 |
|           1.5 |      446,151 |
|           2   |      399,185 |
|           2.5 |      288,036 |
|           3   |      503,440 |
|           3.5 |      699,735 |
|           4   |    1,108,295 |
|           4.5 |    1,352,535 |
|           5   |    1,438,345 |

Extra files beyond the list above:

| file | what it holds |
|---|---|
| `best_rr_per_config.csv` | the winning R:R for each of the 54 base configs |
| `by_risk_reward.csv` | totals per R:R value |
| `by_session_rr.csv` | session x R:R grid |
| `by_timeframe_rr.csv` | timeframe x R:R grid |

---

## Two cautions on reading these

**The R:R result sits on the boundary.** Net P&L dips to a minimum at 1:2 then
climbs all the way to the edge of the tested range at 1:5. That is not
"1:5 is optimal" — at high R:R the take-profit is rarely reached (about 10% of
trades at 1:5), so the strategy is drifting toward "stop-loss plus stop-time
exit". Testing with the take-profit disabled entirely would tell you whether
R:R is doing any work at all.

**486 trials on 7.5 months is a wide search.** The best result out of 486 draws
over a single market regime carries real selection luck. Data back to 2022-11
is available in `data/gc_1m_merged.parquet`; running the top few configurations
on 2023-2025 costs nothing and is the check that settles it.

---

## Reproducing these

```
python tools/run_matrix.py --data data/gc_1m_merged.parquet \
    --start 2026-01-01 --end 2026-08-13 --out matrix_out

python tools/run_matrix.py --data data/gc_1m_merged.parquet \
    --start 2026-01-01 --end 2026-08-13 \
    --rr 1:5 --light --out rr_matrix_out
```

Note that `run_matrix.py` now calls `use_single_session()`, so it drives its own
window correctly even though `config.yaml` has a `sessions:` block. Before that
fix every permutation silently ran the config file's own session instead.
