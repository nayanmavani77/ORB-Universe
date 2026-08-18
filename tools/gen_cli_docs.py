#!/usr/bin/env python3
"""Generate docs/CLI.md from orb/cli.py.

    python tools/gen_cli_docs.py            # write docs/CLI.md
    python tools/gen_cli_docs.py --check    # fail if it is out of date

`tests/test_cli.py` runs the --check form, so the documentation cannot drift
away from the actual flags.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orb.cli import GROUP_ORDER, SPEC, config_paths, get_path   # noqa: E402
from orb.config import AppConfig                                # noqa: E402

#: Anchored to the repo, not to the working directory, so running this from
#: anywhere updates the real docs/CLI.md instead of creating a stray one.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(REPO, "docs", "CLI.md")

HEADER = """# Command reference

The flags below belong to **`run_live.py`** and **`download_data.py`**. Those two
tools expose every configuration field as a flag; a command-line value **always
wins** over the config file, and the config file is never modified — so one
config can serve any number of runs.

```bash
python run_live.py      --engine orb [flags]
python download_data.py [flags]
```

Both default to `orb/engines/orb/config.yaml`. Point them at another engine with
`-c orb/engines/orb_reverse/config.yaml`.

**Backtesting is not on this page.** `tools/backtest.py` and `tools/sweep.py`
have their own, much smaller flag set built around `--engine` and
`--set NAME=VALUE`:

```bash
python tools/backtest.py --help
python tools/sweep.py    --help
```

## Quick start

```bash
# 1. what instruments are in my data?
python download_data.py --list-contracts

# 2. download history for the configured period
python download_data.py --start 2025-01-01 --end 2025-12-31

# 3. go live on the orb engine, everything from its config
python run_live.py

# 4. live on the reverse engine, with two settings changed for this run only
python run_live.py -c orb/engines/orb_reverse/config.yaml \
    --tf M15 --rr 2 --name live_reverse
```

`--show-config` prints exactly what a run will use, without you having to
guess. Every report also records its own settings in the *Performance detail*
panel, so a report is always self-describing.

## Date range

| Flag | Meaning |
|---|---|
| `--start WHEN` | First bar to use. `YYYY-MM-DD`, or `'YYYY-MM-DD HH:MM'` for a precise moment. Always UTC. |
| `--end WHEN` | Last bar to use. A **date** includes that whole day; a **date and time** is exclusive. Always UTC. |

```bash
python download_data.py --start 2025-01-01 --end 2025-06-30
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
| `--config`, `-c` | Engine config to start from (default `orb/engines/orb/config.yaml`). |

Boolean flags all have a negative form: `--reentry` / `--no-reentry`,
`--close-at-stop` / `--no-close-at-stop`, `--dry-run` / `--no-dry-run`.

Typos are rejected rather than ignored — `--set strategy.risk_rewrd=3` fails
with *unknown option 'risk_rewrd'*, and invalid values fail before any work
starts. A run can never quietly use settings you did not intend.

Credentials are **not** on this page and not in any config file. `DATABENTO_API_KEY`,
`MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` and `MT5_TERMINAL_PATH` live in `.env`
at the project root, which is git-ignored. See `.env.example`.

"""

FOOTER = """
## Sweeping

Sweeps are `tools/sweep.py`, not the flags above:

```bash
python tools/sweep.py --engine orb          --set risk_reward=1,2,3
python tools/sweep.py --engine orb_reverse  --set sl_range_mults=0.5,0.75,1.0
```

`--out` sends a run's results somewhere of your choosing; the default is
`backtest/<engine>/<run-name>/`, so parallel runs never overwrite each other.

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
python download_data.py --dataset GLBX.MDP3 --db-symbols GC.FUT \\
    --stype-in parent --schema ohlcv-1m \\
    --download-start 2024-01-01 --download-end 2025-01-01
```

---

*This file is generated from `orb/cli.py` by `tools/gen_cli_docs.py`.
`tests/test_cli.py` fails if it is out of date, so it always matches the code.*
"""


def build() -> str:
    cfg = AppConfig()
    parts = [HEADER]
    for group in GROUP_ORDER:
        opts = [o for o in SPEC if o.group == group]
        if not opts:
            continue
        parts.append(f"## {group}\n")
        parts.append("| Flag | Default | What it does |")
        parts.append("|---|---|---|")
        for o in opts:
            flags = "<br>".join(f"`{f}`" for f in o.flags)
            try:
                default = get_path(cfg, o.path)
            except AttributeError:
                default = ""
            if default is None:
                default = "—"
            elif isinstance(default, bool):
                default = "true" if default else "false"
            elif isinstance(default, list):
                default = "—"
            default = f"`{default}`" if default != "—" else "—"
            help_text = o.help
            if o.choices:
                help_text += " Choices: " + ", ".join(f"`{c}`" for c in o.choices) + "."
            parts.append(f"| {flags} | {default} | {help_text} |")
        parts.append("")
    parts.append(FOOTER)
    return "\n".join(parts)


def main() -> int:
    # Real argparse, so `--help` prints help instead of falling through to the
    # write branch and silently OVERWRITING docs/CLI.md — and so a typo like
    # `--checkk` is rejected rather than treated as "not --check, so write".
    p = argparse.ArgumentParser(
        description="Generate docs/CLI.md from orb/cli.py.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python tools/gen_cli_docs.py            # write docs/CLI.md
  python tools/gen_cli_docs.py --check    # fail if it is out of date
""")
    p.add_argument("--check", action="store_true",
                   help="verify docs/CLI.md matches orb/cli.py and change "
                        "nothing; exit 1 if it is stale. This is what "
                        "tests/test_cli.py runs.")
    a = p.parse_args()

    text = build()
    check = a.check
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if check:
        if not os.path.exists(OUT):
            print(f"{OUT} is missing — run: python tools/gen_cli_docs.py")
            return 1
        current = open(OUT, encoding="utf-8").read()
        if current != text:
            print(f"{OUT} is out of date — run: python tools/gen_cli_docs.py")
            return 1
        print(f"{OUT} is up to date ({len(SPEC)} options documented).")
        return 0
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"Wrote {OUT} ({len(SPEC)} options).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
