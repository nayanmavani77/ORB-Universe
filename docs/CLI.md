# Command reference

Every configuration field has a flag. Command-line values **always win** over
the config file, and the config file is never modified — so one config can
serve any number of runs.

```bash
python tools/backtest.py --engine orb [options]
python run_live.py      -c config.yaml [options]
python download_data.py -c config.yaml [options]
```

`run_live.py` and `download_data.py` share the flags below; `run_live.py --help` prints the same list.

## Quick start

```bash
# 1. what instruments are in my data?
python download_data.py --list-contracts

# 2. a plain run, everything from config.yaml
python tools/backtest.py --engine orb

# 3. my own settings, without touching the config file
python tools/backtest.py --engine orb \
    --range 13:30-14:30 --stop-time 20:00 --utc-offset 0 \
    --tf M15 --rr 3 --lots 1 --sl-mode mid_range --max-trades 1 \
    --symbol GC --value-per-point 100 --tick-size 0.10 \
    --start "2024-01-01" --end "2025-12-31 22:00" \
    --balance 100000 --spread 2 --slippage 1 --commission 2.50 \
    --name my_run
```

`--show-config` prints exactly what a run will use, without you having to
guess. Every report also records its own settings in the *Performance detail*
panel, so a report is always self-describing.

## Backtest period

| Flag | Meaning |
|---|---|
| `--start WHEN` | First bar to use. `YYYY-MM-DD`, or `'YYYY-MM-DD HH:MM'` for a precise moment. Always UTC. |
| `--end WHEN` | Last bar to use. A **date** includes that whole day; a **date and time** is exclusive. Always UTC. |

```bash
python tools/backtest.py --engine orb --start 2025-01-01 --end 2025-06-30
python tools/backtest.py --engine orb --start "2025-01-06 08:00" --end "2025-01-06 22:00"
```

These clamp the *data*. The daily session window is `--range` / `--stop-time`,
which are in **broker server time** — see `--utc-offset`.

## Shortcuts and conveniences

| Flag | Meaning |
|---|---|
| `--range HH:MM-HH:MM` | Sets `--range-start` and `--range-end` together. |
| `--set PATH=VALUE` | Override any field directly, e.g. `--set backtest.pessimistic_intrabar=false`. Repeatable. |
| `--show-config` | Print the resolved settings, then run. |
| `--list-contracts` | Show the instruments in the data and exit. |
| `--quiet` | Suppress the journal (same as `--log-level none`). |
| `--config`, `-c` | Config file to start from (default `config.yaml`). |

Boolean flags all have a negative form: `--reentry` / `--no-reentry`,
`--close-at-stop` / `--no-close-at-stop`, `--dry-run` / `--no-dry-run`.

Typos are rejected rather than ignored — `--set strategy.risk_rewrd=3` fails
with *unknown option 'risk_rewrd'*, and invalid values fail before any work
starts. A run can never quietly use settings you did not intend.


## Session (broker/server time)

| Flag | Default | What it does |
|---|---|---|
| `--engine` | `orb` | Which strategy engine this session runs — a name registered in orb/engines/, e.g. orb or orb_reverse. Each session may use a different one; they run side by side. |
| `--range-start` | `09:00` | Range window start, HH:MM. |
| `--range-end` | `10:00` | Range window end, HH:MM. |
| `--stop-time` | `17:00` | Stop trading at HH:MM. "0" disables it and the session runs until the next range starts. |
| `--utc-offset` | `0.0` | Broker server offset from UTC in hours, e.g. 2 or -5. Databento is UTC; this is what makes 09:00 here mean 09:00 on your MT5 chart. |
| `--tz` | — | DST-aware server zone, e.g. "America/New_York". Overrides --utc-offset when set. |

## Strategy

| Flag | Default | What it does |
|---|---|---|
| `--signal-timeframe`<br>`--tf` | `M5` | Timeframe for the range and the breakout closes: M1 M5 M15 M30 H1 H4 D1. |
| `--sl-mode` | `mid_range` | Stop loss placement: the range midpoint, or the opposite side of the range. Choices: `mid_range`, `full_range`. |
| `--risk-reward`<br>`--rr` | `2.0` | Take profit as a multiple of the stop distance, measured from the real fill price. |
| `--lots` | `0.1` | Position size in lots. |
| `--require-range-reentry`<br>`--reentry` | `true` | After a trade closes, require a close back inside the range before the next breakout is taken. Negate with --no-reentry. |
| `--max-trades-per-session`<br>`--max-trades` | `0` | Cap on trades per session; 0 means unlimited. |
| `--close-at-stop-time`<br>`--close-at-stop` | `true` | Flatten any open position at the stop time. Negate with --no-close-at-stop. |
| `--magic` | `20260814` | Magic number identifying this EA's orders. |
| `--comment` | `RangeBreak` | Order comment. |

