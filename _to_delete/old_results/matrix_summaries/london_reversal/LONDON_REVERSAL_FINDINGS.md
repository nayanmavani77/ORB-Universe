# Is there a best reversal strategy for London?

**No. The reversal edge does not survive out-of-sample testing, and I would not trade it.**

This document records how that was established, because the in-sample numbers look
convincing and it would be easy to act on them.

## What was tested

1. All **162 London configurations** from the 486-run sweep, each with ORIGINAL, R, RR
   and RRR — 648 backtests on **2026-01-01 to 2026-08-13** (the selection period).
2. The best family from that, **ORB15 + SKIP_NEWS** (27 configurations across M1/M5/M15
   and nine R:R values), re-run with all four variants on **2023-01-01 to 2025-12-31**
   — three years that played no part in choosing it.

## Step 1: in-sample, it looks excellent

Across all 486 London reversal runs, 232 of 486 finish profitable (48%), and one family stands out clearly:

| config | variant | trades | win rate | net P&L | PF | max DD | recovery |
|---|---|---:|---:|---:|---:|---:|---:|
| M5_LONDON_ORB15_SKIP_NEWS_RR2p5 | R | 107 | 36.5% | 37,249 | 1.74 | 4.5% | 7.70 |
| M5_LONDON_ORB15_SKIP_NEWS_RR2 | R | 107 | 43.9% | 40,145 | 1.89 | 4.6% | 7.43 |
| M5_LONDON_ORB15_SKIP_NEWS_RR2 | RR | 189 | 42.3% | 53,155 | 1.62 | 5.7% | 7.18 |
| M5_LONDON_ORB15_SKIP_NEWS_RR1p5 | R | 107 | 51.4% | 34,392 | 1.87 | 4.5% | 6.85 |
| M5_LONDON_ORB15_SKIP_NEWS_RR2p5 | RR | 182 | 36.3% | 45,938 | 1.51 | 7.8% | 5.87 |
| M5_LONDON_ORB15_SKIP_NEWS_RR3 | R | 107 | 33.6% | 36,670 | 1.69 | 6.1% | 5.59 |

Every one of the top slots is **M5 / M1 ORB15 with SKIP_NEWS**. That consistency is
exactly what a real effect is supposed to look like — not one lucky configuration but a
whole neighbourhood of them. Profit factors near 1.9, drawdowns under 6%.

## Step 2: out-of-sample, it collapses

The same 27 configurations, same code, on 2023-2025:

| variant | net P&L 2026 | profitable 2026 | net P&L 2023-25 | profitable 2023-25 |
|---|---:|---:|---:|---:|
| **ORIGINAL** | -1,549,155 | 0/27 | 189,958 | 20/27 |
| **R** | 518,266 | 27/27 | -218,398 | 7/27 |
| **RR** | 733,788 | 27/27 | -209,110 | 9/27 |
| **RRR** | 722,626 | 27/27 | -296,013 | 9/27 |

The reversal variants lose money across the board. Correlation between a run's 2026 net
P&L and its 2023-25 net P&L is **+0.01** — no relationship at all.

Every single one of the in-sample leaders loses out of sample:

| config | variant | 2026 net | 2026 DD | 2023-25 net | 2023-25 PF |
|---|---|---:|---:|---:|---:|
| M5_LONDON_ORB15_SKIP_NEWS_RR2p5 | R | 37,249 | 4.5% | **-6,239** | 0.96 |
| M5_LONDON_ORB15_SKIP_NEWS_RR2 | R | 40,145 | 4.6% | **-11,205** | 0.93 |
| M5_LONDON_ORB15_SKIP_NEWS_RR2 | RR | 53,155 | 5.7% | **-14,535** | 0.94 |
| M5_LONDON_ORB15_SKIP_NEWS_RR1p5 | R | 34,392 | 4.5% | **-14,980** | 0.90 |
| M5_LONDON_ORB15_SKIP_NEWS_RR2p5 | RR | 45,938 | 7.8% | **-5,370** | 0.98 |
| M5_LONDON_ORB15_SKIP_NEWS_RR3 | R | 36,670 | 6.1% | **-20,105** | 0.89 |
| M5_LONDON_ORB15_SKIP_NEWS_RR1p5 | RR | 40,349 | 6.7% | **-20,893** | 0.92 |
| M5_LONDON_ORB15_SKIP_NEWS_RR2p5 | RRR | 46,710 | 9.8% | **-5,099** | 0.98 |

## The finding that actually matters

Look at the ORIGINAL row again. In 2023-2025 the **unmodified** London strategy made
+189,958 with 20 of 27 configurations profitable. In 2026 the same configurations lost
1.55 million.

London is not structurally broken. **2026 was an unusual period for it** — and the
reversal 'edge' is nothing more than a mirror of that one bad stretch. Reverse a
strategy that happened to lose, on the same data you measured the loss on, and it will
always look profitable. That is arithmetic, not an edge.

Best ORIGINAL London configurations on 2023-2025, for reference:

| config | trades | win rate | net P&L | PF | max DD |
|---|---:|---:|---:|---:|---:|
| M5_LONDON_ORB15_SKIP_NEWS_RR1 | 2,720 | 51.8% | 25,085 | 1.07 | 12.2% |
| M1_LONDON_ORB15_SKIP_NEWS_RR3p5 | 3,390 | 25.9% | 30,560 | 1.05 | 14.8% |
| M1_LONDON_ORB15_SKIP_NEWS_RR3 | 3,442 | 28.1% | 30,605 | 1.05 | 15.9% |
| M1_LONDON_ORB15_SKIP_NEWS_RR1p5 | 3,780 | 41.2% | 16,020 | 1.03 | 13.2% |
| M5_LONDON_ORB15_SKIP_NEWS_RR1p5 | 2,599 | 42.0% | 19,595 | 1.05 | 15.6% |

Profit factors of 1.03-1.07 are thin — this is not a recommendation either. But it is
a very different picture from 'London loses under every variant tested', which is what
the 2026-only sweep said.

## What this means for the wider results

The 486-configuration sweep is a **single 7.5-month window**. This exercise shows that
window is not representative for London, and there is no reason to assume it is
representative for Asia either. Before trading any configuration from that sweep, run it
on 2023-2025 the way this document does. The data is already in `gc_1m_merged.parquet`;
it costs nothing but time.
