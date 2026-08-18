# ORB — opening-range trading engines

A faithful Python port of **`RangeBreakoutEA.mq5` v1.70**, grown into a small
multi-engine platform: one core, and any number of strategies that plug into it
by name.

| engine | what it does | config | docs |
|---|---|---|---|
| `orb` | trade the opening-range breakout — the 1:1 port | `orb/engines/orb/config.yaml` | [`docs/orb/`](docs/orb/README.md) |
| `orb_reverse` | fade it; stop is a multiple of the range | `orb/engines/orb_reverse/config.yaml` | [`docs/orb_reverse/`](docs/orb_reverse/README.md) |

A session names the engine it runs, so several run side by side on one account —
in backtest and live. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how, and
[`docs/COMMANDS.md`](docs/COMMANDS.md) for every command.

* **Backtesting** — Databento **DBN** files (downloaded history)
* **Live trading** — Databento **Live** market data → **MetaTrader 5** execution
* **Strategy** — unchanged. Same range construction, same entry trigger, same
  SL/TP maths, same arming rules, same filters, same journal wording.

---

## 1. The strategy (unchanged from the EA)

| Step | Rule |
|---|---|
| **Range** | High/Low of the signal-timeframe bars between **Range Start** and **Range End** (server time). Built once, on the first tick at/after the window ends. `Mid = (High + Low) / 2`. |
| **Entry** | A **closed** bar whose close is **above Range High** → BUY, **below Range Low** → SELL. The bar must have closed *after* the range window ended. Market order. |
| **Stop loss** | `mid_range` → the range midpoint. `full_range` → the opposite side of the range. Attached to the order at send time, so the position is protected from the first tick. |
| **Take profit** | `RR × |real fill price − SL|`, applied **after** the fill via a modify — measured from the true execution price, not the pre-trade quote. If the target sits inside the broker stop level the TP is skipped and the trade runs on SL only. |
| **Arming** | The range arms the first breakout. Every fill disarms. With `require_range_reentry: true` the EA re-arms only when a bar closes back **inside** the range; with it `false`, on the first closed bar while flat. |
| **One at a time** | Never adds to a breakout already running. |
| **Session window** | Trading runs from Range End until **Stop Time** — or, when Stop Time is `"0"`, until the next session's range start. `close_at_stop_time` optionally flattens. |
| **Filters** | `max_trades_per_session` (0 = unlimited) and **News Days by category** (see 3d). |
| **News Days** | `news_days` lists dates (single dates and `from-to` ranges). `news_trading` decides how the list is applied: **`on`** trade every day, News Days included (the list is ignored); **`off`** never trade on a News Day; **`only`** trade on News Days and on no other day. The range is always still built and logged — only order placement is filtered. |

Everything is timed in **broker/server time**, exactly like the EA.

---

## 2. Install

```bash
pip install -r requirements.txt
# live trading also needs (Windows):
pip install MetaTrader5
```

Then put your credentials in `.env` at the project root:

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

```ini
DATABENTO_API_KEY=db-xxxxxxxxxxxxxxxx
MT5_LOGIN=12345678
MT5_PASSWORD=your-password
MT5_SERVER=YourBroker-Server
MT5_TERMINAL_PATH=                    # optional
```

`.env` is git-ignored and never leaves your machine; `.env.example` is the
committed template and stays empty. Credentials are deliberately **not** in any
config file — config files are tracked, so a key written into one gets pushed.
A config that carries one is refused at load, naming the variable to move it to.

A real environment variable of the same name beats the file, so one command can
point at a second account without editing anything:

```powershell
$env:MT5_LOGIN="87654321"; python run_live.py --engine orb
```

Backtesting data you already have needs no key at all.

---

## 3. Configure

Each engine owns one complete config file — `orb/engines/<engine>/config.yaml`.
**There is no parent config.** The `strategy:` block (DEFAULTS) is a 1:1 copy of
the EA's inputs; nothing else changes the trading logic. Examples below that say
`config.yaml` mean that engine's file — every tool defaults to the right one, so
`-c` is rarely needed.

The three settings that matter most before your first run:

```yaml
server_timezone: "America/New_York"   # session clock (see 3c) — DST-aware

databento:
  dataset: "GLBX.MDP3"            # any dataset
  symbols: "GC.FUT"               # any symbol
  stype_in: "parent"
  schema:  "ohlcv-1m"             # base resolution fed to the engine

symbol:
  value_per_price_unit: 100.0     # money per 1.0 price move per 1 lot
                                  # GC 100 | ES 50 | NQ 20 | CL 1000 | EURUSD 100000
```

