# London reversal — walk-forward test

**Question asked:** not *"will this edge last forever"* — you already answered that, and
the answer is no. The question is the other one: **when you re-fit to recent data and
trade forward, does "reverse the recent losers" make money?** That is your actual
procedure, and it assumes nothing about permanence.

**Answer: no. 80 out of 80 versions of the rule lose money.**

---

## How it was tested

For every month in the history:

1. Look **only** at the F months before it.
2. Rank all 162 London configurations by how badly they lost in that window.
3. Take the K worst, pick the reversal variant (R / RR / RRR) that worked best there.
4. Trade that choice through the next month and record the result.
5. Roll forward one month and repeat.

Every recorded trade lands on data the selection never saw, but the fit is always
**recent** — the same way you actually trade. Nothing is assumed to persist.

Built: 162 London configurations × 4 variants × 2023-01-02 → 2026-08-13
= **648 full backtests over 1,279,600 bars, 1,036,935 trades**, unchanged engine,
unchanged strategy, no costs applied.

---

## Result 1 — the reversal loses in every window length and selection size

Net P&L over 41–43 forward months, monthly re-fit:

| fit window | worst 1 | worst 3 | worst 5 | worst 10 |
|---|---|---|---|---|
| 1 month  | −29,673 | −87,085 | −180,494 | −348,825 |
| 2 months | −72,084 | −173,784 | −243,995 | −251,194 |
| 3 months | −40,660 | −107,584 | −132,238 | −165,488 |
| 6 months | −19,531 | −43,587 | −108,808 | −197,035 |

Widening the search to **80 rules** (fit 1/2/3/6/12 months × worst 1/3/5/10 ×
variant R / RR / RRR / best-in-fit):

- **profitable rules: 0 of 80**
- best: **−15,170** (fit 12 months, worst 1, RR variant, 32 months, max DD −38,588)
- median: −134,383 · worst: −392,239

There is no corner of the rule space that works. This is not a marginal result.

## Result 2 — the counterfactuals, so the reversal is judged against the right baseline

Same selection, three things you could do with it (fit 3 months, worst 5, 41 months):

| what you do | net P&L | trades | winning months | max DD |
|---|---|---|---|---|
| KEEP_WORST — keep trading the losers unchanged | −89,609 | 11,037 | 21 / 41 (51%) | −159,993 |
| REVERSE_WORST — reverse them (the idea under test) | −132,238 | 6,474 | 17 / 41 (41%) | −185,036 |
| FOLLOW_BEST — back the recent winners instead | −282,188 | 12,675 | 18 / 41 (44%) | −332,706 |

Reversing the losers is **worse than just leaving them alone**. The reversal is not
adding a lost edge back — it is subtracting.

(Backing recent winners is worse still, which is its own finding: in this pool,
recent strength is a negative signal. But "less bad" is not an edge either.)

## Result 3 — why it fails, mechanically

Rank correlation of configuration P&L between one month and the next, across all
43 months:

    mean  -0.002      median  +0.016      negative in 20 of 43 months

**Zero.** Last month's worst configuration is not next month's worst configuration —
it is a coin flip. There is nothing to reverse, because the thing being selected on
does not survive into the month you trade.

That is the whole story. It is not a claim about 2027. It is a measurement of what
happened every single month from 2023 to 2026, including the most recent ones.

---

## What this does **not** say — your regime read is correct

2026 is genuinely a different regime for London. The data agrees with you:

| year | trades | net P&L (162 configs) | profitable configs |
|---|---|---|---|
| 2023 | 111,230 | −1,495,466 | 14 / 162 |
| 2024 | 117,102 | −155,940 | 78 / 162 |
| 2025 | 112,016 | +169,128 | 88 / 162 |
| 2026 (7.5 mo) | 63,958 | −6,575,693 | **2 / 162** |

Rank correlation 2025 → 2026 is **−0.31**, 2024 → 2026 is **−0.47**. The configurations
that worked before 2026 are the ones failing now. Something changed, and you were
right to notice it.

But note what 2026 actually is: the year London ORB stops working almost completely —
2 configurations out of 162 in the black, and the best of those makes 3,300. The
"reversal edge" the earlier study found was the mirror image of that collapse. Take
the configurations that lost the most, on the data that told you they lost the most,
flip the sign, and profit appears by arithmetic. It appears on random noise too.
Walk-forward removes exactly that arithmetic — and the profit goes with it.

## The distinction that matters

| question | answer |
|---|---|
| Will this edge persist? | Wrong question — you don't need it to. |
| Did this edge exist during the fit window? | Yes, by construction. Guaranteed, not evidence. |
| **Would trading it forward have made money at any point in 3.5 years?** | **No. 0 of 80 rules. Including the most recent months.** |

The reversal was never tradeable — not in 2023, not in 2025, not in 2026. It is a
property of how the configurations were picked, not of the market.

## What would change the answer

The rule needs a selection signal that **survives one month**. Config P&L does not
(correlation 0.00). Candidates worth measuring, at a fraction of the cost of this run:

- **Range width / realised volatility regime.** Select the configuration by the
  current volatility state rather than by last month's P&L. Volatility is strongly
  autocorrelated month to month; P&L is not.
- **Trend-day frequency.** London ORB is a breakout system; it should work when London
  trends and fail when it chops. Measure that directly and switch on it.
- **Direction skew.** In 2026, check whether the losses are one-sided (longs bleeding,
  shorts fine). If they are, a directional filter is a real edge; a blanket reversal is not.

Say which of these you want and I'll test it the same way — the harness is built, and
re-running the analysis over a new selection signal takes seconds, not an hour.

---

## Files

| file | what it holds |
|---|---|
| `WALK_FORWARD_FINDINGS.md` | this document |
| `walk_forward_matrix.csv` | the fit-length × selection-size table |
| `walk_forward_grid.csv` | same, with trades / hit rate / drawdown |
| `rule_scan.csv` | all 80 rules, ranked |
| `monthly_fit3_k5.csv` | month-by-month P&L and the exact configurations picked each month |
| `summary_fit3_k5.csv` | the counterfactual table |
| `original_by_year.csv` | the year table above |
| `rank_persistence_monthly.csv` | the month-to-month rank correlation, per month pair |
| `config_monthly_net_original.csv` | every configuration's P&L, every month — the raw input |
| `trades_all.parquet` | all 1,036,935 trades, 648 runs |
| `tools/walk_forward.py` | the harness: `collect` (slow, once) then `analyze` (fast) |

### Validation of the method

The trades are collected once over the full history and then sliced by month, rather
than re-running a backtest per window. That is exact here, not an approximation: lots
are fixed, so a trade's P&L does not depend on the balance before it, and each session
is independent of the ones before it.

Checked two ways:

- Slicing the collected trades to 2026 reproduces the standalone 2026 reversal run:
  **324 of 648 runs identical to the cent**, median difference 0, and every difference
  confined to the final partial month at the data boundary.
- Independent cross-check against the 486-run matrix summary for London 2026:
  −6,575,693 (matrix) vs −6,538,420 (slice) — 0.6%, same boundary month.
