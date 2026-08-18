# Architecture

The system runs any number of **engines**. Every engine has the same folder
shape, the same file names and its own single config file. Every session picks
its engine from config, and engines run side by side in one backtest and one
live process.

Two engines ship today:

| engine | what it does |
|---|---|
| **`orb`** | the original opening-range breakout — a close beyond the range trades **with** the break |
| **`orb_reverse`** | fades it — a close above the range high **sells**, and the stop is a multiple of the range |

---

## Layout

**There is no parent config.** Each engine's `config.yaml` is the complete
configuration for that engine — instrument, data, account, fill assumptions,
news dates, sessions, options and sweep grid. Open one file and you can see
everything a run of it will use.

```
orb/                        the core — knows nothing about any strategy
  registry.py               name -> engine          (the only seam)
  runconfig.py              loads any engine's config.yaml
  engine.py                 the OnTick sequence, one Engine per session
  markets.py                session opens and stops, shared
  outputs.py                where a run writes
  config.py  broker.py  bars.py  timeutils.py  logger.py
  backtest.py  live_trader.py  cli.py  report.py
  data/                     dbn.py  live.py

  engines/
    base.py                 the contract: EngineSettings, GridItem
    orb/                    __init__.py strategy.py settings.py grid.py config.yaml
    orb_reverse/            __init__.py strategy.py settings.py grid.py config.yaml
                            ^ each config.yaml is that engine's MASTER config

docs/
  orb/README.md             one folder per engine
  orb_reverse/README.md

backtest/
  orb/<run-name>/           report, trades CSV, journal
  orb/sweep/_summary/
  orb_reverse/<run-name>/
  orb_reverse/sweep/_summary/

tools/                      backtest.py  sweep.py  golden_master.py  ...
tests/
```

The three per-engine folders line up on purpose:

```
orb/engines/<engine>/    code + config
docs/<engine>/           documentation
backtest/<engine>/       results
```

Both engine folders contain **exactly the same five file names**. A test fails
if either grows a file the other does not have.

---

## Running several engines together

Without a parent config, a mixed run is composed from the engine configs
themselves. Name several and their **sessions merge** onto one account:

```bash
python tools/backtest.py --engine orb,orb_reverse
```

```
session   asia    [orb]          19:00-19:30 -> 02:55   magic 20260801
session   london  [orb_reverse]  03:00-03:15 -> 09:30   magic 20260901
```

Each session runs the engine of the file it is written in, so nothing is
restated. One broker, one equity curve, and the report breaks P&L down per
session.

### The shared blocks must agree

A merged run has one account, one instrument and one data feed — physically
there is only one. So `symbol`, `databento`, `mt5`, the clock and the account
half of `backtest` must **match** across the files taking part. They are not
silently taken from the first file:

```
These engines cannot run together — their configs disagree about things a
single run has only one of:
  symbol.name: orb/engines/orb/config.yaml has 'GC',
               orb/engines/orb_reverse/config.yaml has 'SI'
```

Picking a winner would hide a real misconfiguration. Magic collisions are
caught the same way — the broker tells positions apart by magic, so two
sessions cannot share one.

### The other route: one file, a session overriding `engine:`

Any session may name a different engine, so a single config can drive a mixed
run on its own:

```yaml
# orb/engines/orb/config.yaml
sessions:
  asia:
    engine: orb_reverse         # this session fades instead
    engine_options:
      sl_range_mult: 0.75
```

Both routes work. Use the merge when each engine owns its sessions; use the
override when one file is your portfolio.

`engine_options` is a plain dict validated by **that engine's own
`settings.py`**. The core never learns a strategy's vocabulary: `orb/config.py`
has no idea what `sl_range_mult` means. A test enforces it — no core module may
import an engine or name a strategy class, and `registry.py` is the only file
allowed to mention the engines package at all.

---

## Running

Two tools, both work for every engine.

```bash
python tools/backtest.py --engine orb
python tools/backtest.py --engine orb_reverse

python tools/sweep.py --engine orb_reverse --dry-run
python tools/sweep.py --engine orb_reverse
```

Settings come from that engine's own `config.yaml`. Flags override it for one
run:

```bash
python tools/backtest.py --engine orb_reverse --sl-mult 1.5
python tools/backtest.py --engine orb_reverse --set direction=forward
python tools/sweep.py    --engine orb_reverse --set sl_range_mults=0.5,1,1.5
```

Adding an engine does not mean writing another script.

---

## One master config per engine

`orb/engines/<engine>/config.yaml`, the same sections in every engine:

| section | holds |
|---|---|
| `engine` | which engine this file drives — must match its folder |
| CLOCK | the time zone the session times are written in |
| SESSIONS | which windows to trade, ON/OFF, and each session's `engine_options` |
| DEFAULTS | the shared rule set every session in this file inherits |
| NEWS DAYS | the calendar |
| INSTRUMENT / DATA / BACKTEST / MT5 | the shared blocks |
| `period` | backtest dates, data file, output folder |
| `sweep` | the lists the sweep tool combines |