The session clock is what makes `09:30` in the config mean the same `09:30` you
mean. Databento timestamps are UTC; the engine shifts them into your session
zone once, at ingest. Use `server_timezone` for a real market session (it
follows daylight saving), or `server_utc_offset_hours` for a broker server with
a fixed offset. Section 3c explains why this choice matters more than it looks.

---

## 3b. Working with parent-symbology files (important)

The gold data in `data/` was downloaded with `stype_in="parent"` (`GC.FUT`).
A parent file does **not** contain one instrument — it contains *all* of them.
The 2025 1-minute file alone holds **1,498,745 rows**:

* **35 outright contracts** — GCG5, GCJ5, GCM5, GCQ5, GCZ5, … all trading at
  the same time
* **655,096 rows of calendar spreads** — `GCG5-GCJ5` and friends, which quote a
  price *difference* and are frequently **negative**

Dropping that into a backtester unfiltered gives garbage: bars from four
different contracts and a few negative spread prints all land on the same
timestamp, and the "range" becomes a blend of unrelated instruments.

The loader handles it in three steps:

1. **Spreads are dropped** — any symbol containing `-` (`include_spreads: true`
   if you ever need them).
2. **De-duplication keys on (timestamp, symbol)**, never on the timestamp
   alone — many contracts legitimately share a timestamp.
3. **One contract is selected**, via `databento.contract_mode`:

| mode | behaviour |
|---|---|
| `front_month_volume` *(default)* | For each **server trading date**, the outright contract with the highest volume. The roll is forced **forward only** — it can never fall back to an expiring contract that briefly out-trades the new one — and always lands on a date boundary, so a session's range and its trades always come from a single contract. |
| `symbol` | One fixed contract: `--contract GCG5`. |
| `all` | No selection. Only correct for single-instrument files; warns otherwise. |

Inspect what you have before running anything:

```bash
python download_data.py --list-contracts
```

The roll schedule is printed in the journal on every run:

```
Contract selection: front month by daily volume | 20 contract(s) used
  roll 2024-11-27 -> GCG5  (volume 125,222)
  roll 2025-01-29 -> GCJ5  (volume  73,674)
  roll 2025-03-27 -> GCM5  (volume 118,280)
  ...
```

Because every trade opens and closes inside one session, and the roll happens
between sessions, contract changes never create a phantom gain or loss.

### The `tbbo` files

`gc_tbbo_v0_*.dbn.zst` are trade-and-quote records, not bars — about 2.2 GB in
total. The strategy trades on **bar closes**, so they are not needed, and the
loader rejects them with a clear message rather than misreading them. Their one
real use is measuring the true bid/ask spread so you can set `spread_points`
honestly; that is a separate, optional step.

---

## 3c. Market hours, weekends and daylight saving

**Weekends need no special handling.** COMEX gold trades Sunday 18:00 ET to
Friday 17:00 ET. In your data Saturday has **zero bars**, and Sunday has bars
only from 22:00 UTC (the Globex reopen). When a session's range window falls in
a closed period there are no bars to build a range from, so the EA logs

```
No bars inside range window 2025.03.09 09:30 .. 2025.03.09 10:00 - session skipped.
```

and takes no trades — the same thing the MQL5 EA does. Verified on the full
gold history: **0 Saturday trades, 0 Sunday trades**, and `test_parity` locks
it in with weekend, mid-week-holiday and late-open cases.

**Daylight saving does need handling.** Databento is always UTC; New York is
not. A *fixed* UTC session drifts by an hour for roughly five months a year:

| | 13:30 UTC is… | |
|---|---|---|
| EDT (Mar–Nov) | 09:30 New York | the NYSE open |
| EST (Nov–Mar) | 08:30 New York | an hour early |

Across this dataset that is **352 of 957 weekdays on the wrong hour** — more
than a third of the sample not measuring the session you meant.

So name the zone instead of hard-coding an offset:

```yaml
server_timezone: "America/New_York"
strategy:
  range_start: "09:30"
  range_end:   "10:00"
  stop_time:   "17:00"
```

or on the command line:

```bash
python tools/backtest.py --engine orb --session NEW_YORK --tf M15
```

The engine converts every bar from UTC into that zone once, at load, so the
range, the stop time, the skip dates and the contract roll all line up with the
New York trading day — automatically, on both sides of every DST change.

