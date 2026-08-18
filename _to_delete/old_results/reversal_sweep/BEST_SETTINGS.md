# London reversal sweep — which settings

3,240 runs (1,620 fade + 1,620 forward control), London, 2026-01-01 → 2026-08-13,
218,252 bars, `sl_anchor: range`, no costs applied.

---

## The recommendation

### M15 · ORB 15 · SKIP NEWS · **SL 0.75 × range** · R:R 1:2 · RRR

| | |
|---|---|
| net P&L | **+55,625** |
| trades | 204 |
| win rate | 47.6% |
| profit factor | **1.62** |
| max drawdown | **7.33%** |
| recovery factor | 5.99 |
| avg R | **0.384** — the highest of any candidate |
| profitable months | **8 of 8** |
| worst month | **+756** (never a losing month) |
| forward control | −52,716 |

Not the biggest number in the sweep. Chosen because it is the one that stops
being a single lucky cell.

**The whole SL 0.75 row works, at every R:R:**

| M15 ORB15 SKIP RRR | R:R 1.0 | 1.5 | 2.0 | 2.5 | 3.0 |
|---|---|---|---|---|---|
| **SL 0.75 net** | 44,964 | 47,949 | **55,625** | 47,468 | 50,505 |
| **SL 0.75 PF** | 1.57 | 1.52 | **1.62** | 1.54 | 1.57 |
| **SL 0.75 DD%** | 8.9 | 7.6 | **7.3** | 10.2 | 10.4 |

Every R:R from 1 to 3 lands between +45k and +56k with PF 1.52–1.62 and drawdown
under 11%. Pick the R:R wrong by a whole step and it barely matters. That is what
a real effect looks like: a plateau, not a spike.

---

## Runner-up, if you want the bigger number

### M5 · ORB 15 · SKIP NEWS · **SL 1.0 × range** · R:R 1:1.5 · RRR

net **+66,745** · 233 trades · 51.5% win · PF 1.53 · DD 7.45% · recovery 6.69 ·
**8 of 8 months profitable, worst month +1,450** · forward control −82,035.

More money, equally clean month by month, slightly narrower plateau. A fine
choice. Its neighbours are 43,850 / 53,670 / 39,235 / 66,745 — good, but they
move around more than the M15 row does.

---

## What NOT to pick, despite the headline

**M5 ORB15 SKIP R:R 1 SL 2.0 RR** shows **+76,650** — the largest net in the whole
sweep, PF 1.72, 8/8 months. Three reasons to leave it:

1. **It sits on the edge of the grid.** SL 2.0 is the widest value tested, so half
   its neighbourhood was never measured. Peaks on a boundary are usually the grid
   ending, not the effect ending.
2. **Its neighbours collapse.** One step to R:R 1.5 at the same stop: 18,160.
   A drop of 76% from a single step is a spike, not a plateau.
3. **It risks 4× the money per trade** of an SL 0.5 configuration, at the same
   lot size. The net P&L is bigger partly because the bet is bigger — that is why
   its drawdown is 10.3% against 7.3% for the recommendation.

---

## What the sweep established

### 1. The fade is real, not an artifact of the settings

The forward control arm ran at every identical setting.

- reverse beat its own forward twin in **1,110 of 1,620** pairs (**69%**)
- median edge over forward: **+18,491**
- for the recommended config: fade +55,625 against forward −52,716 — a near
  mirror image

If the stop distance and R:R were doing the work, both directions would profit.
They don't. The fade wins because the breakout loses.

### 2. The stop multiplier matters — and tighter is better

The axis your original matrix never searched. Every prior run was locked at 0.5.

| SL × range | configs profitable | total R | median PF | worst DD |
|---|---|---|---|---|
| 0.25 | **243 / 270** | **9,020** | 1.21 | 19.5% |
| 0.50 | 215 / 270 | 6,312 | 1.12 | 48.0% |
| 0.75 | 193 / 270 | 5,238 | 1.09 | 51.0% |
| 1.00 | 135 / 270 | 2,628 | 1.00 | 54.6% |
| 1.50 | 92 / 270 | 1,641 | 0.89 | 68.3% |
| 2.00 | 87 / 270 | 1,055 | 0.90 | 63.3% |

Risk-adjusted, tight stops win decisively. Wide stops make more raw dollars in a
few cells only because they bet more per trade.

### 3. Stop and R:R trade off against each other

Both grids show the same diagonal ridge: **tight stop wants high R:R, wide stop
wants low R:R.** That makes mechanical sense — the total distance price must
travel is roughly `SL × (1 + R:R)`, and there is a limit to how far London moves
before 09:30. Pairs that ask for more than that stop working.

Which is why 0.75 × R:R 2 is a sensible middle rather than an arbitrary winner.

### 4. ORB 15 dominates

Of 394 configurations that survive the full robustness filter, the top four
families are all **ORB 15**. Longer opening ranges (30, 60) survive far less often.
The fade lives in the first 15 minutes of London.

---

## Robustness filter

394 of 1,620 fade configurations pass all five at once:

- net profit > 0
- beats its own forward control
- at least 100 trades
- max drawdown under 15%
- **every neighbour profitable too** — one step away in R:R, in stop multiplier,
  and in trade cap

That last one is the important one. It removes anything whose success depends on
hitting an exact parameter.

---

## Honest limits

- **All of this is 2026 in-sample.** 3,240 configurations were searched; the best
  of 3,240 looks good by construction. The plateau requirement is a defence
  against that, not a cure.
- **`sl_anchor: range` only.** The anchor is not a swept axis. Your earlier London
  numbers came from `mirror`, which is a wider stop. The two are not comparable —
  under `range`, your old M5 RR2 SL0.5 RR config makes +19,190, not +53,155.
- **No costs.** Gross P&L, as always.
- **Net P&L across different stop multipliers is not like-for-like.** Lots are
  fixed, so a 2.0× config stakes 4× a 0.5× config. `total_r` and `avg_r` are the
  comparable columns, and the tables above rank by them.

---

## Config to paste

```yaml
run:
  session: LONDON
  signal_timeframe: "M15"
  orb_minutes: 15
  risk_reward: 2.0
  news: skip
  lots: 1.0

reversal:
  sl_range_mult: 0.75
  direction: reverse
  sl_anchor: range
  max_trades: 3
```

Then:

```powershell
python tools\reversal_backtest.py
python tools\reversal_backtest.py --forward --out fwd_check
```
