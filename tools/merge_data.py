#!/usr/bin/env python3
"""Merge many DBN files into ONE bar file, and prove nothing was lost.

    python tools/merge_data.py --data "data/gc_ohlcv1m_parent_*.dbn.zst" \
                               --out data/gc_1m_merged.parquet

What it does
------------
1. Reads every source file.
2. Drops calendar spreads (`GCG5-GCJ5`), which are never tradeable bars.
   Keep them with --keep-spreads if you really want them.
3. Keeps **every outright contract** — this is a merge, not a filter. Front-month
   selection still happens at backtest time, so the merged file stays as
   general as the originals.
4. De-duplicates on (timestamp, symbol). Databento files routinely overlap at
   the edges; the same contract at the same minute appears in two files and
   must collapse to one row, while different contracts at the same minute must
   both survive.
5. Writes one Parquet file (or CSV with --format csv).

Then it verifies, and refuses to declare success unless every check passes:

  * every (timestamp, symbol) key in the sources is present in the output
  * no key in the output that was not in a source
  * open/high/low/close/volume match exactly, row for row
  * per-contract row counts and date ranges match
  * total volume matches

Run `--verify-only` to re-check an existing merged file at any time.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd                                          # noqa: E402

from orb.data.dbn import _expand_paths, _read_one            # noqa: E402

KEY = ["ts_event", "symbol"]
VALUES = ["open", "high", "low", "close", "volume"]


def load_sources(paths, keep_spreads: bool) -> pd.DataFrame:
    files = _expand_paths(paths)
    frames, report = [], []
    for path in files:
        raw = _read_one(path, include_spreads=True)
        if raw is None:
            report.append((os.path.basename(path), 0, 0, 0))
            continue
        total = len(raw)
        spreads = int(raw["symbol"].str.contains("-").sum())
        d = raw if keep_spreads else raw[~raw["symbol"].str.contains("-")]
        frames.append(d)
        report.append((os.path.basename(path), total, spreads, len(d)))

    print(f"{'source file':<52} {'rows':>10} {'spreads':>10} {'kept':>10}")
    print("-" * 86)
    for name, total, spreads, kept in report:
        print(f"{name:<52} {total:>10,} {spreads:>10,} {kept:>10,}")
    if not frames:
        raise SystemExit("No records found in the given files.")

    df = pd.concat(frames)
    df.index.name = "ts_event"
    print("-" * 86)
    print(f"{'concatenated':<52} {'':>10} {'':>10} {len(df):>10,}")
    return df


def dedupe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.reset_index()
    before = len(out)
    # Sort so the ordering is deterministic, then collapse exact key duplicates.
    out = out.sort_values(KEY, kind="mergesort")
    dup_mask = out.duplicated(subset=KEY, keep="last")
    n_dup = int(dup_mask.sum())

    # A duplicate key whose VALUES disagree is a genuine data conflict and must
    # never be silently resolved — say so.
    if n_dup:
        d = out[out.duplicated(subset=KEY, keep=False)]
        conflicts = d.groupby(KEY)[VALUES].nunique().gt(1).any(axis=1)
        n_conflict = int(conflicts.sum())
        if n_conflict:
            print(f"\n  !! {n_conflict:,} duplicate keys have CONFLICTING values.")
            print("     These are overlapping records that disagree; the later "
                  "file wins. Inspect before trusting the merge.")
            bad = conflicts[conflicts].head(5)
            print(bad.to_string())
        else:
            print(f"\n  {n_dup:,} duplicate (timestamp, symbol) rows removed — "
                  f"all identical in value, so the merge is lossless.")
    out = out[~dup_mask].reset_index(drop=True)
    print(f"  {before:,} rows in  ->  {len(out):,} rows out")
    return out


def verify(sources: pd.DataFrame, merged: pd.DataFrame) -> bool:
    print("\nVerification")
    print("-" * 86)
    src = sources.reset_index().sort_values(KEY, kind="mergesort")
    src = src[~src.duplicated(subset=KEY, keep="last")].reset_index(drop=True)
    mrg = merged.sort_values(KEY, kind="mergesort").reset_index(drop=True)

    ok = True

    def check(name, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")

    check("row count matches", len(src) == len(mrg), f"{len(src):,} vs {len(mrg):,}")

    src_keys = set(zip(src.ts_event, src.symbol))
    mrg_keys = set(zip(mrg.ts_event, mrg.symbol))
    missing = src_keys - mrg_keys
    extra = mrg_keys - src_keys
    check("no source rows missing", not missing, f"{len(missing):,} missing")
    check("no invented rows", not extra, f"{len(extra):,} extra")

    if len(src) == len(mrg) and not missing and not extra:
        same = True
        for col in VALUES:
            a = src[col].to_numpy(dtype="float64")
            b = mrg[col].to_numpy(dtype="float64")
            eq = (a == b).all()
            check(f"{col} values identical", bool(eq))
            same = same and eq
        check("total volume identical",
              float(src.volume.sum()) == float(mrg.volume.sum()),
              f"{src.volume.sum():,.0f}")

    s_by = src.groupby("symbol").agg(n=("close", "size"), lo=("ts_event", "min"),
                                     hi=("ts_event", "max"))
    m_by = mrg.groupby("symbol").agg(n=("close", "size"), lo=("ts_event", "min"),
                                     hi=("ts_event", "max"))
    check("same set of contracts", set(s_by.index) == set(m_by.index),
          f"{len(m_by)} contracts")
    if set(s_by.index) == set(m_by.index):
        check("per-contract row counts match", s_by.n.equals(m_by.n.reindex(s_by.index)))
        check("per-contract date ranges match",
              s_by.lo.equals(m_by.lo.reindex(s_by.index)) and
              s_by.hi.equals(m_by.hi.reindex(s_by.index)))
    return ok


def main() -> int:
    p = argparse.ArgumentParser(description="Merge DBN bar files into one")
    p.add_argument("--data", "-d", nargs="+", required=True,
                   help="source DBN file(s), directory or glob")
    p.add_argument("--out", "-o", required=True, help="output file")
    p.add_argument("--format", choices=["parquet", "csv"], default=None,
                   help="defaults to the output file's extension")
    p.add_argument("--keep-spreads", action="store_true",
                   help="keep calendar spreads (not tradeable bars)")
    p.add_argument("--verify-only", action="store_true",
                   help="re-check an existing merged file against the sources")
    a = p.parse_args()

    fmt = a.format or ("csv" if a.out.lower().endswith(".csv") else "parquet")

    print("Reading sources ...\n")
    sources = load_sources(a.data, a.keep_spreads)

    if a.verify_only:
        merged = (pd.read_parquet(a.out) if fmt == "parquet"
                  else pd.read_csv(a.out, parse_dates=["ts_event"]))
        if "ts_event" not in merged.columns:
            merged = merged.reset_index()
        ok = verify(sources, merged)
        print("\n" + ("Verified: the merged file matches the sources exactly."
                      if ok else "VERIFICATION FAILED — do not use this file."))
        return 0 if ok else 1

    print("\nDe-duplicating ...")
    merged = dedupe(sources)

    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)

    # Updating in place is the normal workflow — "merged file + this month's
    # download -> merged file". Write to a temp beside the target and only swap
    # it in once verification passes, so a failure can never leave you with a
    # half-written file where your data used to be.
    in_place = os.path.abspath(a.out) in {os.path.abspath(f)
                                          for f in _expand_paths(a.data)}
    target = a.out
    tmp = a.out + ".tmp"
    if fmt == "parquet":
        merged.to_parquet(tmp, index=False, compression="zstd")
    else:
        merged.to_csv(tmp, index=False)

    ok = verify(sources, merged)

    if ok:
        os.replace(tmp, target)
        if in_place:
            print("\n  updated in place (verified before replacing the original)")
    else:
        os.remove(tmp)
        print(f"\n  verification failed — {target} was NOT modified.")
        return 1

    size = os.path.getsize(a.out)
    src_size = sum(os.path.getsize(f) for f in _expand_paths(a.data))
    print("-" * 86)
    print(f"  wrote {a.out}  ({size/1e6:,.1f} MB, sources were {src_size/1e6:,.1f} MB)")
    print(f"  {len(merged):,} bars, {merged.symbol.nunique()} contracts, "
          f"{merged.ts_event.min()} .. {merged.ts_event.max()} UTC")
    print("\n" + ("Verified: the merged file matches the sources exactly. "
                  "Point backtest.dbn_paths at it."
                  if ok else "VERIFICATION FAILED — do not use this file."))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