Use `--utc-offset` only if your broker's server clock genuinely has no DST.


## 3d. News Days, by category

Eight tracked categories, each with **its own dates and its own mode**:

```yaml
strategy:
  news:
    core_cpi_mm:                { mode: "off",  dates: "2026.01.13,2026.02.11" }
    unemployment_rate:          { mode: "off",  dates: "" }
    non_farm_employment_change: { mode: "off",  dates: "" }
    ism_manufacturing_pmi:      { mode: "off",  dates: "" }
    core_pce_price_index_mm:    { mode: "off",  dates: "" }
    federal_funds_rate:         { mode: "only", dates: "2026.01.28,2026.03.18" }
    core_ppi_mm:                { mode: "off",  dates: "" }
    ism_services_pmi:           { mode: "off",  dates: "" }

  # an extra un-categorised bucket; behaves exactly like a ninth category
  news_days: ""
  news_trading: "off"
```

Dates can be one per line, which is easier to maintain for a long list:

```yaml
    core_cpi_mm:
      mode: "off"
      dates: |
        2026.01.13
        2026.02.11
        2026.03.10-2026.03.12      # ranges work too
```

Matching is by **calendar date**, never by day of the week — listing
`2026.01.13` affects that one date, not every Tuesday.

> **Planned:** a separate day-of-week filter is intended for a later phase.
> The agreed rule is that **OFF is final across both filters**: if the weekday
> filter says off, or any news category says off, the day is not traded —
> whatever the other one says. Recorded here so the decision is not lost.

| mode | effect of that category |
|---|---|
| `on` | No restriction. Its dates are ignored (the EA warns if you left some set). |
| `off` *(default)* | Never trade that category's dates. |
| `only` | Trade that category's dates and nothing else. |

Every category also has two command-line flags — `--cpi-*`,
`--unemployment-*`, `--nfp-*`, `--ism-mfg-*`, `--pce-*`, `--fomc-*`,
`--ppi-*`, `--ism-svc-*`:

```bash
python run_live.py \
    --cpi-dates "2026.01.13,2026.02.11" --cpi-mode off \
    --fomc-dates "2026.01.28,2026.03.18" --fomc-mode only
```

### How the categories combine

This is the part worth reading, because it is the one rule you did not choose:

1. **`off` is final.** A day listed by *any* `off` category is never traded —
   full stop. It makes no difference whether that same date also appears in an
   `on` category, in one or more `only` categories, or in the general bucket.
   One `off` listing ends the decision for that day. When another category also
   claims the day, the journal names both so the reason is never hidden.

   Pinned by tests across every combination: `off` alone, `off` + the same date
   `on`, `off` + `only`, all three at once, `off` + two `only` categories, two
   `off` categories, and `off` in either direction between a category and the
   general bucket. (The test constants are named `MON`..`FRI`, but they hold
   *dates* — a readable shorthand, not a weekday rule.)
2. **If any category is `only`**, a day must be listed by at least one of them
   to be tradeable. Several `only` categories form a union.
3. Otherwise the day is tradeable.

A category with no dates can never match, so it restricts nothing — the eight
empty categories in the default config are inert.

The journal states the whole setup on startup and names the deciding category
on every filtered signal:

```
News categories:
   Core CPI m/m                 OFF     8 entries  - do NOT trade these days
   Non-Farm Employment Change   ON      3 entries  - trade these days (no restriction)
   Federal Funds Rate           ONLY    5 entries  - trade ONLY these days
At least one category is ONLY (Federal Funds Rate), so a day must match one to be tradeable.
WARN | Both OFF and ONLY categories are set. OFF wins.
WARN | Non-Farm Employment Change is ON, so its 3 date(s) have no effect on trading.
...
Signal ignored: 2026.01.02 - not listed by any ONLY category (Federal Funds Rate).
```

A real run of exactly that config traded only the five FOMC sessions:

```
sessions traded: 2026.01.28, 2026.03.18, 2026.04.29, 2026.06.17, 2026.07.29
```

Guards: a category set to `only` with no dates is **rejected** (it could never
match, so the EA would never trade), an unknown category key is rejected with
the valid list printed, and a bad mode name is rejected.

---

## 4. Backtest

Download history, then run:

```bash
python download_data.py \
    --dataset GLBX.MDP3 --db-symbols GC.FUT --stype-in parent \
    --schema ohlcv-1m --download-start 2024-01-01 --download-end 2025-01-01

python tools/backtest.py --engine orb
```

