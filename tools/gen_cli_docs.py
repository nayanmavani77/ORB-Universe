#!/usr/bin/env python3
"""Generate docs/CLI.md from orb/cli.py.

    python tools/gen_cli_docs.py            # write docs/CLI.md
    python tools/gen_cli_docs.py --check    # fail if it is out of date

`tests/test_cli.py` runs the --check form, so the documentation cannot drift
away from the actual flags.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orb.cli import GROUP_ORDER, SPEC, config_paths, get_path   # noqa: E402
from orb.config import AppConfig                                # noqa: E402

OUT = os.path.join("docs", "CLI.md")

HEADER = """# Command reference

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
python tools/backtest.py --engine orb \\
    --range 13:30-14:30 --stop-time 20:00 --utc-offset 0 \\
    --tf M15 --rr 3 --lots 1 --sl-mode mid_range --max-trades 1 \\
    --symbol GC --value-per-point 100 --tick-size 0.10 \\
    --start "2024-01-01" --end "2025-12-31 22:00" \\
    --balance 100000 --spread 2 --slippage 1 --commission 2.50 \\
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

"""

FOOTER = """
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
    text = build()
    check = "--check" in sys.argv
    os.makedirs("docs", exist_ok=True)
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
