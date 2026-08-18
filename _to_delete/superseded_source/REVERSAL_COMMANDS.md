# Reversal engine — command sheet

Run everything from the project folder:

```powershell
cd C:\Users\nayan\Desktop\ORB\orb_python
```

---

## 1. One backtest

```powershell
python tools\reversal_backtest.py --show      # print the settings, run nothing
python tools\reversal_backtest.py             # run it
```

Settings come from `reversal_config.yaml`. Edit that file, save, run again.

Output lands in the folder named by `output.dir` (default `reversal_run\`):
the HTML report, the trades CSV, the breakdown CSVs and the journal.

---

## 2. Change one thing without editing the file

Any flag overrides `reversal_config.yaml` for that run only.

```powershell
python tools\reversal_backtest.py --sl-mult 1.0        # stop = 1.0 x range
python tools\reversal_backtest.py --sl-mult 1.5
python tools\reversal_backtest.py --max-trades 1       # R  (reverse trade #1)
python tools\reversal_backtest.py --max-trades 3       # RRR
python tools\reversal_backtest.py --rr 2.5
python tools\reversal_backtest.py --session ASIA --orb 30
python tools\reversal_backtest.py --tf M15
python tools\reversal_backtest.py --include-news
python tools\reversal_backtest.py --anchor mirror      # the older stop rule
python tools\reversal_backtest.py --start 2026-03-01 --end 2026-08-13
python tools\reversal_backtest.py --out my_test
```

Combine freely:

```powershell
python tools\reversal_backtest.py --sl-mult 1.0 --max-trades 2 --rr 2 --out sl1_rr2
```

### The control arm — run this every time

```powershell
python tools\reversal_backtest.py --sl-mult 1.0 --forward --out fwd_check
```

Same settings, ordinary breakout direction. If the forward version is also
profitable, the stop distance is doing the work, not the fade.

---

## 3. The sweep

Always check the size first:

```powershell
python tools\reversal_sweep.py --dry-run
```

That prints the axes, the run count and a time estimate. Then:

```powershell
python tools\reversal_sweep.py
```

---

## 3a. Every permutation at once

**All three sessions, every axis** — 9,720 runs:

```powershell
python tools\reversal_sweep.py --session LONDON,ASIA,NEW_YORK --dry-run
python tools\reversal_sweep.py --session LONDON,ASIA,NEW_YORK --out reversal_sweep_all --resume
```

3 sessions x 3 timeframes x 3 ORB lengths x 2 news x 5 R:R x 6 SL multipliers
x 3 caps x 2 directions.

**Everything, including the full R:R ladder** — 17,496 runs:

```powershell
python tools\reversal_sweep.py --session LONDON,ASIA,NEW_YORK ^
    --rr 1,1.5,2,2.5,3,3.5,4,4.5,5 --out reversal_sweep_full --resume
```

(In PowerShell the line-continuation character is a backtick `` ` ``, not `^`.
Simplest is to put it all on one line.)

Or set it once in `reversal_config.yaml` section 5 and just run
`python tools\reversal_sweep.py`:

```yaml
sweep:
  sessions:      [ASIA, LONDON, NEW_YORK]
  risk_reward:   [1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5]
```

### Long runs: use `--resume`

```powershell
python tools\reversal_sweep.py --session LONDON,ASIA,NEW_YORK --out reversal_sweep_all --resume
```

- a partial `all_results.csv` is written every 100 runs, so an interrupted
  sweep is never a total loss
- `--resume` reads that file, keeps the finished rows and runs only what is
  missing — safe to close the window, reboot, and start the same command again
- it also lets you **grow** a sweep: run a small grid, then re-run with more
  values and `--resume`, and only the new combinations execute

### Use all your cores

```powershell
python tools\reversal_sweep.py --session LONDON,ASIA,NEW_YORK -j 8 --resume
```

Default is every core. Runtime scales close to linearly with `-j`.

Narrow it with the same override flags (comma-separated lists, no spaces):

```powershell
python tools\reversal_sweep.py --sl-mult 0.5,1,1.5 --tf M5 --orb 15 --caps 2 --dry-run
python tools\reversal_sweep.py --sl-mult 0.25,0.5,0.75,1,1.5,2 --rr 2 --tf M5 --out sweep_slmult
python tools\reversal_sweep.py --session LONDON,NEW_YORK --news skip
python tools\reversal_sweep.py --reverse-only          # skip the control arm
python tools\reversal_sweep.py --trades                # save a CSV per config
python tools\reversal_sweep.py -j 4                    # use 4 cores
```

Results in `<out>\_summary\`:

| file | what it answers |
|---|---|
| `all_results.csv` | every configuration, every metric |
| `by_sl_multiplier.csv` | **which stop multiplier works** |
| `by_sl_mult_x_rr.csv` | multiplier against R:R |
| `by_sl_mult_x_cap.csv` | multiplier against R / RR / RRR |
| `by_sl_mult_x_direction.csv` | multiplier, fade vs forward |
| `reverse_vs_forward.csv` | **the fade against its control arm, setting for setting** |
| `by_timeframe.csv`, `by_orb_duration.csv`, `by_news_mode.csv`, `by_risk_reward.csv`, `by_session.csv` | the usual cuts |

**Rank by `total_r`, not net P&L.** Lots are fixed, so a 2.0x multiplier risks
4x the dollars per trade of a 0.5x one. Net P&L across different multipliers is
comparing different bet sizes; `total_r` and `avg_r` are not.

---

## 4. Tests

```powershell
python -m pytest tests\test_reversal_engine.py -q
```

36 tests. They prove `sl_range_mult` 0.5 and 1.0 reproduce the original
`mid_range` and `full_range` trade for trade, that reversed stops sit on the
correct side of the entry, and that nothing under `orb\` is modified.

The original suite is unchanged and still runs the same way:

```powershell
python tests\test_parity.py
python tests\test_sessions.py
python tests\test_single_source.py
python tests\test_data_layer.py
python tests\test_cli.py
```

---

## 5. What to edit

`reversal_config.yaml`, section 3, is the whole reversal:

```yaml
reversal:
  sl_range_mult: 0.5     # 0.5 = old mid_range, 1.0 = old full_range, 1.5, 2.0 ...
  direction: reverse     # reverse (fade) | forward (control arm)
  sl_anchor: range       # range | mirror
  max_trades: 2          # 1=R  2=RR  3=RRR  0=unlimited
```

Section 5 is the sweep's lists. Cutting a list is how you cut the runtime.

---

## Note on the two anchors

`sl_anchor` only affects reversed trades.

- `range` — risk = `sl_range_mult x range height`, measured from the fill.
- `mirror` — risk = the distance the original trade would have taken, overshoot
  included, mirrored. **This is what the earlier London study used**, so set it
  if you want to keep working from those numbers.

Same config, M5 LONDON ORB15 skip-news R:R 2, RR cap, 2026:

| anchor | trades | net | PF | max DD |
|---|---|---|---|---|
| `range` | 200 | +19,190 | 1.27 | 6.4% |
| `mirror` | 189 | +53,155 | 1.62 | 5.7% |