### Running your own settings from the terminal

**Every** configuration field has a command-line flag — 55 of them. Values
given on the command line always win, and the config file is never modified,
so one config can serve any number of runs.

**Full reference: [`docs/CLI.md`](docs/CLI.md)** (or `run_live.py --help`).
Backtest flags are separate: `python tools/backtest.py --help`.

Backtesting — `tools/backtest.py`, a small flag set built around `--engine`:

```bash
python tools/backtest.py --engine orb \
    --session NEW_YORK --orb 30 --tf M15 --rr 3 --lots 1 --max-trades 1 \
    --news skip --start 2026-01-01 --end 2026-08-13 \
    --out backtest/orb/my_test
```

Live and downloading — `run_live.py` / `download_data.py`, where every config
field has a flag:

```bash
python run_live.py --engine orb \
    --range 13:30-14:30 --stop-time 20:00 --tz America/New_York \
    --tf M15 --rr 3 --lots 1 --sl-mode mid_range --max-trades 1 \
    --symbol GC --value-per-point 100 --tick-size 0.10 \
    --dry-run
```

`--start` and `--end` take a plain date or a precise `'YYYY-MM-DD HH:MM'`
(UTC). A date-only `--end` includes that whole day; a date **and time** is
exclusive. These clamp the *data*; the daily session window is `--range` and
`--stop-time`, which are in broker server time.

Anything not worth its own flag is reachable with `--set`:

```bash
python run_live.py --set backtest.pessimistic_intrabar=false \
                       --set databento.roll_min_volume=1000
```

Typos and bad values are rejected before any work starts, so a run can never
quietly use settings you did not intend. `--show-config` prints the resolved
settings, and every report records its own settings in the *Performance
detail* panel.

Sweeping is a shell loop; `--name` keeps the outputs apart:

```bash
for rr in 1.5 2 2.5 3; do
  python tools/backtest.py --engine orb --rr $rr --out backtest/orb/rr_$rr
done
```




### Merging many DBN files into one

Databento hands you one file per download, and they overlap at the edges. To
collapse them into a single file:

```bash
python tools/merge_data.py \
    --data "data/gc_ohlcv1m_parent_*.dbn.zst" \
    --out  data/gc_1m_merged.parquet
```

Then point the config at it — nothing else changes:

```yaml
backtest:
  dbn_paths: "data/gc_1m_merged.parquet"
```

The loader reads `.parquet` and `.csv` alongside `.dbn`, and every downstream
rule (spread filtering, contract selection, the front-month roll) is identical
whichever you load.

**It is a merge, not a filter.** Every outright contract is kept — front-month
selection still happens at backtest time, so the merged file stays as general
as the originals. Only calendar spreads are dropped, and `--keep-spreads`
keeps even those.

**It refuses to claim success without proving it.** After writing, it checks
that every (timestamp, symbol) key survived, that no row was invented, that
open/high/low/close/volume match value for value, that total volume matches,
and that per-contract row counts and date ranges are unchanged. A duplicate key
whose values *disagree* is reported as a conflict rather than silently
resolved. `--verify-only` re-runs those checks against an existing file.

On the gold data:

```
4 source files   4,825,236 rows   1,960,911 spreads dropped
concatenated     2,864,325 rows
de-duplicated    2,604,186 rows   (260,139 overlapping rows, all identical in value)

[PASS] row count, keys, OHLCV values, total volume, 72 contracts, date ranges
wrote data/gc_1m_merged.parquet  (28.9 MB, sources were 66.2 MB)
```

### Proving the merged file is safe to use

`tools/verify_sources.py` runs a matrix of deliberately different
configurations through two data sources and compares every field of every
trade:

```bash
python tools/verify_sources.py \
    --a "data/gc_ohlcv1m_parent_*.dbn.zst" \
    --b "data/gc_1m_merged.parquet" --contract GCZ5
```

On the gold data, all seven configurations that produce trades — different
sessions, timeframes, SL modes, re-entry rules, skip dates, a fixed contract
and a clamped date range — came out **identical, field for field**:

```
default config                  1,516    74,210    74,210   identical
evening session 20:00           2,475   183,070   183,070   identical
M15 bars, RR3, full-range SL    1,041    93,160    93,160   identical
M1 bars, no re-entry, max 1       923       -60       -60   identical
skip dates, no close at stop    1,427    77,200    77,200   identical
fixed contract                    411    49,685    49,685   identical
date-clamped window               647    25,115    25,115   identical
```

