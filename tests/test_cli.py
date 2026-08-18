"""CLI completeness tests.

The point of these is that "every setting is on the command line" stops being
a claim and becomes something the test suite enforces:

  * every field of AppConfig has exactly one flag
  * every flag actually changes the field it names
  * the generated docs/CLI.md matches orb/cli.py

Run:  python -m tests.test_cli
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from orb.cli import (SPEC, apply_options, build_parser, config_paths,  # noqa: E402
                     get_path, parse_clamp)
from orb.config import AppConfig                                       # noqa: E402

PASS = FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")


# ==========================================================================
print("\n--- coverage: every config field has a flag -------------------------")
paths = set(config_paths())
covered = {o.path for o in SPEC}
missing = sorted(paths - covered)
extra = sorted(covered - paths)
print(f"  ({len(paths)} config fields, {len(SPEC)} flags)")
check("no config field is missing a flag", missing, [])
check("no flag points at a non-existent field", extra, [])
check("no field has two flags", len(covered), len(SPEC))

seen = {}
dupes = []
for o in SPEC:
    for f in o.flags:
        if f in seen:
            dupes.append(f)
        seen[f] = o.path
check("no duplicate flag names", dupes, [])


# ==========================================================================
print("\n--- every flag actually applies -------------------------------------")
parser = build_parser("test", "test")

SAMPLE = {"str": "ZZZ", "int": 7, "float": 1.25, "choice": None,
          "bool": None, "list": ["a.dbn", "b.dbn"]}

bad = []
for o in SPEC:
    cfg = AppConfig()
    before = get_path(cfg, o.path)
    if o.kind == "bool":
        value = not bool(before)
        argv = [o.flags[0] if value else "--no" + o.flags[0][1:]]
        expect = value
    elif o.kind == "choice":
        value = next(c for c in o.choices if c != before)
        argv = [o.flags[0], value]
        expect = value
    elif o.kind == "list":
        argv = [o.flags[0], "a.dbn", "b.dbn"]
        expect = ["a.dbn", "b.dbn"]
    else:
        expect = SAMPLE[o.kind]
        argv = [o.flags[0], str(expect)]
    ns = parser.parse_args(argv)
    apply_options(cfg, ns)
    after = get_path(cfg, o.path)
    if after != expect:
        bad.append(f"{o.flags[0]} -> {o.path}: got {after!r}, want {expect!r}")
check("all flags round-trip into the config", bad, [])

# aliases work too
cfg = AppConfig()
apply_options(cfg, parser.parse_args(["--rr", "3.5", "--tf", "M15",
                                      "--max-trades", "2", "--no-reentry"]))
check("--rr alias", cfg.strategy.risk_reward, 3.5)
check("--tf alias", cfg.strategy.signal_timeframe, "M15")
check("--max-trades alias", cfg.strategy.max_trades_per_session, 2)
check("--no-reentry alias", cfg.strategy.require_range_reentry, False)

# --contract implies contract_mode=symbol
cfg = AppConfig()
apply_options(cfg, parser.parse_args(["--contract", "GCZ5"]))
check("--contract sets the symbol", cfg.databento.contract_symbol, "GCZ5")
check("--contract implies contract_mode", cfg.databento.contract_mode, "symbol")

# --utc-offset clears a config-file timezone, --tz wins when both are given
cfg = AppConfig()
cfg.server_timezone = "Europe/Athens"
apply_options(cfg, parser.parse_args(["--utc-offset", "2"]))
check("--utc-offset clears server_timezone", cfg.server_timezone, None)
cfg = AppConfig()
apply_options(cfg, parser.parse_args(["--utc-offset", "2", "--tz", "UTC"]))
check("--tz survives alongside --utc-offset", cfg.server_timezone, "UTC")


# ==========================================================================
print("\n--- --range shortcut and --set --------------------------------------")


class NS:
    pass


ns = parser.parse_args([])
ns.range_window = "13:30-14:30"
cfg = apply_options(AppConfig(), ns)
check("--range sets start", cfg.strategy.range_start, "13:30")
check("--range sets end", cfg.strategy.range_end, "14:30")

ns = parser.parse_args([])
ns.overrides = ["backtest.initial_balance=50000",
                "backtest.pessimistic_intrabar=false",
                "strategy.news_days=2025.12.25",
                "mt5.login=none"]
cfg = apply_options(AppConfig(), ns)
check("--set float", cfg.backtest.initial_balance, 50000.0)
check("--set bool", cfg.backtest.pessimistic_intrabar, False)
check("--set str", cfg.strategy.news_days, "2025.12.25")
check("--set none", cfg.mt5.login, None)

for bad_spec, why in [("strategy.skip_dates=x", "unknown option"),
                      ("nosuch.field=1", "unknown section"),
                      ("strategy.risk_reward", "needs PATH=VALUE")]:
    ns = parser.parse_args([])
    ns.overrides = [bad_spec]
    try:
        apply_options(AppConfig(), ns)
        check(f"--set {bad_spec} rejected", "no error", why)
    except ValueError as exc:
        check(f"--set {bad_spec} rejected", why in str(exc), True)


# ==========================================================================
print("\n--- --start / --end accept dates AND times --------------------------")
check("date only", parse_clamp("2025-06-10"),
      (datetime(2025, 6, 10, tzinfo=timezone.utc), True))
check("date and time", parse_clamp("2025-06-10 13:30"),
      (datetime(2025, 6, 10, 13, 30, tzinfo=timezone.utc), False))
check("ISO form", parse_clamp("2025-06-10T13:30:15"),
      (datetime(2025, 6, 10, 13, 30, 15, tzinfo=timezone.utc), False))
check("dotted form", parse_clamp("2025.06.10"),
      (datetime(2025, 6, 10, tzinfo=timezone.utc), True))
check("empty", parse_clamp(""), None)
try:
    parse_clamp("last tuesday")
    check("garbage rejected", "no error", "ValueError")
except ValueError:
    check("garbage rejected", True, True)


# ==========================================================================
print("\n--- documentation is in sync ----------------------------------------")
r = subprocess.run([sys.executable, "tools/gen_cli_docs.py", "--check"],
                   capture_output=True, text=True)
if r.returncode != 0:
    print("   ", (r.stdout + r.stderr).strip())
check("docs/CLI.md matches orb/cli.py", r.returncode, 0)

doc = open("docs/CLI.md", encoding="utf-8").read()
undocumented = [o.flags[0] for o in SPEC if f"`{o.flags[0]}`" not in doc]
check("every flag appears in docs/CLI.md", undocumented, [])


# ==========================================================================
print("\n--- entry points still parse ----------------------------------------")
for script in ("run_backtest.py", "run_live.py", "download_data.py"):
    r = subprocess.run([sys.executable, script, "--help"],
                       capture_output=True, text=True)
    check(f"{script} --help", r.returncode, 0)


# ==========================================================================
print("\n" + "=" * 62)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 62)
sys.exit(1 if FAIL else 0)
