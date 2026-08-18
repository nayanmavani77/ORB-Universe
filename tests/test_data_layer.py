"""Data-layer tests: spread filtering, contract codes and the front-month roll.

These use synthetic frames so they run without any DBN files present.

Run:  python -m tests.test_data_layer
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

import pandas as pd                                          # noqa: E402

from orb.data.dbn import (contract_rank, is_spread,          # noqa: E402
                          _select_front_month)

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
print("\n--- spread detection ------------------------------------------------")
check("outright GCG5", is_spread("GCG5"), False)
check("spread GCG5-GCJ5", is_spread("GCG5-GCJ5"), True)
check("spread GCF3-GCZ3", is_spread("GCF3-GCZ3"), True)
check("outright ESH5", is_spread("ESH5"), False)


# ==========================================================================
print("\n--- contract codes --------------------------------------------------")
order = ["GCZ4", "GCG5", "GCJ5", "GCM5", "GCQ5", "GCZ5", "GCG6", "GCJ6"]
ranks = [contract_rank(s, 2025) for s in order]
check("ranks strictly increasing", all(a < b for a, b in zip(ranks, ranks[1:])), True)
check("GCZ4 = Dec 2024", contract_rank("GCZ4", 2024), 2024 * 12 + 12)
check("GCG5 = Feb 2025", contract_rank("GCG5", 2025), 2025 * 12 + 2)
check("GCF7 seen in 2026 is Jan 2027",
      contract_rank("GCF7", 2026), 2027 * 12 + 1)
check("GCM9 seen in 2026 is Jun 2029",
      contract_rank("GCM9", 2026), 2029 * 12 + 6)
check("two-digit year GCZ25", contract_rank("GCZ25", 2025), 2025 * 12 + 12)
check("non-contract symbol", contract_rank("SPY", 2025), None)


# ==========================================================================
print("\n--- front-month roll ------------------------------------------------")


def frame(rows):
    """rows = [(date, symbol, volume), ...] -> one bar per row, hourly."""
    recs, idx = [], []
    for i, (d, sym, vol) in enumerate(rows):
        t = datetime.fromisoformat(d) + timedelta(hours=9, minutes=i % 50)
        idx.append(t)
        recs.append({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5,
                     "volume": float(vol), "symbol": sym})
    return pd.DataFrame(recs, index=pd.DatetimeIndex(idx))


# GCG5 is the leader, then GCJ5 takes over — a normal forward roll
df = frame([
    ("2025-01-20", "GCG5", 1000), ("2025-01-20", "GCJ5", 100),
    ("2025-01-21", "GCG5", 900),  ("2025-01-21", "GCJ5", 200),
    ("2025-01-29", "GCG5", 100),  ("2025-01-29", "GCJ5", 800),
    ("2025-01-30", "GCG5", 50),   ("2025-01-30", "GCJ5", 900),
])
out, rolls = _select_front_month(df)
picked = {str(d.date()): s for d, s in zip(out.index, out["symbol"])}
check("day before roll uses GCG5", picked["2025-01-20"], "GCG5")
check("roll day uses GCJ5", picked["2025-01-29"], "GCJ5")
check("after roll stays GCJ5", picked["2025-01-30"], "GCJ5")
check("two contracts recorded", len(rolls), 2)
check("spreads/other contracts dropped from output",
      set(out["symbol"]), {"GCG5", "GCJ5"})
check("one contract per day",
      out.assign(d=out.index.date).groupby("d")["symbol"].nunique().max(), 1)

# a thin day where the OLD contract briefly out-trades the new one must NOT
# roll back to the earlier expiry
df2 = frame([
    ("2025-01-29", "GCG5", 100),  ("2025-01-29", "GCJ5", 800),
    ("2025-01-30", "GCG5", 900),  ("2025-01-30", "GCJ5", 400),   # tempting
    ("2025-01-31", "GCG5", 50),   ("2025-01-31", "GCJ5", 900),
])
out2, rolls2 = _select_front_month(df2)
picked2 = {str(d.date()): s for d, s in zip(out2.index, out2["symbol"])}
check("no roll-back to the expiring contract", picked2["2025-01-30"], "GCJ5")
check("still only one roll recorded", len(rolls2), 1)

# roll_min_volume ignores contracts below the threshold
df3 = frame([
    ("2025-02-03", "GCJ5", 5), ("2025-02-03", "GCM5", 900),
])
out3, _ = _select_front_month(df3, min_volume=100)
check("thin contract ignored by roll_min_volume",
      set(out3["symbol"]), {"GCM5"})


# ==========================================================================
print("\n--- duplicate handling ----------------------------------------------")
# the same timestamp legitimately carries many contracts: de-duplication must
# key on (timestamp, symbol), never on the timestamp alone
t = pd.Timestamp("2025-06-10 09:00")
dup = pd.DataFrame(
    [{"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 10.0,
      "symbol": s} for s in ("GCQ5", "GCZ5", "GCG6")],
    index=pd.DatetimeIndex([t, t, t]))
keyed = ~dup.index.to_frame().assign(s=dup["symbol"].values).duplicated(keep="last")
check("three contracts on one timestamp all survive", int(keyed.sum()), 3)
check("timestamp-only dedupe would have kept 1",
      int((~dup.index.duplicated(keep="last")).sum()), 1)


# ==========================================================================
print("\n" + "=" * 62)
print(f"  {PASS} passed, {FAIL} failed")
print("=" * 62)
sys.exit(1 if FAIL else 0)
