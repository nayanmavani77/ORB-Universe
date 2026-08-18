# Documentation

| | |
|---|---|
| [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | how the system is put together — engines, registry, config, outputs |
| [`CLI.md`](CLI.md) | every command-line flag (generated from `orb/cli.py`) |
| [`orb/`](orb/README.md) | the **orb** engine — the opening-range breakout |
| [`orb_reverse/`](orb_reverse/README.md) | the **orb_reverse** engine — fading it |

One folder per engine, mirroring the code and the results:

```
orb/engines/<engine>/    code + its MASTER config.yaml
docs/<engine>/           documentation
backtest/<engine>/       results
```

There is no parent config. Each engine's `config.yaml` is complete on its own —
instrument, data, account, news dates, sessions, options and sweep grid.

## Running anything

```bash
python tools/backtest.py --engine orb            # one backtest
python tools/sweep.py    --engine orb --dry-run  # size a sweep first
python tools/sweep.py    --engine orb            # run it
```

Both tools work for every engine and read that engine's own `config.yaml`.

To run engines together on one account, name several — their sessions merge:

```bash
python tools/backtest.py --engine orb,orb_reverse
```