The tool is only useful if it can fail, so that was tested too: corrupting the
single bar that sets one session's range high flipped six of the seven cases to
MISMATCH and returned exit code 1.

Note what it cannot see. Corrupting a bar in the *middle* of a range window
changed nothing, because the range depends only on the window's highest high
and lowest low. Row-level completeness across every contract is the job of the
checks inside `merge_data.py`; this tool proves the results match, that one
proves the data does.

Loading is also about twice as fast (9.4 s versus 19.3 s), because Parquet
skips the DBN decode.

### Pick a stop time while the market is still trading

`close_at_stop_time` can only fire when a price arrives — the EA acts on ticks,
and so does this port. If your stop time lands exactly on a market halt, no
tick arrives to trigger it and the position is carried until the market
reopens.

COMEX gold halts 17:00-18:00 ET daily, and from Friday 17:00 to Sunday 18:00.
With `stop_time: "17:00"` every single stop-time close in a 3.7-year run
executed at **18:00**, and Friday positions were carried to **Sunday 18:00** —
up to 80 hours of weekend gap exposure the session window was meant to prevent.

```
stop 17:00 -> 495 late closes, 101 trades held past their entry day, net 82,190
stop 16:55 ->  15 late closes,   6 trades held past their entry day, net 74,210
```

The ~8,000 difference is profit that only existed because positions were held
through closed markets. Backing it out costs about 10% of the headline result
and removes the tail risk.

The engine now warns loudly whenever this happens:

```
WARN | Stop-time close is LATE by 1.0h: stop was 2022.11.20 17:00, first
       tradeable price is 2022.11.20 18:00. The position was held through a
       closed market.
```

and every report shows **Trades held past entry day** and **Longest single
hold** so the exposure cannot hide inside the equity curve.

Note that a session which legitimately spans midnight — the 20:00 ET window,
say — will show many trades "held past entry day" with no problem at all. Read
`Longest single hold` alongside it: 6.9 h is a normal overnight session, 79.7 h
is a weekend you did not intend to be in.

### Sweeping settings: `tools/sweep.py`

Grid-search an engine's own axes instead of guessing. The data is loaded once,
so a 400-run sweep costs one load rather than 400. The axes are the lists under
`sweep:` in that engine's `config.yaml`; `--set` overrides one for this run.

```bash
python tools/sweep.py --engine orb --dry-run          # size it FIRST
python tools/sweep.py --engine orb
python tools/sweep.py --engine orb_reverse --set sl_range_mults=0.5,0.75,1 --tf M5
```

`--dry-run` prints the run count and the axes and stops — worth doing every
time, because the count is the product of every list and grows faster than it
looks. `--resume` picks a long sweep back up after an interruption, and
`-j N` sets the core count.

All P&L is **gross** unless you ask otherwise — costs default to zero
everywhere, and every report and summary states its basis on the first line
(`P&L basis: GROSS — no spread, slippage or commission applied`).

Two things stop a sweep flattering itself:

* Every session's `stop_time` comes from `orb/markets.py`, derived from when
  the next session opens. A fixed stop time would hand an early range a far
  longer trading window than a late one and make the comparison meaningless —
  an 18:00 range with `stop_time 17:00` trades for 21 hours, a 09:30 range for 7.
* Read the results per **year and per month**, not just in total. A setting
  that lost money in two of four years and made it all back in one run has told
  you about the regime, not about the hour. `--start` / `--end` split the
  period; the per-run monthly CSV does the rest.

The winner of a sweep is the winner *of that sweep*. Prefer a setting sitting in
the middle of a plateau of good neighbours over an isolated peak — the peak is
usually the sample, not the edge.

### Adding risk:reward as a fourth dimension

```bash
python tools/run_matrix.py --data data/gc_1m_merged.parquet \
    --start 2026-01-01 --end 2026-08-13 \
    --rr 1:5 --light --out backtest/orb/matrix
```

`--rr 1:5` sweeps 1.0 to 5.0 in 0.5 steps, giving 54 x 9 = **486** runs
(about 18 minutes). `--rr 2,3,4` takes an explicit list instead.

`--light` skips the per-run HTML report and journal, keeping the trade list,
all four breakdown CSVs, the stats file and `config_used.json` in each folder.
486 folders come to ~52 MB that way instead of ~490 MB.

