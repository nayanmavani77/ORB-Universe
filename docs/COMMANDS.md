# Commands

Run everything from the project folder:

```powershell
cd <wherever you cloned this>\orb_python
```

Every setting lives in that engine's own config file. There is no parent config.

| engine | config file | results |
|---|---|---|
| `orb` | `orb\engines\orb\config.yaml` | `backtest\orb\` |
| `orb_reverse` | `orb\engines\orb_reverse\config.yaml` | `backtest\orb_reverse\` |

---

## 0. Instruments — what you trade

Each engine's config has an `instruments:` block. One entry per market, four
lines each: where the signal comes from, where it trades, what a point is worth,
and which data file holds its history.

```yaml
instruments:

  gc:
    signal: "GC.FUT"              # CME gold futures — the signal
    mt5:    "XAUUSDm"             # your terminal's symbol — where it trades
    value_per_point: 100.0        # 1 lot = 100 oz, so $100 per $1 move
    data:   ["data/gc_1m_merged.parquet"]

  es:
    signal: "ES.FUT"              # CME E-mini S&P 500
    mt5:    "US500"
    value_per_point: 50.0
    data:   ["data/es_1m.parquet"]

  nq:
    signal: "NQ.FUT"              # CME E-mini Nasdaq 100
    mt5:    "USTEC"
    value_per_point: 20.0
    data:   ["data/nq_1m.parquet"]
```

Then say which symbols trade which windows. A session is a **window**; the
`instruments:` block under it is the **(session x instrument) matrix**:

```yaml
sessions:

  new_york:
    enabled: true
    range_start: "09:30"          # the ROW: what every symbol here shares
    range_end:   "10:00"
    stop_time:   "16:55"
    risk_reward: 4.0
    instruments:                  # the CELLS: what each one does differently
      gc: {enabled: true,  lots: 1.0,   magic: 20260803}
      es: {enabled: true,  lots: 39.48, magic: 20260804, risk_reward: 2.0}
      nq: {enabled: false, lots: 10.91, magic: 20260805}

  london:                         # the SAME symbol, a different window,
    enabled: true                 # its own settings
    range_start: "03:00"
    range_end:   "03:30"
    stop_time:   "09:25"
    instruments:
      gc: {enabled: true, signal_timeframe: "M15", risk_reward: 1.5}
```

* a cell inherits **DEFAULTS -> ROW -> its own lines**, in that order
* each cell switches on and off **alone**; switching the ROW off silences all
  of them
* each cell becomes a session named `<session>_<instrument>` — `new_york_gc`,
  `london_gc` — which is the name in the journal, the report and the
  per-session tables. Add `name:` to a cell to choose your own.
* `magic` is assigned automatically in declaration order and is stable across
  runs. **Pin it explicitly on anything already trading live**, because
  inserting a cell above it would otherwise shift its number.

So one window can run three symbols on different settings, one symbol can run
three windows on different settings, and any single combination can be switched
off without touching its neighbours.

The older **flat form** — one session naming one `instrument:` — still works
and produces exactly the same session, field for field. Use it when a window
only ever trades one symbol:

```yaml
sessions:
  new_york:
    instrument: gc
    range_start: "09:30"
    # ...
```

That is the whole setup. Digits, tick size and volume limits are read from MT5
at run time and override anything set here, so adding a market really is those
four lines plus `instrument:` on its sessions.

**Don't guess `value_per_point`** — it is your broker's number, not the CME's,
and two brokers can both call a symbol `US500` while paying very different
amounts per point. Ask your terminal:

```powershell
python tools\mt5_check.py --suggest US500,USTEC
```

It reads the contract details, does the arithmetic
(`tick value / tick size`), and prints a ready-to-paste `instruments:` entry
for each symbol.

**Why the broker's number, when the backtest runs on Databento futures data?**
Because the futures supply the *points* and the broker supplies the *money*.
ES and a US500 CFD are both quoted in index points, so a 20-point move is 20
points on either — but only your broker decides what those 20 points pay you.
Using their figure is what makes the backtest predict your account rather than
a CME account you do not have.

That relies on both sides sharing one price scale, so the EA now checks it at
start-up and prints one of:

```
[gc] Scale check OK — signal 2,412.30 vs XAUUSDm 2,468.10 (2.3% apart).
[es] PRICE SCALE MISMATCH — signal 6,080.25 vs US500 608.03 (90.0% apart) ...
```

Under 10% is ordinary basis. Beyond that it says so, before the first order.

**Rules worth knowing:**

- Two sessions on **different** instruments may share a clock window — New York
  is New York for gold and for ES. Two on the **same** instrument may not: they
  would fight over one position slot.
- One position **per instrument**, one shared balance. Gold being long does not
  block an ES entry, but both draw on the same account.
- With exactly one instrument declared, sessions need not name it.

Select what a run trades with `--instruments`, in backtest, sweep and live:

```powershell
python tools\backtest.py --engine orb --instruments gc,es
python tools\sweep.py    --engine orb --instruments gc,es --dry-run
python run_live.py        --engine orb --instruments es
```

Omit the flag to trade everything declared. A name that is not declared is an
error, not a run that quietly does nothing.

Reports gain a per-instrument breakdown, the trades CSV gains an `instrument`
column, and the output folder is named after what the run traded, so GC and ES
on identical settings no longer overwrite each other.

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
python tools\backtest.py --engine orb_reverse --set sl_range_mult=1 --max-trades 1
```

