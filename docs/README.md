# Documentation

| | |
|---|---|
| [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | how the system is put together — engines, registry, config, outputs |
| [`COMMANDS.md`](COMMANDS.md) | every command, copy-paste ready — start here |
| [`CLI.md`](CLI.md) | every `run_live.py` / `download_data.py` flag (generated from `orb/cli.py`) |
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

## Credentials

They are **not** in any config file, because config files are tracked in git.
`DATABENTO_API_KEY`, `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` and the optional
`MT5_TERMINAL_PATH` live in `.env` at the project root, which is git-ignored:

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

Then fill it in. A real environment variable of the same name beats the file,
so one command can point at a second account without editing anything. A config
that tries to carry a credential is refused at load.

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