The summary gains `by_risk_reward.csv`, `by_session_rr.csv`,
`by_timeframe_rr.csv` and `best_rr_per_config.csv` (the winning R:R for each of
the 54 base configurations), and the comparison report gains an R:R section
with the net-P&L-vs-R:R curve for the strongest configurations.

### How trades are attributed to days

Daily, weekday and monthly breakdowns group by **session date** — the day the
opening range was built — not by entry or exit time. Hourly still groups by
entry time, because that question genuinely is about intraday timing.

This matters whenever a session spans midnight. On the Asia session
(19:00 ET → 03:00), the same session's trades land on two calendar dates:

| | Monday | Friday | Sunday |
|---|---|---|---|
| by exit day | −8,305 | +2,885 (29 trades) | +28,820 |
| by entry day | −13,440 | +300 (23 trades) | +38,515 |
| **by session date** | **+2,000** | **0 (0 trades)** | **+35,860** |

Two things to notice. Monday changes sign. And the Friday bar disappears
entirely — correctly, because there is no Friday Asia session (gold closes
17:00 Friday). Those "Friday" trades were Thursday-evening sessions spilling
past midnight; 171 of 677 trades (25%) were filed under the wrong day by exit
attribution, 127 (19%) by entry attribution.

Session date is also the key the News Days filter matches on, so the breakdown
and the filter now describe the same thing: *was this session worth taking?*

The trade CSV carries `session`, `entry_time` and `exit_time`, so any other
pivot is one line of pandas away.

### What the report contains

`backtest/<engine>/<run-name>/<run-name>.html` — one self-contained file, no internet
needed to open it:

* headline tiles — net profit, return, trades, win rate, profit factor,
  expectancy, max drawdown (money and %), max consecutive wins/losses,
  recovery factor, average R
* **equity curve** with the maximum-drawdown period shaded, plus a drawdown
  sub-chart
* full performance detail — gross profit/loss, average and largest win/loss,
  the money made in the longest winning streak and lost in the longest losing
  streak, drawdown peak/trough/recovery dates, long vs short split, total R,
  Sharpe, average duration, exit-reason counts
* **month-wise P&L** — heatmap, bar chart and table
* **day-wise P&L** — bar chart and full table
* **P&L by entry hour** and **by weekday**
* the complete **trade list**

Alongside it: `*_trades.csv`, `*_daily_pnl.csv`, `*_monthly_pnl.csv`,
`*_hourly_pnl.csv`, `*_weekday_pnl.csv`, `*_equity.csv`, `*_stats.csv`.

### Fill model

| Situation | Fill |
|---|---|
| Entry | the price on the tick the EA would have acted on — the open of the base bar that completed the signal bar — plus `spread_points` on buys and `slippage_points` against you |
| SL / TP touched inside a bar | at the level |
| Bar gaps straight through a level | at the bar **open** |
| Both SL and TP inside one bar | `pessimistic_intrabar: true` → the stop wins |
| Stop time | at the market price when the stop time is reached |

---

## 4b. The 54-configuration test matrix

`tools/run_matrix.py` runs the full permutation study using the **unchanged
engine and unchanged strategy** — only configuration differs between runs, so
every result is directly comparable.

```
3 timeframes (M1, M5, M15)
  x 3 sessions (Asia, London, New York)
  x 3 ORB durations (15, 30, 60 min)
  x 2 news modes (include / skip)
  = 54 configurations
```

```bash
python tools/run_matrix.py \
    --data data/gc_1m_merged.parquet \
    --start 2026-01-01 --end 2026-08-13 \
    --news-days news_days.txt \
    --out backtest/orb/matrix

python tools/matrix_report.py --dir backtest/orb/matrix
```

The data is loaded **once** and reused by all 54 runs — the whole matrix takes
about three minutes.

### Session definitions

All times **America/New_York**, DST-aware. Each session builds its range from
its open, then trades until the next session opens:

| Session | Opens (NY) | Trades until |
|---|---|---|
| Asia | 19:00 | 03:00 next day (London opens) |
| London | 03:00 | 09:30 (New York opens) |
| New York | 09:30 | 19:00 (Asia opens) |

This is expressed purely with the engine's existing `stop_time`, set to the
next session's opening time. No strategy code is involved. The Asia window
crosses midnight and is handled by the same session logic the EA always used.

### The news rule

`skip_news` sets `news_trading: "off"` with the supplied dates. The engine's
news check tests **both** the session's own date and the date a signal fires
on, so a listed date removes the Asia, London and New York sessions touching
it — the day-level skip, not just the session containing the release.