## News categories

| Flag | Default | What it does |
|---|---|---|
| `--news-days` | `` | News Days. Single dates and ranges, comma separated: 2025.12.25,2026.01.01,2026.04.03-2026.04.06 |
| `--news-trading` | `off` | How the News Days list is applied. on = trade every day, News Days included (the list is ignored). off = never trade on a News Day. only = trade on News Days and on no other day. Choices: `on`, `off`, `only`. |
| `--news-mode` | — | One switch for the whole news filter: every category takes this mode. on = trade news days, off = skip them, only = trade nothing else. Per session: --set sessions.asia.news_mode=off Choices: `on`, `off`, `only`. |
| `--cpi-dates` | `` | Core CPI m/m: the release dates. Single dates and from-to ranges, comma or newline separated. |
| `--cpi-mode` | `off` | Core CPI m/m: how its dates are used. on = no restriction, off = never trade them, only = trade them and nothing else. Choices: `on`, `off`, `only`. |
| `--unemployment-dates` | `` | Unemployment Rate: the release dates. Single dates and from-to ranges, comma or newline separated. |
| `--unemployment-mode` | `off` | Unemployment Rate: how its dates are used. on = no restriction, off = never trade them, only = trade them and nothing else. Choices: `on`, `off`, `only`. |
| `--nfp-dates` | `` | Non-Farm Employment Change: the release dates. Single dates and from-to ranges, comma or newline separated. |
| `--nfp-mode` | `off` | Non-Farm Employment Change: how its dates are used. on = no restriction, off = never trade them, only = trade them and nothing else. Choices: `on`, `off`, `only`. |
| `--ism-mfg-dates` | `` | ISM Manufacturing PMI: the release dates. Single dates and from-to ranges, comma or newline separated. |
| `--ism-mfg-mode` | `off` | ISM Manufacturing PMI: how its dates are used. on = no restriction, off = never trade them, only = trade them and nothing else. Choices: `on`, `off`, `only`. |
| `--pce-dates` | `` | Core PCE Price Index m/m: the release dates. Single dates and from-to ranges, comma or newline separated. |
| `--pce-mode` | `off` | Core PCE Price Index m/m: how its dates are used. on = no restriction, off = never trade them, only = trade them and nothing else. Choices: `on`, `off`, `only`. |
| `--fomc-dates` | `` | Federal Funds Rate: the release dates. Single dates and from-to ranges, comma or newline separated. |
| `--fomc-mode` | `off` | Federal Funds Rate: how its dates are used. on = no restriction, off = never trade them, only = trade them and nothing else. Choices: `on`, `off`, `only`. |
| `--ppi-dates` | `` | Core PPI m/m: the release dates. Single dates and from-to ranges, comma or newline separated. |
| `--ppi-mode` | `off` | Core PPI m/m: how its dates are used. on = no restriction, off = never trade them, only = trade them and nothing else. Choices: `on`, `off`, `only`. |
| `--ism-svc-dates` | `` | ISM Services PMI: the release dates. Single dates and from-to ranges, comma or newline separated. |
| `--ism-svc-mode` | `off` | ISM Services PMI: how its dates are used. on = no restriction, off = never trade them, only = trade them and nothing else. Choices: `on`, `off`, `only`. |

## Instrument / contract specification

| Flag | Default | What it does |
|---|---|---|
| `--symbol` | `ES` | Instrument name shown in the report, e.g. GC. |
| `--digits` | `2` | Price decimals used for rounding SL and TP. |
| `--point` | `0.01` | Point size; the broker stop level is quoted in these. |
| `--tick-size` | `0.25` | Minimum price increment, e.g. 0.10 for gold. |
| `--stops-level` | `0` | Broker minimum SL/TP distance in points; 0 means no restriction. |
| `--volume-min` | `1.0` | Smallest tradeable volume. |
| `--volume-max` | `100.0` | Largest tradeable volume. |
| `--volume-step` | `1.0` | Volume increment used when normalising the lot size. |
| `--value-per-point` | `50.0` | Money per 1.0 of price movement per 1 lot. GC 100, ES 50, NQ 20, CL 1000, EURUSD 100000. |
| `--currency` | `USD` | Account currency label used in the report. |

## Data source (Databento)

