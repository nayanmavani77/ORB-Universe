# Commands

Run everything from the project folder:

```powershell
cd C:\Users\nayan\Desktop\ORB\orb_python
```

Every setting lives in that engine's own config file. There is no parent config.

| engine | config file | results |
|---|---|---|
| `orb` | `orb\engines\orb\config.yaml` | `backtest\orb\` |
| `orb_reverse` | `orb\engines\orb_reverse\config.yaml` | `backtest\orb_reverse\` |

---

## 1. Backtest — ORB

```powershell
python tools\backtest.py --engine orb --show     # print settings, run nothing
python tools\backtest.py --engine orb            # run it
```

## 2. Backtest — ORB reverse

```powershell
python tools\backtest.py --engine orb_reverse --show
python tools\backtest.py --engine orb_reverse
```

## 3. Backtest — both together, one account

```powershell
python tools\backtest.py --engine orb,orb_reverse --show
python tools\backtest.py --engine orb,orb_reverse
```

Sessions merge from both files. With the shipped defaults that is New York on
`orb` and London on `orb_reverse`. Output goes to
`backtest\mixed\orb_orb_reverse\`, and the summary breaks P&L down per session:

```
per session
  london   [orb_reverse]   216 trades  net  60,169.00
  new_york [orb]           ...
```

To change which sessions take part, set `enabled: true/false` in each engine's
own config. Windows may not overlap — one account, one position at a time.

---

## 4. Change one thing without editing the config

Any flag overrides the file for that run only.

```powershell
python tools\backtest.py --engine orb --session LONDON --orb 15 --rr 3
python tools\backtest.py --engine orb --tf M15
python tools\backtest.py --engine orb --news include
python tools\backtest.py --engine orb --start 2026-03-01 --end 2026-08-13
python tools\backtest.py --engine orb --out my_test
```

Engine-specific options use `--set NAME=VALUE`, repeatable:

```powershell
python tools\backtest.py --engine orb_reverse --set sl_range_mult=1.5
python tools\backtest.py --engine orb_reverse --set direction=forward
python tools\backtest.py --engine orb_reverse --set sl_range_mult=1 --set max_trades_per_session=1
```

Shorthands for the two you'll reach for most:

```powershell
python tools\backtest.py --engine orb_reverse --sl-mult 1.5
python tools\backtest.py --engine orb_reverse --forward --out fwd_check
```

**Run `--forward` alongside every reversal result.** Same settings, ordinary
breakout direction. If forward is profitable too, the stop distance is doing the
work, not the fade.

---

## 5. Sweeps

Always size it first:

```powershell
python tools\sweep.py --engine orb --dry-run
python tools\sweep.py --engine orb_reverse --dry-run
```

Then run:

```powershell
python tools\sweep.py --engine orb
python tools\sweep.py --engine orb_reverse
```

Narrow any axis with `--set AXIS=V1,V2` — axis names are the keys under
`sweep:` in that engine's config:

```powershell
python tools\sweep.py --engine orb_reverse --set sl_range_mults=0.5,1,1.5 --tf M5 --dry-run
python tools\sweep.py --engine orb_reverse --set trade_caps=2 --set directions=reverse
python tools\sweep.py --engine orb --set risk_reward=2,3,4 --session LONDON
```

Long sweeps:

```powershell
python tools\sweep.py --engine orb_reverse --resume -j 16
```

- a partial `all_results.csv` is written every 100 runs
- `--resume` keeps finished rows and runs only what's missing
- `-j` sets cores; each worker loads its own copy of the bars (~120 MB), so
  lower it if RAM is tight

Results land in `backtest\<engine>\sweep\_summary\`. **Rank by `total_r`, not
net P&L** — with a stop-size axis, a 2.0× multiplier stakes 4× the money of a
0.5× one at the same lot size.

---

## 6. Live

```powershell
python run_live.py --engine orb
python run_live.py --engine orb,orb_reverse
```

`dry_run: true` in each config's MT5 block logs orders instead of sending them.
Check the connection first:

```powershell
python tools\mt5_check.py
```

---

## 7. Checks

```powershell
python tools\golden_master.py check      # 24 backtests, trade for trade
python -m pytest tests\test_multi_engine.py tests\test_orb_reverse.py -q
python tests\test_parity.py
python tests\test_sessions.py
python tests\test_cli.py
python tests\test_data_layer.py
python tests\test_single_source.py
```

Run `golden_master.py check` after any change you make to the engines — it is
what proves trading behaviour did not move.

---

## 8. Research tools

```powershell
python tools\reverse_study.py --worst 10 --rr 1,1.5,2,2.5
python tools\walk_forward.py collect --data data\gc_1m_merged.parquet
python tools\walk_forward.py analyze
```