### Output

One folder per configuration, named `TF_SESSION_ORBnn_NEWSMODE`:

```
backtest/orb/matrix/
  M5_ASIA_ORB60_INCLUDE_NEWS/
    M5_ASIA_ORB60_INCLUDE_NEWS.html          full report
    ..._trades.csv  ..._daily_pnl.csv  ..._monthly_pnl.csv
    ..._hourly_pnl.csv  ..._weekday_pnl.csv  ..._equity.csv  ..._stats.csv
    config_used.json                         exact settings for this run
    journal.log                              full [RBEA] journal
  ... 53 more ...
  _summary/
    all_results.csv        one row per configuration
    by_timeframe.csv  by_session.csv  by_orb_duration.csv  by_news_mode.csv
    by_timeframe_session.csv
    news_effect.csv        each include/skip pair, with deltas
    run_info.json          period, bar count, session table, news dates used
    comparison.html        the four comparisons, charted
```

---

## 5. Live trading

```bash
python run_live.py --engine orb --dry-run            # log orders, send nothing
python run_live.py --engine orb                     # live
python run_live.py --engine orb,orb_reverse         # both, one account
```

The loop mirrors `OnTick()`: session sync, range build and stop-time handling
run on every poll; the breakout check runs on every completed timeframe bar.

* **Data** comes from the Databento Live client (the configured OHLCV schema).
* **Orders** go to MT5 via `order_send` — market order with the SL attached,
  then a `TRADE_ACTION_SLTP` modify to set the TP from the real fill price.
  SL and TP therefore live on the broker's server and survive a disconnect,
  exactly as with the original EA.
* On start, `--warmup-days` of history is pulled from Databento so the current
  session's range can be built immediately after a restart.
* Symbol digits, point, stop level and volume limits are read from MT5 and
  override the `symbol:` block.

Ctrl-C stops the EA and leaves open positions untouched, as `OnDeinit` does.

**Check before trading:** the Databento instrument and the MT5 symbol must be
the same market, and `server_utc_offset_hours` must match your MT5 server —
otherwise the range window will be built at the wrong time of day.

---

## 6. Single source of truth

Backtest and live are not two implementations that happen to agree — they are
one implementation with two adapters.

```
                    ┌──────────────────────────────┐
   DBN files  ─────▶│                              │
                    │ MultiEngine (orb/engine.py)  │
   Databento  ─────▶│   owns the OnTick sequence   │──▶ SimBroker  (backtest)
   Live feed        │  Strategy per session, from  │──▶ MT5Broker  (live)
                    │  orb/engines/<engine>/       │
                    └──────────────────────────────┘
```

`orb/engine.py` is the **only** place that sequences a tick — resample,
put the closed bar into history, run the session/range/stop-time housekeeping,
then run the breakout check. `run_backtest()` and `LiveTrader.run()` never call
those steps themselves; they only supply bars and a clock:

```python
# orb/backtest.py                    # orb/live_trader.py
engine.on_bar(bar, now=bar.time)     engine.on_bar(bar, now=now_fn(bar))
                                     engine.on_idle(now_fn())   # quiet poll
```

The **only** deliberate difference is where `now` comes from — simulated in the
backtest, wall clock when live. Broker-specific behaviour hides behind two
hooks on the `Broker` interface (`sync_market`, `settle_bar`) that the
simulated broker uses for fills and the MT5 broker leaves as no-ops.

Both properties are enforced by tests rather than by convention — see below.

### Layout