Shorthands for the two you'll reach for most:

```powershell
python tools\backtest.py --engine orb_reverse --sl-mult 1.5
python tools\backtest.py --engine orb_reverse --max-trades 1     # R instead of RRR
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

Sweep several instruments at once. Each gets the **whole** parameter grid, so
two instruments is twice the runs, and every result row carries an `instrument`
column:

```powershell
python tools\sweep.py --engine orb --instruments gc,es --dry-run
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

## 5b. Getting data for a new instrument

Databento bills by volume, and `stype_in: parent` returns **every** contract
month plus every calendar spread — not just the front month. So price the
request before sending it:

```powershell
python download_data.py --db-symbols ES.FUT ^
    --download-start 2025-08-19 --download-end 2026-08-18 --cost
```

Nothing is downloaded and nothing is billed. Drop `--cost` to run it for real:

```powershell
python download_data.py --db-symbols ES.FUT ^
    --download-start 2025-08-19 --download-end 2026-08-18

python download_data.py --db-symbols NQ.FUT ^
    --download-start 2025-08-19 --download-end 2026-08-18
```

Each lands in `data\` as `GLBX.MDP3_<symbol>_ohlcv-1m_<start>_<end>.dbn.zst`.
Then merge each into the Parquet file the config names — this drops the
calendar spreads, de-duplicates overlapping edges, and verifies nothing was
lost:

```powershell
python tools\merge_data.py --data "data\GLBX.MDP3_ES.FUT_*.dbn.zst" ^
    --out data\es_1m.parquet

python tools\merge_data.py --data "data\GLBX.MDP3_NQ.FUT_*.dbn.zst" ^
    --out data\nq_1m.parquet
```

Check what arrived, then switch the cells on and backtest:

```powershell
python download_data.py --data data\es_1m.parquet --list-contracts
python tools\backtest.py --engine orb --instruments es
```

For reference, a year of gold at this resolution is ~20 MB and ~790,000 rows
across 40 contract months. If the API rejects the end date, it names the
boundary it will serve — use that date.

---

## 6. Live

First, credentials — they are never in a config file:

```powershell
copy .env.example .env
notepad .env
```

Fill in `DATABENTO_API_KEY`, `MT5_LOGIN`, `MT5_PASSWORD` and `MT5_SERVER`
(`MT5_TERMINAL_PATH` only if MT5 is not found automatically). `.env` is
git-ignored; a real environment variable of the same name overrides it for one
command. Every live entry point stops with the missing names listed rather than
failing deep inside the MT5 client.

```powershell
python run_live.py --engine orb
python run_live.py --engine orb,orb_reverse
```

`dry_run: true` in each config's MT5 block logs orders instead of sending them.
Check the connection first:

```powershell
python tools\mt5_check.py
```

It checks **every** instrument's MT5 symbol, so a wrong spelling or a broker
suffix on ES shows up now rather than at 09:30 when its session opens. It also
compares each instrument's `value_per_point` against what the terminal says —
a mismatch means your backtest P&L is scaled differently from live.

To check a `N opened on the account` figure from the journal against your
terminal — it prints the actual deals behind the count, per session, on that
session's own symbol:

```powershell
python tools\mt5_check.py --deals
python tools\mt5_check.py --symbol gc=XAUUSD          # try a different symbol
```

---

## 7. Checks

```powershell
python tools\golden_master.py check      # 24 backtests, trade for trade
python -m pytest tests\test_multi_engine.py tests\test_orb_reverse.py ^
    tests\test_live_feed.py tests\test_late_start.py ^
    tests\test_range_window.py tests\test_exit_journal.py ^
    tests\test_bar_timing.py tests\test_journal.py ^
    tests\test_instruments.py -q
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
# both read a matrix summary, so build one first:
python tools\run_matrix.py --rr 1,1.5,2,2.5

python tools\reverse_study.py --worst 10 --rr 1,1.5,2,2.5
python tools\walk_forward.py collect --data data\gc_1m_merged.parquet
python tools\walk_forward.py analyze
```

These are one-off research scripts kept for reproducing earlier studies, not
part of the normal loop — `tools\sweep.py` is the maintained sweep tool. They
write under `backtest\orb\matrix\` and `backtest\orb_reverse\`.
