"""Where a run writes its results.

One root, one folder per engine, one folder per run:

    outputs/
      breakout/
        M5_NEW_YORK_ORB30_RR4/
          M5_NEW_YORK_ORB30_RR4.html
          M5_NEW_YORK_ORB30_RR4_trades.csv
          ...
      reversal/
        M15_LONDON_ORB15_RR2_SL0p75_REV/
          ...
      mixed/
        asia_breakout_london_reversal/
          ...

Before this, runs landed wherever each tool happened to default — eleven
top-level folders (`backtest_out`, `matrix_out`, `rr_matrix_out`,
`reversal_run`, `reversal_sweep`, `reversal_out`, `walk_forward_london`, …),
several of them hand-renamed variants of the same run.

The FILENAMES inside a run folder are deliberately unchanged. Several tools read
each other's output by name — `tools/matrix_report.py` opens
`_summary/all_results.csv`, `tools/session_report.py` and the sweeps read
`{run_name}_trades.csv` — so renaming files would break the chain for no gain.
Only the folder they sit in is standardised.

Nothing here deletes or moves an existing folder.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Optional

ROOT = "backtest"
#: what an engine folder is called when one run mixes several engines
MIXED = "mixed"


def engine_of(cfg) -> str:
    """The engine folder name for an `AppConfig`.

    A run with several sessions on one engine files under that engine; a run
    that genuinely mixes engines files under `mixed/`, because it belongs to
    none of them.
    """
    try:
        engines = sorted({str(s.engine or "orb").strip().lower()
                          for s in cfg.enabled_sessions()})
    except Exception:                                    # not an AppConfig
        return MIXED
    if not engines:
        return MIXED
    return engines[0] if len(engines) == 1 else MIXED


def safe_name(name: str) -> str:
    """A run name that is valid as a folder on every platform."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "run")).strip("._-")
    return cleaned or "run"


def run_dir(engine: str, run_name: str, root: str = ROOT,
            create: bool = True) -> str:
    """`outputs/<engine>/<run-name>/`."""
    path = os.path.join(root, safe_name(engine), safe_name(run_name))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def resolve(cfg, run_name: str, out_dir: Optional[str] = None,
            root: str = ROOT, create: bool = True) -> str:
    """The folder a run should write to.

    `out_dir` wins when given, so `--out somewhere` keeps behaving exactly as it
    always has and no existing script or habit breaks.
    """
    if out_dir:
        if create:
            os.makedirs(out_dir, exist_ok=True)
        return out_dir
    return run_dir(engine_of(cfg), run_name, root=root, create=create)


def sweep_dir(engines: Iterable[str], sweep_name: str, root: str = ROOT,
              create: bool = True) -> str:
    """The folder for a sweep covering one or more engines."""
    names = sorted({str(e or "").strip().lower() for e in engines if e})
    engine = names[0] if len(names) == 1 else MIXED
    return run_dir(engine, sweep_name, root=root, create=create)