| Flag | Default | What it does |
|---|---|---|
| `--data`<br>`-d` | — | DBN file(s), a directory or a glob. Overrides the config. |
| `--contract-mode` | `front_month_volume` | How to pick an instrument inside a multi-contract file. Choices: `front_month_volume`, `symbol`, `all`. |
| `--contract` | — | Fixed contract, e.g. GCZ5. Implies --contract-mode symbol. |
| `--include-spreads` | `false` | Keep calendar spreads such as GCG5-GCJ5. Off by default, and you almost never want them on. |
| `--roll-min-volume` | `0.0` | Ignore contracts below this daily volume when choosing the front month. |
| `--roll-boundary-hour` | `18` | Server-time hour at which the futures trading day starts, and the only instant the contract may change. 18 = the CME 18:00 New York open (default). Use 0 for a plain midnight boundary. |
| `--dataset` | `GLBX.MDP3` | Databento dataset, e.g. GLBX.MDP3. |
| `--db-symbols` | `ES.c.0` | Databento symbol request, e.g. GC.FUT or ES.c.0. |
| `--stype-in` | `continuous` | Databento symbology type of the request. Choices: `raw_symbol`, `continuous`, `parent`, `instrument_id`. |
| `--schema` | `ohlcv-1m` | Databento schema; must be an OHLCV one, e.g. ohlcv-1m. |
| `--db-api-key` | — | Databento API key. Defaults to $DATABENTO_API_KEY. |
| `--download-start` | — | Start date for download_data.py. |
| `--download-end` | — | End date for download_data.py. |
| `--download-dir` | `data` | Where download_data.py writes files. |

## Costs and account

| Flag | Default | What it does |
|---|---|---|
| `--balance` | `100000.0` | Starting account balance. |
| `--spread` | `0.0` | Bid/ask spread in points, charged on entry. |
| `--slippage` | `0.0` | Slippage in points, always applied against you. |
| `--commission` | `0.0` | Commission per lot per side; charged twice per round turn. |
| `--pessimistic-intrabar` | `true` | When SL and TP both sit inside one bar, assume the stop was hit first. Negate with --no-pessimistic-intrabar. |

## Output and logging

| Flag | Default | What it does |
|---|---|---|
| `--out` | `backtest_out` | Directory for the report and CSV files. |
| `--name` | `orb_backtest_report` | Base name for the output files, so parallel runs do not overwrite each other. |
| `--log-level` | `normal` | Journal detail: errors only, normal, or verbose. Choices: `none`, `normal`, `verbose`. |
| `--log-file` | — | Also append the journal to this file. |
| `--log-show-time` | `true` | Prefix journal lines with the server time. Negate with --no-log-show-time. |

## Live trading (MetaTrader 5)

| Flag | Default | What it does |
|---|---|---|
| `--mt5-symbol` | `ES` | Symbol name inside the MT5 terminal. |
| `--mt5-login` | — | MT5 account number. |
| `--mt5-password` | — | MT5 password. |
| `--mt5-server` | — | MT5 broker server name. |
| `--mt5-path` | — | Path to terminal64.exe, if it is not the default install. |
| `--deviation` | `0` | Maximum price deviation in points when sending an order. |
| `--translate-levels` | `true` | Carry SL/TP across as DISTANCES from the real fill, for when the data feed and the MT5 symbol are different instruments (CME GC signal, spot XAUUSD execution). Turn it off only if the MT5 symbol IS the instrument the bars came from. |
| `--dry-run` | `false` | Log orders instead of sending them to MT5. |


## Sweeping

```bash
for rr in 1.5 2 2.5 3; do
    python tools/backtest.py --engine orb --rr $rr --out rr_$rr
done

for tf in M5 M15 M30; do
  for sl in mid_range full_range; do
    python tools/sweep.py --engine orb --set risk_reward=1,2,3
  done
done
```

`--name` renames every output file, so parallel runs never overwrite each other.

## Live trading extras

`run_live.py` takes everything above, plus:

| Flag | Meaning |
|---|---|
| `--dry-run` | Log orders instead of sending them to MT5. |
| `--warmup-days N` | Days of history to preload so the range can be rebuilt after a restart (default 3). |
| `--no-warmup` | Skip the warm-up download. |
| `--poll SECONDS` | Feed poll interval (default 1.0). |

## Download extras

`download_data.py` uses the **Data source** flags, with the period taken from
`--download-start` / `--download-end`:

```bash
python download_data.py --dataset GLBX.MDP3 --db-symbols GC.FUT \
    --stype-in parent --schema ohlcv-1m \
    --download-start 2024-01-01 --download-end 2025-01-01
```

---

*This file is generated from `orb/cli.py` by `tools/gen_cli_docs.py`.
`tests/test_cli.py` fails if it is out of date, so it always matches the code.*