```
orb/                       the CORE — knows nothing about any strategy
  config.py                every MQL5 input as a dataclass, + .env loading
  timeutils.py             ParseHHMM / ParseDate / skip list / server clock
  logger.py                [RBEA] journal, level filtering, repeat suppression
  bars.py                  Bar, MT5-aligned resampler, history store
  engine.py                the OnTick sequence — the single source of truth
  broker.py                Broker interface, SimBroker, MT5Broker
  backtest.py              backtest driver  (bars in, result out)
  live_trader.py           live driver      (feed in, orders out)
  report.py                statistics and the HTML report
  cli.py                   one spec table -> flags, parsing and docs
  registry.py              engine name -> strategy class
  runconfig.py             loads engine configs; merges them for a mixed run
  markets.py               session opens and stop times, defined once
  outputs.py               backtest/<engine>/<run-name>/
  strategy.py              import shim — the class moved to engines/orb/
  data/dbn.py              DBN loader + history downloader
  data/live.py             Databento Live feed

  engines/                 one folder per engine, all the same five files
    base.py                the contract every engine follows
    orb/                   __init__ strategy settings grid config.yaml
    orb_reverse/           __init__ strategy settings grid config.yaml

run_live.py  download_data.py         entry points
.env                                  credentials — git-ignored
.env.example                          the committed template

tools/
  backtest.py              one backtest of any engine  <- the everyday tool
  sweep.py                 sweep one engine's axes     <- the everyday tool
  golden_master.py         24 backtests, trade for trade — the safety net
  mt5_check.py             prove the MT5 connection before trusting it
  merge_data.py            combine DBN files into one parquet
  gen_cli_docs.py          regenerates docs/CLI.md
  run_matrix.py  matrix_report.py  session_report.py     legacy research
  reverse_study.py  walk_forward.py  verify_sources.py   legacy research

docs/
  COMMANDS.md              every command, copy-paste ready
  CLI.md                   run_live / download_data flags (generated)
  orb/README.md            one folder per engine
  orb_reverse/README.md

backtest/<engine>/<run-name>/         results — git-ignored
tests/
```

---

## 7. Tests

```bash
python tools/golden_master.py check   # 24 backtests, trade for trade

python tests/test_parity.py           # 118 checks — strategy rules
python tests/test_sessions.py         # 103 checks — multi-session behaviour
python tests/test_single_source.py    #  20 checks — backtest == live
python tests/test_data_layer.py       #  22 checks — spreads, contracts, rolls
python tests/test_cli.py              #  32 checks — CLI covers every setting

python -m pytest tests/test_multi_engine.py tests/test_orb_reverse.py \
                tests/test_live_feed.py tests/test_late_start.py \
                tests/test_range_window.py tests/test_exit_journal.py \
                tests/test_bar_timing.py -q
```

`golden_master.py check` is the one to run after ANY change to the engines: it
re-runs 24 fixed backtests — both engines, single- and multi-session, three
timeframes, both news modes, both stop anchors, every stop multiplier — and
compares them trade by trade, to the cent. If it is green, trading behaviour
did not move.

**`test_parity`** covers the range window boundaries (the last in-window bar is
included, the first out-of-window bar is not), long and short breakouts, both
SL modes, TP from the real fill, the arming and re-entry rules, max trades per
session, skip dates, stop-time close, continuous mode, gap fills, multi-day
session rollover, weekend / holiday / late-open sessions, and report generation.

**`test_single_source`** proves the two paths cannot drift:

* *statically* — it parses `backtest.py` and `live_trader.py` and fails if
  either one calls `ingest_bar()`, `on_time()`, `on_bar_closed()` or
  `Resampler.push()` itself. Re-implement the loop in a runner and the test
  goes red.
* *behaviourally* — it pushes 6,000 synthetic bars through the **live** code
  path (`LiveTrader.run()` with a replay feed and a simulated broker) and
  through the **backtest** path, then asserts every trade matches on direction,
  lots, entry/exit time, entry/exit price, SL, TP, exit reason, net profit,
  the range levels it traded and the trade number within the session — and
  that the final balances are identical.

**`test_data_layer`** covers spread detection, contract-code ranking (including
single-digit year roll-over, so `GCF7` seen in 2026 means Jan 2027 not 2017),
the front-month roll — one contract per day, forward only, never falling back
to an expiring contract that briefly out-trades the new one — and the
(timestamp, symbol) de-duplication that keeps simultaneous contracts alive.

**`test_cli`** is what makes "every setting is on the command line" true rather
than merely claimed. It walks `AppConfig`, asserts each of the 55 fields has
exactly one flag, drives every flag through the parser and checks the value
actually lands on the right field, and regenerates `docs/CLI.md` to confirm the
documentation still matches the code. Add a config option without a flag or a
doc entry and the suite goes red.

---

## 8. Differences you should know about

These are environmental, not logic changes:

1. **Chart drawing** is not ported — the EA's boxes and level lines are a
   MetaTrader chart feature. All the same numbers are in the report instead.
2. **Data source.** The EA reads bars from the MT5 terminal; this port reads
   them from Databento. If your MT5 feed and your Databento feed disagree on a
   high or low inside the range window, the two will occasionally pick a
   different breakout level.
3. **Backtest fills** are modelled from OHLC bars (see the table above); MT5's
   Strategy Tester in "every tick" mode models them from ticks. Expect small
   differences on trades where price only just reaches a level.