Nothing is looked up elsewhere. The cost of that is real and worth stating: the
shared blocks are duplicated across engine configs, so changing the data path or
adding a news date means editing both files. `merge()` catches the drift the
moment you try to run them together, but a drift you never merge stays quiet.

`orb/runconfig.py` loads them all. One loader, not one per engine.

---

## The engine contract — `orb/engines/base.py`

`EngineSettings` — an engine's options. Declare dataclass fields with defaults,
override `validate()`, put old spellings in `ALIASES`. `from_options()` rejects
an unknown key rather than ignoring it, so a typo fails loudly instead of
silently running with defaults.

`GridItem` — one point on a sweep grid. **Every** engine's `grid.build()`
returns these, so a sweep tool treats all engines alike: `run_name` and `cfg`
always mean the same thing, `row()` always produces the results record, and each
engine's own axes (`sl_mode` for `orb`, `sl_range_mult` / `direction` for
`orb_reverse`) are flattened in through `axes`.

Each engine also exposes uniform aliases: `engines.orb.Strategy` /
`engines.orb.Settings` mean the same as `engines.orb_reverse.Strategy` /
`engines.orb_reverse.Settings`.

---

## How engine selection works

`Engine.__init__` resolves the class instead of naming one:

```python
cls = strategy_cls or resolve_engine(cfg.engine)
self.strategy = cls(cfg, broker, store=self.store, logger=self.log)
```

`MultiEngine` already gave every session its own strategy instance, bar store
and resampler, sharing one broker. Because the lookup is **per session**, that
fan-out delivers mixed engines with no further change, and both `run_backtest`
and `LiveTrader` drive the same `MultiEngine`.

### What this replaced

A second strategy used to be reached by rebinding
`orb.engine.RangeBreakoutStrategy` — a process-wide monkey-patch, in three
separate copies. Two consequences:

- **two engines could never run together** — whichever patch was active applied
  to every session at once;
- **the reversal had no live path at all**, because `LiveTrader` never applied
  the patch.

All three are gone, and a test greps the whole project to make sure no copy
comes back: with the registry in place a leftover patch would be a *silent
no-op*, and a sweep would run forward trades while labelling them reversed.

`MultiEngine.strategy_for(session_name)` was added for the same reason — exit
journalling used "the first session's strategy", which with mixed engines would
have the `orb` strategy reporting an `orb_reverse` exit.

---

## Adding an engine

1. Copy `orb/engines/orb/` to `orb/engines/<name>/`.
2. `strategy.py` — the strategy class, constructor `(cfg, broker, store=,
   logger=)`. Subclass an existing engine if you are varying one.
3. `settings.py` — an `EngineSettings` subclass.
4. `grid.py` — `AXES` and a `build()` returning `GridItem`s.
5. `config.yaml` — the six sections above.
6. `__init__.py` — `register("<name>", Strategy, Settings, description=...)`,
   plus the `Strategy` / `Settings` aliases.
7. Import it in `orb/engines/__init__.py`.
8. `docs/<name>/README.md`.

`tests/test_multi_engine.py` then checks it automatically: the five files exist
and no others, exactly one config naming its own engine, registered with a
description, constructor signature correct, settings round-trip, grid returns
`GridItem`s, docs present.

---

## Naming

One vocabulary. Core field names win; engine settings adopt them, with the old
spelling kept as an alias so existing configs keep loading.

| concept | canonical | aliases still accepted |
|---|---|---|
| trades per session | `max_trades_per_session` | `max_trades` |
| bar timeframe | `signal_timeframe` | `timeframe` |
| stop multiplier | `sl_range_mult` | `mult` |
| stop anchor | `sl_anchor` | `anchor` |
| direction | `direction: forward\|reverse` | `reverse: bool` |
| order label | `comment` / `order_tag` | `tag` |
| run identity | `run_name` | `name`, `report_name`, `label()` |

Session opens live once, in `orb/markets.py`. There used to be two verbatim
copies of that table.

---

## Outputs

```
backtest/<engine>/<run-name>/
backtest/<engine>/sweep/_summary/
```

`--out` still overrides it. **Filenames inside a run folder are unchanged** —
several tools read each other's output by name (`{run_name}_trades.csv`,
`_summary/all_results.csv`), so renaming files would break the chain for
nothing.

---

## Verification

**No trading behaviour changes.** `tools/golden_master.py` is the proof:

```bash
python tools/golden_master.py record     # before
python tools/golden_master.py check      # after
```

24 backtests — both engines, single- and multi-session, three timeframes, both
news modes, both anchors, every stop multiplier — compared trade by trade, to
the cent. All 24 identical across the whole restructure.

| suite | checks |
|---|---|
| `python tests/test_parity.py` | 118 |
| `python tests/test_sessions.py` | 103 |
| `python tests/test_cli.py` | 33 |
| `python tests/test_data_layer.py` | 22 |
| `python tests/test_single_source.py` | 20 |
| `python -m pytest tests/test_orb_reverse.py` | 29 |
| `python -m pytest tests/test_multi_engine.py` | 29 |

The load-bearing test is `test_mixed_engines_equal_separate_runs`: Asia on `orb`
plus London on `orb_reverse`, in one backtest, produces exactly the trades each
produces alone.
