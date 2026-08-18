# `orb_reversal` — the reversal engine

A separate strategy that **fades** the opening-range breakout, with the stop loss
expressed as a **multiplier of the opening range**.

Nothing under `orb/` is modified. The original strategy, engine, broker,
backtester and reports are imported and driven exactly as they are. There is a
test that fails if any reversal code ever leaks into `orb/strategy.py` or
`orb/config.py`.

---

## What it adds

### 1. Stop loss as a multiple of the range

The original engine offers two fixed choices. This one is continuous:

| `sl_range_mult` | stop distance | equivalent |
|---|---|---|
| 0.25 | quarter of the range | — |
| **0.5** | half the range | **identical to the original `mid_range`** |
| 0.75 | three quarters | — |
| **1.0** | the whole range | **identical to the original `full_range`** |
| 1.5 | one and a half ranges | wider than the original engine can express |
| 2.0 | two ranges | wider still |

0.5 and 1.0 reproduce the original modes **trade for trade, to the cent** — there
are tests that assert exactly this. So every result already produced under the
old modes stays comparable, and the multiplier is a true generalisation rather
than a new strategy wearing the same name.

### 2. A direction switch

`reverse=True` fades the breakout. `reverse=False` runs the ordinary breakout
direction with the same multiplier — the **control arm**. Run it. If the forward
version at the same multiplier is also profitable, what you found is a stop
distance that suits this market, not a fade edge.

### 3. A trades-per-session cap

`max_trades` = 1 (R), 2 (RR), 3 (RRR), 0 (unlimited). Applied through the
engine's own `max_trades_per_session`, so no strategy code is involved.

---

## How the stop is placed

`range_size = range_high - range_low`.

**Ordinary direction** — the stop is a level measured inward from the side that
broke:

```
BUY   stop = range_high - mult x range_size
SELL  stop = range_low  + mult x range_size
```

At 0.5 that is the range midpoint; at 1.0 it is the opposite side.

**Reversed** — that level cannot be reused. A reversed SELL entered on a break
*above* the range would get a stop *below* its entry, which is not a stop at all:
the broker fills it instantly and every trade prints breakeven. So the reversed
trade keeps the stop **distance** and puts it on the far side of the entry:

```
reversed BUY   stop = entry - risk
reversed SELL  stop = entry + risk
```

`risk` comes from one of two anchors:

- **`range`** (default) — `risk = mult x range_size`, measured from the actual
  entry. Deterministic, and independent of how far the breakout bar overshot.
- **`mirror`** — the exact distance the original trade would have taken
  (overshoot included), mirrored. Reproduces the earlier
  `tools/reversal_test.py` study.

Take profit is unchanged: `risk_reward x risk`.

---

## Where the settings live

**`reversal_config.yaml`** in the project root. That is the only file to edit.
Five numbered sections:

| section | what it holds |
|---|---|
| 1 BASE | which original config to inherit instrument / data / news dates / account from |
| 2 RUN | the single backtest — session, timeframe, ORB length, R:R, news, lots |
| 3 REVERSAL | **the stop multiplier, direction, anchor, trade cap** |
| 4 PERIOD | dates, data file, output folder |
| 5 SWEEP | the lists the sweep combines |

Sections 3 and 5 are the ones you will actually touch.

It **inherits** from `config.yaml` rather than duplicating it — the instrument,
the news date lists, the account size and the fill assumptions stay in one place,
and that place is still the original file. `config.yaml` is never written to;
there is a test asserting it is byte-identical after a reversal run.

## Use

### One backtest

Edit `reversal_config.yaml`, then:

```bash
python tools/reversal_backtest.py
```

No arguments needed. `--show` prints the settings without running.

Every setting is also a flag, and a flag **overrides the file** — for trying one
value without editing anything:

```bash
python tools/reversal_backtest.py --sl-mult 1.5
python tools/reversal_backtest.py --sl-mult 1.5 --forward     # control arm
python tools/reversal_backtest.py --session ASIA --orb 30 --rr 3
```

Writes the usual HTML report, trades CSV and journal to `output.dir`.

### A sweep

```bash
python tools/reversal_sweep.py --dry-run     # how many runs is that?
python tools/reversal_sweep.py               # go
```

The lists come from section 5. Same override rule:

```bash
python tools/reversal_sweep.py --sl-mult 0.5,1,1.5 --tf M5 --caps 2 --dry-run
```

`--dry-run` prints the axes, the run count and a time estimate. Keeping both
directions in `directions:` produces `_summary/reverse_vs_forward.csv`, the table
that actually answers "is the fade doing the work, or the stop distance?".

Breakdown tables written to `_summary/`: `by_sl_multiplier.csv`,
`by_sl_mult_x_rr.csv`, `by_sl_mult_x_direction.csv`, `by_sl_mult_x_cap.csv`,
plus the usual timeframe / ORB / news / R:R / session cuts. They are ranked by
`total_r`, not net P&L — see the warning below.

### Reading the output

Lots are fixed, so **dollar risk per trade scales with the multiplier**: a 2.0x
run risks 4x the money per trade of a 0.5x run. Net P&L is therefore not
comparable across multipliers. `avg_r` and `total_r` are, and the summary tables
sort by `total_r` for that reason.

### From Python

```python
from orb_reversal import ReversalSettings, run_reversal
from orb_reversal.runner import run_forward

st  = ReversalSettings(sl_range_mult=1.5, max_trades=2)
rev = run_reversal(app_cfg, bars, st)      # the fade
fwd = run_forward(app_cfg, bars, st)       # the control arm
```

`run_reversal` deep-copies the config, so the caller's configuration is never
mutated — a sweep can reuse one base config for thousands of runs.

---

## Why the original matrix results do not cover this

`tools/run_matrix.py` sweeps timeframe x session x ORB length x news x R:R and
never touches `sl_mode`. Every run inherits `mid_range` from `config.yaml`, so
**all 486 matrix permutations and all 648 runs of the earlier reversal study
were done at 0.5 x range**. The stop dimension was never searched.

For a fade that is the parameter most likely to matter. A reversed trade is
betting price returns into the range; how much room it gets before the return
happens is the whole question, and 0.5 x range puts the stop right where a failed
breakout's overshoot lives.

---

## Files

| file | what it is |
|---|---|
| `reversal_config.yaml` | **the settings file — this is what you edit** |
| `orb_reversal/settings.py` | loads `reversal_config.yaml`, builds the config and the sweep grid |
| `orb_reversal/config.py` | `ReversalSettings` — multiplier, direction, anchor, cap |
| `orb_reversal/strategy.py` | `ReversalStrategy` — overrides `_stop_price` and `_open_trade`, nothing else |
| `orb_reversal/runner.py` | `reversal_engine()` scoped class swap, `run_reversal()`, `run_forward()` |
| `orb_reversal/grid.py` | the sweep grid, self-contained (no import from `tools/run_matrix.py`) |
| `tools/reversal_backtest.py` | one run + report |
| `tools/reversal_sweep.py` | the sweep |
| `tests/test_reversal_engine.py` | 36 tests |

## Tests

```bash
python -m pytest tests/test_reversal_engine.py -q     # 36 passed
```

What they prove:

- `sl_range_mult` 0.5 == `mid_range` and 1.0 == `full_range`, trade for trade,
  including final balance
- 1.5 is genuinely wider than `full_range`; risk is monotonic in the multiplier
  in both directions
- reversed risk equals `mult x range_size` exactly under the `range` anchor
- the reversed stop is always on the losing side of the entry — the bug that made
  every reversed trade close at breakeven cannot come back
- take profit is always `risk_reward x risk`
- the trade cap is respected, and 1 < 2 <= 3 trades
- the original class is restored after use **and after an exception**
- running the original, then a reversal, then the original again gives identical
  trades
- the caller's config is not mutated
- no reversal token has leaked into `orb/strategy.py` or `orb/config.py`
- `reversal_config.yaml` loads, builds the right session window, and its
  overrides win over the file
- a bad session name, a bad news word or a scalar where a list belongs is
  rejected with a message naming the file
- `sl_anchor: mirror` still reproduces the earlier `tools/reversal_test.py`
  numbers, so old results can be re-derived with the new engine
- `config.yaml` is byte-identical after a reversal run

The original suite still passes unchanged: 296 tests across `test_parity.py` (118),
`test_sessions.py` (103), `test_cli.py` (33), `test_data_layer.py` (22),
`test_single_source.py` (20).
